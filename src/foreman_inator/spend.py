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
* **Resume.** A paused ticket can resume when it has an open approval, or when the live
  caps have headroom again (a new local day or month, or a raised cap):
  ``resume_reason``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import NotFoundError, SpendApproval, SpendEntry, Store, Ticket

log = logging.getLogger(__name__)

Bucket = Literal["live", "experiments"]
CapName = Literal["daily", "monthly", "experiments_monthly", "approval"]
BUCKETS: tuple[Bucket, ...] = ("live", "experiments")

#: Flag reason for a ticket paused at the cap (``store.flag_ticket(id, f"{FLAG_SPEND_CAP}: ...")``).
FLAG_SPEND_CAP = "spend_cap"
#: An unsettled reservation is dropped after this long (proxy upstream timeout is 600 s).
DEFAULT_RESERVATION_TTL = timedelta(minutes=15)
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

    @property
    def needs_approval(self) -> bool:
        """True for live spend tied to a ticket: the worker pauses it and asks the user.
        Experiment refusals never ask (replays just stop)."""
        return self.bucket == "live" and self.ticket_id is not None

    @property
    def flag_reason(self) -> str:
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
            return True if phase == "estimate" else None
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
            refusal = self._cap_check(estimate, bucket, now, self._reserved(bucket, now))
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
        return "cap_clear" if self.blocked(None) is None else None

    def approval_comment(self, ticket: Ticket, refusal: Refusal) -> str:
        """The issue comment asking for approval. Amounts and commands only."""
        marker = f"<!-- foreman-inator:flag ticket={ticket.id} -->"
        if refusal.cap == "approval":
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
    "DEFAULT_RESERVATION_TTL",
    "FLAG_SPEND_CAP",
    "Refusal",
    "Spend",
    "SpendError",
    "SpendSummary",
]
