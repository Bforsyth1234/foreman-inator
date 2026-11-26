"""``fman chat``: the terminal front end of chat sessions (todo #71, GH #60, design C3b).

docs/chat-design.md sections 2-4 and its "Decisions (user, 2026-11-25)". Commands:

* ``fman chat OWNER/NAME [--tier N] [--base B]``: start a session and talk to it. Each
  line is a message; a ``/`` line is a ``ChatManager`` command (``/tier up``, ``/model``,
  ``/approve``, ``/status``, ``/cost``, ``/verify``, ``/pr [title]``, ``/discard``,
  ``/help``); ``/quit`` (or Ctrl-D) ends the session here, worktree kept.
* ``fman chat list``, ``fman chat resume ID``, ``fman chat pr ID``, ``fman chat discard ID``.

The REPL is synchronous: a sent message's events (text, tools, the "local slot busy, used
<model>" fallback note, lock warnings, refusals, cost) are printed as they arrive until the
turn goes idle, and no new line is read meanwhile (the engine refuses a second message
while busy anyway). Ctrl-C while a turn runs aborts it; a second Ctrl-C (or two at an idle
prompt) ends the session cleanly: ``ChatManager.shutdown`` stops the server, keeps the
worktree, releases the session's file locks and marks the row ``ended``.

Model text is untrusted: terminal control sequences (ANSI escapes, carriage returns, C1
controls) are stripped before anything is printed, so a model cannot rewrite the screen.

Everything is injectable for tests: ``manager_factory`` (a context manager yielding a
``ChatManager``-like object; tests monkeypatch it with a fake), and ``ChatRepl`` /
``run_session`` take ``read_line`` and ``out``. The wiring step registers the sub-app with
``app.add_typer(chat_app, name="chat")`` in ``cli.py``.

``fman chat OWNER/NAME`` next to ``fman chat list`` is a Click group whose first argument
is either a subcommand or a repo: ``_ChatGroup.parse_args`` sends anything that is not a
subcommand to the hidden ``start`` command (repo names contain ``/``, so they never clash).
"""

from __future__ import annotations

import contextlib
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Annotated, Any, TextIO

import typer
from typer.core import TyperGroup

from foreman_inator.chat import FINAL_STATES, ChatError, ChatEvent, ChatTurn, Command

PROMPT = "chat> "
QUIT_WORDS = frozenset({"/quit", "/exit", "/q"})
POLL_SECONDS = 0.25  # how long one events() read waits before checking again
QUIT_HELP = "/quit (or Ctrl-D): end the session here, worktree kept · Ctrl-C: abort the turn"

ReadLine = Callable[[str], str]
ManagerFactory = Callable[[], AbstractContextManager[Any]]

# --------------------------------------------------------------------------- safe output

_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")  # ESC [ ... final byte
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")  # ESC ] ... BEL / ST
_ESC = re.compile(r"\x1b[@-_]?")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t and \n; drops \r


def safe(text: Any) -> str:
    """Untrusted text (model output, file names) with terminal control sequences removed."""
    s = str(text)
    s = _OSC.sub("", _CSI.sub("", s))
    return _CTRL.sub("", _ESC.sub("", s))


# --------------------------------------------------------------------------- the REPL


class _EndSession(Exception):  # noqa: N818 - control flow, not an error
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def stream_reader(inp: TextIO | None = None, out: TextIO | None = None) -> ReadLine:
    """A ``read_line(prompt)``: ``input()`` (line editing) when ``inp`` is None, else the
    prompt written to ``out`` and one line read from ``inp`` (EOF raises ``EOFError``)."""
    if inp is None:
        return input

    def read(prompt: str) -> str:
        target = out or sys.stdout
        target.write(prompt)
        target.flush()
        line = inp.readline()
        if not line:
            raise EOFError
        return line.rstrip("\r\n")

    return read


class ChatRepl:
    """One session's terminal loop over a ``ChatManager`` (``send``, ``events``,
    ``abort``, ``status``)."""

    def __init__(
        self,
        manager: Any,
        session_id: int,
        *,
        read_line: ReadLine | None = None,
        out: TextIO | None = None,
        poll: float = POLL_SECONDS,
    ) -> None:
        self.manager = manager
        self.sid = int(session_id)
        self.read_line = read_line or input
        self.out = out if out is not None else sys.stdout
        self.poll = poll
        self.seq = 0  # last event seq shown
        self.pending: int | None = None  # the running turn's seq
        self.ended = False  # the session's ``end`` event was seen
        self._at_start = True
        self._part: str | None = None
        self._errors: list[str] = []
        self._armed = False  # one Ctrl-C at the idle prompt already

    # ---------------------------------------------------------------- output

    def _write(self, text: str) -> None:
        if not text:
            return
        self.out.write(text)
        self.out.flush()
        self._at_start = text.endswith("\n")

    def line(self, text: str) -> None:
        """``text`` on its own line (after any streamed text)."""
        if not self._at_start:
            self._write("\n")
        self._part = None
        self._write(text + "\n")

    def show(self, ev: ChatEvent) -> None:
        """Print one event (``user`` echoes and ``tier`` notes are covered elsewhere)."""
        d = ev.data
        t = ev.type
        if t == "text":
            part = str(d.get("part", ""))
            if part != self._part or d.get("mode") == "replace":
                if not self._at_start:
                    self._write("\n")
                if d.get("mode") == "replace" and part == self._part:
                    self._write("[rewritten]\n")
                self._part = part
            self._write(safe(d.get("text", "")))
        elif t == "tool":
            failed = " (failed)" if d.get("is_error") else ""
            self.line(f"  [{safe(d.get('name', 'tool'))}] {safe(d.get('summary', ''))}{failed}")
        elif t == "status":
            self._status(d)
        elif t == "cost":
            self.line(f"  {safe(d.get('text', ''))}")
        elif t == "refused":
            self.line(f"refused: {safe(d.get('message') or 'over a spend cap')}")
            if d.get("hint"):
                self.line(f"  {safe(d['hint'])}")
            if d.get("approvable"):
                self.line("  /approve sends it again, allowed once past the cap")
        elif t == "error":
            msg = safe(d.get("message", ""))
            self._errors.append(msg)
            self.line(f"error: {msg}")
        elif t == "info":
            self.line(safe(d.get("text", "")))
        elif t == "warning":
            self.line(f"warning: {safe(d.get('message', ''))}")
        elif t == "fallback":
            model = safe(d.get("model", "a cloud model"))
            self.line(f"note: {safe(d.get('message') or f'local slot busy, used {model}')}")
        elif t == "end":
            self.ended = True
            self.pending = None
            self.line(f"session {self.sid} ended ({safe(d.get('reason', 'ended'))})")

    def _status(self, d: dict[str, Any]) -> None:
        state = d.get("state")
        if state == "busy":
            if d.get("turn") is not None:
                self.pending = int(d["turn"])
        elif state == "retry":
            self.line("  (the provider asked for a retry; still working)")
        elif state == "idle":
            if d.get("turn") is not None and self.pending == int(d["turn"]):
                self.pending = None
            who = d.get("answered_by") or d.get("model") or "?"
            cost = float(d.get("cost_usd") or 0.0)
            self.line(
                f"[turn {d.get('turn')} {safe(d.get('status') or 'done')} · tier "
                f"{d.get('tier')} · {safe(who)} · ${cost:.4f}]"
            )

    # ---------------------------------------------------------------- events

    def drain(self) -> None:
        """Print every event already there (no waiting)."""
        self._read(timeout=0.0)

    def _read(self, timeout: float) -> None:
        for ev in self.manager.events(self.sid, after=self.seq, timeout=timeout):
            self.seq = max(self.seq, ev.seq)
            self.show(ev)

    def pump(self) -> None:
        """Print events as they arrive until the running turn is idle (or the session
        ends). Ctrl-C aborts the turn; a second Ctrl-C ends the session."""
        interrupts = 0
        while True:
            try:
                self._read(timeout=self.poll if self.pending is not None else 0.0)
                if self.pending is None or self.ended:
                    return
            except KeyboardInterrupt:
                interrupts += 1
                if interrupts >= 2 or self.pending is None:
                    self.line("ending the session (Ctrl-C twice)")
                    raise _EndSession("interrupt") from None
                aborted = False
                with contextlib.suppress(ChatError):
                    aborted = bool(self.manager.abort(self.sid))
                self.line(
                    "aborting the turn; Ctrl-C again ends the session"
                    if aborted
                    else "nothing to abort; Ctrl-C again ends the session"
                )

    # ---------------------------------------------------------------- input

    def handle(self, line: str) -> None:
        """One typed line: ``/quit``, the ``/discard`` y/N, else ``manager.send``."""
        stripped = line.strip()
        if not stripped:
            return
        low = stripped.lower()
        if low in QUIT_WORDS:
            raise _EndSession("quit")
        if low == "/discard":
            answer = self.read_line(
                f"Discard session {self.sid}? Its worktree and branch are deleted and"
                " nothing is pushed. [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                self.line("not discarded")
                return
            line = "/discard yes"
        self._errors = []
        try:
            result = self.manager.send(self.sid, line)
        except ChatError as exc:
            self.drain()
            msg = safe(exc)
            if msg not in self._errors:
                self.line(f"error: {msg}")
            return
        if isinstance(result, ChatTurn) and result.status is None:
            self.pending = result.seq
        self.pump()
        if isinstance(result, Command) and result.name == "help":
            self.line(QUIT_HELP)

    def banner(self) -> None:
        try:
            st = self.manager.status(self.sid)
        except ChatError as exc:
            self.line(f"chat session {self.sid} ({safe(exc)})")
            return
        extras = (" · local only" if st.get("local_only") else "") + (
            " · network on" if st.get("network") else ""
        )
        self.line(
            f"chat session {st['id']} on {safe(st['repo'])} ({safe(st['branch'])} from "
            f"{safe(st['base_branch'])}): tier {st['tier']} {safe(st['model'])}"
            + (f" ({st['kind']})" if st.get("kind") else "")
            + extras
        )
        self.line("/help lists the commands; " + QUIT_HELP)

    def run(self) -> str:
        """Loop until the user quits or the session ends. Returns why: ``quit``,
        ``eof``, ``interrupt`` or ``ended`` (``/pr`` or ``/discard`` closed it)."""
        self.banner()
        try:
            self.drain()
            while not self.ended:
                try:
                    text = self.read_line(PROMPT)
                except EOFError:
                    self.line("")
                    return "eof"
                except KeyboardInterrupt:
                    if self._armed:
                        self.line("ending the session")
                        return "interrupt"
                    self._armed = True
                    self.line("(Ctrl-C again or /quit ends the session)")
                    continue
                self._armed = False
                self.handle(text)
        except _EndSession as stop:
            return stop.reason
        return "ended"


def _shutdown(manager: Any, out: TextIO) -> None:
    """``manager.shutdown()``, finished even if Ctrl-C is pressed again meanwhile (a
    half-done shutdown would leave locks held)."""
    for _ in range(5):
        try:
            manager.shutdown()
            return
        except KeyboardInterrupt:
            out.write("still ending the session (releasing locks)...\n")
            out.flush()


def run_session(
    manager: Any,
    opener: Callable[[], Any],
    *,
    read_line: ReadLine | None = None,
    out: TextIO | None = None,
    poll: float = POLL_SECONDS,
) -> int:
    """Open a session (``opener()``: ``manager.start`` or ``manager.resume``), run the
    REPL, then shut the manager down (locks released, worktree kept). Exit code."""
    out = out if out is not None else sys.stdout
    try:
        session = opener()
    except ChatError as exc:
        out.write(f"Error: {safe(exc)}\n")
        _shutdown(manager, out)
        return 1
    repl = ChatRepl(manager, session.id, read_line=read_line, out=out, poll=poll)
    try:
        repl.run()
    finally:
        _shutdown(manager, out)
    try:
        state = manager.get(session.id).state
    except ChatError:
        state = None
    if state is not None and state not in FINAL_STATES:
        sid = session.id
        repl.line(
            f"session {sid} is {state}; worktree kept. Continue: fman chat resume {sid} · "
            f"open a draft PR: fman chat pr {sid} · drop it: fman chat discard {sid}"
        )
    return 0


# --------------------------------------------------------------------------- the manager


@contextlib.contextmanager
def default_manager() -> Iterator[Any]:
    """The real ``ChatManager``: config, the writable store, traces, the App's GitHub
    client (None when ``fman setup`` has not run), worktrees under the bot's identity.
    The manager builds and owns its proxy and ``Spend`` (the config's chat caps)."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths, tracing
    from foreman_inator.chat import ChatManager
    from foreman_inator.db.store import Store
    from foreman_inator.runner.worktree import GitIdentity, WorktreeManager

    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        raise ChatError(f"config.toml: {exc}") from exc
    store = Store(paths.db_path())
    try:
        tracing.init(traces_dir=paths.traces_dir())
        github = None
        key = paths.app_key_path()
        if cfg.app_id is not None and cfg.installation_ids and key.exists():
            from foreman_inator.worker_run import build_github

            github = build_github(cfg, key)
        bot = f"{cfg.app_slug}[bot]" if cfg.app_slug else "foreman-inator[bot]"
        worktrees = WorktreeManager(
            paths.worktrees_dir(),
            identity=GitIdentity(bot, f"{bot}@users.noreply.github.com"),
        )
        manager = ChatManager(cfg, store, github=github, worktrees=worktrees, home=paths.home())
        try:
            yield manager
        finally:
            manager.shutdown()
    finally:
        store.close()


#: Tests replace this with a factory yielding a fake manager.
manager_factory: ManagerFactory = default_manager


@contextlib.contextmanager
def _manager() -> Iterator[Any]:
    try:
        cm = manager_factory()
        manager = cm.__enter__()
    except ChatError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        yield manager
    except BaseException as exc:
        if not cm.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        cm.__exit__(None, None, None)


# --------------------------------------------------------------------------- typer app


class _ChatGroup(TyperGroup):
    """``fman chat OWNER/NAME ...`` runs the hidden ``start``; subcommands run as usual."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in ("--help", "-h"):
            args = ["start", *args]
        return super().parse_args(ctx, args)


chat_app = typer.Typer(
    cls=_ChatGroup,
    help="Interactive coding sessions through the same tiers, caps and sandbox as tickets.\n\n"
    "fman chat OWNER/NAME [--tier N] [--base B] starts one; see also list, resume, pr,"
    " discard.",
    no_args_is_help=True,
)


def _fail(exc: BaseException) -> None:
    typer.echo(f"Error: {safe(exc)}", err=True)
    raise typer.Exit(1) from exc


@chat_app.command("start", hidden=True)
def chat_start(
    repo: Annotated[str, typer.Argument(help="Watched repo, OWNER/NAME.")],
    tier: Annotated[
        int | None,
        typer.Option("--tier", help="Start tier (default chat_start_tier).", show_default=False),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", help="Base branch (default: the repo's).", show_default=False),
    ] = None,
) -> None:
    """Start a chat session on REPO and talk to it."""
    with _manager() as manager:
        code = run_session(
            manager,
            lambda: manager.start(repo, base_branch=base, tier=tier),
            read_line=stream_reader(),
        )
    raise typer.Exit(code)


@chat_app.command("resume")
def chat_resume(
    session_id: Annotated[int, typer.Argument(help="An ended session's id (fman chat list).")],
) -> None:
    """Reopen an ended session in its kept worktree, with its history."""
    with _manager() as manager:
        code = run_session(manager, lambda: manager.resume(session_id), read_line=stream_reader())
    raise typer.Exit(code)


@chat_app.command("list")
def chat_list() -> None:
    """Every chat session, newest first."""
    with _manager() as manager:
        rows = manager.sessions()
        if not rows:
            typer.echo("No chat sessions yet. Start one: fman chat OWNER/NAME")
            return
        typer.echo(f"{'ID':>4}  {'STATE':<10} {'REPO':<28} {'TIER':<4} {'MODEL':<22} "
                   f"{'COST':>7}  {'STARTED':<20} PR")  # fmt: skip
        for s in rows:
            try:
                cost = f"${float(manager.cost(s.id)['session_usd']):.2f}"
            except (ChatError, KeyError, TypeError, ValueError):
                cost = "?"
            pr = f"#{s.pr_number}" if s.pr_number else ""
            typer.echo(
                f"{s.id:>4}  {safe(s.state):<10} {safe(s.repo):<28} {s.tier:<4} "
                f"{safe(s.model):<22} {cost:>7}  {safe(s.started_at)[:19]:<20} {pr}".rstrip()
            )


@chat_app.command("pr")
def chat_pr(
    session_id: Annotated[int, typer.Argument(help="Session id (fman chat list).")],
    title: Annotated[
        str | None,
        typer.Option("--title", help="PR title (default: the first message).", show_default=False),
    ] = None,
) -> None:
    """Open a session's work as a draft PR (no AI review; you were in the loop)."""
    with _manager() as manager:
        try:
            s = manager.open_pr(session_id, title=title)
        except ChatError as exc:
            _fail(exc)
        typer.echo(f"Opened draft PR #{s.pr_number} from {safe(s.branch)}.")


@chat_app.command("discard")
def chat_discard(
    session_id: Annotated[int, typer.Argument(help="Session id (fman chat list).")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask to confirm.")] = False,
) -> None:
    """Drop a session: worktree and branch deleted, nothing pushed."""
    if not yes and not typer.confirm(
        f"Discard chat session {session_id}? Its worktree and branch are deleted.", default=False
    ):
        typer.echo("Not discarded.")
        raise typer.Exit(1)
    with _manager() as manager:
        try:
            manager.discard(session_id)
        except ChatError as exc:
            _fail(exc)
        typer.echo(f"Discarded chat session {session_id}: nothing was pushed.")


__all__ = [
    "PROMPT",
    "ChatRepl",
    "chat_app",
    "default_manager",
    "manager_factory",
    "run_session",
    "safe",
    "stream_reader",
]
