"""Worker loop v1 scenarios (todo #14), ported to worker loop v2 (#21).

The milestone-1 loop against fakes (``fakes.worker_env``: ``FakeGitHub`` linked to a real
bare git repo, ``FakeOpenCode`` behind the real driver, ``FakeModels`` behind the real
proxy). v2 adds routing, escalation and the AI review to every run: the fake router
answers "small" (tier 1) and the fake reviewer approves unless a test queues another
verdict. Scenarios whose v1 behaviour #21 replaces say so in their docstring.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes.opencode import Script, edit, hang, say
from fakes.worker_env import (
    REPO,
    USER,
    VERIFY,
    Env,
    SimulatedCrash,
    failing,
    git,
    passing,
)
from foreman_inator import worker_policy
from foreman_inator.db.store import Store
from foreman_inator.tracing import capture_spans
from foreman_inator.worker_run import install_signal_handlers
from foreman_inator.worker_status import render_status, set_verify_command


@pytest.fixture
def spans():
    with capture_spans() as exporter:
        yield exporter


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- the full loop


def test_full_loop_label_kickback_approve_merge(env: Env, spans) -> None:
    n = env.label()

    # 1. Labeled → routed (tier 1) → attempt 1 passes → draft PR → AI review approves →
    #    marked ready (v2: v1 marked it ready as soon as the checks passed).
    env.once(passing())
    t = env.ticket(n)
    assert t.column == "testing" and t.pr_number is not None and t.flag_reason is None
    assert t.routed_tier == 1 and "classified the issue as small" in t.route_reason
    pr = env.gh.get_pr(REPO, t.pr_number)
    assert pr.head_ref == "fman/issue-1/a1" and not pr.draft and pr.author == env.gh.bot_login()
    assert len(env.gh.writes_named("create_draft_pr")) == 1
    assert len(env.gh.writes_named("post_review")) == 1
    assert len(env.gh.writes_named("mark_ready_for_review")) == 1
    assert [r.verdict for r in env.reviews(n)] == ["approve"]
    a1 = env.attempts(n)[0]
    assert (a1.trigger, a1.tests_passed, a1.outcome) == ("first", True, None)
    assert a1.branch == "fman/issue-1/a1" and a1.base_commit and a1.worktree
    assert a1.variant_id and a1.variant_id.startswith("inkling-2-mini/writer-v1@")
    assert a1.diff_files == 2 and a1.diff_lines and a1.seconds is not None
    assert a1.tokens_in > 0 and a1.tokens_out > 0  # from the proxy's llm.call
    assert a1.verify_output_path is None
    # The worker committed and pushed; the PR head on the remote is that commit.
    head = git("rev-parse", "fman/issue-1/a1", cwd=env.remote)
    assert git("show", f"{head}:status.txt", cwd=env.remote) == "pass"

    # 2. The user requests changes with a note and a kickback:incomplete label.
    env.gh.apply_label(REPO, t.pr_number, "kickback:incomplete", by=USER)
    env.gh.submit_review(
        REPO, t.pr_number, by=USER, state="REQUEST_CHANGES", body="Also handle zero."
    )
    env.once(passing("def feature(x=0):\n    return x or 1\n"))
    t = env.ticket(n)
    assert t.column == "testing"
    a1, a2 = env.attempts(n)
    assert a1.outcome == "kicked_back" and a2.trigger == "kickback" and a2.tests_passed
    assert a2.branch == "fman/issue-1/a2"
    kicks = env.store.list_kickbacks(ticket_id=t.id)
    assert [(k.attempt_id, k.by, k.tag, k.note) for k in kicks] == [
        (a1.id, "user", "incomplete", "Also handle zero.")
    ]
    assert "Also handle zero." in env.driver.prompts[1]
    assert "kickback:incomplete" not in env.gh.get_issue(REPO, t.pr_number).labels
    # Same PR, force-pushed with attempt 2's commit (built on attempt 1's).
    assert len(env.gh.writes_named("create_draft_pr")) == 1
    assert env.gh.writes_named("update_pr")
    head2 = git("rev-parse", "fman/issue-1/a1", cwd=env.remote)
    assert head2 != head and git("rev-parse", f"{head2}~1", cwd=env.remote) == head

    # 3. Approve, then merge: accept recorded, issue closed, worktrees cleaned.
    env.gh.submit_review(REPO, t.pr_number, by=USER, state="APPROVE")
    env.once()
    assert env.ticket(n).column == "testing"
    env.gh.merge_pr(REPO, t.pr_number, by=USER)
    env.once()
    t = env.ticket(n)
    assert t.column == "done" and t.done_at is not None and t.accepted_attempt_id == a2.id
    assert [a.outcome for a in env.attempts(n)] == ["kicked_back", "accepted"]
    assert env.gh.get_issue(REPO, n).state == "closed"
    env.once()  # the sweep runs on the next pass, when the repo is idle
    assert not list((env.tmp / "worktrees" / REPO).glob("issue-*"))
    assert len(env.driver.prompts) == 2

    # 4. One trace for the ticket, with the expected spans.
    mine = [s for s in spans.get_finished_spans() if s.attributes.get("ticket.id") == t.id]
    assert {format(s.context.trace_id, "032x") for s in mine} == {t.trace_id}
    names = [s.name for s in mine]
    assert names.count("ticket") == 1 and names.count("route") == 1
    assert names.count("attempt") == 2 and names.count("verify") == 2
    assert names.count("review") == 2
    assert "llm.call" in names and "tool.call" in names
    by_id = {s.context.span_id: s for s in mine}
    for s in mine:
        if s.name in ("verify", "tool.call"):
            assert by_id[s.parent.span_id].name == "attempt", s.name
        if (
            s.name == "llm.call"
        ):  # the writer's, the clarify check's (#44), the router's, the reviewer's
            assert by_id[s.parent.span_id].name in ("attempt", "clarify", "route", "review")
    for r in (s for s in mine if s.name == "review"):
        kids = [c.name for c in mine if c.parent and c.parent.span_id == r.context.span_id]
        assert kids.count("review.check") == 5 and set(kids) <= {"review.check", "llm.call"}
    actions = [s.attributes.get("github.action") for s in mine if s.name == "github"]
    for a in (
        "branch_pushed",
        "pr_created",
        "ai_review_posted",
        "pr_ready",
        "review_received",
        "pr_updated",
        "summary_posted",
    ):
        assert a in actions, (a, actions)
    kick = [e for s in mine if s.name == "github" for e in s.events if e.name == "kickback"]
    assert len(kick) == 1 and kick[0].attributes["attempt.id"] == a1.id
    # Every GitHub write the worker made happened inside a github span.
    assert "kickback_label_cleared" in actions and "issue_closed" in actions
    assert env.gh.write_spans and all(name == "github" for _, name in env.gh.write_spans), (
        env.gh.write_spans
    )


def test_merge_closes_issue_github_left_open(env: Env) -> None:
    n = env.label()
    env.once(passing(model_call=False))
    t = env.ticket(n)
    env.gh.merge_pr(REPO, t.pr_number, by=USER)
    env.gh.reopen_issue_as(REPO, n, by=USER)  # as if the PR body had not closed it
    env.once()
    assert env.ticket(n).column == "done"
    assert env.gh.get_issue(REPO, n).state == "closed"
    assert env.gh.writes_named("close_issue")


# --------------------------------------------------------------------------- caps (7e)


def test_two_failed_checks_escalate_instead_of_a_third_try(env: Env) -> None:
    """v1 flagged the ticket after two failed checks; v2 (#16) moves up one tier instead
    (the hand-over at the top of the ladder is ``test_worker_v2``'s tier-3 test)."""
    n = env.label()
    for _ in range(3):
        env.once(failing())
    t = env.ticket(n)
    attempts = env.attempts(n)
    assert len(attempts) == 3
    assert [a.outcome for a in attempts] == ["failed_checks"] * 3
    assert [a.trigger for a in attempts] == ["first", "first", "escalation"]  # retry keeps it
    assert [a.tier for a in attempts] == [1, 1, 2]
    assert all(a.verify_output_path and Path(a.verify_output_path).exists() for a in attempts)
    assert t.column == "in_progress" and t.flag_reason is None
    assert not env.gh.writes_named("post_comment")
    assert not env.gh.writes_named("create_draft_pr")
    # The retry was told why: the check output is in its prompt.
    assert "CHECK OUTPUT" in env.driver.prompts[1] and "fail" in env.driver.prompts[1]
    assert len(env.driver.prompts) == 3


def test_retry_after_one_failure_then_pass(env: Env) -> None:
    n = env.label()
    env.once(failing())
    env.once(passing(model_call=False))
    t = env.ticket(n)
    assert t.column == "testing" and t.flag_reason is None
    assert [a.outcome for a in env.attempts(n)] == ["failed_checks", None]


def test_user_kickbacks_on_one_tier_are_capped(env: Env) -> None:
    """v1 flagged the ticket at the third request for changes; v2 (#16) answers the first
    on the same tier and moves up one tier at the second."""
    n = env.label()
    env.once(passing(model_call=False))
    pr = env.ticket(n).pr_number
    for i in range(worker_policy.MAX_USER_KICKBACKS):
        env.gh.submit_review(REPO, pr, by=USER, state="REQUEST_CHANGES", body=f"round {i}")
        env.once(passing(f"def feature():\n    return {i + 2}\n", model_call=False))
    t = env.ticket(n)
    attempts = env.attempts(n)
    assert len(attempts) == 1 + worker_policy.MAX_USER_KICKBACKS
    assert [a.trigger for a in attempts] == ["first", "kickback", "escalation"]
    assert [a.tier for a in attempts] == [1, 1, 2]
    assert t.flag_reason is None and t.column == "testing"
    assert not env.gh.get_pr(REPO, pr).draft


# --------------------------------------------------------------------------- no verify command


def test_no_verify_command_flags_once_then_set_verify_resumes(tmp_path: Path) -> None:
    e = Env(tmp_path, verify=None)  # the seed repo has nothing detectable
    try:
        n = e.label()
        e.once()
        e.once()
        t = e.ticket(n)
        assert t.column == "todo" and t.flag_reason.startswith("no_verify_command")
        comments = e.gh.writes_named("post_comment")
        assert len(comments) == 1
        assert "fman repo set-verify acme/widgets" in comments[0].args["body"]
        assert not e.attempts(n) and not e.driver.prompts
        assert set_verify_command(e.store, REPO, VERIFY) == [t.id]
        e.once(passing(model_call=False))
        assert e.ticket(n).column == "testing"
    finally:
        e.close()


# --------------------------------------------------------------------------- crash safety


def test_crash_mid_attempt_recovers_without_double_pr(env: Env) -> None:
    n = env.label()
    env.driver.crash_next = True
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    (a,) = env.attempts(n)
    assert a.ended_at is None and env.ticket(n).column == "in_progress"

    env.worker = env.new_worker()  # launchd restarts the worker
    assert env.worker.recover() == [a.id]
    env.once()
    attempts = env.attempts(n)
    assert [x.outcome for x in attempts] == ["superseded", None]
    assert attempts[1].trigger == "first"  # re-run with the interrupted attempt's trigger
    assert env.ticket(n).column == "testing"
    assert len(env.gh.writes_named("create_draft_pr")) == 1


def test_crash_after_pr_opened_resumes_publish(env: Env) -> None:
    """v1 crashed between "PR opened" and "ready"; in v2 the ready step follows the AI
    review, so the same crash point is after the review comment (see also v2's crash
    tests)."""
    n = env.label()
    env.gh.fail_next("mark_ready_for_review", SimulatedCrash("killed after the PR opened"))
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    t = env.ticket(n)
    assert t.column == "testing" and t.pr_number is not None
    assert env.gh.get_pr(REPO, t.pr_number).draft
    assert env.attempts(n)[0].tests_passed

    assert env.restart() == []
    env.once()
    t = env.ticket(n)
    assert t.column == "testing" and not env.gh.get_pr(REPO, t.pr_number).draft
    assert len(env.gh.writes_named("create_draft_pr")) == 1
    assert len(env.gh.writes_named("post_review")) == 1
    assert len(env.attempts(n)) == 1 and len(env.driver.prompts) == 1


def test_interrupted_attempts_are_capped(env: Env) -> None:
    n = env.label()
    for _ in range(worker_policy.MAX_INTERRUPTED + 1):
        env.driver.crash_next = True
        with pytest.raises(SimulatedCrash):
            env.once(passing(model_call=False))
        env.worker = env.new_worker()
        env.worker.recover()
    env.once()
    t = env.ticket(n)
    assert len(env.attempts(n)) == worker_policy.MAX_INTERRUPTED + 1
    assert t.flag_reason and t.flag_reason.startswith("interrupted")


# --------------------------------------------------------------------------- drop, shutdown


def test_closing_the_issue_drops_the_ticket(env: Env) -> None:
    n = env.label()
    env.once(passing(model_call=False))
    env.gh.close_issue_as(REPO, n, by=USER)
    env.once()
    assert env.ticket(n).column == "dropped"
    env.once()
    assert not list((env.tmp / "worktrees" / REPO).glob("issue-*"))


def test_stop_cancels_a_running_attempt_and_kills_opencode(env: Env) -> None:
    env.label()
    env.oc.set_script(Script([hang()]))
    env.worker.poll_and_handle()
    env.worker.dispatch(inline=False)  # the attempt runs on its own thread
    deadline = time.monotonic() + 20
    while not env.oc.launches() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert env.oc.launches(), "opencode never started"
    time.sleep(0.5)
    t0 = time.monotonic()
    env.worker.shutdown(timeout=30)
    assert time.monotonic() - t0 < 20
    (a,) = env.attempts()
    assert a.outcome == "superseded" and a.ended_at is not None
    deadline = time.monotonic() + 5
    while _pid_alive(env.oc.launches()[0]["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(env.oc.launches()[0]["pid"])


def test_sigterm_stops_serve_forever(env: Env) -> None:
    old = install_signal_handlers(env.worker)
    th = threading.Thread(target=env.worker.serve_forever)
    try:
        th.start()
        time.sleep(0.3)
        signal.raise_signal(signal.SIGTERM)
        th.join(10)
        assert not th.is_alive() and env.worker.stopping
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


# --------------------------------------------------------------------------- status + CLI


def test_status_lists_columns_attempts_flags_and_prs(env: Env) -> None:
    env.label("First issue")
    env.once(passing(model_call=False))
    env.label("Second issue")
    other = Script([edit("other.py", "x = 0\n"), say("Tried.")])  # not ticket 1's files
    env.once(other)
    env.once(other)
    text = render_status(env.store, config=env.config)
    assert "TESTING" in text and "IN PROGRESS" in text
    assert "acme/widgets#1" in text and "PR #" in text
    assert "attempt 2 (first, inkling-2-mini, tier 1): failed checks" in text
    assert "cloud spend: $" in text


def test_cli_status_reads_the_store_read_only(foreman_home: Path) -> None:
    from foreman_inator import paths
    from foreman_inator.cli import app

    runner = CliRunner()
    assert "No tickets yet" in runner.invoke(app, ["status"]).output
    with Store(paths.db_path()) as s:
        s.create_ticket(repo=REPO, issue_number=7, title="Fix it", request="", base_branch="main")
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0 and "acme/widgets#7" in res.output and "TODO (1)" in res.output
    res = runner.invoke(app, ["repo", "set-verify", REPO, "make test"])
    assert res.exit_code == 0, res.output
    with Store(paths.db_path(), read_only=True) as s:
        assert s.get_repo_settings(REPO).verify_command == "make test"
    assert runner.invoke(app, ["repo", "set-verify", "nope", "x"]).exit_code != 0
