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

Wave 6b:

* #41, GitHub CI between "checks pass + draft PR pushed" and the AI review: the poller
  emits ``CiResult`` (decided by the pure ``decide_ci`` below) for a ticket in
  ``testing`` whose PR attempt waits on CI. ``failed`` (red, and not also red on the base
  branch) → ``in_progress`` with ``RECORD_CI, NEXT_ATTEMPT`` (counts as failed checks);
  any other result → ``testing`` with ``RECORD_CI`` (the AI review may go ahead).
* #42, line comments: a request-changes ``ReviewSubmitted`` carries the review's line
  comments (``line_comments``); ``kickback_note`` is the note to store, the review body
  plus the comments rendered by ``format_line_comments`` after ``LINE_COMMENTS_MARKER``.
  The prompt builder splits the stored note there (``split_kickback_note``).
* #43, base conflicts: ``BaseConflict`` (the PR's ``mergeable`` is ``false``) on a ticket
  in ``testing`` → ``in_progress`` with ``START_REBASE`` (one attempt, trigger ``rebase``,
  on the tier that wrote the change) while ``rebase_allowed`` (at most ``REBASE_CAP`` = 2
  rebase attempts per ticket); after that → ``testing`` with ``FLAG_TICKET`` (7e). Not a
  kickback: nothing is recorded against the attempt.
* #44, clarifying questions: ``NeedsInfoAnswered`` (an allowed user answered the
  worker's question on the issue, or removed ``needs-info``) on a ticket in ``todo`` →
  ``todo`` with no action: its cursors (``clarify_cursor`` = how it resumed,
  ``clarify_qa_cursor`` = the Q&A text for the writer prompt) are the whole effect, and
  the worker's route job picks the ticket up on its next pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

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
    RECORD_CI = "record_ci"  # store.set_ci_result (#41); "failed" also saves the log tail
    START_REBASE = "start_rebase"  # #43: one attempt with trigger "rebase" (replay + resolve)


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
class LineComment:
    """One line comment of a request-changes review (#42)."""

    path: str
    line: int | None  # None: a comment on the whole file
    diff_hunk: str
    body: str


#: Separates a kickback note's review body from its rendered line comments (#42). An HTML
#: comment, so the note reads cleanly wherever it is shown as Markdown.
LINE_COMMENTS_MARKER = "<!-- fman:line-comments -->"
#: Lines of each comment's diff hunk kept in the note (todo #42 decision 2).
HUNK_LINES = 3


def format_line_comments(comments: Sequence[LineComment]) -> str:
    """Todo #42 decision 2: each comment as ``path:line``, the last 3 lines of its diff
    hunk, then its text; in file order (path, then line; file-level comments first)."""
    blocks = []
    for c in sorted(comments, key=lambda c: (c.path, c.line if c.line is not None else -1)):
        where = f"{c.path}:{c.line}" if c.line is not None else f"{c.path} (whole file)"
        hunk = [ln for ln in c.diff_hunk.replace("\r\n", "\n").split("\n") if ln.strip()]
        lines = [where, *hunk[-HUNK_LINES:], c.body.replace("\r\n", "\n").strip()]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def compose_kickback_note(body: str, comments: Sequence[LineComment]) -> str:
    """The note stored for a user kickback: the review body, then the line comments."""
    body = (body or "").strip()
    if not comments:
        return body
    return f"{body}\n\n{LINE_COMMENTS_MARKER}\n{format_line_comments(comments)}".lstrip()


def split_kickback_note(note: str) -> tuple[str, str | None]:
    """``(body, rendered line comments or None)`` of a stored note."""
    body, sep, rest = (note or "").partition(LINE_COMMENTS_MARKER)
    if not sep:
        return note or "", None
    return body.strip(), rest.strip() or None


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
    #: #42: the review's line comments (request changes by an allowed user, on the head).
    line_comments: tuple[LineComment, ...] = ()

    @property
    def kickback_note(self) -> str:
        """What RECORD_KICKBACK stores as the note: body + rendered line comments (#42)."""
        return compose_kickback_note(self.comment, self.line_comments)


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


CiState = Literal["passed", "failed", "base_red", "no_ci", "timed_out", "no_permission"]


@dataclass(frozen=True)
class CiResult(GitHubEvent):
    """GitHub CI on the PR head reached a decision for the attempt waiting on it (#41).

    ``state`` is the store's ``attempts.ci_status``. Only ``failed`` is charged to the
    attempt; ``note`` is the line for the PR description (base red, pending too long, no
    permission); ``failure_output`` is the failing job's log tail for the retry prompt.
    No cursor: the poller stops emitting once ``attempts.ci_status`` is set."""

    ticket_id: int
    attempt_id: int
    head_sha: str
    state: CiState
    note: str | None = None
    failed_jobs: tuple[str, ...] = ()
    failure_output: str | None = None

    @property
    def charged(self) -> bool:
        return self.state == "failed"


@dataclass(frozen=True)
class BaseConflict(GitHubEvent):
    """The ticket's PR conflicts with its base branch (``mergeable`` is ``false``) while
    the ticket is in ``testing`` (#43). ``rebases_done`` is ``count_rebase_attempts`` of
    the ticket's attempts when the poller saw it; the cursor is the PR head it was seen
    on, so one head conflicts once."""

    ticket_id: int
    pr_number: int
    head_sha: str
    rebases_done: int = 0

    @property
    def reason(self) -> str:
        """The flag reason when the cap is reached (read by the worker's FLAG_TICKET)."""
        return (
            f"{FLAG_REBASE_CAP}: PR #{self.pr_number} still conflicts with its base branch"
            f" after {self.rebases_done} rebase attempts"
        )


def clarify_cursor(ticket_id: int) -> str:
    """#44: the ``poll_cursors`` name holding a ticket's clarification state: ``clear`` |
    ``asked`` | ``answered`` | ``released`` | ``resumed`` (no row: not assessed yet)."""
    return f"clarify:{int(ticket_id)}"


def clarify_qa_cursor(ticket_id: int) -> str:
    """#44: the ``poll_cursors`` name holding the Q&A text for the writer prompt."""
    return f"clarify-qa:{int(ticket_id)}"


@dataclass(frozen=True)
class NeedsInfoAnswered(GitHubEvent):
    """#44: the worker asked on the issue (``needs-info``) and an allowed user answered in
    a comment (``how="answered"``, ``qa`` = the Q&A text) or removed the label
    (``how="released"``: go ahead as written). The event's cursors store both."""

    ticket_id: int
    how: Literal["answered", "released"]
    by: str
    qa: str | None = None


# Worker events: produced by the worker loop, never by the poller.


@dataclass(frozen=True)
class AttemptStarted:
    ticket_id: int
    trigger: Literal["first", "escalation", "kickback", "ai_kickback", "explore", "rebase"] = (
        "first"
    )


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
    | CiResult
    | BaseConflict
    | NeedsInfoAnswered
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
        case CiResult() if col == "testing":
            if event.charged:
                return Transition("in_progress", (_A.RECORD_CI, _A.NEXT_ATTEMPT))
            return Transition(col, (_A.RECORD_CI,))
        case NeedsInfoAnswered() if col == "todo":
            return Transition(col)
        case BaseConflict() if col == "testing":
            if event.rebases_done < REBASE_CAP:
                return Transition("in_progress", (_A.START_REBASE,))
            return Transition(col, (_A.FLAG_TICKET,))
    raise IllegalTransition(column, event)


# --------------------------------------------------------------------------- rebase (#43)

#: Rebase attempts per ticket before the conflict is handed to the user (7e).
REBASE_CAP = 2
#: The ``flag_reason`` prefix when the cap is reached.
FLAG_REBASE_CAP = "rebase_cap"


class AttemptLike(Protocol):
    id: int
    trigger: str
    outcome: str | None


def count_rebase_attempts(attempts: Sequence[AttemptLike]) -> int:
    """Rebase attempts the ticket has had, whatever their outcome (an interrupted one
    counts too, so a crash loop is bounded by the cap as well)."""
    return sum(1 for a in attempts if a.trigger == "rebase")


def rebase_allowed(attempts: Sequence[AttemptLike], cap: int = REBASE_CAP) -> bool:
    """May the ticket start (another) rebase attempt? For the worker's policy, e.g. after
    a rebase attempt failed its checks: True → another rebase attempt, False → flag."""
    return count_rebase_attempts(attempts) < cap


def rebase_uncounted(attempts: Sequence[AttemptLike]) -> frozenset[int]:
    """Attempts escalation must not see because of rebases (todo #43 decision 3): every
    rebase attempt that failed its checks or was interrupted, and the attempt a rebase
    replaced on the PR (the latest non-rebase attempt before each rebase attempt). A
    rebase attempt that passed carries the writer's change on and counts as usual; so
    does one stopped at its per-attempt budget (#58), so escalation hands it over."""
    out: set[int] = set()
    last_writer: int | None = None
    for a in attempts:
        if a.trigger != "rebase":
            last_writer = a.id
            continue
        if last_writer is not None:
            out.add(last_writer)
        if getattr(a, "end_reason", None) == "budget":
            continue
        if a.outcome in ("failed_checks", "superseded"):
            out.add(a.id)
    return frozenset(out)


# --------------------------------------------------------------------------- CI (#41)

#: No check runs this long after the attempt's change was ready → the repo has no CI.
CI_GRACE = timedelta(minutes=2)
#: Check runs still not finished this long after → go on, and say so in the PR body.
CI_PENDING_LIMIT = timedelta(minutes=30)
#: Conclusions that make a check run red. ``cancelled``, ``skipped``, ``neutral``,
#: ``stale`` and ``action_required`` are not charged to the attempt.
CI_FAILED = frozenset({"failure", "timed_out", "startup_failure"})


#: The PR-description line for a CI result that let the attempt go on (#41 decisions 3-5).
CI_STATUS_NOTES: dict[str, str] = {
    "base_red": "CI already red on base",
    "timed_out": "CI still pending after 30 minutes",
    "no_permission": "CI not checked: the App lacks the checks: read permission",
    "no_ci": "No CI runs on this repository",
}


class CheckRunLike(Protocol):
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class CiDecision:
    """``state`` is a ``CiState`` or ``pending`` (wait) or ``needs_base`` (red: read the
    base branch head's runs and decide again with ``base_runs``)."""

    state: Literal["pending", "needs_base", "passed", "failed", "base_red", "no_ci", "timed_out"]
    failed: tuple[str, ...] = ()  # names of the red runs charged to the attempt
    note: str | None = None

    @property
    def decided(self) -> bool:
        return self.state not in ("pending", "needs_base")


def decide_ci(
    runs: Sequence[CheckRunLike],
    *,
    waiting_since: datetime,
    now: datetime,
    base_runs: Sequence[CheckRunLike] | None = None,
    grace: timedelta = CI_GRACE,
    pending_limit: timedelta = CI_PENDING_LIMIT,
) -> CiDecision:
    """Todo #41 decisions 3 and 4, pure (the clock is ``now``).

    * no runs: ``pending`` inside the grace period, then ``no_ci`` (skip the step);
    * runs not all completed: ``pending`` until the pending limit, then decide on the
      completed ones (``timed_out`` with a note if none of them is red);
    * all completed and none red: ``passed``;
    * red ones: ``needs_base`` until ``base_runs`` is given; then ``failed`` for the red
      ones that are not also red (same name) on the base branch head, else ``base_red``
      (not charged; the PR notes "CI already red on base").
    """
    waited = now - waiting_since
    if not runs:
        return CiDecision("pending") if waited < grace else CiDecision("no_ci")
    unfinished = sorted({r.name for r in runs if r.status != "completed"})
    red = sorted({r.name for r in runs if r.status == "completed" and r.conclusion in CI_FAILED})
    if unfinished and waited < pending_limit:
        return CiDecision("pending")
    late = (
        f"CI still pending after {int(pending_limit.total_seconds() // 60)} minutes: "
        + ", ".join(unfinished)
        if unfinished
        else None
    )
    if not red:
        return CiDecision("timed_out", note=late) if unfinished else CiDecision("passed")
    if base_runs is None:
        return CiDecision("needs_base", failed=tuple(red), note=late)
    base_red = {r.name for r in base_runs if r.status == "completed" and r.conclusion in CI_FAILED}
    charged = tuple(n for n in red if n not in base_red)
    if charged:
        return CiDecision("failed", failed=charged, note=late)
    note = "CI already red on base: " + ", ".join(red)
    return CiDecision("base_red", note=f"{note}. {late}" if late else note)


__all__ = [
    "KICKBACK_TAGS",
    "OPEN",
    "TERMINAL",
    "CI_FAILED",
    "CI_GRACE",
    "CI_PENDING_LIMIT",
    "CI_STATUS_NOTES",
    "FLAG_REBASE_CAP",
    "REBASE_CAP",
    "AIReviewed",
    "Action",
    "BaseConflict",
    "CiDecision",
    "CiResult",
    "CiState",
    "HUNK_LINES",
    "LINE_COMMENTS_MARKER",
    "LineComment",
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
    "NeedsInfoAnswered",
    "ReviewSubmitted",
    "TicketRequested",
    "TierOverride",
    "Transition",
    "apply",
    "clarify_cursor",
    "clarify_qa_cursor",
    "compose_kickback_note",
    "count_rebase_attempts",
    "decide_ci",
    "format_line_comments",
    "rebase_allowed",
    "rebase_uncounted",
    "split_kickback_note",
]
