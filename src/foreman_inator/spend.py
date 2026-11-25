"""Spend caps (todo #18): cloud model spend can't run away (invariant 7c).

``Spend(store, config)`` enforces SPEC's caps on every cloud model call the proxy (#9)
sees. ``spend.hook`` has exactly the proxy's spend-hook signature, so the worker passes
it straight to ``ModelProxy(spend_hook=spend.hook)``.

Rules as implemented:

* **What counts.** Only endpoints with ``kind == "cloud"``. Local models are always
  allowed and write no ledger row (the ledger would otherwise grow by thousands of zero
  rows a day). A cloud call whose actual cost is 0 (refused upstream) writes no row.
* **Buckets.** ``live`` has a daily and a monthly cap. ``experiments`` (replays, shadow
  reviews) has only its own monthly budget and never draws on, or is blocked by, the
  live caps. Over its budget an experiment call is refused and nobody is asked.
* **Windows** are the user's local calendar day and month (the system time zone, or
  ``tz=``), converted to UTC to query the store, whose timestamps are UTC. A DST day is
  23 or 25 hours long; the month starts at local midnight on the 1st.
* **Boundary.** A call is allowed when ``spent + reserved + estimate <= cap``: reaching
  the cap exactly is allowed, a cent over is not. A cap of 0 turns cloud calls off.
* **Reservation.** An allowed estimate is reserved (in memory, under a lock) until the
  proxy reports the actual cost, which is then written to the ledger and the
  reservation released, so N parallel calls cannot each see the same headroom. A
  reservation the proxy never settles expires after ``reservation_ttl``. Worst-case
  overshoot: the sum over in-flight calls of (actual - estimate) where positive. The
  proxy's estimate is deliberately high (prompt chars/3 tokens in, ``max_tokens`` or
  4,096 out), so it is only exceeded by output past 4,096 tokens with no ``max_tokens``
  set; plus whatever a user approval grants (below). Reservations are per process: a
  second process with its own proxy sees the ledger but not this one's reservations.
* **Approval** (invariant 7e). At the live cap, a call is refused before it is sent and
  the refusal is remembered (``refusal_for_attempt`` / ``refusal_for_ticket``). The
  worker pauses the ticket (flag reason ``spend_cap``), comments ``approval_comment``
  on the issue, and the user runs ``fman approve-spend <ticket>`` (``approve``). One
  approval grants **one attempt** of that ticket (its review included) up to
  ``approval_usd`` (default: the daily cap) of cloud spend past the caps. It is consumed
  by that ticket's first call refused by the caps and covers only that attempt; another
  over-cap attempt, or that attempt going past ``approval_usd``, is refused again and
  the worker asks again. Approving twice before one is used is one approval. So every
  dollar past the cap follows a user command, and nothing retries silently.
* **Thrifty approval** (#51). A repo whose profile has ``writer = thrifty`` makes every
  cloud writer attempt wait for the user, caps or not: ``writer_gate(ticket_id,
  approval_required=True)`` returns a Refusal with cap ``thrifty`` (flag reason
  ``spend_cap: thrifty ...``), and the worker pauses and asks exactly as at a cap. The
  same ``fman approve-spend`` grants one attempt: when the caps have headroom the gate
  consumes the approval itself (so the next cloud attempt asks again); when a cap is also
  reached it leaves the approval for the cap path above, which consumes it on the
  attempt's first over-cap call, so one approval still covers one attempt. A
  ``thrifty`` pause resumes only on an approval, never because the caps cleared.
* **Resume.** A paused ticket can resume when it has an open approval, or when the live
  caps have headroom again (a new local day or month, or a raised cap):
  ``resume_reason``.
* **Per-attempt budget** (#58). One writer attempt (live calls with an ``attempt_id`` and
  no ``review_id``) may spend at most ``attempt_budget_usd`` on cloud models, and may
  send at most ``attempt_max_input_tokens_cloud`` / ``_local`` input tokens (by the
  endpoint's kind; local calls are checked too, though they cost nothing). The running
  totals are the attempt row's ``cost_usd`` and ``tokens_in``, which the proxy adds
  before it settles a call here. A call is refused (cap ``attempt``, before it is sent)
  when ``cost_usd + reserved + estimate > attempt_budget_usd`` (the same boundary as the
  caps) or when ``tokens_in`` has already reached the token limit. The refusal is not a
  pause and asks nobody (``needs_approval`` is False): ``budget_stop(attempt_id)`` tells
  the worker, which ends the attempt with ``end_reason = 'budget'``, and escalation hands
  the ticket to the user (flag ``attempt_budget: ...``) with no retry on any tier.
  Worst-case overshoot, for one attempt (its writer calls run one at a time): the USD
  check uses the proxy's high estimate, so the attempt's cost passes the budget by at
  most one call's (actual - estimate), which is zero unless that call returned more than
  its estimated output tokens; the token check sees only calls already made, so input
  tokens pass the limit by at most one call's input. ``None`` turns a limit off, and is
  the constructor's default (so a bare ``Spend`` keeps the old behaviour); the worker
  passes the config's values, whose defaults are the ``DEFAULT_ATTEMPT_*`` constants.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from foreman_inator import config as config_defaults
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import NotFoundError, SpendApproval, SpendEntry, Store, Ticket

log = logging.getLogger(__name__)

Bucket = Literal["live", "experiments"]
CapName = Literal["daily", "monthly", "experiments_monthly", "approval", "attempt", "thrifty"]
BUCKETS: tuple[Bucket, ...] = ("live", "experiments")

#: Flag reason for a ticket paused at the cap (``store.flag_ticket(id, f"{FLAG_SPEND_CAP}: ...")``).
FLAG_SPEND_CAP = "spend_cap"
#: An unsettled reservation is dropped after this long (proxy upstream timeout is 600 s).
DEFAULT_RESERVATION_TTL = timedelta(minutes=15)
#: Flag reason prefix for a ticket handed over after an attempt reached its budget (#58).
FLAG_ATTEMPT_BUDGET = "attempt_budget"
#: #51: the flag reason of a ticket paused by the ``thrifty`` writer mode (a spend_cap
#: pause, so the status and resume paths treat it like one).
FLAG_THRIFTY = f"{FLAG_SPEND_CAP}: thrifty profile"
#: Per-attempt limits (#58), defined in ``config`` (whose keys default to them) and
#: re-exported here. The live run that prompted #58 spent $4.78 and 3.34M input tokens
#: in one tier 3 attempt.
DEFAULT_ATTEMPT_BUDGET_USD = config_defaults.DEFAULT_ATTEMPT_BUDGET_USD
DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD = config_defaults.DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD
DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL = config_defaults.DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL
_EPS = 1e-9  # float slack so 4.0 + 1.0 <= 5.0 holds after summing ledger REALs


class SpendError(Exception):
    """An approval could not be given (unknown or finished ticket)."""


# =========================================================================== values


@dataclass(frozen=True)
class Refusal:
    """A cloud call (or a would-be attempt) refused by a cap."""

    bucket: Bucket
    cap: CapName
    cap_usd: float
    spent_usd: float  # in the cap's window (for ``approval``: spent under the grant)
    reserved_usd: float
    estimate_usd: float
    resets_at: datetime | None  # UTC end of the cap's window; None for ``approval``
    at: datetime
    ticket_id: int | None = None
    attempt_id: int | None = None
    review_id: int | None = None
    model: str | None = None
    # cap "attempt" only (#58): the attempt's input tokens and the limit that refused it
    # (None when the dollar budget refused it).
    tokens_in: int | None = None
    token_limit: int | None = None

    @property
    def needs_approval(self) -> bool:
        """True for live spend tied to a ticket: the worker pauses it and asks the user.
        Experiment refusals never ask (replays just stop); nor does an attempt budget
        stop (the attempt ends and escalation hands the ticket over)."""
        return self.bucket == "live" and self.ticket_id is not None and self.cap != "attempt"

    @property
    def detail(self) -> str:
        """For cap ``attempt``: what the attempt used against which limit."""
        if self.token_limit is not None:
            return f"sent {self.tokens_in or 0:,} input tokens of {self.token_limit:,}"
        return f"spent ${self.spent_usd:.2f} of ${self.cap_usd:.2f}"

    @property
    def flag_reason(self) -> str:
        if self.cap == "attempt":
            return f"{FLAG_ATTEMPT_BUDGET}: {self.detail}"
        if self.cap == "thrifty":
            return f"{FLAG_THRIFTY}: a cloud writer attempt waits for approval"
        return f"{FLAG_SPEND_CAP}: {self.cap} cap ${self.cap_usd:.2f} reached"


@dataclass(frozen=True)
class SpendSummary:
    """Read-only numbers for `fman status` and the metrics page."""

    now: datetime
    day_start: datetime  # UTC instants of the local day/month windows
    day_end: datetime
    month_start: datetime
    month_end: datetime
    live_today_usd: float
    live_month_usd: float
    experiments_month_usd: float
    reserved_live_usd: float
    reserved_experiments_usd: float
    daily_cap_usd: float
    monthly_cap_usd: float
    experiments_cap_usd: float
    remaining_today_usd: float  # min(daily, monthly) headroom for live spend, >= 0
    remaining_month_usd: float
    remaining_experiments_usd: float
    live_blocked: bool
    experiments_blocked: bool
    open_approvals: int

    def as_dict(self) -> dict[str, object]:
        out = asdict(self)
        for k, v in out.items():
            if isinstance(v, datetime):
                out[k] = v.isoformat()
        return out

    def status_line(self) -> str:
        line = (
            f"cloud spend: ${self.live_today_usd:.2f} of ${self.daily_cap_usd:.2f} today, "
            f"${self.live_month_usd:.2f} of ${self.monthly_cap_usd:.2f} this month; "
            f"experiments ${self.experiments_month_usd:.2f} of "
            f"${self.experiments_cap_usd:.2f} this month"
        )
        if self.live_blocked:
            line += " (live cap reached: tier 3 waits for `fman approve-spend`)"
        return line


@dataclass
class _Reservation:
    amount: float
    at: datetime
    attempt_id: int | None


@dataclass
class _Grant:
    approval_id: int
    ticket_id: int
    limit_usd: float
    spent_usd: float = 0.0
    reserved_usd: float = 0.0


@dataclass
class _Who:
    ticket_id: int | None
    attempt_id: int | None  # only when the attempts row exists (ledger foreign key)
    review_id: int | None


@dataclass
class _State:
    reservations: dict[tuple, deque[_Reservation]] = field(default_factory=dict)
    grants: dict[int, _Grant] = field(default_factory=dict)  # attempt_id -> grant


# =========================================================================== Spend


class Spend:
    """Cloud spend caps over the store's ``spend_ledger`` / ``spend_approvals`` tables."""

    def __init__(
        self,
        store: Store,
        config: Config | SpendCaps,
        *,
        clock=None,
        tz: tzinfo | str | None = None,
        approval_usd: float | None = None,
        reservation_ttl: timedelta = DEFAULT_RESERVATION_TTL,
        attempt_budget_usd: float | None = None,
        attempt_max_input_tokens_cloud: int | None = None,
        attempt_max_input_tokens_local: int | None = None,
    ) -> None:
        self.store = store
        self.caps = config.spend_caps if isinstance(config, Config) else config
        self._clock = clock or store.now
        self.tz: tzinfo | None = ZoneInfo(tz) if isinstance(tz, str) else tz
        if approval_usd is None:
            approval_usd = self.caps.daily_usd if self.caps.daily_usd > 0 else SpendCaps.daily_usd
        self.approval_usd = float(approval_usd)
        self.reservation_ttl = reservation_ttl
        self._lock = threading.Lock()
        self._state = _State()
        self._attempt_ticket: dict[int, int | None] = {}
        self._refusals_by_attempt: dict[int, Refusal] = {}
        self._refusals_by_ticket: dict[int, Refusal] = {}
        self.attempt_budget_usd = None if attempt_budget_usd is None else float(attempt_budget_usd)
        self.attempt_max_input_tokens = {
            "cloud": attempt_max_input_tokens_cloud,
            "local": attempt_max_input_tokens_local,
        }
        self._budget_stops: dict[int, Refusal] = {}

    # ------------------------------------------------------------------ windows

    def now(self) -> datetime:
        now = self._clock()
        return now if now.tzinfo else now.replace(tzinfo=UTC)

    def _local_midnight(self, d: date) -> datetime:
        if self.tz is None:  # naive local time: astimezone() applies the system zone + DST
            return datetime(d.year, d.month, d.day).astimezone(UTC)
        return datetime(d.year, d.month, d.day, tzinfo=self.tz).astimezone(UTC)

    def _local_date(self, now: datetime) -> date:
        return (now.astimezone() if self.tz is None else now.astimezone(self.tz)).date()

    def day_window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """The local calendar day containing ``now``, as UTC [start, end)."""
        d = self._local_date(now or self.now())
        return self._local_midnight(d), self._local_midnight(d + timedelta(days=1))

    def month_window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """The local calendar month containing ``now``, as UTC [start, end)."""
        first = self._local_date(now or self.now()).replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return self._local_midnight(first), self._local_midnight(nxt)

    def _total(self, bucket: Bucket, window: tuple[datetime, datetime]) -> float:
        return self.store.spend_total(bucket, since=window[0], until=window[1])

    # ------------------------------------------------------------------ cap checks

    def _reserved(self, bucket: Bucket, now: datetime) -> float:
        """Sum of unexpired reservations in ``bucket``; drops expired ones."""
        total = 0.0
        cutoff = now - self.reservation_ttl
        for key, q in list(self._state.reservations.items()):
            while q and q[0].at < cutoff:
                stale = q.popleft()
                self._release_grant(stale)
                log.warning("spend: reservation of $%.4f expired unsettled", stale.amount)
            if not q:
                del self._state.reservations[key]
            elif key[0] == bucket:
                total += sum(r.amount for r in q)
        return total

    def _live_caps(self, now: datetime) -> list[tuple[CapName, float, tuple[datetime, datetime]]]:
        return [
            ("monthly", self.caps.monthly_usd, self.month_window(now)),
            ("daily", self.caps.daily_usd, self.day_window(now)),
        ]

    def _cap_check(
        self, estimate: float, bucket: Bucket, now: datetime, reserved: float
    ) -> Refusal | None:
        """The first cap ``estimate`` would cross, or None. Monthly is reported over daily
        when both are crossed (it lasts longer)."""
        if bucket == "live":
            checks = self._live_caps(now)
        elif bucket == "experiments":
            month = self.month_window(now)
            checks = [("experiments_monthly", self.caps.experiments_monthly_usd, month)]
        else:
            raise ValueError(f"unknown spend bucket {bucket!r}")
        for cap, cap_usd, window in checks:
            spent = self._total(bucket, window)
            if not _fits(spent + reserved, estimate, cap_usd):
                return Refusal(
                    bucket=bucket,
                    cap=cap,
                    cap_usd=cap_usd,
                    spent_usd=spent,
                    reserved_usd=reserved,
                    estimate_usd=estimate,
                    resets_at=window[1],
                    at=now,
                )
        return None

    def allow(self, cost_estimate: float, bucket: Bucket = "live") -> bool:
        """Would a cloud call costing ``cost_estimate`` fit inside ``bucket``'s caps right
        now (counting in-flight reservations)? No side effects; no approvals."""
        with self._lock:
            now = self.now()
            return self._cap_check(cost_estimate, bucket, now, self._reserved(bucket, now)) is None

    def record(
        self,
        cost: float,
        bucket: Bucket = "live",
        *,
        model: str | None = None,
        ticket_id: int | None = None,
        attempt_id: int | None = None,
        experiment_id: int | None = None,
    ) -> SpendEntry | None:
        """Write spend that happened to the ledger. Zero cost writes nothing."""
        if bucket not in BUCKETS:
            raise ValueError(f"unknown spend bucket {bucket!r}")
        if cost <= 0:
            return None
        return self.store.record_spend(
            bucket,
            cost,
            model=model,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            experiment_id=experiment_id,
        )

    # ------------------------------------------------------------------ the proxy hook

    def hook(
        self, endpoint: ModelEndpoint, cost_usd: float, context, *, phase: str = "estimate"
    ) -> bool | None:
        """``ModelProxy(spend_hook=spend.hook)``. ``estimate``: allow (and reserve) or refuse
        before anything is sent. ``actual``: settle the reservation and record the cost."""
        if endpoint.kind != "cloud":
            if phase == "estimate":
                return self._local_estimate(endpoint, context)
            return None
        bucket = getattr(context, "bucket", "live")
        if phase == "estimate":
            return self._estimate(endpoint, float(cost_usd), context, bucket)
        self._actual(endpoint, float(cost_usd), context, bucket)
        return None

    @staticmethod
    def _key(endpoint: ModelEndpoint, ctx, bucket: str) -> tuple:
        return (
            bucket,
            getattr(ctx, "kind", None),
            getattr(ctx, "attempt_id", None),
            getattr(ctx, "review_id", None),
            getattr(ctx, "token", None),
            endpoint.name,
        )

    def _who(self, ctx) -> _Who:
        attempt_id = getattr(ctx, "attempt_id", None)
        review_id = getattr(ctx, "review_id", None)
        if attempt_id is None and review_id is not None:
            try:
                attempt_id = self.store.get_review(review_id).attempt_id
            except NotFoundError:
                attempt_id = None
        if attempt_id is None:
            return _Who(None, None, review_id)
        if attempt_id not in self._attempt_ticket:
            try:
                self._attempt_ticket[attempt_id] = self.store.get_attempt(attempt_id).ticket_id
            except NotFoundError:
                self._attempt_ticket[attempt_id] = None
        ticket_id = self._attempt_ticket[attempt_id]
        return _Who(ticket_id, attempt_id if ticket_id is not None else None, review_id)

    def _estimate(self, ep: ModelEndpoint, estimate: float, ctx, bucket: str) -> bool:
        if bucket not in BUCKETS:
            log.warning("spend: unknown bucket %r; refusing", bucket)
            return False
        with self._lock:
            now = self.now()
            who = self._who(ctx)
            reserved = self._reserved(bucket, now)
            stop = self._attempt_check(ep, estimate, ctx, bucket, who, now)
            if stop is not None:
                return self._stop(stop, who, ep)
            refusal = self._cap_check(estimate, bucket, now, reserved)
            granted = False
            if refusal is not None and bucket == "live" and who.attempt_id is not None:
                refusal, granted = self._try_grant(refusal, who, estimate)
            if refusal is not None:
                refusal = _with_who(refusal, who, ep.name)
                if who.attempt_id is not None:
                    self._refusals_by_attempt[who.attempt_id] = refusal
                if who.ticket_id is not None and bucket == "live":
                    self._refusals_by_ticket[who.ticket_id] = refusal
                log.info("spend: refused %s call (%s cap)", ep.name, refusal.cap)
                return False
            key = self._key(ep, ctx, bucket)
            res = _Reservation(estimate, now, who.attempt_id if granted else None)
            self._state.reservations.setdefault(key, deque()).append(res)
            return True

    def _local_estimate(self, ep: ModelEndpoint, ctx) -> bool:
        """A local call: only the attempt's input-token limit can refuse it (#58)."""
        if self.attempt_max_input_tokens.get(ep.kind) is None:
            return True
        bucket = getattr(ctx, "bucket", "live")
        with self._lock:
            now = self.now()
            who = self._who(ctx)
            stop = self._attempt_check(ep, 0.0, ctx, bucket, who, now)
            return True if stop is None else self._stop(stop, who, ep)

    def _attempt_check(
        self, ep: ModelEndpoint, estimate: float, ctx, bucket: str, who: _Who, now: datetime
    ) -> Refusal | None:
        """The per-attempt budget (#58) for a writer call, or None when it fits."""
        if bucket != "live" or who.attempt_id is None or getattr(ctx, "review_id", None):
            return None
        token_limit = self.attempt_max_input_tokens.get(ep.kind)
        budget = self.attempt_budget_usd if ep.kind == "cloud" else None
        if token_limit is None and budget is None:
            return None
        try:
            att = self.store.get_attempt(who.attempt_id)
        except NotFoundError:
            return None
        reserved = self._attempt_reserved(who.attempt_id)
        base = Refusal(
            bucket="live",
            cap="attempt",
            cap_usd=budget if budget is not None else 0.0,
            spent_usd=float(att.cost_usd or 0.0),
            reserved_usd=reserved,
            estimate_usd=estimate,
            resets_at=None,
            at=now,
            tokens_in=int(att.tokens_in or 0),
        )
        if token_limit is not None and int(att.tokens_in or 0) >= token_limit:
            return replace(base, token_limit=int(token_limit))
        if budget is not None and not _fits(base.spent_usd + reserved, estimate, budget):
            return base
        return None

    def _attempt_reserved(self, attempt_id: int) -> float:
        """Unsettled live writer-call reservations of one attempt (keys from ``_key``)."""
        return sum(
            r.amount
            for key, q in self._state.reservations.items()
            if key[0] == "live" and key[2] == attempt_id and key[3] is None
            for r in q
        )

    def _stop(self, stop: Refusal, who: _Who, ep: ModelEndpoint) -> bool:
        stop = _with_who(stop, who, ep.name)
        assert who.attempt_id is not None
        self._budget_stops[who.attempt_id] = stop
        log.info("spend: refused %s call: attempt %s %s", ep.name, who.attempt_id, stop.detail)
        return False

    def _try_grant(
        self, refusal: Refusal, who: _Who, estimate: float
    ) -> tuple[Refusal | None, bool]:
        """Past the cap: let the call through on the attempt's approval grant, consuming
        the ticket's open approval when the attempt has no grant or has used it up."""
        assert who.attempt_id is not None and who.ticket_id is not None
        grant = self._state.grants.get(who.attempt_id)
        if grant is not None and _fits(
            grant.spent_usd + grant.reserved_usd, estimate, grant.limit_usd
        ):
            grant.reserved_usd += estimate
            return None, True
        over_grant = replace(
            refusal,
            cap="approval",
            cap_usd=self.approval_usd,
            spent_usd=grant.spent_usd if grant else 0.0,
            reserved_usd=grant.reserved_usd if grant else 0.0,
            resets_at=None,
        )
        if not _fits(0.0, estimate, self.approval_usd):
            return over_grant, False  # one call bigger than a whole approval: never consume
        approval = self.store.open_spend_approval(who.ticket_id)
        if approval is None or not self.store.consume_spend_approval(approval.id):
            return (over_grant if grant is not None else refusal), False
        log.info("spend: approval %s used for attempt %s", approval.id, who.attempt_id)
        self._state.grants[who.attempt_id] = _Grant(
            approval.id, who.ticket_id, self.approval_usd, reserved_usd=estimate
        )
        return None, True

    def _release_grant(self, res: _Reservation) -> _Grant | None:
        if res.attempt_id is None:
            return None
        grant = self._state.grants.get(res.attempt_id)
        if grant is not None:
            grant.reserved_usd = max(0.0, grant.reserved_usd - res.amount)
        return grant

    def _actual(self, ep: ModelEndpoint, cost: float, ctx, bucket: str) -> None:
        if bucket not in BUCKETS:
            bucket = "live"  # spend happened; never lose it
        with self._lock:
            key = self._key(ep, ctx, bucket)
            q = self._state.reservations.get(key)
            res = q.popleft() if q else None
            if q is not None and not q:
                del self._state.reservations[key]
            grant = self._release_grant(res) if res else None
            if grant is not None:
                grant.spent_usd += max(cost, 0.0)
            who = self._who(ctx)
        self.record(cost, bucket, model=ep.name, ticket_id=who.ticket_id, attempt_id=who.attempt_id)

    # ------------------------------------------------------------------ worker pieces

    def refusal_for_attempt(self, attempt_id: int) -> Refusal | None:
        """The last cap refusal of a call in this attempt (in memory; lost on restart)."""
        with self._lock:
            return self._refusals_by_attempt.get(attempt_id)

    def budget_stop(self, attempt_id: int) -> Refusal | None:
        """#58: the refusal that stopped this attempt at its own budget (cap ``attempt``),
        or None. In memory; an attempt cut by a restart is superseded anyway."""
        with self._lock:
            return self._budget_stops.get(attempt_id)

    def refusal_for_ticket(self, ticket_id: int) -> Refusal | None:
        with self._lock:
            return self._refusals_by_ticket.get(ticket_id)

    def clear_refusals(self, ticket_id: int) -> None:
        """Forget a ticket's refusals (after it is paused, or resumes)."""
        with self._lock:
            self._refusals_by_ticket.pop(ticket_id, None)
            for aid, r in list(self._refusals_by_attempt.items()):
                if r.ticket_id == ticket_id:
                    del self._refusals_by_attempt[aid]

    def blocked(self, ticket_id: int | None = None) -> Refusal | None:
        """Before starting a cloud (tier 3) attempt: is a live cap reached (no headroom
        left) with no open approval for ``ticket_id``? A Refusal means pause and ask."""
        with self._lock:
            now = self.now()
            reserved = self._reserved("live", now)
            refusal = None
            for cap, cap_usd, window in self._live_caps(now):
                spent = self._total("live", window)
                if spent + reserved >= cap_usd - _EPS:
                    refusal = Refusal("live", cap, cap_usd, spent, reserved, 0.0, window[1], now)
                    break
        if refusal is None:
            return None
        if ticket_id is not None and self.store.open_spend_approval(ticket_id) is not None:
            return None
        return _with_who(refusal, _Who(ticket_id, None, None), None)

    def writer_gate(self, ticket_id: int, *, approval_required: bool = False) -> Refusal | None:
        """Before a cloud writer attempt: ``blocked(ticket_id)``, plus, for a ``thrifty``
        repo (``approval_required``), the profile's own approval (see the module
        docstring). None: start the attempt. Without ``approval_required`` this is
        exactly ``blocked``."""
        if not approval_required:
            return self.blocked(ticket_id)
        capped = self.blocked(None)
        approval = self.store.open_spend_approval(ticket_id)
        if approval is None:
            now = self.now()
            return Refusal("live", "thrifty", 0.0, 0.0, 0.0, 0.0, None, now, ticket_id=ticket_id)
        if capped is None and self.store.consume_spend_approval(approval.id):
            log.info("spend: approval %s used for a thrifty cloud attempt", approval.id)
        return None

    def approve(self, ticket_id: int) -> SpendApproval:
        """`fman approve-spend <ticket>`: one more attempt past the cap, up to
        ``approval_usd``. Idempotent while an approval is still unused."""
        try:
            ticket = self.store.get_ticket(ticket_id)
        except NotFoundError as exc:
            raise SpendError(f"no ticket {ticket_id}") from exc
        if ticket.column in ("done", "dropped"):
            raise SpendError(f"ticket {ticket_id} is {ticket.column}; nothing to approve")
        existing = self.store.open_spend_approval(ticket_id)
        return existing if existing is not None else self.store.approve_spend(ticket_id)

    def resume_reason(self, ticket: Ticket) -> Literal["approved", "cap_clear"] | None:
        """For a ticket paused at the cap: why it may run again now, or None to keep
        waiting. Tickets not flagged ``spend_cap`` return None."""
        if not (ticket.flag_reason or "").startswith(FLAG_SPEND_CAP):
            return None
        if self.store.open_spend_approval(ticket.id) is not None:
            return "approved"
        if (ticket.flag_reason or "").startswith(FLAG_THRIFTY):
            return None  # #51: only the user's approval lifts a thrifty pause
        return "cap_clear" if self.blocked(None) is None else None

    def approval_comment(self, ticket: Ticket, refusal: Refusal) -> str:
        """The issue comment asking for approval. Amounts and commands only."""
        marker = f"<!-- foreman-inator:flag ticket={ticket.id} -->"
        if refusal.cap == "thrifty":
            why = (
                "this repo's profile is thrifty, so every cloud writer attempt waits for "
                "your approval"
            )
        elif refusal.cap == "approval":
            why = (
                f"the attempt used the ${refusal.cap_usd:.2f} of cloud spend your last "
                "approval allowed"
            )
        else:
            label = {"daily": "daily", "monthly": "monthly"}.get(refusal.cap, refusal.cap)
            window = "today" if refusal.cap == "daily" else "this month"
            why = (
                f"the {label} cloud spend cap (${refusal.cap_usd:.2f}) is reached "
                f"(${refusal.spent_usd:.2f} spent {window})"
            )
        when = ""
        if refusal.resets_at is not None:
            local = (
                refusal.resets_at.astimezone()
                if self.tz is None
                else refusal.resets_at.astimezone(self.tz)
            )
            when = f" It resumes on its own when the cap resets ({local:%Y-%m-%d %H:%M %Z})."
        return (
            f"{marker}\nforeman-inator paused work on this issue: {why}. The next cloud "
            "(tier 3) attempt waits for your approval; nothing was sent past the cap.\n\n"
            f"To allow one more attempt (up to ${self.approval_usd:.2f} of cloud spend), run "
            "on the worker's Mac:\n\n"
            f"    fman approve-spend {ticket.id}\n\n"
            f"Each approval covers one attempt.{when}"
        )

    # ------------------------------------------------------------------ summaries

    def summary(self) -> SpendSummary:
        with self._lock:
            now = self.now()
            day, month = self.day_window(now), self.month_window(now)
            today = self._total("live", day)
            live_month = self._total("live", month)
            exp_month = self._total("experiments", month)
            r_live = self._reserved("live", now)
            r_exp = self._reserved("experiments", now)
        caps = self.caps
        rem_month = max(0.0, caps.monthly_usd - live_month - r_live)
        rem_today = max(0.0, min(caps.daily_usd - today - r_live, rem_month))
        rem_exp = max(0.0, caps.experiments_monthly_usd - exp_month - r_exp)
        open_approvals = self.store.query(
            "SELECT COUNT(*) AS n FROM spend_approvals WHERE consumed_at IS NULL"
        )[0]["n"]
        return SpendSummary(
            now=now,
            day_start=day[0],
            day_end=day[1],
            month_start=month[0],
            month_end=month[1],
            live_today_usd=today,
            live_month_usd=live_month,
            experiments_month_usd=exp_month,
            reserved_live_usd=r_live,
            reserved_experiments_usd=r_exp,
            daily_cap_usd=caps.daily_usd,
            monthly_cap_usd=caps.monthly_usd,
            experiments_cap_usd=caps.experiments_monthly_usd,
            remaining_today_usd=rem_today,
            remaining_month_usd=rem_month,
            remaining_experiments_usd=rem_exp,
            live_blocked=rem_today <= _EPS,
            experiments_blocked=rem_exp <= _EPS,
            open_approvals=int(open_approvals),
        )


def _fits(used: float, estimate: float, cap: float) -> bool:
    """THE boundary: reaching a cap exactly is allowed, going past it is not."""
    return used + estimate <= cap + _EPS


def _with_who(refusal: Refusal, who: _Who, model: str | None) -> Refusal:
    return replace(
        refusal,
        ticket_id=who.ticket_id,
        attempt_id=who.attempt_id,
        review_id=who.review_id,
        model=model,
    )


__all__ = [
    "BUCKETS",
    "DEFAULT_ATTEMPT_BUDGET_USD",
    "DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD",
    "DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL",
    "DEFAULT_RESERVATION_TTL",
    "FLAG_ATTEMPT_BUDGET",
    "FLAG_SPEND_CAP",
    "FLAG_THRIFTY",
    "Refusal",
    "Spend",
    "SpendError",
    "SpendSummary",
]
