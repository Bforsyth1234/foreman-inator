"""What each lifecycle ``Action`` does in worker loop v1 (todo #14).

``ActionHandlers`` is mixed into ``Worker``; ``Worker.actions`` maps every ``Action`` to
one ``_a_<name>`` method here. Each handler gets a ``Ctx`` (ticket, event, and for the
PR actions the attempt, verify result and diff). Handlers called while a poll event is
handled run inside that event's store transaction; their GitHub writes go through
``Worker._gh``, which defers them until the transaction commits.

Seams for #21: replace ``_a_route`` (#15 router), ``_a_next_attempt`` (#16 escalation),
``_a_ai_review`` (#17 reviewer; v1 marks the PR ready instead, behind
``V1_READY_ON_CHECKS``), ``_a_post_summary`` (#20) and ``_a_release_locks`` (#19).
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from foreman_inator import tracing
from foreman_inator.db.store import Attempt, Ticket
from foreman_inator.github.app_auth import GitHubNotFound
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import HandedToUser
from foreman_inator.worker_policy import (
    FLAG_CHECKS_FAILED,
    FLAG_INTERRUPTED,
    FLAG_KICKBACK_CAP,
    FLAG_NO_VERIFY,
    FLAG_PUBLISH_FAILED,
    V1_TIER,
    HandToUser,
    Retry,
    published_attempt,
)

log = logging.getLogger("foreman_inator.worker")

#: v1 only: a PR is marked ready as soon as its checks pass (no AI review yet). #21 removes
#: this flag and lets only a reviewer approval or a handover mark a PR ready (7a).
V1_READY_ON_CHECKS = True


@dataclass
class Ctx:
    """What an action handler sees: the ticket, the event, and the publish inputs."""

    ticket: Ticket | None
    event: Any
    attempt: Attempt | None = None
    verify: Any = None
    diff: Any = None
    publish: Any = None
    stop: bool = False  # set by a handler to skip the remaining actions


class ActionHandlers:
    """Action handlers; see the module docstring. Uses ``Worker``'s attributes."""

    store: Any
    api: Any
    publisher: Any
    policy: Any
    _lock: Any
    _running: Any
    _sweep: Any

    def _a_create_ticket(self, ctx: Ctx) -> None:
        ev = ctx.event
        base = self.api.get_default_branch(ev.repo)
        ctx.ticket = self.store.create_ticket(
            repo=ev.repo, issue_number=ev.issue_number, title=ev.title, request=ev.body,
            base_branch=base,
        )  # fmt: skip
        log.info(
            "ticket %d (%s#%d): created in todo (labeled by %s)",
            ctx.ticket.id,
            ev.repo,
            ev.issue_number,
            ev.labeled_by,
        )

    def _a_tier_override(self, ctx: Ctx) -> None:
        ev = ctx.event
        tier = getattr(ev, "tier_override", None) or getattr(ev, "tier", None)
        if isinstance(ev, lifecycle.TierOverride) and ctx.ticket is not None:
            reason = f"v1: tier {V1_TIER} only (tier:{tier} label noted, not applied)"
            ctx.ticket = self.store.update_ticket(ctx.ticket.id, route_reason=reason)
        log.info("ticket %s: tier:%s label noted; v1 runs tier %d only", _tid(ctx), tier, V1_TIER)

    def _a_route(self, ctx: Ctx) -> None:
        """v1 router: tier 1 always. #15 replaces this action."""
        assert ctx.ticket is not None
        override = getattr(ctx.event, "tier_override", None)
        reason = f"v1: tier {V1_TIER} only"
        if override is not None:
            reason += f" (tier:{override} label noted, not applied)"
        with tracing.route_span(V1_TIER, reason):
            ctx.ticket = self.store.update_ticket(
                ctx.ticket.id, routed_tier=V1_TIER, route_reason=reason
            )

    def _a_start_attempt(self, ctx: Ctx) -> None:
        log.info("ticket %s: queued for its first attempt", _tid(ctx))

    def _a_update_request(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        ctx.ticket = self.store.update_ticket(ctx.ticket.id, title=ev.title, request=ev.body)
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
        log.info("ticket %d: PR #%d %s", t.id, res.pr.number, res.action)
        if res.action in ("closed", "merged"):
            ctx.stop = True  # a person closed or merged it; the poller reports that next

    def _a_ai_review(self, ctx: Ctx) -> None:
        if not V1_READY_ON_CHECKS:  # pragma: no cover - #21 replaces this action
            raise NotImplementedError("AI review is #17/#21")
        self._mark_ready(ctx, "v1_checks_passed")

    def _a_mark_ready(self, ctx: Ctx) -> None:
        self._mark_ready(ctx, "v1_checks_passed")

    def _mark_ready(self, ctx: Ctx, reason: str) -> None:
        assert ctx.ticket is not None
        t = ctx.ticket
        pr = self._gh(
            t,
            "pr_ready",
            lambda: self.publisher.mark_ready(t, reason=reason),  # type: ignore[arg-type]
            attempt_id=ctx.attempt.id if ctx.attempt else None,
            pr_number=t.pr_number,
        )
        if pr is not None:
            log.info("ticket %d: PR #%d ready for review (%s)", t.id, pr.number, reason)

    def _a_record_kickback(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        att = published_attempt(self.store.list_attempts(ctx.ticket.id))
        if att is None:
            log.warning("ticket %d: kickback with no published attempt", ctx.ticket.id)
            return
        tag = ev.kickback_tag if ev.kickback_tag in lifecycle.KICKBACK_TAGS else None
        self.store.record_kickback(att.id, by="user", tag=tag, note=ev.comment or None)
        self.store.set_attempt_outcome(att.id, "kicked_back")
        with tracing.github_span("review_received", attempt_id=att.id, pr_number=ev.pr_number) as s:
            s.set_attribute("review.verdict", "request_changes")
            tracing.add_kickback(
                s, attempt_id=att.id, by="user", tag=tag or "none", note=ev.comment
            )
        log.info("ticket %d: changes requested on attempt %d (tag %s)", ctx.ticket.id, att.id, tag)

    def _a_clear_kickback_label(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        ev = ctx.event
        repo, pr, label = ev.repo, ev.pr_number, ev.kickback_label

        def remove() -> None:
            with contextlib.suppress(GitHubNotFound):
                self.api.remove_label(repo, pr, label)

        self._gh(ctx.ticket, "kickback_label_cleared", remove, pr_number=pr)

    def _a_next_attempt(self, ctx: Ctx) -> None:
        """v1: same tier or hand to the user (``policy``). #21: escalation (#16)."""
        assert ctx.ticket is not None
        step = self.policy(self.store.list_attempts(ctx.ticket.id))
        if isinstance(step, HandToUser):
            self._event(ctx.ticket.id, HandedToUser(ctx.ticket.id, step.reason))
        elif isinstance(step, Retry):
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

        self._gh(t, "issue_closed", close)

    def _a_post_summary(self, ctx: Ctx) -> None:
        """No summary comment in v1; #20 (``pr/summary.py``) fills this action."""

    def _a_cancel_attempt(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        with self._lock:
            for run in self._running.values():
                if run.job.ticket_id == ctx.ticket.id:
                    run.cancel.set()
                    log.info("ticket %d: cancelling its running job", ctx.ticket.id)

    def _a_release_locks(self, ctx: Ctx) -> None:
        if ctx.ticket is not None:
            self.store.release_locks(ctx.ticket.id)

    def _a_cleanup_worktree(self, ctx: Ctx) -> None:
        if ctx.ticket is not None:
            self._sweep.add(ctx.ticket.repo)  # done when the repo has no running job

    def _a_flag_ticket(self, ctx: Ctx) -> None:
        assert ctx.ticket is not None
        t = ctx.ticket
        reason = getattr(ctx.event, "reason", "flagged")
        ctx.ticket = self.store.flag_ticket(t.id, reason)
        log.info("ticket %d: flagged for the user (%s)", t.id, reason.split(":", 1)[0])
        body = flag_comment(t, reason)
        self._gh(t, "issue_flagged", lambda: self.api.post_comment(t.repo, t.issue_number, body))


def _tid(ctx: Ctx) -> str:
    return str(ctx.ticket.id) if ctx.ticket is not None else "?"


# ====================================================================== flag comments

_FLAG_TEXT = {
    FLAG_CHECKS_FAILED: (
        "The checks failed on two attempts in a row, so foreman-inator stopped working on "
        "this issue. The check output is in the worker's local trace."
    ),
    FLAG_KICKBACK_CAP: (
        "Changes were requested more times than foreman-inator v1 answers, so it stopped "
        "working on this issue. The pull request stays open for you."
    ),
    FLAG_INTERRUPTED: (
        "Attempts on this issue were interrupted too many times (worker crashes or "
        "restarts), so foreman-inator stopped working on it."
    ),
    FLAG_PUBLISH_FAILED: "foreman-inator could not open or update the pull request.",
}


def flag_comment(ticket: Ticket, reason: str) -> str:
    """The issue comment for a flag. Counts and names only: no code, output or prompts."""
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
    if kind == FLAG_PUBLISH_FAILED and detail.strip():
        text += f" ({detail.strip()})"
    return (
        f"{marker}\n{text}\n\nTo start over, remove the `agent` label and add it again. "
        "Nothing else will run on this issue until then."
    )


__all__ = ["V1_READY_ON_CHECKS", "ActionHandlers", "Ctx", "flag_comment"]
