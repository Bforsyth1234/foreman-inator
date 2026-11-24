"""OpenCode driver tests (todo #10). Never starts the real `opencode` or reaches a model:
the driver runs `tests/fakes/opencode.py` as its binary, or talks to it in-process."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from fakes.opencode import (
    FakeOpenCode,
    FakeOpenCodeServer,
    Script,
    ask_permission,
    ask_question,
    bash,
    crash,
    edit,
    fail,
    hang,
    read,
    say,
    spawn_child,
    tool,
)
from foreman_inator.config import ModelEndpoint
from foreman_inator.runner import opencode as oc
from foreman_inator.runner.opencode import (
    OUTCOME_FAILED,
    OpenCodeDriver,
    OpenCodeError,
    OpenCodeServer,
    OpenCodeSession,
    TextEvent,
    ToolEvent,
    build_config,
    iter_sse,
    parse_event,
)
from foreman_inator.runner.worktree import GitIdentity, Worktree, WorktreeManager
from foreman_inator.tracing import (
    Recorder,
    attempt_span,
    attempt_span_id,
    capture_spans,
    ticket_span,
)

ENDPOINT = ModelEndpoint(
    "qwen3.6-35b-a3b", 1, "local", "qwen3.6:35b-a3b", "http://127.0.0.1:11434/v1", "qwen"
)
PROXY = "http://127.0.0.1:47999/a/7/v1"
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


@pytest.fixture
def worktree(tmp_path: Path) -> Worktree:
    remote = tmp_path / "remote" / "acme" / "widgets.git"
    remote.mkdir(parents=True)
    git("init", "-q", "--bare", "--initial-branch=trunk", cwd=remote)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "--initial-branch=trunk", cwd=seed)
    (seed / "app.py").write_text("def hello():\n    return 'hi'\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "--no-gpg-sign", "-m", "first", cwd=seed)
    git("push", "-q", str(remote), "trunk", cwd=seed)
    mgr = WorktreeManager(tmp_path / "home" / "worktrees", identity=GitIdentity("bot", "b@x.y"))
    return mgr.create("acme/widgets", "trunk", 7, 1, clone_url=str(remote))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); ask ps for its state
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(st.stdout.strip()) and not st.stdout.strip().startswith("Z")


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def run(fake: FakeOpenCode, worktree, attempt_id: int = 7, **kw):
    timeout = kw.pop("timeout", 20.0)
    driver = fake.driver(**kw)
    return driver.run(
        attempt_id=attempt_id,
        worktree=worktree,
        prompt="Change hello() to return 'hello'.",
        endpoint=ENDPOINT,
        proxy_base_url=PROXY,
        timeout=timeout,
    )


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- verify bullet 1


def test_scripted_edit_yields_tool_spans_rows_and_diff(tmp_path: Path, worktree: Worktree):
    script = Script(
        [
            read("app.py"),
            tool("grep", {"pattern": "hello"}, "app.py:1: def hello():"),
            edit("app.py", "def hello():\n    return 'hello'\n"),
            bash("echo checked"),
            tool("bash", {"command": "false"}, error="exit 1"),
            say("Changed hello() to return 'hello'."),
        ],
        tokens_in=321,
        tokens_out=45,
    )
    fake = FakeOpenCode(tmp_path / "oc", script)
    rec_dir = tmp_path / "recordings"
    with capture_spans() as exporter, ticket_span(3, title="t") as tt:
        with (
            attempt_span(7, variant_id="v", model=ENDPOINT.name, tier=1, trigger="first"),
            Recorder(rec_dir, 3, 7, trace_id=tt.trace_id) as rec,
        ):
            result = run(fake, worktree)
            rec_path = rec.path
        spans = exporter.get_finished_spans()

    assert result.status == "finished" and result.finished
    assert result.outcome is None
    assert result.final_message == "Changed hello() to return 'hello'."
    assert (result.tool_calls, result.tool_errors, result.edits) == (5, 1, 1)
    assert result.assistant_messages == 1
    assert (result.tokens_in, result.tokens_out) == (321, 45)
    assert any(f.endswith("app.py") for f in result.files_edited)

    tools = sorted((s for s in spans if s.name == "tool.call"), key=lambda s: s.start_time)
    assert [s.attributes["tool.name"] for s in tools] == ["read", "grep", "edit", "bash", "bash"]
    assert [s.attributes["tool.kind"] for s in tools] == [
        "read",
        "search",
        "edit",
        "shell",
        "shell",
    ]
    assert [s.attributes["tool.is_error"] for s in tools] == [False, False, False, False, True]
    assert "checked" in tools[3].attributes["output.value"]
    want_parent = int(attempt_span_id(tt.trace_id, 7), 16)
    assert all(s.parent.span_id == want_parent for s in tools)
    assert all(s.attributes["attempt.id"] == 7 and s.attributes["ticket.id"] == 3 for s in tools)
    assert not any(s.name == "llm.call" for s in spans)  # those come from the proxy (#9)

    rows = read_rows(rec_path)
    assert [r["kind"] for r in rows if r["kind"] in ("user", "tool")] == ["user"] + ["tool"] * 5
    assert rows[1]["text"].startswith("Change hello()")
    tool_rows = [r for r in rows if r["kind"] == "tool"]
    assert tool_rows[2]["name"] == "edit" and tool_rows[2]["is_error"] is False
    assert tool_rows[4]["is_error"] is True and tool_rows[4]["content"] == "exit 1"

    assert worktree.changed_files() == ["app.py"]
    stats = worktree.diff_stats()
    assert stats.diff_files == 1 and stats.diff_lines > 0
    assert "return 'hello'" in worktree.diff_text()


# --------------------------------------------------------------------------- verify bullet 2


def test_timeout_aborts_the_session_and_marks_failed_checks(tmp_path: Path, worktree):
    """Non-vacuity: with the abort call removed from `_drive`, the driver waits the whole
    abort grace (120 s) for an idle that never comes and pytest-timeout (60 s) fails it."""
    fake = FakeOpenCode(tmp_path / "oc", Script([edit("app.py", "x = 1\n"), hang()]))
    t0 = time.monotonic()
    result = run(fake, worktree, timeout=2.0, abort_grace=120.0)
    assert result.status == "timed_out"
    assert result.outcome == OUTCOME_FAILED == "failed_checks"
    assert "timed out" in (result.error or "")
    assert time.monotonic() - t0 < 30
    assert any(c["path"].endswith("/abort") for c in fake.calls())
    assert result.tool_calls == 1  # work before the timeout is still reported
    (launch,) = fake.launches()
    assert wait_dead(launch["pid"])


def test_server_that_ignores_abort_is_killed_after_the_grace(tmp_path: Path, worktree):
    fake = FakeOpenCode(tmp_path / "oc", Script([hang()], ignore_abort=True))
    t0 = time.monotonic()
    result = run(fake, worktree, timeout=1.0, abort_grace=1.0)
    assert result.status == "timed_out" and result.outcome == "failed_checks"
    assert time.monotonic() - t0 < 20
    (launch,) = fake.launches()
    assert wait_dead(launch["pid"])


def test_default_timeout_is_twenty_minutes():
    assert oc.DEFAULT_TIMEOUT == 1200.0
    assert OpenCodeDriver().timeout == 1200.0


# --------------------------------------------------------------------------- cleanup


@pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt])
def test_process_group_is_killed_on_exception_and_ctrl_c(tmp_path: Path, worktree, exc):
    fake = FakeOpenCode(tmp_path / "oc", Script([spawn_child(), say("working"), hang()]))

    def boom(ev):
        if isinstance(ev, TextEvent) and ev.text == "working":
            raise exc("stop")

    with pytest.raises(exc):
        run(fake, worktree, on_event=boom)
    (launch,) = fake.launches()
    (child,) = fake.children()
    assert launch["pgid"] == launch["pid"]  # its own group, so the child goes with it
    assert wait_dead(launch["pid"])
    assert wait_dead(child)
    assert not list((tmp_path / "oc" / "state" / "attempts").iterdir())


def test_state_dir_is_removed_and_kept_on_request(tmp_path: Path, worktree):
    fake = FakeOpenCode(tmp_path / "oc", Script([say("ok")]))
    run(fake, worktree)
    assert not list((tmp_path / "oc" / "state" / "attempts").iterdir())
    run(fake, worktree, keep_state=True)
    (kept,) = (tmp_path / "oc" / "state" / "attempts").iterdir()
    assert (kept / "opencode.json").is_file() and (kept / "serve.log").is_file()


# --------------------------------------------------------------------------- headless setup


def test_config_env_and_isolation(tmp_path: Path, worktree, monkeypatch):
    base_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "user-home"),
        "LANG": "C",
        "ANTHROPIC_API_KEY": "sk-fake",
        "GITHUB_TOKEN": "ghs_fake",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "OPENCODE_CONFIG": "/user/global/opencode.json",
        "XDG_CONFIG_HOME": "/user/.config",
        "MY_SECRET": "x",
    }
    fake = FakeOpenCode(tmp_path / "oc", Script([say("ok")]))
    result = run(fake, worktree, base_env=base_env)
    assert result.finished
    (launch,) = fake.launches()
    assert Path(launch["cwd"]).resolve() == worktree.path.resolve()
    assert launch["argv"][-6:] == ["serve", "--pure", "--hostname", "127.0.0.1", "--port", "0"]

    cfg = launch["config"]
    prov = cfg["provider"]["fman"]
    assert prov["options"]["baseURL"] == PROXY
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert list(prov["models"]) == ["qwen3.6:35b-a3b"]
    assert cfg["model"] == "fman/qwen3.6:35b-a3b" and cfg["enabled_providers"] == ["fman"]
    assert cfg["share"] == "disabled" and cfg["autoupdate"] is False
    perms = json.dumps(cfg["permission"])
    assert '"ask"' not in perms  # an unattended run never waits for a human
    assert cfg["permission"]["external_directory"] == "deny"
    assert cfg["permission"]["question"] == "deny"
    assert cfg["permission"]["doom_loop"] == "deny"

    env = launch["env"]
    assert env["OPENCODE_CONFIG"] == launch["config_path"] != "/user/global/opencode.json"
    for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        p = Path(env[k]).resolve()
        assert p.is_relative_to((tmp_path / "oc" / "state").resolve()), k
        assert not p.is_relative_to(worktree.path.resolve()), k
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert len(env["OPENCODE_SERVER_PASSWORD"]) >= 20
    for gone in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "MY_SECRET"):
        assert gone not in launch["env_names"]
    assert not Path(launch["config_path"]).resolve().is_relative_to(worktree.path.resolve())

    # every request carried the password (the fake answers 401 otherwise) and the directory
    calls = fake.calls()
    assert {c["path"] for c in calls} >= {"/session", "/event"}
    assert all(c["query"].get("directory") == [str(worktree.path.resolve())] for c in calls)
    prompt = next(c for c in calls if c["path"].endswith("/prompt_async"))
    assert prompt["body"]["model"] == {"providerID": "fman", "modelID": "qwen3.6:35b-a3b"}
    assert prompt["body"]["parts"] == [
        {"type": "text", "text": "Change hello() to return 'hello'."}
    ]
    assert worktree.changed_files() == []  # nothing of ours lands in the PR diff


def test_build_config_strips_trailing_slash():
    cfg = build_config(ENDPOINT, "http://127.0.0.1:1/a/9/v1/")
    assert cfg["provider"]["fman"]["options"]["baseURL"] == "http://127.0.0.1:1/a/9/v1"


def test_state_dir_inside_the_worktree_is_refused(worktree):
    server = OpenCodeServer(
        worktree.path,
        config={},
        attempt_dir=worktree.path / ".oc",
        shared_dir=worktree.path / ".oc-shared",
    )
    with pytest.raises(OpenCodeError, match="outside the worktree"):
        server.start()


def test_permission_and_question_asks_are_rejected(tmp_path: Path, worktree):
    fake = FakeOpenCode(
        tmp_path / "oc", Script([ask_permission("bash", ["rm -rf /"]), ask_question(), say("ok")])
    )
    t0 = time.monotonic()
    result = run(fake, worktree)
    assert result.finished and result.permission_rejections == 2
    assert time.monotonic() - t0 < 15  # no 30 s wait in the fake: we answered at once
    replies = [c for c in fake.calls() if c["path"].startswith("/permission/")]
    assert replies and replies[0]["body"]["reply"] == "reject"
    assert any(c["path"].endswith("/reject") and "/question/" in c["path"] for c in fake.calls())


# --------------------------------------------------------------------------- failures


def test_session_error_is_errored_failed_checks(tmp_path: Path, worktree):
    fake = FakeOpenCode(tmp_path / "oc", Script([say("trying"), fail("upstream 500")]))
    result = run(fake, worktree)
    assert result.status == "errored" and result.outcome == "failed_checks"
    assert "upstream 500" in (result.error or "")


def test_server_crash_mid_run_is_errored(tmp_path: Path, worktree):
    fake = FakeOpenCode(tmp_path / "oc", Script([say("x"), crash(3)]))
    result = run(fake, worktree, timeout=30.0)
    assert result.status == "errored" and result.outcome == "failed_checks"


def test_startup_failure_is_errored(tmp_path: Path, worktree):
    bad = tmp_path / "bad-opencode"
    bad.write_text("#!/bin/sh\necho 'config invalid' >&2\nexit 1\n")
    bad.chmod(0o755)
    driver = OpenCodeDriver(opencode_bin=str(bad), state_root=tmp_path / "state")
    result = driver.run(
        attempt_id=1, worktree=worktree, prompt="x", endpoint=ENDPOINT, proxy_base_url=PROXY
    )
    assert result.status == "errored" and result.outcome == "failed_checks"
    assert "config invalid" in (result.error or "")


# --------------------------------------------------------------------------- the HTTP adapter


def test_adapter_against_fake_server_in_process(tmp_path: Path):
    (tmp_path / "w").mkdir()
    server = FakeOpenCodeServer(
        Script([edit("a.txt", "A\n"), say("all done")]), directory=tmp_path / "w", password="pw"
    )
    url = server.start()
    try:
        bad = OpenCodeSession(url, directory=tmp_path / "w", auth=("opencode", "nope"))
        with pytest.raises(OpenCodeError, match="401"):
            bad.health()
        bad.close()
        api = OpenCodeSession(url, directory=tmp_path / "w", auth=("opencode", "pw"))
        assert api.health()["version"] == "1.18.7"
        stop = threading.Event()
        got: list = []
        events = api.events(stop)
        assert isinstance(next(events), oc.Connected)
        sid = api.create("t")
        api.prompt("go", model_id="m")
        for ev in events:
            got.append(ev)
            if isinstance(ev, oc.IdleEvent) and any(
                isinstance(e, oc.StatusEvent) and e.status == "busy" for e in got
            ):
                break
        stop.set()
        tools = [e for e in got if isinstance(e, ToolEvent)]
        assert [(t.name, t.is_error) for t in tools] == [("write", False)]
        assert (tmp_path / "w" / "a.txt").read_text() == "A\n"
        assert api.final_text() == "all done"
        assert api.abort() is True
        assert server.aborted and sid in server.sessions
        api.close()
    finally:
        server.stop()


def test_parse_event_shapes_from_opencode_1_18_7():
    # Shapes as seen from the real server (GET /event) and its OpenAPI document.
    assert parse_event({"id": "evt_1", "type": "server.connected", "properties": {}}) == (
        oc.Connected()
    )
    assert parse_event(
        {"type": "session.status", "properties": {"sessionID": "ses_1", "status": {"type": "idle"}}}
    ) == oc.StatusEvent("ses_1", "idle")
    assert parse_event({"type": "session.idle", "properties": {"sessionID": "ses_1"}}) == (
        oc.IdleEvent("ses_1")
    )
    part = {
        "id": "prt_1",
        "sessionID": "ses_1",
        "messageID": "msg_1",
        "type": "tool",
        "callID": "call_1",
        "tool": "bash",
        "state": {
            "status": "error",
            "input": {"command": "false"},
            "error": "exit 1",
            "time": {"start": 1000, "end": 2000},
        },
    }
    ev = parse_event({"type": "message.part.updated", "properties": {"part": part, "time": 1}})
    assert ev == ToolEvent(
        "call_1", "bash", {"command": "false"}, "exit 1", True, "ses_1", 1000, 2000
    )
    part["state"] = {"status": "running", "input": {}, "time": {"start": 1}}
    assert parse_event({"type": "message.part.updated", "properties": {"part": part}}) is None
    err = parse_event(
        {
            "type": "session.error",
            "properties": {
                "sessionID": "ses_1",
                "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}},
            },
        }
    )
    assert err == oc.ErrorEvent("ses_1", "MessageAbortedError", "MessageAbortedError: aborted")
    assert parse_event({"type": "lsp.updated", "properties": {}}) is None


def test_iter_sse():
    lines = iter(
        [": keepalive", 'data: {"type": "a"}', "", "event: x", 'data: {"type":', 'data: "b"}', ""]
    )
    assert list(iter_sse(lines)) == [{"type": "a"}, {"type": "b"}]
