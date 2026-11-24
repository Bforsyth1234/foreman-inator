"""The ticket lifecycle: a pure state machine over the SPEC columns (todo #11).

``apply(column, event) -> Transition`` takes a ticket's current column (``None`` when no
ticket exists yet) and one event, and returns the new column plus the actions the worker
must take, in order. It does no I/O: no GitHub, no SQLite, no clock. The poller
(``foreman_inator.github.poller``) turns GitHub state into the GitHub events below; the
worker loop (#14, #21) produces the worker events itself. Replays (#28) and escalation
(#16) can drive it with recorded events.

Columns (SPEC "Data model"): ``todo`` (labeled, not started) · ``in_progress`` (worker
running) · ``testing`` (draft PR awaiting AI review or the user's review) · ``done``
(merged) · ``dropped`` (closed unmerged). ``done`` and ``dropped`` are terminal: every
event on them raises ``IllegalTransition``, as does any event the table below does not
list for a column.

SPEC "Architecture and GitHub mapping", row by row:

=========================================  ===============================================
GitHub event                               Event here → transition
=========================================  ===============================================
``agent`` label added (allowed user)       ``TicketRequested``: None → todo
``tier:N`` label on the issue              ``TicketRequested.tier_override`` or
                                           ``TierOverride``: same column
Attempt passes checks                      ``ChecksPassed``: in_progress → testing
PR review: Request changes                 ``ReviewSubmitted(changes)``: testing →
                                           in_progress
PR review: Approve, then merge             ``ReviewSubmitted(approve)``: testing → testing,
                                           then ``Merged``: testing|in_progress → done
Issue closed without merge                 ``ClosedUnmerged``: open → dropped
=========================================  ===============================================

Beyond the table (decisions recorded in todo #11): ``AgentLabelRemoved`` drops the
ticket; ``IssueEdited`` updates the request for the next attempt; a review on a commit
that is no longer the PR head (``on_head=False``) changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

Column = Literal["todo", "in_progress", "testing", "done", "dropped"]
OPEN: frozenset[str] = frozenset({"todo", "in_progress", "testing"})
TERMINAL: frozenset[str] = frozenset({"done", "dropped"})

#: Kickback tags (SPEC "kickbacks.tag"), read from ``kickback:<tag>`` labels on the PR.
KICKBACK_TAGS: tuple[str, ...] = ("wrong", "incomplete", "broke", "style")


class IllegalTransition(ValueError):
    """The event is not allowed in the ticket's current column."""

    def __init__(self, column: str | None, event: object) -> None:
        self.column = column
        self.event = event
        super().__init__(f"{type(event).__name__} is not allowed in column {column!r}")


class Action(StrEnum):
    """What the worker does after a transition. Payloads come from the event itself."""

    CREATE_TICKET = "create_ticket"  # store.create_ticket(title=, request=body, ...)
    APPLY_TIER_OVERRIDE = "apply_tier_override"  # record tier:N in route_reason / routed_tier
    ROUTE = "route"  # router (#15), honoring an override
    START_ATTEMPT = "start_attempt"  # first attempt
    UPDATE_REQUEST = "update_request"  # store.update_ticket(title=, request=)
    OPEN_OR_UPDATE_DRAFT_PR = "open_or_update_draft_pr"  # publisher (#12)
    AI_REVIEW = "ai_review"  # reviewer (#17)
    MARK_READY_FOR_REVIEW = "mark_ready_for_review"  # only after approve / handover (7a)
    RECORD_KICKBACK = "record_kickback"  # store.record_kickback(by=user|reviewer, tag, note)
    CLEAR_KICKBACK_LABEL = "clear_kickback_label"  # remove kickback:<tag> from the PR
    NEXT_ATTEMPT = "next_attempt"  # escalation (#16): retry, escalate or hand to user
    RECORD_APPROVAL = "record_approval"  # the user's approve (accept happens on merge)
    RECORD_ACCEPT = "record_accept"  # store.set_accepted_attempt
    CLOSE_ISSUE = "close_issue"  # no-op if the merge already closed it
    POST_SUMMARY = "post_summary"  # one-line summary comment (#20)
    CANCEL_ATTEMPT = "cancel_attempt"  # stop a running attempt
    RELEASE_LOCKS = "release_locks"  # file locks (#19)
    CLEANUP_WORKTREE = "cleanup_worktree"
    FLAG_TICKET = "flag_ticket"  # store.flag_ticket: handed to the user (7e)


# --------------------------------------------------------------------------- events

#: A cursor write the worker makes (through ``Poller.ack``) once it has handled the event.
CursorWrite = tuple[str, str]


@dataclass(frozen=True)
class GitHubEvent:
    """Base of the events the poller derives from GitHub state."""

    repo: str
    issue_number: int
    #: Cursor writes that mark this event handled. ``Poller.ack(event)`` applies them.
    cursors: tuple[CursorWrite, ...] = field(default=(), kw_only=True, compare=False)


@dataclass(frozen=True)
class TicketRequested(GitHubEvent):
    """``agent`` label applied by an allowed user to an open issue with no open ticket."""

    title: str
    body: str
    labeled_by: str
    label_event_id: int
    tier_override: int | None = None  # from a ``tier:N`` label by an allowed user
    review_skip: bool = False  # ``review:skip`` label by an allowed user (#17)


@dataclass(frozen=True)
class TierOverride(GitHubEvent):
    """A new ``tier:N`` label, by an allowed user, on an issue with an open ticket."""

    ticket_id: int
    tier: int
    labeled_by: str


@dataclass(frozen=True)
class IssueEdited(GitHubEvent):
    """The issue's title or body differs from the ticket's stored title/request."""

    ticket_id: int
    title: str
    body: str


@dataclass(frozen=True)
class AgentLabelRemoved(GitHubEvent):
    """An allowed user removed the ``agent`` label while the ticket was open."""

    ticket_id: int
    removed_by: str


@dataclass(frozen=True)
class ReviewSubmitted(GitHubEvent):
    """A human review (allowed user, not the bot) on the ticket's PR while in testing."""

    ticket_id: int
    pr_number: int
    review_id: int
    reviewer: str
    verdict: Literal["approve", "changes"]
    comment: str  # the review body: the kickback note
    kickback_tag: str | None = None  # from a ``kickback:<tag>`` PR label (allowed user)
    kickback_label: str | None = None  # the label itself, for CLEAR_KICKBACK_LABEL
    commit_id: str | None = None
    on_head: bool = True  # False: the review is on an older commit than the PR head


@dataclass(frozen=True)
class Merged(GitHubEvent):
    """The ticket's PR was merged (by the user: the worker cannot merge)."""

    ticket_id: int
    pr_number: int
    merged_at: datetime | None = None


@dataclass(frozen=True)
class ClosedUnmerged(GitHubEvent):
    """The issue, or the ticket's PR, was closed with no merged PR."""

    ticket_id: int
    pr_number: int | None
    what: Literal["issue", "pr"]


# Worker events: produced by the worker loop, never by the poller.


@dataclass(frozen=True)
class AttemptStarted:
    ticket_id: int
    trigger: Literal["first", "escalation", "kickback", "ai_kickback", "explore"] = "first"


@dataclass(frozen=True)
class ChecksPassed:
    ticket_id: int
    review_skip: bool = False  # ``review:skip`` → PR goes straight to the user


@dataclass(frozen=True)
class ChecksFailed:
    ticket_id: int


@dataclass(frozen=True)
class AIReviewed:
    ticket_id: int
    verdict: Literal["approve", "request_changes", "handed_to_user"]


@dataclass(frozen=True)
class HandedToUser:
    """Escalation ran out (e.g. tier 3 failed): flag the ticket, column unchanged (7e)."""

    ticket_id: int
    reason: str


Event = (
    TicketRequested
    | TierOverride
    | IssueEdited
    | AgentLabelRemoved
    | ReviewSubmitted
    | Merged
    | ClosedUnmerged
    | AttemptStarted
    | ChecksPassed
    | ChecksFailed
    | AIReviewed
    | HandedToUser
)


@dataclass(frozen=True)
class Transition:
    column: Column
    actions: tuple[Action, ...] = ()


# --------------------------------------------------------------------------- the machine

_A = Action
_STOP = (_A.RELEASE_LOCKS, _A.CLEANUP_WORKTREE)


def apply(column: str | None, event: Event) -> Transition:
    """Return the column after ``event`` and the actions to take. Raises IllegalTransition."""
    if column is not None and column not in OPEN | TERMINAL:
        raise ValueError(f"unknown column {column!r}")
    if column is None:
        if isinstance(event, TicketRequested):
            pre = (_A.APPLY_TIER_OVERRIDE,) if event.tier_override is not None else ()
            return Transition("todo", (_A.CREATE_TICKET, *pre, _A.ROUTE, _A.START_ATTEMPT))
        raise IllegalTransition(column, event)
    if column in TERMINAL:
        raise IllegalTransition(column, event)

    col: Column = column  # type: ignore[assignment]
    running = col == "in_progress"
    cancel = (_A.CANCEL_ATTEMPT,) if running else ()

    match event:
        case TierOverride():
            return Transition(col, (_A.APPLY_TIER_OVERRIDE,))
        case IssueEdited():
            return Transition(col, (_A.UPDATE_REQUEST,))
        case AgentLabelRemoved() | ClosedUnmerged():
            return Transition("dropped", (*cancel, *_STOP))
        case Merged() if col in ("in_progress", "testing"):
            return Transition(
                "done", (*cancel, _A.RECORD_ACCEPT, _A.CLOSE_ISSUE, _A.POST_SUMMARY, *_STOP)
            )
        case ReviewSubmitted() if col == "testing":
            if not event.on_head:
                return Transition(col)  # stale review on an older commit: ignored
            if event.verdict == "approve":
                return Transition(col, (_A.RECORD_APPROVAL,))
            clear = (_A.CLEAR_KICKBACK_LABEL,) if event.kickback_label else ()
            return Transition("in_progress", (_A.RECORD_KICKBACK, *clear, _A.NEXT_ATTEMPT))
        case AttemptStarted() if col in ("todo", "in_progress"):
            return Transition("in_progress")
        case ChecksPassed() if running:
            nxt = _A.MARK_READY_FOR_REVIEW if event.review_skip else _A.AI_REVIEW
            return Transition("testing", (_A.OPEN_OR_UPDATE_DRAFT_PR, nxt))
        case ChecksFailed() if running:
            return Transition(col, (_A.NEXT_ATTEMPT,))
        case AIReviewed() if col == "testing":
            if event.verdict == "approve":
                return Transition(col, (_A.MARK_READY_FOR_REVIEW,))
            if event.verdict == "handed_to_user":
                return Transition(col, (_A.MARK_READY_FOR_REVIEW, _A.FLAG_TICKET))
            return Transition("in_progress", (_A.RECORD_KICKBACK, _A.NEXT_ATTEMPT))
        case HandedToUser():
            return Transition(col, (_A.FLAG_TICKET,))
    raise IllegalTransition(column, event)


__all__ = [
    "KICKBACK_TAGS",
    "OPEN",
    "TERMINAL",
    "AIReviewed",
    "Action",
    "AgentLabelRemoved",
    "AttemptStarted",
    "ChecksFailed",
    "ChecksPassed",
    "ClosedUnmerged",
    "Column",
    "CursorWrite",
    "Event",
    "GitHubEvent",
    "HandedToUser",
    "IllegalTransition",
    "IssueEdited",
    "Merged",
    "ReviewSubmitted",
    "TicketRequested",
    "TierOverride",
    "Transition",
    "apply",
]
