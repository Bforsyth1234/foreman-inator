"""Chat sessions: interactive coding through the ladder, proxy, caps and sandbox (todo #71).

The design is ``docs/chat-design.md`` (its "Decisions (user, 2026-11-25)" override the
earlier text). This module holds the pure helpers (command parser, chat branch name,
per-session tier ladder and tier moves, SSE framing, text-part delta, event buffer) and
``ChatManager``, the engine both front ends (``fman chat``, ``/chat``) call. It is built
on the wave 19 pieces: the store's chat tables (migration 012), the proxy's chat
contexts and local-slot fallback hook (``proxy.server``), ``runner.opencode.ChatServer``
and ``build_chat_config``, ``RepoMirror.create_chat/open_chat``, the chat spend gate
(``spend.Spend`` chat methods, ``spend.chat_cloud_fallback``) and the chat file locks
(``locks.Locks.chat_touch/release_chat``).

SSE framing follows the WHATWG HTML standard, "Server-sent events", section 9.2.6
(https://html.spec.whatwg.org/multipage/server-sent-events.html, read 2026-11-25): a
frame is ``field: value`` lines ended by a blank line; ``id`` sets the last event id the
browser sends back as ``Last-Event-ID`` on reconnect; ``event`` names the type; each
``data`` line is one line of payload. The payload here is one line of JSON
(``json.dumps`` escapes newlines), so model text can never end a frame early or forge
another field.

Resume (rule 12): OpenCode 1.18.7 keeps sessions in ``<XDG_DATA_HOME>/opencode/
opencode.db``; a server started again on the same data dir answers ``GET /session/{id}``
with the old session and its full message list, so resuming an ``ended`` chat is "start a
new server on the kept state dir ``<home>/opencode/chats/<id>`` and keep prompting the
stored session id" (``ChatServer.start(resume=...)``; checked against the binary and
https://opencode.ai/docs/server/ on 2026-11-25, recorded in ``ChatServer``'s docstring).
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from foreman_inator import tracing
from foreman_inator.config import TIERS, ModelEndpoint
from foreman_inator.db.store import NotFoundError, StoreError
from foreman_inator.locks import Locks
from foreman_inator.runner import opencode as oc
from foreman_inator.runner.worktree import (
    BranchRefused,
    Worktree,
    WorktreeError,
    WorktreeManager,
    check_chat_base,
)
from foreman_inator.spend import Refusal, Spend, chat_cloud_fallback
from foreman_inator.tracing import otel
from foreman_inator.tracing.recorder import chat_recorder, chat_recording_path

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- defaults
# The wiring step adds the config keys (docs/chat-design.md section 13) with these values.

DEFAULT_START_TIER = 1  # chat_start_tier: the cheap local tier
DEFAULT_CHAT_MONTHLY_USD = 20.0  # spend_caps.chat_monthly_usd (option C sub-cap)
DEFAULT_TURN_BUDGET_USD = 1.50  # chat_turn_budget_usd (= config.attempt_budget_usd default)
DEFAULT_IDLE_MINUTES = 30  # chat_idle_minutes
DEFAULT_MAX_SESSIONS = 1  # chat_max_sessions (per host process)
EVENT_BUFFER = 2000  # events kept per session for Last-Event-ID replay

CHAT_BRANCH_PREFIX = "fman/chat/"  # invariant 7a: every bot branch starts with fman/
RECORDER_ROLE = "chat"

ChatState = Literal[
    "starting", "idle", "busy", "closing", "opened_pr", "discarded", "ended", "failed"
]
#: States a session can still take messages in (a live OpenCode server).
LIVE_STATES: frozenset[str] = frozenset({"starting", "idle", "busy"})
#: States after which nothing more happens to the worktree.
FINAL_STATES: frozenset[str] = frozenset({"opened_pr", "discarded", "failed"})

TurnStatus = Literal["finished", "aborted", "timed_out", "errored", "refused"]

#: ``user`` echoes a sent message (scrubbed); ``info`` is a command's reply; ``warning``
#: a ticket holding a file the chat edited (decision 6); ``fallback`` "local slot busy,
#: used <model>" (decision 2).
EventType = Literal[
    "text", "tool", "status", "tier", "cost", "refused", "error", "end",
    "user", "info", "warning", "fallback",
]  # fmt: skip
EVENT_TYPES: frozenset[str] = frozenset(
    {"text", "tool", "status", "tier", "cost", "refused", "error", "end"}
    | {"user", "info", "warning", "fallback"}
)


class ChatError(RuntimeError):
    """A chat request that cannot be done (shown to the user as is)."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Rung:
    """One tier of a session's ladder: the first model of that tier in try order."""

    tier: int
    endpoint: ModelEndpoint


@dataclass(frozen=True)
class ChatSession:
    """A ``chat_sessions`` row (migration 012, wave C1a) as the manager sees it."""

    id: int
    repo: str
    base_branch: str
    branch: str
    state: ChatState
    tier: int
    model: str
    started_at: str
    worktree: str | None = None
    base_commit: str | None = None
    opencode_session_id: str | None = None
    trace_id: str | None = None
    pr_number: int | None = None
    end_reason: str | None = None
    ended_at: str | None = None


@dataclass(frozen=True)
class ChatTurn:
    """A ``chat_turns`` row: one user message and the model's work on it."""

    session_id: int
    seq: int
    tier: int
    model: str
    started_at: str
    status: TurnStatus | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    edits: int = 0
    ended_at: str | None = None


@dataclass(frozen=True)
class ChatEvent:
    """One event for the page (SSE) or the terminal printer."""

    seq: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown chat event type {self.type!r}")
        if self.seq < 1:
            raise ValueError("chat event seq starts at 1")

    def to_sse(self) -> str:
        """The event as one SSE frame: ``id``, ``event``, one ``data`` line of JSON."""
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        # json.dumps never emits a raw CR or LF (they are escaped inside strings), and
        # U+2028/U+2029 are not SSE line breaks, so the payload is always one data line.
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n"


@dataclass(frozen=True)
class Command:
    """An in-chat command (``/tier up`` ...); ``arg`` is the rest of the line, stripped."""

    name: Literal[
        "tier", "model", "abort", "cost", "status", "verify", "pr", "discard", "approve", "help"
    ]  # fmt: skip
    arg: str = ""


COMMANDS: frozenset[str] = frozenset(
    {"tier", "model", "abort", "cost", "status", "verify", "pr", "discard", "approve", "help"}
)
_ALIASES = {"up": ("tier", "up"), "down": ("tier", "down")}


# --------------------------------------------------------------------------- pure helpers


def parse_command(text: str) -> Command | str:
    """A message line -> a ``Command``, or the text to send to the model.

    A line starting with ``/`` is a command (``/up`` and ``/down`` are short for ``/tier
    up|down``); ``//`` sends the rest as text with one ``/`` removed. An unknown command
    raises ``ChatError`` rather than going to the model (a typo must not cost a turn)."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return text
    if stripped.startswith("//"):
        return stripped[1:]
    head, _, rest = stripped[1:].partition(" ")
    name = head.lower()
    if name in _ALIASES:
        name, arg = _ALIASES[name]
        return Command(name, arg)  # type: ignore[arg-type]
    if name not in COMMANDS:
        raise ChatError(f"unknown command /{head}; /help lists them")
    return Command(name, rest.strip())  # type: ignore[arg-type]


def chat_branch(session_id: int) -> str:
    """The session's branch, ``fman/chat/<id>`` (7a: always under ``fman/``)."""
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id < 1:
        raise ValueError(f"chat session id must be a positive int, not {session_id!r}")
    return f"{CHAT_BRANCH_PREFIX}{session_id}"


def tier_ladder(models: Iterable[ModelEndpoint], *, local_only: bool = False) -> tuple[Rung, ...]:
    """The session's ladder: per tier, the first model in try order; tiers with no model
    skipped. ``local_only`` (#49, a repo set ``cloud = false``) keeps local models only."""
    first: dict[int, ModelEndpoint] = {}
    for m in models:
        if local_only and m.kind != "local":
            continue
        if m.tier in TIERS and m.tier not in first:
            first[m.tier] = m
    return tuple(Rung(t, first[t]) for t in sorted(first))


def rung_for(ladder: tuple[Rung, ...], tier: int) -> Rung:
    """The rung of ``tier``; ``ChatError`` when the ladder has none."""
    for r in ladder:
        if r.tier == tier:
            return r
    have = ", ".join(str(r.tier) for r in ladder) or "none"
    raise ChatError(f"tier {tier} has no model for this session (tiers: {have})")


def start_rung(
    ladder: tuple[Rung, ...], tier: int | None = None, *, default_tier: int = DEFAULT_START_TIER
) -> Rung:
    """The rung a session starts on: ``tier`` if given (must exist), else the lowest rung
    at or above ``default_tier`` (config ``chat_start_tier``), else the lowest rung."""
    if not ladder:
        raise ChatError(
            "no model is available for this repo (a local-only repo needs a local model)"
        )
    if tier is not None:
        return rung_for(ladder, tier)
    for r in ladder:
        if r.tier >= default_tier:
            return r
    return ladder[0]


def move_tier(ladder: tuple[Rung, ...], current: int, arg: str) -> Rung:
    """``/tier up`` / ``down`` / ``N`` from ``current``. Applies to the next message only
    (the caller's job). ``ChatError`` at the top, the bottom or for a tier not in the
    ladder; for a local-only ladder the top is the last local tier."""
    arg = arg.strip().lower()
    tiers = [r.tier for r in ladder]
    if arg in ("", "up"):
        higher = [r for r in ladder if r.tier > current]
        if not higher:
            raise ChatError(f"already on the top tier available here (tier {current})")
        return higher[0]
    if arg == "down":
        lower = [r for r in ladder if r.tier < current]
        if not lower:
            raise ChatError(f"already on the lowest tier available here (tier {current})")
        return lower[-1]
    if arg.isdigit():
        return rung_for(ladder, int(arg))
    raise ChatError(f"/tier takes up, down or a tier number ({tiers}), not {arg!r}")


def text_delta(prev: str, new: str) -> tuple[Literal["append", "replace"], str]:
    """OpenCode sends a text part's whole text so far (``runner.opencode.TextEvent``).
    Returns ``("append", suffix)`` when ``new`` extends ``prev`` (the usual case), else
    ``("replace", new)`` (the part was rewritten)."""
    if new.startswith(prev):
        return "append", new[len(prev) :]
    return "replace", new


class EventBuffer:
    """The last ``limit`` events of a session, for ``Last-Event-ID`` replay. Thread-safe:
    the OpenCode event pump appends while SSE handlers read."""

    def __init__(self, limit: int = EVENT_BUFFER) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self.limit = limit
        self._events: list[ChatEvent] = []
        self._seq = 0
        self._lock = threading.Condition()

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def add(self, type_: str, data: dict[str, Any] | None = None) -> ChatEvent:
        with self._lock:
            ev = ChatEvent(self._seq + 1, type_, dict(data or {}))
            self._seq = ev.seq
            self._events.append(ev)
            if len(self._events) > self.limit:
                del self._events[: len(self._events) - self.limit]
            self._lock.notify_all()
            return ev

    def after(self, seq: int) -> list[ChatEvent]:
        """Events with ``seq`` greater than the given one, oldest first. Events that fell
        out of the buffer are gone (the page then reloads the transcript)."""
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    def wait_after(self, seq: int, timeout: float | None = None) -> list[ChatEvent]:
        """``after(seq)``, waiting up to ``timeout`` seconds (None: forever) for one."""
        with self._lock:
            self._lock.wait_for(lambda: self._seq > seq, timeout)
            return [e for e in self._events if e.seq > seq]


# --------------------------------------------------------------------------- the engine

HELP = (
    "/tier up|down|N (/up, /down): the next message's tier · /model [name]: show or pick "
    "the model · /abort: stop the running turn · /cost: spend · /status: the session · "
    "/verify: run the repo's checks · /approve: allow the refused message once · "
    "/pr [title]: open as a draft PR · /discard yes: drop the work · //text: send text "
    "starting with /"
)
STATE_DIR = ("opencode", "chats")  # <home>/opencode/chats/<id>: the ChatServer state dir
HOST_FILE = "host.json"  # which process hosts a live session (crash recovery)
#: States whose row still claims a live server (recovery looks at these).
CLAIMED_STATES: tuple[str, ...] = ("starting", "idle", "busy", "closing")
TITLE_LIMIT = 72
SUMMARY_LIMIT = 120
TAIL_LIMIT = 1500
_JOIN_S = 10.0  # shutdown: how long to wait for an aborted turn's thread

#: 7d: the proxy's key-shaped patterns plus GitHub tokens and token-bearing URLs.
_GH_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})")
_URL_CRED = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
#: Sessions hosted by a live ``ChatManager`` in this process: (database path, session id).
_HOSTED: set[tuple[str, int]] = set()
_HOSTED_LOCK = threading.Lock()


class ChatServerLike(Protocol):
    """What the engine needs of ``runner.opencode.ChatServer`` (tests inject a fake)."""

    session_id: str | None

    def start(self, *, resume: str | None = None, title: str | None = None) -> str: ...
    def send(self, text: str, model_id: str) -> Any: ...  # iterable; ``.result`` after
    def abort(self) -> bool: ...
    def stop(self) -> None: ...


ServerFactory = Callable[..., ChatServerLike]


@dataclass(frozen=True)
class VerifyOutcome:
    """A ``/verify`` (or pre-PR verify) result. ``passed`` None: nothing was run."""

    passed: bool | None
    summary: str
    tail: str = ""


@dataclass
class _Turn:
    """The running turn's accumulators (the proxy's ``on_usage`` adds to them)."""

    row_id: int
    seq: int
    tier: int
    model: str
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    models: dict[str, int] = field(default_factory=dict)  # config name -> calls answered
    answered_by: str | None = None  # the last model that answered a call
    fallback: str | None = None  # decision 2: the cloud model used while local was busy
    parts: dict[str, str] = field(default_factory=dict)  # part id -> text so far
    tool_calls: int = 0
    refused: bool = False
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Live:
    """A session with a running OpenCode server in this process."""

    id: int
    repo: str
    base_branch: str
    branch: str
    ladder: tuple[Rung, ...]
    rung: Rung
    worktree: Any
    server: ChatServerLike
    ctx_cm: Any
    ctx: Any
    trace_id: str
    recorder: Any
    state_dir: Path
    state: str = "idle"
    lock: threading.RLock = field(default_factory=threading.RLock)
    thread: threading.Thread | None = None
    turn: _Turn | None = None
    last: _Turn | None = None
    last_activity: float = 0.0
    first_message: str | None = None
    refused_text: str | None = None
    warned: frozenset[tuple[int, tuple[str, ...]]] = frozenset()


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat() if hasattr(value, "isoformat") else value


def _view(row: Any) -> ChatSession:
    """A store ``ChatSession`` row as this module's ``ChatSession``."""
    return ChatSession(
        id=row.id,
        repo=row.repo,
        base_branch=row.base_branch,
        branch=row.branch,
        state=row.state,
        tier=row.tier,
        model=row.model,
        started_at=_iso(row.started_at) or "",
        worktree=row.worktree,
        base_commit=row.base_commit,
        opencode_session_id=row.opencode_session_id,
        trace_id=row.trace_id,
        pr_number=row.pr_number,
        end_reason=row.end_reason,
        ended_at=_iso(row.ended_at),
    )


def _turn_view(row: Any) -> ChatTurn:
    return ChatTurn(
        session_id=row.session_id,
        seq=row.seq,
        tier=row.tier,
        model=row.model,
        started_at=_iso(row.started_at) or "",
        status=row.status,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        cost_usd=row.cost_usd,
        tool_calls=row.tool_calls,
        edits=row.edits,
        ended_at=_iso(row.ended_at),
    )


def _one_line(text: str, limit: int) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _tool_summary(name: str, data: dict[str, Any]) -> str:
    """A one-line summary of a tool call's input (the output is only in the recording)."""
    for key in ("command", "filePath", "path", "pattern", "url", "description"):
        if isinstance(data.get(key), str) and data[key]:
            return _one_line(f"{name} {data[key]}", SUMMARY_LIMIT)
    try:
        raw = json.dumps(data, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = ""
    return _one_line(f"{name} {raw}", SUMMARY_LIMIT)


def _refusal_data(r: Refusal) -> dict[str, Any]:
    """The ``refused`` event: the numbers and what the user can do."""
    if r.cap == "local_only":
        hint = "this repo is local only; no approval can send it to a cloud model (/tier down)"
    elif r.cap == "chat_model":
        hint = "the call named a model that is not this session's; it was not sent"
    elif r.cap == "chat_turn":
        hint = "this message used its whole turn budget; send a new message to go on"
    elif r.needs_approval:
        hint = "type /approve to allow this message once"
    else:
        hint = "refused"
    return {
        "cap": r.cap,
        "cap_usd": round(r.cap_usd, 4),
        "spent_usd": round(r.spent_usd, 4),
        "reserved_usd": round(r.reserved_usd, 4),
        "estimate_usd": round(r.estimate_usd, 4),
        "model": r.model,
        "approvable": r.needs_approval,
        "message": r.flag_reason,
        "hint": hint,
    }


class ChatManager:
    """Runs chat sessions for one host process (``fman chat`` or ``fman metrics``): its
    own ``ModelProxy`` and ``Spend``, one sandboxed ``opencode serve`` per session.
    See docs/chat-design.md sections 2-5 and the 2026-11-25 decisions.

    Every collaborator is injectable (tests pass fakes); the defaults are the real ones:

    * ``proxy``: a started ``ModelProxy`` whose ``spend_hook`` is ``spend.hook``. The
      manager takes over its ``fallback`` hook (decision 2) and chains its ``on_usage``
      (per-turn tokens, cost and which model answered). None: one is built and started
      on first use (``shared_local_slots``, ``chat_local_wait_seconds``) and stopped by
      ``shutdown``.
    * ``spend``: a ``Spend`` over ``store`` (option C sub-cap, turn budget, approvals).
    * ``worktrees``: a ``WorktreeManager``; ``clone_url(repo)`` gives the remote.
    * ``github``: ``token()``, ``get_default_branch``, ``create_draft_pr`` only (7a).
    * ``server_factory(session_id=, worktree_path=, config=, state_dir=, sandbox=,
      network=, extra_time=)``: a ``ChatServer``.
    * ``sandbox_ready() -> str | None``, ``writer_policy(id, path)``,
      ``verifier(session, worktree) -> VerifyOutcome``, ``push(worktree, token)``.
    * ``host_alive(session_id) -> bool``: does a live process host that row (recovery).

    Threads: ``send`` starts a turn on its own thread and returns at once; events go to
    the session's ``EventBuffer`` (``events``); ``wait_turn`` joins. An idle reaper thread
    (``reaper_interval`` seconds, None: off) calls ``end_idle``.
    """

    def __init__(
        self,
        config: Any,
        store: Any,
        *,
        proxy: Any = None,
        spend: Any = None,
        worktrees: Any = None,
        github: Any = None,
        locks: Any = None,
        server_factory: ServerFactory | None = None,
        sandbox_ready: Callable[[], str | None] | None = None,
        writer_policy: Callable[[int, Path], Any] | None = None,
        verifier: Callable[[ChatSession, Any], VerifyOutcome] | None = None,
        push: Callable[[Any, str | None], None] | None = None,
        clone_url: Callable[[str], str] | None = None,
        home: Path | str | None = None,
        max_sessions: int | None = None,
        idle_minutes: float | None = None,
        start_tier: int | None = None,
        turn_budget_usd: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        host_alive: Callable[[int], bool] | None = None,
        reaper_interval: float | None = 60.0,
        recover: bool = True,
    ) -> None:
        self.config = config
        self.store = store

        def cfg(name: str, default: Any) -> Any:
            value = getattr(config, name, None)
            return default if value is None else value

        self.max_sessions = int(max_sessions or cfg("chat_max_sessions", DEFAULT_MAX_SESSIONS))
        self.idle_minutes = float(
            idle_minutes if idle_minutes is not None else cfg("chat_idle_minutes", 30.0)
        )
        self.start_tier = int(start_tier or cfg("chat_start_tier", DEFAULT_START_TIER))
        if turn_budget_usd is None:
            turn_budget_usd = getattr(config, "chat_turn_budget", None) or DEFAULT_TURN_BUDGET_USD
        self.turn_budget_usd = float(turn_budget_usd)
        self._home = Path(home) if home is not None else None
        self.spend = (
            spend
            if spend is not None
            else Spend(store, config, chat_turn_budget_usd=self.turn_budget_usd)
        )
        self.worktrees = worktrees
        self.github = github
        self.locks = locks if locks is not None else Locks(store)
        self._server_factory = server_factory or self._default_server
        self._sandbox_ready = sandbox_ready or self._default_sandbox_ready
        self._writer_policy = writer_policy or self._default_writer_policy
        self._verifier = verifier or self._default_verify
        self._push = push or (lambda wt, token: wt.push(token))
        self._clone_url = clone_url or (lambda repo: f"https://github.com/{repo}.git")
        self._clock = clock
        self._host_alive = host_alive or self._default_host_alive
        self._reaper_interval = reaper_interval
        self._lock = threading.RLock()
        self._live: dict[int, _Live] = {}
        self._starting: set[int] = set()
        self._buffers: dict[int, EventBuffer] = {}
        self._secrets: set[str] = set()
        self._owns_proxy = proxy is None
        self.proxy = proxy
        self._prev_on_usage: Callable[[Any], None] | None = None
        if proxy is not None:
            self._hook_proxy(proxy)
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        if recover:
            self.recover()

    # ------------------------------------------------------------------ paths and defaults

    @property
    def home(self) -> Path:
        if self._home is None:
            from foreman_inator import paths

            self._home = paths.home()
        return self._home

    def state_dir(self, session_id: int) -> Path:
        return self.home.joinpath(*STATE_DIR, str(int(session_id)))

    @property
    def recordings_dir(self) -> Path:
        return self.home / "recordings"

    def _db_key(self, session_id: int) -> tuple[str, int]:
        return (str(getattr(self.store, "path", id(self.store))), int(session_id))

    def _default_server(
        self,
        *,
        session_id: int,
        worktree_path: Path,
        config: dict[str, Any],
        state_dir: Path,
        sandbox: Any,
        network: bool,
        extra_time: Callable[[], float],
    ) -> ChatServerLike:
        return oc.ChatServer(
            worktree_path,
            chat_id=session_id,
            config=config,
            state_dir=state_dir,
            shared_dir=self.home / "opencode" / "shared",
            turn_timeout=float(getattr(self.config, "writer_timeout_seconds", oc.DEFAULT_TIMEOUT)),
            sandbox=sandbox,
            network=network,
            extra_time=extra_time,
        )

    def _default_sandbox_ready(self) -> str | None:
        """As ``worker_sandbox.sandbox_ready``: None when a launch may go ahead (7g)."""
        if getattr(self.config, "sandbox", "on") != "on":
            return None
        from foreman_inator.runner import sandbox as sb

        try:
            sb.ensure_available(sb.Policy(worktree=self.home, scratch=self.home))
        except sb.SandboxUnavailable as exc:
            return str(exc)
        return None

    def _scratch(self, session_id: int, role: str) -> Path:
        scratch = self.home / "sandbox" / f"chat-{int(session_id)}-{role}"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        return scratch

    def _default_writer_policy(self, session_id: int, wt_path: Path) -> Any:
        """The writer's policy (section 7): writes to the worktree and its own scratch,
        no network but the proxy port (``ChatServer`` fills it in from the proxy URL)."""
        from foreman_inator.runner import sandbox as sb

        scratch = self._scratch(session_id, "writer")
        if getattr(self.config, "sandbox", "on") == "off":
            return sb.Policy(worktree=Path(wt_path), scratch=scratch, mode="off")
        return sb.Policy.create(wt_path, scratch, mode="on", network="none")

    def _verify_policy(self, session_id: int, wt_path: Path, repo: str) -> Any:
        from foreman_inator.runner import sandbox as sb

        scratch = self._scratch(session_id, "verify")
        if getattr(self.config, "sandbox", "on") == "off":
            return sb.Policy(worktree=Path(wt_path), scratch=scratch, mode="off")
        network = self.config.sandbox_network_for(repo)
        return sb.Policy.create(wt_path, scratch, mode="on", network=network)

    def _default_verify(self, session: ChatSession, wt: Any) -> VerifyOutcome:
        """The repo's verify command under the verify policy (the repo's network)."""
        from foreman_inator.runner import sandbox as sb
        from foreman_inator.runner import verify as verify_mod

        settings = self.store.get_repo_settings(session.repo)
        cmd = verify_mod.detect(wt.path, override=settings.verify_command if settings else None)
        if cmd is None:
            return VerifyOutcome(None, "no verify command found for this repo")
        try:
            res = verify_mod.run(
                cmd,
                wt.path,
                output_path=self.state_dir(session.id) / "verify.log",
                sandbox=self._verify_policy(session.id, wt.path, session.repo),
            )
        except sb.SandboxUnavailable as exc:
            return VerifyOutcome(False, f"sandbox unavailable: {exc}")
        word = "passed" if res.passed else "FAILED"
        return VerifyOutcome(res.passed, f"{res.command}: {word}", res.output_tail(TAIL_LIMIT))

    def _default_host_alive(self, session_id: int) -> bool:
        """Is a live process hosting this session? Read from its ``host.json``."""
        try:
            info = json.loads((self.state_dir(session_id) / HOST_FILE).read_text())
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if pid == os.getpid():
            with _HOSTED_LOCK:
                return self._db_key(session_id) in _HOSTED
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    # ------------------------------------------------------------------ proxy hooks

    def _hook_proxy(self, proxy: Any) -> None:
        proxy.fallback = self._fallback
        self._prev_on_usage = getattr(proxy, "on_usage", None)
        proxy.on_usage = self._on_usage

    def _ensure_proxy(self) -> Any:
        with self._lock:
            if self.proxy is None:
                from foreman_inator.proxy.server import ModelProxy, shared_local_slots

                proxy = ModelProxy(
                    self.config,
                    store=self.store,
                    spend_hook=self.spend.hook,
                    local_slots=shared_local_slots(
                        int(getattr(self.config, "max_local_inference", 1))
                    ),
                    chat_local_wait_seconds=float(
                        getattr(self.config, "chat_local_wait_seconds", 5.0)
                    ),
                )
                self._hook_proxy(proxy)
                proxy.start()
                self.proxy = proxy
            return self.proxy

    def _fallback(self, ctx: Any) -> ModelEndpoint | None:
        """Decision 2: the proxy's hook when a chat call waited out the local slot. The
        first cloud rung at or above the turn's tier, only inside every cap and never for
        a local-only repo (7c, read again here on every ask); None keeps it waiting."""
        sid = getattr(ctx, "chat_session_id", None)
        live = self._live.get(sid) if sid is not None else None
        if live is None:
            return None
        turn = live.turn
        if turn is None or not self.store.repo_allows_cloud(live.repo):
            return None
        session = ChatSession(
            id=live.id,
            repo=live.repo,
            base_branch=live.base_branch,
            branch=live.branch,
            state="busy",
            tier=turn.tier,
            model=turn.model,
            started_at="",
        )
        return chat_cloud_fallback(self.spend, self.store, session, live.ladder)

    def _on_usage(self, usage: Any) -> None:
        if self._prev_on_usage is not None:
            try:
                self._prev_on_usage(usage)
            except Exception as exc:  # the chained hook is not ours to fail
                log.warning("chat: chained on_usage failed: %s", type(exc).__name__)
        ctx = getattr(usage, "context", None)
        sid = getattr(ctx, "chat_session_id", None)
        live = self._live.get(sid) if sid is not None else None
        if live is None:
            return
        with live.lock:
            turn = live.turn
            if turn is None:
                return
            turn.tokens_in += int(usage.tokens_in or 0)
            turn.tokens_out += int(usage.tokens_out or 0)
            turn.cost_usd += max(0.0, float(usage.cost_usd or 0.0))
            turn.models[usage.model] = turn.models.get(usage.model, 0) + 1
            turn.answered_by = usage.model
            fallen = getattr(ctx, "fallback_from", None)
            if fallen and turn.fallback is None:
                turn.fallback = usage.model
                self._emit(
                    live.id,
                    "fallback",
                    {
                        "model": usage.model,
                        "from": fallen,
                        "message": f"local slot busy, used {usage.model}",
                    },
                )

    # ------------------------------------------------------------------ events

    def _buf(self, session_id: int) -> EventBuffer:
        with self._lock:
            buf = self._buffers.get(session_id)
            if buf is None:
                buf = self._buffers[session_id] = EventBuffer()
            return buf

    def scrub(self, text: str) -> str:
        """7d: key-shaped strings, GitHub tokens, URL credentials and the App token
        this manager handled, out of everything shown or recorded."""
        from foreman_inator.proxy.server import scrub_secrets

        text = scrub_secrets(str(text))
        for secret in list(self._secrets):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = _GH_TOKEN.sub("[redacted]", text)
        return _URL_CRED.sub(r"\1[redacted]@", text)

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self._clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        return value

    def _emit(self, session_id: int, type_: str, data: dict[str, Any] | None = None) -> ChatEvent:
        return self._buf(session_id).add(type_, self._clean(dict(data or {})))

    def events(
        self, session_id: int, *, after: int = 0, timeout: float | None = None
    ) -> Iterator[ChatEvent]:
        """Events after ``after`` (``Last-Event-ID``), then live ones until the session's
        ``end`` event, or until ``timeout`` seconds pass with no new event (the SSE route
        then closes and the browser reconnects with its last id)."""
        buf = self._buf(session_id)

        def gen() -> Iterator[ChatEvent]:
            seq = after
            while True:
                got = buf.wait_after(seq, timeout)
                if not got:
                    return
                for ev in got:
                    seq = ev.seq
                    yield ev
                    if ev.type == "end":
                        return

        return gen()

    # ------------------------------------------------------------------ reads

    def get(self, session_id: int) -> ChatSession:
        try:
            return _view(self.store.get_chat_session(session_id))
        except NotFoundError as exc:
            raise ChatError(f"no chat session {session_id}") from exc

    def sessions(self) -> list[ChatSession]:
        """Every session, newest first."""
        return [_view(r) for r in self.store.list_chat_sessions()]

    def turns(self, session_id: int) -> list[ChatTurn]:
        return [_turn_view(t) for t in self.store.list_chat_turns(session_id)]

    def ladder(self, session_id: int) -> tuple[Rung, ...]:
        live = self._live.get(session_id)
        if live is not None:
            return live.ladder
        row = self.get(session_id)
        return tier_ladder(
            self.config.models, local_only=not self.store.repo_allows_cloud(row.repo)
        )

    def cost(self, session_id: int) -> dict[str, Any]:
        """Section 5's line, all read from the ledger: ``session $x · chat this month $y
        of $z · month $m of $M``."""
        month_start, month_end = self.spend.month_window()
        session = self.store.chat_spend(session_id)
        chat_month = self.store.chat_spend_total(month_start, month_end)
        month = self.store.spend_total("live", since=month_start, until=month_end)
        chat_cap = float(self.spend.chat_monthly_usd)
        month_cap = float(self.spend.caps.monthly_usd)
        return {
            "session_usd": round(session, 6),
            "chat_month_usd": round(chat_month, 6),
            "chat_cap_usd": chat_cap,
            "chat_left_usd": round(max(0.0, chat_cap - chat_month), 6),
            "month_usd": round(month, 6),
            "month_cap_usd": month_cap,
            "text": (
                f"session ${session:.2f} · chat this month ${chat_month:.2f} of "
                f"${chat_cap:.2f} · month ${month:.2f} of ${month_cap:.2f}"
            ),
        }

    def status(self, session_id: int) -> dict[str, Any]:
        row = self.get(session_id)
        live = self._live.get(session_id)
        rung = live.rung if live else None
        return {
            "id": row.id,
            "repo": row.repo,
            "state": live.state if live else row.state,
            "tier": rung.tier if rung else row.tier,
            "model": rung.endpoint.name if rung else row.model,
            "kind": rung.endpoint.kind if rung else None,
            "branch": row.branch,
            "base_branch": row.base_branch,
            "local_only": not self.store.repo_allows_cloud(row.repo),
            "network": self.store.repo_allows_chat_network(row.repo),
            "locks": list(self.locks.chat_held(session_id)),
            "approval_pending": self.spend.chat_approval_pending(session_id),
            "cost": self.cost(session_id)["text"],
        }

    # ------------------------------------------------------------------ start / resume

    def _claimed_elsewhere(self) -> list[int]:
        """Rows that claim a live server in another live host."""
        out = []
        for row in self.store.list_chat_sessions(states=CLAIMED_STATES):
            if row.id in self._live or row.id in self._starting:
                continue
            if self._host_alive(row.id):
                out.append(row.id)
        return out

    def _reserve(self, session_id: int | None = None) -> None:
        """Decision 7: at most ``max_sessions`` open sessions (here and in other hosts)."""
        with self._lock:
            open_ = [*self._live, *self._starting, *self._claimed_elsewhere()]
            if len(open_) >= self.max_sessions:
                ids = ", ".join(str(i) for i in sorted(set(open_)))
                raise ChatError(
                    f"chat session {ids} is already open (at most {self.max_sessions}); "
                    "/pr, /discard or let it end first"
                )
            if session_id is not None:
                self._starting.add(session_id)

    def _token(self) -> str | None:
        if self.github is None:
            return None
        token = self.github.token()
        if token:
            self._secrets.add(token)
        return token

    def _mirror(self, repo: str) -> Any:
        if self.worktrees is None:
            self.worktrees = WorktreeManager(self.home / "worktrees")
        return self.worktrees.mirror(repo, self._clone_url(repo))

    def _rungs(self, repo: str) -> tuple[Rung, ...]:
        """The ladder, local models only for a local-only repo (#49, 7c)."""
        return tier_ladder(self.config.models, local_only=not self.store.repo_allows_cloud(repo))

    def start(
        self, repo: str, *, base_branch: str | None = None, tier: int | None = None
    ) -> ChatSession:
        """Section 2, Start: watched repo, capacity, base branch only (decision 5),
        ladder and tier, sandbox gate (7g, fail closed: nothing is created), session row,
        worktree on ``fman/chat/<id>``, spend registration, proxy context, OpenCode."""
        if repo not in tuple(getattr(self.config, "repos", ())):
            raise ChatError(f"{repo} is not a watched repo (config repos)")
        self._reserve()
        base = base_branch
        if base is None:
            base = self.github.get_default_branch(repo) if self.github is not None else "main"
        try:
            base = check_chat_base(base)
        except BranchRefused as exc:
            raise ChatError(str(exc)) from exc
        ladder = self._rungs(repo)
        rung = start_rung(ladder, tier, default_tier=self.start_tier)
        why = self._sandbox_ready()
        if why:
            raise ChatError(f"sandbox unavailable, so no chat can start (7g): {why}")
        row = self.store.create_chat_session(
            repo, base_branch=base, tier=rung.tier, model=rung.endpoint.name
        )
        try:
            self._reserve(row.id)
        except ChatError as exc:  # another start won the race
            self.store.set_chat_state(row.id, "failed", end_reason="capacity")
            raise exc
        wt = None
        try:
            wt = self._mirror(repo).create_chat(base, row.id, token=self._token())
            self.store.update_chat_session(row.id, worktree=str(wt.path), base_commit=wt.base_sha)
            self._open(self.store.get_chat_session(row.id), ladder, rung, wt, resume=None)
        except Exception as exc:
            self._fail_start(row.id, wt, exc)
            raise ChatError(f"chat session {row.id} could not start: {self.scrub(exc)}") from exc
        finally:
            with self._lock:
                self._starting.discard(row.id)
        return self.get(row.id)

    def resume(self, session_id: int) -> ChatSession:
        """An ``ended`` session again: a new server in the kept worktree, on the kept
        state dir, reopening the same OpenCode session id (section 2, Resume)."""
        row = self.store.get_chat_session(session_id)
        if row.state != "ended":
            raise ChatError(f"chat session {session_id} is {row.state}; only an ended one resumes")
        self._reserve(session_id)
        try:
            ladder = self._rungs(row.repo)
            try:
                rung = rung_for(ladder, row.tier)
            except ChatError:
                rung = start_rung(ladder, default_tier=self.start_tier)
            why = self._sandbox_ready()
            if why:
                raise ChatError(f"sandbox unavailable, so the chat cannot resume (7g): {why}")
            try:
                wt = self._mirror(row.repo).open_chat(session_id)
            except WorktreeError as exc:
                raise ChatError(f"chat session {session_id} cannot resume: {exc}") from exc
            if (rung.tier, rung.endpoint.name) != (row.tier, row.model):
                self.store.update_chat_session(session_id, tier=rung.tier, model=rung.endpoint.name)
            try:
                self._open(row, ladder, rung, wt, resume=row.opencode_session_id)
            except Exception as exc:
                self.spend.end_chat_session(session_id)
                with contextlib.suppress(StoreError):
                    self.store.set_chat_state(
                        session_id, "ended", end_reason=f"resume_failed: {self.scrub(exc)}"[:200]
                    )
                raise ChatError(
                    f"chat session {session_id} could not resume: {self.scrub(exc)}"
                ) from exc
        finally:
            with self._lock:
                self._starting.discard(session_id)
        return self.get(session_id)

    def _open(
        self, row: Any, ladder: tuple[Rung, ...], rung: Rung, wt: Any, *, resume: str | None
    ) -> _Live:
        sid = row.id
        proxy = self._ensure_proxy()
        self.spend.register_chat_session(
            sid, repo=row.repo, model=rung.endpoint.name, small_model=ladder[0].endpoint.name
        )
        state_dir = self.state_dir(sid)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        ctx_cm = server = None
        try:
            # The proxy context is registered inside the chat span, so it captures it:
            # every llm.call of the session parents to the session's trace (section 9).
            with otel.chat_span(
                sid, trace_id=row.trace_id, repo=row.repo, base_branch=row.base_branch
            ) as tr:
                ctx_cm = proxy.context(chat_session_id=sid)
                ctx = ctx_cm.__enter__()
            self.store.update_chat_session(sid, trace_id=tr.trace_id)
            oc_config = oc.build_chat_config(ladder, ladder[0].endpoint, ctx.base_url)
            server = self._server_factory(
                session_id=sid,
                worktree_path=Path(wt.path),
                config=oc_config,
                state_dir=state_dir,
                sandbox=self._writer_policy(sid, Path(wt.path)),
                network=bool(self.store.repo_allows_chat_network(row.repo)),  # decision 4
                extra_time=lambda: proxy.chat_queue_wait(sid),
            )
            oc_sid = server.start(resume=resume, title=f"foreman-inator chat {sid}")
            self.store.update_chat_session(sid, opencode_session_id=oc_sid)
            recorder = self._open_recorder(row, tr.trace_id, ladder, rung, wt, resumed=bool(resume))
        except BaseException:
            if server is not None:
                with contextlib.suppress(Exception):
                    server.stop()
            if ctx_cm is not None:
                ctx_cm.__exit__(None, None, None)
            raise
        live = _Live(
            id=sid,
            repo=row.repo,
            base_branch=row.base_branch,
            branch=row.branch,
            ladder=ladder,
            rung=rung,
            worktree=wt,
            server=server,
            ctx_cm=ctx_cm,
            ctx=ctx,
            trace_id=tr.trace_id,
            recorder=recorder,
            state_dir=state_dir,
            last_activity=self._clock(),
            first_message=self._first_message(sid, None) if resume else None,
        )
        (state_dir / HOST_FILE).write_text(json.dumps({"pid": os.getpid()}))
        with _HOSTED_LOCK:
            _HOSTED.add(self._db_key(sid))
        with self._lock:
            self._live[sid] = live
        self.store.set_chat_state(sid, "idle")
        self._emit(sid, "status", {"state": "idle", "resumed": bool(resume)})
        self._emit_tier(live)
        self._start_reaper()
        return live

    def _open_recorder(
        self,
        row: Any,
        trace_id: str,
        ladder: tuple[Rung, ...],
        rung: Rung,
        wt: Any,
        *,
        resumed: bool,
    ) -> Any:
        sid = row.id
        old = tracing.get_recorder(sid, RECORDER_ROLE)
        if old is not None:  # a crashed host's leftover in this process
            old.close(outcome="ended", stopped_reason="error", end_reason="host_restart")
        path = chat_recording_path(self.recordings_dir, sid)
        if resumed and path.exists():
            # The recorder starts a fresh file; keep the earlier part beside it (pruned
            # with it, as ``prune_chats`` removes the session's ``.files`` dir).
            side = path.parent / f"{sid}.files"
            side.mkdir(parents=True, exist_ok=True)
            n = len(list(side.glob("before-resume-*.jsonl.gz"))) + 1
            shutil.move(str(path), side / f"before-resume-{n}.jsonl.gz")
        from foreman_inator.runner import sandbox as sb

        return chat_recorder(
            self.recordings_dir,
            sid,
            trace_id=trace_id,
            base_commit=getattr(wt, "base_sha", None),
            model=rung.endpoint.name,
            tier=rung.tier,
            repo=row.repo,
            ladder=[
                {"tier": r.tier, "model": r.endpoint.name, "kind": r.endpoint.kind} for r in ladder
            ],
            sandbox=sb.describe(
                getattr(self.config, "sandbox", "on"), dict(self.config.sandbox_network)
            ),
            resumed=resumed,
        )

    def _fail_start(self, session_id: int, wt: Any, exc: BaseException) -> None:
        """A start that broke: nothing of the user's exists yet, so the worktree goes."""
        log.warning("chat %s failed to start: %s", session_id, type(exc).__name__)
        self.spend.end_chat_session(session_id)
        if wt is not None:
            with contextlib.suppress(Exception):
                wt.remove(delete_branch=True)
        self.locks.release_chat(session_id)
        with contextlib.suppress(StoreError):
            self.store.set_chat_state(
                session_id, "failed", end_reason=self.scrub(f"start: {exc}")[:200]
            )

    def _start_reaper(self) -> None:
        if self._reaper_interval is None or self._reaper is not None:
            return
        interval = float(self._reaper_interval)

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.end_idle()
                except Exception as exc:
                    log.warning("chat: idle reaper failed: %s", type(exc).__name__)

        self._reaper = threading.Thread(target=loop, name="chat-idle-reaper", daemon=True)
        self._reaper.start()

    # ------------------------------------------------------------------ messages

    def _live_or_raise(self, session_id: int) -> _Live:
        live = self._live.get(session_id)
        if live is None:
            state = self.get(session_id).state
            raise ChatError(f"chat session {session_id} is {state}, not open here")
        return live

    def send(self, session_id: int, text: str) -> ChatTurn | Command:
        """One user line: a command is handled here (its reply is an ``info`` event);
        text starts a turn on the current tier's model, on its own thread. Refused
        (``ChatError``) while the session is busy: no queueing (7e)."""
        parsed = parse_command(text)
        if isinstance(parsed, Command):
            self.command(session_id, parsed)
            return parsed
        return self._begin_turn(self._live_or_raise(session_id), parsed)

    def _begin_turn(self, live: _Live, text: str) -> ChatTurn:
        sid = live.id
        with live.lock:
            if live.state != "idle" or live.turn is not None:
                raise ChatError("still working: /abort or wait")
            rung = live.rung
            self.spend.start_chat_turn(sid)
            row = self.store.add_chat_turn(sid, tier=rung.tier, model=rung.endpoint.name)
            if live.first_message is None:
                live.first_message = text
            live.refused_text = None
            live.last_activity = self._clock()
            clean = self.scrub(text)
            if live.recorder is not None:
                live.recorder.user(clean, by="user")
            self._emit(sid, "user", {"text": clean, "turn": row.seq})
            refusal = self._precheck(live, rung)
            if refusal is not None:
                live.refused_text = text
                done = self.store.finish_chat_turn(row.id, "refused")
                self._emit(sid, "refused", {**_refusal_data(refusal), "turn": row.seq})
                return _turn_view(done)
            turn = _Turn(row.id, row.seq, rung.tier, rung.endpoint.name, text)
            live.turn = turn
            live.state = "busy"
            self.store.set_chat_state(sid, "busy")
            self._emit(sid, "status", {"state": "busy", "turn": row.seq})
            thread = threading.Thread(
                target=self._run_turn, args=(live, turn, rung), name=f"chat-{sid}-turn-{row.seq}",
                daemon=True,
            )  # fmt: skip
            live.thread = thread
            thread.start()
            return _turn_view(row)

    def _precheck(self, live: _Live, rung: Rung) -> Refusal | None:
        """Before sending a cloud-tier turn: a local-only repo (7c) or no room under the
        caps refuses it up front unless an approval is waiting (the proxy's spend hook
        checks every call again)."""
        if rung.endpoint.kind != "cloud":
            return None
        now = self.spend.now()
        if not self.store.repo_allows_cloud(live.repo):
            return Refusal(
                "live", "local_only", 0.0, 0.0, 0.0, 0.0, None, now,
                model=rung.endpoint.name, chat_session_id=live.id,
            )  # fmt: skip
        room = self.spend.chat_room(live.id)
        if room is not None and room.cap == "chat_turn":
            return None  # a fresh turn: the proxy's per-call check is the budget's
        if room is not None and not (
            room.needs_approval and self.spend.chat_approval_pending(live.id)
        ):
            return replace(room, model=rung.endpoint.name)
        return None

    def _run_turn(self, live: _Live, turn: _Turn, rung: Rung) -> None:
        result: Any = None
        error: BaseException | None = None
        try:
            with (
                otel.chat_span(live.id, trace_id=live.trace_id),
                otel.chat_turn_span(live.id, turn.seq, tier=turn.tier, model=turn.model) as span,
            ):
                stream = live.server.send(turn.text, rung.endpoint.model)
                for ev in stream:
                    self._on_event(live, turn, ev)
                    if not turn.refused and self.spend.refusal_for_chat(live.id) is not None:
                        turn.refused = True  # 7e: the turn stops at a refused call
                        live.server.abort()
                result = getattr(stream, "result", None)
                span.set_attribute("chat.answered_by", turn.answered_by or "")
                if turn.fallback:
                    span.set_attribute(
                        "chat.local_fallback", f"local slot busy, used {turn.fallback}"
                    )
        except Exception as exc:
            error = exc
            log.warning("chat %s turn %s failed: %s", live.id, turn.seq, type(exc).__name__)
        finally:
            self._finish_turn(live, turn, result, error)

    def _on_event(self, live: _Live, turn: _Turn, ev: Any) -> None:
        sid = live.id
        if isinstance(ev, oc.TextEvent):
            new = self.scrub(ev.text)
            mode, text = text_delta(turn.parts.get(ev.part_id, ""), new)
            turn.parts[ev.part_id] = new
            if text or mode == "replace":
                self._emit(sid, "text", {"part": ev.part_id, "mode": mode, "text": text})
        elif isinstance(ev, oc.ToolEvent):
            turn.tool_calls += 1
            self._emit(
                sid,
                "tool",
                {
                    "name": ev.name,
                    "kind": oc.tool_kind(ev.name),
                    "summary": _tool_summary(ev.name, dict(ev.input or {})),
                    "is_error": bool(ev.is_error),
                },
            )
        elif isinstance(ev, oc.ErrorEvent):
            self._emit(sid, "error", {"message": _one_line(ev.message or ev.name, 500)})
        elif isinstance(ev, oc.StatusEvent) and ev.status == "retry":
            self._emit(sid, "status", {"state": "retry", "turn": turn.seq})

    def _finish_turn(
        self, live: _Live, turn: _Turn, result: Any, error: BaseException | None
    ) -> None:
        sid = live.id
        refusal = self.spend.refusal_for_chat(sid)
        if refusal is not None:
            status = "refused"
        elif result is not None:
            status = result.status
        else:
            status = "errored"
        with live.lock:
            try:  # decision 6 first, so a finished turn row means its locks are taken
                self._touch_locks(live)
            except Exception as exc:
                log.warning("chat %s: taking locks failed: %s", sid, type(exc).__name__)
            with contextlib.suppress(StoreError):
                self.store.finish_chat_turn(
                    turn.row_id,
                    status,
                    tokens_in=turn.tokens_in or getattr(result, "tokens_in", 0),
                    tokens_out=turn.tokens_out or getattr(result, "tokens_out", 0),
                    cost_usd=turn.cost_usd,
                    tool_calls=max(turn.tool_calls, getattr(result, "tool_calls", 0)),
                    edits=getattr(result, "edits", 0),
                )
            if refusal is not None:
                live.refused_text = turn.text
                self._emit(sid, "refused", {**_refusal_data(refusal), "turn": turn.seq})
            elif status != "finished":
                msg = getattr(result, "error", None) or (str(error) if error else status)
                self._emit(sid, "error", {"message": _one_line(msg, 500), "turn": turn.seq})
            models = sorted(turn.models)
            self._emit(
                sid,
                "status",
                {
                    "state": "idle",
                    "turn": turn.seq,
                    "status": status,
                    "tier": turn.tier,
                    "model": turn.model,
                    "answered_by": turn.answered_by,
                    "models": models,
                    "fallback": turn.fallback,
                    "cost_usd": round(turn.cost_usd, 6),
                },
            )
            self._emit(sid, "cost", self.cost(sid))
            live.last = turn
            live.turn = None
            live.last_activity = self._clock()
            if live.state == "busy" and self._live.get(sid) is live:
                live.state = "idle"
                with contextlib.suppress(StoreError):
                    self.store.set_chat_state(sid, "idle")
            turn.done.set()

    def _touch_locks(self, live: _Live) -> None:
        """Decision 6: lock what the chat changed; warn about tickets holding the rest."""
        try:
            paths = live.worktree.changed_files()
        except Exception as exc:
            log.warning("chat %s: changed files unknown: %s", live.id, type(exc).__name__)
            return
        if not paths:
            return
        holders = self.locks.chat_touch(live.id, live.repo, paths)
        seen = frozenset((h.ticket_id, tuple(h.paths)) for h in holders)
        if holders and seen != live.warned:
            names = ", ".join(f"#{h.issue_number} ({', '.join(h.paths)})" for h in holders)
            self._emit(
                live.id,
                "warning",
                {
                    "kind": "locks",
                    "holders": [
                        {"ticket": h.ticket_id, "issue": h.issue_number, "paths": list(h.paths)}
                        for h in holders
                    ],
                    "message": f"files this chat edited are held by ticket issue {names}; "
                    "the ticket's PR may conflict",
                },
            )
        live.warned = seen

    def wait_turn(self, session_id: int, timeout: float | None = None) -> ChatTurn | None:
        """Join the running turn (the terminal front end, tests); its finished row."""
        live = self._live.get(session_id)
        if live is not None:
            thread = live.thread
            if thread is not None:
                thread.join(timeout)
        turns = self.store.list_chat_turns(session_id)
        return _turn_view(turns[-1]) if turns else None

    # ------------------------------------------------------------------ commands

    def command(self, session_id: int, cmd: Command) -> str:
        """Run one command; its reply goes out as an ``info`` event and is returned."""
        try:
            reply = self._command(session_id, cmd)
        except ChatError as exc:
            self._emit(session_id, "error", {"message": str(exc), "command": cmd.name})
            raise
        self._emit(session_id, "info", {"command": cmd.name, "text": reply})
        return reply

    def _command(self, session_id: int, cmd: Command) -> str:
        if cmd.name == "help":
            return HELP
        if cmd.name == "tier":
            r = self.set_tier(session_id, cmd.arg)
            return f"next message: tier {r.tier}, {r.endpoint.name} ({r.endpoint.kind})"
        if cmd.name == "model":
            return self._model_command(session_id, cmd.arg)
        if cmd.name == "abort":
            return "aborting the running turn" if self.abort(session_id) else "nothing is running"
        if cmd.name == "cost":
            return self.cost(session_id)["text"]
        if cmd.name == "status":
            st = self.status(session_id)
            return (
                f"session {st['id']} on {st['repo']} ({st['branch']} from {st['base_branch']}): "
                f"{st['state']}, tier {st['tier']} {st['model']}"
                + (" · local only" if st["local_only"] else "")
                + (" · network on" if st["network"] else "")
                + f" · locks: {', '.join(st['locks']) or 'none'} · {st['cost']}"
            )
        if cmd.name == "verify":
            out = self.verify(session_id)
            return out.summary + (f"\n{out.tail}" if out.tail else "")
        if cmd.name == "approve":
            self.approve(session_id)
            usd = self.spend.approval_usd
            return f"approved: one message may spend up to ${usd:.2f} past the caps"
        if cmd.name == "pr":
            s = self.open_pr(session_id, title=cmd.arg or None)
            return f"opened draft PR #{s.pr_number} from {s.branch}"
        if cmd.name == "discard":
            if cmd.arg.strip().lower() not in ("yes", "y", "confirm"):
                return "this deletes the session's worktree and branch; type /discard yes"
            self.discard(session_id)
            return "discarded: worktree and branch removed, nothing pushed"
        raise ChatError(f"/{cmd.name} is not handled here")

    def _model_command(self, session_id: int, arg: str) -> str:
        live = self._live_or_raise(session_id)
        name = arg.strip()
        if not name:
            r = live.rung
            have = ", ".join(f"{x.tier}:{x.endpoint.name}" for x in live.ladder)
            return f"tier {r.tier}, {r.endpoint.name} ({r.endpoint.kind}); ladder {have}"
        for r in live.ladder:
            if r.endpoint.name == name:
                self.set_tier(session_id, str(r.tier))
                return f"next message: tier {r.tier}, {r.endpoint.name} ({r.endpoint.kind})"
        raise ChatError(f"{name} is not on this session's ladder")

    def _emit_tier(self, live: _Live) -> None:
        r = live.rung
        self._emit(
            live.id,
            "tier",
            {"tier": r.tier, "model": r.endpoint.name, "kind": r.endpoint.kind},
        )

    def set_tier(self, session_id: int, arg: str) -> Rung:
        """``/tier up|down|N``: the next message's model; the running turn keeps its own
        (spend pins it). A local-only ladder tops out at its last local tier (#49)."""
        live = self._live_or_raise(session_id)
        with live.lock:
            new = move_tier(live.ladder, live.rung.tier, arg)
            live.rung = new
            self.spend.set_chat_model(session_id, new.endpoint.name)
            self.store.update_chat_session(session_id, tier=new.tier, model=new.endpoint.name)
            live.last_activity = self._clock()
        self._emit_tier(live)
        return new

    def abort(self, session_id: int) -> bool:
        live = self._live.get(session_id)
        if live is None or live.turn is None:
            return False
        return bool(live.server.abort())

    def approve(self, session_id: int) -> ChatTurn | None:
        """``/approve``: one turn may pass a cap, up to ``approval_usd`` (held in memory).
        The message the cap refused is sent again now, once; the manager never re-sends
        one by itself (7e)."""
        live = self._live_or_raise(session_id)
        self.spend.approve_chat(session_id)
        with live.lock:
            text = live.refused_text
            if text is None or live.state != "idle" or live.turn is not None:
                return None
            live.refused_text = None
        return self._begin_turn(live, text)

    def verify(self, session_id: int) -> VerifyOutcome:
        row = self.get(session_id)
        live = self._live.get(session_id)
        if row.state in FINAL_STATES:
            raise ChatError(f"chat session {session_id} is {row.state}")
        wt = live.worktree if live else self._mirror(row.repo).open_chat(session_id)
        return self._verifier(row, wt)

    # ------------------------------------------------------------------ ends

    def _close_live(self, live: _Live | None, outcome: str, **extra: Any) -> None:
        """Stop the server and the proxy context, close the recording, forget it."""
        if live is None:
            return
        with self._lock:
            if self._live.get(live.id) is live:
                del self._live[live.id]
        with _HOSTED_LOCK:
            _HOSTED.discard(self._db_key(live.id))
        with contextlib.suppress(Exception):
            live.server.stop()
        with contextlib.suppress(Exception):
            live.ctx_cm.__exit__(None, None, None)
        if live.recorder is not None:
            with contextlib.suppress(Exception):
                live.recorder.close(outcome, **extra)
        self.spend.end_chat_session(live.id)
        with contextlib.suppress(OSError):
            (live.state_dir / HOST_FILE).unlink()

    def _closable(self, session_id: int) -> tuple[Any, _Live | None]:
        try:
            row = self.store.get_chat_session(session_id)
        except NotFoundError as exc:
            raise ChatError(f"no chat session {session_id}") from exc
        if row.state in FINAL_STATES:
            raise ChatError(f"chat session {session_id} is already {row.state}")
        live = self._live.get(session_id)
        if live is None and row.state in CLAIMED_STATES and self._host_alive(session_id):
            raise ChatError(f"chat session {session_id} is open in another process")
        if live is not None and (live.state != "idle" or live.turn is not None):
            raise ChatError("still working: /abort or wait")
        return row, live

    def open_pr(self, session_id: int, *, title: str | None = None) -> ChatSession:
        """Commit, verify (sandboxed), push ``fman/chat/<id>``, open a **draft** PR with
        no AI review (decision 3, 7a: the bot never marks it ready, merges or approves).
        A failing verify does not block (the user decided); the body's first line says
        so. Then the server stops, the worktree goes (the remote branch stays), the
        locks are released and the row is ``opened_pr``."""
        row, live = self._closable(session_id)
        if self.github is None:
            raise ChatError("no GitHub client: cannot open a PR")
        if live is not None:
            live.state = "closing"
        self.store.set_chat_state(session_id, "closing")
        try:
            wt = live.worktree if live else self._mirror(row.repo).open_chat(session_id)
            pr_title = self._pr_title(session_id, title, live)
            head = wt.commit_all(f"{pr_title}\n\nFrom foreman-inator chat session {session_id}.")
            if wt.head_sha() == wt.base_sha:
                raise ChatError("nothing to open: the session changed no files")
            outcome = self._verifier(_view(row), wt)
            body = self._pr_body(_view(row), outcome)
            self._push(wt, self._token())
            pr = self.github.create_draft_pr(
                row.repo, head=row.branch, base=row.base_branch, title=pr_title, body=body
            )
        except Exception as exc:
            with contextlib.suppress(StoreError):
                self.store.set_chat_state(session_id, "idle" if live is not None else "ended")
            if live is not None:
                live.state = "idle"
            if isinstance(exc, ChatError):
                raise
            raise ChatError(f"could not open the PR: {self.scrub(exc)}") from exc
        head_sha = head or wt.head_sha()
        self._close_live(live, "opened_pr", pr_number=pr.number, head_commit=head_sha)
        with contextlib.suppress(Exception):
            wt.remove(delete_branch=True)
        self.store.set_chat_state(
            session_id, "opened_pr", pr_number=pr.number, end_reason="opened_pr"
        )
        self.locks.release_chat(session_id)
        self._emit(session_id, "end", {"reason": "opened_pr", "pr_number": pr.number})
        return self.get(session_id)

    def _first_message(self, session_id: int, live: _Live | None) -> str | None:
        if live is not None and live.first_message:
            return live.first_message
        path = chat_recording_path(self.recordings_dir, session_id)
        side = path.parent / f"{session_id}.files"
        candidates = sorted(side.glob("before-resume-*.jsonl.gz")) + [path]
        for p in candidates:
            try:
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        row = json.loads(line)
                        if row.get("kind") == "user" and row.get("text"):
                            return str(row["text"])
            except (OSError, ValueError):
                continue
        return None

    def _pr_title(self, session_id: int, title: str | None, live: _Live | None) -> str:
        from foreman_inator.tickets.prompt import defuse

        if title and title.strip():
            return _one_line(self.scrub(title), TITLE_LIMIT)
        first = self._first_message(session_id, live)
        if first and first.strip():
            line = next(ln for ln in first.splitlines() if ln.strip())
            return _one_line(defuse(self.scrub(line)), TITLE_LIMIT)
        return f"Chat session {session_id}"

    def _pr_body(self, session: ChatSession, outcome: VerifyOutcome) -> str:
        """No ticket marker (``publisher.body_ticket_id`` is None), so the worker never
        adopts the PR."""
        from foreman_inator.runner import sandbox as sb
        from foreman_inator.tickets.prompt import defuse

        used: dict[tuple[int, str], None] = {}
        for t in self.store.list_chat_turns(session.id):
            used[(t.tier, t.model)] = None
        models = ", ".join(f"tier {t} {m}" for t, m in used) or "none (no message sent)"
        lines: list[str] = []
        if outcome.passed is False:
            lines += [f"**Verify failed** ({outcome.summary}); opened as asked.", ""]
        lines += [
            f"Opened from chat session {session.id} (branch `{session.branch}` from "
            f"`{session.base_branch}`).",
            "",
            f"- Tiers and models used: {models}",
            f"- Cost: ${self.store.chat_spend(session.id):.2f}",
            f"- Verify: {outcome.summary}",
        ]
        if self.store.repo_allows_chat_network(session.repo):
            lines.append("- Chat network: on for this repo (ports 443/80)")
        lines += [
            "",
            sb.describe(getattr(self.config, "sandbox", "on"), dict(self.config.sandbox_network)),
            "",
            "Not reviewed by the AI reviewer: the user was in the loop for every change.",
        ]
        if outcome.tail:
            tail = defuse(self.scrub(outcome.tail[-TAIL_LIMIT:])).replace("```", "'''")
            lines += ["", "<details><summary>Verify output (tail)</summary>", "", "```text",
                      tail, "```", "</details>"]  # fmt: skip
        return self.scrub("\n".join(lines))

    def discard(self, session_id: int) -> ChatSession:
        """Server stopped, worktree and local branch removed, nothing pushed."""
        row, live = self._closable(session_id)
        self._close_live(live, "discarded")
        if live is not None:
            wt = live.worktree
        else:
            mirror = self._mirror(row.repo)
            wt = Worktree(mirror, mirror.chat_path(session_id), row.branch, row.base_commit or "")
        wt.remove(delete_branch=True)
        self.store.set_chat_state(session_id, "discarded", end_reason="discarded")
        self.locks.release_chat(session_id)
        self._emit(session_id, "end", {"reason": "discarded"})
        return self.get(session_id)

    def _end(self, live: _Live, reason: str) -> None:
        """Idle timeout or host exit: server stopped, worktree kept, locks released,
        row ``ended`` (it can be resumed, opened as a PR or discarded later)."""
        self._close_live(live, "ended", end_reason=reason)
        with contextlib.suppress(StoreError):
            self.store.set_chat_state(live.id, "ended", end_reason=reason)
        self.locks.release_chat(live.id)
        self._emit(live.id, "end", {"reason": reason})

    def end_idle(self) -> list[int]:
        """Sessions idle past ``idle_minutes`` -> ``ended`` (worktree kept; 7e)."""
        now = self._clock()
        limit = self.idle_minutes * 60.0
        ended = []
        for live in list(self._live.values()):
            with live.lock:
                stale = live.state == "idle" and live.turn is None
                stale = stale and now - live.last_activity >= limit
            if stale:
                self._end(live, "idle_timeout")
                ended.append(live.id)
        return ended

    def shutdown(self) -> None:
        """Host process exit: every live session -> ``ended``; servers stopped."""
        self._stop.set()
        for live in list(self._live.values()):
            if live.turn is not None:
                with contextlib.suppress(Exception):
                    live.server.abort()
                if live.thread is not None:
                    live.thread.join(_JOIN_S)
            self._end(live, "host_exit")
        if self._owns_proxy and self.proxy is not None:
            with contextlib.suppress(Exception):
                self.proxy.stop()

    def recover(self) -> list[int]:
        """Crash safety: rows that claim a live server no live host has (this one
        included) are settled: ``starting`` -> ``failed`` (its half-made worktree
        removed: nothing of the user's is in it), ``idle``/``busy``/``closing`` ->
        ``ended`` (``host_restart``; worktree kept, resumable); their running turns
        -> ``errored``. Any lock held by a session that is not open goes (decision 6)."""
        fixed = []
        for row in self.store.list_chat_sessions(states=CLAIMED_STATES):
            if row.id in self._live or row.id in self._starting or self._host_alive(row.id):
                continue
            for t in self.store.list_chat_turns(row.id):
                if t.status is None:
                    with contextlib.suppress(StoreError):
                        self.store.finish_chat_turn(t.id, "errored")
            if row.state == "starting":
                with contextlib.suppress(Exception):
                    mirror = self._mirror(row.repo)
                    Worktree(mirror, mirror.chat_path(row.id), row.branch, "").remove()
                self.store.set_chat_state(row.id, "failed", end_reason="host_restart")
            else:
                self.store.set_chat_state(row.id, "ended", end_reason="host_restart")
            fixed.append(row.id)
        holders = {lk.chat_session_id for lk in self.store.list_locks() if lk.chat_session_id}
        for sid in sorted(holders):
            if sid in self._live:
                continue
            state = self.store.get_chat_session(sid).state
            if state not in CLAIMED_STATES or not self._host_alive(sid):
                self.locks.release_chat(sid)
        return fixed


__all__ = [
    "CHAT_BRANCH_PREFIX",
    "COMMANDS",
    "DEFAULT_CHAT_MONTHLY_USD",
    "DEFAULT_IDLE_MINUTES",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_START_TIER",
    "DEFAULT_TURN_BUDGET_USD",
    "EVENT_TYPES",
    "HELP",
    "ChatError",
    "ChatEvent",
    "ChatManager",
    "ChatServerLike",
    "ChatSession",
    "ChatState",
    "ChatTurn",
    "Command",
    "EventBuffer",
    "Rung",
    "VerifyOutcome",
    "chat_branch",
    "move_tier",
    "parse_command",
    "rung_for",
    "start_rung",
    "text_delta",
    "tier_ladder",
]
