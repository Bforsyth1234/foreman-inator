"""#41: GitHub CI results for the PR head, against FakeGitHub and a real Store.

Scenarios from the item's Verify: green → continues; red → ``failed_checks`` with the log
tail in the retry prompt; none → skipped; pending past the limit → continues with the note;
red on base too → not charged; missing permission → one warning, no crash.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fakes.github import FakeGitHub
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.github.app_auth import GitHubError
from foreman_inator.github.client import GitHubClient, MissingPermission
from foreman_inator.github.poller import CI_PERMISSION_WARNING, Poller
from foreman_inator.setup import manifest
from foreman_inator.tickets.lifecycle import Action as A
from foreman_inator.tickets.lifecycle import (
    CiResult,
    IllegalTransition,
    Transition,
    apply,
    decide_ci,
)
from foreman_inator.tickets.prompt import writer_prompt_for

REPO = "brooks/toy"
ME = "brooks"
T0 = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
LOG = "collected 3 items\n" + "x" * 50 + "\nFAILED tests/test_cli.py::test_flag - KeyError: 'v'\n"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Env:
    def __init__(self, tmp_path: Path, *, ci: bool = True) -> None:
        self.tmp = tmp_path
        self.clock = Clock()
        self.gh = FakeGitHub()
        self.gh.create_repo(REPO)
        self.store = Store(tmp_path / "fman.db", clock=self.clock)
        self.cfg = Config(allowed_users=(ME,), repos=(REPO,))
        self.poller = Poller(self.gh, self.store, self.cfg, ci=ci, clock=self.clock)

    def ticket_waiting_on_ci(self, issue_title: str = "Add a flag"):
        """A ticket in testing whose attempt passed local checks and pushed a draft PR."""
        n = self.gh.create_issue(REPO, issue_title, "Please add --verbose.")
        t = self.store.create_ticket(
            repo=REPO,
            issue_number=n,
            title=issue_title,
            request="Please add --verbose.",
            base_branch="main",
        )
        att = self.store.add_attempt(t.id, model="qwen", tier=1, trigger="first")
        verify = self.tmp / f"verify-{att.id}.txt"
        verify.write_text("3 passed\n")
        att = self.store.finish_attempt(
            att.id, None, tests_passed=True, verify_output_path=str(verify)
        )
        branch = f"fman/issue-{n}/a1"
        head = self.gh.push_branch(REPO, branch)
        pr = self.gh.create_draft_pr(REPO, head=branch, base="main", title="t", body="b")
        self.store.update_ticket(t.id, pr_number=pr.number)
        self.store.set_column(t.id, "in_progress")
        t = self.store.set_column(t.id, "testing")
        self.gh.calls.clear()
        return t, att, head

    def poll(self) -> list:
        return list(self.poller.poll_once())

    def ci_events(self) -> list[CiResult]:
        return [e for e in self.poll() if isinstance(e, CiResult)]

    def base_sha(self) -> str:
        return self.gh.repos[REPO].branches["main"]


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def _calls(gh: FakeGitHub, name: str) -> int:
    return sum(1 for c in gh.calls if c.method == name)


# --------------------------------------------------------------------------- scenarios


def test_green_ci_continues_to_ai_review(env: Env):
    t, att, head = env.ticket_waiting_on_ci()
    run = env.gh.add_check_run(REPO, head, "test", status="in_progress")
    assert env.ci_events() == []  # still running: wait
    env.gh.set_check_run(REPO, run, status="completed", conclusion="success")
    (ev,) = env.ci_events()
    assert (ev.ticket_id, ev.attempt_id, ev.state, ev.charged) == (t.id, att.id, "passed", False)
    assert apply("testing", ev) == Transition("testing", (A.RECORD_CI,))
    stored = env.store.set_ci_result(att.id, ev.state)
    assert (stored.ci_passed, stored.ci_status, stored.outcome) == (True, "passed", None)
    assert env.ci_events() == []  # decided: no more CI reads for this attempt
    assert _calls(env.gh, "list_check_runs") == 2


def test_red_ci_is_failed_checks_with_the_log_tail_in_the_retry_prompt(env: Env):
    t, att, head = env.ticket_waiting_on_ci()
    env.gh.add_check_run(REPO, head, "lint", status="completed", conclusion="success")
    env.gh.add_check_run(REPO, head, "test", status="completed", conclusion="failure", log=LOG)
    env.gh.add_check_run(REPO, env.base_sha(), "test", status="completed", conclusion="success")
    (ev,) = env.ci_events()
    assert ev.state == "failed" and ev.charged and ev.failed_jobs == ("test",)
    assert "FAILED tests/test_cli.py::test_flag" in ev.failure_output
    assert "test (failure)" in ev.failure_output
    assert apply("testing", ev) == Transition("in_progress", (A.RECORD_CI, A.NEXT_ATTEMPT))

    # The worker's RECORD_CI (runner wiring): save the output, store the result.
    out = env.tmp / f"ci-{att.id}.txt"
    out.write_text(ev.failure_output)
    stored = env.store.set_ci_result(att.id, "failed", failure_output_path=str(out))
    assert (stored.ci_passed, stored.outcome, stored.tests_passed) == (False, "failed_checks", True)
    env.store.set_column(t.id, "in_progress")

    # NEXT_ATTEMPT: escalation's same-tier retry; its prompt shows the CI log tail.
    retry = env.store.add_attempt(t.id, model="qwen", tier=1, trigger="first")
    wp = writer_prompt_for(
        env.store, env.store.get_ticket(t.id), retry, worktree=env.tmp, verify_command="pytest"
    )
    assert wp.situation == "retry"
    assert "FAILED tests/test_cli.py::test_flag - KeyError" in wp.text
    assert "GitHub CI failed on the pull request: test (failure)" in wp.text


def test_no_check_runs_after_the_grace_period_means_no_ci(env: Env):
    _t, att, _head = env.ticket_waiting_on_ci()
    assert env.ci_events() == []  # inside the 2-minute grace period
    env.clock.advance(minutes=1, seconds=59)
    assert env.ci_events() == []
    env.clock.advance(seconds=1)
    (ev,) = env.ci_events()
    assert ev.state == "no_ci" and not ev.charged
    assert apply("testing", ev) == Transition("testing", (A.RECORD_CI,))
    assert env.store.set_ci_result(att.id, "no_ci").ci_passed is None


def test_pending_past_the_limit_continues_with_a_note(env: Env):
    _t, _att, head = env.ticket_waiting_on_ci()
    env.gh.add_check_run(REPO, head, "lint", status="completed", conclusion="success")
    env.gh.add_check_run(REPO, head, "e2e", status="queued")
    env.clock.advance(minutes=29, seconds=59)
    assert env.ci_events() == []
    env.clock.advance(seconds=1)
    (ev,) = env.ci_events()
    assert ev.state == "timed_out" and not ev.charged
    assert ev.note == "CI still pending after 30 minutes: e2e"


def test_red_on_base_too_is_not_charged(env: Env):
    _t, att, head = env.ticket_waiting_on_ci()
    env.gh.add_check_run(REPO, head, "test", status="completed", conclusion="failure", log=LOG)
    env.gh.add_check_run(REPO, env.base_sha(), "test", status="completed", conclusion="failure")
    (ev,) = env.ci_events()
    assert ev.state == "base_red" and not ev.charged
    assert ev.note == "CI already red on base: test"
    assert ev.failure_output is None
    assert apply("testing", ev) == Transition("testing", (A.RECORD_CI,))
    stored = env.store.set_ci_result(att.id, ev.state)
    assert (stored.ci_passed, stored.outcome) == (None, None)
    assert _calls(env.gh, "get_job_log_tail") == 0


def test_a_job_red_on_base_does_not_excuse_another_red_job(env: Env):
    _t, _att, head = env.ticket_waiting_on_ci()
    env.gh.add_check_run(REPO, head, "lint", status="completed", conclusion="failure")
    env.gh.add_check_run(REPO, head, "test", status="completed", conclusion="failure", log=LOG)
    env.gh.add_check_run(REPO, env.base_sha(), "lint", status="completed", conclusion="failure")
    (ev,) = env.ci_events()
    assert ev.state == "failed" and ev.failed_jobs == ("test",)


def test_missing_permission_warns_once_and_does_not_crash(env: Env, caplog):
    env.ticket_waiting_on_ci("one")
    env.ticket_waiting_on_ci("two")
    env.gh.deny_permission("checks")
    with caplog.at_level(logging.WARNING, logger="foreman_inator.poller"):
        first = env.poll()
        second = env.poll()
    warnings = [r for r in caplog.records if r.getMessage() == CI_PERMISSION_WARNING]
    assert len(warnings) == 1
    states = [e.state for e in first + second if isinstance(e, CiResult)]
    assert states == ["no_permission"] * 4
    assert _calls(env.gh, "list_check_runs") == 1  # skipped after the first 403
    ev = next(e for e in first if isinstance(e, CiResult))
    assert apply("testing", ev) == Transition("testing", (A.RECORD_CI,))


def test_missing_actions_permission_still_fails_the_attempt_without_a_log(env: Env):
    _t, _att, head = env.ticket_waiting_on_ci()
    env.gh.add_check_run(REPO, head, "test", status="completed", conclusion="failure", log=LOG)
    env.gh.deny_permission("actions")
    (ev,) = env.ci_events()
    assert ev.state == "failed"
    assert "the App lacks 'actions: read'" in ev.failure_output


def test_requests_per_poll(env: Env):
    """+1 list_check_runs per waiting ticket per poll; when red, once: base sha, base runs,
    one job log."""
    _t, _att, head = env.ticket_waiting_on_ci()
    env.gh.add_check_run(REPO, head, "test", status="in_progress")
    env.poll()
    base = env.poller.poll_once().requests[REPO]
    no_ci = Poller(env.gh, env.store, env.cfg, ci=False, clock=env.clock).poll_once()
    assert base == no_ci.requests[REPO] + 1
    env.gh.add_check_run(REPO, head, "lint", status="completed", conclusion="failure", log=LOG)
    run = env.gh.list_check_runs(REPO, head)[0].id
    env.gh.set_check_run(REPO, run, status="completed", conclusion="success")
    red = env.poller.poll_once()
    assert red.requests[REPO] == base + 3


def test_ci_reads_are_off_unless_enabled(tmp_path: Path):
    e = Env(tmp_path, ci=False)
    _t, _att, head = e.ticket_waiting_on_ci()
    e.gh.add_check_run(REPO, head, "test", status="completed", conclusion="failure", log=LOG)
    e.clock.advance(hours=1)
    assert e.ci_events() == []
    assert _calls(e.gh, "list_check_runs") == 0
    e.store.close()


def test_no_ci_read_once_an_ai_review_exists_or_outside_testing(env: Env):
    t, att, head = env.ticket_waiting_on_ci()
    env.gh.add_check_run(REPO, head, "test", status="completed", conclusion="success")
    env.store.record_review(att.id, reviewer_model="glm", verdict="approve")
    assert env.ci_events() == []
    t2, _att2, _ = env.ticket_waiting_on_ci("other")
    env.store.set_column(t2.id, "in_progress")
    assert env.ci_events() == []
    assert _calls(env.gh, "list_check_runs") == 0


def test_ci_result_outside_testing_is_illegal():
    ev = CiResult(REPO, 1, ticket_id=1, attempt_id=1, head_sha="a", state="passed")
    for column in (None, "todo", "in_progress", "done", "dropped"):
        with pytest.raises(IllegalTransition):
            apply(column, ev)


# --------------------------------------------------------------------------- decide_ci


class Run:
    def __init__(self, name: str, status: str = "completed", conclusion: str | None = None):
        self.name, self.status, self.conclusion = name, status, conclusion


def _d(runs, minutes: float = 0, base=None):
    return decide_ci(runs, waiting_since=T0, now=T0 + timedelta(minutes=minutes), base_runs=base)


def test_decide_ci_rules():
    assert _d([]).state == "pending"
    assert _d([], minutes=2).state == "no_ci"
    assert _d([Run("t", "queued")], minutes=10).state == "pending"
    assert _d([Run("t", conclusion="success"), Run("u", conclusion="skipped")]).state == "passed"
    # cancelled / neutral are not charged
    assert _d([Run("t", conclusion="cancelled"), Run("n", conclusion="neutral")]).state == "passed"
    red = [Run("t", conclusion="failure")]
    assert _d(red).state == "needs_base" and not _d(red).decided
    assert _d(red, base=[]).state == "failed"
    assert _d(red, base=[Run("t", conclusion="timed_out")]).state == "base_red"
    assert _d(red, base=[Run("t", "in_progress")]).state == "failed"  # base unknown: charged
    # a red job with another still running waits, until the limit
    mixed = [Run("t", conclusion="failure"), Run("e2e", "in_progress")]
    assert _d(mixed, minutes=5, base=[]).state == "pending"
    late = _d(mixed, minutes=30, base=[])
    assert late.state == "failed" and late.note == "CI still pending after 30 minutes: e2e"


# --------------------------------------------------------------------------- real client


def _client(handler) -> GitHubClient:
    class _Auth:
        def token(self) -> str:
            return "ghs_test"

        def invalidate(self) -> None:
            pass

    return GitHubClient(
        _Auth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def test_real_client_403_is_missing_permission():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    c = _client(handler)
    with pytest.raises(MissingPermission) as exc:
        c.list_check_runs(REPO, "a" * 40)
    assert exc.value.permission == "checks" and isinstance(exc.value, GitHubError)
    with pytest.raises(MissingPermission) as exc:
        c.get_job_log_tail(REPO, 7)
    assert exc.value.permission == "actions"


def test_real_client_404_log_is_not_found_not_permission():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubError) as exc:
        _client(handler).get_job_log_tail(REPO, 7)
    assert not isinstance(exc.value, MissingPermission)


def test_real_client_keeps_only_the_tail_of_a_long_log():
    big = "".join(f"2026-11-23T09:00:00.0000000Z line {i}\n" for i in range(5000))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://blob.example/log"})
        assert "authorization" not in request.headers
        return httpx.Response(200, text=big)

    tail = _client(handler).get_job_log_tail(REPO, 7, max_chars=500)
    assert len(tail) <= 500 and tail.endswith("line 4999\n") and "2026-11-23T" not in tail


def test_real_client_job_id_from_details_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/commits/{'b' * 40}/check-runs"
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "check_runs": [
                    {
                        "id": 1,
                        "name": "test",
                        "status": "completed",
                        "conclusion": "failure",
                        "head_sha": "b" * 40,
                        "details_url": f"https://github.com/{REPO}/actions/runs/5/job/99",
                        "app": {"slug": "github-actions"},
                    },
                    {
                        "id": 2,
                        "name": "codecov",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": "b" * 40,
                        "details_url": "https://codecov.example/x",
                        "app": {"slug": "codecov"},
                    },
                ],
            },
        )

    runs = _client(handler).list_check_runs(REPO, "b" * 40)
    assert [(r.name, r.job_id) for r in runs] == [("test", 99), ("codecov", None)]


# --------------------------------------------------------------------------- setup


OLD_APP = {"issues": "write", "pull_requests": "write", "contents": "write", "metadata": "read"}


def test_manifest_asks_for_checks_and_actions_read():
    assert manifest.PERMISSIONS["checks"] == "read"
    assert manifest.PERMISSIONS["actions"] == "read"
    built = manifest.build_manifest("http://127.0.0.1:1/callback", "x")
    assert built["default_permissions"] == manifest.PERMISSIONS


def test_setup_text_for_an_app_created_before_41():
    missing = manifest.missing_permissions(OLD_APP)
    assert missing == {"checks": "read", "actions": "read"}
    text = manifest.permissions_upgrade_text("foreman-inator-abc123", missing, installation_ids=[7])
    assert "https://github.com/settings/apps/foreman-inator-abc123/permissions" in text
    assert "Checks: read" in text and "Actions: read" in text
    assert "https://github.com/settings/installations/7" in text
    assert manifest.missing_permissions({**OLD_APP, "checks": "write", "actions": "read"}) == {}
    assert manifest.permissions_upgrade_text("x", {}) == ""
    org = manifest.permissions_upgrade_text("x", missing, org="acme")
    assert "https://github.com/organizations/acme/settings/apps/x/permissions" in org


def test_installation_permissions_reads_the_installation(tmp_path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/42"
        return httpx.Response(200, json={"id": 42, "permissions": OLD_APP})

    auth = manifest.AppAuth(1, pem, transport=httpx.MockTransport(handler))
    assert manifest.installation_permissions(auth, 42) == OLD_APP
