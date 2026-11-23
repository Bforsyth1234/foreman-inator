"""Exploration: start about 10% of tickets one tier lower than the router picked (todo #23).

SPEC "Routing and escalation": "on about 10% of tickets, start one tier lower than the
router picked, so the data covers cheaper models too. These attempts are marked
``explore`` and never count against a model in the metrics if they fail."

Split of work: this module only *decides* whether a ticket explores. Escalation (#16)
picks the explore rung (the rung group one configured tier below the routed one) when
``History(explore=True)``, marks the attempt ``explore``, and after a failed explore
attempt jumps straight to the routed rung. Metrics (#20) hide failed explore attempts.

``should_explore`` is pure and deterministic per ticket: the draw is
``sha256(salt + ":" + ticket_id)`` read as a number in [0, 1), and the ticket explores
when the draw is below ``rate``. No process randomness, so a restarted worker reaches the
same answer for the same ticket with the same inputs. It never explores:

* when the rate is 0 (#51's ``fast`` profile),
* for a ``tier:N`` override (the user chose the tier),
* when no configured tier is below the tier the ticket would start on (e.g. the routed
  tier is already the lowest configured tier, or ``routed_tier`` is None, which
  escalation reads as the lowest rung). So an explore start is never below tier 1.

The rate is an argument (default 0.10) so #51 can pass its per-profile rate (thrifty
0.25, normal 0.10, fast 0). Because a profile or the config can change while a ticket is
open, the worker must persist the decision when it routes the ticket and read the stored
value afterwards: ``poll_cursors`` row ``(ticket.repo, explore_cursor(ticket.id))`` with
value ``encode(decision)`` (``"1"`` / ``"0"``), written in the same transaction that
stores ``routed_tier``. ``Worker.explore(ticket)`` returns ``decode(stored)``, and False
when there is no row (tickets routed before #23).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable

from foreman_inator.config import Config, ModelEndpoint

DEFAULT_RATE = 0.10
DEFAULT_SALT = "foreman-inator/explore-v1"
CURSOR_PREFIX = "explore:"


def explore_draw(ticket_id: int, salt: str = DEFAULT_SALT) -> float:
    """The ticket's fixed draw in [0, 1)."""
    digest = hashlib.sha256(f"{salt}:{int(ticket_id)}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


def _tiers(tiers: Config | Iterable[int] | Iterable[ModelEndpoint]) -> frozenset[int]:
    if isinstance(tiers, Config):
        return frozenset(m.tier for m in tiers.models)
    out = set()
    for t in tiers:
        out.add(t.tier if isinstance(t, ModelEndpoint) else int(t))
    return frozenset(out)


def explore_tier(
    routed_tier: int | None, tiers: Config | Iterable[int] | Iterable[ModelEndpoint]
) -> int | None:
    """The tier an explore start would run on: the highest configured tier below the tier
    the ticket starts on (the routed tier, or the next configured one up, as escalation
    resolves it). None when there is no such tier."""
    have = sorted(_tiers(tiers))
    if routed_tier is None or not have:
        return None
    start = next((t for t in have if t >= routed_tier), have[-1])
    lower = [t for t in have if t < start]
    return lower[-1] if lower else None


def should_explore(
    ticket_id: int,
    rate: float = DEFAULT_RATE,
    *,
    routed_tier: int | None,
    tiers: Config | Iterable[int] | Iterable[ModelEndpoint],
    override: bool = False,
    salt: str = DEFAULT_SALT,
) -> bool:
    """Whether ``ticket_id`` starts one tier below ``routed_tier``. See the module
    docstring. ``override``: the ticket was routed by a ``tier:N`` label
    (``route.source == "override"``). ``rate`` must be in [0, 1]."""
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate):
        raise ValueError(f"exploration rate must be a number in [0, 1], not {rate!r}")
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"exploration rate must be in [0, 1], not {rate!r}")
    if override or rate == 0.0:
        return False
    if explore_tier(routed_tier, tiers) is None:
        return False
    return explore_draw(ticket_id, salt) < rate


def explore_cursor(ticket_id: int) -> str:
    """The ``poll_cursors`` name holding a ticket's stored decision."""
    return f"{CURSOR_PREFIX}{int(ticket_id)}"


def encode(decision: bool) -> str:
    return "1" if decision else "0"


def decode(value: str | None) -> bool:
    """A stored decision; no row (or anything else) means no exploration."""
    return value == "1"


__all__ = [
    "CURSOR_PREFIX",
    "DEFAULT_RATE",
    "DEFAULT_SALT",
    "decode",
    "encode",
    "explore_cursor",
    "explore_draw",
    "explore_tier",
    "should_explore",
]
