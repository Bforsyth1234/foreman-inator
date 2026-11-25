"""Publishing an attempt that passed its checks (todo #21; v1 was #14).

``PublishSteps`` is mixed into ``Worker``. ``publish_job_for``: force-push the attempt's
branch to the PR head (``pr_head_branch``; a ``rebase`` attempt (#43) lands there the same
way), open or update the draft PR (a new PR under the repo's open-PR lock, #77, with the
cap checked again inside it), move to ``testing``, then the next gate in the same
job: the CI gate (#41) and the AI review, or the ``review:skip`` hand-over, which waits
at the same CI gate. Re-running it after a crash is harmless: the push is the same commit
and the publisher adopts the open PR.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from foreman_inator import tracing
from foreman_inator.db.store import Attempt
from foreman_inator.pr.publisher import PublishError, pr_head_branch
from foreman_inator.runner.worktree import WorktreeError
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import ChecksPassed
from foreman_inator.worker_actions import Ctx
from foreman_inator.worker_policy import FLAG_PUBLISH_FAILED

if TYPE_CHECKING:
    from foreman_inator.worker_queue import Job

log = logging.getLogger("foreman_inator.worker")


class PublishSteps:
    # Provided by Worker.
    store: Any

    def publish_job(self, job: Job, cancel: threading.Event) -> None:
        """Publish an attempt that passed its checks but has not been published (after a
        crash, an error, or a wait on file locks)."""
        ticket = self.store.get_ticket(job.ticket_id)
        att = self.store.list_attempts(ticket.id)[-1]
        merged = self.blocker_merged(ticket, att)  # type: ignore[attr-defined]
        if merged is not None:
            self.set_aside(  # type: ignore[attr-defined]
                ticket, att, f"ticket #{merged} merged changes to its files; starting over"
            )
            return
        self.publish_job_for(job.ticket_id, att, cancel=cancel)

    def publish_job_for(
        self,
        ticket_id: int,
        att: Attempt,
        *,
        verify: Any = None,
        diff: Any = None,
        cancel: threading.Event | None = None,
    ) -> None:
        ticket = self.store.get_ticket(ticket_id)
        if ticket.column != "in_progress":
            log.info("ticket %d: no longer in progress; not publishing", ticket.id)
            return
        if cancel is not None and cancel.is_set():
            return
        if not self.open_pr_gate(ticket):  # type: ignore[attr-defined]
            return  # #77: at the open-PR cap; the queue's publish job retries it later
        mirror = self._mirror(ticket.repo)  # type: ignore[attr-defined]
        ordinal = [a.id for a in self.store.list_attempts(ticket.id)].index(att.id) + 1
        try:
            wt = mirror.open(ticket.issue_number, ordinal)
        except WorktreeError:
            log.warning("ticket %d: attempt %d's worktree is gone; re-running", ticket.id, att.id)
            self.store.set_attempt_outcome(att.id, "superseded")
            return
        head = pr_head_branch(ticket)
        with (
            self._trace(ticket),  # type: ignore[attr-defined]
            tracing.github_span("branch_pushed", attempt_id=att.id),
        ):
            wt.push(self._token(ticket.repo), remote_branch=head, force=True)  # type: ignore[attr-defined]
        event = ChecksPassed(ticket.id, review_skip=self.review_skipped(ticket))
        plan = lifecycle.apply(ticket.column, event)
        ctx = Ctx(ticket, event, attempt=att, verify=verify, diff=diff or wt.diff_stats())
        ctx.cancel = cancel
        try:
            if ticket.pr_number is None:
                # #77: the gate above is a cheap early out; this check and the PR's
                # creation are atomic per repo, so concurrent publishes at cap - 1 open
                # one PR, not two (the other waits; its publish job retries it later).
                with self.pr_open_lock(ticket.repo):  # type: ignore[attr-defined]
                    ctx.ticket = ticket = self.store.get_ticket(ticket.id)
                    if not self.open_pr_gate(ticket):  # type: ignore[attr-defined]
                        return
                    self._run_actions(ctx, plan.actions[:1])  # type: ignore[attr-defined]
            else:
                self._run_actions(ctx, plan.actions[:1])  # type: ignore[attr-defined]
        except PublishError as exc:
            self._event(  # type: ignore[attr-defined]
                ticket.id, lifecycle.HandedToUser(ticket.id, f"{FLAG_PUBLISH_FAILED}: {exc}")
            )
            return
        if ctx.stop:  # a person closed or merged the PR: wait for the poller to say so
            self.hold_repo(ticket.repo)  # type: ignore[attr-defined]
            return
        # The draft PR is open: now the ticket is in testing (the PR awaits review).
        if self._commit(ticket.id, event) is None:  # type: ignore[attr-defined]
            return
        ctx.ticket = self.store.get_ticket(ticket.id)
        self._run_actions(ctx, plan.actions[1:])  # type: ignore[attr-defined]  # review / skip


__all__ = ["PublishSteps"]
