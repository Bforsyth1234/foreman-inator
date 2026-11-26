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
* **Flat-fee plans.** A cloud model with ``plan_usd_month`` set (the GLM Coding Plan)
  costs nothing per call, so its calls skip the daily, monthly and experiments caps,
  approvals and the per-attempt USD budget; they reserve nothing. Each call is still
  written to the ledger at its list price with ``plan = 1``, which ``spend_total``
  leaves out, so ``summary().plans`` can show what the plan would have cost metered.
  The local-only rule and the per-attempt input-token limit still apply. Chat calls
  to a plan model skip the chat turn budget and the chat sub-cap too
  (``chat_spend_total`` leaves plan rows out).
* **Local only** (#49). A repo set ``cloud = false`` (``repo_settings.cloud = 0``,
  ``Store.set_cloud``) never sends its code to a cloud model. Escalation already stops at
  its last local tier and review picks a local reviewer; as defence in depth (invariant
  7c), every cloud call the proxy attributes to one of that repo's tickets (through its
  attempt, or its review's attempt), in either bucket, is refused here before it is sent
  (cap ``local_only``). No approval can lift it (``needs_approval`` is False) and no
  ledger row is written; ``refusal_for_attempt`` remembers it for the logs. The setting
  is read from the store on every call, so a change takes effect at once.
* **Chat** (todo #71, ``docs/chat-design.md`` sections 5 and 8, option C). A call whose
  context carries a ``chat_session_id`` (read with ``getattr``) is a chat call and takes a
  separate path; every ticket rule above is untouched. The chat manager registers each
  session (``register_chat_session``: repo, current model, ``small_model``), starts each
  turn (``start_chat_turn``) and moves tiers (``set_chat_model``, next turn only). Chat
  spend is bucket ``live``, so it counts toward ``daily_usd`` and ``monthly_usd``, and at
  most ``chat_monthly_usd`` (default $20) of the month's live spend may be chat (cap
  ``chat_monthly``). In order, a chat call is refused before it is sent when: the session
  is not registered, or the endpoint is neither the turn's model nor the session's
  ``small_model`` (cap ``chat_model``, local calls too); a cloud call's repo is local only
  (cap ``local_only``, read from ``store.repo_allows_cloud(repo)`` on every call, no
  approval lifts it); the turn's cloud spend plus reservations plus the estimate passes
  ``chat_turn_budget_usd`` (default = ``attempt_budget_usd``, $1.50; cap ``chat_turn``,
  not approvable: the turn is aborted, the next message starts a new budget); a live cap
  or the chat sub-cap would be crossed (caps ``monthly`` / ``chat_monthly`` / ``daily``).
  At a cap, ``approve_chat(session_id)`` (``/approve``) holds one approval in memory
  (``spend_approvals`` needs a ticket id); the turn's first over-cap call consumes it and
  the rest of that turn may spend up to ``approval_usd`` past the caps; a new turn, or
  the turn passing ``approval_usd``, is refused again. ``refusal_for_chat`` gives the last
  refusal. Ledger rows carry ``chat_session_id``. ``chat_cloud_fallback`` (decision 2)
  picks the cloud rung a turn may use while the local slot is busy, or None.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from foreman_inator import config as config_defaults
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import NotFoundError, SpendApproval, SpendEntry, Store, Ticket

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from foreman_inator.chat import ChatSession, Rung

log = logging.getLogger(__name__)

Bucket = Literal["live", "experiments"]
CapName = Literal[
    "daily",
    "monthly",
    "experiments_monthly",
    "approval",
    "attempt",
    "thrifty",
    "local_only",
    "chat_monthly",
    "chat_turn",
    "chat_model",
]
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
#: #49: a cloud call refused because the ticket's repo is local only (``cloud = false``).
FLAG_LOCAL_ONLY = "local_only"
#: Per-attempt limits (#58), defined in ``config`` (whose keys default to them) and
#: re-exported here. The live run that prompted #58 spent $4.78 and 3.34M input tokens
#: in one tier 3 attempt.
DEFAULT_ATTEMPT_BUDGET_USD = config_defaults.DEFAULT_ATTEMPT_BUDGET_USD
DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD = config_defaults.DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD
DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL = config_defaults.DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL
#: Chat (todo #71, option C): the most of the month's live spend chat may take, and the
#: cloud spend one chat message (turn) may use. The wiring step adds the config keys
#: ``spend_caps.chat_monthly_usd`` and ``chat_turn_budget_usd`` with these defaults.
DEFAULT_CHAT_MONTHLY_USD = config_defaults.DEFAULT_CHAT_MONTHLY_USD
DEFAULT_CHAT_TURN_BUDGET_USD = DEFAULT_ATTEMPT_BUDGET_USD
#: Caps a chat ``/approve`` can lift (the others stop the turn whatever the user says).
_CHAT_APPROVABLE: frozenset[str] = frozenset({"daily", "monthly", "chat_monthly", "approval"})
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
    chat_session_id: int | None = None  # a chat call (todo #71)

    @property
    def needs_approval(self) -> bool:
        """True for live spend tied to a ticket: the worker pauses it and asks the user.
        Experiment refusals never ask (replays just stop); nor does an attempt budget
        stop (the attempt ends and escalation hands the ticket over). For a chat call:
        True at a live cap or the chat sub-cap (the user types ``/approve``)."""
        if self.chat_session_id is not None:
            return self.cap in _CHAT_APPROVABLE
        return (
            self.bucket == "live"
            and self.ticket_id is not None
            and self.cap not in ("attempt", "local_only")
        )

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
        if self.cap == "chat_turn":
            return f"chat turn budget: {self.detail}"
        if self.cap == "chat_model":
            return f"chat: model {self.model or '?'} is not the session's current model"
        if self.cap == "local_only":
            return (
                f"{FLAG_LOCAL_ONLY}: the repo is local only (cloud = false); refused a call "
                f"to cloud model {self.model or '?'}"
            )
        return f"{FLAG_SPEND_CAP}: {self.cap} cap ${self.cap_usd:.2f} reached"


@dataclass(frozen=True)
class PlanUse:
    """One flat-fee plan model this month: its fee and what its calls would have cost."""

    model: str
    fee_usd_month: float
    calls: int
    list_usd: float  # the same calls at pay-per-token list price

    @property
    def saved_usd(self) -> float:
        return self.list_usd - self.fee_usd_month


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
    plans: tuple[PlanUse, ...] = ()

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
        for p in self.plans:
            line += (
                f"; {p.model} plan ${p.fee_usd_month:.2f}/month, not capped: {p.calls} calls"
                f" this month, ${p.list_usd:.2f} at list price"
            )
        return line


@dataclass
class _Reservation:
    amount: float
    at: datetime
    attempt_id: int | None
    chat: tuple[int, int] | None = None  # (chat session id, turn) of a chat call
    chat_grant: bool = False  # let through on the chat turn's approval


@dataclass
class _ChatGrant:
    turn: int
    limit_usd: float
    spent_usd: float = 0.0
    reserved_usd: float = 0.0


@dataclass
class _Chat:
    """A registered chat session, in memory (the manager registers it)."""

    repo: str
    model: str  # the session's current model (its tier's rung)
    small_model: str | None
    turn: int = 0
    turn_model: str | None = None  # the running turn's model (None: ``model``)
    turn_spent_usd: float = 0.0
    approval_pending: bool = False
    grant: _ChatGrant | None = None


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
        chat_monthly_usd: float | None = None,
        chat_turn_budget_usd: float | None = DEFAULT_CHAT_TURN_BUDGET_USD,
    ) -> None:
        self.store = store
        self.caps = config.spend_caps if isinstance(config, Config) else config
        models = config.models if isinstance(config, Config) else ()
        self.plan_fees = {m.name: m.plan_usd_month for m in models if m.plan_usd_month is not None}
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
        if chat_monthly_usd is None:
            chat_monthly_usd = getattr(self.caps, "chat_monthly_usd", DEFAULT_CHAT_MONTHLY_USD)
        self.chat_monthly_usd = float(chat_monthly_usd)
        self.chat_turn_budget_usd = (
            None if chat_turn_budget_usd is None else float(chat_turn_budget_usd)
        )
        self._chats: dict[int, _Chat] = {}
        self._chat_refusals: dict[int, Refusal] = {}

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
                self._release_chat_grant(stale)
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
        chat_session_id: int | None = None,
        plan: bool = False,
    ) -> SpendEntry | None:
        """Write spend that happened to the ledger. Zero cost writes nothing. A chat row
        carries ``chat_session_id`` (store column from migration 012). ``plan``: a
        flat-fee plan call at list price, kept out of the caps (migration 013)."""
        if bucket not in BUCKETS:
            raise ValueError(f"unknown spend bucket {bucket!r}")
        if cost <= 0:
            return None
        if chat_session_id is not None:
            return self.store.record_spend(
                bucket, cost, model=model, chat_session_id=chat_session_id, plan=plan
            )
        return self.store.record_spend(
            bucket,
            cost,
            model=model,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            experiment_id=experiment_id,
            plan=plan,
        )

    # ------------------------------------------------------------------ the proxy hook

    def hook(
        self, endpoint: ModelEndpoint, cost_usd: float, context, *, phase: str = "estimate"
    ) -> bool | None:
        """``ModelProxy(spend_hook=spend.hook)``. ``estimate``: allow (and reserve) or refuse
        before anything is sent. ``actual``: settle the reservation and record the cost."""
        chat_id = getattr(context, "chat_session_id", None)
        if chat_id is not None:
            return self._chat_hook(endpoint, float(cost_usd), context, int(chat_id), phase)
        if endpoint.kind != "cloud":
            if phase == "estimate":
                return self._local_estimate(endpoint, context)
            return None
        bucket = getattr(context, "bucket", "live")
        if endpoint.plan_usd_month is not None:
            if phase == "estimate":
                return self._plan_estimate(endpoint, context, bucket)
            who = self._who(context)
            self.record(
                float(cost_usd),
                bucket if bucket in BUCKETS else "live",
                model=endpoint.name,
                ticket_id=who.ticket_id,
                attempt_id=who.attempt_id,
                plan=True,
            )
            return None
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
            getattr(ctx, "chat_session_id", None),
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
            if not self._repo_allows_cloud(who):
                return self._refuse_local_only(ep, estimate, bucket, who, now)
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

    def _refuse_local_only(
        self, ep: ModelEndpoint, estimate: float, bucket: str, who: _Who, now: datetime
    ) -> bool:
        refusal = _with_who(
            Refusal(bucket, "local_only", 0.0, 0.0, 0.0, estimate, None, now),  # type: ignore[arg-type]
            who,
            ep.name,
        )
        if who.attempt_id is not None:
            self._refusals_by_attempt[who.attempt_id] = refusal
        log.warning(
            "spend: refused %s call: ticket %s's repo is local only", ep.name, who.ticket_id
        )
        return False

    def _plan_estimate(self, ep: ModelEndpoint, ctx, bucket: str) -> bool:
        """A flat-fee plan call: no caps, no reservation; local-only and the attempt's
        input-token limit still refuse it."""
        if bucket not in BUCKETS:
            log.warning("spend: unknown bucket %r; refusing", bucket)
            return False
        with self._lock:
            now = self.now()
            who = self._who(ctx)
            if not self._repo_allows_cloud(who):
                return self._refuse_local_only(ep, 0.0, bucket, who, now)
            stop = self._attempt_check(ep, 0.0, ctx, bucket, who, now)
            return True if stop is None else self._stop(stop, who, ep)

    def _repo_allows_cloud(self, who: _Who) -> bool:
        """#49: False when the call's ticket belongs to a repo set local only. A call the
        proxy cannot attribute to a ticket is not a repo's code, so the caps alone apply."""
        if who.ticket_id is None:
            return True
        try:
            repo = self.store.get_ticket(who.ticket_id).repo
        except NotFoundError:
            return True
        return self.store.repo_allows_cloud(repo)

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
        budget = self.attempt_budget_usd if ep.metered else None
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

    # ------------------------------------------------------------------ chat (todo #71)

    def register_chat_session(
        self, session_id: int, *, repo: str, model: str, small_model: str | None = None
    ) -> None:
        """A chat session opened: its repo (for the local-only check), current model and
        ``small_model`` (OpenCode's own title/summary calls). Re-registering keeps the
        turn and any pending approval and updates the rest."""
        with self._lock:
            st = self._chats.get(session_id)
            if st is None:
                self._chats[session_id] = _Chat(repo=repo, model=model, small_model=small_model)
            else:
                st.repo, st.model, st.small_model = repo, model, small_model

    def set_chat_model(self, session_id: int, model: str) -> None:
        """A tier move: the session's model for the **next** turn; the running turn
        keeps its own (``start_chat_turn`` switches)."""
        with self._lock:
            st = self._chat_or_raise(session_id)
            if st.turn_model is None:
                st.turn_model = st.model  # pin the running turn's model
            st.model = model

    def start_chat_turn(self, session_id: int, *, model: str | None = None) -> int:
        """A new message: a fresh turn budget, using ``model`` (the session's current
        model, or a cloud fallback from ``chat_cloud_fallback``). Returns the turn
        number. An approval left unused carries over; a used one ends with its turn."""
        with self._lock:
            st = self._chat_or_raise(session_id)
            st.turn += 1
            st.turn_model = model or st.model
            st.turn_spent_usd = 0.0
            st.grant = None
            self._chat_refusals.pop(session_id, None)
            return st.turn

    def end_chat_session(self, session_id: int) -> None:
        """Forget a closed session; its later calls are refused (``chat_model``)."""
        with self._lock:
            self._chats.pop(session_id, None)
            self._chat_refusals.pop(session_id, None)

    def approve_chat(self, session_id: int) -> None:
        """``/approve``: the session's next over-cap turn may spend up to ``approval_usd``
        past the caps. Held in memory; approving twice before one is used is one."""
        with self._lock:
            self._chat_or_raise(session_id).approval_pending = True

    def chat_approval_pending(self, session_id: int) -> bool:
        with self._lock:
            st = self._chats.get(session_id)
            return bool(st and st.approval_pending)

    def refusal_for_chat(self, session_id: int) -> Refusal | None:
        """The last refusal of a call in this session's current turn, or None."""
        with self._lock:
            return self._chat_refusals.get(session_id)

    def chat_turn_spent(self, session_id: int) -> float:
        """Cloud spend settled in the session's current turn (in memory)."""
        with self._lock:
            st = self._chats.get(session_id)
            return st.turn_spent_usd if st else 0.0

    def chat_room(
        self, session_id: int, *, now: datetime | None = None, estimate_usd: float = 0.0
    ) -> Refusal | None:
        """No side effects: the first of the monthly cap, the chat sub-cap, the daily cap
        and the session's turn budget that has no room for ``estimate_usd`` more cloud
        spend (with 0, no headroom left at all), or None. Approvals are not counted."""
        with self._lock:
            now = now or self.now()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            st = self._chats.get(session_id)
            live = list(self._unexpired(now))
            reserved = sum(r.amount for r in live)
            chat_reserved = sum(r.amount for r in live if r.chat is not None)
            for cap, cap_usd, window, spent, res in self._chat_caps(now, reserved, chat_reserved):
                if not _room(spent + res, estimate_usd, cap_usd):
                    return Refusal(
                        "live", cap, cap_usd, spent, res, estimate_usd, window[1], now,
                        chat_session_id=session_id,
                    )  # fmt: skip
            budget = self.chat_turn_budget_usd
            if budget is not None:
                turn = st.turn if st else 0
                spent = st.turn_spent_usd if st else 0.0
                res = sum(r.amount for r in live if r.chat == (session_id, turn))
                if not _room(spent + res, estimate_usd, budget):
                    return Refusal(
                        "live", "chat_turn", budget, spent, res, estimate_usd, None, now,
                        chat_session_id=session_id,
                    )  # fmt: skip
            return None

    def _chat_or_raise(self, session_id: int) -> _Chat:
        st = self._chats.get(session_id)
        if st is None:
            raise SpendError(f"no chat session {session_id} is registered")
        return st

    def _unexpired(self, now: datetime) -> Iterator[_Reservation]:
        """Live reservations still in force, without dropping expired ones."""
        cutoff = now - self.reservation_ttl
        for key, q in self._state.reservations.items():
            if key[0] == "live":
                yield from (r for r in q if r.at >= cutoff)

    def _chat_total(self, window: tuple[datetime, datetime]) -> float:
        """Chat spend in ``window`` from the ledger (``Store.chat_spend_total``, wave C1a;
        a store without the chat column has no chat rows)."""
        fn = getattr(self.store, "chat_spend_total", None)
        if fn is not None:
            return float(fn(since=window[0], until=window[1]))
        if not self.store._has_column("spend_ledger", "chat_session_id"):
            return 0.0
        rows = self.store.query(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger"
            " WHERE bucket = 'live' AND chat_session_id IS NOT NULL"
            " AND created_at >= ? AND created_at < ?",
            (_store_ts(window[0]), _store_ts(window[1])),
        )
        return float(rows[0]["total"]) if rows else 0.0

    def _chat_caps(
        self, now: datetime, reserved: float, chat_reserved: float
    ) -> Iterable[tuple[CapName, float, tuple[datetime, datetime], float, float]]:
        """(cap, cap_usd, window, spent, reserved) in report order: monthly, chat sub-cap,
        daily. Lazy, so a check stops reading the ledger at the first full cap."""
        month = self.month_window(now)
        yield "monthly", self.caps.monthly_usd, month, self._total("live", month), reserved
        yield "chat_monthly", self.chat_monthly_usd, month, self._chat_total(month), chat_reserved
        day = self.day_window(now)
        yield "daily", self.caps.daily_usd, day, self._total("live", day), reserved

    def _chat_hook(
        self, ep: ModelEndpoint, cost: float, ctx, session_id: int, phase: str
    ) -> bool | None:
        bucket = getattr(ctx, "bucket", "live")
        if bucket != "live":
            log.warning("spend: chat call with bucket %r counted as live", bucket)
        if phase == "estimate":
            return self._chat_estimate(ep, cost, ctx, session_id)
        if ep.kind == "cloud":
            self._chat_actual(ep, cost, ctx, session_id)
        return None

    def _chat_refuse(
        self, refusal: Refusal, session_id: int, ep: ModelEndpoint, *, record: bool = True
    ) -> bool:
        """Refuse a chat call; ``record`` makes it the turn's refusal (7e: the turn stops).
        A refused local-slot fallback (decision 2) is not recorded: that call keeps waiting
        for the local slot, so the turn goes on."""
        refusal = replace(refusal, chat_session_id=session_id, model=ep.name)
        if record:
            self._chat_refusals[session_id] = refusal
        log.info("spend: refused chat %s call to %s (%s)", session_id, ep.name, refusal.cap)
        return False

    def _chat_estimate(self, ep: ModelEndpoint, estimate: float, ctx, session_id: int) -> bool:
        # A cloud call standing in for a queued local one (decision 2) that is refused
        # just keeps waiting for the local slot: not the turn's refusal.
        fb = getattr(ctx, "fallback_from", None)
        record = fb is None
        with self._lock:
            now = self.now()
            st = self._chats.get(session_id)
            if st is None:
                bare = Refusal("live", "chat_model", 0.0, 0.0, 0.0, estimate, None, now)
                return self._chat_refuse(bare, session_id, ep, record=record)
            cloud = ep.kind == "cloud"
            if cloud and not self.store.repo_allows_cloud(st.repo):
                bare = Refusal("live", "local_only", 0.0, 0.0, 0.0, estimate, None, now)
                return self._chat_refuse(bare, session_id, ep, record=record)
            allowed = {m for m in (st.turn_model or st.model, st.small_model) if m}
            # The proxy's 5 s local-slot fallback (decision 2): a cloud call standing in
            # for a queued call to one of this turn's own (local) models. An ordinary call
            # has fallback_from None, which must never match an unset small_model.
            fallback = cloud and fb is not None and fb in allowed
            if ep.name not in allowed and not fallback:
                bare = Refusal("live", "chat_model", 0.0, 0.0, 0.0, estimate, None, now)
                return self._chat_refuse(bare, session_id, ep, record=record)
            if not cloud or not ep.metered:
                return True  # local, or a flat-fee plan: no turn budget, no caps
            reserved = self._reserved("live", now)
            live = list(self._unexpired(now))
            chat_reserved = sum(r.amount for r in live if r.chat is not None)
            budget = self.chat_turn_budget_usd
            if budget is not None:
                turn_res = sum(r.amount for r in live if r.chat == (session_id, st.turn))
                if not _fits(st.turn_spent_usd + turn_res, estimate, budget):
                    over = Refusal(
                        "live", "chat_turn", budget, st.turn_spent_usd, turn_res, estimate,
                        None, now,
                    )  # fmt: skip
                    return self._chat_refuse(over, session_id, ep, record=record)
            refusal = None
            for cap, cap_usd, window, spent, res in self._chat_caps(now, reserved, chat_reserved):
                if not _fits(spent + res, estimate, cap_usd):
                    refusal = Refusal("live", cap, cap_usd, spent, res, estimate, window[1], now)
                    break
            granted = False
            if refusal is not None:
                refusal, granted = self._try_chat_grant(st, refusal, estimate)
            if refusal is not None:
                return self._chat_refuse(refusal, session_id, ep, record=record)
            res_ = _Reservation(estimate, now, None, chat=(session_id, st.turn), chat_grant=granted)
            self._state.reservations.setdefault(self._key(ep, ctx, "live"), deque()).append(res_)
            return True

    def _try_chat_grant(
        self, st: _Chat, refusal: Refusal, estimate: float
    ) -> tuple[Refusal | None, bool]:
        """Past a cap: the turn's grant, or the session's pending approval (consumed)."""
        grant = st.grant if st.grant is not None and st.grant.turn == st.turn else None
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
            return over_grant, False  # one call bigger than a whole approval
        if grant is not None or not st.approval_pending:
            return (over_grant if grant is not None else refusal), False
        st.approval_pending = False
        st.grant = _ChatGrant(st.turn, self.approval_usd, reserved_usd=estimate)
        log.info("spend: chat approval used for turn %s", st.turn)
        return None, True

    def _release_chat_grant(self, res: _Reservation) -> _ChatGrant | None:
        if res.chat is None or not res.chat_grant:
            return None
        st = self._chats.get(res.chat[0])
        grant = st.grant if st is not None else None
        if grant is None or grant.turn != res.chat[1]:
            return None
        grant.reserved_usd = max(0.0, grant.reserved_usd - res.amount)
        return grant

    def _chat_actual(self, ep: ModelEndpoint, cost: float, ctx, session_id: int) -> None:
        if not ep.metered:  # a flat-fee plan: logged at list price, outside every cap
            self.record(cost, "live", model=ep.name, chat_session_id=session_id, plan=True)
            return
        with self._lock:
            key = self._key(ep, ctx, "live")
            q = self._state.reservations.get(key)
            res = q.popleft() if q else None
            if q is not None and not q:
                del self._state.reservations[key]
            grant = self._release_chat_grant(res) if res else None
            if grant is not None:
                grant.spent_usd += max(cost, 0.0)
            st = self._chats.get(session_id)
            if st is not None and (res is None or res.chat == (session_id, st.turn)):
                st.turn_spent_usd += max(cost, 0.0)
        self.record(cost, "live", model=ep.name, chat_session_id=session_id)

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
        use = self.store.plan_use(since=month[0], until=month[1]) if self.plan_fees else {}
        plans = tuple(
            PlanUse(name, fee, *use.get(name, (0, 0.0))) for name, fee in self.plan_fees.items()
        )
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
            plans=plans,
        )


def _fits(used: float, estimate: float, cap: float) -> bool:
    """THE boundary: reaching a cap exactly is allowed, going past it is not."""
    return used + estimate <= cap + _EPS


def _room(used: float, estimate: float, cap: float) -> bool:
    """``estimate`` more fits, and (for an estimate of 0) some headroom is left."""
    return _fits(used, estimate, cap) and used < cap - _EPS


def _store_ts(at: datetime) -> str:
    """The store's timestamp text (UTC ISO, as ``db.store._ts`` writes it)."""
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def chat_cloud_fallback(
    spend: Spend,
    store: Store,
    session: ChatSession,
    ladder: tuple[Rung, ...],
    now: datetime | None = None,
    *,
    estimate_usd: float = 0.0,
) -> ModelEndpoint | None:
    """Decision 2 (``docs/chat-design.md``, decisions of 2026-11-25): the local slot is
    busy past ``chat_local_wait_seconds``; which cloud model may this turn use instead?

    The first cloud rung of ``ladder`` at or above ``session.tier``, only when the repo
    allows cloud (``store.repo_allows_cloud``; never for a local-only repo, #49) and the
    monthly cap, the chat sub-cap, the daily cap and the session's turn budget all have
    room (``Spend.chat_room``; approvals are not counted). Else None: the turn keeps
    waiting for the local slot. No side effects; the caller starts the turn with the
    result (``spend.start_chat_turn(id, model=endpoint.name)``) so the model check lets
    it through, and the session's own tier does not change."""
    if not store.repo_allows_cloud(session.repo):
        return None
    rung = next((r for r in ladder if r.tier >= session.tier and r.endpoint.kind == "cloud"), None)
    if rung is None:
        return None
    if spend.chat_room(session.id, now=now, estimate_usd=estimate_usd) is not None:
        return None
    return rung.endpoint


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
    "DEFAULT_CHAT_MONTHLY_USD",
    "DEFAULT_CHAT_TURN_BUDGET_USD",
    "DEFAULT_ATTEMPT_BUDGET_USD",
    "DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_CLOUD",
    "DEFAULT_ATTEMPT_MAX_INPUT_TOKENS_LOCAL",
    "DEFAULT_RESERVATION_TTL",
    "FLAG_ATTEMPT_BUDGET",
    "FLAG_LOCAL_ONLY",
    "FLAG_SPEND_CAP",
    "FLAG_THRIFTY",
    "PlanUse",
    "Refusal",
    "Spend",
    "SpendError",
    "SpendSummary",
    "chat_cloud_fallback",
]
