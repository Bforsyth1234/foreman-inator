"""Metrics queries over the store (todo #20, SPEC.md "Metrics view").

One function per metric. Each takes a read-only ``Store`` and the same
``MetricsFilter`` (model, repo, date range) and runs one SQL statement with bind
parameters only (no filter value is ever formatted into SQL). Results are small frozen
dataclasses whose ``value`` is ``None`` when there is no data: a metric with an empty
denominator reads "no data", never 0 %.

Readings shared by every metric (SPEC leaves these open; also listed in the item):

* **Dropped tickets are excluded** from every metric (SPEC "Ticket lifecycle").
* **The date range filters on the ticket's ``created_at``** (UTC), for every metric
  and for the trend buckets, so one filter selects one set of tickets everywhere.
  ``until`` is inclusive (the whole day).
* **Closed = accepted:** a ticket counts as accepted when its column is ``done`` and it
  has an ``accepted_attempt_id``. Accept-based metrics (first-try accept rate, cost per
  accepted ticket, time to accept, router hit rate, the trend lines, share solved
  locally) use closed tickets only; tickets still open are left out of their
  denominators. Event counts (kickbacks per ticket, escalation rate, AI kickbacks per
  ticket) include open tickets, because a kickback or an escalation is a fact as soon as
  it happens (a ticket stuck on tier 3 still shows up as an escalation).
* **Exploration** (SPEC: explore attempts "never count against a model in the metrics if
  they fail"): an attempt with trigger ``explore`` whose outcome is ``failed_checks`` or
  ``kicked_back`` is *invisible* to every model metric: it is not the ticket's start,
  its kickbacks are not counted, and escalating away from it is not an escalation. An
  explore attempt that succeeds counts like any other. Its cost still counts in the
  ticket's spend ("Total spend on all attempts … including the failed attempts").
* **Rebases** (#43 decision 3: "a failed ``rebase`` attempt doesn't count against the
  model in metrics"): an attempt with trigger ``rebase`` whose outcome is
  ``failed_checks`` or ``superseded`` is invisible the same way. A rebase is the worker
  keeping a finished change mergeable after the base branch moved, not the model's
  answer to the issue. A rebase attempt that passes counts like any other, and every
  rebase attempt's cost stays in the ticket's spend (cost per accepted ticket).
* **Set-aside attempts** (#53): an attempt the worker set aside, listed in the store's
  ``worker:uncounted:<ticket>`` cursor (an attempt that could not run because its setup
  failed, one paused at a spend cap, one restarted after a lock yield), is invisible the
  same way, whatever its trigger: escalation does not count it (``Worker.history``), so
  no model metric counts it either. Its cost stays in the ticket's spend.
* **A ticket's start attempt** is its first visible attempt (lowest id). Ticket-level
  metrics are attributed to the start attempt's model: the model filter on a ticket
  metric means "tickets started on this model". Attempt-level metrics (kickbacks per
  ticket, AI kickbacks per ticket) filter on the attempt's own model.
* **Escalated:** a visible attempt after the start has a higher tier than the start, or
  has trigger ``escalation``.
* **Ticket spend** = every attempt's ``cost_usd`` plus every non-shadow review's
  ``cost_usd`` (SPEC reviews table: review tokens and cost are "Counted in cost per
  accepted ticket"). Shadow reviews are experiment cost and never count, here or as the
  review of record.
* **After merge** (#24 / todo 47): ``post_merge_quality`` counts closed tickets by the
  model of their *accepted* attempt (the model that wrote the merged change, as
  ``metrics.quality.quality_by_model`` does), not the start model, and how many of them
  were later reverted or followed up (``post_merge_signals``, found by
  ``metrics.quality.scan``). A reverted ticket still counts as accepted in every other
  metric here: the signal is shown next to the accepted count, not subtracted from it.
* **Pairs** (``ab:pair``, #34): a pair's losing side is a dropped ticket, so it is out of
  every metric like any dropped ticket, except that its spend is charged to the winner
  in cost per accepted ticket and the cost trend (``pair_loser_spend_usd``, decided
  pairs only). ``pair_record`` is the W-L per model pair (``experiments.pair.score``);
  only the repo filter applies to it. Unpaired tickets read exactly as before.
* **Local** = accepted on tier 1 or 2 (SPEC "Routing and escalation": tiers 1–2 are
  local, tier 3 is cloud).
* **Chat** (#60, docs/chat-design.md sections 5 and 6): chat sessions never feed the
  router, so no ticket, writer, reviewer or router metric reads ``chat_sessions``,
  ``chat_turns`` or a chat ledger row (a chat has no ticket and no attempt rows, so the
  ticket queries above never see one). Chat shows up only in the spend split below.
* **Spend split** (``spend_split``, ``spend_trend``, ``chat_status``): unlike the
  ticket metrics, the date range filters on the ledger row's ``created_at`` (when the
  money was spent). Flat-fee plan rows (``plan = 1``) are left out, as in the caps.
  *Tickets* = bucket ``live`` rows with no ``chat_session_id``; *chat* = rows with one
  (bucket ``live`` by contract, option C; any bucket is counted, as in
  ``Store.chat_spend_total``); *experiments* = bucket ``experiments`` rows. The repo
  filter matches a chat row by its session's repo and any other row by its ticket's
  repo (a row with neither drops out under a repo filter); the model filter is ignored.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from foreman_inator.config import DEFAULT_CHAT_MONTHLY_USD
from foreman_inator.db.store import Store
from foreman_inator.experiments import pair as pair_mod
from foreman_inator.experiments.pair import PairRecord

Bucket = Literal["day", "week", "month"]
BUCKETS: tuple[str, ...] = ("day", "week", "month")
LOCAL_TIERS: tuple[int, ...] = (1, 2)

# Bucket start dates, chosen from this fixed table (never from user input).
_BUCKET_SQL: dict[str, str] = {
    "day": "date(substr(created_at, 1, 10))",
    "week": "date(substr(created_at, 1, 10), '-6 days', 'weekday 1')",  # Monday
    "month": "date(substr(created_at, 1, 7) || '-01')",
}


# --- filter ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricsFilter:
    """Model, repo and date range. ``None`` means "any". ``until`` is inclusive.

    ``model`` means the ticket's start model for ticket metrics, the attempt's model for
    attempt metrics, and the reviewer model for the reviewer miss rate.
    """

    model: str | None = None
    repo: str | None = None
    since: date | None = None
    until: date | None = None

    def __post_init__(self) -> None:
        if self.since and self.until and self.since > self.until:
            raise ValueError("the date range starts after it ends")

    def params(self) -> dict[str, Any]:
        """Bind parameters shared by every query (times in the store's text format)."""

        def ts(d: date) -> str:
            return datetime.combine(d, time(), tzinfo=UTC).isoformat(timespec="microseconds")

        return {
            "model": self.model or None,
            "repo": self.repo or None,
            "since": ts(self.since) if self.since else None,
            "until": ts(self.until + timedelta(days=1)) if self.until else None,
        }

    def with_model(self, model: str | None) -> MetricsFilter:
        return MetricsFilter(model=model, repo=self.repo, since=self.since, until=self.until)


# --- result types ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rate:
    """``numerator ÷ denominator``; ``value`` is None when the denominator is 0."""

    numerator: int
    denominator: int
    value: float | None = field(init=False)

    def __post_init__(self) -> None:
        v = self.numerator / self.denominator if self.denominator else None
        object.__setattr__(self, "value", v)


@dataclass(frozen=True, slots=True)
class Mean:
    """``total ÷ count``; ``value`` is None when count is 0."""

    total: float
    count: int
    value: float | None = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.total / self.count if self.count else None)


@dataclass(frozen=True, slots=True)
class KickbackStats:
    """Kickbacks charged to visible attempts, over the tickets those attempts worked on."""

    tickets: int
    total: int
    by_tag: dict[str, int]  # wrong / incomplete / broke / style / untagged
    by_who: dict[str, int]  # user / reviewer
    value: float | None = field(init=False)
    per_tag: dict[str, float | None] = field(init=False)

    def __post_init__(self) -> None:
        t = self.tickets
        object.__setattr__(self, "value", self.total / t if t else None)
        object.__setattr__(
            self, "per_tag", {k: (n / t if t else None) for k, n in self.by_tag.items()}
        )


@dataclass(frozen=True, slots=True)
class Duration:
    """Median of ``count`` durations in seconds; None when count is 0."""

    count: int
    median_seconds: float | None


@dataclass(frozen=True, slots=True)
class PostMerge:
    """Closed tickets accepted on a model, and what happened to them after the merge."""

    accepted: int
    reverted: int  # "accepted, then reverted"
    followup: int  # "accepted, then followed up"
    reverted_rate: Rate = field(init=False)
    followup_rate: Rate = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reverted_rate", Rate(self.reverted, self.accepted))
        object.__setattr__(self, "followup_rate", Rate(self.followup, self.accepted))


@dataclass(frozen=True, slots=True)
class PipelineCost:
    writer_reviewer: Mean  # cost per accepted ticket: tier 1–2 start, AI-reviewed
    strong_writer_alone: Mean  # cost per accepted ticket: tier 3 start, review cost left out


@dataclass(frozen=True, slots=True)
class TrendPoint:
    bucket: str  # ISO date of the bucket start
    accepted: int
    spend_usd: float
    local: int
    cost_per_accepted: float | None = field(init=False)
    local_share: float | None = field(init=False)

    def __post_init__(self) -> None:
        a = self.accepted
        object.__setattr__(self, "cost_per_accepted", self.spend_usd / a if a else None)
        object.__setattr__(self, "local_share", self.local / a if a else None)


@dataclass(frozen=True, slots=True)
class RecentTicket:
    id: int
    repo: str
    issue_number: int
    title: str  # untrusted GitHub text: escape before display
    start_model: str
    accepted_model: str | None
    accepted_tier: int | None
    attempts: int
    spend_usd: float
    created_at: str
    done_at: str | None


@dataclass(frozen=True, slots=True)
class FilterOptions:
    writer_models: list[str]
    reviewer_models: list[str]
    repos: list[str]


# --- shared SQL -----------------------------------------------------------------------

# scope: non-dropped tickets in the repo/date filter.
# vis:   attempts that count against their model (failed explore and rebase attempts
#        and the worker's set-aside attempts removed).
_SCOPE_VIS = """
scope AS (
    SELECT t.* FROM tickets t
    WHERE t."column" != 'dropped'
      AND (:repo IS NULL OR t.repo = :repo)
      AND (:since IS NULL OR t.created_at >= :since)
      AND (:until IS NULL OR t.created_at < :until)
),
vis AS (
    SELECT a.* FROM attempts a JOIN scope s ON s.id = a.ticket_id
    WHERE NOT (a."trigger" = 'explore' AND a.outcome IN ('failed_checks', 'kicked_back'))
      AND NOT (a."trigger" = 'rebase' AND a.outcome IN ('failed_checks', 'superseded'))
      AND NOT (a.outcome = 'superseded' AND EXISTS (
          SELECT 1 FROM poll_cursors pc,
              json_each(CASE WHEN json_valid(pc.value) THEN pc.value ELSE '[]' END) j
          WHERE pc.repo = s.repo AND pc.name = 'worker:uncounted:' || a.ticket_id
            AND j.value = a.id))
)"""

# started: one row per scoped ticket with a visible start attempt on the filter model.
_STARTED = (
    "WITH "
    + _SCOPE_VIS
    + """,
started AS (
    SELECT s.*,
        v.id AS start_attempt_id, v.model AS start_model, v.tier AS start_tier,
        EXISTS (SELECT 1 FROM vis e WHERE e.ticket_id = s.id AND e.id > v.id
                AND (e.tier > v.tier OR e."trigger" = 'escalation')) AS escalated,
        EXISTS (SELECT 1 FROM kickbacks k JOIN vis kv ON kv.id = k.attempt_id
                WHERE kv.ticket_id = s.id) AS kicked_back,
        (SELECT COALESCE(SUM(a.cost_usd), 0) FROM attempts a WHERE a.ticket_id = s.id)
          + (SELECT COALESCE(SUM(r.cost_usd), 0) FROM reviews r
             JOIN attempts ra ON ra.id = r.attempt_id
             WHERE ra.ticket_id = s.id AND r.shadow = 0) AS spend_usd,
        (SELECT COALESCE(SUM(a.cost_usd), 0) FROM attempts a
         WHERE a.ticket_id = s.id) AS attempt_spend_usd,
        (SELECT COALESCE(SUM(pa.cost_usd), 0) FROM pairs p
         JOIN attempts pa ON pa.ticket_id = p.ticket_a + p.ticket_b - s.id
         WHERE p.status = 'decided' AND p.winner_ticket_id = s.id)
          + (SELECT COALESCE(SUM(r.cost_usd), 0) FROM pairs p
             JOIN attempts ra ON ra.ticket_id = p.ticket_a + p.ticket_b - s.id
             JOIN reviews r ON r.attempt_id = ra.id
             WHERE p.status = 'decided' AND p.winner_ticket_id = s.id AND r.shadow = 0)
          AS pair_loser_spend_usd,
        (SELECT COALESCE(SUM(a.seconds), 0) FROM attempts a
         WHERE a.ticket_id = s.id) AS attempt_seconds,
        (SELECT COUNT(*) FROM kickbacks k JOIN attempts ka ON ka.id = k.attempt_id
         WHERE ka.ticket_id = s.id AND k."by" = 'user') AS user_kickbacks,
        EXISTS (SELECT 1 FROM reviews r JOIN attempts ra ON ra.id = r.attempt_id
                WHERE ra.ticket_id = s.id AND r.shadow = 0) AS reviewed,
        (SELECT COUNT(*) FROM attempts a WHERE a.ticket_id = s.id) AS n_attempts,
        acc.model AS accepted_model, acc.tier AS accepted_tier,
        (s."column" = 'done' AND s.accepted_attempt_id IS NOT NULL) AS closed
    FROM scope s
    JOIN vis v ON v.ticket_id = s.id
         AND v.id = (SELECT MIN(m.id) FROM vis m WHERE m.ticket_id = s.id)
    LEFT JOIN attempts acc ON acc.id = s.accepted_attempt_id
    WHERE (:model IS NULL OR v.model = :model)
)
"""
)


def _p(filt: MetricsFilter | None) -> dict[str, Any]:
    return (filt or MetricsFilter()).params()


def _rate(row: Any) -> Rate:
    return Rate(int(row["num"] or 0), int(row["den"] or 0))


# --- the 6 core metrics ---------------------------------------------------------------


def first_try_accept_rate(store: Store, filt: MetricsFilter | None = None) -> Rate:
    """First-try accept rate.

    SPEC: "Accepted with no kickbacks and no escalation ÷ tickets started on this model".

    Implemented: closed tickets started on the model (start = first visible attempt)
    that have no kickback (by you or the AI reviewer) on any visible attempt and no
    escalation after the start ÷ closed tickets started on the model. A same-tier retry
    after failed checks is neither a kickback nor an escalation, so it is still a first
    try. Open tickets are left out until they close.
    """
    sql = _STARTED + (
        "SELECT COALESCE(SUM(NOT escalated AND NOT kicked_back), 0) AS num, COUNT(*) AS den"
        " FROM started WHERE closed"
    )
    return _rate(store.query(sql, _p(filt))[0])


def kickbacks_per_ticket(store: Store, filt: MetricsFilter | None = None) -> KickbackStats:
    """Kickbacks per ticket.

    SPEC: "Mean kickbacks charged to this model's attempts, split by tag".

    Implemented: kickbacks (by you and by the AI reviewer) charged to the model's visible
    attempts ÷ distinct non-dropped tickets (open or closed) on which the model has a
    visible attempt; also split by tag (``untagged`` for a kickback with no
    ``kickback:<tag>`` label) and by who kicked back. Kickbacks on failed explore
    attempts are not counted.
    """
    sql = (
        "WITH "
        + _SCOPE_VIS
        + """,
mv AS (SELECT * FROM vis WHERE (:model IS NULL OR model = :model))
SELECT t.tickets AS tickets, g.who AS who, g.tag AS tag, g.n AS n
FROM (SELECT COUNT(DISTINCT ticket_id) AS tickets FROM mv) t
LEFT JOIN (
    SELECT k."by" AS who, COALESCE(k.tag, 'untagged') AS tag, COUNT(*) AS n
    FROM kickbacks k JOIN mv ON mv.id = k.attempt_id
    GROUP BY k."by", COALESCE(k.tag, 'untagged')
) g ON 1
"""
    )
    rows = store.query(sql, _p(filt))
    by_tag: dict[str, int] = {}
    by_who: dict[str, int] = {}
    for r in rows:
        if r["who"] is None:
            continue
        by_tag[r["tag"]] = by_tag.get(r["tag"], 0) + int(r["n"])
        by_who[r["who"]] = by_who.get(r["who"], 0) + int(r["n"])
    return KickbackStats(
        tickets=int(rows[0]["tickets"]) if rows else 0,
        total=sum(by_tag.values()),
        by_tag=by_tag,
        by_who=by_who,
    )


def escalation_rate(store: Store, filt: MetricsFilter | None = None) -> Rate:
    """Escalation rate.

    SPEC: "Tickets this model handed up a tier ÷ tickets it started".

    Implemented: non-dropped tickets (open or closed) started on the model that were
    escalated after the start (a later visible attempt on a higher tier, or with trigger
    ``escalation``) ÷ non-dropped tickets started on the model. Only tickets the model
    started count in the numerator, so the rate stays within 0–1; handing up a ticket
    the model received by escalation is counted for the ticket's start model. Moving up
    from a failed explore attempt is not an escalation.
    """
    sql = _STARTED + ("SELECT COALESCE(SUM(escalated), 0) AS num, COUNT(*) AS den FROM started")
    return _rate(store.query(sql, _p(filt))[0])


def cost_per_accepted_ticket(store: Store, filt: MetricsFilter | None = None) -> Mean:
    """Cost per accepted ticket.

    SPEC: "Total spend on all attempts ÷ accepted tickets, including the failed attempts".

    Implemented: for closed tickets started on the model, the sum of every attempt's
    cost (failed, superseded and explore attempts included) plus every non-shadow AI
    review's cost ÷ the number of those tickets. Spend on tickets still open is left
    out until they close (the denominator only has accepted tickets). A ``ab:pair``
    winner also carries its losing sibling's spend (attempts plus non-shadow reviews,
    through ``pairs``): the loser is dropped, so without this its spend would vanish
    from every metric, and the head-to-head is what bought the accepted change.
    """
    sql = _STARTED + (
        "SELECT COALESCE(SUM(spend_usd + pair_loser_spend_usd), 0) AS total, COUNT(*) AS n"
        " FROM started WHERE closed"
    )
    row = store.query(sql, _p(filt))[0]
    return Mean(float(row["total"]), int(row["n"]))


def attempt_minutes_per_accepted_ticket(store: Store, filt: MetricsFilter | None = None) -> Mean:
    """Attempt minutes per accepted ticket (todo #46).

    Why: a local model costs $0 per token, so cost per accepted ticket shows a local
    model that took three attempts as free. Attempt time is its real price.

    Implemented: for closed tickets started on the model, the sum of every attempt's
    ``seconds`` (failed, superseded and explore attempts included, like the ticket's
    spend; an attempt with no recorded ``seconds`` adds 0) in minutes ÷ the number of
    those tickets.
    """
    sql = _STARTED + (
        "SELECT COALESCE(SUM(attempt_seconds), 0) AS total, COUNT(*) AS n FROM started WHERE closed"
    )
    row = store.query(sql, _p(filt))[0]
    return Mean(float(row["total"]) / 60.0, int(row["n"]))


def user_reviews_per_accepted_ticket(store: Store, filt: MetricsFilter | None = None) -> Mean:
    """Your reviews per accepted ticket (todo #46).

    Implemented: for closed tickets started on the model, your kickbacks (``by =
    user``) on any of the ticket's attempts plus 1 for the review that accepted it ÷ the
    number of those tickets. Your kickbacks on attempts hidden from model metrics
    (failed explore or rebase, set aside) still count: you reviewed them all the same.
    AI reviewer kickbacks are not yours and are left out.
    """
    sql = _STARTED + (
        "SELECT COALESCE(SUM(user_kickbacks + 1), 0) AS total, COUNT(*) AS n "
        "FROM started WHERE closed"
    )
    row = store.query(sql, _p(filt))[0]
    return Mean(float(row["total"]), int(row["n"]))


def time_to_accept(store: Store, filt: MetricsFilter | None = None) -> Duration:
    """Time to accept.

    SPEC: "Created → accepted, median".

    Implemented: median of ``done_at − created_at`` (the ticket's creation to the merge
    that accepted it) over closed tickets started on the model.
    """
    sql = _STARTED + "SELECT created_at, done_at FROM started WHERE closed AND done_at IS NOT NULL"
    secs = [
        (
            datetime.fromisoformat(r["done_at"]) - datetime.fromisoformat(r["created_at"])
        ).total_seconds()
        for r in store.query(sql, _p(filt))
    ]
    return Duration(len(secs), statistics.median(secs) if secs else None)


def router_hit_rate(store: Store, filt: MetricsFilter | None = None) -> Rate:
    """Router hit rate.

    SPEC: "Tickets accepted on the tier the router first picked".

    Implemented: closed tickets whose accepted attempt's tier equals
    ``tickets.routed_tier`` ÷ closed tickets with a ``routed_tier``, for tickets started
    on the model. Tickets with no recorded router pick (``routed_tier`` NULL, e.g. from
    worker v1) are left out. An explore ticket accepted below the router's pick is a
    miss.
    """
    sql = _STARTED + (
        "SELECT COALESCE(SUM(accepted_tier = routed_tier), 0) AS num, COUNT(*) AS den"
        " FROM started WHERE closed AND routed_tier IS NOT NULL"
    )
    return _rate(store.query(sql, _p(filt))[0])


# --- the 3 review metrics -------------------------------------------------------------


def reviewer_miss_rate(store: Store, filt: MetricsFilter | None = None) -> Rate:
    """Reviewer miss rate.

    SPEC: "your kickbacks on PRs the reviewer approved ÷ PRs it approved".

    Implemented, per reviewer model (the filter's ``model`` is the reviewer model here):
    non-shadow ``approve`` reviews whose attempt you then kicked back (a ``user``
    kickback on that attempt) ÷ non-shadow ``approve`` reviews whose attempt has a
    known result (outcome ``accepted`` or ``kicked_back``). One approval is one reviewed
    attempt (one PR revision); approvals still waiting on you are left out. Shadow
    reviews never count.
    """
    sql = (
        "WITH "
        + _SCOPE_VIS
        + """
SELECT COUNT(*) AS den,
    COALESCE(SUM(EXISTS (SELECT 1 FROM kickbacks k
                         WHERE k.attempt_id = a.id AND k."by" = 'user')), 0) AS num
FROM reviews r
JOIN attempts a ON a.id = r.attempt_id
JOIN scope s ON s.id = a.ticket_id
WHERE r.shadow = 0 AND r.verdict = 'approve'
  AND a.outcome IN ('accepted', 'kicked_back')
  AND (:model IS NULL OR r.reviewer_model = :model)
"""
    )
    return _rate(store.query(sql, _p(filt))[0])


def ai_kickbacks_per_ticket(store: Store, filt: MetricsFilter | None = None) -> Mean:
    """AI kickbacks per ticket, per writer model.

    SPEC: "AI kickbacks per ticket, per writer model".

    Implemented: kickbacks with ``by = reviewer`` charged to the writer model's visible
    attempts ÷ distinct non-dropped tickets (open or closed) on which the model has a
    visible attempt (the same denominator as kickbacks per ticket).
    """
    sql = (
        "WITH "
        + _SCOPE_VIS
        + """,
mv AS (SELECT * FROM vis WHERE (:model IS NULL OR model = :model))
SELECT (SELECT COUNT(DISTINCT ticket_id) FROM mv) AS n,
       (SELECT COUNT(*) FROM kickbacks k JOIN mv ON mv.id = k.attempt_id
        WHERE k."by" = 'reviewer') AS total
"""
    )
    row = store.query(sql, _p(filt))[0]
    return Mean(float(row["total"]), int(row["n"]))


def writer_reviewer_vs_strong_writer(
    store: Store, filt: MetricsFilter | None = None
) -> PipelineCost:
    """Writer + reviewer vs. strong writer alone.

    SPEC: "cost per accepted ticket for each, to confirm the split actually saves money".

    Implemented (one statement, both groups): *writer + reviewer* = closed tickets
    started on tier 1 or 2 with at least one non-shadow AI review, costed as ticket spend
    (attempts + reviews, escalated tier 3 attempts included); *strong writer alone* =
    closed tickets started on tier 3, costed on their attempts only (review cost left
    out, which is what the strong writer would cost without a reviewer). The filter's
    model is ignored here, since the comparison is between pipelines; repo and dates
    apply.
    """
    params = _p(filt)
    params["model"] = None
    sql = (
        _STARTED
        + """
SELECT
  COALESCE(SUM(CASE WHEN start_tier IN (1, 2) AND reviewed THEN spend_usd END), 0) AS wr_total,
  COALESCE(SUM(start_tier IN (1, 2) AND reviewed), 0) AS wr_n,
  COALESCE(SUM(CASE WHEN start_tier = 3 THEN attempt_spend_usd END), 0) AS sw_total,
  COALESCE(SUM(start_tier = 3), 0) AS sw_n
FROM started WHERE closed
"""
    )
    row = store.query(sql, params)[0]
    return PipelineCost(
        writer_reviewer=Mean(float(row["wr_total"]), int(row["wr_n"])),
        strong_writer_alone=Mean(float(row["sw_total"]), int(row["sw_n"])),
    )


# --- the 2 trend lines ----------------------------------------------------------------


def trend(
    store: Store, filt: MetricsFilter | None = None, *, bucket: Bucket = "week"
) -> list[TrendPoint]:
    """Both trend lines, one point per bucket (``day``, ``week`` from Monday, ``month``).

    SPEC: "Cost per accepted ticket over time" and "Share of tickets solved locally".

    Implemented: closed tickets started on the model, bucketed by the ticket's
    ``created_at`` (UTC). Per bucket: ticket spend ÷ accepted tickets (as in cost per
    accepted ticket), and tickets accepted on a local tier (1–2) ÷ accepted tickets.
    Buckets with no accepted ticket between the first and the last are filled with an
    empty point (value None) so the x axis is evenly spaced in time.
    """
    if bucket not in _BUCKET_SQL:
        raise ValueError(f"bucket must be one of {BUCKETS}")
    sql = _STARTED + (
        f"SELECT {_BUCKET_SQL[bucket]} AS bucket, COUNT(*) AS accepted,"
        " COALESCE(SUM(spend_usd + pair_loser_spend_usd), 0) AS spend,"
        " COALESCE(SUM(accepted_tier IN (1, 2)), 0) AS local"
        " FROM started WHERE closed GROUP BY 1 ORDER BY 1"
    )
    found = {
        r["bucket"]: TrendPoint(r["bucket"], int(r["accepted"]), float(r["spend"]), int(r["local"]))
        for r in store.query(sql, _p(filt))
    }
    if not found:
        return []
    keys = sorted(found)
    out: list[TrendPoint] = []
    d, last = date.fromisoformat(keys[0]), date.fromisoformat(keys[-1])
    while d <= last:
        k = d.isoformat()
        out.append(found.get(k) or TrendPoint(k, 0, 0.0, 0))
        d = _next_bucket(d, bucket)
    return out


def _next_bucket(d: date, bucket: str) -> date:
    if bucket == "day":
        return d + timedelta(days=1)
    if bucket == "week":
        return d + timedelta(days=7)
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def local_share(store: Store, filt: MetricsFilter | None = None) -> Rate:
    """Share of tickets solved locally, over the whole filter range.

    SPEC: "Share of tickets solved locally, the number that tells you whether Max can go".

    Implemented: closed tickets started on the model whose accepted attempt is on a local
    tier (1–2) ÷ closed tickets started on the model.
    """
    sql = _STARTED + (
        "SELECT COALESCE(SUM(accepted_tier IN (1, 2)), 0) AS num, COUNT(*) AS den"
        " FROM started WHERE closed"
    )
    return _rate(store.query(sql, _p(filt))[0])


# --- page helpers ---------------------------------------------------------------------


def recent_tickets(
    store: Store, filt: MetricsFilter | None = None, *, limit: int = 20
) -> list[RecentTicket]:
    """The latest closed tickets in the filter (newest merge first)."""
    params = _p(filt)
    params["limit"] = max(0, int(limit))
    sql = _STARTED + (
        "SELECT id, repo, issue_number, title, start_model, accepted_model, accepted_tier,"
        " n_attempts, spend_usd, created_at, done_at FROM started WHERE closed"
        " ORDER BY done_at DESC, id DESC LIMIT :limit"
    )
    return [
        RecentTicket(
            id=r["id"],
            repo=r["repo"],
            issue_number=r["issue_number"],
            title=r["title"],
            start_model=r["start_model"],
            accepted_model=r["accepted_model"],
            accepted_tier=r["accepted_tier"],
            attempts=int(r["n_attempts"]),
            spend_usd=float(r["spend_usd"]),
            created_at=r["created_at"],
            done_at=r["done_at"],
        )
        for r in store.query(sql, params)
    ]


def post_merge_quality(store: Store, filt: MetricsFilter | None = None) -> PostMerge:
    """Accepted, then reverted / followed up (#24 / todo 47).

    Implemented: closed tickets in the repo/date filter whose *accepted* attempt ran on
    the filter model (any model when None) = ``accepted``; of those, how many have a
    ``reverted`` / ``followup`` row in ``post_merge_signals``. Reads the same table as
    ``metrics.quality.quality_by_model``, with the page's filter.
    """
    sql = (
        "WITH "
        + _SCOPE_VIS
        + """
SELECT COUNT(*) AS accepted,
    COALESCE(SUM(EXISTS (SELECT 1 FROM post_merge_signals ps
        WHERE ps.ticket_id = s.id AND ps.kind = 'reverted')), 0) AS reverted,
    COALESCE(SUM(EXISTS (SELECT 1 FROM post_merge_signals ps
        WHERE ps.ticket_id = s.id AND ps.kind = 'followup')), 0) AS followup
FROM scope s JOIN attempts acc ON acc.id = s.accepted_attempt_id
WHERE s."column" = 'done' AND (:model IS NULL OR acc.model = :model)
"""
    )
    r = store.query(sql, _p(filt))[0]
    return PostMerge(int(r["accepted"]), int(r["reverted"]), int(r["followup"]))


def filter_options(store: Store) -> FilterOptions:
    """Values for the page's filter controls."""
    rows = store.query(
        "SELECT 'w' AS k, model AS v FROM attempts"
        " UNION SELECT 'r', reviewer_model FROM reviews"
        " UNION SELECT 'p', repo FROM tickets ORDER BY 1, 2"
    )
    by: dict[str, list[str]] = {"w": [], "r": [], "p": []}
    for r in rows:
        by[r["k"]].append(r["v"])
    return FilterOptions(writer_models=by["w"], reviewer_models=by["r"], repos=by["p"])


# --- spend split: tickets vs chat vs experiments (#60, chat-design section 5) ----------


@dataclass(frozen=True, slots=True)
class ChatSubCap:
    """Chat's share of one month's live spend against ``chat_monthly_usd`` (option C)."""

    month_start: str  # UTC instants of the month window [start, end)
    month_end: str
    used_usd: float  # ``Store.chat_spend_total`` over the window
    cap_usd: float
    remaining_usd: float = field(init=False)
    share: float | None = field(init=False)  # used ÷ cap; None for a $0 cap

    def __post_init__(self) -> None:
        object.__setattr__(self, "remaining_usd", max(0.0, self.cap_usd - self.used_usd))
        object.__setattr__(self, "share", self.used_usd / self.cap_usd if self.cap_usd else None)


@dataclass(frozen=True, slots=True)
class SpendSplit:
    """Ledger spend in the filter's range, split by who spent it, plus chat activity.

    ``chat_fallback_turns`` is derived, not stored: a turn counts when its session has a
    ledger row inside the turn's window (``started_at`` .. ``ended_at``, or the next
    turn's start while it has no ``ended_at``) whose model is
    not the turn's model. Local calls cost $0 and write no ledger row, so such a row is
    the decision-2 cloud fallback ("local slot busy, used <model>"). Plan rows count here
    (a fallback to a flat-fee plan model is still a fallback). It over-counts only when
    the session's ``small_model`` (tier 1) is a cloud model and was called during a turn
    on another model; the exact note lives in the turn's span (``chat.local_fallback``)."""

    tickets_usd: float
    chat_usd: float
    experiments_usd: float
    chat_sessions: int  # sessions started in the range
    chat_turns: int  # turns started in the range
    chat_turns_by_status: dict[str, int]  # finished / aborted / ... / running (NULL)
    chat_fallback_turns: int
    chat_subcap: ChatSubCap
    live_usd: float = field(init=False)  # tickets + chat: what daily/monthly caps see
    total_usd: float = field(init=False)

    def __post_init__(self) -> None:
        live = self.tickets_usd + self.chat_usd
        object.__setattr__(self, "live_usd", live)
        object.__setattr__(self, "total_usd", live + self.experiments_usd)


@dataclass(frozen=True, slots=True)
class SpendPoint:
    """One trend bucket of the spend split (bucketed by the ledger row's day)."""

    bucket: str  # ISO date of the bucket start
    tickets_usd: float
    chat_usd: float
    experiments_usd: float


@dataclass(frozen=True, slots=True)
class ChatStatus:
    """The chat figure on ``fman status``'s spend line (``SpendSummary``)."""

    chat_today_usd: float
    chat_month_usd: float
    tickets_today_usd: float
    tickets_month_usd: float
    chat_cap_usd: float
    chat_blocked: bool = field(init=False)  # the chat sub-cap is used up

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_blocked", self.chat_month_usd >= self.chat_cap_usd - 1e-9)

    def line(self) -> str:
        out = (
            f"chat ${self.chat_today_usd:.2f} today, ${self.chat_month_usd:.2f} of "
            f"${self.chat_cap_usd:.2f} this month (tickets ${self.tickets_today_usd:.2f} "
            f"today, ${self.tickets_month_usd:.2f} this month)"
        )
        return out + (" [chat sub-cap reached]" if self.chat_blocked else "")


# led: plan = 0 ledger rows in the range, each with the repo it belongs to.
_LEDGER = """
led AS (
    SELECT l.bucket, l.cost_usd, l.created_at, l.chat_session_id,
           COALESCE(cs.repo, t.repo) AS repo
    FROM spend_ledger l
    LEFT JOIN chat_sessions cs ON cs.id = l.chat_session_id
    LEFT JOIN tickets t ON t.id = l.ticket_id
    WHERE l.plan = 0
      AND (:since IS NULL OR l.created_at >= :since)
      AND (:until IS NULL OR l.created_at < :until)
)"""

_SPLIT_SUMS = """
    COALESCE(SUM(CASE WHEN bucket = 'live' AND chat_session_id IS NULL
                      THEN cost_usd END), 0) AS tickets,
    COALESCE(SUM(CASE WHEN chat_session_id IS NOT NULL THEN cost_usd END), 0) AS chat,
    COALESCE(SUM(CASE WHEN bucket = 'experiments' AND chat_session_id IS NULL
                      THEN cost_usd END), 0) AS experiments"""


def _ts(value: datetime) -> str:
    """Datetime → the store's text format (naive = UTC), for bind parameters."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _split_sums(store: Store, params: dict[str, Any]) -> tuple[float, float, float]:
    sql = "WITH " + _LEDGER + "\nSELECT" + _SPLIT_SUMS + " FROM led"
    sql += " WHERE (:repo IS NULL OR repo = :repo)"
    r = store.query(sql, params)[0]
    return float(r["tickets"]), float(r["chat"]), float(r["experiments"])


def utc_month(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month containing ``now`` as [start, end). The caps use the
    configured local month (``Spend.month_window``); callers that have it pass it."""
    now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    first = datetime(now.year, now.month, 1, tzinfo=UTC)
    nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return first, nxt


def chat_subcap(
    store: Store,
    *,
    chat_monthly_usd: float = DEFAULT_CHAT_MONTHLY_USD,
    month: tuple[datetime, datetime] | None = None,
) -> ChatSubCap:
    """Chat spend in ``month`` (default: the UTC month of ``store.now()``) against the
    chat sub-cap. Reads ``Store.chat_spend_total``, the number ``Spend`` checks."""
    start, end = month or utc_month(store.now())
    used = store.chat_spend_total(start, end)
    return ChatSubCap(_ts(start), _ts(end), used, float(chat_monthly_usd))


def spend_split(
    store: Store,
    filt: MetricsFilter | None = None,
    *,
    chat_monthly_usd: float = DEFAULT_CHAT_MONTHLY_USD,
    month: tuple[datetime, datetime] | None = None,
) -> SpendSplit:
    """Live ticket spend vs chat spend vs experiments in the filter's range (ledger
    ``created_at``; repo applies, model ignored), chat sessions and turns started in the
    range, turns that used the cloud fallback, and the chat sub-cap for ``month``."""
    params = _p(filt)
    tickets, chat, experiments = _split_sums(store, params)
    in_range = (
        "(:since IS NULL OR {c} >= :since) AND (:until IS NULL OR {c} < :until)"
        " AND (:repo IS NULL OR cs.repo = :repo)"
    )
    sessions = store.query(
        "SELECT COUNT(*) AS n FROM chat_sessions cs WHERE " + in_range.format(c="cs.started_at"),
        params,
    )[0]["n"]
    turn_where = in_range.format(c="ct.started_at")
    by_status = {
        r["status"]: int(r["n"])
        for r in store.query(
            "SELECT COALESCE(ct.status, 'running') AS status, COUNT(*) AS n FROM chat_turns ct"
            " JOIN chat_sessions cs ON cs.id = ct.session_id WHERE "
            + turn_where
            + " GROUP BY 1 ORDER BY 1",
            params,
        )
    }
    fallback = store.query(
        "SELECT COUNT(*) AS n FROM chat_turns ct JOIN chat_sessions cs ON cs.id = ct.session_id"
        " WHERE "
        + turn_where
        + """ AND EXISTS (
        SELECT 1 FROM spend_ledger l
        WHERE l.chat_session_id = ct.session_id
          AND l.created_at >= ct.started_at
          AND (ct.ended_at IS NULL OR l.created_at <= ct.ended_at)
          AND NOT EXISTS (SELECT 1 FROM chat_turns nx WHERE nx.session_id = ct.session_id
                          AND nx.seq > ct.seq AND nx.started_at <= l.created_at)
          AND l.model IS NOT NULL AND l.model != ct.model)""",
        params,
    )[0]["n"]
    return SpendSplit(
        tickets_usd=tickets,
        chat_usd=chat,
        experiments_usd=experiments,
        chat_sessions=int(sessions),
        chat_turns=sum(by_status.values()),
        chat_turns_by_status=by_status,
        chat_fallback_turns=int(fallback),
        chat_subcap=chat_subcap(store, chat_monthly_usd=chat_monthly_usd, month=month),
    )


def spend_trend(
    store: Store, filt: MetricsFilter | None = None, *, bucket: Bucket = "week"
) -> list[SpendPoint]:
    """The spend split per ``day`` / ``week`` (from Monday) / ``month`` of the ledger
    row's ``created_at`` (UTC); empty buckets between the first and last are filled."""
    if bucket not in _BUCKET_SQL:
        raise ValueError(f"bucket must be one of {BUCKETS}")
    sql = (
        "WITH "
        + _LEDGER
        + f"\nSELECT {_BUCKET_SQL[bucket]} AS bucket,"
        + _SPLIT_SUMS
        + " FROM led WHERE (:repo IS NULL OR repo = :repo) GROUP BY 1 ORDER BY 1"
    )
    found = {
        r["bucket"]: SpendPoint(
            r["bucket"], float(r["tickets"]), float(r["chat"]), float(r["experiments"])
        )
        for r in store.query(sql, _p(filt))
    }
    if not found:
        return []
    keys = sorted(found)
    out: list[SpendPoint] = []
    d, last = date.fromisoformat(keys[0]), date.fromisoformat(keys[-1])
    while d <= last:
        k = d.isoformat()
        out.append(found.get(k) or SpendPoint(k, 0.0, 0.0, 0.0))
        d = _next_bucket(d, bucket)
    return out


def chat_status(
    store: Store,
    *,
    day: tuple[datetime, datetime],
    month: tuple[datetime, datetime],
    chat_monthly_usd: float = DEFAULT_CHAT_MONTHLY_USD,
) -> ChatStatus:
    """Chat and ticket spend for ``fman status``: today and this month, in the windows
    ``Spend.day_window`` / ``Spend.month_window`` give (so the figures match the caps)."""

    def sums(w: tuple[datetime, datetime]) -> tuple[float, float, float]:
        return _split_sums(store, {"since": _ts(w[0]), "until": _ts(w[1]), "repo": None})

    t_day, c_day, _ = sums(day)
    t_month, c_month, _ = sums(month)
    return ChatStatus(
        chat_today_usd=c_day,
        chat_month_usd=c_month,
        tickets_today_usd=t_day,
        tickets_month_usd=t_month,
        chat_cap_usd=float(chat_monthly_usd),
    )


# --- the whole report (the page and the JSON endpoint share it) ------------------------


@dataclass(frozen=True, slots=True)
class WriterRow:
    model: str | None  # None = all models
    first_try_accept_rate: Rate
    kickbacks_per_ticket: KickbackStats
    escalation_rate: Rate
    cost_per_accepted_ticket: Mean
    attempt_minutes_per_accepted_ticket: Mean
    user_reviews_per_accepted_ticket: Mean
    time_to_accept: Duration
    router_hit_rate: Rate
    ai_kickbacks_per_ticket: Mean
    post_merge: PostMerge  # by the accepted attempt's model, not the start model


@dataclass(frozen=True, slots=True)
class ReviewerRow:
    reviewer_model: str | None  # None = all reviewers
    miss_rate: Rate


@dataclass(frozen=True, slots=True)
class Report:
    filter: MetricsFilter
    bucket: str
    overall: WriterRow
    writers: list[WriterRow]
    reviewers: list[ReviewerRow]
    pipeline: PipelineCost
    local_share: Rate
    trend: list[TrendPoint]
    recent: list[RecentTicket]
    # #60: tickets vs chat vs experiments (never part of a writer/router metric)
    spend: SpendSplit | None = None
    spend_trend: list[SpendPoint] = field(default_factory=list)
    # #34: ab:pair W-L per model pair (shown only; never gates promotion)
    pairs: list[PairRecord] = field(default_factory=list)


def writer_row(store: Store, filt: MetricsFilter) -> WriterRow:
    return WriterRow(
        model=filt.model,
        first_try_accept_rate=first_try_accept_rate(store, filt),
        kickbacks_per_ticket=kickbacks_per_ticket(store, filt),
        escalation_rate=escalation_rate(store, filt),
        cost_per_accepted_ticket=cost_per_accepted_ticket(store, filt),
        attempt_minutes_per_accepted_ticket=attempt_minutes_per_accepted_ticket(store, filt),
        user_reviews_per_accepted_ticket=user_reviews_per_accepted_ticket(store, filt),
        time_to_accept=time_to_accept(store, filt),
        router_hit_rate=router_hit_rate(store, filt),
        ai_kickbacks_per_ticket=ai_kickbacks_per_ticket(store, filt),
        post_merge=post_merge_quality(store, filt),
    )


def report(
    store: Store,
    filt: MetricsFilter | None = None,
    *,
    bucket: Bucket = "week",
    chat_monthly_usd: float = DEFAULT_CHAT_MONTHLY_USD,
    month: tuple[datetime, datetime] | None = None,
) -> Report:
    """Every metric for the filter: an overall row plus one row per writer model, one
    row per reviewer model, the pipeline comparison and the trend lines, plus the spend
    split (#60; ``chat_monthly_usd`` and ``month`` are for its chat sub-cap)."""
    filt = filt or MetricsFilter()
    opts = filter_options(store)
    writers: Sequence[str] = [filt.model] if filt.model else opts.writer_models
    reviewers: Sequence[str] = [filt.model] if filt.model else opts.reviewer_models
    return Report(
        filter=filt,
        bucket=bucket,
        overall=writer_row(store, filt),
        writers=[writer_row(store, filt.with_model(m)) for m in writers],
        reviewers=[
            ReviewerRow(m, reviewer_miss_rate(store, filt.with_model(m))) for m in reviewers
        ],
        pipeline=writer_reviewer_vs_strong_writer(store, filt),
        local_share=local_share(store, filt),
        trend=trend(store, filt, bucket=bucket),
        recent=recent_tickets(store, filt),
        spend=spend_split(store, filt, chat_monthly_usd=chat_monthly_usd, month=month),
        spend_trend=spend_trend(store, filt, bucket=bucket),
        pairs=pair_record(store, filt),
    )


def pair_record(store: Store, filt: MetricsFilter | None = None) -> list[PairRecord]:
    """W-L and cost per win per model pair over decided ``ab:pair`` pairs (#34), from
    ``experiments.pair.score``. Only the repo filter applies (a pair's two sides are on
    two models, so the model filter does not pick one). Empty when nothing is paired."""
    return pair_mod.score(store, repo=(filt or MetricsFilter()).repo)


# --- failure steps per variant (#27, wired in #29) -------------------------------------


@dataclass(frozen=True, slots=True)
class FailureSteps:
    """Kickbacks charged to one writer variant's visible attempts, by failure step."""

    variant_id: str
    steps: dict[str, int]  # failure step -> kickbacks
    unlabeled: int  # failure_step still NULL (not labeled yet, or no answer)

    @property
    def labeled(self) -> int:
        return sum(self.steps.values())

    @property
    def total(self) -> int:
        return self.labeled + self.unlabeled

    @property
    def unlabeled_share(self) -> Rate:
        return Rate(self.unlabeled, self.total)


@dataclass(frozen=True, slots=True)
class ReviewerMisses:
    """``reviewer_miss`` labels per reviewer variant, over its judged approvals."""

    variant_id: str
    misses: int  # approvals whose attempt you kicked back, labeled reviewer_miss
    approvals: int  # non-shadow approvals with a known result

    @property
    def rate(self) -> Rate:
        return Rate(self.misses, self.approvals)


def failure_steps_by_variant(store: Store, filt: MetricsFilter | None = None) -> list[FailureSteps]:
    """SPEC "Tracing": failure steps per variant, for writer variants.

    Implemented: every kickback (yours and the AI reviewer's) on a visible attempt of a
    scoped ticket (failed explore and rebase attempts are invisible, dropped tickets left
    out; the model filter is the attempt's model), grouped by the attempt's
    ``variant_id`` (its model name when an old row has none) and ``failure_step``.
    ``reviewer_miss`` is charged to the reviewer (``reviewer_misses_by_variant``), so it is
    left out here; a kickback with no label yet counts as unlabeled."""
    sql = (
        "WITH "
        + _SCOPE_VIS
        + """
SELECT COALESCE(v.variant_id, v.model) AS variant, k.failure_step AS step, COUNT(*) AS n
FROM kickbacks k JOIN vis v ON v.id = k.attempt_id
WHERE (:model IS NULL OR v.model = :model)
  AND (k.failure_step IS NULL OR k.failure_step != 'reviewer_miss')
GROUP BY variant, step
ORDER BY variant
"""
    )
    by: dict[str, tuple[dict[str, int], list[int]]] = {}
    for r in store.query(sql, _p(filt)):
        steps, unl = by.setdefault(r["variant"], ({}, [0]))
        if r["step"] is None:
            unl[0] += int(r["n"])
        else:
            steps[r["step"]] = int(r["n"])
    return [FailureSteps(v, dict(sorted(st.items())), u[0]) for v, (st, u) in sorted(by.items())]


def reviewer_misses_by_variant(
    store: Store, filt: MetricsFilter | None = None
) -> list[ReviewerMisses]:
    """``reviewer_miss`` per reviewer variant: non-shadow ``approve`` reviews whose attempt
    has a known result (accepted or kicked back), and how many of them carry a
    ``reviewer_miss`` label (#27 sets it by rule on your kickback after an approve). The
    model filter is the reviewer model. Shadow reviews never count."""
    sql = (
        "WITH "
        + _SCOPE_VIS
        + """
SELECT COALESCE(r.variant_id, r.reviewer_model) AS variant, COUNT(*) AS approvals,
    COALESCE(SUM(EXISTS (SELECT 1 FROM kickbacks k WHERE k.attempt_id = a.id
                         AND k.failure_step = 'reviewer_miss')), 0) AS misses
FROM reviews r
JOIN attempts a ON a.id = r.attempt_id
JOIN scope s ON s.id = a.ticket_id
WHERE r.shadow = 0 AND r.verdict = 'approve'
  AND a.outcome IN ('accepted', 'kicked_back')
  AND (:model IS NULL OR r.reviewer_model = :model)
GROUP BY variant
ORDER BY variant
"""
    )
    return [
        ReviewerMisses(r["variant"], int(r["misses"]), int(r["approvals"]))
        for r in store.query(sql, _p(filt))
    ]


__all__ = [
    "BUCKETS",
    "LOCAL_TIERS",
    "Bucket",
    "ChatStatus",
    "ChatSubCap",
    "Duration",
    "FailureSteps",
    "FilterOptions",
    "KickbackStats",
    "Mean",
    "MetricsFilter",
    "PipelineCost",
    "PostMerge",
    "Rate",
    "RecentTicket",
    "Report",
    "ReviewerMisses",
    "ReviewerRow",
    "SpendPoint",
    "SpendSplit",
    "TrendPoint",
    "WriterRow",
    "ai_kickbacks_per_ticket",
    "attempt_minutes_per_accepted_ticket",
    "chat_status",
    "chat_subcap",
    "cost_per_accepted_ticket",
    "escalation_rate",
    "failure_steps_by_variant",
    "filter_options",
    "first_try_accept_rate",
    "kickbacks_per_ticket",
    "local_share",
    "pair_record",
    "post_merge_quality",
    "recent_tickets",
    "report",
    "reviewer_miss_rate",
    "reviewer_misses_by_variant",
    "router_hit_rate",
    "spend_split",
    "spend_trend",
    "time_to_accept",
    "trend",
    "utc_month",
    "writer_reviewer_vs_strong_writer",
    "user_reviews_per_accepted_ticket",
    "writer_row",
]
