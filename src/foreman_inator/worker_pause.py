"""Pauses (todo #21): a spend cap (#18) or a file lock (#19) stops a ticket for a while.

A pause is never a failed attempt. It never counts toward escalation (#16) or the
interrupted cap, and the ticket resumes on its own: when the cap resets, when the user
runs ``fman approve-spend <ticket>``, or when the lock holder releases.

**Spend.** Before a cloud writer attempt starts, ``Spend.blocked`` is asked; at the cap
the ticket is paused before anything runs (no attempt row). A call refused in the middle
of an attempt or of an AI review (``Spend.refusal_for_attempt``) pauses the ticket after
it: the attempt is stored ``superseded`` and marked *uncounted*, or the review's verdict
is thrown away (no review row, so the review runs again after the pause). A paused ticket
is flagged ``spend_cap: ...``; the issue gets ``Spend.approval_comment`` (once per pause,
by a hidden marker); ``resume_paused`` clears the flag on each pass once
``Spend.resume_reason`` allows it.

**Locks.** A ticket whose wanted files are held by another ticket waits in the queue
(``worker_queue``). An attempt whose diff touched a held file (``Locks.widen``) is not
published: its row stays "checks passed, no outcome" and the queue retries the publish
once the lock frees. Its blockers are remembered; when one of them merged meanwhile, the
paused attempt is superseded (uncounted) and the ticket starts fresh on the new base,
same trigger. A ticket made to yield its locks to break a deadlock is treated the same.

**Durable marks without a schema change.** Uncounted attempt ids and lock waits are kept
in the store's ``poll_cursors`` key/value table under ``worker:``-prefixed names
(``worker:uncounted:<ticket>``, ``worker:lockwait:<attempt>``), so a restart sees them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from foreman_inator.db.store import OPEN_COLUMNS, Attempt, Ticket
from foreman_inator.spend import FLAG_SPEND_CAP, Refusal

log = logging.getLogger("foreman_inator.worker")


def _uncounted_key(ticket_id: int) -> str:
    return f"worker:uncounted:{ticket_id}"


def _lockwait_key(attempt_id: int) -> str:
    return f"worker:lockwait:{attempt_id}"


class PauseSteps:
    # Provided by Worker.
    store: Any
    spend: Any
    api: Any

    # ------------------------------------------------------------------ uncounted attempts

    def uncounted_attempts(self, ticket: Ticket) -> frozenset[int]:
        """Attempts that escalation must not see (spend pauses, lock restarts)."""
        raw = self.store.get_cursor(ticket.repo, _uncounted_key(ticket.id))
        return frozenset(int(i) for i in json.loads(raw)) if raw else frozenset()

    def set_aside(self, ticket: Ticket, attempt: Attempt, why: str) -> None:
        """Store ``attempt`` as superseded and keep it out of every count."""
        with self.store.transaction():
            ids = sorted(self.uncounted_attempts(ticket) | {attempt.id})
            self.store.set_cursor(ticket.repo, json.dumps(ids), _uncounted_key(ticket.id))
            if self.store.get_attempt(attempt.id).ended_at is None:
                self.store.finish_attempt(attempt.id, "superseded")
            else:
                self.store.set_attempt_outcome(attempt.id, "superseded")
        log.info("ticket %d: attempt %d set aside (%s); not counted", ticket.id, attempt.id, why)

    # ------------------------------------------------------------------ lock waits

    def lock_wait(self, ticket: Ticket, attempt: Attempt) -> tuple[int, ...]:
        raw = self.store.get_cursor(ticket.repo, _lockwait_key(attempt.id))
        return tuple(json.loads(raw)) if raw else ()

    def set_lock_wait(self, ticket: Ticket, attempt: Attempt, blockers: tuple[int, ...]) -> None:
        self.store.set_cursor(ticket.repo, json.dumps(list(blockers)), _lockwait_key(attempt.id))

    def blocker_merged(self, ticket: Ticket, attempt: Attempt) -> int | None:
        """A ticket that blocked this attempt's publish and has merged since, or None."""
        for tid in self.lock_wait(ticket, attempt):
            if self.store.get_ticket(int(tid)).column == "done":
                return int(tid)
        return None

    # ------------------------------------------------------------------ spend

    def pause_for_spend(self, ticket: Ticket, refusal: Refusal) -> None:
        """Pause ``ticket`` at a spend cap: ask for approval on the issue, then flag it."""
        approvals = self.store.query(
            "SELECT COUNT(*) AS n FROM spend_approvals WHERE ticket_id = ?", (ticket.id,)
        )[0]["n"]
        n = len(self.store.list_attempts(ticket.id))
        key = f"spend-a{n}-p{approvals}"
        body = self.spend.approval_comment(ticket, refusal)
        self.post_once(ticket, key, body, name="spend_paused")  # type: ignore[attr-defined]
        self.store.flag_ticket(ticket.id, refusal.flag_reason)
        self.spend.clear_refusals(ticket.id)
        log.info(
            "ticket %d: paused at the %s spend cap; waits for `fman approve-spend %d`",
            ticket.id,
            refusal.cap,
            ticket.id,
        )

    def spend_refusal(self, attempt_id: int) -> Refusal | None:
        """A live cap refusal of a call made for this attempt (writer or its review)."""
        r = self.spend.refusal_for_attempt(attempt_id)
        return r if r is not None and r.needs_approval else None

    def resume_paused(self, repo: str) -> list[int]:
        """Unflag the repo's tickets paused at a spend cap that may run again."""
        resumed: list[int] = []
        for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=repo):
            if not (t.flag_reason or "").startswith(FLAG_SPEND_CAP):
                continue
            why = self.spend.resume_reason(t)
            if why is None:
                continue
            self.store.clear_flag(t.id)
            self.spend.clear_refusals(t.id)
            resumed.append(t.id)
            log.info("ticket %d: spend pause over (%s); resuming", t.id, why.replace("_", " "))
        return resumed


__all__ = ["PauseSteps"]
