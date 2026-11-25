"""Manual retry of a flagged or failed ticket (todo #79, issue #73): ``foreman_inator.retry``.

Store level: ``retry`` clears the flag, keeps the attempt rows and records one retry mark;
``with_manual_retry`` (the hook ``Worker.policy_for`` gets in the wiring step) forces one
attempt on the chosen rung and then caps the round. Refusals change nothing. End to end
(``fakes.worker_env``, the hook patched into ``Worker.policy_for`` as the wiring will do):
a ``checks_failed`` ticket runs exactly one more attempt and is handed over again.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes.github import FakeGitHub
from fakes.worker_env import REPO, Env, failing
from foreman_inator import retry as rt
from foreman_inator.config import Config, SpendCaps, local_tier2_models, qwen_27b_local
from foreman_inator.db.store import Store, Ticket
from foreman_inator.routing import escalation as esc
from foreman_inator.worker import Worker
from foreman_inator.worker_actions import flag_comment

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
CFG = Config()  # default ladder: inkling (1, local), glm (2, cloud), opus, fable (3, cloud)
LADDER = esc.build_ladder(CFG.models)


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


def flagged(store: Store, flag: str, fails: list[tuple[int, str]], *, n: int = 1) -> Ticket:
    """A ticket on issue ``n`` with failed-checks attempts on (tier, model), then ``flag``."""
    t = store.create_ticket(repo="me/toy", issue_number=n, title="t", request="r",
                            base_branch="main", routed_tier=1)  # fmt: skip
    store.set_column(t.id, "in_progress")
    for i, (tier, model) in enumerate(fails):
        a = store.add_attempt(t.id, model=model, tier=tier, trigger="first" if i == 0 else
                              "escalation", branch=f"fman/issue-{n}-a{i + 1}")  # fmt: skip
        store.finish_attempt(a.id, "failed_checks")
    return store.flag_ticket(t.id, flag)


def decide(store: Store, t: Ticket) -> esc.Step | None:
    """What the wired ``Worker.policy_for`` decides: escalation, then the retry mark."""
    t = store.get_ticket(t.id)
    h = esc.History(rt.counted_attempts(store, t), routed_tier=t.routed_tier)
    return rt.with_manual_retry(store, t, h, esc.next_step(h, LADDER))


def fail_next(store: Store, t: Ticket, step: esc.Step | None) -> None:
    assert isinstance(step, esc.Retry), step
    a = store.add_attempt(t.id, model=step.model or "?", tier=step.tier, trigger=step.trigger)
    store.finish_attempt(a.id, "failed_checks")


TOP = [(1, "inkling-2-mini")] * 2 + [(2, "glm")] * 2 + [(3, "claude-opus")] * 2
TOP += [(3, "claude-fable")] * 2


# --------------------------------------------------------------------------- the retry


def test_checks_failed_retry_clears_the_flag_keeps_attempts_and_runs_once(store):
    t = flagged(store, "checks_failed: the checks failed 2 times in a row", TOP)
    assert isinstance(esc.next_step(esc.History(tuple(store.list_attempts(t.id))), LADDER),
                      esc.HandToUser)  # fmt: skip
    res = rt.retry(store, CFG, f"me/toy#{t.issue_number}", now=NOW)
    assert res.status == "queued" and res.ok
    assert (res.tier, res.model) == (3, "claude-fable")
    after = store.get_ticket(t.id)
    assert after.flag_reason is None and after.column == "in_progress"
    assert len(store.list_attempts(t.id)) == 8 and len(store.tickets_for_issue("me/toy", 1)) == 1
    (mark,) = rt.retry_marks(store, after)
    assert mark.flag.startswith("checks_failed") and mark.after_attempt_id == 8
    step = decide(store, after)
    assert isinstance(step, esc.Retry) and not isinstance(step, esc.Escalate)
    assert (step.tier, step.model, step.trigger) == (3, "claude-fable", "escalation")
    assert "manual retry" in step.reason
    fail_next(store, after, step)
    again = decide(store, after)  # no reset: the stint's counts carry on, so it hands over
    assert isinstance(again, esc.HandToUser) and again.flag == "checks_failed"
    assert rt.manual_retry_attempts(store, after) == {9}


def test_two_clicks_queue_it_once(store):
    t = flagged(store, "checks_failed: x", TOP)
    first = rt.retry(store, CFG, t.id, now=NOW)
    second = rt.retry(store, CFG, t.id, tier="up", now=NOW)
    assert first.status == "queued"
    assert second.status == "already_queued" and second.ok
    assert (second.tier, second.model) == (first.tier, first.model)
    assert len(rt.retry_marks(store, store.get_ticket(t.id))) == 1
    fail_next(store, t, decide(store, t))  # the one forced attempt
    assert isinstance(decide(store, t), esc.HandToUser)  # not a second forced one
    assert len(store.list_attempts(t.id)) == 9


def test_up_moves_one_rung_higher(store):
    t = flagged(store, "interrupted: 4 attempts were cut short", [(1, "inkling-2-mini")])
    res = rt.retry(store, CFG, t.id, tier="up", now=NOW)
    assert res.status == "queued" and (res.tier, res.model) == (2, "glm")
    step = decide(store, t)
    assert isinstance(step, esc.Escalate) and (step.tier, step.model) == (2, "glm")


def test_tier_n_picks_that_tier(store):
    t = flagged(store, "attempt_budget: spent $5.00", [(1, "inkling-2-mini")])
    res = rt.retry(store, CFG, t.id, tier=3, now=NOW)
    assert (res.tier, res.model) == (3, "claude-opus")
    two = SimpleNamespace(models=CFG.models[:2])  # a config with no tier 3 model
    other = flagged(store, "checks_failed: x", [(1, "inkling-2-mini")], n=2)
    res = rt.retry(store, two, other.id, tier=3, now=NOW)
    assert res.reason == "no writer model is configured on tier 3"


def test_the_round_is_capped_and_never_escalates(store):
    t = flagged(store, "interrupted: x", [(1, "inkling-2-mini")])
    rt.retry(store, CFG, t.id, tier="up", now=NOW)
    fail_next(store, t, decide(store, t))  # forced attempt on glm
    step = decide(store, t)
    assert isinstance(step, esc.Retry) and step.model == "glm"  # same-rung retry, in round
    fail_next(store, t, step)
    end = decide(store, t)  # escalation would move up to tier 3: the round ends instead
    assert isinstance(end, esc.HandToUser) and end.flag == rt.FLAG_RETRY_ROUND


def test_bound_stays_finite_with_every_retry_used(store):
    """7e: a ticket that always fails, retried by hand whenever it is flagged, ends flagged
    within ``retry_attempt_bound`` attempts, and the last retry is refused."""
    t = flagged(store, "checks_failed: x", [])
    store.clear_flag(t.id)
    choices = ["up", 1, "same"]
    refused = None
    for _ in range(200):
        step = decide(store, t)
        if isinstance(step, esc.HandToUser):
            store.flag_ticket(t.id, step.reason)
            res = rt.retry(store, CFG, t.id, tier=choices[len(rt.retry_marks(store, t)) % 3],
                           now=NOW)  # fmt: skip
            if "top tier" in res.reason:
                res = rt.retry(store, CFG, t.id, now=NOW)
            if not res.ok:
                refused = res
                break
            continue
        fail_next(store, t, step)
    assert refused is not None and "retried 3 times" in refused.reason
    n = len(store.list_attempts(t.id))
    assert n <= rt.retry_attempt_bound(len(LADDER))
    assert n > 2 * len(LADDER) + 2  # escalation's own run, then the retries' attempts


def test_publish_failed_retry_publishes_without_a_new_attempt(store):
    t = flagged(store, "publish_failed: 502", [])
    a = store.add_attempt(t.id, model="inkling-2-mini", tier=1, trigger="first")
    store.finish_attempt(a.id, None, tests_passed=True)
    assert rt.retry(store, CFG, t.id, tier="up", now=NOW).status == "refused"
    assert rt.retry(store, CFG, t.id, now=NOW).status == "queued"
    assert decide(store, t) is None  # nothing to start: the queue publishes the attempt
    store.set_attempt_outcome(a.id, "kicked_back")  # later review flow is escalation's own
    step = decide(store, t)
    assert isinstance(step, esc.Retry) and step.trigger == "kickback"


# --------------------------------------------------------------------------- refusals


def unchanged(store: Store, t: Ticket) -> None:
    now = store.get_ticket(t.id)
    assert now.flag_reason == t.flag_reason and now.column == t.column
    assert rt.retry_marks(store, now) == []


def test_spend_cap_is_refused_while_the_cap_is_reached(store):
    cfg = Config(spend_caps=SpendCaps(daily_usd=1.0, monthly_usd=100.0))
    t = flagged(store, "spend_cap: daily cap $1.00 reached", [(3, "claude-opus")])
    store.record_spend("live", 1.0, model="claude-opus", ticket_id=t.id)
    res = rt.retry(store, cfg, t.id, now=NOW)
    assert res.status == "refused"
    assert "daily spend cap is still reached ($1.00 of $1.00)" in res.reason
    assert f"fman approve-spend {t.id}" in res.reason
    unchanged(store, t)
    assert rt.retry(store, Config(), t.id, now=NOW).status == "queued"  # cap clear


def test_thrifty_pause_needs_approve_spend(store):
    t = flagged(store, "spend_cap: thrifty profile: a cloud writer attempt waits", [])
    res = rt.retry(store, CFG, t.id, now=NOW)
    assert res.status == "refused" and "approve-spend" in res.reason
    unchanged(store, t)


def test_local_only_repo_cannot_move_to_a_cloud_tier(store):
    t = flagged(store, "local_only: checks_failed: x", [(1, "inkling-2-mini")] * 2)
    store.set_cloud("me/toy", False)
    res = rt.retry(store, CFG, t.id, tier="up", now=NOW)
    assert res.status == "refused" and "local only (cloud = false)" in res.reason
    assert "glm" in res.reason
    unchanged(store, t)
    assert rt.retry(store, CFG, t.id, now=NOW).status == "queued"  # same local tier is fine


@pytest.mark.parametrize(
    ("flag", "words"),
    [
        ("no_verify_command: none found", "fman repo set-verify"),
        ("split_proposed: 3 parts", "tier:N"),
        ("reviewer_handover: x", "remove the `agent` label"),
    ],
)
def test_flags_a_retry_does_not_lift(store, flag, words):
    t = flagged(store, flag, [])
    res = rt.retry(store, CFG, t.id, now=NOW)
    assert res.status == "refused" and words in res.reason
    unchanged(store, t)


def test_other_refusals(store):
    assert rt.retry(store, CFG, 99, now=NOW).reason == "no ticket 99"
    assert "not a ticket id" in rt.retry(store, CFG, "nonsense", now=NOW).reason
    assert rt.retry(store, CFG, "me/toy#5", now=NOW).reason == "no ticket for me/toy#5"
    t = flagged(store, "checks_failed: x", TOP)
    store.clear_flag(t.id)
    assert "not flagged" in rt.retry(store, CFG, t.id, now=NOW).reason
    top = flagged(store, "checks_failed: x", TOP, n=2)
    assert "is the top tier" in rt.retry(store, CFG, top.id, tier="up", now=NOW).reason
    unchanged(store, top)
    store.set_column(top.id, "testing")
    assert "with you for review" in rt.retry(store, CFG, top.id, now=NOW).reason
    store.set_column(top.id, "done")
    assert "merged" in rt.retry(store, CFG, top.id, now=NOW).reason


# --------------------------------------------------------------------------- dropped


def dropped(store: Store, gh: FakeGitHub, *, label: bool = True) -> Ticket:
    gh.create_repo("me/toy")
    n = gh.create_issue("me/toy", "t", "r", labels=["agent"] if label else [])
    t = flagged(store, "checks_failed: x", [(1, "inkling-2-mini")], n=n)
    store.clear_flag(t.id)
    return store.set_column(t.id, "dropped")


def test_dropped_ticket_with_the_agent_label_goes_back_in_the_queue(store):
    gh = FakeGitHub()
    t = dropped(store, gh)
    assert "needs GitHub" in rt.retry(store, CFG, t.id, now=NOW).reason
    res = rt.retry(store, CFG, f"me/toy#{t.issue_number}", now=NOW, api=gh)
    assert res.status == "queued" and "put back in the queue" in res.reason
    back = store.get_ticket(t.id)
    assert back.column == "in_progress" and back.done_at is None
    assert store.find_open_ticket("me/toy", t.issue_number).id == t.id
    assert rt.retry(store, CFG, t.id, now=NOW, api=gh).status == "already_queued"
    step = decide(store, back)
    assert isinstance(step, esc.Retry) and step.model == "inkling-2-mini"


def test_dropped_ticket_without_the_label_or_with_a_closed_issue_is_refused(store):
    gh = FakeGitHub()
    t = dropped(store, gh, label=False)
    assert "no longer has the `agent` label" in rt.retry(store, CFG, t.id, now=NOW, api=gh).reason
    assert store.get_ticket(t.id).column == "dropped"
    gh.apply_label("me/toy", t.issue_number, "agent", by="brooks")
    gh.close_issue_as("me/toy", t.issue_number)
    assert "is closed" in rt.retry(store, CFG, t.id, now=NOW, api=gh).reason
    assert store.get_ticket(t.id).column == "dropped"


def test_dropped_ticket_with_a_newer_open_ticket_is_refused(store):
    gh = FakeGitHub()
    t = dropped(store, gh)
    newer = store.create_ticket(repo="me/toy", issue_number=t.issue_number, title="t",
                                request="r", base_branch="main")  # fmt: skip
    res = rt.retry(store, CFG, t.id, now=NOW, api=gh)
    assert res.status == "refused" and f"open ticket ({newer.id})" in res.reason
    assert store.get_ticket(t.id).column == "dropped"


# --------------------------------------------------------------------------- the issue comment


def test_flag_comment_offers_fman_retry_for_retryable_flags(store):
    t = flagged(store, "checks_failed: x", [])
    assert f"`fman retry me/toy#{t.issue_number}`" in flag_comment(t, "checks_failed: x")
    assert "fman retry" not in flag_comment(t, "no_verify_command")
    assert "fman retry" not in flag_comment(t, "review_skipped: x", pr_number=3)
    assert "manual retry" in flag_comment(t, f"{rt.FLAG_RETRY_ROUND}: used")


# --------------------------------------------------------------------------- end to end


def test_worker_runs_one_more_attempt_after_a_retry(tmp_path: Path, monkeypatch) -> None:
    orig = Worker.policy_for

    def wired(self: Worker, ticket: Ticket):  # what the wiring step adds to policy_for
        return rt.with_manual_retry(self.store, ticket, self.history(ticket), orig(self, ticket))

    monkeypatch.setattr(Worker, "policy_for", wired)
    local3 = replace(qwen_27b_local(2), name="qwen-3", tier=3)
    models = (*local_tier2_models()[:2], local3)  # three local rungs
    env = Env(tmp_path, models=models, reviewer_models=("qwen-3",))
    try:
        n = env.label()
        for _ in range(7):
            env.once(failing())
        assert len(env.attempts(n)) == 6
        t = env.ticket(n)
        assert t.flag_reason.startswith("checks_failed")
        res = rt.retry(env.store, env.config, f"{REPO}#{n}", now=NOW)
        assert res.status == "queued"
        assert rt.retry(env.store, env.config, f"{REPO}#{n}", now=NOW).status == "already_queued"
        env.once(failing())
        env.once(failing())
        attempts = env.attempts(n)
        assert len(attempts) == 7 and attempts[-1].model == attempts[-2].model
        assert env.ticket(n).id == t.id and env.ticket(n).flag_reason.startswith("checks_failed")
        assert rt.manual_retry_attempts(env.store, env.ticket(n)) == {attempts[-1].id}
    finally:
        env.close()
