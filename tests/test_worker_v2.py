"""Worker loop v2 (todo #21): tiers, escalation, AI review, spend caps, file locks.

One scenario test per row of SPEC "Ticket lifecycle" (``test_row_*``, named after the
diagram's edge), plus the prose rules under it (tier 3 fails → handed to you; the AI
reviewer's two kickbacks on tier 3 → the PR goes to you with the notes), invariant 7a,
crash safety through the new steps, pauses (spend caps, file locks), parallel tickets
in one repo, the CLI, and the kickback-retry prompt fix.

Everything runs against ``fakes.worker_env``: ``FakeGitHub`` linked to a real bare git
repo, ``FakeOpenCode`` behind the real OpenCode driver, ``FakeModels`` behind the real
proxy with the real spend caps, a real store. The fake router answers "small" unless a
test sets ``env.route_label``; the fake reviewer approves unless a test queues another
reply in ``env.review_replies``.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes.opencode import Script, bash, edit, say
from fakes.worker_env import (
    REPO,
    USER,
    Env,
    SimulatedCrash,
    failing,
    passing,
    request_changes,
)
from foreman_inator.config import SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.routing import escalation
from foreman_inator.tickets.prompt import Note, PriorAttempt, build_writer_prompt
from foreman_inator.tracing import capture_spans
from foreman_inator.worker_status import render_status

SNAPSHOTS = Path(__file__).parent / "snapshots" / "ticket_prompt"
LADDER = ["inkling-2-mini", "qwen3.8-27b", "glm", "claude-sonnet"]


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


@pytest.fixture
def spans():
    with capture_spans() as exporter:
        yield exporter


def pr_of(env: Env, n: int = 1):
    return env.gh.get_pr(REPO, env.ticket(n).pr_number)


def handover_comments(env: Env, n: int = 1) -> list[str]:
    return [c for c in env.issue_comments(n) if "key=handover-" in c]


def wait_idle(env: Env, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with env.worker._lock:
            if not env.worker._running:
                return
        time.sleep(0.05)
    raise AssertionError("jobs did not finish")


# =========================================================================== lifecycle rows


def test_row_issue_labeled_router_picks_the_start_tier(env: Env, spans) -> None:
    """Issue labeled → router picks: the classifier's "large" starts on tier 3 (GLM)."""
    env.route_label = "large"
    n = env.label()
    env.once(passing(model_call=False))
    t = env.ticket(n)
    assert env.route_calls == 1
    assert t.routed_tier == 3 and "classified the issue as large -> tier 3" in t.route_reason
    (a,) = env.attempts(n)
    assert (a.model, a.tier, a.trigger) == ("glm", 3, "first")
    routes = [s for s in spans.get_finished_spans() if s.name == "route"]
    assert len(routes) == 1 and routes[0].attributes["route.tier"] == 3


def test_row_issue_labeled_tier_label_overrides_the_router(env: Env) -> None:
    """Issue labeled with ``tier:2`` by an allowed user: no classifier call, tier 2."""
    n = env.label("Add a feature", "Please add feature().", "tier:2")
    env.once(passing(model_call=False))
    t = env.ticket(n)
    assert env.route_calls == 0
    assert t.routed_tier == 2 and t.route_reason.startswith("override: tier:2 label")
    assert [(a.model, a.tier) for a in env.attempts(n)] == [("qwen3.8-27b", 2)]


def test_row_writing_checks_fail_retry_or_escalate(env: Env) -> None:
    """Writing → checks fail: retry once on the tier with the output, then move up."""
    n = env.label()
    env.once(failing())
    env.once(failing())
    env.once(passing(model_call=False))
    attempts = env.attempts(n)
    assert [(a.tier, a.trigger, a.outcome) for a in attempts] == [
        (1, "first", "failed_checks"),
        (1, "first", "failed_checks"),
        (2, "escalation", None),
    ]
    assert "CHECK OUTPUT" in env.driver.prompts[1]
    assert env.driver.endpoints == LADDER[:1] * 2 + LADDER[1:2]
    assert env.ticket(n).column == "testing"


def test_row_checks_pass_ai_review(env: Env, spans) -> None:
    """Writing → checks pass → AI review by a tier 3 model of another family; the PR is
    a draft until the verdict, which is posted as the bot's COMMENT review."""
    n = env.label()
    env.once(passing())
    t = env.ticket(n)
    (review,) = env.reviews(n)
    assert review.reviewer_model == "claude-sonnet" and review.verdict == "approve"
    assert review.attempt_id == env.attempts(n)[0].id
    posts = env.gh.writes_named("post_review")
    marker = f"foreman-inator:ai-review review={review.id}"
    assert len(posts) == 1 and marker in posts[0].args["body"]
    (bot_review,) = env.bot_reviews(t.pr_number)
    assert bot_review.state == "COMMENTED"  # the bot never approves its own PR (7a)
    order = [c.method for c in env.gh.writes]
    assert order.index("create_draft_pr") < order.index("post_review")
    assert order.index("post_review") < order.index("mark_ready_for_review")
    mine = [s for s in spans.get_finished_spans() if s.attributes.get("ticket.id") == t.id]
    rspans = [s for s in mine if s.name == "review"]
    assert len(rspans) == 1 and rspans[0].attributes["review.verdict"] == "approve"
    assert {format(s.context.trace_id, "032x") for s in mine} == {t.trace_id}


def test_row_ai_kickback_max_two_then_writer_escalates(env: Env) -> None:
    """AI review → AI kickback (max 2) → writing: the first retries on the tier with the
    reviewer's note, the second moves the writer up one tier (``step.model``)."""
    env.review_replies.extend([request_changes("incomplete", "Handle zero."), request_changes()])
    n = env.label()
    env.once(passing())
    t = env.ticket(n)
    assert t.column == "in_progress" and pr_of(env, n).draft
    env.once(passing())
    assert env.ticket(n).column == "in_progress" and pr_of(env, n).draft
    env.once(passing())
    attempts = env.attempts(n)
    assert [(a.tier, a.trigger, a.outcome) for a in attempts] == [
        (1, "first", "kicked_back"),
        (1, "ai_kickback", "kicked_back"),
        (2, "escalation", None),
    ]
    kicks = env.store.list_kickbacks(ticket_id=t.id)
    assert [(k.by, k.attempt_id) for k in kicks] == [
        ("reviewer", attempts[0].id),
        ("reviewer", attempts[1].id),
    ]
    assert "Handle zero." in env.driver.prompts[1]
    assert env.driver.endpoints == ["inkling-2-mini", "inkling-2-mini", "qwen3.8-27b"]
    assert [r.verdict for r in env.reviews(n)] == ["request_changes"] * 2 + ["approve"]
    assert env.ticket(n).column == "testing" and not pr_of(env, n).draft
    assert len(env.gh.writes_named("post_review")) == 3
    assert len(env.gh.writes_named("create_draft_pr")) == 1  # one PR, force-pushed


def test_row_ai_approved_pr_marked_ready(env: Env) -> None:
    """AI review → approved → your review: the PR is marked ready, its body carries the
    AI review section from the stored row."""
    n = env.label()
    env.once(passing())
    pr = pr_of(env, n)
    assert not pr.draft and env.ticket(n).column == "testing"
    assert env.ticket(n).flag_reason is None
    assert "## AI review" in pr.body and "**Approved** by `claude-sonnet`" in pr.body
    assert len(env.gh.writes_named("mark_ready_for_review")) == 1
    assert env.gh.ready_violations == [] and env.gh.ready_checks == 1


def test_row_your_kickback_request_changes_back_to_writing(env: Env) -> None:
    """Your review → request changes + comment → writing: a kickback row by the user with
    the label's tag, the next attempt on the same tier with your note, reviewed again."""
    n = env.label()
    env.once(passing())
    pr = env.ticket(n).pr_number
    env.gh.apply_label(REPO, pr, "kickback:style", by=USER)
    env.gh.submit_review(REPO, pr, by=USER, state="REQUEST_CHANGES", body="Use snake_case.")
    env.once(passing("def feature_one():\n    return 1\n"))
    a1, a2 = env.attempts(n)
    assert a1.outcome == "kicked_back" and (a2.trigger, a2.tier) == ("kickback", 1)
    (k,) = env.store.list_kickbacks(ticket_id=env.ticket(n).id)
    assert (k.by, k.tag, k.note, k.attempt_id) == ("user", "style", "Use snake_case.", a1.id)
    assert "Use snake_case." in env.driver.prompts[1]
    assert [r.attempt_id for r in env.reviews(n)] == [a1.id, a2.id]
    assert env.ticket(n).column == "testing"


def test_row_approve_merge_issue_closed(env: Env) -> None:
    """Your review → approve + merge → merged: accept recorded, issue closed, one summary
    comment, file locks released."""
    n = env.label("Fix it", "Change `feature.py`.")
    env.once(passing())
    t = env.ticket(n)
    assert env.store.list_locks(ticket_id=t.id)
    env.gh.submit_review(REPO, t.pr_number, by=USER, state="APPROVE")
    env.gh.merge_pr(REPO, t.pr_number, by=USER)
    env.once()
    env.once()
    t = env.ticket(n)
    assert t.column == "done" and t.accepted_attempt_id == env.attempts(n)[0].id
    assert env.gh.get_issue(REPO, n).state == "closed"
    summaries = [c for c in env.issue_comments(n) if "foreman-inator:summary" in c]
    assert len(summaries) == 1 and "foreman-inator summary:" in summaries[0]
    assert not env.store.list_locks(ticket_id=t.id)


def test_row_tier3_failure_handed_to_user(env: Env) -> None:
    """ "Tier 3 fails → the ticket stays in progress and is flagged for you": every rung
    of the ladder (tier 1, 2, GLM, Claude) fails its checks twice, then the user."""
    n = env.label()
    for _ in range(len(LADDER) * 2 + 1):
        env.once(failing())
    attempts = env.attempts(n)
    assert [a.model for a in attempts] == [m for m in LADDER for _ in (0, 1)]
    assert all(a.outcome == "failed_checks" for a in attempts)
    t = env.ticket(n)
    assert t.column == "in_progress" and t.flag_reason.startswith("checks_failed")
    assert "top of the ladder" in t.flag_reason
    (comment,) = handover_comments(env, n)
    assert "Why:" in comment and "checks failed" in comment
    env.once(failing())
    assert len(env.attempts(n)) == len(LADDER) * 2  # invariant 7e: nothing more runs
    assert len(handover_comments(env, n)) == 1


def test_ai_kickbacks_on_tier3_hand_the_pr_to_you_with_the_notes(env: Env) -> None:
    """ "If tier 3 is already writing, the PR goes to you with the reviewer's notes": two
    AI kickbacks on GLM move to Claude, two more there hand over and mark the PR ready."""
    env.review_replies.extend(request_changes(note=f"Missing case {i}.") for i in range(4))
    n = env.label("Big change", "Rework it.", "tier:3")
    for _ in range(5):
        env.once(passing(model_call=False))
    attempts = env.attempts(n)
    assert [(a.model, a.trigger) for a in attempts] == [
        ("glm", "first"),
        ("glm", "ai_kickback"),
        ("claude-sonnet", "escalation"),
        ("claude-sonnet", "ai_kickback"),
    ]
    assert [r.reviewer_model for r in env.reviews(n)] == ["claude-sonnet"] * 2 + ["glm"] * 2
    t = env.ticket(n)
    assert t.flag_reason.startswith(escalation.FLAG_AI_KICKBACK_CAP)
    pr = pr_of(env, n)
    assert not pr.draft and "## Handed to you" in pr.body and "Missing case 3." in pr.body
    (comment,) = handover_comments(env, n)
    assert f"Pull request #{pr.number} is marked ready" in comment
    assert env.gh.ready_violations == []
    # Every GitHub write happened inside a github span.
    assert env.gh.write_spans and all(name == "github" for _, name in env.gh.write_spans)


# =========================================================================== invariant 7a


def test_pr_never_marked_ready_before_an_approve_or_a_handover(tmp_path: Path) -> None:
    """Invariant 7a: at every "mark ready", the attempt on the PR has a stored AI approve
    or a hand-over comment is already on the issue. Kickbacks keep the PR a draft."""
    e = Env(tmp_path / "a")
    try:
        e.review_replies.extend([request_changes(), request_changes()])
        e.label()
        for _ in range(2):
            e.once(passing(model_call=False))
            assert pr_of(e).draft and not e.gh.writes_named("mark_ready_for_review")
        e.once(passing(model_call=False))
        assert not pr_of(e).draft
        assert e.gh.ready_checks == 1 and e.gh.ready_violations == []
    finally:
        e.close()
    e = Env(tmp_path / "b")
    try:
        e.label("Typo", "Fix it.", "review:skip")
        e.once(passing(model_call=False))
        assert not pr_of(e).draft and e.review_calls == 0
        assert e.gh.ready_checks == 1 and e.gh.ready_violations == []
    finally:
        e.close()


def test_review_skip_label_hands_over_without_a_review(env: Env) -> None:
    n = env.label("Typo", "Fix the typo.", "review:skip")
    env.once(passing(model_call=False))
    t = env.ticket(n)
    assert not env.reviews(n) and env.review_calls == 0
    assert t.column == "testing" and t.flag_reason.startswith("review_skipped")
    pr = pr_of(env, n)
    assert not pr.draft and "## Handed to you" in pr.body
    (comment,) = handover_comments(env, n)
    assert "review:skip" in comment
    env.once()
    assert len(handover_comments(env, n)) == 1 and not env.reviews(n)


def test_review_skip_by_a_stranger_is_ignored(env: Env) -> None:
    n = env.gh.create_issue(REPO, "Typo", "Fix it.", author="stranger")
    env.gh.apply_label(REPO, n, "review:skip", by="stranger")
    env.gh.apply_label(REPO, n, "agent", by=USER)
    env.once(passing(model_call=False))
    assert [r.verdict for r in env.reviews(n)] == ["approve"]  # invariant 7b
    assert env.ticket(n).flag_reason is None


def test_no_independent_reviewer_hands_over(tmp_path: Path) -> None:
    e = Env(tmp_path, reviewer_models=("glm",))
    try:
        n = e.label("Big", "Rework it.", "tier:3")  # writer glm, the only reviewer is glm
        e.once(passing(model_call=False))
        t = e.ticket(n)
        assert t.flag_reason.startswith("no_reviewer") and not e.reviews(n)
        assert not pr_of(e, n).draft and e.gh.ready_violations == []
        (comment,) = handover_comments(e, n)
        assert "different model family" in comment
    finally:
        e.close()


def test_reviewer_without_a_verdict_hands_over(env: Env) -> None:
    env.review_replies.extend(["looks fine to me", "still not json"])
    n = env.label()
    env.once(passing(model_call=False))
    (review,) = env.reviews(n)
    assert review.verdict == "handed_to_user"
    t = env.ticket(n)
    assert t.flag_reason.startswith("reviewer_handover") and not pr_of(env, n).draft
    assert len(handover_comments(env, n)) == 1 and env.gh.ready_violations == []
    assert len(env.bot_reviews(t.pr_number)) == 1


# =========================================================================== crash safety


def test_crash_during_routing_routes_again(env: Env) -> None:
    def crash(t, *, override=None):
        raise SimulatedCrash("killed while routing")

    env.worker._route_ticket = crash  # type: ignore[method-assign]
    n = env.label()
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    assert env.ticket(n).routed_tier is None and not env.attempts(n)
    env.restart()
    env.once()
    assert env.ticket(n).routed_tier == 1 and env.route_calls == 1
    assert len(env.attempts(n)) == 1 and env.ticket(n).column == "testing"


def test_crash_mid_review_reruns_it_without_a_second_comment(env: Env) -> None:
    def crash(**kw):
        raise SimulatedCrash("killed during the AI review")

    env.worker._launch_review = crash  # type: ignore[method-assign]
    n = env.label()
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    t = env.ticket(n)
    assert t.column == "testing" and not env.reviews(n) and pr_of(env, n).draft
    assert env.restart() == []
    env.once()
    assert [r.verdict for r in env.reviews(n)] == ["approve"]
    assert len(env.gh.writes_named("post_review")) == 1 and not pr_of(env, n).draft
    assert len(env.attempts(n)) == 1 and len(env.gh.writes_named("create_draft_pr")) == 1


def test_crash_between_review_row_and_comment(env: Env) -> None:
    env.gh.fail_next("post_review", SimulatedCrash("killed before posting the review"))
    n = env.label()
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    assert len(env.reviews(n)) == 1 and not env.bot_reviews(env.ticket(n).pr_number)
    env.restart()
    env.once()
    env.once()
    assert len(env.reviews(n)) == 1 and env.review_calls == 1
    assert len(env.bot_reviews(env.ticket(n).pr_number)) == 1 and not pr_of(env, n).draft


def test_crash_between_review_row_and_marking_ready(env: Env) -> None:
    env.gh.fail_next("mark_ready_for_review", SimulatedCrash("killed before marking ready"))
    n = env.label()
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    assert len(env.reviews(n)) == 1 and len(env.bot_reviews(env.ticket(n).pr_number)) == 1
    assert pr_of(env, n).draft
    env.restart()
    env.once()
    env.once()
    assert not pr_of(env, n).draft and len(env.reviews(n)) == 1
    assert len(env.gh.writes_named("post_review")) == 1  # found by its marker, not reposted
    assert env.gh.ready_violations == []


def test_crash_during_a_handover_finishes_it_once(env: Env) -> None:
    env.gh.fail_next("mark_ready_for_review", SimulatedCrash("killed mid hand-over"))
    n = env.label("Typo", "Fix it.", "review:skip")
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    assert env.ticket(n).flag_reason is None  # the flag is stored last
    assert len(handover_comments(env, n)) == 1
    env.restart()
    env.once()
    t = env.ticket(n)
    assert t.flag_reason.startswith("review_skipped") and not pr_of(env, n).draft
    assert len(handover_comments(env, n)) == 1


def test_shutdown_mid_review_stores_nothing_and_reviews_again(env: Env) -> None:
    real = env.worker._launch_review
    started = threading.Event()

    def slow(**kw):
        started.set()
        kw["cancel"].wait(20)
        return real(**kw)

    env.worker._launch_review = slow  # type: ignore[method-assign]
    env.oc.set_script(passing(model_call=False))
    n = env.label()
    env.worker.poll_and_handle()
    env.worker.dispatch(inline=False)
    assert started.wait(30)
    env.worker.shutdown(timeout=30)
    t = env.ticket(n)
    assert t.column == "testing" and t.flag_reason is None and not env.reviews(n)
    assert not env.gh.writes_named("post_review")
    env.restart()
    env.once()
    assert [r.verdict for r in env.reviews(n)] == ["approve"] and not pr_of(env, n).draft


# =========================================================================== spend pauses


def test_spend_cap_pauses_a_cloud_attempt_until_approved(tmp_path: Path) -> None:
    e = Env(tmp_path, caps=SpendCaps(daily_usd=0.0))
    try:
        n = e.label("Big", "Rework it.", "tier:3")
        e.once(passing())
        t = e.ticket(n)
        assert t.flag_reason.startswith("spend_cap") and not e.attempts(n)
        (ask,) = [c for c in e.issue_comments(n) if "approve-spend" in c]
        assert f"fman approve-spend {t.id}" in ask
        e.once()
        assert not e.attempts(n) and len(e.issue_comments(n)) == 1
        assert "WAITING: spend approval" in render_status(e.store, config=e.config)
        e.spend.approve(t.id)
        e.once(passing())
        t = e.ticket(n)
        (a,) = e.attempts(n)
        assert (a.model, a.outcome, a.tests_passed) == ("glm", None, True)
        assert t.flag_reason is None and t.column == "testing" and not pr_of(e, n).draft
        since = datetime(2000, 1, 1, tzinfo=UTC)
        assert e.store.spend_total("live", since=since) > 0  # the grant paid writer + review
        assert all(name == "github" for _, name in e.gh.write_spans)
    finally:
        e.close()


def test_spend_refusal_mid_attempt_is_not_a_failed_attempt(tmp_path: Path) -> None:
    e = Env(tmp_path, caps=SpendCaps(daily_usd=0.000001), approval_usd=5.0)
    try:
        n = e.label("Big", "Rework it.", "tier:3")
        e.once(passing())
        t = e.ticket(n)
        (a1,) = e.attempts(n)
        assert a1.outcome == "superseded" and t.flag_reason.startswith("spend_cap")
        assert e.worker.uncounted_attempts(t) == {a1.id}
        assert not e.worker.history(t).attempts  # never counted, not even as interrupted
        e.spend.approve(t.id)
        e.once(passing())
        a1, a2 = e.attempts(n)
        assert (a2.trigger, a2.model, a2.outcome) == ("first", "glm", None)
        assert e.ticket(n).column == "testing" and not pr_of(e, n).draft
    finally:
        e.close()


def test_spend_refusal_during_review_pauses_the_review(tmp_path: Path) -> None:
    e = Env(tmp_path, caps=SpendCaps(daily_usd=0.000001), approval_usd=5.0)
    try:
        n = e.label()  # tier 1, local writer: only the cloud reviewer hits the cap
        e.once(passing(model_call=False))
        t = e.ticket(n)
        assert t.column == "testing" and t.flag_reason.startswith("spend_cap")
        assert not e.reviews(n) and pr_of(e, n).draft and e.review_calls == 0
        e.spend.approve(t.id)
        e.once()
        assert [r.verdict for r in e.reviews(n)] == ["approve"] and not pr_of(e, n).draft
        assert len(e.attempts(n)) == 1 and e.review_calls == 1
    finally:
        e.close()


# =========================================================================== locks + parallel


def test_ticket_waits_on_another_tickets_file_lock(tmp_path: Path) -> None:
    e = Env(tmp_path, parallel=2)
    try:
        a = e.label("First", "Change `feature.py`.")
        b = e.label("Second", "Also change `feature.py`.")
        e.once(passing(model_call=False))
        assert e.ticket(a).column == "testing"
        tb = e.ticket(b)
        assert tb.routed_tier == 1 and not e.attempts(b) and tb.flag_reason is None
        text = render_status(e.store, config=e.config)
        assert f"WAITING: on files held by ticket(s) #{e.ticket(a).id}" in text
        e.once()
        assert not e.attempts(b)
        e.gh.merge_pr(REPO, e.ticket(a).pr_number, by=USER)
        e.once(passing("def feature():\n    return 2\n", model_call=False))
        assert e.ticket(a).column == "done"
        assert len(e.attempts(b)) == 1 and e.ticket(b).column == "testing"
    finally:
        e.close()


def test_lock_conflict_after_an_attempt_pauses_its_publish(tmp_path: Path) -> None:
    """Two issues that name no files both edit ``feature.py``: the second is paused
    before it publishes; when the first merges, it starts again from the new base."""
    e = Env(tmp_path, parallel=2)
    try:
        a = e.label("First", "Do the first thing.")
        b = e.label("Second", "Do the second thing.")
        e.once(passing(model_call=False))
        (b1,) = e.attempts(b)
        assert b1.tests_passed and b1.outcome is None and e.ticket(b).column == "in_progress"
        assert e.ticket(b).pr_number is None
        e.once()
        assert len(e.attempts(b)) == 1 and e.ticket(b).pr_number is None
        e.gh.merge_pr(REPO, e.ticket(a).pr_number, by=USER)
        e.once()
        e.once()
        b1, b2 = e.attempts(b)
        assert b1.outcome == "superseded" and (b2.trigger, b2.outcome) == ("first", None)
        assert e.worker.uncounted_attempts(e.ticket(b)) == {b1.id}
        assert e.ticket(b).column == "testing" and e.ticket(b).pr_number is not None
    finally:
        e.close()


def test_lock_conflict_with_a_dropped_ticket_publishes_the_kept_diff(tmp_path: Path) -> None:
    e = Env(tmp_path, parallel=2)
    try:
        a = e.label("First", "Do the first thing.")
        b = e.label("Second", "Do the second thing.")
        e.once(passing(model_call=False))
        e.gh.close_issue_as(REPO, a, by=USER)
        e.once()
        assert e.ticket(a).column == "dropped"
        (b1,) = e.attempts(b)
        assert e.ticket(b).column == "testing" and b1.outcome is None
    finally:
        e.close()


def test_parallel_tickets_in_one_repo_run_at_once(tmp_path: Path) -> None:
    e = Env(tmp_path, parallel=2)
    try:
        e.store.set_verify_command(REPO, "ls mod_*.py")
        e.oc.set_script(
            Script(
                [
                    bash('n=$(basename "$PWD" | cut -d- -f2); echo "x = $n" > "mod_$n.py"'),
                    say("Done."),
                ]
            )
        )
        e.driver.barrier = threading.Barrier(2)  # both attempts must be running together
        a = e.label("First", "Touch `a.py`.")
        b = e.label("Second", "Touch `b.py`.")
        e.worker.poll_and_handle()
        e.worker.dispatch(inline=False)
        wait_idle(e)
        assert e.driver.max_in_flight == 2
        for n in (a, b):
            assert e.ticket(n).column == "testing" and not pr_of(e, n).draft
    finally:
        e.close()


def test_default_cap_runs_one_ticket_per_repo(env: Env) -> None:
    env.label("First", "Touch `a.py`.")
    env.label("Second", "Touch `b.py`.")
    env.oc.set_script(passing(model_call=False))
    env.driver.gate = threading.Event()  # hold the first attempt while we look
    env.worker.poll_and_handle()
    env.worker.dispatch(inline=False)
    deadline = time.monotonic() + 20
    while not env.driver.in_flight and time.monotonic() < deadline:
        time.sleep(0.05)
    env.worker.dispatch(inline=False)  # the repo's one slot is taken: nothing more starts
    with env.worker._lock:
        assert len(env.worker._running) == 1
    env.driver.gate.set()
    wait_idle(env)
    assert env.driver.max_in_flight == 1


# =========================================================================== prompt fix


def test_kickback_retry_prompt_has_the_note_and_the_check_output() -> None:
    """A kickback-triggered attempt that failed its checks is retried with its trigger
    kept: the retry's prompt shows the note and the check output (#21's prompt fix)."""
    from test_ticket_prompt import FAILED_OUTPUT, REVIEWER_NOTE, attempt, build, ticket

    history = (
        PriorAttempt(
            attempt(1, outcome="kicked_back"), notes=(Note("reviewer", "broke", REVIEWER_NOTE),)
        ),
        PriorAttempt(
            attempt(2, "ai_kickback", outcome="failed_checks"), failure_output=FAILED_OUTPUT
        ),
    )
    wp = build(ticket(), attempt(3, "ai_kickback"), history)
    assert wp.situation == "kickback"
    assert REVIEWER_NOTE in wp.text and "IndexError: list index out of range" in wp.text
    assert "BEGIN CHECK OUTPUT" in wp.text
    path = SNAPSHOTS / "kickback_retry_failed_checks.txt"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.write_text(wp.text, encoding="utf-8")
    assert wp.text == path.read_text(encoding="utf-8")
    assert build_writer_prompt  # the builder under test


def test_kickback_retry_after_failed_checks_in_the_loop(env: Env) -> None:
    env.review_replies.append(request_changes("broke", "Zero breaks it."))
    n = env.label()
    env.once(passing())
    env.once(Script([edit("status.txt", "fail\n"), say("Tried.")]))
    env.once(passing())
    a1, a2, a3 = env.attempts(n)
    assert [a.trigger for a in (a1, a2, a3)] == ["first", "ai_kickback", "ai_kickback"]
    assert a2.outcome == "failed_checks"
    assert "Zero breaks it." in env.driver.prompts[2] and "CHECK OUTPUT" in env.driver.prompts[2]


# =========================================================================== CLI + status


def test_cli_approve_spend(foreman_home: Path) -> None:
    from foreman_inator import paths
    from foreman_inator.cli import app

    runner = CliRunner()
    with Store(paths.db_path()) as s:
        t = s.create_ticket(repo=REPO, issue_number=3, title="Big", request="", base_branch="main")
    res = runner.invoke(app, ["approve-spend", str(t.id)])
    assert res.exit_code == 0, res.output
    assert f"ticket #{t.id}" in res.output and "$5.00" in res.output
    with Store(paths.db_path(), read_only=True) as s:
        assert s.open_spend_approval(t.id) is not None
    res = runner.invoke(app, ["approve-spend", "999"])
    assert res.exit_code == 1 and "no ticket 999" in res.output


def test_cli_metrics_serves_on_loopback(foreman_home: Path, monkeypatch) -> None:
    from foreman_inator import paths
    from foreman_inator.cli import app
    from foreman_inator.metrics import web

    calls = []
    monkeypatch.setattr(web, "serve", lambda path, *, port: calls.append((path, port)))
    runner = CliRunner()
    assert runner.invoke(app, ["metrics"]).exit_code == 1  # no database yet
    Store(paths.db_path()).close()
    res = runner.invoke(app, ["metrics", "--port", "9911"])
    assert res.exit_code == 0, res.output
    assert "http://127.0.0.1:9911/" in res.output and calls == [(paths.db_path(), 9911)]


def test_cli_status_shows_the_spend_line(foreman_home: Path) -> None:
    from foreman_inator import paths
    from foreman_inator.cli import app

    with Store(paths.db_path()) as s:
        t = s.create_ticket(repo=REPO, issue_number=7, title="Big", request="", base_branch="main")
        s.flag_ticket(t.id, "spend_cap: daily cap $5.00 reached")
    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert f"run `fman approve-spend {t.id}`" in res.output
    assert "cloud spend: $0.00 of $5.00 today" in res.output


def test_opencode_warm_up_starts_and_stops_one_server(env: Env) -> None:
    from foreman_inator.runner.opencode import OpenCodeDriver
    from foreman_inator.worker_run import warm_up_opencode

    driver = env.oc.driver(timeout=30)
    assert warm_up_opencode(driver, env.config, env.proxy)
    (launch,) = env.oc.launches()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(launch["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the warm-up server is still running")
    assert not list((driver.state_root / "attempts").glob("warm-up-*"))
    broken = OpenCodeDriver(opencode_bin=str(env.tmp / "no-such-opencode"), state_root=env.tmp)
    assert warm_up_opencode(broken, env.config, env.proxy) is False  # logged, never raised
