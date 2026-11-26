"""``fman chat`` (chat_cli.py, design C3b): the REPL and the subcommands, driven with a
fake ChatManager (real ``EventBuffer`` and ``parse_command``; no model, no GitHub)."""

from __future__ import annotations

import contextlib
import io
from collections.abc import Iterator
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from foreman_inator import chat_cli
from foreman_inator.chat import (
    HELP,
    ChatError,
    ChatEvent,
    ChatSession,
    ChatTurn,
    Command,
    EventBuffer,
    parse_command,
)


def _session(sid: int = 7, state: str = "idle", **kw: Any) -> ChatSession:
    base = dict(
        id=sid, repo="acme/app", base_branch="main", branch=f"fman/chat/{sid}", state=state,
        tier=1, model="qwen-local", started_at="2026-11-25T10:00:00+00:00",
    )  # fmt: skip
    base.update(kw)
    return ChatSession(**base)  # type: ignore[arg-type]


class FakeManager:
    """The slice of ``ChatManager`` the CLI uses. ``turn_events`` are emitted for each
    message (as the engine's turn thread would); ``interrupt_reads`` makes that many
    ``events()`` reads raise KeyboardInterrupt while a turn is running (Ctrl-C)."""

    def __init__(self, turn_events: list[tuple[str, dict[str, Any]]] | None = None) -> None:
        self.buf = EventBuffer()
        self.sent: list[str] = []
        self.calls: list[tuple[Any, ...]] = []
        self.turn_events = turn_events if turn_events is not None else [
            ("text", {"part": "p1", "mode": "append", "text": "Hel"}),
            ("text", {"part": "p1", "mode": "append", "text": "lo"}),
        ]  # fmt: skip
        self.hold_turn = False  # True: the turn stays busy until abort()
        self.interrupt_reads = 0
        self.seq = 0
        self.running: int | None = None
        self.state = "idle"
        self.shutdowns = 0
        self.locks_held = True
        self.raise_on_send: ChatError | None = None

    # -- lifecycle
    def start(self, repo: str, *, base_branch: str | None = None, tier: int | None = None):
        self.calls.append(("start", repo, base_branch, tier))
        return _session()

    def resume(self, session_id: int) -> ChatSession:
        self.calls.append(("resume", session_id))
        return _session(session_id)

    def shutdown(self) -> None:
        self.shutdowns += 1
        self.locks_held = False
        if self.state not in ("opened_pr", "discarded"):
            self.state = "ended"

    def get(self, session_id: int) -> ChatSession:
        return _session(session_id, state=self.state)

    def status(self, session_id: int) -> dict[str, Any]:
        return {
            "id": session_id, "repo": "acme/app", "branch": f"fman/chat/{session_id}",
            "base_branch": "main", "tier": 1, "model": "qwen-local", "kind": "local",
            "local_only": False, "network": False,
        }  # fmt: skip

    # -- events
    def emit(self, type_: str, data: dict[str, Any] | None = None) -> ChatEvent:
        return self.buf.add(type_, data)

    def events(self, session_id: int, *, after: int = 0, timeout: float | None = None):
        self.reads = getattr(self, "reads", 0) + 1
        if self.reads > 500:  # fail fast instead of hanging when a turn never ends
            raise AssertionError("the REPL kept waiting on a turn that never ended")
        if self.running is not None and self.interrupt_reads > 0:
            self.interrupt_reads -= 1
            raise KeyboardInterrupt

        def gen() -> Iterator[ChatEvent]:
            seq = after
            while True:
                got = self.buf.wait_after(seq, timeout)
                if not got:
                    return
                for ev in got:
                    seq = ev.seq
                    yield ev
                    if ev.type == "end":
                        return

        return gen()

    # -- messages and commands
    def _finish(self, status: str) -> None:
        seq = self.running
        self.running = None
        self.emit("status", {"state": "idle", "turn": seq, "status": status, "tier": 1,
                             "model": "qwen-local", "answered_by": "qwen-local",
                             "cost_usd": 0.0123})  # fmt: skip
        self.emit("cost", {"text": "session $0.01 · chat this month $0.01 of $20.00"})

    def send(self, session_id: int, text: str) -> ChatTurn | Command:
        self.sent.append(text)
        if self.raise_on_send is not None:
            raise self.raise_on_send
        parsed = parse_command(text)
        if isinstance(parsed, Command):
            self.command(session_id, parsed)
            return parsed
        if self.running is not None:
            raise ChatError("still working: /abort or wait")
        self.seq += 1
        self.running = self.seq
        self.emit("user", {"text": parsed, "turn": self.seq})
        self.emit("status", {"state": "busy", "turn": self.seq})
        for type_, data in self.turn_events:
            self.emit(type_, data)
        if not self.hold_turn:
            self._finish("finished")
        return ChatTurn(session_id, self.seq, 1, "qwen-local", "now")

    def command(self, session_id: int, cmd: Command) -> str:
        if cmd.name == "help":
            reply = HELP
        elif cmd.name == "tier":
            if cmd.arg == "9":
                self.emit("error", {"message": "tier 9 has no model", "command": "tier"})
                raise ChatError("tier 9 has no model")
            reply = "next message: tier 2, glm-5.4 (cloud)"
        elif cmd.name == "discard":
            if cmd.arg != "yes":
                reply = "type /discard yes"
            else:
                self.state = "discarded"
                self.emit("info", {"command": "discard", "text": "discarded"})
                self.emit("end", {"reason": "discarded"})
                return "discarded"
        else:
            reply = f"did /{cmd.name} {cmd.arg}".strip()
        self.emit("info", {"command": cmd.name, "text": reply})
        return reply

    def abort(self, session_id: int) -> bool:
        self.calls.append(("abort", session_id))
        if self.running is None:
            return False
        self._finish("aborted")
        return True

    # -- other subcommands
    def sessions(self) -> list[ChatSession]:
        return [_session(3, "opened_pr", pr_number=41), _session(2, "ended", tier=2,
                                                                  model="glm-5.4")]  # fmt: skip

    def cost(self, session_id: int) -> dict[str, Any]:
        return {"session_usd": 0.5 * session_id}

    def open_pr(self, session_id: int, *, title: str | None = None) -> ChatSession:
        self.calls.append(("open_pr", session_id, title))
        if session_id == 99:
            raise ChatError("chat session 99 is already opened_pr")
        return _session(session_id, "opened_pr", pr_number=12)

    def discard(self, session_id: int) -> ChatSession:
        self.calls.append(("discard", session_id))
        return _session(session_id, "discarded")


def lines_reader(lines: list[Any]):
    """A ``read_line``: each item is a line, or an exception class/instance to raise."""
    items = list(lines)
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        if not items:
            raise EOFError
        item = items.pop(0)
        if isinstance(item, BaseException) or (
            isinstance(item, type) and issubclass(item, BaseException)
        ):
            raise item
        return item

    read.prompts = prompts  # type: ignore[attr-defined]
    return read


def run(mgr: FakeManager, lines: list[Any]) -> tuple[int, str]:
    out = io.StringIO()
    code = chat_cli.run_session(
        mgr, lambda: mgr.start("acme/app"), read_line=lines_reader(lines), out=out, poll=0.01
    )
    return code, out.getvalue()


# --------------------------------------------------------------------------- REPL


def test_a_message_streams_its_events_until_the_turn_is_idle() -> None:
    mgr = FakeManager(
        [
            ("text", {"part": "p1", "mode": "append", "text": "Hel"}),
            ("text", {"part": "p1", "mode": "append", "text": "lo"}),
            ("tool", {"name": "edit", "summary": "src/a.py", "is_error": False}),
            ("fallback", {"model": "glm-5.4", "message": "local slot busy, used glm-5.4"}),
            ("warning", {"kind": "locks", "message": "files this chat edited are held by "
                         "ticket issue #5 (src/a.py); the ticket's PR may conflict"}),
        ]
    )  # fmt: skip
    code, out = run(mgr, ["fix the bug", "/quit"])
    assert code == 0
    assert mgr.sent == ["fix the bug"]  # /quit is the CLI's, never sent
    assert "chat session 7 on acme/app (fman/chat/7 from main): tier 1 qwen-local" in out
    assert "\nHello\n" in out
    assert "  [edit] src/a.py" in out
    assert "note: local slot busy, used glm-5.4" in out
    assert "warning: files this chat edited are held by ticket issue #5" in out
    assert "[turn 1 finished · tier 1 · qwen-local · $0.0123]" in out
    assert "session $0.01 · chat this month" in out
    assert mgr.shutdowns >= 1 and not mgr.locks_held
    assert "fman chat resume 7" in out and "fman chat pr 7" in out


def test_slash_lines_are_manager_commands() -> None:
    mgr = FakeManager()
    _, out = run(mgr, ["/tier up", "/model glm-5.4", "/approve", "/status", "/help", "/exit"])
    assert mgr.sent == ["/tier up", "/model glm-5.4", "/approve", "/status", "/help"]
    assert "next message: tier 2, glm-5.4 (cloud)" in out
    assert "did /approve" in out and "did /status" in out
    assert HELP in out and chat_cli.QUIT_HELP in out


def test_a_command_error_is_printed_once() -> None:
    mgr = FakeManager()
    _, out = run(mgr, ["/tier 9", "/nope", "/quit"])
    assert out.count("error: tier 9 has no model") == 1
    assert "error: unknown command /nope; /help lists them" in out


def test_busy_refusal_from_send_is_shown() -> None:
    mgr = FakeManager()
    mgr.raise_on_send = ChatError("still working: /abort or wait")
    _, out = run(mgr, ["hello", "/quit"])
    assert "error: still working: /abort or wait" in out


def test_refused_turn_says_how_to_approve() -> None:
    mgr = FakeManager(
        [("refused", {"message": "chat cap reached", "hint": "raise chat_monthly_usd",
                      "approvable": True})]
    )  # fmt: skip
    _, out = run(mgr, ["big change", "/quit"])
    assert "refused: chat cap reached" in out
    assert "raise chat_monthly_usd" in out and "/approve" in out


def test_discard_asks_y_n_first() -> None:
    mgr = FakeManager()
    _, out = run(mgr, ["/discard", "n", "/quit"])
    assert mgr.sent == [] and "not discarded" in out

    mgr = FakeManager()
    code, out = run(mgr, ["/discard", "y", "never read"])
    assert code == 0
    assert mgr.sent == ["/discard yes"]
    assert "session 7 ended (discarded)" in out
    assert "fman chat resume" not in out  # a discarded session is final


def test_ctrl_c_aborts_the_turn_and_the_session_goes_on() -> None:
    mgr = FakeManager()
    mgr.hold_turn = True
    mgr.interrupt_reads = 1
    _, out = run(mgr, ["long job", "/status", "/quit"])
    assert ("abort", 7) in mgr.calls
    assert "aborting the turn; Ctrl-C again ends the session" in out
    assert "[turn 1 aborted" in out
    assert mgr.sent == ["long job", "/status"]  # the REPL kept reading after the abort


def test_second_ctrl_c_ends_the_session_cleanly() -> None:
    mgr = FakeManager()
    mgr.hold_turn = True
    mgr.interrupt_reads = 5  # abort() here does not end the turn: hold it busy
    mgr.abort = lambda sid: True  # type: ignore[method-assign]
    code, out = run(mgr, ["long job", "never read"])
    assert code == 0
    assert "ending the session (Ctrl-C twice)" in out
    assert mgr.sent == ["long job"]
    assert mgr.shutdowns >= 1 and not mgr.locks_held  # locks released, worktree kept
    assert "session 7 is ended; worktree kept" in out


def test_two_ctrl_c_at_the_prompt_end_the_session() -> None:
    mgr = FakeManager()
    _, out = run(mgr, [KeyboardInterrupt, "/status", KeyboardInterrupt, KeyboardInterrupt])
    assert "(Ctrl-C again or /quit ends the session)" in out
    assert mgr.sent == ["/status"]  # a typed line disarms the first Ctrl-C
    assert mgr.shutdowns >= 1


def test_eof_ends_the_session() -> None:
    mgr = FakeManager()
    code, _ = run(mgr, [])
    assert code == 0 and mgr.shutdowns >= 1


def test_shutdown_finishes_even_if_interrupted() -> None:
    mgr = FakeManager()
    real = mgr.shutdown
    hits = {"n": 0}

    def flaky() -> None:
        hits["n"] += 1
        if hits["n"] == 1:
            raise KeyboardInterrupt
        real()

    mgr.shutdown = flaky  # type: ignore[method-assign]
    _, out = run(mgr, ["/quit"])
    assert not mgr.locks_held and "still ending the session" in out


def test_model_text_cannot_drive_the_terminal() -> None:
    evil = "ok\x1b[2J\x1b]0;pwned\x07\rgone\x9b31m\x00"
    mgr = FakeManager([("text", {"part": "p", "mode": "append", "text": evil})])
    _, out = run(mgr, ["hi", "/quit"])
    assert "\x1b" not in out and "\r" not in out and "\x9b" not in out
    assert "okgone31m" in out
    assert chat_cli.safe("a\tb\nc") == "a\tb\nc"


def test_rewritten_part_is_marked() -> None:
    mgr = FakeManager(
        [
            ("text", {"part": "p", "mode": "append", "text": "first"}),
            ("text", {"part": "p", "mode": "replace", "text": "second"}),
        ]
    )
    _, out = run(mgr, ["hi", "/quit"])
    assert "first\n[rewritten]\nsecond\n" in out


def test_start_failure_exits_1_and_still_shuts_down() -> None:
    mgr = FakeManager()
    out = io.StringIO()

    def opener() -> ChatSession:
        raise ChatError("sandbox unavailable, so no chat can start (7g): no backend")

    code = chat_cli.run_session(mgr, opener, read_line=lines_reader([]), out=out)
    assert code == 1 and "Error: sandbox unavailable" in out.getvalue()
    assert mgr.shutdowns == 1


def test_stream_reader_reads_lines_and_raises_eof() -> None:
    out = io.StringIO()
    read = chat_cli.stream_reader(io.StringIO("one\r\ntwo\n"), out)
    assert read("> ") == "one" and read("> ") == "two"
    with pytest.raises(EOFError):
        read("> ")
    assert out.getvalue() == "> > > "


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> tuple[typer.Typer, FakeManager]:
    mgr = FakeManager()
    exits: list[int] = []

    @contextlib.contextmanager
    def factory() -> Iterator[FakeManager]:
        try:
            yield mgr
        finally:
            exits.append(1)

    monkeypatch.setattr(chat_cli, "manager_factory", factory)
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    app = typer.Typer()

    @app.callback()
    def main() -> None:
        """root"""

    app.add_typer(chat_cli.chat_app, name="chat")  # the wiring step's line in cli.py
    mgr.factory_exits = exits  # type: ignore[attr-defined]
    return app, mgr


def test_cli_chat_repo_starts_a_repl(cli: tuple[typer.Typer, FakeManager]) -> None:
    app, mgr = cli
    res = CliRunner().invoke(
        app, ["chat", "acme/app", "--tier", "2", "--base", "dev"], input="hello\n/quit\n"
    )
    assert res.exit_code == 0, res.output
    assert mgr.calls[0] == ("start", "acme/app", "dev", 2)
    assert mgr.sent == ["hello"]
    assert "Hello" in res.output and chat_cli.PROMPT in res.output
    assert mgr.shutdowns >= 1 and mgr.factory_exits  # type: ignore[attr-defined]


def test_cli_options_before_the_repo(cli: tuple[typer.Typer, FakeManager]) -> None:
    app, mgr = cli
    res = CliRunner().invoke(app, ["chat", "--tier", "3", "acme/app"], input="")
    assert res.exit_code == 0, res.output
    assert mgr.calls[0] == ("start", "acme/app", None, 3)


def test_cli_start_refused_exits_1(
    cli: tuple[typer.Typer, FakeManager], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, mgr = cli

    def refuse(repo: str, **kw: Any) -> ChatSession:
        raise ChatError("x/y is not a watched repo (config repos)")

    monkeypatch.setattr(mgr, "start", refuse)
    res = CliRunner().invoke(app, ["chat", "x/y"], input="")
    assert res.exit_code == 1
    assert "not a watched repo" in res.output


def test_cli_resume(cli: tuple[typer.Typer, FakeManager]) -> None:
    app, mgr = cli
    res = CliRunner().invoke(app, ["chat", "resume", "4"], input="/quit\n")
    assert res.exit_code == 0, res.output
    assert mgr.calls[0] == ("resume", 4)
    assert "chat session 4 on acme/app" in res.output


def test_cli_list(cli: tuple[typer.Typer, FakeManager]) -> None:
    app, _ = cli
    res = CliRunner().invoke(app, ["chat", "list"])
    assert res.exit_code == 0, res.output
    rows = res.output.splitlines()
    assert rows[0].split()[:3] == ["ID", "STATE", "REPO"]
    assert rows[1].split()[:2] == ["3", "opened_pr"] and rows[1].endswith("#41")
    assert "glm-5.4" in rows[2] and "$1.00" in rows[2]


def test_cli_list_empty(cli: tuple[typer.Typer, FakeManager], monkeypatch) -> None:
    app, mgr = cli
    monkeypatch.setattr(mgr, "sessions", lambda: [])
    res = CliRunner().invoke(app, ["chat", "list"])
    assert res.exit_code == 0 and "No chat sessions yet" in res.output


def test_cli_pr(cli: tuple[typer.Typer, FakeManager]) -> None:
    app, mgr = cli
    res = CliRunner().invoke(app, ["chat", "pr", "5", "--title", "Fix it"])
    assert res.exit_code == 0, res.output
    assert ("open_pr", 5, "Fix it") in mgr.calls
    assert "Opened draft PR #12 from fman/chat/5." in res.output
    res = CliRunner().invoke(app, ["chat", "pr", "99"])
    assert res.exit_code == 1 and "already opened_pr" in res.output


def test_cli_discard_confirms(cli: tuple[typer.Typer, FakeManager]) -> None:
    app, mgr = cli
    res = CliRunner().invoke(app, ["chat", "discard", "5"], input="n\n")
    assert res.exit_code == 1 and "Not discarded." in res.output
    assert ("discard", 5) not in mgr.calls
    res = CliRunner().invoke(app, ["chat", "discard", "5", "--yes"])
    assert res.exit_code == 0 and ("discard", 5) in mgr.calls
    assert "nothing was pushed" in res.output


def test_cli_no_args_shows_help(cli: tuple[typer.Typer, FakeManager]) -> None:
    app, _ = cli
    res = CliRunner().invoke(app, ["chat"])
    assert "list" in res.output and "resume" in res.output and "discard" in res.output
