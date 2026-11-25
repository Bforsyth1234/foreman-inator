"""Chat sessions: interactive coding through the ladder, proxy, caps and sandbox (todo #71).

**Skeleton only** (wave 18, slot F7). The design is ``docs/chat-design.md``; this module
holds the types and signatures it names. What is settled is real and tested: the command
parser, the chat branch name, the per-session tier ladder and tier moves, the SSE framing
and the text-part delta. Everything that needs the wave C1 pieces (store tables, the
proxy's ``chat_session_id`` context, ``runner.opencode.ChatServer``, the chat spend gate)
is a ``ChatManager`` method that raises ``NotImplementedError``. Nothing here is wired.

SSE framing follows the WHATWG HTML standard, "Server-sent events", section 9.2.6
(https://html.spec.whatwg.org/multipage/server-sent-events.html, read 2026-11-25): a
frame is ``field: value`` lines ended by a blank line; ``id`` sets the last event id the
browser sends back as ``Last-Event-ID`` on reconnect; ``event`` names the type; each
``data`` line is one line of payload. The payload here is one line of JSON
(``json.dumps`` escapes newlines), so model text can never end a frame early or forge
another field.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from foreman_inator.config import TIERS, ModelEndpoint

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

EventType = Literal["text", "tool", "status", "tier", "cost", "refused", "error", "end"]
EVENT_TYPES: frozenset[str] = frozenset(
    {"text", "tool", "status", "tier", "cost", "refused", "error", "end"}
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

    name: Literal["tier", "abort", "cost", "verify", "pr", "discard", "approve", "help"]
    arg: str = ""


COMMANDS: frozenset[str] = frozenset(
    {"tier", "abort", "cost", "verify", "pr", "discard", "approve", "help"}
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
        self._lock = threading.Lock()

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
            return ev

    def after(self, seq: int) -> list[ChatEvent]:
        """Events with ``seq`` greater than the given one, oldest first. Events that fell
        out of the buffer are gone (the page then reloads the transcript)."""
        with self._lock:
            return [e for e in self._events if e.seq > seq]


# --------------------------------------------------------------------------- the engine


class ChatManager:
    """Runs chat sessions for one host process (``fman chat`` or ``fman metrics``): its
    own ``ModelProxy`` and ``Spend``, one sandboxed ``opencode serve`` per session.
    Built in wave C2 on top of the C1 pieces; see docs/chat-design.md sections 2-5."""

    def __init__(
        self,
        config: Any,
        store: Any,
        *,
        proxy: Any = None,
        spend: Any = None,
        worktrees: Any = None,
        github: Any = None,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        idle_minutes: float = DEFAULT_IDLE_MINUTES,
        start_tier: int = DEFAULT_START_TIER,
        turn_budget_usd: float = DEFAULT_TURN_BUDGET_USD,
    ) -> None:
        self.config = config
        self.store = store
        self.proxy = proxy
        self.spend = spend
        self.worktrees = worktrees
        self.github = github
        self.max_sessions = max_sessions
        self.idle_minutes = idle_minutes
        self.start_tier = start_tier
        self.turn_budget_usd = turn_budget_usd

    def start(
        self, repo: str, *, base_branch: str | None = None, tier: int | None = None
    ) -> ChatSession:
        """Sandbox gate, session row, worktree on ``fman/chat/<id>``, proxy context,
        OpenCode server and session (section 2, Start)."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def send(self, session_id: int, text: str) -> ChatTurn | Command:
        """One user line: a command is handled here; text starts a turn on the current
        tier's model. Refused (``ChatError``) while the session is busy."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def set_tier(self, session_id: int, arg: str) -> Rung:
        """``/tier up|down|N``: the next message's model."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def abort(self, session_id: int) -> bool:
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def approve(self, session_id: int) -> None:
        """Allow the turn refused at a cap, once, up to the approval amount."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def verify(self, session_id: int) -> Any:
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def open_pr(self, session_id: int, *, title: str | None = None) -> ChatSession:
        """Commit, verify (sandboxed), push ``fman/chat/<id>``, open a draft PR (7a)."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def discard(self, session_id: int) -> ChatSession:
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def get(self, session_id: int) -> ChatSession:
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def sessions(self) -> list[ChatSession]:
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def events(self, session_id: int, *, after: int = 0) -> Iterator[ChatEvent]:
        """Events after ``after`` (``Last-Event-ID``), then live ones until the session ends."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def end_idle(self) -> list[int]:
        """Sessions idle past ``idle_minutes`` -> ``ended`` (worktree kept)."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")

    def shutdown(self) -> None:
        """Host process exit: every live session -> ``ended``; servers stopped."""
        raise NotImplementedError("chat sessions are designed, not built (docs/chat-design.md)")


__all__ = [
    "CHAT_BRANCH_PREFIX",
    "COMMANDS",
    "DEFAULT_CHAT_MONTHLY_USD",
    "DEFAULT_IDLE_MINUTES",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_START_TIER",
    "DEFAULT_TURN_BUDGET_USD",
    "EVENT_TYPES",
    "ChatError",
    "ChatEvent",
    "ChatManager",
    "ChatSession",
    "ChatState",
    "ChatTurn",
    "Command",
    "EventBuffer",
    "Rung",
    "chat_branch",
    "move_tier",
    "parse_command",
    "rung_for",
    "start_rung",
    "text_delta",
    "tier_ladder",
]
