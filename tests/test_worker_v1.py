"""Worker loop v1 (todo #14): the whole milestone-1 loop against fakes.

GitHub is ``FakeGitHub``; the "GitHub remote" is a real bare git repo under ``tmp_path``
(worktrees, commits and force-pushes are real git); OpenCode is ``FakeOpenCode``'s shim
driven by the real ``OpenCodeDriver``; the model proxy is real, in front of
``FakeModels``. Nothing touches the network, a real model, the Keychain or
``~/.foreman-inator``.

The verify command is a saved override that passes only when the agent wrote ``pass``
into ``status.txt``, so each test decides per attempt whether the checks pass.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from typer.testing import CliRunner

from fakes.github import FakeGitHub
from fakes.models import FakeModels
from fakes.opencode import FakeOpenCode, Script, bash, edit, hang, say
from foreman_inator import worker_policy
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.proxy.server import ModelProxy
from foreman_inator.runner.worktree import GitIdentity, WorktreeManager
from foreman_inator.tracing import capture_spans
from foreman_inator.worker import Worker
from foreman_inator.worker_run import install_signal_handlers
from foreman_inator.worker_status import render_status, set_verify_command

REPO = "acme/widgets"
USER = "brooks"
VERIFY = 'cat status.txt; test "$(cat status.txt)" = pass'
SEED = {
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.invalid",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.invalid",
}


def git(*args: str, cwd: Path) -> str:
    env = (
        {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        | SEED
        | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    )
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


# --------------------------------------------------------------------------- fakes


class LinkedFakeGitHub(FakeGitHub):
    """FakeGitHub whose branches follow a real bare repo, so the worker's `git push` is
    what GitHub sees (PR heads, review commit ids)."""

    def __init__(self, remotes: dict[str, Path]) -> None:
        super().__init__()
        self.remotes = remotes
        self.write_spans: list[tuple[str, str | None]] = []  # (method, current span name)

    def _write(self, method: str, **args: Any) -> None:
        cur = otel_trace.get_current_span()
        self.write_spans.append((method, getattr(cur, "name", None)))
        super()._write(method, **args)

    def sync(self, repo: str) -> None:
        out = git("for-each-ref", "--format=%(refname:short) %(objectname)", cwd=self.remotes[repo])
        for line in out.splitlines():
            name, sha = line.split()
            if name.startswith("fman/"):
                self.repos[repo].branches[name] = sha

    def get_branch_sha(self, repo: str, branch: str) -> str:
        self.sync(repo)
        return super().get_branch_sha(repo, branch)

    def create_draft_pr(self, repo: str, **kw: Any) -> Any:
        self.sync(repo)
        return super().create_draft_pr(repo, **kw)

    def submit_review(self, repo: str, number: int, **kw: Any) -> Any:
        self.sync(repo)
        return super().submit_review(repo, number, **kw)


class RecordingDriver:
    """Wraps the fake-backed OpenCodeDriver: keeps each prompt; can simulate a kill."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.prompts: list[str] = []
        self.crash_next = False

    def run(self, **kw: Any) -> Any:
        self.prompts.append(kw["prompt"])
        if self.crash_next:
            self.crash_next = False
            raise SimulatedCrash("worker killed mid-attempt")
        return self.inner.run(**kw)


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in the worker catches it, like SIGKILL."""


def proxy_call() -> dict[str, Any]:
    """A bash step that makes one model call through the attempt's proxy URL, the way
    OpenCode would (its provider baseURL is in OPENCODE_CONFIG)."""
    code = (
        "import json,os,urllib.request as u;"
        "c=json.load(open(os.environ['OPENCODE_CONFIG']))['provider']['fman'];"
        "m=list(c['models'])[0];"
        "r=u.Request(c['options']['baseURL']+'/chat/completions',"
        "data=json.dumps({'model':m,'messages':[{'role':'user','content':'hi'}]}).encode(),"
        "headers={'Content-Type':'application/json'});"
        "print(u.urlopen(r).read()[:40])"
    )
    return bash(f'"{sys.executable}" -c "{code}"')


def passing(feature: str = "def feature():\n    return 1\n", *, model_call: bool = True) -> Script:
    steps = [proxy_call()] if model_call else []
    steps += [edit("status.txt", "pass\n"), edit("feature.py", feature), say("Done.")]
    return Script(steps)


def failing() -> Script:
    return Script([edit("feature.py", "def feature():\n    return 0\n"), say("Tried.")])


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def spans():
    with capture_spans() as exporter:
        yield exporter


class Env:
    def __init__(self, tmp: Path, *, verify: str | None = VERIFY) -> None:
        self.tmp = tmp
        remote = tmp / "remote" / "acme" / "widgets.git"
        remote.mkdir(parents=True)
        git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp)
        seed = tmp / "seed"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        (seed / "README.md").write_text("# widgets\n")
        (seed / "status.txt").write_text("fail\n")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "seed", cwd=seed)
        git("push", "-q", str(remote), "main", cwd=seed)
        self.remote = remote
        self.gh = LinkedFakeGitHub({REPO: remote})
        self.gh.create_repo(REPO, default_branch="main", sha=git("rev-parse", "HEAD", cwd=seed))
        self.store = Store(tmp / "fman.db")
        if verify is not None:
            self.store.set_verify_command(REPO, verify)
        self.config = Config(app_id=1, allowed_users=(USER,), repos=(REPO,))
        self.models = FakeModels()
        self.proxy = ModelProxy(self.config, store=self.store, transport=self.models.transport)
        self.proxy.start()
        self.oc = FakeOpenCode(tmp / "oc", passing())
        self.driver = RecordingDriver(self.oc.driver(timeout=30))
        self.clock_now = 0.0
        self.worker = self.new_worker()

    def new_worker(self) -> Worker:
        """A fresh worker on the same store and disk, as after a restart."""
        return Worker(
            api=self.gh,
            store=self.store,
            config=self.config,
            worktrees=WorktreeManager(self.tmp / "worktrees", identity=GitIdentity()),
            driver=self.driver,
            proxy=self.proxy,
            recordings_dir=self.tmp / "recordings",
            traces_dir=self.tmp / "traces",
            clone_url=lambda repo: str(self.remote),
            clock=lambda: self.clock_now,
        )

    def label(self, title: str = "Add a feature", body: str = "Please add feature().") -> int:
        n = self.gh.create_issue(REPO, title, body, author=USER)
        self.gh.apply_label(REPO, n, "agent", by=USER)
        return n

    def once(self, script: Script | None = None) -> None:
        if script is not None:
            self.oc.set_script(script)
        self.worker.run_once()

    def ticket(self, n: int = 1):
        return self.store.tickets_for_issue(REPO, n)[-1]

    def attempts(self, n: int = 1):
        return self.store.list_attempts(self.ticket(n).id)

    def close(self) -> None:
        self.proxy.stop()
        self.store.close()


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

    # 1. Labeled → ticket → attempt 1 passes → draft PR → v1 marks it ready.
    env.once(passing())
    t = env.ticket(n)
    assert t.column == "testing" and t.pr_number is not None and t.flag_reason is None
    pr = env.gh.get_pr(REPO, t.pr_number)
    assert pr.head_ref == "fman/issue-1/a1" and not pr.draft and pr.author == env.gh.bot_login()
    assert len(env.gh.writes_named("create_draft_pr")) == 1
    assert len(env.gh.writes_named("mark_ready_for_review")) == 1
    a1 = env.attempts(n)[0]
    assert (a1.trigger, a1.tests_passed, a1.outcome) == ("first", True, None)
    assert a1.branch == "fman/issue-1/a1" and a1.base_commit and a1.worktree
    assert a1.variant_id and a1.variant_id.startswith("qwen3.6-35b-a3b/writer-v1@")
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
    assert "llm.call" in names and "tool.call" in names
    by_id = {s.context.span_id: s for s in mine}
    for s in mine:
        if s.name in ("verify", "llm.call", "tool.call"):
            assert by_id[s.parent.span_id].name == "attempt", s.name
    actions = [s.attributes.get("github.action") for s in mine if s.name == "github"]
    for a in ("branch_pushed", "pr_created", "pr_ready", "review_received", "pr_updated"):
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


def test_two_failed_checks_flag_the_issue_and_stop(env: Env) -> None:
    n = env.label()
    for _ in range(4):
        env.once(failing())
    t = env.ticket(n)
    attempts = env.attempts(n)
    assert len(attempts) == 2, "invariant 7e: no third attempt after two failed checks"
    assert [a.outcome for a in attempts] == ["failed_checks", "failed_checks"]
    assert [a.trigger for a in attempts] == ["first", "first"]  # retry keeps the trigger
    assert all(a.verify_output_path and Path(a.verify_output_path).exists() for a in attempts)
    assert t.column == "in_progress" and t.flag_reason.startswith("checks_failed")
    comments = env.gh.writes_named("post_comment")
    assert len(comments) == 1 and "checks failed" in comments[0].args["body"]
    assert not env.gh.writes_named("create_draft_pr")
    # The retry was told why: the check output is in its prompt.
    assert "CHECK OUTPUT" in env.driver.prompts[1] and "fail" in env.driver.prompts[1]
    assert len(env.driver.prompts) == 2


def test_retry_after_one_failure_then_pass(env: Env) -> None:
    n = env.label()
    env.once(failing())
    env.once(passing(model_call=False))
    t = env.ticket(n)
    assert t.column == "testing" and t.flag_reason is None
    assert [a.outcome for a in env.attempts(n)] == ["failed_checks", None]


def test_user_kickbacks_are_capped(env: Env) -> None:
    n = env.label()
    env.once(passing(model_call=False))
    pr = env.ticket(n).pr_number
    for i in range(worker_policy.MAX_USER_KICKBACKS + 1):
        env.gh.submit_review(REPO, pr, by=USER, state="REQUEST_CHANGES", body=f"round {i}")
        env.once(passing(f"def feature():\n    return {i + 2}\n", model_call=False))
    env.once()
    t = env.ticket(n)
    assert len(env.attempts(n)) == 1 + worker_policy.MAX_USER_KICKBACKS
    assert t.flag_reason and t.flag_reason.startswith("kickback_cap")
    assert t.column == "in_progress" and not env.gh.get_pr(REPO, pr).draft


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
    n = env.label()
    env.gh.fail_next("mark_ready_for_review", SimulatedCrash("killed after the PR opened"))
    with pytest.raises(SimulatedCrash):
        env.once(passing(model_call=False))
    t = env.ticket(n)
    assert t.column == "in_progress" and t.pr_number is not None
    assert env.attempts(n)[0].tests_passed

    env.worker = env.new_worker()
    assert env.worker.recover() == []
    env.once()
    t = env.ticket(n)
    assert t.column == "testing" and not env.gh.get_pr(REPO, t.pr_number).draft
    assert len(env.gh.writes_named("create_draft_pr")) == 1
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
    env.once(failing())
    env.once(failing())
    text = render_status(env.store)
    assert "TESTING" in text and "IN PROGRESS" in text
    assert "acme/widgets#1" in text and "PR #" in text
    assert "FLAGGED: checks_failed" in text
    assert "attempt 2 (first, qwen3.6-35b-a3b, tier 1): failed checks" in text


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
