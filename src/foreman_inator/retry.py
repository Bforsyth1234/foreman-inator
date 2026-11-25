"""Retry a flagged or failed ticket by hand (todo #79, GitHub issue #73).

``retry(store, config, ticket_ref, tier=..., now=...)`` is what ``fman retry
<repo>#<issue> [--tier N | --up]`` and the activity page's Retry button (wave 18) call.
It clears the ticket's flag and puts it back in the queue, keeping its history: the same
ticket row, its attempts, traces, kickbacks, reviews and PR branch, so the metrics count
it as one ticket. It never writes to GitHub.

**What it records.** One *retry mark* per manual retry, in the store's generic
``poll_cursors`` key/value table under ``retry:<ticket id>`` (the ``worker:`` pause marks,
the clarify and explore cursors use the same table; no schema change). The mark says
which attempt it came after (the latest *counted* attempt, as ``Worker.history`` counts
them) and that attempt's outcome then, the rung chosen for the next attempt (tier and
model), the flag it cleared, and when. The router and escalation tell a manual retry
from an automatic escalation by it: ``manual_retry_attempts`` names the attempts a mark
forced (the first counted attempt after each mark).

**What the worker does with it** (``with_manual_retry``, wired into
``Worker.policy_for``). The queue is derived from the store, and escalation is pure, so
the mark is read on every decision:

* *pending* (no counted attempt since the mark, and the last one's outcome unchanged):
  the next step is the forced attempt on the chosen rung, whatever escalation's own
  decision was (it would hand over again: the caps are still reached). Trigger: the last
  attempt's own on the same rung, ``escalation`` on another rung.
* *used*: escalation decides as usual from the same rows. Nothing is reset: the stint's
  failed checks, kickbacks and interrupted counts carry on, so a same-tier retry of a
  ``checks_failed`` ticket gets exactly one attempt before the next hand-over. On top of
  that the retry's round is capped: once ``RETRY_ROUND_ATTEMPTS`` non-interrupted
  attempts ran since the mark, or as soon as escalation would move to another rung, the
  ticket is handed over again with flag ``retry_round`` (``mark_ready`` when the last
  change passed its checks).

**Bound (invariant 7e).** Escalation alone gives at most ``attempt_bound(rungs)``
attempts. A retry adds at most ``RETRY_ROUND_ATTEMPTS`` non-interrupted attempts, plus
one interrupted one (the forced attempt runs even past ``MAX_INTERRUPTED``; any further
interruption hands over, since the per-ticket cut count is never reset), and only a
person starts one, at most ``MAX_MANUAL_RETRIES`` times per ticket. So a ticket gets at
most ``retry_attempt_bound(rungs)`` = ``attempt_bound(rungs) + MAX_MANUAL_RETRIES ×
(RETRY_ROUND_ATTEMPTS + 1)`` attempts, and every chain still ends flagged on the issue.
After the last retry the way out is the old one: remove the ``agent`` label and add it.

**Refusals** change nothing and say why (``RetryResult.reason``): an unknown ticket; a
merged one; a PR with you for review (``testing``); a flag no retry fixes (e.g.
``no_verify_command``: ``fman repo set-verify``); ``spend_cap`` while the cap is still
reached, or a ``thrifty`` pause (``fman approve-spend``); a tier move to a cloud tier on
a local-only repo (#49), up from the top tier, or to a tier with no model; the retry
limit; a dropped ticket whose issue is closed, lost its ``agent`` label, has another open
ticket, or whose PR was closed or merged.

A ``publish_failed`` ticket whose last attempt passed its checks gets no new attempt:
the retry clears the flag and the queue publishes that attempt again (same tier only).

**Once.** A second call while the first retry is still queued (the flag is clear and the
mark is pending) is a no-op that reports ``already_queued``: two clicks queue one attempt.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from foreman_inator.db.store import OPEN_COLUMNS, Attempt, NotFoundError, Store, Ticket
from foreman_inator.locks import flag_kind
from foreman_inator.routing import escalation as esc
from foreman_inator.spend import FLAG_SPEND_CAP, FLAG_THRIFTY, Spend
from foreman_inator.tickets.lifecycle import rebase_uncounted
from foreman_inator.worker_policy import (
    FLAG_CHECKS_FAILED,
    FLAG_INTERRUPTED,
    FLAG_KICKBACK_CAP,
    FLAG_NO_VERIFY,
    FLAG_PUBLISH_FAILED,
)
from foreman_inator.worker_setup import FLAG_SETUP

log = logging.getLogger("foreman_inator.retry")

#: Manual retries per ticket; after that, relabel to start over (7e: finite).
MAX_MANUAL_RETRIES = 3
#: Non-interrupted attempts one retry may run: the forced attempt plus one same-rung
#: retry (one segment, ``MAX_CHECK_ATTEMPTS``), never a move to another rung.
RETRY_ROUND_ATTEMPTS = esc.MAX_CHECK_ATTEMPTS
#: The hand-over after a retry's round is used up.
FLAG_RETRY_ROUND = "retry_round"
#: The agent label a dropped ticket's issue must still carry (``github.poller``'s).
AGENT_LABEL = "agent"

#: Flags a retry can lift: causes the user may have fixed, or caps they choose to pass.
RETRYABLE_FLAGS: frozenset[str] = frozenset(
    {
        FLAG_CHECKS_FAILED,
        FLAG_KICKBACK_CAP,
        esc.FLAG_KICKBACK_WRONG,
        esc.FLAG_AI_KICKBACK_CAP,
        FLAG_INTERRUPTED,
        FLAG_PUBLISH_FAILED,
        FLAG_SETUP,
        FLAG_SPEND_CAP,
        esc.FLAG_ATTEMPT_BUDGET,
        esc.FLAG_LOCAL_ONLY,
        esc.FLAG_NO_MODEL,
        "sandbox_unavailable",
        FLAG_RETRY_ROUND,
    }
)

#: What to do instead, for flags a retry does not lift.
_INSTEAD = {
    FLAG_NO_VERIFY: "set the verify command with `fman repo set-verify <repo> <command>`",
    "split_proposed": "add a `tier:N` label to run it as one ticket, or open the sub-issues",
}

TierChoice = int | Literal["same", "up"] | None
Status = Literal["queued", "already_queued", "refused"]


def _key(ticket_id: int) -> str:
    return f"retry:{ticket_id}"


def _uncounted_key(ticket_id: int) -> str:
    """``worker_pause``'s mark of attempts set aside (spend pauses, lock restarts)."""
    return f"worker:uncounted:{ticket_id}"


@dataclass(frozen=True)
class RetryMark:
    """One manual retry, as stored. ``after_attempt_id`` None: before any attempt."""

    at: str
    flag: str | None
    choice: str  # "same", "up" or the tier number asked for
    tier: int
    model: str
    trigger: str
    after_attempt_id: int | None
    after_outcome: str | None
    reopened: bool = False  # a dropped ticket put back in the queue
    moved: bool = False  # the retry's rung differs from the last attempt's


@dataclass(frozen=True)
class RetryResult:
    """What ``retry`` did. ``reason`` is the one line to show (refusal or success)."""

    status: Status
    reason: str
    ticket_id: int | None = None
    tier: int | None = None
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "refused"


# --------------------------------------------------------------------------- marks


def retry_marks(store: Store, ticket: Ticket) -> list[RetryMark]:
    """The ticket's manual retries, oldest first."""
    raw = store.get_cursor(ticket.repo, _key(ticket.id))
    if not raw:
        return []
    try:
        return [RetryMark(**m) for m in json.loads(raw)]
    except (ValueError, TypeError):
        log.warning("ticket %d: unreadable retry mark ignored", ticket.id)
        return []


def counted_attempts(store: Store, ticket: Ticket) -> tuple[Attempt, ...]:
    """The attempts escalation sees, as ``Worker.history`` filters them: without the
    ones set aside (``worker:uncounted:<ticket>``) and the rebase-uncounted ones."""
    attempts = store.list_attempts(ticket.id)
    raw = store.get_cursor(ticket.repo, _uncounted_key(ticket.id))
    skip = frozenset(int(i) for i in json.loads(raw)) if raw else frozenset()
    skip |= rebase_uncounted(attempts)
    return tuple(a for a in attempts if a.id not in skip)


def _pending(mark: RetryMark, attempts: Sequence[Attempt]) -> bool:
    """No counted attempt has started since ``mark``, and the last one is as it was."""
    last = attempts[-1] if attempts else None
    if (last.id if last else None) != mark.after_attempt_id:
        return False
    return (last.outcome if last else None) == mark.after_outcome


def manual_retry_attempts(store: Store, ticket: Ticket) -> frozenset[int]:
    """Attempts started by a manual retry (the first counted attempt after each mark), so
    the router and the metrics can tell them from automatic escalations."""
    attempts = counted_attempts(store, ticket)
    out: set[int] = set()
    for m in retry_marks(store, ticket):
        after = m.after_attempt_id or 0
        first = next((a for a in attempts if a.id > after), None)
        if first is not None:
            out.add(first.id)
    return frozenset(out)


def retry_attempt_bound(rungs: int, limits: esc.Limits | None = None) -> int:
    """Most attempts a ticket can get with every manual retry used (see the docstring)."""
    return esc.attempt_bound(rungs, limits) + MAX_MANUAL_RETRIES * (RETRY_ROUND_ATTEMPTS + 1)


# --------------------------------------------------------------------------- the worker's hook


def with_manual_retry(
    store: Store,
    ticket: Ticket,
    history: esc.History,
    step: esc.Step | None,
    ladder: esc.Ladder | None = None,
) -> esc.Step | None:
    """``Worker.policy_for``'s decision with the ticket's latest retry mark applied.

    A pending mark that no longer fits is dropped (logged) and escalation's ``step``
    stands: its model left the configured ``ladder``, or its rung is cloud (or of unknown
    kind: fails closed) while the repo is now local only (#49, invariant 7c), e.g. after
    ``fman repo set-cloud <repo> false`` between the retry and the attempt."""
    marks = retry_marks(store, ticket)
    mark = marks[-1] if marks else None
    if mark is not None and _pending(mark, history.attempts):
        stale = _stale(store, ticket, mark, ladder)
        if stale:
            log.info("ticket %d: manual retry on %s dropped: %s", ticket.id, mark.model, stale)
            store.set_cursor(ticket.repo, json.dumps([asdict(m) for m in marks[:-1]]),
                             _key(ticket.id))  # fmt: skip
            return step
    return apply_mark(mark, history.attempts, step)


def _stale(store: Store, ticket: Ticket, mark: RetryMark, ladder: esc.Ladder | None) -> str:
    """Why a pending ``mark`` may no longer run ("" when it may)."""
    rung = next((r for r in ladder or () if r.model == mark.model), None)
    if ladder is not None and rung is None:
        return "the model is no longer in the configured ladder"
    if not store.repo_allows_cloud(ticket.repo) and (rung is None or not rung.local):
        return "the repo is now local only (cloud = false) and the rung is cloud"
    return ""


def apply_mark(
    mark: RetryMark | None, attempts: Sequence[Attempt], step: esc.Step | None
) -> esc.Step | None:
    """Pure: escalation's ``step`` for counted ``attempts``, changed by a retry ``mark``
    (the rules in the module docstring)."""
    if mark is None:
        return step
    last = attempts[-1] if attempts else None
    if last is not None and (last.ended_at is None or last.outcome in (None, "accepted")):
        return step  # running, or passed and waiting on publish/review: nothing to start
    if _pending(mark, attempts):
        why = f"manual retry (fman retry) after {mark.flag or 'a drop'}, on tier {mark.tier}"
        why += f" ({mark.model})"
        cls = esc.Escalate if mark.moved else esc.Retry
        return cls(mark.trigger, mark.tier, why, mark.model)  # type: ignore[arg-type]
    if step is None or isinstance(step, esc.HandToUser):
        return step
    if mark.after_attempt_id is not None and mark.after_outcome is None:
        return step  # a publish retry (#79 publish_failed): no attempt was forced
    since = [a for a in attempts if a.id > (mark.after_attempt_id or 0)]
    ran = sum(a.outcome != "superseded" for a in since)
    if isinstance(step, esc.Escalate) or ran >= RETRY_ROUND_ATTEMPTS:
        return esc.HandToUser(
            f"{FLAG_RETRY_ROUND}: the manual retry's round is used ({ran} attempt"
            f"{'s' if ran != 1 else ''} since `fman retry`); over to you",
            mark_ready=bool(last is not None and last.tests_passed),
        )
    return step


# --------------------------------------------------------------------------- retry


def retry(
    store: Store,
    config: Any,
    ticket_ref: int | str,
    *,
    tier: TierChoice = None,
    now: datetime,
    api: Any = None,
    spend: Spend | None = None,
) -> RetryResult:
    """Retry a flagged ticket, or a dropped one whose issue still has the agent label.

    ``ticket_ref``: a ticket id, ``"<id>"`` or ``"<owner>/<repo>#<issue>"``. ``tier``:
    None or ``"same"`` (the last attempt's rung), ``"up"`` (the next rung) or a tier
    number. ``api``: a GitHub client, needed only for a dropped ticket (its issue and PR
    are checked). ``spend``: the cap checker (default ``Spend(store, config)``).
    A refusal changes nothing; see the module docstring."""
    try:
        t = resolve_ticket(store, ticket_ref)
    except LookupError as exc:
        return RetryResult("refused", str(exc))
    if t.column == "dropped":  # GitHub reads first, outside the store's write lock
        why = _github_refusal(t, api)
        if why is not None:
            return RetryResult("refused", why, t.id)
    with store.transaction():
        t = store.get_ticket(t.id)
        marks = retry_marks(store, t)
        attempts = counted_attempts(store, t)
        queued = not t.flag_reason and t.column in OPEN_COLUMNS and marks
        if queued and _pending(marks[-1], attempts):  # the once-only guard: two clicks
            m = marks[-1]
            return RetryResult(
                "already_queued",
                f"ticket {t.id} is already queued for its retry on tier {m.tier}",
                t.id,
                m.tier,
                m.model,
            )
        why = _refusal(store, config, t, api, spend)
        if why is None and len(marks) >= MAX_MANUAL_RETRIES:
            why = (
                f"ticket {t.id} was retried {len(marks)} times already (the most, so it "
                "cannot loop); remove the `agent` label and add it again to start over"
            )
        if why is not None:
            return RetryResult("refused", why, t.id)
        rung, err = _pick(store, config, t, attempts, tier)
        if err is not None:
            return RetryResult("refused", err, t.id)
        assert rung is not None
        last = attempts[-1] if attempts else None
        same = last is not None and (rung.tier, rung.model) == (last.tier, last.model)
        if last is not None and last.tests_passed and last.outcome is None and not same:
            return RetryResult(
                "refused",
                f"ticket {t.id}'s last attempt passed its checks and waits to be "
                "published; retry it on the same tier (the retry publishes it)",
                t.id,
            )
        trigger = (last.trigger if same else "escalation") if last else "first"
        if trigger == "rebase":
            trigger = "escalation"
        reopened = t.column == "dropped"
        mark = RetryMark(
            at=now.isoformat(),
            flag=t.flag_reason,
            choice=str(tier if tier is not None else "same"),
            tier=rung.tier,
            model=rung.model,
            trigger=trigger,
            after_attempt_id=last.id if last else None,
            after_outcome=last.outcome if last else None,
            reopened=reopened,
            moved=last is not None and not same,
        )
        store.set_cursor(t.repo, json.dumps([asdict(m) for m in [*marks, mark]]), _key(t.id))
        if reopened:
            store.set_column(t.id, "in_progress" if attempts else "todo")
        if t.flag_reason:
            store.clear_flag(t.id)
    what = "put back in the queue" if reopened else "unflagged and queued"
    log.info("ticket %d: manual retry on tier %d (%s); %s", t.id, rung.tier, rung.model, what)
    return RetryResult(
        "queued",
        f"ticket {t.id} ({t.repo}#{t.issue_number}) {what}: next attempt on tier {rung.tier} "
        f"({rung.model}); retry {len(marks) + 1} of {MAX_MANUAL_RETRIES}",
        t.id,
        rung.tier,
        rung.model,
    )


_REF = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<n>\d+)$")


def resolve_ticket(store: Store, ref: int | str) -> Ticket:
    """The ticket ``ref`` names: an id, or ``owner/repo#issue`` (its open ticket, else
    its latest). Raises LookupError with a message to show."""
    if isinstance(ref, int) or str(ref).strip().isdigit():
        try:
            return store.get_ticket(int(ref))
        except NotFoundError:
            raise LookupError(f"no ticket {ref}") from None
    m = _REF.match(str(ref).strip())
    if m is None:
        raise LookupError(f"{ref!r} is not a ticket id or <owner>/<repo>#<issue>")
    repo, n = m["repo"], int(m["n"])
    t = store.find_open_ticket(repo, n)
    if t is None:
        tickets = store.tickets_for_issue(repo, n)
        if not tickets:
            raise LookupError(f"no ticket for {repo}#{n}")
        t = tickets[-1]
    return t


def _refusal(store: Store, config: Any, t: Ticket, api: Any, spend: Spend | None) -> str | None:
    """Why ``t`` cannot be retried now (ladder choice aside), or None."""
    if t.column == "done":
        return f"ticket {t.id} is merged; nothing to retry"
    if t.column == "dropped":
        other = store.find_open_ticket(t.repo, t.issue_number)
        if other is not None:
            return f"{t.repo}#{t.issue_number} already has an open ticket ({other.id}); retry that"
        return None
    if t.column == "testing":
        if t.flag_reason:
            return (
                f"ticket {t.id}'s PR is with you for review; approve it, or request changes "
                "to send it back"
            )
        return f"ticket {t.id} is waiting on its review; nothing to retry"
    if not t.flag_reason:
        return f"ticket {t.id} is not flagged; it is still in the queue"
    kind = flag_kind(t.flag_reason)
    if kind not in RETRYABLE_FLAGS:
        instead = _INSTEAD.get(kind, "remove the `agent` label and add it again")
        return f"a retry does not lift the {kind} flag; {instead}"
    if kind == FLAG_SPEND_CAP:
        return _spend_refusal(store, config, t, spend)
    return None


def _spend_refusal(store: Store, config: Any, t: Ticket, spend: Spend | None) -> str | None:
    if (t.flag_reason or "").startswith(FLAG_THRIFTY):
        return (
            f"ticket {t.id} waits for your approval of a cloud attempt (thrifty profile); "
            f"run `fman approve-spend {t.id}`"
        )
    s = spend or Spend(store, config)
    r = s.blocked(t.id)
    if r is None:
        return None
    resets = f"; it resets {r.resets_at:%Y-%m-%d %H:%M} UTC" if r.resets_at else ""
    return (
        f"the {r.cap} spend cap is still reached (${r.spent_usd:.2f} of ${r.cap_usd:.2f})"
        f"{resets}; run `fman approve-spend {t.id}` to allow one more attempt"
    )


def _github_refusal(t: Ticket, api: Any) -> str | None:
    """A dropped ticket: its issue must be open with the agent label, its PR open."""
    if api is None:
        return f"ticket {t.id} was dropped; retrying it needs GitHub to check its issue"
    issue = api.get_issue(t.repo, t.issue_number)
    if issue.state != "open":
        return f"{t.repo}#{t.issue_number} is closed; reopen it first"
    if AGENT_LABEL not in {x.lower() for x in issue.labels}:
        return f"{t.repo}#{t.issue_number} no longer has the `{AGENT_LABEL}` label; add it again"
    if t.pr_number is not None:
        pr = api.get_pr(t.repo, t.pr_number)
        if pr.merged:
            return f"PR #{pr.number} was merged; nothing to retry"
        if pr.state != "open":
            return f"PR #{pr.number} was closed; reopen it first, or relabel to start over"
    return None


def _pick(
    store: Store, config: Any, t: Ticket, attempts: Sequence[Attempt], tier: TierChoice
) -> tuple[esc.Rung | None, str | None]:
    """The rung for the retry's attempt, or a refusal."""
    ladder = esc.build_ladder(config.models)
    if not ladder:
        return None, "no writer model is configured"
    local_only = not store.repo_allows_cloud(t.repo)
    last = attempts[-1] if attempts else None
    here = _rung_index(last, ladder) if last else _first_index(store, t, ladder, local_only)
    if tier in (None, "same"):
        idx = here
    elif tier == "up":
        if here + 1 >= len(ladder):
            return None, f"tier {ladder[here].tier} ({ladder[here].model}) is the top tier"
        idx = here + 1
    else:
        try:
            n = int(tier)
        except (TypeError, ValueError):
            return None, f"tier must be 'same', 'up' or a number, not {tier!r}"
        found = [i for i, r in enumerate(ladder) if r.tier == n]
        if not found:
            return None, f"no writer model is configured on tier {n}"
        idx = here if ladder[here].tier == n else found[0]
    rung = ladder[idx]
    if local_only and not rung.local:
        return None, (
            f"{t.repo} is local only (cloud = false), so the retry cannot move to tier "
            f"{rung.tier} ({rung.model}), a cloud model; `fman repo set-cloud` changes that"
        )
    return rung, None


def _rung_index(a: Attempt, ladder: esc.Ladder) -> int:
    """The rung an attempt ran on (as escalation finds it)."""
    for i, r in enumerate(ladder):
        if (r.tier, r.model) == (a.tier, a.model):
            return i
    return next((i for i, r in enumerate(ladder) if r.tier >= a.tier), len(ladder) - 1)


def _first_index(store: Store, t: Ticket, ladder: esc.Ladder, local_only: bool) -> int:
    """No attempt yet: the rung escalation would start on."""
    start = esc.next_step(
        esc.History((), routed_tier=t.routed_tier), ladder, esc.Limits(cloud=not local_only)
    )
    model = getattr(start, "model", None)
    for i, r in enumerate(ladder):
        if r.model == model:
            return i
    return 0


__all__ = [
    "FLAG_RETRY_ROUND",
    "MAX_MANUAL_RETRIES",
    "RETRYABLE_FLAGS",
    "RETRY_ROUND_ATTEMPTS",
    "RetryMark",
    "RetryResult",
    "apply_mark",
    "counted_attempts",
    "manual_retry_attempts",
    "resolve_ticket",
    "retry",
    "retry_attempt_bound",
    "retry_marks",
    "with_manual_retry",
]
