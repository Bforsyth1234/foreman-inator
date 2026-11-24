"""Base step types, flag names and attempt helpers shared by the worker and escalation.

Worker loop v1 (todo #14) decided the next attempt here with its own one-tier
``next_step``. #21 replaced that with the escalation state machine (#16,
``routing.escalation.next_step``), whose ``Retry``/``Escalate``/``HandToUser`` subclass the
two classes below, so this module keeps only what both still share: the step base
classes, the caps escalation reuses, the flag names, and three pure helpers over a
ticket's attempt rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from foreman_inator.db.store import Attempt, Trigger

#: Attempts per segment whose checks may fail before moving up (first try plus one
#: same-tier retry). Escalation (#16) reuses it.
MAX_CHECK_ATTEMPTS = 2
#: User "request changes" reviews answered on one rung before moving up (#16 reuses it).
MAX_USER_KICKBACKS = 2
#: Attempts re-run after a crash or shutdown; the next interruption hands over.
MAX_INTERRUPTED = 3

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
    "HandToUser",
    "Retry",
    "Step",
    "published_attempt",
    "start_from",
]
