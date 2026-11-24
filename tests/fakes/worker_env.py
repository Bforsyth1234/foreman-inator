"""A whole worker against fakes, for the worker loop tests (#14 v1, #21 v2).

GitHub is ``FakeGitHub``; the "GitHub remote" is a real bare git repo under ``tmp_path``
(worktrees, commits and force-pushes are real git); OpenCode is ``FakeOpenCode``'s shim
driven by the real ``OpenCodeDriver``; the model proxy is real, with the real spend caps
as its hook, in front of ``FakeModels``, which answers the router, the writer and the AI
reviewer (``Env.responder``). Nothing touches the network, a real model, the Keychain
(cloud keys come from ``key_resolver``) or ``~/.foreman-inator``.

The verify command is a saved override that passes only when the agent wrote ``pass``
into ``status.txt``, so each test decides per attempt whether the checks pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from opentelemetry import trace as otel_trace

from fakes.github import FakeGitHub
from fakes.models import FakeModels
from fakes.opencode import FakeOpenCode, Script, bash, edit, say
from foreman_inator.config import Config, SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.proxy.server import ModelProxy
from foreman_inator.review.reviewer import MODEL_MAX_TOKENS
from foreman_inator.routing.rules import MAX_TOKENS as ROUTER_MAX_TOKENS
from foreman_inator.runner.worktree import GitIdentity, WorktreeManager
from foreman_inator.spend import Spend
from foreman_inator.worker import Worker

REPO = "acme/widgets"
USER = "brooks"
VERIFY = 'cat status.txt; test "$(cat status.txt)" = pass'
SEED = {
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.invalid",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.invalid",
}
APPROVE = json.dumps({"verdict": "approve", "tag": None, "note": "Does what the issue asks."})


def request_changes(tag: str = "incomplete", note: str = "Handle the empty case.") -> str:
    return json.dumps({"verdict": "request_changes", "tag": tag, "note": note})


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


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in the worker catches it, like SIGKILL."""


class LinkedFakeGitHub(FakeGitHub):
    """FakeGitHub whose branches follow a real bare repo, so the worker's `git push` is
    what GitHub sees (PR heads, review commit ids). Also records the span each write ran
    in, and checks invariant 7a at every "mark ready" (``ready_violations``)."""

    def __init__(self, remotes: dict[str, Path]) -> None:
        super().__init__()
        self.remotes = remotes
        self.write_spans: list[tuple[str, str | None]] = []  # (method, current span name)
        self.store: Store | None = None
        self.ready_violations: list[str] = []
        self.ready_checks = 0

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

    def mark_ready_for_review(self, repo: str, number: int) -> Any:
        if self.store is not None:
            self.ready_checks += 1
            why = ready_violation(self, self.store, repo, number)
            if why:
                self.ready_violations.append(why)
        return super().mark_ready_for_review(repo, number)


def ready_violation(gh: FakeGitHub, store: Store, repo: str, number: int) -> str | None:
    """Invariant 7a, checked at the moment the PR is marked ready: the attempt on the PR
    has a stored AI approve, or a hand-over comment is already on the issue."""
    tickets = [t for t in store.list_tickets() if t.repo == repo and t.pr_number == number]
    if not tickets:
        return f"PR #{number}: no ticket"
    t = tickets[-1]
    attempts = [
        a for a in store.list_attempts(t.id) if a.tests_passed and a.outcome != "superseded"
    ]
    current = attempts[-1] if attempts else None
    approved = current is not None and any(
        r.verdict == "approve" for r in store.list_reviews(attempt_id=current.id, shadow=False)
    )
    bot = gh.bot_login()
    marker = f"foreman-inator:note ticket={t.id} key=handover-"
    item = gh._item(repo, t.issue_number)
    handed = any(c.author == bot and marker in c.body for c in item.comments)
    if approved or handed:
        return None
    return f"PR #{number} marked ready with no AI approve and no hand-over (ticket {t.id})"


class RecordingDriver:
    """Wraps the fake-backed OpenCodeDriver: keeps each prompt; can simulate a kill;
    tracks how many attempts run at once."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.prompts: list[str] = []
        self.endpoints: list[str] = []
        self.crash_next = False
        self.in_flight = 0
        self.max_in_flight = 0
        self.barrier: threading.Barrier | None = None
        self.gate: threading.Event | None = None  # when set: each run waits for it
        self._lock = threading.Lock()

    def run(self, **kw: Any) -> Any:
        self.prompts.append(kw["prompt"])
        self.endpoints.append(kw["endpoint"].name)
        if self.crash_next:
            self.crash_next = False
            raise SimulatedCrash("worker killed mid-attempt")
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=20)
            if self.gate is not None:
                self.gate.wait(timeout=20)
            return self.inner.run(**kw)
        finally:
            with self._lock:
                self.in_flight -= 1


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


# --------------------------------------------------------------------------- the env


class Env:
    def __init__(
        self,
        tmp: Path,
        *,
        verify: str | None = VERIFY,
        caps: SpendCaps | None = None,
        parallel: int = 1,
        seed_files: dict[str, str] | None = None,
        approval_usd: float | None = None,
        **config_kw: Any,
    ) -> None:
        self.tmp = tmp
        remote = tmp / "remote" / "acme" / "widgets.git"
        remote.mkdir(parents=True)
        git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp)
        seed = tmp / "seed"
        seed.mkdir()
        git("init", "-q", "-b", "main", cwd=seed)
        files = {"README.md": "# widgets\n", "status.txt": "fail\n", **(seed_files or {})}
        for name, text in files.items():
            (seed / name).write_text(text)
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "seed", cwd=seed)
        git("push", "-q", str(remote), "main", cwd=seed)
        self.remote = remote
        self.gh = LinkedFakeGitHub({REPO: remote})
        self.gh.create_repo(REPO, default_branch="main", sha=git("rev-parse", "HEAD", cwd=seed))
        self.store = Store(tmp / "fman.db")
        self.gh.store = self.store
        if verify is not None:
            self.store.set_verify_command(REPO, verify)
        self.config = Config(
            app_id=1,
            allowed_users=(USER,),
            repos=(REPO,),
            spend_caps=caps or SpendCaps(),
            max_parallel_per_repo=parallel,
            **config_kw,
        )
        self.models = FakeModels()
        self.route_label = "small"
        self.review_replies: deque[str] = deque()
        self.review_calls = 0
        self.route_calls = 0
        self.models.responder = self.responder
        self.spend = Spend(self.store, self.config, approval_usd=approval_usd)
        self.proxy = ModelProxy(
            self.config,
            store=self.store,
            transport=self.models.transport,
            spend_hook=self.spend.hook,
            key_resolver=lambda ep: "fake-test-key",
        )
        self.proxy.start()
        self.oc = FakeOpenCode(tmp / "oc", passing())
        self.driver = RecordingDriver(self.oc.driver(timeout=30))
        self.clock_now = 0.0
        self.worker = self.new_worker()

    def responder(self, body: dict[str, Any]) -> str | None:
        """Router calls get ``route_label``; reviewer calls the next queued reply (approve
        when none is queued); the writer's own calls the fake's default."""
        if body.get("max_tokens") == ROUTER_MAX_TOKENS:
            self.route_calls += 1
            return self.route_label
        if body.get("max_tokens") == MODEL_MAX_TOKENS:
            self.review_calls += 1
            return self.review_replies.popleft() if self.review_replies else APPROVE
        return None

    def new_worker(self) -> Worker:
        """A fresh worker on the same store and disk, as after a restart."""
        return Worker(
            api=self.gh,
            store=self.store,
            config=self.config,
            worktrees=WorktreeManager(self.tmp / "worktrees", identity=GitIdentity()),
            driver=self.driver,
            proxy=self.proxy,
            spend=self.spend,
            recordings_dir=self.tmp / "recordings",
            traces_dir=self.tmp / "traces",
            clone_url=lambda repo: str(self.remote),
            clock=lambda: self.clock_now,
        )

    def restart(self) -> list[int]:
        self.worker = self.new_worker()
        return self.worker.recover()

    def label(
        self, title: str = "Add a feature", body: str = "Please add feature().", *labels: str
    ) -> int:
        n = self.gh.create_issue(REPO, title, body, author=USER)
        for extra in labels:
            self.gh.apply_label(REPO, n, extra, by=USER)
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

    def reviews(self, n: int = 1):
        return self.store.list_reviews(ticket_id=self.ticket(n).id, shadow=False)

    def bot_reviews(self, pr: int):
        bot = self.gh.bot_login()
        return [r for r in self.gh._pr(REPO, pr).reviews if r.author == bot]

    def issue_comments(self, n: int = 1) -> list[str]:
        bot = self.gh.bot_login()
        return [c.body for c in self.gh._item(REPO, n).comments if c.author == bot]

    def close(self) -> None:
        self.worker.shutdown(timeout=30)
        self.proxy.stop()
        self.store.close()


__all__ = [
    "APPROVE",
    "REPO",
    "USER",
    "VERIFY",
    "Env",
    "LinkedFakeGitHub",
    "RecordingDriver",
    "SimulatedCrash",
    "failing",
    "git",
    "passing",
    "proxy_call",
    "ready_violation",
    "request_changes",
]
