"""The draft PR for a ticket: one PR per ticket, body regenerated from the store each time.

One PR per ticket. Its head branch is the first attempt's branch (``pr_head_branch``),
because a PR's head cannot change; later attempts are force-pushed onto it by the worker.
This module never pushes and never merges.

Invariant 7a: ``Publisher.open_or_update_draft`` only creates drafts and edits bodies. It
never marks a PR ready. ``Publisher.mark_ready`` exists for the worker alone, after the AI
reviewer approved (``reviewer_approved``) or the ticket was handed to the user
(``handover``: the reviewer handed it over, no independent reviewer exists, the escalation
ladder ran out with a change that passed its checks, or ``review:skip``), and takes the
reason. Nothing else marks a PR ready (#21 removed v1's "ready when checks pass").

The bot's AI review is posted as a COMMENT review (``post_ai_review``): GitHub refuses
APPROVE and REQUEST_CHANGES from the PR's own author. The structured verdict lives in the
``reviews`` row; the body's AI review section is rendered from that row. Each posted
review carries a hidden per-review marker, so posting again after a crash finds the
earlier one and does not post twice.

Invariant 7c: the body holds counts, names and costs only. No model reasoning, tool
output, verify output or file contents; those stay in the local traces.

PRs changed by a person are not fought over:

- converted out of draft: the body is still kept current; it is never put back to draft.
- closed without merge: left closed and untouched (no reopen, no new PR, no body edit);
  the result's ``action`` is ``"closed"`` so the worker can flag the ticket.
- merged: left untouched; ``action`` is ``"merged"``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from foreman_inator.db import Attempt, Review, Store, Ticket
from foreman_inator.github import BRANCH_PREFIX, GitHubAPI, PullRequest
from foreman_inator.github.client import Review as GitHubReview
from foreman_inator.runner.verify import VerifyResult
from foreman_inator.runner.worktree import DiffStats, branch_name
from foreman_inator.tickets.lifecycle import CI_STATUS_NOTES

# Stable markers. #21 replaces only what sits between a START/END pair.
BODY_MARKER = "<!-- foreman-inator:pr ticket={ticket_id} -->"
HANDOVER_START = "<!-- foreman-inator:handover:start -->"
HANDOVER_END = "<!-- foreman-inator:handover:end -->"
REVIEW_START = "<!-- foreman-inator:review:start -->"
REVIEW_END = "<!-- foreman-inator:review:end -->"

_MARKER_RE = re.compile(r"<!-- foreman-inator:pr ticket=(\d+) -->")
MAX_FILES_LISTED = 50
MAX_NOTE_CHARS = 2000

ReadyReason = Literal["reviewer_approved", "handover"]
AI_REVIEW_MARKER = "<!-- foreman-inator:ai-review review={review_id} -->"
Action = Literal["created", "updated", "unchanged", "closed", "merged"]

_TRIGGERS = {
    "first": "first",
    "escalation": "escalation",
    "kickback": "kickback",
    "ai_kickback": "AI kickback",
    "explore": "explore",
    "rebase": "rebase",
}
_VERDICTS = {
    "approve": "Approved",
    "request_changes": "Changes requested",
    "handed_to_user": "Handed to you",
}


class PublishError(Exception):
    """The PR cannot be published safely (wrong head, someone else's PR, no PR yet)."""


@dataclass(frozen=True)
class Handover:
    """The loop cap was hit and the PR goes to the user (rendered by #21)."""

    attempts: int
    reason: str


@dataclass(frozen=True)
class PublishResult:
    pr: PullRequest
    action: Action
    body: str  # the body this call rendered (what the PR holds unless closed/merged)
    note: str | None = None  # set when a person changed the PR (out of draft, closed, merged)


def pr_head_branch(ticket: Ticket) -> str:
    """The PR's head branch for ``ticket``: its first attempt's branch, ``fman/issue-<n>/a1``.

    The worker force-pushes every later attempt onto this branch.
    """
    return branch_name(ticket.issue_number, 1)


# --------------------------------------------------------------------------- rendering


def format_cost(cost_usd: float) -> str:
    """``$0.00`` for local (free) models, cents for normal costs, 4 places below a cent."""
    if cost_usd <= 0:
        return "$0.00"
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    return f"${cost_usd:.2f}"


def _text(value: object) -> str:
    """Free text made safe for the body: cannot open or close an HTML comment marker."""
    return str(value).replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _cell(value: object) -> str:
    return _text(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _code(value: object) -> str:
    return "`" + _cell(value).replace("`", "'") + "`"


def _what_changed(current: Attempt, diff: DiffStats | None) -> list[str]:
    out = ["## What changed", ""]
    if diff is not None:
        n = diff.diff_files
        out.append(f"{n} file{'s' if n != 1 else ''} changed, +{diff.insertions} -{diff.deletions}")
        if diff.files:
            out.append("")
            out.extend(f"- {_code(f)}" for f in diff.files[:MAX_FILES_LISTED])
            if len(diff.files) > MAX_FILES_LISTED:
                out.append(f"- ... and {len(diff.files) - MAX_FILES_LISTED} more")
    elif current.diff_files is not None:
        out.append(f"{current.diff_files} files changed, {current.diff_lines or 0} lines")
    else:
        out.append("No diff stats recorded.")
    return out


def _this_attempt(current: Attempt, ordinal: int, attempts: Sequence[Attempt]) -> list[str]:
    total = sum(a.cost_usd for a in attempts)
    return [
        "## This attempt",
        "",
        "| Attempt | Model | Tier | Trigger | Cost |",
        "| --- | --- | --- | --- | --- |",
        f"| {ordinal} of {len(attempts)} | {_code(current.model)} | {current.tier} "
        f"| {_TRIGGERS.get(current.trigger, _cell(current.trigger))} "
        f"| {format_cost(current.cost_usd)} |",
        "",
        f"Total spend on this ticket: {format_cost(total)} over {len(attempts)} "
        f"attempt{'s' if len(attempts) != 1 else ''}.",
    ]


def _verify(current: Attempt, verify: VerifyResult | None) -> list[str]:
    out = ["## Verify", ""]
    if verify is not None:
        status = "Timed out" if verify.timed_out else ("Passed" if verify.passed else "Failed")
        if verify.tests_run is not None:
            counts = f"{verify.tests_run} tests run, {verify.tests_failed or 0} failed"
        else:
            counts = "test counts not recognised"
        out.append(f"**{status}**: {counts} ({_code(verify.command)}, {verify.seconds:.1f} s)")
    elif current.tests_passed is not None:
        out.append(f"**{'Passed' if current.tests_passed else 'Failed'}** (counts not recorded)")
    else:
        out.append("Not run.")
    if current.build_passed is not None:
        out.append(f"Build: {'passed' if current.build_passed else 'failed'}.")
    return out


def _ci_and_sandbox(current: Attempt, sandbox: str | None) -> list[str]:
    """#41's CI line (once CI decided for this attempt) and #40's sandbox line (loud
    when the sandbox is off). Nothing for either when unknown, so older bodies are
    unchanged."""
    out: list[str] = []
    status = current.ci_status
    if status in ("passed", "failed"):
        out.append(f"GitHub CI: {status}.")
    elif status in CI_STATUS_NOTES:
        out.append(f"GitHub CI: {_text(CI_STATUS_NOTES[status])}.")
    if sandbox:
        out.append(f"**{_text(sandbox)}**" if "OFF" in sandbox else _text(sandbox))
    return out


def _review(review: Review | None) -> list[str]:
    out = [REVIEW_START, "## AI review", ""]
    if review is None:
        out.append("_No AI review yet._")
    else:
        verdict = _VERDICTS.get(review.verdict, _cell(review.verdict))
        out.append(f"**{verdict}** by {_code(review.reviewer_model)}")
        out.append("")
        yes_no = {True: "yes", False: "no", None: "unknown"}
        cov = review.changed_line_coverage
        out.append(f"- Tests pass: {yes_no[review.tests_passed]}")
        out.append(f"- Changed-line coverage: {'unknown' if cov is None else f'{cov:.0f}%'}")
        out.append(f"- New tests fail without the change: {yes_no[review.tests_nonvacuous]}")
        out.append(f"- Ran the change: {_cell(review.ran_app or 'unknown')}")
        if review.tag:
            out.append(f"- Tag: {_cell(review.tag)}")
        out.append(f"- Review cost: {format_cost(review.cost_usd)}")
        if review.note:
            note = review.note.strip()
            if len(note) > MAX_NOTE_CHARS:
                note = note[:MAX_NOTE_CHARS] + " ..."
            out.append("")
            out.extend("> " + _text(line) for line in note.splitlines())
    out.append(REVIEW_END)
    return out


def _handover(handover: Handover | None) -> list[str]:
    if handover is None:
        return [HANDOVER_START, HANDOVER_END]
    n = handover.attempts
    return [
        HANDOVER_START,
        "## Handed to you",
        "",
        f"Handed to you after {n} attempt{'s' if n != 1 else ''}: {_text(handover.reason)}",
        HANDOVER_END,
    ]


def _history(attempts: Sequence[Attempt], current: Attempt) -> list[str]:
    out = [
        "## Attempt history",
        "",
        "| # | Model | Tier | Trigger | Outcome | Cost |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, a in enumerate(attempts, 1):
        if a.outcome is not None:
            outcome = a.outcome.replace("_", " ")
        else:
            outcome = "this attempt" if a.id == current.id else "pending"
        out.append(
            f"| {i} | {_code(a.model)} | {a.tier} | {_TRIGGERS.get(a.trigger, _cell(a.trigger))} "
            f"| {outcome} | {format_cost(a.cost_usd)} |"
        )
    return out


def render_body(
    ticket: Ticket,
    attempts: Sequence[Attempt],
    current: Attempt,
    *,
    verify: VerifyResult | None = None,
    diff: DiffStats | None = None,
    review: Review | None = None,
    handover: Handover | None = None,
    sandbox: str | None = None,
) -> str:
    """The full PR body. Pure: same inputs, same text.

    ``attempts`` is every attempt of the ticket in order (``Store.list_attempts``) and must
    contain ``current``. ``review`` (the AI verdict) and ``handover`` are #21's inputs; their
    sections sit between ``REVIEW_START``/``REVIEW_END`` and ``HANDOVER_START``/``HANDOVER_END``.
    ``sandbox`` is ``runner.sandbox.describe(...)`` (#40); the attempt's ``ci_status``
    gives the CI line (#41). Both go at the end of the Verify section.
    """
    ids = [a.id for a in attempts]
    if current.id not in ids:
        raise ValueError(f"attempt {current.id} is not in the ticket's attempts {ids}")
    if any(a.ticket_id != ticket.id for a in attempts):
        raise ValueError(f"attempts must all belong to ticket {ticket.id}")
    ordinal = ids.index(current.id) + 1
    lines = [
        BODY_MARKER.format(ticket_id=ticket.id),
        f"Closes #{ticket.issue_number}",
        "",
        *_handover(handover),
        "",
        *_what_changed(current, diff),
        "",
        *_this_attempt(current, ordinal, attempts),
        "",
        *_verify(current, verify),
        *_ci_and_sandbox(current, sandbox),
        "",
        *_review(review),
        "",
        *_history(attempts, current),
        "",
        "---",
        "_Opened by foreman-inator. Model reasoning and traces stay on the worker's machine._",
    ]
    return "\n".join(lines) + "\n"


def body_ticket_id(body: str) -> int | None:
    """The ticket id in a body this module rendered, or None."""
    m = _MARKER_RE.search(body or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- publishing


class Publisher:
    """Opens and updates a ticket's draft PR as the App's bot. Never merges or pushes."""

    def __init__(self, gh: GitHubAPI, store: Store, *, sandbox_line: str | None = None) -> None:
        self.gh = gh
        self.store = store
        #: #40: the sandbox line every body shows (``runner.sandbox.describe``).
        self.sandbox_line = sandbox_line

    def open_or_update_draft(
        self,
        ticket: Ticket,
        attempt: Attempt,
        *,
        verify: VerifyResult | None = None,
        diff: DiffStats | None = None,
        review: Review | None = None,
        handover: Handover | None = None,
        head_branch: str | None = None,
    ) -> PublishResult:
        """Create the ticket's draft PR, or bring its body up to date. Idempotent.

        The branch must already be pushed. Safe after a crash between "PR created" and
        "pr_number saved": an existing PR for the head branch is adopted, not duplicated.
        Never marks the PR ready (invariant 7a).
        """
        ticket = self.store.get_ticket(ticket.id)
        attempt = self.store.get_attempt(attempt.id)
        if attempt.ticket_id != ticket.id:
            raise PublishError(f"attempt {attempt.id} belongs to ticket {attempt.ticket_id}")
        head = head_branch or pr_head_branch(ticket)
        if not head.startswith(BRANCH_PREFIX):
            raise PublishError(f"head branch {head!r} is not a worker branch")
        attempts = self.store.list_attempts(ticket.id)
        body = render_body(
            ticket,
            attempts,
            attempt,
            verify=verify,
            diff=diff,
            review=review,
            handover=handover,
            sandbox=self.sandbox_line,
        )

        pr = self._existing_pr(ticket, head)
        if pr is None:
            pr = self.gh.create_draft_pr(
                ticket.repo, head=head, base=ticket.base_branch, title=ticket.title, body=body
            )
            self._save_pr_number(ticket, pr)
            return PublishResult(pr, "created", body)

        self._save_pr_number(ticket, pr)
        if pr.merged:
            return PublishResult(pr, "merged", body, "PR was merged; left untouched")
        if pr.state != "open":
            return PublishResult(pr, "closed", body, "PR was closed by a person; left closed")
        action: Action = "unchanged"
        if pr.body != body:
            pr = self.gh.update_pr(ticket.repo, pr.number, body=body)
            action = "updated"
        note = None if pr.draft else "PR is no longer a draft (changed by a person); left as is"
        return PublishResult(pr, action, body, note)

    def mark_ready(self, ticket: Ticket, *, reason: ReadyReason) -> PullRequest:
        """Mark the ticket's PR ready for review. Worker only, never called from this module.

        Allowed reasons (invariant 7a): ``reviewer_approved`` (an AI approve) and
        ``handover`` (the ticket was handed to the user).
        """
        if reason not in get_args(ReadyReason):
            raise PublishError(f"not a reason to mark a PR ready: {reason!r}")
        ticket = self.store.get_ticket(ticket.id)
        if ticket.pr_number is None:
            raise PublishError(f"ticket {ticket.id} has no PR")
        pr = self.gh.get_pr(ticket.repo, ticket.pr_number)
        if pr.state != "open":
            raise PublishError(f"PR #{pr.number} is {pr.state}; not marking it ready")
        if not pr.draft:
            return pr
        return self.gh.mark_ready_for_review(ticket.repo, pr.number)

    def post_ai_review(
        self, ticket: Ticket, review_id: int, body: str, *, commit_id: str | None = None
    ) -> tuple[bool, GitHubReview | None]:
        """Post the AI review as the bot's COMMENT review on the ticket's PR, once.

        The body gets a hidden marker with ``review_id``; when a bot review with that
        marker already exists (a crash after posting), nothing is posted. Returns
        ``(posted, github_review)``; a closed or merged PR gets nothing."""
        ticket = self.store.get_ticket(ticket.id)
        if ticket.pr_number is None:
            raise PublishError(f"ticket {ticket.id} has no PR to review")
        marker = AI_REVIEW_MARKER.format(review_id=int(review_id))
        bot = self.gh.bot_login()
        for r in self.gh.list_reviews(ticket.repo, ticket.pr_number):
            if r.author == bot and marker in (r.body or ""):
                return False, r
        pr = self.gh.get_pr(ticket.repo, ticket.pr_number)
        if pr.state != "open":
            return False, None
        posted = self.gh.post_review(
            ticket.repo, ticket.pr_number, f"{marker}\n{body}", commit_id=commit_id
        )
        return True, posted

    # -- internals

    def _existing_pr(self, ticket: Ticket, head: str) -> PullRequest | None:
        if ticket.pr_number is not None:
            pr = self.gh.get_pr(ticket.repo, ticket.pr_number)
            if pr.head_ref != head:
                raise PublishError(
                    f"ticket {ticket.id}'s PR #{pr.number} has head {pr.head_ref!r}, not {head!r}"
                )
            return pr
        pr = self.gh.find_pr_for_branch(ticket.repo, head)
        if pr is None:
            return None
        if pr.author != self.gh.bot_login():
            raise PublishError(f"PR #{pr.number} on {head!r} was opened by {pr.author!r}")
        owner = body_ticket_id(pr.body)
        if owner is not None and owner != ticket.id:
            if pr.state == "open":
                raise PublishError(f"open PR #{pr.number} belongs to ticket {owner}")
            return None  # an earlier ticket's closed PR for this issue: open a fresh one
        return pr

    def _save_pr_number(self, ticket: Ticket, pr: PullRequest) -> None:
        if ticket.pr_number != pr.number:
            self.store.update_ticket(ticket.id, pr_number=pr.number)
