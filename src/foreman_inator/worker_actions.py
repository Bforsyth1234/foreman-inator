"""What each lifecycle ``Action`` does in worker loop v2 (todo #21).

``ActionHandlers`` is mixed into ``Worker``; ``Worker.actions`` maps every ``Action`` to
one ``_a_<name>`` method here. Each handler gets a ``Ctx`` (ticket, event, and for the PR
actions the attempt, verify result, diff, review). Handlers called while a poll event is
handled run inside that event's store transaction, so they do store work only; their
GitHub writes go through ``Worker._gh``, which defers them until the transaction commits.
Anything slow (routing's model call, attempts, reviews) is a queue job, not an action.

v2 wiring: ``ROUTE`` → the router (#15, as a ``route`` job; a ``tier:N`` label is applied
at once, with no model call); ``NEXT_ATTEMPT`` → escalation (#16, decided by the queue
from the stored rows); ``AI_REVIEW`` → the reviewer (#17, ``worker_review``);
``MARK_READY_FOR_REVIEW`` → only after an AI approve or a hand-over (7a); ``POST_SUMMARY``
→ the summary comment (#20); ``RELEASE_LOCKS`` → file locks (#19). v1's
``V1_READY_ON_CHECKS`` flag is gone.

Wave 6b: ``RECORD_CI`` → ``attempts.ci_status`` (#41; a red run's job log is saved for the
retry prompt and the attempt counts as failed checks); ``START_REBASE`` → a pending
rebase (#43, ``worker_rebase``); a user kickback stores ``ReviewSubmitted.kickback_note``,
the review body plus its line comments (#42).

Wave 9 (#29): a user kickback queues its failure-step label (#27) on the background lane
once the poll event's transaction has committed (``Worker.after_commit``).
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from foreman_inator import tracing
from foreman_inator.db.store import Attempt, Review, Ticket
from foreman_inator.github.app_auth import GitHubNotFound
from foreman_inator.locks import DORMANT_FLAGS, flag_kind, predict_files
from foreman_inator.pr.summary import post_summary
from foreman_inator.retry import FLAG_RETRY_ROUND, RETRYABLE_FLAGS
from foreman_inator.routing import Override
from foreman_inator.routing import escalation as esc
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.decompose import FLAG_SPLIT_PROPOSED
from foreman_inator.tickets.lifecycle import AIReviewed, ChecksPassed, HandedToUser
from foreman_inator.worker_policy import (
    FLAG_CHECKS_FAILED,
    FLAG_INTERRUPTED,
    FLAG_KICKBACK_CAP,
    FLAG_NO_VERIFY,
    FLAG_PUBLISH_FAILED,
    published_attempt,
)

log = logging.getLogger("foreman_inator.worker")

# Hand-over reasons that do not come from escalation (#16 has the others).
FLAG_REVIEWER_HANDOVER = "reviewer_handover"
FLAG_NO_REVIEWER = "no_reviewer"
FLAG_REVIEW_SKIPPED = "review_skipped"

NOTE_MARKER = "<!-- foreman-inator:note ticket={ticket_id} key={key} -->"


@dataclass
class Ctx:
    """What an action handler sees: the ticket, the event, and the publish inputs."""

    ticket: Ticket | None
    event: Any
    attempt: Attempt | None = None
    verify: Any = None
    diff: Any = None
    publish: Any = None
    review: Review | None = None
    cancel: threading.Event | None = None
    mark_ready: bool = False  # a hand-over of a change that passed its checks
    stop: bool = False  # set by a handler to skip the remaining actions


class ActionHandlers:
    """Action handlers; see the module docstring. Uses ``Worker``'s attributes."""

    store: Any
    api: Any
    config: Any
    publisher: Any
    locks: Any
    _lock: Any
    _running: Any
    _sweep: Any

    def _a_create_ticket(self, ctx: Ctx) -> None:
        ev = ctx.event
        base = self.api.get_default_branch(ev.repo)
        t = self.store.create_ticket(
            repo=ev.repo, issue_number=ev.issue_number, title=ev.title, request=ev.body,
            base_branch=base,
        )  # fmt: skip
        owned = predict_files(ev.title, ev.body)
        ctx.ticket = self.store.update_ticket(t.id, owned_files=list(owned)) if owned else t
        log.info(
            "ticket %d (%s#%d): created in todo (labeled by %s)",
            t.id,
            ev.repo,
            ev.issue_number,
            ev.labeled_by,
        )

    def _a_tier_override(self, ctx: Ctx) -> None:
        """A ``tier:N`` label by an allowed user. At creation it is the route (no model
        call). Later it re-routes only a ticket that has not started an attempt yet;
        after that escalation owns the tier, and the label is only noted."""
        assert ctx.ticket is not None
        override = Override.from_event(ctx.event)
        if override is None:
            return
        t = ctx.ticket
        if isinstance(ctx.event, lifecycle.TicketRequested) or not self.store.list_attempts(t.id):
            ctx.ticket = self._route_ticket(t, override=override)  # type: ignore[attr-defined]
            if flag_kind(t.flag_reason) == FLAG_SPLIT_PROPOSED:
                # #50: a tier:N label after a split proposal runs the issue as one ticket.
                ctx.ticket = self.store.clear_flag(t.id)
                log.info("ticket %d: %s after the split proposal; running as one", t.id,
                         override.describe())  # fmt: skip
            return
        note = f"{override.describe()} noted after work started; escalation keeps the tier"
        reason = f"{t.route_reason}; {note}" if t.route_reason else note
        ctx.ticket = self.store.update_ticket(t.id, route_reason=reason)
        log.info("ticket %d: %s", t.id, note)

    def _a_route(self, ctx: Ctx) -> None:
        """The router's model call is slow, so it runs as a ``route`` job, not here."""
        assert ctx.ticket is not None
        if ctx.ticket.routed_tier is None:
            log.info("ticket %d: queued for routing", ctx.ticket.id)

    def _a_start_attempt(self, ctx: Ctx) -> None:
        log.info("ticket %s: queued for its first attempt", _tid(ctx))

    def _a_update_request(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        ctx.ticket = self.store.update_ticket(ctx.ticket.id, title=ev.title, request=ev.body)
        files = predict_files(ev.title, ev.body)
        if files:
            r = self.locks.acquire(ctx.ticket, files)  # wanted only grows (#19)
            ctx.ticket = self.store.get_ticket(ctx.ticket.id)
            if not r.ok and not r.closed:
                log.info("ticket %d: the edited issue names files others hold", ctx.ticket.id)
        log.info("ticket %d: issue edited; the next attempt uses the new text", ctx.ticket.id)

    def _a_open_or_update_pr(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None and ctx.attempt is not None
        t = ctx.ticket
        with tracing.github_span("pr_publish", attempt_id=ctx.attempt.id) as s:
            res = self.publisher.open_or_update_draft(
                t, ctx.attempt, verify=ctx.verify, diff=ctx.diff
            )
            s.set_attribute("github.action", f"pr_{res.action}")
            s.set_attribute("github.pr_number", res.pr.number)
        ctx.publish = res
        ctx.ticket = self.store.get_ticket(t.id)
        log.info("ticket %d: PR #%d %s (draft until reviewed)", t.id, res.pr.number, res.action)
        if res.action in ("closed", "merged"):
            ctx.stop = True  # a person closed or merged it; the poller reports that next

    def _a_ai_review(self, ctx: Ctx) -> None:
        """Checks passed and the draft PR is up: the AI review (``worker_review``)."""
        assert ctx.ticket is not None and ctx.attempt is not None
        self.review_stage(ctx.ticket, ctx.attempt, ctx.cancel)  # type: ignore[attr-defined]

    def _a_mark_ready(self, ctx: Ctx) -> None:
        """Invariant 7a: only an AI approve or a hand-over marks the PR ready."""
        assert ctx.ticket is not None
        ev = ctx.event
        if isinstance(ev, AIReviewed) and ev.verdict == "approve":
            assert ctx.attempt is not None
            self._mark_ready(ctx.ticket, ctx.attempt, "reviewer_approved")  # type: ignore[attr-defined]
        elif isinstance(ev, AIReviewed) and ev.verdict == "handed_to_user":
            self.hand_over(  # type: ignore[attr-defined]
                ctx.ticket, reviewer_reason(ctx.review), ready=True, attempt=ctx.attempt
            )
        elif isinstance(ev, ChecksPassed) and ev.review_skip:
            assert ctx.attempt is not None
            if not self.ci_gate(ctx.ticket, ctx.attempt):  # type: ignore[attr-defined]
                # #41: the review job does the review:skip hand-over once CI decided.
                log.info("ticket %d: waiting on GitHub CI before handing over", ctx.ticket.id)
                return
            # #51: the repo profile's review = off, or the label (strict ignores it).
            reason = self.skip_reason(ctx.ticket) or (  # type: ignore[attr-defined]
                f"{FLAG_REVIEW_SKIPPED}: the review:skip label was applied by an allowed user"
            )
            self.hand_over(ctx.ticket, reason, ready=True, attempt=ctx.attempt)  # type: ignore[attr-defined]
        else:  # pragma: no cover - the lifecycle never asks for this
            raise RuntimeError(f"refusing to mark a PR ready on {type(ev).__name__} (7a)")

    def _a_record_kickback(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        if isinstance(ev, AIReviewed):
            self._record_ai_kickback(ctx)
            return
        att = published_attempt(self.store.list_attempts(ctx.ticket.id))
        if att is None:
            log.warning("ticket %d: kickback with no published attempt", ctx.ticket.id)
            return
        tag = ev.kickback_tag if ev.kickback_tag in lifecycle.KICKBACK_TAGS else None
        note = ev.kickback_note or None  # #42: the body plus the review's line comments
        kickback = self.store.record_kickback(att.id, by="user", tag=tag, note=note)
        self.store.set_attempt_outcome(att.id, "kicked_back")
        repo = ctx.ticket.repo
        # #27: label it in the background, only once this poll transaction committed.
        self.after_commit(  # type: ignore[attr-defined]
            ctx.ticket,
            "failure_label",
            lambda: self.queue_label(repo, kickback.id),  # type: ignore[attr-defined]
        )
        if ctx.ticket.flag_reason:
            # The user asked for changes on a handed-over PR: that is new work, not a loop.
            ctx.ticket = self.store.clear_flag(ctx.ticket.id)
        with tracing.github_span("review_received", attempt_id=att.id, pr_number=ev.pr_number) as s:
            s.set_attribute("review.verdict", "request_changes")
            tracing.add_kickback(s, attempt_id=att.id, by="user", tag=tag or "none", note=note)
        log.info("ticket %d: changes requested on attempt %d (tag %s)", ctx.ticket.id, att.id, tag)

    def _record_ai_kickback(self, ctx: Ctx) -> None:
        """The reviewer's kickback row was written with its review row (``record_outcome``),
        in one transaction with the move to ``in_progress``; here only its trace event."""
        assert ctx.ticket is not None and ctx.attempt is not None
        att = self.store.get_attempt(ctx.attempt.id)
        if att.outcome != "kicked_back":
            self.store.set_attempt_outcome(att.id, "kicked_back")
        review = ctx.review
        with tracing.github_span(
            "ai_kickback", attempt_id=att.id, pr_number=ctx.ticket.pr_number
        ) as s:
            s.set_attribute("review.verdict", "request_changes")
            tracing.add_kickback(
                s,
                attempt_id=att.id,
                by="reviewer",
                tag=(review.tag if review else None) or "none",
                note=review.note if review else None,
            )
        log.info("ticket %d: the AI reviewer sent attempt %d back", ctx.ticket.id, att.id)

    def _a_record_ci(self, ctx: Ctx) -> None:
        """``RECORD_CI`` (#41, poll transaction, store only). A red run (``failed``) saves
        its job log tail where the retry prompt reads failed-check output and stores the
        attempt as ``failed_checks``; ``NEXT_ATTEMPT`` (escalation) follows."""
        assert ctx.ticket is not None
        ev = ctx.event
        att = self.store.get_attempt(ev.attempt_id)
        if att.ci_status is not None or att.ticket_id != ctx.ticket.id:
            return
        if ev.state == "failed":
            path = self.traces_dir / str(ctx.ticket.id) / f"attempt-{att.id}-ci.log"  # type: ignore[attr-defined]
            path.parent.mkdir(parents=True, exist_ok=True)
            text = ev.failure_output or "GitHub CI failed: " + ", ".join(ev.failed_jobs)
            path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
            self.store.set_ci_result(att.id, "failed", failure_output_path=str(path))
        else:
            self.store.set_ci_result(att.id, ev.state)
        log.info(
            "ticket %d: GitHub CI on attempt %d: %s%s",
            ctx.ticket.id,
            att.id,
            ev.state.replace("_", " "),
            f" ({', '.join(ev.failed_jobs)})" if ev.failed_jobs else "",
        )

    def _a_clear_kickback_label(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        repo, pr, label = ev.repo, ev.pr_number, ev.kickback_label

        def remove() -> None:
            with contextlib.suppress(GitHubNotFound):
                self.api.remove_label(repo, pr, label)

        self._gh(ctx.ticket, "kickback_label_cleared", remove, pr_number=pr)  # type: ignore[attr-defined]

    def _a_next_attempt(self, ctx: Ctx) -> None:
        """Escalation (#16) decides from the stored rows when the queue picks the ticket,
        so a crash here loses nothing; this only logs the decision."""
        assert ctx.ticket is not None
        t = self.store.get_ticket(ctx.ticket.id)
        if self.rebase_pending(t):  # type: ignore[attr-defined]
            log.info("ticket %d: next, another rebase attempt or the rebase cap", t.id)
            return
        step = self.policy_for(t)  # type: ignore[attr-defined]
        if isinstance(step, esc.HandToUser):
            log.info("ticket %d: next, hand to the user (%s)", ctx.ticket.id, step.flag)
        elif step is not None:
            log.info("ticket %d: next attempt queued (%s)", ctx.ticket.id, step.reason)

    def _a_record_approval(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        with tracing.github_span("review_received", pr_number=ev.pr_number) as s:
            s.set_attribute("review.verdict", "approve")
        log.info("ticket %d: PR #%d approved by %s", ctx.ticket.id, ev.pr_number, ev.reviewer)

    def _a_record_accept(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        att = published_attempt(self.store.list_attempts(ctx.ticket.id))
        with tracing.github_span("pr_merged", pr_number=getattr(ctx.event, "pr_number", None)):
            if att is not None:
                self.store.set_accepted_attempt(ctx.ticket.id, att.id)
        log.info("ticket %d: merged; accepted attempt %s", ctx.ticket.id, att.id if att else None)

    def _a_close_issue(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        t = ctx.ticket

        def close() -> None:
            if self.api.get_issue(t.repo, t.issue_number).state == "open":
                self.api.close_issue(t.repo, t.issue_number)
                log.info("ticket %d: closed issue #%d", t.id, t.issue_number)

        self._gh(t, "issue_closed", close)  # type: ignore[attr-defined]

    def _a_post_summary(self, ctx: Ctx) -> None:
        """The one-line summary comment (#20); idempotent by its hidden marker."""
        assert ctx.ticket is not None
        t = ctx.ticket

        def post() -> None:
            res = post_summary(
                self.api,
                self.store,
                t.id,
                attempt_budget_usd=getattr(self.config, "attempt_budget_usd", None),
            )
            if res.posted:
                log.info("ticket %d: summary comment posted", t.id)

        self._gh(t, "summary_posted", post)  # type: ignore[attr-defined]

    def _a_cancel_attempt(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        with self._lock:
            run = self._running.get(ctx.ticket.id)
            if run is not None:
                run.cancel.set()
                log.info("ticket %d: cancelling its running job", ctx.ticket.id)

    def _a_release_locks(self, ctx: Ctx) -> None:
        if ctx.ticket is not None and self.locks.release(ctx.ticket):
            log.info("ticket %d: file locks released", ctx.ticket.id)

    def _a_cleanup_worktree(self, ctx: Ctx) -> None:
        if ctx.ticket is not None:
            self._sweep.add(ctx.ticket.repo)  # done when the repo has no running job

    def _a_flag_ticket(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        if isinstance(ev, AIReviewed):
            reason = reviewer_reason(ctx.review)
        else:
            reason = getattr(ev, "reason", "flagged")
        self.hand_over(  # type: ignore[attr-defined]
            ctx.ticket, reason, ready=ctx.mark_ready, attempt=ctx.attempt
        )
        ctx.ticket = self.store.get_ticket(ctx.ticket.id)

    # ------------------------------------------------------------------ helpers

    def post_once(self, ticket: Ticket, key: str, body: str, *, name: str) -> None:
        """Post ``body`` on the ticket's issue unless the bot already posted it (by a
        hidden ``key`` marker), so a crash-and-redo never posts twice."""
        marker = NOTE_MARKER.format(ticket_id=ticket.id, key=key)

        def post() -> None:
            bot = self.api.bot_login()
            for c in self.api.list_issue_comments(ticket.repo, ticket.issue_number):
                if c.author == bot and marker in (c.body or ""):
                    return
            self.api.post_comment(ticket.repo, ticket.issue_number, f"{marker}\n{body}")

        self._gh(ticket, name, post)  # type: ignore[attr-defined]


def _safe_text(text: str) -> str:
    """Text for a GitHub comment: no @mentions, no HTML comment markers."""
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return re.sub(r"@(?=[A-Za-z0-9])", "@\u200b", text)


def _tid(ctx: Ctx) -> str:
    return str(ctx.ticket.id) if ctx.ticket is not None else "?"


def reviewer_reason(review: Review | None) -> str:
    """The hand-over reason for an AI review that ended ``handed_to_user``."""
    note = " ".join(((review.note if review else None) or "no verdict").split())
    return f"{FLAG_REVIEWER_HANDOVER}: the AI reviewer handed the change to you ({note[:300]})"


# ====================================================================== flag comments

_FLAG_TEXT = {
    FLAG_CHECKS_FAILED: (
        "The checks kept failing on every model up to the top of the escalation ladder, so "
        "foreman-inator handed this issue to you. The check output is in the worker's "
        "local trace."
    ),
    FLAG_KICKBACK_CAP: (
        "Changes were requested more times than the escalation ladder answers, so "
        "foreman-inator handed this issue to you."
    ),
    esc.FLAG_KICKBACK_WRONG: (
        "The change was sent back as wrong on the last model of the escalation ladder, so "
        "foreman-inator handed this issue to you."
    ),
    esc.FLAG_AI_KICKBACK_CAP: (
        "The AI reviewer sent the change back twice on the last model of the escalation "
        "ladder, so the change goes to you with the reviewer's notes."
    ),
    FLAG_INTERRUPTED: (
        "Attempts on this issue were interrupted too many times (worker crashes or "
        "restarts), so foreman-inator stopped working on it."
    ),
    FLAG_PUBLISH_FAILED: "foreman-inator could not open or update the pull request.",
    esc.FLAG_ATTEMPT_BUDGET: (
        "An attempt reached its per-attempt spend or token budget (`attempt_budget_usd`), "
        "so foreman-inator stopped and handed this issue to you instead of retrying."
    ),
    FLAG_REVIEWER_HANDOVER: (
        "The AI reviewer could not reach a verdict, so the change goes to you without one."
    ),
    FLAG_NO_REVIEWER: (
        "No configured reviewer is from a different model family than the writer "
        "(invariant: the reviewer is independent), so the change goes to you without an "
        "AI review."
    ),
    FLAG_REVIEW_SKIPPED: (
        "The `review:skip` label is set, so the change goes to you without an AI review."
    ),
    "sandbox_unavailable": (
        "The sandbox for model-written code could not start on the worker's Mac, so "
        "foreman-inator did not run anything for this issue (it fails closed). Fix the "
        'sandbox, or set `sandbox = "off"` in config.toml only if you accept running '
        "model-written code unsandboxed."
    ),
    "setup_failed": (
        "foreman-inator could not start work on this issue: a setup step failed before "
        "any model was asked to write code (the coding agent's server or session could "
        "not start, the worktree could not be prepared, or the repository's dependencies "
        "could not be installed). No model can fix that, so no other model was tried "
        "and no model was charged. Fix the cause on the worker's Mac; the worker's log "
        "has the details."
    ),
    FLAG_RETRY_ROUND: (
        "The extra round a manual retry (`fman retry`) gives is used up, so "
        "foreman-inator handed this issue to you again."
    ),
    lifecycle.FLAG_REBASE_CAP: (
        "The pull request still conflicts with its base branch after the rebase attempts "
        "foreman-inator makes, so the conflict goes to you."
    ),
    "rebase_failed": (
        "foreman-inator could not replay the change onto the moved base branch, so the "
        "conflict goes to you."
    ),
}


#: #51: the hand-over comment when the repo profile turns the AI review off.
REVIEW_OFF_TEXT = (
    "This repository's profile turns the AI review off (`fman repo set-profile`), so the "
    "change goes to you without one."
)


def flag_comment(ticket: Ticket, reason: str, *, pr_number: int | None = None) -> str:
    """The issue comment for a hand-over: what happened, why, and what to do next.

    Counts, names and the reason only: no code, check output or prompts. ``pr_number``:
    the PR that was marked ready with it."""
    kind, _, detail = reason.partition(":")
    marker = f"<!-- foreman-inator:flag ticket={ticket.id} -->"
    if kind == FLAG_NO_VERIFY:
        return (
            f"{marker}\nforeman-inator found no command to run this repository's checks "
            "(no Makefile `test`/`check` target, no package.json `test` script, no Python "
            "tests, no Package.swift), so work on this issue has not started.\n\n"
            "Tell it the command on the worker's Mac, for example:\n\n"
            f'    fman repo set-verify {ticket.repo} "make test"\n\n'
            "Work starts on the next poll after that."
        )
    text = _FLAG_TEXT.get(kind, "foreman-inator stopped working on this issue.")
    if kind == FLAG_REVIEW_SKIPPED and "profile" in detail:  # #51: review = off
        text = REVIEW_OFF_TEXT
    lines = [marker, text]
    if detail.strip():
        lines += ["", f"Why: {_safe_text(' '.join(detail.split()))[:600]}"]
    if pr_number is not None:
        lines += [
            "",
            f"Pull request #{pr_number} is marked ready for your review. Approve and merge "
            "it, or request changes (with a `kickback:<tag>` label) to send it back.",
        ]
    else:
        lines += [
            "",
            "To start over, remove the `agent` label and add it again. Nothing else will "
            "run on this issue until then.",
        ]
        if kind in RETRYABLE_FLAGS:  # #79: or retry it, keeping its history
            lines.append(
                "Once the cause is fixed, you can also retry it on the worker's Mac, "
                f"keeping its attempts and branch: `fman retry {ticket.repo}#{ticket.issue_number}`"
                " (add `--up` to move up a tier)."
            )
        if ticket.pr_number is None and flag_kind(reason) in DORMANT_FLAGS:
            lines.append(  # #55: a dormant ticket's file locks are released
                "While it waits, its file locks are released so other issues can change the "
                "same files; it takes them again when it starts over."
            )
    return "\n".join(lines)


__all__ = [
    "FLAG_NO_REVIEWER",
    "FLAG_REVIEWER_HANDOVER",
    "FLAG_REVIEW_SKIPPED",
    "ActionHandlers",
    "Ctx",
    "HandedToUser",
    "flag_comment",
    "reviewer_reason",
]
