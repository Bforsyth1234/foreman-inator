"""The AI review stage, its GitHub side, and hand-overs (todo #21).

``ReviewSteps`` is mixed into ``Worker``. The stage sits between "checks passed + draft PR
pushed" (``testing``) and "PR marked ready for your review":

1. ``ci_gate`` — the seam where #41 waits for GitHub CI (no behaviour yet).
2. ``review:skip`` by an allowed user → hand-over (the PR is marked ready, no review).
3. ``pick_reviewer`` (#17): a reviewer of another model family than the writer (7f);
   none → hand-over.
4. ``_launch_review`` → ``review_change`` inside the ticket's trace: one ``review`` span
   with five ``review.check`` children. Nothing is stored while it runs.
5. Cut short (shutdown) → nothing is stored; the review runs again. A cloud call refused
   at the spend cap → the verdict is thrown away and the ticket pauses (``worker_pause``).
6. One transaction: the ``reviews`` row (+ a ``kickbacks`` row ``by="reviewer"`` and the
   attempt's outcome ``kicked_back`` for request_changes) and ``AIReviewed`` (request
   changes moves the ticket back to ``in_progress``; escalation picks the next step).
7. ``finalize_review``: post the verdict as the bot's COMMENT review (once, by marker),
   then the lifecycle's ``AIReviewed`` actions: approve → PR body + mark ready;
   handed_to_user → hand-over; request_changes → the kickback's trace event.

``finalize_review`` is idempotent and is run again for every open ticket's latest review
after a restart (``Worker.recover`` empties the finalized set), which is what makes a
crash between the review row and the comment, or between the comment and marking ready,
come out right with no second comment.

Hand-over (``hand_over``), always in this order: the PR body's hand-over section, the
issue comment saying why (once, by marker), marking the PR ready when the change passed
its checks, and last the ticket's flag. The flag is the "done" mark: a crash before it
redoes the hand-over, and every step before it is idempotent.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from foreman_inator.db.store import Attempt, Review, Ticket
from foreman_inator.github.client import IssueEvent
from foreman_inator.github.poller import REVIEW_SKIP_LABEL
from foreman_inator.pr.publisher import Handover, PublishError, pr_head_branch
from foreman_inator.review import reviewer as rv
from foreman_inator.runner.worktree import WorktreeError
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import AIReviewed
from foreman_inator.worker_actions import FLAG_NO_REVIEWER, FLAG_REVIEW_SKIPPED, Ctx, flag_comment
from foreman_inator.worker_policy import published_attempt

if TYPE_CHECKING:
    from foreman_inator.worker_queue import Job

log = logging.getLogger("foreman_inator.worker")


class ReviewSteps:
    # Provided by Worker.
    store: Any
    config: Any
    api: Any
    proxy: Any
    publisher: Any
    recordings_dir: Any
    review_timeout: float | None
    _finalized: set[int]

    # ------------------------------------------------------------------ seams

    def ci_gate(self, ticket: Ticket, attempt: Attempt) -> bool:
        """Seam for #41: between "local checks passed + draft PR pushed" and the AI review.
        Return False to wait (the review job is derived again on the next pass). No
        behaviour yet: always go on to the review."""
        return True

    def _launch_review(self, **kw: Any) -> rv.ReviewOutcome:
        """Process launch point 3 of 3 (#40's sandbox): the reviewer's checks (tests,
        coverage, revert, run-app) run inside ``review_change``."""
        return rv.review_change(**kw)

    # ------------------------------------------------------------------ jobs

    def review_job(self, job: Job, cancel: threading.Event) -> None:
        """Review the PR's attempt (after a crash, a pause, or a wait at the CI gate)."""
        t = self.store.get_ticket(job.ticket_id)
        if t.column != "testing" or t.flag_reason:
            return
        att = self.store.list_attempts(t.id)[-1]
        if not att.tests_passed or att.outcome is not None:
            return
        if self.store.list_reviews(attempt_id=att.id, shadow=False):
            return
        if self.review_skipped(t):
            self.hand_over(t, _SKIP_REASON, ready=True, attempt=att)
            return
        self.review_stage(t, att, cancel)

    def finalize_job(self, job: Job, cancel: threading.Event) -> None:
        t = self.store.get_ticket(job.ticket_id)
        reviews = self.store.list_reviews(ticket_id=t.id, shadow=False)
        if reviews and reviews[-1].id not in self._finalized:
            self.finalize_review(t, reviews[-1])

    # ------------------------------------------------------------------ the stage

    def review_stage(self, t: Ticket, att: Attempt, cancel: threading.Event | None = None) -> None:
        if not self.ci_gate(t, att):
            log.info("ticket %d: waiting at the CI gate before the AI review", t.id)
            return
        writer = self._endpoint_named(att)
        try:
            reviewer = rv.pick_reviewer(self.config, writer)
        except rv.ReviewError as exc:
            self.hand_over(t, f"{FLAG_NO_REVIEWER}: {exc}", ready=True, attempt=att)
            return
        mirror = self._mirror(t.repo)  # type: ignore[attr-defined]
        head = self._head_sha(mirror, t, att)
        settings = self.store.get_repo_settings(t.repo)
        log.info("ticket %d: AI review of attempt %d by %s", t.id, att.id, reviewer.name)
        with self._trace(t):  # type: ignore[attr-defined]
            outcome = self._launch_review(
                issue=rv.IssueText(t.issue_number, t.title, t.request),
                change=rv.ChangeRef(mirror, att.base_commit or "", head),
                writer=writer,
                reviewer=reviewer,
                proxy=self.proxy,
                attempt_id=att.id,
                ticket_id=t.id,
                store=None,  # stored below, unless the review was cut short or refused
                verify_override=settings.verify_command if settings else None,
                recordings_dir=self.recordings_dir,
                timeout=self.review_timeout or rv.DEFAULT_TIMEOUT_S,
                cancel=cancel,
            )
        if (cancel is not None and cancel.is_set()) or self.stopping:  # type: ignore[attr-defined]
            log.info("ticket %d: AI review cut short; it runs again", t.id)
            return
        refusal = self.spend_refusal(att.id)  # type: ignore[attr-defined]
        if refusal is not None:
            self.pause_for_spend(t, refusal)  # type: ignore[attr-defined]
            return
        with self.store.transaction():
            outcome = rv.record_outcome(self.store, att.id, outcome)
            if outcome.verdict == "request_changes":
                self.store.set_attempt_outcome(att.id, "kicked_back")
            self._commit(t.id, AIReviewed(t.id, outcome.verdict))  # type: ignore[arg-type, attr-defined]
        assert outcome.review is not None
        log.info(
            "ticket %d: AI review %d of attempt %d: %s",
            t.id,
            outcome.review.id,
            att.id,
            outcome.verdict,
        )
        self.finalize_review(self.store.get_ticket(t.id), outcome.review, outcome.comment_body)

    def finalize_review(self, t: Ticket, review: Review, body: str | None = None) -> None:
        """The GitHub side of a stored review; safe to run any number of times."""
        att = self.store.get_attempt(review.attempt_id)
        text = body or comment_from_row(review)
        if t.pr_number is not None:
            self._gh(  # type: ignore[attr-defined]
                t,
                "ai_review_posted",
                lambda: self.publisher.post_ai_review(t, review.id, text),
                attempt_id=att.id,
                pr_number=t.pr_number,
            )
        counted = self.history(t).attempts  # type: ignore[attr-defined]
        att = self.store.get_attempt(att.id)
        cur = self.store.get_ticket(t.id)
        # Effects only while this review still decides the ticket: request_changes's are
        # idempotent; approve and hand-over only while nobody judged the change since.
        live = review.verdict == "request_changes" or (
            att.outcome is None and cur.column == "testing"
        )
        if counted and counted[-1].id == att.id and live:
            ev = AIReviewed(t.id, review.verdict)  # type: ignore[arg-type]
            plan = lifecycle.apply("testing", ev)  # the column the review was made in
            ctx = Ctx(self.store.get_ticket(t.id), ev, attempt=att, review=review)
            if review.verdict == "approve":
                self._update_body(ctx.ticket, att)  # type: ignore[arg-type]
            self._run_actions(ctx, plan.actions)  # type: ignore[attr-defined]
        self._finalized.add(review.id)

    # ------------------------------------------------------------------ ready + hand-over

    def _mark_ready(self, t: Ticket, att: Attempt, reason: str, *, handover: bool = False) -> None:
        """The only caller of ``Publisher.mark_ready`` (invariant 7a)."""
        t = self.store.get_ticket(t.id)
        if reason == "reviewer_approved":
            att = self.store.get_attempt(att.id)
            approved = any(
                r.verdict == "approve"
                for r in self.store.list_reviews(attempt_id=att.id, shadow=False)
            )
            current = published_attempt(self.history(t).attempts)  # type: ignore[attr-defined]
            if not (approved and t.column == "testing" and att.outcome is None
                    and current is not None and current.id == att.id):  # fmt: skip
                log.warning("ticket %d: not marking the PR ready: no current approval", t.id)
                return
        elif not (reason == "handover" and handover):
            raise RuntimeError(f"refusing to mark a PR ready for {reason!r} (7a)")
        if t.pr_number is None:
            return

        def ready() -> Any:
            try:
                return self.publisher.mark_ready(t, reason=reason)
            except PublishError as exc:
                log.warning("ticket %d: PR not marked ready: %s", t.id, exc)
                return None

        pr = self._gh(t, "pr_ready", ready, attempt_id=att.id, pr_number=t.pr_number)  # type: ignore[attr-defined]
        if pr is not None:
            log.info("ticket %d: PR #%d ready for your review (%s)", t.id, pr.number, reason)

    def hand_over(
        self, t: Ticket, reason: str, *, ready: bool, attempt: Attempt | None = None
    ) -> None:
        """Hand the ticket to the user and say why (invariant 7e). See the module docstring
        for the order; the flag is stored last, so a stored flag means "done"."""
        t = self.store.get_ticket(t.id)
        if t.flag_reason == reason:
            return
        att = attempt or published_attempt(self.history(t).attempts)  # type: ignore[attr-defined]
        ready = bool(ready and t.pr_number is not None and att is not None and att.tests_passed)
        n = len(self.store.list_attempts(t.id))
        if ready:
            assert att is not None
            self._update_body(t, att, handover=Handover(n, reason.partition(":")[2].strip()))
        kind = reason.split(":", 1)[0]
        self.post_once(  # type: ignore[attr-defined]
            t,
            f"handover-a{n}-{kind}",
            flag_comment(t, reason, pr_number=t.pr_number if ready else None),
            name="issue_flagged",
        )
        if ready:
            assert att is not None
            self._mark_ready(t, att, "handover", handover=True)
        self.store.flag_ticket(t.id, reason)
        log.info("ticket %d: handed to the user (%s)", t.id, kind)

    # ------------------------------------------------------------------ helpers

    def review_skipped(self, ticket: Ticket) -> bool:
        """Is the ``review:skip`` label on the issue, applied by an allowed user (7b)?
        Read from GitHub when it matters, so a label added after the ticket was created
        counts too. (#51's ``review = off|strict`` profiles decide here as well.)"""
        issue = self.api.get_issue(ticket.repo, ticket.issue_number)
        if REVIEW_SKIP_LABEL not in {lb.lower() for lb in issue.labels}:
            return False
        events = self.api.list_issue_events(ticket.repo, ticket.issue_number)
        ev = _applied(events, REVIEW_SKIP_LABEL)
        bot = self.api.bot_login()
        return (
            ev is not None
            and ev.actor is not None
            and ev.actor != bot
            and self.config.is_allowed_user(ev.actor)
        )

    def _update_body(self, t: Ticket, att: Attempt, *, handover: Handover | None = None) -> None:
        """Re-render the PR body: the AI review section from the stored row, and the
        hand-over section when handing over."""
        if t.pr_number is None:
            return
        reviews = self.store.list_reviews(attempt_id=att.id, shadow=False)
        review = reviews[-1] if reviews else None
        self._gh(  # type: ignore[attr-defined]
            t,
            "pr_updated",
            lambda: self.publisher.open_or_update_draft(t, att, review=review, handover=handover),
            attempt_id=att.id,
            pr_number=t.pr_number,
        )

    def _endpoint_named(self, att: Attempt) -> Any:
        try:
            return self.config.model(att.model)
        except Exception:
            return self.config.models_for_tier(att.tier)[0]

    def _head_sha(self, mirror: Any, t: Ticket, att: Attempt) -> str:
        """The attempt's commit: its local branch in the mirror, else the PR head."""
        for ref in (f"refs/heads/{att.branch}", f"refs/remotes/origin/{pr_head_branch(t)}"):
            try:
                return mirror.resolve(ref)
            except WorktreeError:
                continue
        mirror.ensure(self._token(t.repo))  # type: ignore[attr-defined]
        return mirror.resolve(f"refs/remotes/origin/{pr_head_branch(t)}")


def _applied(events: Iterable[IssueEvent], label: str) -> IssueEvent | None:
    """The ``labeled`` event that applied ``label`` (if it is still on)."""
    current: IssueEvent | None = None
    for ev in sorted(events, key=lambda e: (e.created_at, e.id)):
        if ev.label is None or ev.label.lower() != label:
            continue
        current = ev if ev.event == "labeled" else None
    return current


_SKIP_REASON = f"{FLAG_REVIEW_SKIPPED}: the review:skip label was applied by an allowed user"

_YES_NO = {True: "passed", False: "failed", None: "unknown"}


def comment_from_row(review: Review) -> str:
    """The COMMENT review body rebuilt from a stored row (after a crash lost the
    reviewer's own text): the verdict, tag, note and the four stored check results."""
    cov = review.changed_line_coverage
    nonvac = {True: "the new tests fail without the change", False: "vacuous", None: "unknown"}
    rows = [
        ("1. Tests and build", _YES_NO[review.tests_passed]),
        ("2. Changed-line coverage", "unknown" if cov is None else f"{cov:.0f}%"),
        ("3. Revert check", nonvac[review.tests_nonvacuous]),
        ("4. Run it", review.ran_app or "unknown"),
        ("5. Read the diff", "stored verdict"),
    ]
    return rv.render_comment(
        verdict=review.verdict,
        tag=review.tag,
        note=review.note or "",
        reviewer_model=review.reviewer_model,
        variant_id=review.variant_id or "",
        check_lines=rows,
    )


__all__ = ["ReviewSteps", "comment_from_row"]
