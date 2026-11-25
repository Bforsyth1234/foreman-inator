"""Chat foundations in the OpenCode runner (todo #71, wave C1c): ``build_chat_config``,
``ChatServer`` (one server, many turns, resume) and the chat sandbox's network opt-in.

Never starts the real `opencode` or reaches a model: the server is
`tests/fakes/opencode.py` run as a binary (``FakeOpenCode``)."""

from __future__ import annotations

import gzip
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from fakes.opencode import FakeOpenCode, Script, ask_permission, edit, hang, say
from foreman_inator.chat import Rung, tier_ladder
from foreman_inator.config import ModelEndpoint
from foreman_inator.runner import opencode as oc
from foreman_inator.runner import sandbox as sb
from foreman_inator.runner.opencode import (
    CHAT_REJECT,
    PERMISSIONS,
    ChatBusy,
    ChatServer,
    OpenCodeError,
    build_chat_config,
    build_config,
    chat_sandbox,
)
from foreman_inator.runner.sandbox import Policy
from foreman_inator.tracing import Recorder

PROXY = "http://127.0.0.1:47998/c/tok/v1"
LOCAL = ModelEndpoint("qwen-small", 1, "local", "qwen3:8b", "http://127.0.0.1:11434/v1", "qwen")
MID = ModelEndpoint("qwen-big", 2, "local", "qwen3:32b", "http://127.0.0.1:11434/v1", "qwen")
CLOUD = ModelEndpoint(
    "sonnet", 3, "cloud", "claude-sonnet-5", "https://api.anthropic.com/v1", "claude",
    wire="anthropic",
)  # fmt: skip
LADDER = tier_ladder([LOCAL, MID, CLOUD])


# --------------------------------------------------------------------------- config


def test_chat_config_lists_the_ladder_per_wire_and_stays_as_closed_as_an_attempt():
    cfg = build_chat_config(LADDER, LOCAL, PROXY + "/")
    assert cfg["provider"] == {
        "fman": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "foreman-inator proxy",
            "options": {"baseURL": PROXY, "apiKey": "fman-proxy"},
            "models": {
                "qwen3:8b": {"name": "qwen-small", "tool_call": True},
                "qwen3:32b": {"name": "qwen-big", "tool_call": True},
            },
        },
        "fman-anthropic": {
            "npm": "@ai-sdk/anthropic",
            "name": "foreman-inator proxy",
            "options": {"baseURL": PROXY, "apiKey": "fman-proxy"},
            "models": {"claude-sonnet-5": {"name": "sonnet", "tool_call": True}},
        },
    }
    assert cfg["model"] == cfg["small_model"] == "fman/qwen3:8b"
    assert cfg["enabled_providers"] == ["fman", "fman-anthropic"]
    assert oc.config_models(cfg) == {
        "qwen3:8b": "fman",
        "qwen3:32b": "fman",
        "claude-sonnet-5": "fman-anthropic",
    }
    # Everything but the models is the attempt's config: same permissions, nothing on.
    attempt = build_config(LOCAL, PROXY)
    for key in ("$schema", "share", "autoupdate", "snapshot", "lsp", "formatter"):
        assert cfg[key] == attempt[key]
    assert cfg["permission"] is PERMISSIONS
    assert "instructions" not in cfg


def test_chat_config_takes_endpoints_adds_a_missing_small_model_and_refuses_bad_ladders():
    cfg = build_chat_config([CLOUD], LOCAL, PROXY)
    assert cfg["model"] == "fman-anthropic/claude-sonnet-5"
    assert cfg["small_model"] == "fman/qwen3:8b"
    assert set(oc.config_models(cfg)) == {"claude-sonnet-5", "qwen3:8b"}
    assert build_chat_config([Rung(1, LOCAL)], LOCAL, PROXY)["enabled_providers"] == ["fman"]
    with pytest.raises(ValueError):
        build_chat_config([], LOCAL, PROXY)
    clash = ModelEndpoint(
        "x", 2, "cloud", "qwen3:8b", "https://x.invalid/v1", "q", wire="anthropic"
    )
    with pytest.raises(ValueError, match="two wires"):
        build_chat_config([LOCAL, clash], LOCAL, PROXY)
    with pytest.raises(TypeError):
        build_chat_config(["qwen3:8b"], LOCAL, PROXY)


# --------------------------------------------------------------------------- sandbox


def _policy(tmp_path: Path) -> Policy:
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return Policy.create(wt, scratch, home=tmp_path / "home", foreman_home=tmp_path / "fh")


def test_chat_network_opt_in_uses_the_first_install_outbound_rule(tmp_path: Path):
    policy = _policy(tmp_path)
    assert chat_sandbox(policy) is policy  # default: closed, as for tickets
    assert chat_sandbox(policy, network=False) is policy
    opened = chat_sandbox(policy, network=True)
    assert opened is not None
    assert opened.outbound_ports == sb.INSTALL_PORTS == (443, 80)
    assert opened.network == "none"  # ports only, not the repo's "allow"
    profile = sb.render_profile(opened)
    assert '(remote ip "*:443")' in profile and '(remote ip "*:80")' in profile
    assert '(remote ip "*:443")' not in sb.render_profile(policy)
    env = sb.child_env(opened, "/usr/bin:/bin")
    assert "UV_OFFLINE" not in env  # the network is the point of the opt-in
    assert sb.child_env(policy, "/usr/bin:/bin")["UV_OFFLINE"] == "1"
    # Nothing else widens: reads, writes, the home, the Keychain's services.
    assert (opened.reads, opened.writes, opened.home) == (policy.reads, policy.writes, policy.home)
    assert "com.apple.securityd" not in profile
    off = Policy(worktree=tmp_path, scratch=tmp_path, mode="off")
    assert chat_sandbox(off, network=True) is off
    assert chat_sandbox(None, network=True) is None


def test_chat_server_binds_the_writer_policy_with_proxy_listen_and_network(tmp_path: Path):
    policy = _policy(tmp_path)
    cfg = build_chat_config(LADDER, LOCAL, PROXY)

    def bound(network: bool) -> Policy:
        server = ChatServer(
            policy.worktree, chat_id=4, config=cfg, state_dir=tmp_path / "state" / "c4",
            shared_dir=tmp_path / "state" / "shared", sandbox=policy, network=network,
        )  # fmt: skip
        p = server.server.sandbox_policy()
        assert p is not None
        return p

    closed, opened = bound(False), bound(True)
    for p in (closed, opened):
        assert p.proxy_port == 47998 and p.listen_loopback
        assert (tmp_path / "state" / "c4").resolve() in p.writes
    assert closed.outbound_ports == () and opened.outbound_ports == (443, 80)


# --------------------------------------------------------------------------- the server


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    p = tmp_path / "wt"
    p.mkdir()
    (p / "app.py").write_text("x = 0\n")
    return p


def make(fake: FakeOpenCode, wt: Path, tmp_path: Path, **kw) -> ChatServer:
    kw.setdefault("turn_timeout", 20.0)
    return ChatServer(
        wt,
        chat_id=kw.pop("chat_id", 3),
        config=build_chat_config(LADDER, LOCAL, PROXY),
        state_dir=kw.pop("state_dir", tmp_path / "state" / "chats" / "3"),
        shared_dir=tmp_path / "state" / "shared",
        opencode_bin=fake.bin,
        startup_timeout=20.0,
        **kw,
    )


def prompts(fake: FakeOpenCode) -> list[dict]:
    return [c for c in fake.calls() if c["path"].endswith("/prompt_async")]


def test_one_server_many_turns_each_with_its_own_model_and_tally(tmp_path: Path, wt: Path):
    """Verify (#71): one `opencode serve` and one OpenCode session for the whole chat; each
    message names the current tier's model (a tier move = the next prompt's modelID); each
    turn's counts are its own, and its final text is its own."""
    fake = FakeOpenCode(tmp_path / "oc", Script([edit("app.py", "x = 1\n"), say("did it")]))
    server = make(fake, wt, tmp_path)
    rec = Recorder(tmp_path / "rec", 0, 3, role="chat")
    try:
        sid = server.start()
        assert not server.resumed and server.alive()
        first = server.send("make x one", "qwen3:8b")
        kinds = [type(e).__name__ for e in first]
        r1 = first.result
        assert r1 is not None and r1.finished, r1
        assert "ToolEvent" in kinds and "Connected" not in kinds
        assert (r1.tool_calls, r1.edits, r1.final_message) == (1, 1, "did it")
        r2 = server.send("now on the cloud tier", "claude-sonnet-5").wait()
        assert r2.finished and (r2.tool_calls, r2.edits) == (1, 1)  # not 2: per turn
        assert r2.model_id == "claude-sonnet-5" and r2.final_message == "did it"
        assert not server.busy
    finally:
        server.stop()
        rec.close()
    assert len(fake.launches()) == 1
    assert [c["path"] for c in fake.calls()].count("/session") == 1
    bodies = [c["body"] for c in prompts(fake)]
    assert [b["model"] for b in bodies] == [
        {"providerID": "fman", "modelID": "qwen3:8b"},
        {"providerID": "fman-anthropic", "modelID": "claude-sonnet-5"},
    ]
    assert {c["path"] for c in prompts(fake)} == {f"/session/{sid}/prompt_async"}
    assert (wt / "app.py").read_text() == "x = 1\n"
    # The default tool recorder wrote one `tool` row per call into the chat recording.
    with gzip.open(rec.path, "rt") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert [r["name"] for r in rows if r["kind"] == "tool"] == ["edit", "edit"]
    # stop() keeps the state dir (resume needs it); the launch used it as XDG_DATA_HOME.
    state = (tmp_path / "state" / "chats" / "3").resolve()
    assert (state / "data").is_dir()
    assert fake.launches()[0]["env"]["XDG_DATA_HOME"] == str(state / "data")


def test_a_model_outside_the_config_and_a_send_while_busy_are_refused(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([hang()]))
    server = make(fake, wt, tmp_path)
    try:
        server.start()
        with pytest.raises(OpenCodeError, match="not in this chat"):
            server.send("x", "gpt-unknown")
        turn = server.send("work", "qwen3:8b")
        assert server.busy
        with pytest.raises(ChatBusy):
            server.send("again", "qwen3:8b")
        assert server.abort()
        result = turn.wait()
    finally:
        server.stop()
    assert result.status == "aborted"
    assert len(prompts(fake)) == 1


def test_abort_from_another_thread_ends_the_turn_and_keeps_the_server(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([hang()]))
    server = make(fake, wt, tmp_path, abort_grace=10.0)
    try:
        server.start()
        turn = server.send("long job", "qwen3:32b")
        threading.Timer(0.5, server.abort).start()
        t0 = time.monotonic()
        result = turn.wait()
        assert time.monotonic() - t0 < 8
        assert result.status == "aborted" and server.alive()
        assert not server.abort()  # nothing running now
    finally:
        server.stop()
    assert any(c["path"].endswith("/abort") for c in fake.calls())


def test_turn_timeout_aborts_and_an_ignored_abort_stops_the_server(tmp_path: Path, wt):
    """7e: a turn never runs past its limit unseen. OpenCode ignoring the abort for the
    grace period means the server is stopped, not left running."""
    fake = FakeOpenCode(tmp_path / "oc", Script([hang()], ignore_abort=True))
    server = make(fake, wt, tmp_path, turn_timeout=1.0, abort_grace=1.0)
    try:
        server.start()
        result = server.send("x", "qwen3:8b").wait()
        assert result.status == "timed_out" and "timed out after 1s" in (result.error or "")
        assert not server.alive()
    finally:
        server.stop()


def test_extra_time_extends_the_turn_limit(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([hang()]))
    server = make(fake, wt, tmp_path, turn_timeout=0.5, extra_time=lambda: 1.5)
    try:
        server.start()
        t0 = time.monotonic()
        result = server.send("x", "qwen3:8b").wait()
        assert result.status == "timed_out" and time.monotonic() - t0 >= 1.9
        assert "queued for a local-model slot" in (result.error or "")
    finally:
        server.stop()


def test_leaving_a_turn_early_aborts_it(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([say("partial"), hang()]))
    server = make(fake, wt, tmp_path)
    try:
        server.start()
        turn = server.send("x", "qwen3:8b")
        for ev in turn:
            if isinstance(ev, oc.TextEvent):
                break
        it = iter(turn)
        it.close()  # type: ignore[attr-defined]
        assert turn.result is not None and turn.result.status == "aborted"
        assert not server.busy
    finally:
        server.stop()
    assert any(c["path"].endswith("/abort") for c in fake.calls())


def test_permission_asks_are_rejected_with_the_chat_message(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([ask_permission("webfetch"), say("ok")]))
    server = make(fake, wt, tmp_path)
    try:
        server.start()
        result = server.send("fetch the docs", "qwen3:8b").wait()
    finally:
        server.stop()
    assert result.finished and result.permission_rejections == 1
    replies = [c for c in fake.calls() if c["path"].startswith("/permission/")]
    assert [c["body"] for c in replies] == [{"reply": "reject", "message": CHAT_REJECT}]


def test_a_failed_prompt_is_an_errored_turn_not_an_exception(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([say("x")], prompt_status=500))
    server = make(fake, wt, tmp_path)
    try:
        server.start()
        turn = server.send("x", "qwen3:8b")
        assert turn.done and list(turn) == []
        assert turn.result is not None and turn.result.status == "errored"
        assert "500" in (turn.result.error or "")
        assert not server.busy
    finally:
        server.stop()


def test_startup_failure_raises_and_leaves_nothing_running(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([say("x")], startup_exit=4))
    server = make(fake, wt, tmp_path)
    with pytest.raises(OpenCodeError):
        server.start()
    assert not server.alive()


# --------------------------------------------------------------------------- resume

# The fake keeps sessions in memory; this shim persists them under XDG_DATA_HOME the way
# OpenCode 1.18.7 does (its opencode.db), so a second launch can reopen one.
PERSIST = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, {tests!r})
import fakes.opencode as fo
store = Path(os.environ["XDG_DATA_HOME"]) / "fake-sessions.json"
_init, _create = fo.FakeOpenCodeServer.__init__, fo.FakeOpenCodeServer.create_session
def init(self, *a, **k):
    _init(self, *a, **k)
    for sid, info in (json.loads(store.read_text()) if store.exists() else {{}}).items():
        self.sessions[sid] = fo._Session(sid, info["directory"], info["title"])
def create(self, body):
    info = _create(self, body)
    data = json.loads(store.read_text()) if store.exists() else {{}}
    data[info["id"]] = info
    store.write_text(json.dumps(data))
    return info
fo.FakeOpenCodeServer.__init__, fo.FakeOpenCodeServer.create_session = init, create
sys.exit(fo.main())
"""


def persistent_fake(root: Path, script: Script) -> FakeOpenCode:
    fake = FakeOpenCode(root, script)
    shim = root / "persist.py"
    shim.write_text(PERSIST.format(tests=str(Path(__file__).parent)))
    Path(fake.bin).write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{shim}" --fake-script "{fake.script_path}" '
        f'--fake-record "{fake.record_dir}" "$@"\n'
    )
    os.chmod(fake.bin, 0o755)
    return fake


def test_resume_reopens_the_session_on_a_new_server(tmp_path: Path, wt):
    """Rule 12 finding: a new server on the kept XDG_DATA_HOME serves the old session, so
    resume keeps prompting the same session id (no new session, no summary prompt)."""
    fake = persistent_fake(tmp_path / "oc", Script([say("hello")]))
    first = make(fake, wt, tmp_path)
    try:
        sid = first.start()
        assert first.send("one", "qwen3:8b").wait().finished
    finally:
        first.stop()
    second = make(fake, wt, tmp_path)
    try:
        assert second.start(resume=sid) == sid
        assert second.resumed
        assert second.send("two", "qwen3:32b").wait().finished
    finally:
        second.stop()
    assert len(fake.launches()) == 2
    assert [c["path"] for c in fake.calls()].count("/session") == 1  # created once
    assert [c["path"] for c in prompts(fake)] == [f"/session/{sid}/prompt_async"] * 2


def test_resume_of_an_unknown_session_starts_a_new_one(tmp_path: Path, wt):
    fake = FakeOpenCode(tmp_path / "oc", Script([say("hello")]))
    server = make(fake, wt, tmp_path)
    try:
        sid = server.start(resume="ses_gone")
        assert sid != "ses_gone" and not server.resumed
        assert server.send("hi", "qwen3:8b").wait().finished
    finally:
        server.stop()
