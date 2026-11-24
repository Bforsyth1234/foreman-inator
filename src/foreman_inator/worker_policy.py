"""Worker loop v1's next-step policy (todo #14): what happens after an attempt ends.

Pure: store rows in, a decision out, no I/O. The worker calls ``next_step`` for the
lifecycle's ``NEXT_ATTEMPT`` action and again when it picks work from the queue, so a
restarted worker reaches the same decision from the same rows (crash safety).

v1 has one tier and no AI review, so the rules are SPEC's escalation rules with every
"move up a tier" replaced by "hand to the user":

* checks fail → retry once on the same tier with the failure output; a second failure in
  the same round flags the ticket (invariant 7e). A round is the attempts since the last
  user kickback. The retry keeps the trigger of the attempt it retries (runner decision).
* the user requests changes → a new attempt with the note (trigger ``kickback``), at most
  ``MAX_USER_KICKBACKS`` times; the next request for changes flags the ticket.
* an attempt cut short by a crash or a shutdown (outcome ``superseded``) is re-run with
  the same trigger, at most ``MAX_INTERRUPTED`` times per ticket, then the ticket is
  flagged, so a crash loop cannot run forever either.

Worst case per ticket: (1 + MAX_USER_KICKBACKS) rounds × MAX_CHECK_ATTEMPTS attempts,
plus MAX_INTERRUPTED re-runs = 3 × 2 + 3 = 9 attempts, then the user.

#16 (escalation) replaces this module's ``next_step`` with its own; #21 wires it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from foreman_inator.db.store import Attempt, Trigger

#: Attempts per round whose checks may fail before the ticket goes to the user (first try
#: plus one same-tier retry). Invariant 7e: no third attempt.
MAX_CHECK_ATTEMPTS = 2
#: User "request changes" reviews answered with a new attempt; the next one flags.
MAX_USER_KICKBACKS = 2
#: Attempts re-run after a crash or shutdown; the next interruption flags.
MAX_INTERRUPTED = 3
#: The only tier v1 runs.
V1_TIER = 1

FLAG_CHECKS_FAILED = "checks_failed"
FLAG_KICKBACK_CAP = "kickback_cap"
FLAG_INTERRUPTED = "interrupted"
FLAG_NO_VERIFY = "no_verify_command"
FLAG_PUBLISH_FAILED = "publish_failed"


@dataclass(frozen=True)
class Retry:
    """Start another attempt with this trigger on this tier."""

    trigger: Trigger
    tier: int
    reason: str


@dataclass(frozen=True)
class HandToUser:
    """Stop: flag the ticket for the user. ``reason`` starts with one of the FLAG_ names."""

    reason: str


Step = Retry | HandToUser


def next_step(attempts: Sequence[Attempt]) -> Step | None:
    """The next step for a ticket whose attempts (oldest first) are ``attempts``.

    None when there is nothing to start: the last attempt is still running, passed its
    checks and waits on publish or review, or was accepted.
    """
    if not attempts:
        return Retry("first", V1_TIER, "first attempt")
    last = attempts[-1]
    if last.ended_at is None or last.outcome in (None, "accepted"):
        return None
    if last.outcome == "superseded":
        interrupted = sum(a.outcome == "superseded" for a in attempts)
        if interrupted > MAX_INTERRUPTED:
            return HandToUser(f"{FLAG_INTERRUPTED}: {interrupted} attempts were cut short")
        return Retry(last.trigger, last.tier, "re-run after an interrupted attempt")
    if last.outcome == "kicked_back":
        kickbacks = sum(a.outcome == "kicked_back" for a in attempts)
        if kickbacks > MAX_USER_KICKBACKS:
            return HandToUser(
                f"{FLAG_KICKBACK_CAP}: changes were requested {kickbacks} times "
                f"(v1 answers at most {MAX_USER_KICKBACKS})"
            )
        return Retry("kickback", V1_TIER, f"user kickback {kickbacks}")
    # failed_checks
    failed = sum(a.outcome == "failed_checks" for a in current_round(attempts))
    if failed >= MAX_CHECK_ATTEMPTS:
        return HandToUser(f"{FLAG_CHECKS_FAILED}: the checks failed on {failed} attempts in a row")
    return Retry(last.trigger, last.tier, "same-tier retry after failed checks")


def current_round(attempts: Sequence[Attempt]) -> list[Attempt]:
    """Attempts after the last kicked-back one, without interrupted (superseded) ones."""
    start = 0
    for i, a in enumerate(attempts):
        if a.outcome == "kicked_back":
            start = i + 1
    return [a for a in attempts[start:] if a.outcome != "superseded"]


def published_attempt(attempts: Sequence[Attempt]) -> Attempt | None:
    """The attempt whose change is on the PR now: the latest one that passed its checks."""
    for a in reversed(attempts):
        if a.tests_passed and a.outcome != "superseded":
            return a
    return None


def start_from(attempts: Sequence[Attempt]) -> Attempt | None:
    """The attempt whose work the next attempt builds on (SPEC: "its change may still be
    in the worktree"): the latest finished, non-interrupted attempt."""
    for a in reversed(attempts):
        if a.ended_at is not None and a.outcome != "superseded":
            return a
    return None


__all__ = [
    "FLAG_CHECKS_FAILED",
    "FLAG_INTERRUPTED",
    "FLAG_KICKBACK_CAP",
    "FLAG_NO_VERIFY",
    "FLAG_PUBLISH_FAILED",
    "MAX_CHECK_ATTEMPTS",
    "MAX_INTERRUPTED",
    "MAX_USER_KICKBACKS",
    "V1_TIER",
    "HandToUser",
    "Retry",
    "Step",
    "current_round",
    "next_step",
    "published_attempt",
    "start_from",
]
