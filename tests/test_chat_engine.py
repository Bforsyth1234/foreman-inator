"""The chat engine (todo #71 / GH #60, wave 20 slot A): ``chat.ChatManager``.

Real store, real git (a bare remote in tmp_path), a real ``ModelProxy`` on loopback over
``FakeModels`` with a real ``Spend`` as its hook, and a fake ``ChatServer`` that streams
scripted OpenCode events and makes its model calls through the session's proxy URL,
exactly as ``opencode serve`` would. A fake GitHub takes the PR.

Covers decisions 1-7 of docs/chat-design.md (2026-11-25) and invariants 7a (draft PR
only, no merge or ready), 7c (local by default, local-only never reaches cloud, fallback
included) and 7d (no secrets in events or recordings).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from fakes.github import FAKE_TOKEN, FakeGitHub
from fakes.models import FakeModels
from foreman_inator import chat, tracing
from foreman_inator.chat import ChatError, ChatManager, VerifyOutcome
from foreman_inator.config import Config, ModelEndpoint, SpendCaps, key_ref
from foreman_inator.db.store import Store
from foreman_inator.locks import Locks
from foreman_inator.pr.publisher import body_ticket_id
from foreman_inator.proxy.server import LocalSlots, ModelProxy
from foreman_inator.runner.opencode import (
    ErrorEvent,
    TextEvent,
    ToolEvent,
    TurnResult,
    config_models,
    config_proxy_url,
)
from foreman_inator.runner.worktree import GitIdentity, WorktreeManager
from foreman_inator.spend import Spend
from foreman_inator.tracing.recorder import chat_recording_path

OLLAMA = "http://127.0.0.1:11434/v1"
SMALL = ModelEndpoint("qwen-small", 1, "local", "qwen3:8b", OLLAMA, "qwen")
BIG = ModelEndpoint("qwen-big", 2, "local", "qwen3:32b", OLLAMA, "qwen")
CLAUDE = ModelEndpoint(
    "claude", 3, "cloud", "claude-opus-4-5", "https://api.anthropic.com/v1", "claude",
    key_ref("anthropic"),
)  # fmt: skip
MODELS = (SMALL, BIG, CLAUDE)
REPO = "acme/widgets"
API_KEY = "sk-fake-" + secrets.token_hex(16)  # generated at test time (7d)

SEED = {
    "GIT_AUTHOR_NAME": "Seed",
    "GIT_AUTHOR_EMAIL": "seed@example.invalid",
    "GIT_COMMITTER_NAME": "Seed",
    "GIT_COMMITTER_EMAIL": "seed@example.invalid",
}


def git(*args: str, cwd: Path, check: bool = True) -> str:
    env = (
        {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        | SEED
        | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    )
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check:
        assert p.returncode == 0, p.stderr
    return p.stdout.strip()


# --------------------------------------------------------------------------- fakes


class FakeTurn:
    """One turn of the fake server: an iterator of DriverEvents, ``result`` at the end.

    Steps: ("say", text[, part]), ("llm",) one model call through the proxy,
    ("edit", path, content), ("hang",) until aborted, ("tool", name, input)."""

    def __init__(self, server: FakeServer, text: str, model_id: str, steps: list[tuple]):
        self.server, self.text, self.model_id, self.steps = server, text, model_id, steps
        self.result: TurnResult | None = None
        self._gen = self._run()

    def __iter__(self):
        return self._gen

    def _run(self):
        srv, sid = self.server, self.server.session_id or "ses"
        status, error, edits, tools = "finished", None, 0, 0
        try:
            for step in self.steps:
                if srv.aborted.is_set():
                    status = "aborted"
                    break
                op = step[0]
                if op == "say":
                    yield TextEvent(sid, "msg1", step[2] if len(step) > 2 else "p1", step[1])
                elif op == "llm":
                    url = config_proxy_url(srv.config) + "/chat/completions"
                    body = {"model": self.model_id, "messages": [{"role": "user", "content": "go"}]}
                    r = httpx.post(url, json=body, timeout=10)
                    if r.status_code != 200:
                        status, error = "errored", f"HTTP {r.status_code}"
                        yield ErrorEvent(sid, "APIError", r.text[:200])
                        break
                elif op == "edit":
                    path = srv.worktree / step[1]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(step[2])
                    edits += 1
                    tools += 1
                    yield ToolEvent(f"c{tools}", "write", {"filePath": step[1]}, "ok", False, sid)
                elif op == "tool":
                    tools += 1
                    yield ToolEvent(f"c{tools}", step[1], step[2], "out", False, sid)
                elif op == "hang":
                    srv.hanging.set()
                    srv.aborted.wait(10)
                    status = "aborted"
                    break
        finally:
            self.result = TurnResult(
                status, self.model_id, "", error=error, tool_calls=tools, edits=edits
            )


class FakeServer:
    def __init__(self, harness: Harness, **kw: Any):
        self.h = harness
        self.kw = kw
        self.config = kw["config"]
        self.worktree = Path(kw["worktree_path"])
        self.session_id: str | None = None
        self.starts: list[str | None] = []
        self.sent: list[tuple[str, str]] = []
        self.stopped = False
        self.aborted = threading.Event()
        self.hanging = threading.Event()

    def start(self, *, resume: str | None = None, title: str | None = None) -> str:
        self.starts.append(resume)
        self.session_id = resume or f"ses_{self.kw['session_id']}"
        return self.session_id

    def send(self, text: str, model_id: str) -> FakeTurn:
        assert model_id in config_models(self.config)
        self.sent.append((text, model_id))
        self.aborted.clear()
        steps = self.h.scripts.pop(0) if self.h.scripts else [("say", "ok")]
        return FakeTurn(self, text, model_id, steps)

    def abort(self) -> bool:
        self.aborted.set()
        return True

    def stop(self) -> None:
        self.stopped = True
        self.aborted.set()


class GitHub(FakeGitHub):
    """The fake GitHub sees the branch the manager pushed to the (real) bare remote."""

    def create_draft_pr(self, repo, *, head, base, title, body):
        if head not in self.repos[repo].branches:
            self.push_branch(repo, head)
        return super().create_draft_pr(repo, head=head, base=base, title=title, body=body)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.remote = self._remote(tmp)
        self.store = Store(tmp / "fman.db")
        self.home = tmp / "home"
        self.config = self.make_config()
        self.spend = Spend(self.store, self.config, tz="UTC", chat_turn_budget_usd=1.5)
        self.models = FakeModels()
        self.slots = LocalSlots(1)
        self.proxy = ModelProxy(
            self.config,
            store=self.store,
            spend_hook=self.spend.hook,
            key_resolver=lambda ep: API_KEY,
            transport=self.models.transport,
            local_slots=self.slots,
            chat_local_wait_seconds=0.2,
        ).start()
        self.github = GitHub()
        self.github.create_repo(REPO, default_branch="trunk")
        self.worktrees = WorktreeManager(tmp / "wts", identity=GitIdentity("bot", "b@x.y"))
        self.scripts: list[list[tuple]] = []
        self.servers: list[FakeServer] = []
        self.clock = Clock()
        self.managers: list[ChatManager] = []
        self.verify = VerifyOutcome(True, "pytest: passed", "3 passed")

    def make_config(self, **kw: Any) -> Config:
        caps = SpendCaps(daily_usd=5.0, monthly_usd=50.0, experiments_monthly_usd=10.0)
        base = dict(
            repos=(REPO,), models=MODELS, sandbox="off", reviewer_models=("claude",),
            spend_caps=caps,
        )  # fmt: skip
        return Config(**(base | kw))

    @staticmethod
    def _remote(tmp: Path) -> Path:
        remote = tmp / "remote" / "acme" / "widgets.git"
        remote.mkdir(parents=True)
        git("init", "-q", "--bare", "--initial-branch=trunk", cwd=remote)
        seed = tmp / "seed"
        seed.mkdir()
        git("init", "-q", "--initial-branch=trunk", cwd=seed)
        (seed / "app.py").write_text("x = 0\n")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "--no-gpg-sign", "-m", "first", cwd=seed)
        git("push", "-q", str(remote), "trunk", cwd=seed)
        git("checkout", "-q", "-b", "fman/issue-7/a1", cwd=seed)
        (seed / "app.py").write_text("x = 7\n")
        git("commit", "-q", "--no-gpg-sign", "-am", "ticket work", cwd=seed)
        git("push", "-q", str(remote), "fman/issue-7/a1", cwd=seed)
        return remote

    def factory(self, **kw: Any) -> FakeServer:
        s = FakeServer(self, **kw)
        self.servers.append(s)
        return s

    def manager(self, **kw: Any) -> ChatManager:
        args: dict[str, Any] = dict(
            proxy=self.proxy,
            spend=self.spend,
            worktrees=self.worktrees,
            github=self.github,
            server_factory=self.factory,
            sandbox_ready=lambda: None,
            verifier=lambda session, wt: self.verify,
            clone_url=lambda repo: str(self.remote),
            home=self.home,
            clock=self.clock,
            reaper_interval=None,
        )
        args.update(kw)
        config = args.pop("config", self.config)
        m = ChatManager(config, self.store, **args)
        self.managers.append(m)
        return m

    def run(self, mgr: ChatManager, sid: int, text: str, *steps: tuple) -> chat.ChatTurn:
        self.scripts.append(list(steps))
        mgr.send(sid, text)
        turn = mgr.wait_turn(sid, 60)
        live = mgr._live.get(sid)
        assert live is None or live.thread is None or not live.thread.is_alive()
        assert turn is not None and turn.status is not None
        return turn

    def close(self) -> None:
        for m in self.managers:
            m.shutdown()
        self.proxy.stop()
        self.store.close()


@pytest.fixture
def h(tmp_path: Path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


@pytest.fixture
def spans():
    with tracing.capture_spans() as exporter:
        yield exporter


def types(mgr: ChatManager, sid: int) -> list[str]:
    return [e.type for e in mgr._buf(sid).after(0)]


def of(mgr: ChatManager, sid: int, kind: str) -> list[dict]:
    return [e.data for e in mgr._buf(sid).after(0) if e.type == kind]


def hosts(models: FakeModels) -> list[str]:
    return [httpx.URL(r.url).host for r in models.requests]


def hold_local_slot(slots: LocalSlots) -> None:
    asyncio.run(slots.acquire())  # in-process slots are not bound to one loop


# --------------------------------------------------------------------------- start


def test_start_opens_a_local_tier_1_session_on_its_chat_branch(h: Harness):
    mgr = h.manager()
    s = mgr.start(REPO)
    assert (s.state, s.tier, s.model, s.branch, s.base_branch) == (
        "idle", 1, "qwen-small", "fman/chat/1", "trunk",
    )  # fmt: skip
    wt = Path(s.worktree)
    assert git("symbolic-ref", "--short", "HEAD", cwd=wt) == "fman/chat/1"
    assert s.base_commit == git("rev-parse", "trunk", cwd=h.remote)
    assert s.opencode_session_id == "ses_1" and s.trace_id
    (srv,) = h.servers
    assert srv.kw["network"] is False  # decision 4: closed by default
    assert set(config_models(srv.config)) == {"qwen3:8b", "qwen3:32b", "claude-opus-4-5"}
    assert srv.config["small_model"].endswith("qwen3:8b")
    assert config_proxy_url(srv.config).startswith(h.proxy.url + "/c/")
    assert of(mgr, 1, "tier") == [{"tier": 1, "model": "qwen-small", "kind": "local"}]
    assert (srv.kw["state_dir"] / chat.HOST_FILE).exists()


def test_chat_network_opt_in_reaches_the_server(h: Harness):
    """Decision 4: the per-repo opt-in gives the chat sandbox the install network."""
    h.store.set_chat_network(REPO, True)
    h.manager().start(REPO)
    assert h.servers[0].kw["network"] is True


@pytest.mark.parametrize("base", ["fman/issue-7/a1", "fman/chat/9"])
def test_chat_never_starts_from_an_fman_branch(h: Harness, base: str):
    """Decision 5: base branch only; nothing is created."""
    with pytest.raises(ChatError, match="base"):
        h.manager().start(REPO, base_branch=base)
    assert h.store.list_chat_sessions() == []


def test_unwatched_repo_is_refused(h: Harness):
    with pytest.raises(ChatError, match="not a watched repo"):
        h.manager().start("someone/else")


def test_no_sandbox_backend_fails_closed_with_nothing_created(h: Harness):
    """7g: the gate runs first; no row, no worktree, no server."""
    mgr = h.manager(sandbox_ready=lambda: "sandbox-exec not found")
    with pytest.raises(ChatError, match="sandbox unavailable"):
        mgr.start(REPO)
    assert h.store.list_chat_sessions() == [] and h.servers == []
    assert not (h.tmp / "wts" / "acme" / "widgets" / "chat-1").exists()


def test_one_open_session_at_a_time(h: Harness):
    """Decision 7: a second start is refused until the first one closes."""
    mgr = h.manager()
    mgr.start(REPO)
    with pytest.raises(ChatError, match="already open"):
        mgr.start(REPO)
    other = h.manager(recover=False)  # another host process on the same database
    with pytest.raises(ChatError, match="already open"):
        other.start(REPO)
    mgr.discard(1)
    assert mgr.start(REPO).id == 2


def test_a_failed_start_is_failed_and_leaves_no_worktree(h: Harness):
    def broken(**kw):
        raise RuntimeError("opencode exited")

    mgr = h.manager(server_factory=broken)
    with pytest.raises(ChatError, match="could not start"):
        mgr.start(REPO)
    row = h.store.get_chat_session(1)
    assert row.state == "failed" and "opencode exited" in row.end_reason
    assert not Path(row.worktree).exists()
    assert h.spend.refusal_for_chat(1) is None and h.spend._chats == {}


# --------------------------------------------------------------------------- turns


def test_a_turn_streams_events_and_records_usage(h: Harness, spans):
    mgr = h.manager()
    mgr.start(REPO)
    turn = h.run(
        mgr, 1, "fix the bug",
        ("say", "Hel"), ("llm",), ("say", "Hello"), ("tool", "bash", {"command": "ls -la"}),
    )  # fmt: skip
    assert (turn.status, turn.seq, turn.tier, turn.model) == ("finished", 1, 1, "qwen-small")
    assert (turn.tokens_in, turn.tokens_out, turn.tool_calls) == (11, 7, 1)
    assert types(mgr, 1)[2:] == ["user", "status", "text", "text", "tool", "status", "cost"]
    assert of(mgr, 1, "text") == [
        {"part": "p1", "mode": "append", "text": "Hel"},
        {"part": "p1", "mode": "append", "text": "lo"},
    ]
    assert of(mgr, 1, "tool") == [
        {"name": "bash", "kind": "shell", "summary": "bash ls -la", "is_error": False}
    ]
    idle = of(mgr, 1, "status")[-1]
    assert idle["state"] == "idle" and idle["answered_by"] == "qwen-small"
    assert idle["fallback"] is None
    assert h.servers[0].sent == [("fix the bug", "qwen3:8b")]
    assert hosts(h.models) == ["127.0.0.1"]  # 7c: tier 1 is local
    assert mgr.get(1).state == "idle"
    (span,) = [s for s in spans.get_finished_spans() if s.name == "chat.turn"]
    assert span.attributes["chat.turn"] == 1 and span.attributes["chat.answered_by"] == "qwen-small"
    (call,) = [s for s in spans.get_finished_spans() if s.name == "llm.call"]
    assert call.attributes["chat.id"] == 1 and call.attributes["chat.turn"] == 1
    assert call.context.trace_id == span.context.trace_id


def test_a_second_message_while_busy_is_refused_and_abort_ends_the_turn(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.scripts.append([("hang",)])
    mgr.send(1, "long job")
    assert h.servers[0].hanging.wait(5)
    assert mgr.get(1).state == "busy"
    with pytest.raises(ChatError, match="still working"):
        mgr.send(1, "and this")
    assert isinstance(mgr.send(1, "/abort"), chat.Command)
    assert mgr.wait_turn(1, 60).status == "aborted"
    assert mgr.get(1).state == "idle"
    assert len(h.store.list_chat_turns(1)) == 1  # the refused line never became a turn


def test_tier_move_applies_to_the_next_message_only(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.scripts.append([("hang",)])
    mgr.send(1, "first")
    assert h.servers[0].hanging.wait(5)
    mgr.send(1, "/tier up")  # while the first turn runs
    h.servers[0].abort()
    first = mgr.wait_turn(1, 60)
    assert (first.tier, first.model) == (1, "qwen-small")
    second = h.run(mgr, 1, "second", ("llm",))
    assert (second.tier, second.model) == (2, "qwen-big")
    assert [m for _, m in h.servers[0].sent] == ["qwen3:8b", "qwen3:32b"]
    assert h.models.requests[-1].json["model"] == "qwen3:32b"
    assert (mgr.get(1).tier, mgr.get(1).model) == (2, "qwen-big")
    assert mgr.send(1, "/model qwen-small") and mgr.get(1).tier == 1
    with pytest.raises(ChatError, match="lowest tier"):
        mgr.send(1, "/tier down")


def test_cloud_turn_cost_equals_the_ledger(h: Harness):
    """Decision 1 (option C): chat spend is live spend, tagged, and the cost line is the
    ledger's own sum."""
    mgr = h.manager()
    mgr.start(REPO, tier=3)
    turn = h.run(mgr, 1, "hard thing", ("llm",), ("llm",))
    assert turn.status == "finished" and turn.cost_usd > 0
    assert hosts(h.models) == ["api.anthropic.com", "api.anthropic.com"]
    cost = mgr.cost(1)
    assert cost["session_usd"] == pytest.approx(h.store.chat_spend(1))
    assert cost["session_usd"] == pytest.approx(turn.cost_usd)
    assert cost["chat_cap_usd"] == 20.0 and cost["month_cap_usd"] == 50.0
    assert cost["chat_month_usd"] == pytest.approx(turn.cost_usd)
    assert of(mgr, 1, "cost")[-1]["text"].startswith(f"session ${turn.cost_usd:.2f}")


def test_cap_refusal_waits_for_approve_and_the_lift_is_one_turn(h: Harness):
    """Decision 1: at the chat sub-cap nothing is sent until /approve, which re-sends
    the refused message once; the next message is refused again."""
    mgr = h.manager()
    mgr.start(REPO, tier=3)
    h.store.record_spend("live", 20.0, model="claude", chat_session_id=1)  # sub-cap full
    turn = h.run(mgr, 1, "expensive", ("llm",))
    assert turn.status == "refused" and h.models.requests == []
    (refused,) = of(mgr, 1, "refused")
    assert refused["cap"] == "chat_monthly" and refused["approvable"] is True
    assert "/approve" in refused["hint"]
    h.scripts.append([("llm",)])
    mgr.send(1, "/approve")
    again = mgr.wait_turn(1, 60)
    assert (again.seq, again.status) == (2, "finished")
    assert h.servers[0].sent[-1][0] == "expensive" and hosts(h.models) == ["api.anthropic.com"]
    later = h.run(mgr, 1, "more", ("llm",))
    assert later.status == "refused" and len(h.models.requests) == 1


def test_turn_budget_refusal_stops_the_turn_and_is_not_approvable(h: Harness):
    """7e: a call past chat_turn_budget_usd is refused by the proxy's spend hook; the
    turn ends refused and nothing re-sends it."""
    h.spend.chat_turn_budget_usd = 1e-9
    mgr = h.manager()
    mgr.start(REPO, tier=3)
    turn = h.run(mgr, 1, "loop forever", ("llm",), ("llm",), ("say", "never"))
    assert turn.status == "refused"
    (refused,) = of(mgr, 1, "refused")
    assert refused["cap"] == "chat_turn" and refused["approvable"] is False
    assert len(h.servers[0].sent) == 1 and h.models.requests == []


# --------------------------------------------------------------------------- decision 2


def test_busy_local_slot_falls_back_to_cloud_for_that_turn_only(h: Harness, spans):
    mgr = h.manager()
    mgr.start(REPO)
    hold_local_slot(h.slots)  # a ticket's local call holds the only slot
    try:
        turn = h.run(mgr, 1, "quick fix", ("llm",))
    finally:
        h.slots.release()
    assert turn.status == "finished" and (turn.tier, turn.model) == (1, "qwen-small")
    assert hosts(h.models) == ["api.anthropic.com"]
    (fb,) = of(mgr, 1, "fallback")
    assert fb == {
        "model": "claude",
        "from": "qwen-small",
        "message": "local slot busy, used claude",
    }
    idle = of(mgr, 1, "status")[-1]
    assert idle["answered_by"] == "claude" and idle["fallback"] == "claude"
    assert turn.cost_usd > 0 and h.store.chat_spend(1) == pytest.approx(turn.cost_usd)
    (span,) = [s for s in spans.get_finished_spans() if s.name == "chat.turn"]
    assert span.attributes["chat.local_fallback"] == "local slot busy, used claude"
    # the session's tier did not change; the next turn tries local again
    assert mgr.get(1).tier == 1
    nxt = h.run(mgr, 1, "again", ("llm",))
    assert of(mgr, 1, "status")[-1]["answered_by"] == "qwen-small" and nxt.cost_usd == 0
    assert hosts(h.models)[-1] == "127.0.0.1"


def test_fallback_refused_at_the_chat_sub_cap_keeps_waiting_for_local(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.store.record_spend("live", 20.0, model="claude", chat_session_id=1)
    hold_local_slot(h.slots)
    threading.Timer(0.6, h.slots.release).start()
    t0 = time.monotonic()
    turn = h.run(mgr, 1, "quick fix", ("llm",))
    assert time.monotonic() - t0 >= 0.5  # it waited for the slot
    assert turn.status == "finished" and hosts(h.models) == ["127.0.0.1"]
    assert of(mgr, 1, "fallback") == []


# --------------------------------------------------------------------------- 7c local only


def test_local_only_repo_never_reaches_cloud_even_through_fallback(h: Harness):
    h.store.set_cloud(REPO, False)
    mgr = h.manager()
    mgr.start(REPO)
    assert [r.tier for r in mgr.ladder(1)] == [1, 2]
    with pytest.raises(ChatError, match="tier 3 has no model"):
        mgr.send(1, "/tier 3")
    mgr.send(1, "/tier up")
    with pytest.raises(ChatError, match="top tier"):
        mgr.send(1, "/tier up")
    hold_local_slot(h.slots)
    threading.Timer(0.6, h.slots.release).start()
    turn = h.run(mgr, 1, "work", ("llm",))
    assert turn.status == "finished"
    assert hosts(h.models) == ["127.0.0.1"] and of(mgr, 1, "fallback") == []
    ctx = mgr._live[1].ctx
    mgr._live[1].turn = chat._Turn(0, 9, 1, "qwen-small", "x")  # a turn asking the hook
    assert mgr._fallback(ctx) is None
    mgr._live[1].turn = None


def test_repo_set_local_only_mid_session_refuses_the_cloud_turn(h: Harness):
    mgr = h.manager()
    mgr.start(REPO, tier=3)
    h.store.set_cloud(REPO, False)
    turn = h.run(mgr, 1, "send my code out", ("llm",))
    assert turn.status == "refused" and h.models.requests == []
    (refused,) = of(mgr, 1, "refused")
    assert refused["cap"] == "local_only" and refused["approvable"] is False
    mgr.send(1, "/approve")  # no approval lifts it
    assert mgr.wait_turn(1, 60).status == "refused" and h.models.requests == []


# --------------------------------------------------------------------------- decision 6


def ticket_holding(store: Store, path: str) -> int:
    t = store.create_ticket(
        repo=REPO, issue_number=42, title="t", request="r", base_branch="trunk",
        owned_files=(path,),
    )  # fmt: skip
    store.set_column(t.id, "in_progress")
    assert Locks(store).acquire(t.id).ok
    return t.id


def test_chat_locks_what_it_edits_and_warns_about_ticket_holders(h: Harness):
    tid = ticket_holding(h.store, "app.py")
    mgr = h.manager()
    mgr.start(REPO)
    turn = h.run(mgr, 1, "edit", ("edit", "app.py", "x = 1\n"), ("edit", "lib/new.py", "y\n"))
    assert turn.status == "finished" and turn.edits == 2  # a chat never waits on locks
    assert [lk.path for lk in h.store.chat_lock_rows(1)] == ["lib/new.py"]
    (warn,) = of(mgr, 1, "warning")
    assert warn["holders"] == [{"ticket": tid, "issue": 42, "paths": ["app.py"]}]
    assert "#42" in warn["message"]
    h.run(mgr, 1, "again", ("say", "no edits"))
    assert len(of(mgr, 1, "warning")) == 1  # not repeated while unchanged
    mgr.open_pr(1)
    assert h.store.chat_lock_rows(1) == []  # released on /pr


# --------------------------------------------------------------------------- 7a / decision 3


def test_open_pr_is_a_draft_with_no_ai_review_and_nothing_merges(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.run(mgr, 1, "Add a flag\nmore detail", ("edit", "app.py", "x = 2\n"))
    wt = Path(mgr.get(1).worktree)
    s = mgr.open_pr(1)
    assert s.state == "opened_pr" and s.pr_number
    assert [c.method for c in h.github.writes] == ["create_draft_pr"]
    args = h.github.writes[0].args
    assert (args["head"], args["base"], args["title"]) == ("fman/chat/1", "trunk", "Add a flag")
    pr = h.github.get_pr(REPO, s.pr_number)
    assert pr.draft is True and pr.merged is False
    assert "Not reviewed by the AI reviewer" in args["body"]
    assert "Opened from chat session 1" in args["body"]
    assert body_ticket_id(args["body"]) is None  # the worker never adopts it
    assert "tier 1 qwen-small" in args["body"] and "Sandbox: OFF" in args["body"]
    # the remote got the branch with the change; the local worktree is gone
    assert git("show", "fman/chat/1:app.py", cwd=h.remote) == "x = 2"
    assert not wt.exists()
    assert h.servers[0].stopped and of(mgr, 1, "end") == [
        {"reason": "opened_pr", "pr_number": s.pr_number}
    ]
    with pytest.raises(ChatError, match="already opened_pr"):
        mgr.discard(1)


def test_failing_verify_does_not_block_but_leads_the_body(h: Harness):
    h.verify = VerifyOutcome(False, "pytest: FAILED", "1 failed ghp_" + "a" * 36)
    mgr = h.manager()
    mgr.start(REPO)
    h.run(mgr, 1, "change", ("edit", "app.py", "x = 3\n"))
    mgr.send(1, "/pr My title")
    body = h.github.writes[0].args["body"]
    assert body.startswith("**Verify failed**")
    assert "ghp_" not in body  # 7d
    assert h.github.writes[0].args["title"] == "My title"


def test_pr_with_no_changes_is_refused_and_the_session_stays_open(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    with pytest.raises(ChatError, match="nothing to open"):
        mgr.open_pr(1)
    assert mgr.get(1).state == "idle" and h.github.writes == []


def test_discard_removes_worktree_and_branch_and_pushes_nothing(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.run(mgr, 1, "edit", ("edit", "app.py", "x = 5\n"))
    wt = Path(mgr.get(1).worktree)
    assert h.store.chat_lock_rows(1)
    mgr.send(1, "/discard")
    assert mgr.get(1).state == "idle"  # asked to confirm first
    mgr.send(1, "/discard yes")
    assert mgr.get(1).state == "discarded" and not wt.exists()
    mirror = h.worktrees.mirror(REPO, str(h.remote))
    assert git("branch", "--list", "fman/chat/*", cwd=mirror.mirror_path) == ""
    assert git("branch", "--list", "fman/chat/*", cwd=h.remote) == ""
    assert h.github.writes == [] and h.store.chat_lock_rows(1) == []


# --------------------------------------------------------------------------- ends


def test_idle_timeout_ends_the_session_keeps_the_worktree_releases_locks(h: Harness):
    mgr = h.manager(idle_minutes=30)
    mgr.start(REPO)
    h.run(mgr, 1, "edit", ("edit", "app.py", "x = 6\n"))
    assert h.store.chat_lock_rows(1)
    h.clock.t += 29 * 60
    assert mgr.end_idle() == []
    h.clock.t += 2 * 60
    assert mgr.end_idle() == [1]
    s = mgr.get(1)
    assert (s.state, s.end_reason) == ("ended", "idle_timeout")
    assert Path(s.worktree).exists() and h.store.chat_lock_rows(1) == []
    assert h.servers[0].stopped and of(mgr, 1, "end") == [{"reason": "idle_timeout"}]
    assert chat_recording_path(h.home / "recordings", 1).exists()


def test_resume_reopens_the_same_opencode_session_in_the_kept_worktree(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.run(mgr, 1, "First line of work", ("edit", "app.py", "x = 8\n"))
    mgr.shutdown()
    assert mgr.get(1).state == "ended" and mgr.get(1).end_reason == "host_exit"
    again = h.manager()
    s = again.resume(1)
    assert s.state == "idle" and s.opencode_session_id == "ses_1"
    assert h.servers[-1].starts == ["ses_1"]
    assert Path(h.servers[-1].kw["worktree_path"]) == Path(s.worktree)
    assert (Path(s.worktree) / "app.py").read_text() == "x = 8\n"
    side = h.home / "recordings" / "chat" / "1.files"
    assert list(side.glob("before-resume-*.jsonl.gz"))  # the earlier part is kept
    assert h.run(again, 1, "go on", ("say", "ok")).seq == 2
    again.open_pr(1)
    assert h.github.writes[0].args["title"] == "First line of work"  # from the recording


def test_an_ended_session_can_be_opened_as_a_pr_without_resuming(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.run(mgr, 1, "Tidy up", ("edit", "app.py", "x = 9\n"))
    mgr.shutdown()
    later = h.manager()
    assert later.open_pr(1).state == "opened_pr"
    assert h.github.writes[0].args["title"] == "Tidy up"


def test_restart_settles_rows_no_live_host_claims_and_releases_their_locks(h: Harness):
    st = h.store
    busy = st.create_chat_session(REPO, base_branch="trunk", tier=1, model="qwen-small").id
    st.set_chat_state(busy, "busy")
    running = st.add_chat_turn(busy, tier=1, model="qwen-small")
    st.insert_chat_locks(busy, REPO, ["app.py"])
    starting = st.create_chat_session(REPO, base_branch="trunk", tier=1, model="qwen-small").id
    ended = st.create_chat_session(REPO, base_branch="trunk", tier=1, model="qwen-small").id
    st.set_chat_state(ended, "ended", end_reason="idle_timeout")
    st.insert_chat_locks(ended, REPO, ["stale.py"])  # a crash between two steps
    h.manager()  # default host_alive: no host.json for any of them
    assert (st.get_chat_session(busy).state, st.get_chat_session(busy).end_reason) == (
        "ended", "host_restart",
    )  # fmt: skip
    assert st.get_chat_turn(running.id).status == "errored"
    assert st.get_chat_session(starting).state == "failed"
    assert st.list_locks() == []


def test_restart_leaves_a_session_another_live_process_hosts(h: Harness, tmp_path: Path):
    st = h.store
    sids = [
        st.create_chat_session(REPO, base_branch="trunk", tier=1, model="qwen-small").id
        for _ in range(3)
    ]
    dead = subprocess.Popen(["true"])
    dead.wait()
    pids = [os.getppid(), dead.pid, os.getpid()]  # alive elsewhere, dead, "us" (not hosted)
    for sid, pid in zip(sids, pids, strict=True):
        st.set_chat_state(sid, "idle")
        d = h.home / "opencode" / "chats" / str(sid)
        d.mkdir(parents=True)
        (d / chat.HOST_FILE).write_text(json.dumps({"pid": pid}))
    st.insert_chat_locks(sids[0], REPO, ["app.py"])
    h.manager()
    assert [st.get_chat_session(s).state for s in sids] == ["idle", "ended", "ended"]
    assert [lk.chat_session_id for lk in st.list_locks()] == [sids[0]]


def test_shutdown_aborts_a_running_turn_and_ends_every_session(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.scripts.append([("hang",)])
    mgr.send(1, "long")
    assert h.servers[0].hanging.wait(5)
    mgr.shutdown()
    assert mgr.get(1).state == "ended" and h.store.list_chat_turns(1)[0].status == "aborted"


def test_events_replay_after_last_event_id_and_stop_at_end(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    h.run(mgr, 1, "hi", ("say", "yo"))
    first = list(mgr.events(1, timeout=0.05))
    assert [e.seq for e in first] == list(range(1, len(first) + 1))
    cut = first[3].seq
    assert [e.seq for e in mgr.events(1, after=cut, timeout=0.05)] == [e.seq for e in first[4:]]
    got: list[str] = []
    reader = threading.Thread(
        target=lambda: got.extend(e.type for e in mgr.events(1, after=first[-1].seq))
    )
    reader.start()
    mgr.discard(1)
    reader.join(5)
    assert got == ["end"]
    frame = first[0].to_sse()
    assert frame.startswith("id: 1\nevent: status\ndata: ")


def test_commands_reply_as_info_events(h: Harness):
    mgr = h.manager()
    mgr.start(REPO)
    mgr.send(1, "/help")
    mgr.send(1, "/status")
    mgr.send(1, "/model")
    mgr.send(1, "/cost")
    mgr.send(1, "/verify")
    infos = of(mgr, 1, "info")
    assert [i["command"] for i in infos] == ["help", "status", "model", "cost", "verify"]
    assert "/approve" in infos[0]["text"]
    assert "tier 1 qwen-small" in infos[1]["text"] and "locks: none" in infos[1]["text"]
    assert "ladder 1:qwen-small, 2:qwen-big, 3:claude" in infos[2]["text"]
    assert infos[3]["text"].startswith("session $0.00 · chat this month $0.00 of $20.00")
    assert infos[4]["text"].startswith("pytest: passed")
    with pytest.raises(ChatError, match="unknown command"):
        mgr.send(1, "/tierr")


# --------------------------------------------------------------------------- 7d


def test_no_secrets_in_events_or_recordings(h: Harness):
    pasted = "my key is sk-ant-" + "b" * 30 + " and Authorization: Bearer abcdefghijkl123"
    gh = "ghp_" + "c" * 36
    mgr = h.manager()
    mgr.start(REPO)
    h.run(mgr, 1, pasted, ("say", f"echo {gh} {FAKE_TOKEN}"), ("llm",),
          ("tool", "bash", {"command": f"curl -H 'x-api-key: {API_KEY}'"}))  # fmt: skip
    mgr._token()  # the App token this manager handled is a secret too
    h.run(mgr, 1, f"token {FAKE_TOKEN}", ("say", "ok"))
    mgr.shutdown()
    shown = json.dumps([e.data for e in mgr._buf(1).after(0)])
    with gzip.open(chat_recording_path(h.home / "recordings", 1), "rt") as fh:
        recorded = fh.read()
    for secret in ("sk-ant-", "abcdefghijkl123", gh, API_KEY, FAKE_TOKEN):
        assert secret not in shown, secret
    for secret in ("sk-ant-", "abcdefghijkl123", API_KEY, FAKE_TOKEN):
        assert secret not in recorded, secret
    assert "[redacted]" in shown and "my key is" in recorded
