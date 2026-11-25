"""Keeping a PR mergeable when its base branch moves (todo #43, wired in wave 6b).

``RebaseSteps`` is mixed into ``Worker``.

Flow
----
1. The poller (``Poller(conflicts=True)``) sees ``mergeable is False`` on the PR of a
   ticket in ``testing`` and emits ``BaseConflict`` once per PR head. Under the cap
   (``lifecycle.REBASE_CAP`` = 2 rebase attempts per ticket) it moves the ticket to
   ``in_progress`` with ``START_REBASE``; at the cap it flags the ticket (``rebase_cap``,
   the PR is not marked ready).
2. ``START_REBASE`` (inside the poll transaction, store only): the pending mark
   ``worker:rebase:<ticket>`` = the id of the attempt now on the PR, and any running job
   of the ticket (an AI review) is cancelled; a review that finishes anyway re-checks the
   column and stores nothing (``worker_review``).
3. The queue gives the ticket a ``rebase`` job while a rebase is pending: the mark names
   the ticket's latest attempt (no rebase attempt has started since), or the latest
   attempt is a rebase attempt that failed its checks or was cut short. A rebase attempt
   stopped at its per-attempt budget (#58, ``end_reason`` budget) is not retried: it stays
   visible to escalation (``lifecycle.rebase_uncounted``), which hands the ticket to the
   user with ``attempt_budget`` as for any attempt.
4. ``rebase_job``: under the cap, replay the PR head (``origin/<pr branch>``) onto the moved
   base (``RepoMirror.replay_onto_base``), store an attempt with trigger ``rebase`` on the
   model and tier that wrote the change, and run it like any attempt with the conflicted
   files in the writer prompt (``rebase`` section). Leftover ``<<<<<<<``/``>>>>>>>`` lines
   in those files count as failed checks. Passed → force-push onto the ticket's own
   ``fman/`` PR branch (7a) → ``testing`` → CI gate (#41) → AI review, as for any attempt.
   Failed → back to step 3 (another rebase attempt, or the ``rebase_cap`` flag). A replay
   git itself cannot do (not a conflict) flags ``rebase_failed``.

Counting (decision 3): a rebase is not a kickback and not escalation's business.
``Worker.history`` leaves out ``lifecycle.rebase_uncounted`` (failed or interrupted
rebase attempts, and the attempt each rebase replaced on the PR), so a passing rebase
attempt simply continues the writer's stint; ``metrics.queries`` hides failed rebase
attempts from the model metrics. The replaced attempt keeps its row as it was (outcome
NULL): it is still the change on the PR until a rebase attempt passes, so a merge in the
meantime accepts it.

Crash safety: killed with a rebase pending but not started → the mark is durable, the job
is derived again. Killed during the rebase attempt → ``recover`` stores it ``superseded``;
it is still pending (step 3) and re-runs, and every rebase attempt counts toward the cap
whatever its outcome, so a crash loop is bounded too.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from foreman_inator.config import ConfigError
from foreman_inator.db.store import Attempt, Ticket
from foreman_inator.pr.publisher import pr_head_branch
from foreman_inator.runner import verify as verify_mod
from foreman_inator.runner.worktree import WorktreeError
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import FLAG_REBASE_CAP, AttemptStarted
from foreman_inator.worker_actions import Ctx
from foreman_inator.worker_policy import FLAG_NO_VERIFY, published_attempt

if TYPE_CHECKING:
    from foreman_inator.worker_queue import Job

log = logging.getLogger("foreman_inator.worker")

FLAG_REBASE_FAILED = "rebase_failed"


def _rebase_key(ticket_id: int) -> str:
    return f"worker:rebase:{ticket_id}"


class RebaseSteps:
    # Provided by Worker.
    store: Any
    config: Any
    _lock: Any
    _running: Any

    # ------------------------------------------------------------------ state

    def rebase_pending(self, t: Ticket, attempts: list[Attempt] | None = None) -> bool:
        """Does ``t`` need a (another) rebase attempt? See the module docstring, step 3."""
        attempts = self.store.list_attempts(t.id) if attempts is None else attempts
        if not attempts:
            return False
        last = attempts[-1]
        if last.trigger == "rebase":
            if getattr(last, "end_reason", None) == "budget":
                return False  # #58: stopped at its budget; escalation hands it over
            return last.ended_at is not None and last.outcome in ("failed_checks", "superseded")
        return self.store.get_cursor(t.repo, _rebase_key(t.id)) == str(last.id)

    def _a_start_rebase(self, ctx: Ctx) -> None:
        """``START_REBASE`` (poll transaction, store only): mark the rebase pending."""
        assert ctx.ticket is not None
        t = ctx.ticket
        attempts = self.store.list_attempts(t.id)
        if not attempts:
            return
        self.store.set_cursor(t.repo, str(attempts[-1].id), _rebase_key(t.id))
        with self._lock:
            run = self._running.get(t.id)
            if run is not None:
                run.cancel.set()  # its AI review would judge the conflicting change
        log.info(
            "ticket %d: PR #%s conflicts with %s; rebase attempt %d of %d queued",
            t.id,
            t.pr_number,
            t.base_branch,
            lifecycle.count_rebase_attempts(attempts) + 1,
            lifecycle.REBASE_CAP,
        )

    # ------------------------------------------------------------------ the job

    def rebase_job(self, job: Job, cancel: threading.Event) -> None:
        t = self.store.get_ticket(job.ticket_id)
        if t.column != "in_progress" or t.flag_reason:
            return
        attempts = self.store.list_attempts(t.id)
        if not self.rebase_pending(t, attempts):
            return
        if not lifecycle.rebase_allowed(attempts):
            done = lifecycle.count_rebase_attempts(attempts)
            self._event(  # type: ignore[attr-defined]
                t.id,
                lifecycle.HandedToUser(
                    t.id,
                    f"{FLAG_REBASE_CAP}: PR #{t.pr_number} still conflicts with its base "
                    f"branch after {done} rebase attempts",
                ),
            )
            return
        src = published_attempt(attempts)
        if src is None:  # pragma: no cover - a PR always has a passing attempt
            log.warning("ticket %d: rebase with no published attempt", t.id)
            return
        try:
            endpoint = self.config.model(src.model)
        except ConfigError:
            endpoint = self.config.models_for_tier(src.tier)[0]
        if endpoint.is_cloud:
            refusal = self.spend.blocked(t.id)  # type: ignore[attr-defined]
            if refusal is not None:
                self.pause_for_spend(t, refusal)  # type: ignore[attr-defined]
                return
        from foreman_inator.worker_attempt import _remove_old_worktrees, _write_excludes

        mirror = self._mirror(t.repo)  # type: ignore[attr-defined]
        token = self._token(t.repo)  # type: ignore[attr-defined]
        mirror.ensure(token)
        _write_excludes(mirror)
        ordinal = len(attempts) + 1
        try:
            rp = mirror.replay_onto_base(
                t.base_branch,
                t.issue_number,
                ordinal,
                previous=f"refs/remotes/origin/{pr_head_branch(t)}",
                token=token,
                fetch=False,
            )
        except WorktreeError as exc:
            self._event(  # type: ignore[attr-defined]
                t.id, lifecycle.HandedToUser(t.id, f"{FLAG_REBASE_FAILED}: {exc}")
            )
            return
        wt = rp.worktree
        _remove_old_worktrees(mirror, t, ordinal)
        settings = self.store.get_repo_settings(t.repo)
        cmd = verify_mod.detect(wt.path, override=settings.verify_command if settings else None)
        if cmd is None:
            wt.remove()
            self._event(  # type: ignore[attr-defined]
                t.id, lifecycle.HandedToUser(t.id, f"{FLAG_NO_VERIFY}: no verify command found")
            )
            return
        if self._sandbox_refused(t, wt):  # type: ignore[attr-defined]
            return
        with self.store.transaction():
            if self._commit(t.id, AttemptStarted(t.id, trigger="rebase")) is None:  # type: ignore[attr-defined]
                wt.remove()
                return
            att = self.store.add_attempt(
                t.id,
                model=endpoint.name,
                tier=endpoint.tier,
                trigger="rebase",
                worktree=str(wt.path),
                branch=wt.branch,
                base_commit=wt.base_sha,
            )
        log.info(
            "ticket %d: rebase attempt %d started (%s, tier %d): %d conflicted file(s)",
            t.id,
            att.id,
            endpoint.name,
            endpoint.tier,
            len(rp.conflicted),
        )
        self._run_and_finish(  # type: ignore[attr-defined]
            self.store.get_ticket(t.id), att, wt, endpoint, cmd, cancel, conflicted=rp.conflicted
        )


__all__ = ["FLAG_REBASE_FAILED", "RebaseSteps"]
