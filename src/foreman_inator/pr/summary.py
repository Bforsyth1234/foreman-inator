"""The one-line summary comment posted on the issue when a ticket closes (todo #20).

SPEC "Metrics view": "When an issue closes, it also posts a one-line summary comment on
the issue: model, attempts, kickbacks and cost." The line also gives the tier, the
starting tier when the ticket was escalated, and the time from label to merge.

The comment is counts, model names and money only. It never carries reasoning, file
contents, verify output, prompts or kickback notes: traces stay on the Mac (7c).

``summary_line`` is pure. ``post_summary`` reads the store and posts through the
``GitHubAPI``; it is idempotent: before posting it lists the issue's comments and skips
the post when one written by the bot already carries this ticket's marker
``<!-- foreman-inator:summary ticket=<id> -->``. A re-labelled issue gets a new ticket id,
so it gets its own summary.

ab:pair (#34, GH #16, wave 23): for a side of a pair, ``post_summary`` adds the pair
total, both sides' spend (``experiments.pair.ticket_cost``: attempts plus non-shadow
reviews), right after the ticket's own cost: ``pair total $1.20 (both sides)``. The
loser's spend is part of what the merged change cost. Unpaired lines are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from foreman_inator.db.store import Attempt, Kickback, Review, Store, Ticket
from foreman_inator.github.client import Comment, GitHubAPI
from foreman_inator.pr.publisher import format_cost

SUMMARY_PREFIX = "foreman-inator summary:"


def summary_marker(ticket_id: int) -> str:
    """Hidden marker that identifies this ticket's summary comment."""
    return f"<!-- foreman-inator:summary ticket={int(ticket_id)} -->"


def format_duration(seconds: float | None) -> str:
    """``45s``, ``12m``, ``2h 5m``, ``3d 4h``; ``unknown`` for None."""
    if seconds is None:
        return "unknown"
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    m, _ = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _name(value: str) -> str:
    """A model name made safe for one markdown line (no newlines, no marker forgery)."""
    text = " ".join(str(value).split())
    return text.replace("<!--", "").replace("-->", "").replace("`", "'")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def summary_line(
    ticket: Ticket,
    attempts: Sequence[Attempt],
    reviews: Sequence[Review],
    kickbacks: Sequence[Kickback],
    *,
    now: datetime | None = None,
    attempt_budget_usd: float | None = None,
    pair_total_usd: float | None = None,
) -> str:
    """One line: who solved it, attempts, kickbacks, cost and time.

    * model / tier: the accepted attempt's (``ticket.accepted_attempt_id``); if none is
      set, the last attempt's, said as "last tried".
    * attempts: every attempt except ``superseded`` ones (runs interrupted by a crash or
      restart and re-run, not separate tries).
    * kickbacks: all of the ticket's kickbacks, split into yours and the AI reviewer's.
    * cost: every attempt plus every non-shadow review (shadow reviews are experiment
      spend), formatted like the PR body. With ``attempt_budget_usd`` (#58) it adds the
      most one attempt spent against that per-attempt limit, and how many attempts
      stopped at their budget (``end_reason`` ``budget``) whenever any did.
    * pair total (``pair_total_usd``, #34): both sides' spend when the ticket is a side
      of an ``ab:pair``.
    * time: ``created_at`` → ``done_at`` (or ``now`` while ``done_at`` is not set yet).
    """
    ordered = sorted(attempts, key=lambda a: a.id)
    accepted = next((a for a in ordered if a.id == ticket.accepted_attempt_id), None)
    tries = [a for a in ordered if a.outcome != "superseded"]
    parts: list[str] = []
    if accepted is not None:
        who = f"solved by {_name(accepted.model)} (tier {accepted.tier})"
        first = tries[0] if tries else accepted
        if first.tier != accepted.tier:
            who += f", started on tier {first.tier}"
        parts.append(who)
    elif ordered:
        last = ordered[-1]
        parts.append(f"last tried {_name(last.model)} (tier {last.tier})")
    else:
        parts.append("no attempts")
    parts.append(_plural(len(tries), "attempt"))
    user = sum(1 for k in kickbacks if k.by == "user")
    ai = sum(1 for k in kickbacks if k.by == "reviewer")
    kb = _plural(user + ai, "kickback")
    if user or ai:
        kb += f" ({user} yours, {ai} AI reviewer)"
    parts.append(kb)
    cost = sum(a.cost_usd for a in attempts) + sum(r.cost_usd for r in reviews if not r.shadow)
    parts.append(f"cost {format_cost(cost)}")
    if pair_total_usd is not None:
        parts.append(f"pair total {format_cost(pair_total_usd)} (both sides)")
    budget = _attempt_budget_part(tries, attempt_budget_usd)
    if budget:
        parts.append(budget)
    end = ticket.done_at or now
    if end is not None:
        secs = (end - ticket.created_at).total_seconds()
        parts.append(f"{format_duration(secs)} from label to merge")
    return f"{SUMMARY_PREFIX} " + " · ".join(parts)


def _attempt_budget_part(tries: Sequence[Attempt], limit: float | None) -> str:
    """``max $0.42 per attempt of $1.50 limit``, ``1 stopped at its budget`` (#58)."""
    bits: list[str] = []
    if limit is not None and tries:
        top = max(a.cost_usd for a in tries)
        bits.append(f"max {format_cost(top)} per attempt of {format_cost(limit)} limit")
    stopped = sum(1 for a in tries if getattr(a, "end_reason", None) == "budget")
    if stopped:
        bits.append(f"{stopped} stopped at the attempt budget")
    return ", ".join(bits)


def summary_comment(
    ticket: Ticket,
    attempts: Sequence[Attempt],
    reviews: Sequence[Review],
    kickbacks: Sequence[Kickback],
    *,
    now: datetime | None = None,
    attempt_budget_usd: float | None = None,
    pair_total_usd: float | None = None,
) -> str:
    """The comment body: the hidden marker, then the one visible line."""
    line = summary_line(
        ticket,
        attempts,
        reviews,
        kickbacks,
        now=now,
        attempt_budget_usd=attempt_budget_usd,
        pair_total_usd=pair_total_usd,
    )
    return f"{summary_marker(ticket.id)}\n{line}"


@dataclass(frozen=True)
class SummaryResult:
    posted: bool  # False when an earlier summary for this ticket was found
    comment: Comment | None  # the new or the existing comment
    body: str


def find_summary(api: GitHubAPI, ticket: Ticket) -> Comment | None:
    """The bot's existing summary comment for this ticket, if any."""
    marker = summary_marker(ticket.id)
    bot = api.bot_login()
    for c in api.list_issue_comments(ticket.repo, ticket.issue_number):
        if c.author == bot and marker in c.body:
            return c
    return None


def pair_total(store: Store, ticket_id: int) -> float | None:
    """Both sides' spend when the ticket is a side of a pair, else None (also on a
    database from before migration 016)."""
    try:
        pair = store.pair_for_ticket(ticket_id)
    except Exception:
        return None
    if pair is None:
        return None
    from foreman_inator.experiments.pair import ticket_cost

    return ticket_cost(store, pair.ticket_a) + ticket_cost(store, pair.ticket_b)


def post_summary(
    api: GitHubAPI, store: Store, ticket_id: int, *, attempt_budget_usd: float | None = None
) -> SummaryResult:
    """Post the ticket's summary on its issue once; a retry finds it and does not repost.

    Reads the store when called, so call it after the accept is recorded (the worker's
    deferred GitHub write runs after the event's transaction commits).
    """
    ticket = store.get_ticket(ticket_id)
    body = summary_comment(
        ticket,
        store.list_attempts(ticket.id),
        store.list_reviews(ticket_id=ticket.id, shadow=False),
        store.list_kickbacks(ticket_id=ticket.id),
        now=store.now(),
        attempt_budget_usd=attempt_budget_usd,
        pair_total_usd=pair_total(store, ticket.id),
    )
    existing = find_summary(api, ticket)
    if existing is not None:
        return SummaryResult(posted=False, comment=existing, body=existing.body)
    comment = api.post_comment(ticket.repo, ticket.issue_number, body)
    return SummaryResult(posted=True, comment=comment, body=body)


__all__ = [
    "SUMMARY_PREFIX",
    "SummaryResult",
    "find_summary",
    "format_duration",
    "pair_total",
    "post_summary",
    "summary_comment",
    "summary_line",
    "summary_marker",
]
