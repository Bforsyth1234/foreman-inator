"""Escalation state machine (todo #16): what happens after an attempt ends, across tiers.

Pure: store rows in, a decision out. No store, no GitHub, no clock, no config. The same
rows and the same ladder always give the same decision, so a restarted worker decides
exactly what the crashed one would have (crash safety, as in v1's ``worker_policy``).

**The ladder.** Every configured writer model is one rung, ordered by tier and, inside a
tier, by config order (``Config.models_for_tier``: tier 3 is GLM, then Claude). Tiers with
no model simply have no rung, so they are skipped. "Move up one tier" means "move to the
next rung"; for tier 3 that is GLM → Claude (SPEC Decisions: "the Claude API is used on
escalation from GLM"), and "tier 3 fails" means the *last rung* fails: then the ticket is
handed to the user. If tier 3 is not configured, the top configured rung plays that role.

**Rules** (SPEC "Routing and escalation" and "AI review"), counted per *stint*: the run
of consecutive non-interrupted attempts on one rung. Counts reset when the rung changes.

==================================  ==========================================  =========
After the last attempt              Decision                                    Trigger
==================================  ==========================================  =========
no attempts yet                     start on the routed tier's first rung       first
                                    (explore: the rung group one tier lower)    explore
still running / passed checks and   None (nothing to start)                     —
awaiting review / accepted
interrupted (``superseded``)        re-run on the same rung, at most            same as
                                    ``MAX_INTERRUPTED`` times per ticket;       the cut
                                    then hand to the user (``interrupted``)     attempt
checks failed, 1st in the segment   retry on the same rung                      same as
                                                                                retried
checks failed, 2nd in the segment   move up (``checks_failed``)                 escalation
user kickback tagged ``wrong``      move up (``kickback_wrong``)                escalation
user kickback, 1st on this rung     retry on the same rung with the note        kickback
user kickback, 2nd on this rung     move up (``kickback_cap``)                  escalation
AI kickback, 1st on this rung       retry on the same rung with the note        ai_kickback
AI kickback, 2nd on this rung       move up (``ai_kickback_cap``)               escalation
an ``explore`` attempt failed       move straight to the routed tier, with no   escalation
(checks or any kickback)            same-tier retry (``explore_failed``)
"move up" from the last rung        hand to the user; ``mark_ready`` when the   —
                                    last change passed its checks
checks failed with ``end_reason``   hand to the user (``attempt_budget``), no   —
``budget`` (#58)                    retry on this or any tier: another attempt
                                    would spend the same again
==================================  ==========================================  =========

A *segment* is the part of a stint after its last kickback, so a kickback-triggered
attempt that fails its checks still gets one same-rung retry. A ``wrong`` tag from the
AI reviewer does not skip a rung (the "tagged wrong" rule is the user's; the reviewer's
rule is "two automatic kickbacks"). An explore stint never counts against the rung the
ticket escalates to: that rung starts with fresh counts.

**Bound** (invariant 7e). A stint holds at most ``MAX_ATTEMPTS_PER_RUNG`` attempts:
three segments (separated by at most one user and one AI kickback that stay on the rung)
of at most ``MAX_CHECK_ATTEMPTS`` attempts each. Rungs only go up. So a ticket gets at
most ``attempt_bound(len(ladder)) = rungs × 6 + MAX_INTERRUPTED`` attempts before it is
handed to the user; the default five-rung ladder gives ``SPEC_LADDER_WORST_CASE`` = 33.

**Kickback kinds.** A ``kicked_back`` attempt's kickbacks come from its kickback rows
(``by`` user or reviewer, oldest first). When it has no reviewer row but a non-shadow AI
review with verdict ``request_changes``, that review counts as one AI kickback (the same
fallback as ``tickets.prompt.load_history``). A ``kicked_back`` attempt with neither
counts as one user kickback (v1 rows). The latest kickback decides which rule applies.

**Limits** (#51, repo profiles). The counts above are ``Limits``' fields; ``next_step``,
``make_policy`` and ``attempt_bound`` take a ``limits=`` argument whose default (None)
is exactly the module constants, read at call time (``DEFAULT_LIMITS`` holds the same
values), so a repo with no profile decides as before. ``routing.profile.limits_for``
builds the other profiles' limits (``thrifty``: three check attempts per segment;
``fast``: one; ``strict`` review: one AI kickback moves up).
Every field is at least 1, so every profile keeps a finite bound (7e). A profile's start
shift is not applied here: the worker stores the shifted start when it routes the ticket
and passes it as ``History.start_tier`` (``routed_tier`` stays the router's own pick, which
#23's kNN router learns from). The per-attempt budget hand-over (#58) comes before every
count, so it holds for every profile.

**Local only** (#49). A repo set ``cloud = false`` passes ``Limits(cloud=False)``
(``with_cloud``): the decision runs on the ladder's *local* rungs only (``Rung.cloud``
False; a rung of unknown kind counts as cloud, so a hand-built ladder fails closed).
"Move up" from the last local rung hands over with flag ``local_only`` (the reason keeps
the trigger's own flag after it, e.g. ``local_only: checks_failed: ...``) instead of
escalating to a cloud tier; a routed tier that only has cloud models starts on the last
local rung; no local rung at all hands over at once. Every other rule and count is the
profile's, so the bound is ``attempt_bound(local rungs, limits)``, at most the full
ladder's (7e). The spend gate refuses that repo's cloud calls as well (``spend.py``).

Return types subclass v1's ``worker_policy.Retry`` / ``HandToUser``, so code that checks
``isinstance(step, worker_policy.Retry)`` keeps working; they add the model to run
(``model``) and, for a hand-over, whether to mark the PR ready (``mark_ready``).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from foreman_inator import worker_policy as v1
from foreman_inator.db.store import Attempt, Kickback, KickbackBy, Review, Tag, Trigger

#: Attempts per segment whose checks may fail before moving up (first try + one retry).
MAX_CHECK_ATTEMPTS = v1.MAX_CHECK_ATTEMPTS
#: User kickbacks on one rung: the first retries on the rung, this many moves up.
MAX_USER_KICKBACKS = 2
#: AI-reviewer kickbacks on one rung: the first retries on the rung, this many moves up.
MAX_AI_KICKBACKS = 2
#: Interrupted (crash/shutdown) attempts re-run per ticket; the next one hands over.
MAX_INTERRUPTED = v1.MAX_INTERRUPTED
#: Most attempts one stint can hold: 3 segments × MAX_CHECK_ATTEMPTS.
MAX_ATTEMPTS_PER_RUNG = (1 + (MAX_USER_KICKBACKS - 1) + (MAX_AI_KICKBACKS - 1)) * (
    MAX_CHECK_ATTEMPTS
)

FLAG_CHECKS_FAILED = v1.FLAG_CHECKS_FAILED
FLAG_KICKBACK_CAP = v1.FLAG_KICKBACK_CAP
FLAG_INTERRUPTED = v1.FLAG_INTERRUPTED
FLAG_KICKBACK_WRONG = "kickback_wrong"
FLAG_AI_KICKBACK_CAP = "ai_kickback_cap"
FLAG_EXPLORE_FAILED = "explore_failed"
FLAG_NO_MODEL = "no_model"
#: #58: the last attempt reached its per-attempt budget (``spend.FLAG_ATTEMPT_BUDGET``).
FLAG_ATTEMPT_BUDGET = "attempt_budget"
#: #49: a local-only repo (``cloud = false``) reached its last local rung.
FLAG_LOCAL_ONLY = "local_only"


@dataclass(frozen=True)
class Limits:
    """The escalation counts for one repo profile (#51). The defaults are today's rules.

    ``start_shift``, ``explore_rate`` and ``cloud_approval`` are not read by ``next_step``:
    the worker applies them when it routes (``profile.shift_start``, the explore draw) and
    before a cloud writer attempt (``Spend.writer_gate``). ``explore_rate`` None means the
    config's ``explore_rate`` (the ``normal`` writer mode, unchanged).

    ``cloud`` (#49) is the repo's setting, not the profile's: False keeps the ladder to
    its local rungs (see the module docstring). Build it with ``with_cloud``."""

    max_check_attempts: int = MAX_CHECK_ATTEMPTS
    max_user_kickbacks: int = MAX_USER_KICKBACKS
    max_ai_kickbacks: int = MAX_AI_KICKBACKS
    start_shift: int = 0
    explore_rate: float | None = None
    cloud_approval: bool = False
    cloud: bool = True

    def __post_init__(self) -> None:
        for name in ("max_check_attempts", "max_user_kickbacks", "max_ai_kickbacks"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"Limits.{name} must be at least 1 (invariant 7e)")
        if self.start_shift < 0:
            raise ValueError("Limits.start_shift must not be negative (rungs only go up)")

    @property
    def attempts_per_rung(self) -> int:
        """Most attempts one stint can hold: segments × check attempts per segment."""
        segments = 1 + (self.max_user_kickbacks - 1) + (self.max_ai_kickbacks - 1)
        return segments * self.max_check_attempts


#: Today's rules: what a repo with no profile (``normal`` + ``normal``) gets.
DEFAULT_LIMITS = Limits()


def _limits(limits: Limits | None) -> Limits:
    """``limits``, or (None) today's rules read from the module constants at call time,
    so a test or a replay that patches a constant still changes the decision."""
    if limits is not None:
        return limits
    return Limits(MAX_CHECK_ATTEMPTS, MAX_USER_KICKBACKS, MAX_AI_KICKBACKS)


def with_cloud(limits: Limits | None, cloud: bool) -> Limits | None:
    """``limits`` for a repo whose cloud setting is ``cloud`` (#49): unchanged (None stays
    None, today's rules) when cloud is allowed; else the same counts with ``cloud=False``."""
    if cloud:
        return limits
    return replace(_limits(limits), cloud=False)


def attempt_bound(rungs: int, limits: Limits | None = None) -> int:
    """Most attempts a ticket can get on a ladder of ``rungs`` rungs before the user."""
    return rungs * _limits(limits).attempts_per_rung + MAX_INTERRUPTED


#: The bound for the default ladder (tier 1, tier 2, tier 3 GLM, Claude Opus, Claude Fable).
SPEC_LADDER_WORST_CASE = attempt_bound(5)


# --------------------------------------------------------------------------- types


@dataclass(frozen=True)
class Rung:
    """One step of the ladder: a writer model on a tier."""

    tier: int
    model: str
    #: #49: does this rung's model leave the Mac? ``build_ladder`` sets it from the
    #: endpoint's ``kind``; None (a hand-built rung) is treated as cloud by a local-only
    #: repo. Not part of equality, so ``Rung(1, "m")`` still equals a built rung.
    cloud: bool | None = field(default=None, compare=False)

    @property
    def local(self) -> bool:
        return self.cloud is False


Ladder = tuple[Rung, ...]


class _ModelLike(Protocol):
    @property
    def tier(self) -> int: ...
    @property
    def name(self) -> str: ...


def local_rungs(ladder: Sequence[Rung]) -> Ladder:
    """The rungs a local-only repo (#49) may use: those whose model is local."""
    return tuple(r for r in ladder if r.local)


def build_ladder(models: Iterable[_ModelLike]) -> Ladder:
    """The ladder from configured models (e.g. ``config.models``): by tier, then in the
    given order; a repeated model name keeps its first place. ``Rung.cloud`` comes from
    the model's ``kind`` (None when it has none)."""
    seen: set[str] = set()
    out: list[Rung] = []
    for m in sorted(models, key=lambda m: m.tier):
        if m.name not in seen:
            seen.add(m.name)
            kind = getattr(m, "kind", None)
            out.append(Rung(m.tier, m.name, None if kind is None else kind != "local"))
    return tuple(out)


@dataclass(frozen=True)
class History:
    """Everything the decision reads about one ticket. All rows oldest first."""

    attempts: Sequence[Attempt]
    kickbacks: Sequence[Kickback] = ()
    reviews: Sequence[Review] = ()
    #: The router's pick (#15, including a ``tier:N`` override). None: the lowest rung.
    routed_tier: int | None = None
    #: #23: start one tier below ``routed_tier`` with trigger ``explore``.
    explore: bool = False
    #: #51: the start after a profile's shift (``profile.shift_start``), stored when the
    #: ticket was routed. None: start on ``routed_tier``.
    start_tier: int | None = None


@dataclass(frozen=True)
class Retry(v1.Retry):
    """Start another attempt on the same rung (or the first attempt)."""

    model: str | None = None


@dataclass(frozen=True)
class Escalate(Retry):
    """Start another attempt one or more rungs up. ``trigger`` is ``escalation``."""


@dataclass(frozen=True)
class HandToUser(v1.HandToUser):
    """Stop and flag the ticket. ``reason`` starts with a ``FLAG_`` name and a colon.

    ``mark_ready``: the last change passed its checks (it was kicked back), so the PR is
    handed over too and should be marked ready for review (SPEC: "the PR goes to you
    with the reviewer's notes")."""

    mark_ready: bool = False

    @property
    def flag(self) -> str:
        return self.reason.split(":", 1)[0]


Step = Retry | Escalate | HandToUser


# --------------------------------------------------------------------------- decision


def next_step(
    history: History | Sequence[Attempt],
    ladder: Sequence[Rung],
    limits: Limits | None = None,
) -> Step | None:
    """The next step for a ticket, or None when there is nothing to start (an attempt is
    running, passed its checks and waits on review or publish, or was accepted).
    ``limits``: the repo profile's counts (#51); None is today's rules.

    ``history`` may be a bare attempt list (v1's call shape): then every kicked-back
    attempt counts as a user kickback and the ticket starts on the lowest rung."""
    h = history if isinstance(history, History) else History(tuple(history))
    limits = _limits(limits)
    full: Ladder = tuple(ladder)
    if not full:
        return HandToUser(f"{FLAG_NO_MODEL}: no writer model is configured on any tier")
    rungs = full if limits.cloud else local_rungs(full)
    attempts = list(h.attempts)
    if attempts and (attempts[-1].ended_at is None or attempts[-1].outcome in (None, "accepted")):
        return None
    if not rungs:
        return HandToUser(
            f"{FLAG_LOCAL_ONLY}: the repo is local only (cloud = false) and no local writer "
            "model is configured; over to you"
        )
    if not attempts:
        return _first(h, rungs, local_only=not limits.cloud)
    up = functools.partial(_up, cloud_next=None if limits.cloud else full)
    last = attempts[-1]
    idx = _rung_of(last, rungs)
    here = rungs[idx]
    if last.outcome == "superseded":
        cut = sum(a.outcome == "superseded" for a in attempts)
        if cut > MAX_INTERRUPTED:
            return HandToUser(
                f"{FLAG_INTERRUPTED}: {cut} attempts were cut short by a crash or shutdown"
            )
        return Retry(last.trigger, here.tier, "re-run after an interrupted attempt", here.model)

    if last.outcome == "failed_checks" and getattr(last, "end_reason", None) == "budget":
        what = getattr(last, "end_detail", None) or "reached its per-attempt budget"
        return HandToUser(
            f"{FLAG_ATTEMPT_BUDGET}: {what} on {_name(here)}; stopped at the per-attempt "
            "budget, not retried on any tier; over to you"
        )

    stint = _stint([a for a in attempts if a.outcome != "superseded"], idx, rungs)
    kicks = _kicks_by_attempt(h)

    if stint[0].trigger == "explore":
        target = _start_index(_start(h), rungs)
        what = "failed its checks" if last.outcome == "failed_checks" else "was kicked back"
        return up(
            rungs,
            idx,
            FLAG_EXPLORE_FAILED,
            f"the explore attempt {what}",
            last,
            target=target if target > idx else idx + 1,
        )

    if last.outcome == "kicked_back":
        by, tag = kicks.get(last.id, [("user", None)])[-1]
        if by == "reviewer":
            n = _count(stint, kicks, "reviewer")
            if n >= limits.max_ai_kickbacks:
                return up(
                    rungs,
                    idx,
                    FLAG_AI_KICKBACK_CAP,
                    f"the AI reviewer kicked back {n} changes",
                    last,
                )
            return Retry(
                "ai_kickback", here.tier, f"AI reviewer kickback {n} on {_name(here)}", here.model
            )
        n = _count(stint, kicks, "user")
        if tag == "wrong":
            return up(rungs, idx, FLAG_KICKBACK_WRONG, "the change was kicked back as wrong", last)
        if n >= limits.max_user_kickbacks:
            return up(rungs, idx, FLAG_KICKBACK_CAP, f"changes were requested {n} times", last)
        return Retry("kickback", here.tier, f"user kickback {n} on {_name(here)}", here.model)

    # failed_checks
    failed = sum(a.outcome == "failed_checks" for a in _segment(stint))
    if failed >= limits.max_check_attempts:
        return up(
            rungs, idx, FLAG_CHECKS_FAILED, f"the checks failed {failed} times in a row", last
        )
    return Retry(
        last.trigger, here.tier, f"same-tier retry after failed checks on {_name(here)}", here.model
    )


def make_policy(ladder: Sequence[Rung], limits: Limits | None = None) -> Callable[..., Step | None]:
    """``next_step`` bound to one ladder (and limits): the worker's ``policy=`` callable.
    A call may still pass ``limits=`` to use one repo's profile."""
    return functools.partial(next_step, ladder=tuple(ladder), limits=limits)


# --------------------------------------------------------------------------- helpers


def _name(r: Rung) -> str:
    return f"tier {r.tier} ({r.model})"


def _start_index(routed_tier: int | None, rungs: Ladder) -> int:
    """The first rung at or above the routed tier; the top rung if none is."""
    if routed_tier is None:
        return 0
    for i, r in enumerate(rungs):
        if r.tier >= routed_tier:
            return i
    return len(rungs) - 1


def _start(h: History) -> int | None:
    """The tier the ticket starts on: the profile-shifted start, else the router's pick."""
    return h.start_tier if h.start_tier is not None else h.routed_tier


def _first(h: History, rungs: Ladder, *, local_only: bool = False) -> Retry:
    start = _start_index(_start(h), rungs)
    r = rungs[start]
    want = _start(h)
    if local_only and want is not None and r.tier < want:
        why = f"first attempt on {_name(r)}, the last local tier: the repo is local only "
        why += f"(cloud = false), so not tier {want}"
    elif h.start_tier is not None and h.start_tier != h.routed_tier:
        why = f"first attempt on {_name(r)}: the repo profile moved the start up from tier "
        why += f"{h.routed_tier}"
    elif h.routed_tier is None:
        why = f"first attempt on the lowest configured tier, {_name(r)}"
    elif r.tier == h.routed_tier:
        why = f"first attempt on the routed tier, {_name(r)}"
    else:
        why = f"first attempt: tier {h.routed_tier} has no model, so {_name(r)}"
    if h.explore:
        lower = [i for i, x in enumerate(rungs) if x.tier < r.tier]
        if lower:
            low_tier = rungs[lower[-1]].tier
            i = next(i for i in lower if rungs[i].tier == low_tier)
            return Retry(
                "explore",
                low_tier,
                f"explore: one tier below {_name(r)}, on {_name(rungs[i])}",
                rungs[i].model,
            )
        why += " (explore skipped: no lower tier is configured)"
    return Retry("first", r.tier, why, r.model)


def _rung_of(a: Attempt, rungs: Ladder) -> int:
    """The rung an attempt ran on. If the ladder changed since, the first rung of its tier,
    else the first rung above it, else the top rung."""
    for i, r in enumerate(rungs):
        if r.tier == a.tier and r.model == a.model:
            return i
    for i, r in enumerate(rungs):
        if r.tier >= a.tier:
            return i
    return len(rungs) - 1


def _stint(done: list[Attempt], idx: int, rungs: Ladder) -> list[Attempt]:
    """The trailing attempts that ran on rung ``idx`` (counts reset when the rung changes)."""
    start = len(done)
    while start > 0 and _rung_of(done[start - 1], rungs) == idx:
        start -= 1
    return done[start:]


def _segment(stint: list[Attempt]) -> list[Attempt]:
    """The stint after its last kicked-back attempt."""
    start = 0
    for i, a in enumerate(stint):
        if a.outcome == "kicked_back":
            start = i + 1
    return stint[start:]


Kick = tuple[KickbackBy, Tag | None]


def _kicks_by_attempt(h: History) -> dict[int, list[Kick]]:
    """Each kicked-back attempt's kickbacks, oldest first (see the module docstring)."""
    rows: dict[int, list[Kickback]] = {}
    for k in sorted(h.kickbacks, key=lambda k: k.id):
        rows.setdefault(k.attempt_id, []).append(k)
    asked: dict[int, Review] = {}
    for r in sorted(h.reviews, key=lambda r: r.id):
        if not r.shadow and r.verdict == "request_changes":
            asked.setdefault(r.attempt_id, r)
    out: dict[int, list[Kick]] = {}
    for a in h.attempts:
        if a.outcome != "kicked_back":
            continue
        own = rows.get(a.id, [])
        kicks: list[Kick] = []
        if a.id in asked and not any(k.by == "reviewer" for k in own):
            kicks.append(("reviewer", asked[a.id].tag))
        kicks += [(k.by, k.tag) for k in own]
        out[a.id] = kicks or [("user", None)]
    return out


def _count(stint: list[Attempt], kicks: dict[int, list[Kick]], by: KickbackBy) -> int:
    return sum(1 for a in stint for k in kicks.get(a.id, ()) if k[0] == by)


def _up(
    rungs: Ladder,
    idx: int,
    flag: str,
    what: str,
    last: Attempt,
    *,
    target: int | None = None,
    cloud_next: Ladder | None = None,
) -> Escalate | HandToUser:
    """Move up from rung ``idx``. ``cloud_next``: the full ladder of a local-only repo
    (``rungs`` is then its local rungs): past the last local rung, hand over with
    ``local_only`` when a cloud rung would have been next."""
    to = idx + 1 if target is None else target
    here = rungs[idx]
    if to >= len(rungs) and cloud_next is not None:
        above = [r for r in cloud_next if not r.local and r.tier >= here.tier]
        if above:
            return HandToUser(
                f"{FLAG_LOCAL_ONLY}: {flag}: {what} on {_name(here)}, the last local tier; "
                f"the repo is local only (cloud = false), so it does not move up to "
                f"{_name(above[0])}; over to you",
                mark_ready=bool(last.tests_passed),
            )
    if to >= len(rungs):
        return HandToUser(
            f"{flag}: {what} on {_name(here)}, the top of the ladder; over to you",
            mark_ready=bool(last.tests_passed),
        )
    there = rungs[to]
    trigger: Trigger = "escalation"
    return Escalate(
        trigger,
        there.tier,
        f"{flag}: {what} on {_name(here)}; moving up to {_name(there)}",
        there.model,
    )


__all__ = [
    "FLAG_AI_KICKBACK_CAP",
    "FLAG_ATTEMPT_BUDGET",
    "FLAG_CHECKS_FAILED",
    "FLAG_EXPLORE_FAILED",
    "FLAG_INTERRUPTED",
    "FLAG_KICKBACK_CAP",
    "FLAG_KICKBACK_WRONG",
    "FLAG_LOCAL_ONLY",
    "FLAG_NO_MODEL",
    "MAX_AI_KICKBACKS",
    "MAX_ATTEMPTS_PER_RUNG",
    "MAX_CHECK_ATTEMPTS",
    "MAX_INTERRUPTED",
    "MAX_USER_KICKBACKS",
    "SPEC_LADDER_WORST_CASE",
    "DEFAULT_LIMITS",
    "Escalate",
    "HandToUser",
    "History",
    "Ladder",
    "Limits",
    "Retry",
    "Rung",
    "Step",
    "attempt_bound",
    "build_ladder",
    "local_rungs",
    "make_policy",
    "next_step",
    "with_cloud",
]
