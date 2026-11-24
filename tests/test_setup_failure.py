"""#53: a setup failure flags the ticket, it does not climb the ladder.

Found in the first live run: the OpenCode session could not be opened (``POST /session:
500``), the driver returned ``errored``, the worker stored ``failed_checks`` and escalation
climbed every rung up to the top cloud model, 10 attempts in 20 seconds, all counted
against their models. Here the same failure (and the other ways an attempt cannot run)
sets the attempt aside, flags the ticket once, and nothing else runs.

The rule is ``worker_setup.could_not_run`` (its module docstring); the unit tests at the
bottom pin it down case by case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fakes.opencode import Script, edit, fail, say
from fakes.worker_env import REPO, Env, failing, passing
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.metrics import queries as q
from foreman_inator.runner.opencode import AttemptResult
from foreman_inator.runner.worktree import RepoMirror, WorktreeError
from foreman_inator.worker_setup import (
    FLAG_SETUP,
    OPENCODE_START,
    PROMPT_SEND,
    SESSION_CREATE,
    SetupFailed,
    clean_detail,
    could_not_run,
)
from test_replay_sets import CHALLENGER_MARKER, World, key_of, three_merges, writer_pair
from test_replay_sets import script as replay_script

LADDER = ["inkling-2-mini", "qwen3.8-27b", "glm", "claude-opus", "claude-fable"]


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def flag_comments(env: Env, n: int = 1) -> list[str]:
    return [c for c in env.issue_comments(n) if "foreman-inator:flag" in c]


def cloud_requests(env: Env) -> list[Any]:
    """Model requests the proxy forwarded to a cloud endpoint."""
    cloud = {m.model for m in env.config.models if m.is_cloud}
    return [r for r in env.models.requests if (r.json or {}).get("model") in cloud]


def assert_flagged_once(env: Env, n: int, what: str) -> None:
    """The #53 end state: one attempt row, set aside; one flag comment; nothing counted."""
    t = env.ticket(n)
    assert t.flag_reason is not None and t.flag_reason.startswith(f"{FLAG_SETUP}: {what}: ")
    (att,) = env.attempts(n)
    assert att.outcome == "superseded" and att.ended_at is not None
    assert att.tier == 1 and att.model == LADDER[0]
    assert env.worker.history(t).attempts == ()  # escalation counts nothing
    assert env.worker.uncounted_attempts(t) == {att.id}
    (comment,) = flag_comments(env, n)
    assert "could not start work on this issue" in comment
    assert f"Why: {what}: " in comment
    assert "remove the `agent` label and add it again" in comment
    assert set(env.driver.endpoints) <= {LADDER[0]}  # no higher rung
    assert cloud_requests(env) == []  # and no cloud call


# =========================================================================== the live bug


def test_a_session_that_cannot_open_flags_the_ticket_once_and_never_escalates(
    env: Env,
) -> None:
    """The live run's failure: ``POST /session`` answers 500 on every launch. One attempt
    row, two OpenCode launches (the one immediate re-run), then the flag; more passes and
    a restart run nothing; the metrics see no attempt on any model."""
    n = env.label()
    env.once(Script([edit("status.txt", "pass\n"), say("Done.")], session_status=500))
    assert_flagged_once(env, n, SESSION_CREATE)
    assert "POST /session: 500" in env.ticket(n).flag_reason
    assert len(env.oc.launches()) == 2 and len(env.driver.prompts) == 2
    assert env.ticket(n).column == "in_progress" and env.ticket(n).pr_number is None
    # nothing climbs the ladder, nothing re-runs, on later passes or after a restart
    for _ in range(3):
        env.once()
    env.restart()
    for _ in range(2):
        env.once()
    assert_flagged_once(env, n, SESSION_CREATE)
    assert len(env.oc.launches()) == 2
    # the metrics: the set-aside attempt is no model's start, failure or escalation
    assert q.escalation_rate(env.store).denominator == 0
    assert q.kickbacks_per_ticket(env.store).tickets == 0


def test_a_prompt_that_cannot_be_sent_is_a_setup_failure_too(env: Env) -> None:
    n = env.label()
    env.once(Script([say("never")], prompt_status=500))
    assert_flagged_once(env, n, PROMPT_SEND)
    assert "prompt_async: 500" in env.ticket(n).flag_reason
    assert len(env.oc.launches()) == 2


def test_a_server_that_cannot_start_is_a_setup_failure_too(env: Env) -> None:
    n = env.label()
    env.once(Script([say("never")], startup_exit=4))
    assert_flagged_once(env, n, OPENCODE_START)
    assert "opencode serve exited with 4" in env.ticket(n).flag_reason
    assert len(env.oc.launches()) == 2


def test_a_transient_failure_gets_exactly_one_rerun_and_the_attempt_goes_on(env: Env) -> None:
    """The first launch's session fails, the re-run works: one attempt, no flag."""
    n = env.label()
    env.once(
        Script(
            [edit("status.txt", "pass\n"), edit("feature.py", "x = 1\n"), say("Done.")],
            session_status=500,
            fail_launches=1,
        )
    )
    t = env.ticket(n)
    assert t.flag_reason is None and t.column == "testing"
    (att,) = env.attempts(n)
    assert att.tests_passed and att.outcome is None
    assert len(env.oc.launches()) == 2 and flag_comments(env, n) == []


def test_the_worktree_that_cannot_be_created_flags_before_any_attempt(
    env: Env, monkeypatch
) -> None:
    def broken(self, *a: Any, **kw: Any):
        raise WorktreeError("git worktree add failed: disk full")

    monkeypatch.setattr(RepoMirror, "create", broken)
    n = env.label()
    env.once(passing())
    t = env.ticket(n)
    assert t.flag_reason.startswith(f"{FLAG_SETUP}: worktree: git worktree add failed")
    assert env.attempts(n) == [] and env.oc.launches() == []
    assert len(flag_comments(env, n)) == 1
    env.once()
    assert env.attempts(n) == [] and len(flag_comments(env, n)) == 1


# =========================================================================== the seam


def test_prepare_worktree_runs_after_the_row_and_before_the_writer(env: Env) -> None:
    seen: list[tuple[int, str, bool, int]] = []

    def prepare(ticket, attempt, worktree) -> None:
        seen.append((attempt.id, attempt.trigger, Path(worktree.path).is_dir(),
                     len(env.oc.launches())))  # fmt: skip

    env.worker.prepare_worktree = prepare  # what slot B's mixin overrides
    n = env.label()
    env.once(passing(model_call=False))
    (att,) = env.attempts(n)
    assert seen == [(att.id, "first", True, 0)]
    assert att.tests_passed and env.ticket(n).flag_reason is None


def test_setup_failed_from_prepare_worktree_flags_and_runs_no_writer(env: Env) -> None:
    def prepare(ticket, attempt, worktree) -> None:
        raise SetupFailed("dependency_install", "`npm ci` exited 1: ENOTFOUND registry")

    env.worker.prepare_worktree = prepare
    n = env.label()
    env.once(passing())
    assert_flagged_once(env, n, "dependency_install")
    assert "npm ci" in env.ticket(n).flag_reason
    assert env.oc.launches() == [] and env.driver.prompts == []  # never re-run
    for _ in range(2):
        env.once()
    env.restart()
    env.once()
    assert_flagged_once(env, n, "dependency_install")
    assert q.escalation_rate(env.store).denominator == 0


def test_the_rebase_job_has_the_same_seam(env: Env) -> None:
    from test_worker_v2b import conflicted

    n = env.label()
    conflicted(env, n)
    (pr_attempt,) = env.attempts(n)

    def prepare(ticket, attempt, worktree) -> None:
        if attempt.trigger == "rebase":
            raise SetupFailed("dependency_install", "uv sync failed")

    env.worker.prepare_worktree = prepare
    launches = len(env.oc.launches())
    env.once()
    t = env.ticket(n)
    assert t.flag_reason.startswith(f"{FLAG_SETUP}: dependency_install: uv sync failed")
    first, rebase = env.attempts(n)
    assert first.id == pr_attempt.id and first.outcome is None and first.tests_passed
    assert (rebase.trigger, rebase.outcome) == ("rebase", "superseded")
    assert len(env.oc.launches()) == launches  # no writer ran
    assert rebase.id not in {a.id for a in env.worker.history(t).attempts}
    assert rebase.id in env.worker.uncounted_attempts(t)
    assert len(flag_comments(env, n)) == 1
    env.once()
    assert len(env.attempts(n)) == 2


# =========================================================================== unchanged paths


def test_a_real_check_failure_still_retries_and_escalates(env: Env) -> None:
    n = env.label()
    env.once(failing())
    env.once(failing())
    assert [(a.tier, a.outcome) for a in env.attempts(n)] == [
        (1, "failed_checks"),
        (1, "failed_checks"),
    ]
    env.once(passing(model_call=False))
    assert env.attempts(n)[-1].tier == 2 and env.ticket(n).flag_reason is None


def test_a_model_error_after_the_session_opened_is_still_failed_checks(env: Env) -> None:
    """The session opened and the prompt was sent; the model side then failed. That is
    not a setup failure: escalation decides, as before."""
    n = env.label()
    env.once(Script([fail("upstream model error")]))
    (att,) = env.attempts(n)
    assert att.outcome == "failed_checks" and env.ticket(n).flag_reason is None
    assert len(env.oc.launches()) == 1  # no re-run either
    assert env.worker.history(env.ticket(n)).attempts == (att,)


# =========================================================================== metrics


def test_set_aside_attempts_are_invisible_to_the_model_metrics(tmp_path: Path) -> None:
    store = Store(tmp_path / "db")
    try:
        t = store.create_ticket(
            repo=REPO, issue_number=1, title="t", request="r", base_branch="main"
        )
        a1 = store.add_attempt(t.id, model="m1", tier=1, trigger="first")
        store.finish_attempt(a1.id, "superseded")
        a2 = store.add_attempt(t.id, model="m2", tier=2, trigger="first")
        store.finish_attempt(a2.id, "failed_checks", tests_passed=False)
        assert q.escalation_rate(store).numerator == 1  # a1 is the start: counted
        store.set_cursor(REPO, json.dumps([a1.id]), f"worker:uncounted:{t.id}")
        rate = q.escalation_rate(store)
        assert (rate.numerator, rate.denominator) == (0, 1)  # a2 is the start now
        assert q.escalation_rate(store, q.MetricsFilter(model="m1")).denominator == 0
        store.set_cursor(REPO, "not json", f"worker:uncounted:{t.id}")
        assert q.escalation_rate(store).numerator == 1  # a bad cursor hides nothing
    finally:
        store.close()


# =========================================================================== replays


def test_a_replay_side_that_could_not_run_is_an_error_not_a_loss(tmp_path: Path) -> None:
    """The champion's sessions cannot open: its cases are ``error`` (kept out of the
    comparison), not ``agent_failed`` (a loss that would hand the challenger a win)."""
    w = World(tmp_path)
    try:
        three_merges(w)
        champ, chall = writer_pair(w)

        def choose(prompt: str, endpoint: Any) -> Script:
            if CHALLENGER_MARKER in prompt:
                return replay_script("ok", key_of(prompt))
            return Script([say("never")], session_status=500)

        w.driver.choose = choose
        c = rr.run_replay(w.deps(), role="writer", champion=champ, challenger=chall,
                          settings=rr.ReplaySettings(min_cases=3))  # fmt: skip
        rows = {(r.side, r.status) for r in rr.results(w.store, c.experiment_id)}
        assert rows == {("champion", "error"), ("challenger", "passed")}
        assert all(
            r.detail.startswith(f"{FLAG_SETUP}: {SESSION_CREATE}")
            for r in rr.results(w.store, c.experiment_id)
            if r.side == "champion"
        )
        assert (c.errored_cases, c.scored_cases, c.verdict) == (3, 0, "inconclusive")
    finally:
        w.close()


# =========================================================================== the rule


def R(**kw: Any) -> AttemptResult:
    base: dict[str, Any] = {"status": "errored", "outcome": "failed_checks", "final_message": ""}
    return AttemptResult(**(base | kw))


@pytest.mark.parametrize(
    ("result", "proxy_tokens", "expected"),
    [
        (R(error="POST /session: 500 {}"), 0, SESSION_CREATE),
        (R(error="ConnectError: connection refused"), 0, SESSION_CREATE),
        (R(error="opencode serve exited with 1: boom"), 0, OPENCODE_START),
        (R(error="opencode serve did not start in 30s: "), 0, OPENCODE_START),
        (R(error="POST /session/ses_1/prompt_async: 500 x", session_id="ses_1"), 0, PROMPT_SEND),
        # the session opened and something else went wrong: the model's business
        (R(error="model error", session_id="ses_1"), 0, None),
        (R(error="opencode exited: x", session_id="ses_1"), 0, None),
        (R(error="GET /session/ses_1/message: 500", session_id="ses_1"), 0, None),
        # any sign of work, or a model call the proxy saw, is not "could not run"
        (R(error="POST /session: 500", tool_calls=1), 0, None),
        (R(error="POST /session: 500", assistant_messages=1), 0, None),
        (R(error="POST /session: 500", tokens_in=10), 0, None),
        (R(error="POST /session: 500", files_edited=("a.py",)), 0, None),
        (R(error="POST /session: 500"), 12, None),
        # other statuses keep their paths
        (R(error="cancelled by the worker", cancelled=True), 0, None),
        (R(status="timed_out", error="timed out after 30s"), 0, None),
        (R(status="finished", outcome=None), 0, None),
    ],
)
def test_could_not_run_rule(result: AttemptResult, proxy_tokens: int, expected) -> None:
    assert could_not_run(result, proxy_tokens=proxy_tokens) == expected


def test_setup_failed_reason_and_detail_for_the_issue() -> None:
    home = str(Path.home())
    exc = SetupFailed("dependency: install", f"failed in {home}/x\n  line two " + "y" * 900)
    assert exc.reason == "dependency  install"
    assert exc.flag_reason.startswith("setup_failed: dependency  install: failed in ~/x line two")
    assert home not in exc.flag_reason and len(exc.detail) <= 500
    assert clean_detail(None) == "" and SetupFailed("x").flag_reason == "setup_failed: x"


def test_worker_setup_imports_without_the_worker() -> None:
    import subprocess
    import sys

    code = (
        "import sys, foreman_inator.worker_setup as m; "
        "assert 'foreman_inator.worker' not in sys.modules; print(m.SetupFailed.__name__)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "SetupFailed"
