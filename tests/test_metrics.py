"""Metrics queries, the local page and the summary comment (todo #20).

The fixture is a real ``Store`` under ``tmp_path``, filled through the store's own
methods with a fake clock. Every metric is checked against an answer worked out by hand
from the ticket list below, and each test quotes the SPEC definition it checks.

Models: ``small`` (tier 1, local), ``big`` (tier 2, local), ``cloud`` (tier 3).
Reviewer ``rev`` (non-shadow), challenger ``shadow-rev`` (shadow only).
Weeks start Monday 2026-11-02 and Monday 2026-11-09.

T1 acme/app  11-02 10:00 routed 1: small first → rev approve $0.10 → accepted. 2h.
   Also a shadow review (request_changes, $0.50) that must never count.
T2 acme/app  11-03 09:00 routed 1: small first failed_checks; small first (retry)
   → rev request_changes $0.10 + reviewer kickback [incomplete]; small ai_kickback
   → rev approve $0.10 → user kickback [style]; small kickback → rev approve $0.10
   → accepted. 4h. Three non-shadow reviews, $0.30.
T3 acme/lib  11-04 08:00 routed 1: small first failed_checks ($0.03); small first
   failed_checks; big escalation → rev approve $0.10 → accepted. 1h.
T4 acme/app  11-10 10:00 routed 2: small EXPLORE failed_checks ×2; big escalation
   → rev approve $0.10 → accepted. 6h.  (Failed exploration: invisible.)
T5 acme/lib  11-11 10:00 routed 3: cloud first $1.00 → rev approve $0.20 → accepted. 1h.
T6 acme/app  11-12 10:00 routed 2: big first → rev approve $0.10 (+ shadow approve)
   → user kickback [wrong]; cloud escalation $2.00 → rev approve $0.20 → accepted. 24h.
T7 acme/app  11-12 12:00 routed 1: small first → rev approve $0.10 → still in testing.
T8 acme/app  11-05 10:00 routed 1: small first $5.00, user kickback [broke] → DROPPED.
T9 acme/lib  11-06 10:00 routed 2: small EXPLORE → rev approve $0.10 → accepted. 30m.
   (Successful exploration: counts.)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from fakes.github import BOT_LOGIN, FakeGitHub
from foreman_inator.db.store import Store
from foreman_inator.metrics import queries as q
from foreman_inator.metrics.web import (
    MetricsServer,
    create_app,
    line_chart,
    serve,
)
from foreman_inator.pr.summary import (
    format_duration,
    post_summary,
    summary_line,
    summary_marker,
)

APP, LIB = "acme/app", "acme/lib"
BASE = "http://127.0.0.1:8642"


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def at(self, text: str) -> None:
        self.t = datetime.fromisoformat(text).replace(tzinfo=UTC)

    def plus(self, **kw: float) -> None:
        self.t += timedelta(**kw)


@dataclass
class Fixture:
    store: Store
    path: Path
    clock: Clock
    ids: dict[str, int]


def _build(path: Path) -> Fixture:
    clock = Clock()
    s = Store(path, clock=clock)
    ids: dict[str, int] = {}
    issue = iter(range(1, 100))

    def ticket(name: str, created: str, repo: str, routed: int, title: str = "") -> int:
        clock.at(created)
        t = s.create_ticket(
            repo=repo,
            issue_number=next(issue),
            title=title or f"ticket {name}",
            request="do it",
            base_branch="main",
            routed_tier=routed,
        )
        s.set_column(t.id, "in_progress")
        ids[name] = t.id
        return t.id

    def attempt(tid: int, model: str, tier: int, trigger: str, outcome=None, cost=0.0) -> int:
        a = s.add_attempt(tid, model=model, tier=tier, trigger=trigger)  # type: ignore[arg-type]
        clock.plus(minutes=1)
        s.finish_attempt(a.id, outcome, cost_usd=cost)
        return a.id

    def review(aid: int, verdict: str, cost: float, *, model="rev", shadow=False) -> None:
        s.record_review(
            aid,
            reviewer_model=model,
            verdict=verdict,
            cost_usd=cost,
            shadow=shadow,  # type: ignore[arg-type]
        )

    def kick(aid: int, by: str, tag: str | None) -> None:
        s.record_kickback(aid, by=by, tag=tag, note="secret note text")  # type: ignore[arg-type]
        s.set_attempt_outcome(aid, "kicked_back")

    def accept(tid: int, aid: int, done: str) -> None:
        s.set_accepted_attempt(tid, aid)
        clock.at(done)
        s.set_column(tid, "done")

    # T1
    t = ticket("T1", "2026-11-02T10:00:00", APP, 1)
    a = attempt(t, "small", 1, "first")
    review(a, "approve", 0.10)
    review(a, "request_changes", 0.50, model="shadow-rev", shadow=True)
    accept(t, a, "2026-11-02T12:00:00")
    # T2
    t = ticket("T2", "2026-11-03T09:00:00", APP, 1)
    attempt(t, "small", 1, "first", "failed_checks")
    a = attempt(t, "small", 1, "first")
    review(a, "request_changes", 0.10)
    kick(a, "reviewer", "incomplete")
    a = attempt(t, "small", 1, "ai_kickback")
    review(a, "approve", 0.10)
    kick(a, "user", "style")
    a = attempt(t, "small", 1, "kickback")
    review(a, "approve", 0.10)
    accept(t, a, "2026-11-03T13:00:00")
    # T3
    t = ticket("T3", "2026-11-04T08:00:00", LIB, 1)
    attempt(t, "small", 1, "first", "failed_checks", cost=0.03)
    attempt(t, "small", 1, "first", "failed_checks")
    a = attempt(t, "big", 2, "escalation")
    review(a, "approve", 0.10)
    accept(t, a, "2026-11-04T09:00:00")
    # T4: failed exploration
    t = ticket("T4", "2026-11-10T10:00:00", APP, 2)
    attempt(t, "small", 1, "explore", "failed_checks")
    attempt(t, "small", 1, "explore", "failed_checks")
    a = attempt(t, "big", 2, "escalation")
    review(a, "approve", 0.10)
    accept(t, a, "2026-11-10T16:00:00")
    # T5
    t = ticket("T5", "2026-11-11T10:00:00", LIB, 3)
    a = attempt(t, "cloud", 3, "first", cost=1.00)
    review(a, "approve", 0.20)
    accept(t, a, "2026-11-11T11:00:00")
    # T6
    t = ticket("T6", "2026-11-12T10:00:00", APP, 2)
    a = attempt(t, "big", 2, "first")
    review(a, "approve", 0.10)
    review(a, "approve", 0.30, model="shadow-rev", shadow=True)
    kick(a, "user", "wrong")
    a = attempt(t, "cloud", 3, "escalation", cost=2.00)
    review(a, "approve", 0.20)
    accept(t, a, "2026-11-13T10:00:00")
    # T7: still open
    t = ticket("T7", "2026-11-12T12:00:00", APP, 1)
    a = attempt(t, "small", 1, "first")
    review(a, "approve", 0.10)
    s.set_column(t, "testing")
    # T8: dropped
    t = ticket("T8", "2026-11-05T10:00:00", APP, 1)
    a = attempt(t, "small", 1, "first", cost=5.00)
    kick(a, "user", "broke")
    s.set_column(t, "dropped")
    # T9: successful exploration
    t = ticket("T9", "2026-11-06T10:00:00", LIB, 2, title='<script>alert("x")</script> & co')
    a = attempt(t, "small", 1, "explore")
    review(a, "approve", 0.10)
    accept(t, a, "2026-11-06T10:30:00")
    return Fixture(s, path, clock, ids)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    f = _build(tmp_path / "fixture.db")
    yield f
    f.store.close()


@pytest.fixture
def ro(fx: Fixture) -> Store:
    """The metrics read the database through a read-only store, as the page does."""
    s = Store(fx.path, read_only=True)
    yield s
    s.close()


F = q.MetricsFilter


def by_model(fn, store: Store, **kw):
    return {m: fn(store, F(model=m, **kw)) for m in ("small", "big", "cloud")}


# --- the 6 core metrics ---------------------------------------------------------------


def test_first_try_accept_rate(ro: Store) -> None:
    """SPEC: "Accepted with no kickbacks and no escalation ÷ tickets started on this model".

    Closed starts: small T1 T2 T3 T9, big T4 T6, cloud T5. First try: T1, T4, T5, T9.
    T4's failed explore attempts are invisible, so T4 started on big (first try).
    """
    r = q.first_try_accept_rate(ro)
    assert (r.numerator, r.denominator) == (4, 7)
    assert r.value == pytest.approx(4 / 7)
    got = {
        m: (x.numerator, x.denominator) for m, x in by_model(q.first_try_accept_rate, ro).items()
    }
    assert got == {"small": (2, 4), "big": (1, 2), "cloud": (1, 1)}


def test_kickbacks_per_ticket(ro: Store) -> None:
    """SPEC: "Mean kickbacks charged to this model's attempts, split by tag".

    Kickbacks on visible attempts: T2 incomplete (AI), T2 style (you), T6 wrong (you);
    T8's is dropped. Tickets with a visible attempt: 8 (T8 dropped).
    """
    k = q.kickbacks_per_ticket(ro)
    assert (k.total, k.tickets) == (3, 8)
    assert k.value == pytest.approx(3 / 8)
    assert k.by_tag == {"incomplete": 1, "style": 1, "wrong": 1}
    assert k.by_who == {"user": 2, "reviewer": 1}
    assert k.per_tag["wrong"] == pytest.approx(1 / 8)
    per = by_model(q.kickbacks_per_ticket, ro)
    assert (per["small"].total, per["small"].tickets) == (2, 5)  # T1 T2 T3 T7 T9
    assert (per["big"].total, per["big"].tickets) == (1, 3)  # T3 T4 T6
    assert (per["cloud"].total, per["cloud"].tickets) == (0, 2)
    assert per["cloud"].value == 0.0


def test_escalation_rate(ro: Store) -> None:
    """SPEC: "Tickets this model handed up a tier ÷ tickets it started".

    Starts (open included): small T1 T2 T3 T7 T9, big T4 T6, cloud T5; escalated T3, T6.
    Leaving T4's failed exploration is not an escalation.
    """
    r = q.escalation_rate(ro)
    assert (r.numerator, r.denominator) == (2, 8)
    got = {m: (x.numerator, x.denominator) for m, x in by_model(q.escalation_rate, ro).items()}
    assert got == {"small": (1, 5), "big": (1, 2), "cloud": (0, 1)}


def test_cost_per_accepted_ticket(ro: Store) -> None:
    """SPEC: "Total spend on all attempts ÷ accepted tickets, including the failed attempts".

    Spend (attempts + non-shadow reviews): T1 .10, T2 .30, T3 .13 (failed attempt .03
    included), T4 .10, T5 1.20, T6 2.30, T9 .10 = 4.23 over 7. Shadow reviews ($0.80),
    open T7 and dropped T8 ($5) do not count.
    """
    c = q.cost_per_accepted_ticket(ro)
    assert c.count == 7
    assert c.total == pytest.approx(4.23)
    assert c.value == pytest.approx(4.23 / 7)
    per = by_model(q.cost_per_accepted_ticket, ro)
    assert (per["small"].total, per["small"].count) == (pytest.approx(0.63), 4)
    assert (per["big"].total, per["big"].count) == (pytest.approx(2.40), 2)
    assert (per["cloud"].total, per["cloud"].count) == (pytest.approx(1.20), 1)


def test_time_to_accept(ro: Store) -> None:
    """SPEC: "Created → accepted, median".

    0.5h 1h 1h 2h 4h 6h 24h → 2h. small: 0.5 1 2 4 → 1.5h. big: 6 24 → 15h.
    """
    d = q.time_to_accept(ro)
    assert (d.count, d.median_seconds) == (7, 2 * 3600)
    per = by_model(q.time_to_accept, ro)
    assert per["small"].median_seconds == 1.5 * 3600
    assert per["big"].median_seconds == 15 * 3600
    assert per["cloud"].median_seconds == 3600


def test_router_hit_rate(ro: Store) -> None:
    """SPEC: "Tickets accepted on the tier the router first picked".

    Hits T1 T2 T4 T5; misses T3 (1→2), T6 (2→3), T9 (picked 2, accepted on 1 by explore).
    """
    r = q.router_hit_rate(ro)
    assert (r.numerator, r.denominator) == (4, 7)
    got = {m: (x.numerator, x.denominator) for m, x in by_model(q.router_hit_rate, ro).items()}
    assert got == {"small": (2, 4), "big": (1, 2), "cloud": (1, 1)}


# --- the 3 review metrics -------------------------------------------------------------


def test_reviewer_miss_rate(ro: Store) -> None:
    """SPEC: "Reviewer miss rate: your kickbacks on PRs the reviewer approved ÷ PRs it
    approved."

    rev approvals with a known result: T1 T2a3 T2a4 T3 T4 T5 T6a1 T6a2 T9 = 9 (T7 still
    waits on you); you kicked back T2a3 and T6a1. The shadow approval of T6a1 never counts.
    """
    r = q.reviewer_miss_rate(ro)
    assert (r.numerator, r.denominator) == (2, 9)
    assert q.reviewer_miss_rate(ro, F(model="rev")) == r
    shadow = q.reviewer_miss_rate(ro, F(model="shadow-rev"))
    assert (shadow.denominator, shadow.value) == (0, None)


def test_ai_kickbacks_per_ticket(ro: Store) -> None:
    """SPEC: "AI kickbacks per ticket, per writer model".

    One reviewer kickback (T2, small) over 8 tickets; small 1/5, big 0/3, cloud 0/2.
    """
    m = q.ai_kickbacks_per_ticket(ro)
    assert (m.total, m.count) == (1, 8)
    per = by_model(q.ai_kickbacks_per_ticket, ro)
    assert {k: (v.total, v.count) for k, v in per.items()} == {
        "small": (1, 5),
        "big": (0, 3),
        "cloud": (0, 2),
    }


def test_writer_reviewer_vs_strong_writer(ro: Store) -> None:
    """SPEC: "Writer + reviewer vs. strong writer alone: cost per accepted ticket for each,
    to confirm the split actually saves money."

    Writer + reviewer (tier 1–2 start, reviewed): T1 T2 T3 T4 T6 T9 = 3.03 over 6.
    Strong writer alone (tier 3 start, attempts only): T5 = 1.00 over 1.
    """
    p = q.writer_reviewer_vs_strong_writer(ro)
    assert (p.writer_reviewer.total, p.writer_reviewer.count) == (pytest.approx(3.03), 6)
    assert (p.strong_writer_alone.total, p.strong_writer_alone.count) == (1.0, 1)
    assert q.writer_reviewer_vs_strong_writer(ro, F(model="small")) == p  # model ignored


# --- the 2 trend lines ----------------------------------------------------------------


def test_trend_lines(ro: Store) -> None:
    """SPEC: "Cost per accepted ticket over time" and "Share of tickets solved locally".

    Week of 11-02: T1 T2 T3 T9, spend .63, all local. Week of 11-09: T4 T5 T6, spend 3.60,
    local T4 only.
    """
    pts = q.trend(ro, bucket="week")
    assert [p.bucket for p in pts] == ["2026-11-02", "2026-11-09"]
    w1, w2 = pts
    assert (w1.accepted, w1.local) == (4, 4)
    assert w1.cost_per_accepted == pytest.approx(0.63 / 4)
    assert w1.local_share == 1.0
    assert (w2.accepted, w2.local) == (3, 1)
    assert w2.cost_per_accepted == pytest.approx(1.20)
    assert w2.local_share == pytest.approx(1 / 3)
    share = q.local_share(ro)
    assert (share.numerator, share.denominator) == (5, 7)


def test_trend_fills_empty_buckets(ro: Store) -> None:
    days = q.trend(ro, bucket="day")
    assert days[0].bucket == "2026-11-02" and days[-1].bucket == "2026-11-12"
    assert len(days) == 11
    gap = next(p for p in days if p.bucket == "2026-11-05")  # only dropped T8
    assert (gap.accepted, gap.cost_per_accepted, gap.local_share) == (0, None, None)
    months = q.trend(ro, bucket="month")
    assert [p.bucket for p in months] == ["2026-11-01"]
    with pytest.raises(ValueError):
        q.trend(ro, bucket="year")  # type: ignore[arg-type]


# --- exploration (non-vacuity target) -------------------------------------------------


def test_failed_exploration_never_counts_against_the_model(ro: Store) -> None:
    """SPEC: explore attempts "never count against a model in the metrics if they fail".

    T4 started with two failed explore attempts on small. If they counted, small would
    have 5 first-try starts (2/5) and 2 escalations of 6, and all models 4 → 3 of 7.
    """
    assert q.first_try_accept_rate(ro, F(model="small")) == q.Rate(2, 4)
    assert q.escalation_rate(ro, F(model="small")) == q.Rate(1, 5)
    assert q.first_try_accept_rate(ro) == q.Rate(4, 7)


def test_successful_exploration_counts(ro: Store) -> None:
    """T9's explore attempt succeeded on small: it is a first-try accept for small."""
    r = q.first_try_accept_rate(ro, F(model="small", repo=LIB))
    assert (r.numerator, r.denominator) == (1, 2)  # T3 no, T9 yes


# --- filters --------------------------------------------------------------------------


def test_filters_repo_date_and_model(ro: Store) -> None:
    assert q.first_try_accept_rate(ro, F(repo=LIB)) == q.Rate(2, 3)  # T3 T5 T9
    assert q.first_try_accept_rate(ro, F(since=date(2026, 11, 9))) == q.Rate(2, 3)  # T4 T5 T6
    # until is inclusive: 11-04 keeps T1 T2 T3
    assert q.first_try_accept_rate(ro, F(until=date(2026, 11, 4))) == q.Rate(1, 3)
    assert q.first_try_accept_rate(ro, F(model="small", repo=APP)) == q.Rate(1, 2)  # T1 T2
    one_day = F(since=date(2026, 11, 11), until=date(2026, 11, 11))
    assert q.cost_per_accepted_ticket(ro, one_day).total == pytest.approx(1.20)
    with pytest.raises(ValueError):
        F(since=date(2026, 11, 9), until=date(2026, 11, 1))


def test_no_data_is_none_not_zero(tmp_path: Path) -> None:
    s = Store(tmp_path / "empty.db")
    assert q.first_try_accept_rate(s).value is None
    assert q.kickbacks_per_ticket(s).value is None
    assert q.escalation_rate(s).value is None
    assert q.cost_per_accepted_ticket(s).value is None
    assert q.time_to_accept(s).median_seconds is None
    assert q.router_hit_rate(s).value is None
    assert q.reviewer_miss_rate(s).value is None
    assert q.ai_kickbacks_per_ticket(s).value is None
    p = q.writer_reviewer_vs_strong_writer(s)
    assert p.writer_reviewer.value is None and p.strong_writer_alone.value is None
    assert q.trend(s) == [] and q.local_share(s).value is None
    s.close()


def test_filter_values_are_bound_not_formatted(ro: Store, fx: Fixture) -> None:
    evil = "x' OR '1'='1"
    assert q.first_try_accept_rate(ro, F(model=evil)) == q.Rate(0, 0)
    assert q.first_try_accept_rate(ro, F(repo=evil)) == q.Rate(0, 0)
    assert q.first_try_accept_rate(ro, F(model="small'); DROP TABLE tickets; --")).denominator == 0
    assert len(fx.store.list_tickets()) == 9


def test_router_hit_skips_tickets_without_a_router_pick(tmp_path: Path) -> None:
    s = Store(tmp_path / "v1.db")
    t = s.create_ticket(repo=APP, issue_number=1, title="t", request="r", base_branch="main")
    a = s.add_attempt(t.id, model="small", tier=1, trigger="first")
    s.set_accepted_attempt(t.id, a.id)
    s.set_column(t.id, "done")
    assert q.router_hit_rate(s).denominator == 0
    assert q.first_try_accept_rate(s) == q.Rate(1, 1)
    s.close()


def test_report_rows(ro: Store) -> None:
    rep = q.report(ro)
    assert [w.model for w in rep.writers] == ["big", "cloud", "small"]
    assert [r.reviewer_model for r in rep.reviewers] == ["rev", "shadow-rev"]
    assert rep.overall.first_try_accept_rate == q.Rate(4, 7)
    assert [t.id for t in rep.recent][0] == 6  # newest merge first (T6, 11-13)
    only = q.report(ro, F(model="big"))
    assert [w.model for w in only.writers] == ["big"]


# --- the page -------------------------------------------------------------------------


@pytest.fixture
def client(fx: Fixture) -> TestClient:
    return TestClient(create_app(fx.path), base_url=BASE)


def test_page_shows_every_metric_and_escapes_db_text(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    for heading in (
        "First-try accept",
        "Kickbacks / ticket",
        "Escalation rate",
        "Cost / accepted ticket",
        "Time to accept",
        "Router hit rate",
        "AI kickbacks / ticket",
        "Miss rate",
        "Strong writer alone",
        "Cost per accepted ticket",
        "Share of tickets solved locally",
    ):
        assert heading in body
    assert "57% <small>(4/7)</small>" in body  # first-try, all models
    assert body.count("<svg") == 2
    assert '<script>alert("x")</script>' not in body
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; co" in body
    assert "<script" not in body  # no JavaScript at all
    assert "http://" not in body.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "secret note text" not in body


def test_page_escapes_model_and_repo_names(tmp_path: Path) -> None:
    s = Store(tmp_path / "x.db")
    t = s.create_ticket(
        repo="<b>evil</b>/r", issue_number=1, title="t", request="r", base_branch="main"
    )
    s.add_attempt(t.id, model='"><img src=x onerror=alert(1)>', tier=1, trigger="first")
    s.close()
    body = TestClient(create_app(tmp_path / "x.db"), base_url=BASE).get("/").text
    assert "<img" not in body and "<b>evil" not in body
    assert "&lt;b&gt;evil&lt;/b&gt;/r" in body
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in body


def test_json_matches_queries(client: TestClient, ro: Store) -> None:
    r = client.get("/api/metrics", params={"repo": APP, "since": "2026-11-01"})
    assert r.status_code == 200
    rep = r.json()["report"]
    want = q.first_try_accept_rate(ro, F(repo=APP, since=date(2026, 11, 1)))
    got = rep["overall"]["first_try_accept_rate"]
    assert (got["numerator"], got["denominator"], got["value"]) == (
        want.numerator,
        want.denominator,
        want.value,
    )
    assert rep["filter"] == {"model": None, "repo": APP, "since": "2026-11-01", "until": None}
    assert rep["pipeline"]["strong_writer_alone"]["value"] is None  # no tier-3 start in app
    json.dumps(r.json())  # plain JSON


def test_page_filter_form_round_trips(client: TestClient) -> None:
    body = client.get("/", params={"model": "small", "repo": LIB, "bucket": "day"}).text
    assert '<option value="small" selected>small</option>' in body
    assert f'<option value="{LIB}" selected>{LIB}</option>' in body
    assert '<option value="day" selected>day</option>' in body
    assert 'method="get"' in body
    assert "All models" not in body  # one model row only


@pytest.mark.parametrize(
    "params",
    [{"since": "yesterday"}, {"bucket": "year"}, {"since": "2026-11-09", "until": "2026-11-01"}],
)
def test_bad_filters_are_400(client: TestClient, params: dict[str, str]) -> None:
    assert client.get("/", params=params).status_code == 400
    assert client.get("/api/metrics", params=params).status_code == 400


def test_missing_database_is_503(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path / "nope.db"), base_url=BASE)
    assert c.get("/").status_code == 503
    assert c.get("/api/metrics").status_code == 503
    assert not (tmp_path / "nope.db").exists()  # the page never creates a database


def test_non_loopback_host_header_refused(fx: Fixture) -> None:
    """DNS-rebinding guard: a page on another origin cannot read the metrics."""
    app = create_app(fx.path)
    assert TestClient(app, base_url="http://evil.example").get("/").status_code == 403
    for base in ("http://localhost:8642", "http://[::1]:8642", BASE):
        assert TestClient(app, base_url=base).get("/").status_code == 200


def test_page_reads_while_worker_writes(fx: Fixture, client: TestClient) -> None:
    before = client.get("/api/metrics").json()["report"]["overall"]["escalation_rate"]
    t = fx.store.create_ticket(repo=APP, issue_number=99, title="n", request="r", base_branch="m")
    fx.store.add_attempt(t.id, model="small", tier=1, trigger="first")
    after = client.get("/api/metrics").json()["report"]["overall"]["escalation_rate"]
    assert after["denominator"] == before["denominator"] + 1  # fresh store per request


def test_line_chart_gaps_and_escaping() -> None:
    svg = line_chart(
        [("<a>", 1.0), ("b", None), ("c", 2.0), ("d", 3.0)], label="L&<", fmt=lambda v: f"{v}"
    )
    assert svg.count("<polyline") == 1  # c–d; the gap splits a from c
    assert svg.count("<circle") == 3
    assert "&lt;a&gt;" in svg and "L&amp;&lt;" in svg
    assert "no accepted tickets" in line_chart([("a", None)], label="x", fmt=str)


# --- serving on loopback only ---------------------------------------------------------


def test_serve_refuses_non_loopback_hosts(fx: Fixture) -> None:
    for host in ("0.0.0.0", "192.168.1.5", "example.com", "::"):
        with pytest.raises(ValueError, match="loopback"):
            serve(fx.path, host=host, port=0)
        with pytest.raises(ValueError, match="loopback"):
            MetricsServer(fx.path, host=host, port=0)


def test_metrics_server_on_a_real_loopback_socket(fx: Fixture) -> None:
    with MetricsServer(fx.path, port=0) as srv:
        assert srv.url.startswith("http://127.0.0.1:")
        r = httpx.get(srv.url + "api/metrics", timeout=5)
        assert r.status_code == 200
        assert r.json()["report"]["overall"]["first_try_accept_rate"]["numerator"] == 4


# --- summary comment ------------------------------------------------------------------


def _parts(fx: Fixture, name: str):
    s, tid = fx.store, fx.ids[name]
    return (
        s.get_ticket(tid),
        s.list_attempts(tid),
        s.list_reviews(ticket_id=tid, shadow=False),
        s.list_kickbacks(ticket_id=tid),
    )


def test_summary_line_lists_model_attempts_kickbacks_cost_time(fx: Fixture) -> None:
    """SPEC: "a one-line summary comment on the issue: model, attempts, kickbacks and cost"."""
    line = summary_line(*_parts(fx, "T6"))
    assert line == (
        "foreman-inator summary: solved by cloud (tier 3), started on tier 2 · 2 attempts · "
        "1 kickback (1 yours, 0 AI reviewer) · cost $2.30 · 1d from label to merge"
    )
    line = summary_line(*_parts(fx, "T2"))
    assert line == (
        "foreman-inator summary: solved by small (tier 1) · 4 attempts · "
        "2 kickbacks (1 yours, 1 AI reviewer) · cost $0.30 · 4h from label to merge"
    )
    assert "\n" not in line


def test_summary_has_no_notes_reasoning_or_output(fx: Fixture) -> None:
    ticket, attempts, reviews, kickbacks = _parts(fx, "T2")
    line = summary_line(ticket, attempts, reviews, kickbacks)
    assert "secret note text" not in line
    assert ticket.title not in line and ticket.request not in line


def test_summary_shadow_review_cost_left_out(fx: Fixture) -> None:
    s, tid = fx.store, fx.ids["T1"]
    all_reviews = s.list_reviews(ticket_id=tid)  # includes the $0.50 shadow review
    line = summary_line(s.get_ticket(tid), s.list_attempts(tid), all_reviews, [])
    assert "cost $0.10" in line and "0 kickbacks" in line


def test_format_duration() -> None:
    assert [format_duration(x) for x in (None, 5, 61, 3600, 3660, 86400, 90000)] == [
        "unknown",
        "5s",
        "1m",
        "1h",
        "1h 1m",
        "1d",
        "1d 1h",
    ]


def test_post_summary_is_idempotent(fx: Fixture) -> None:
    gh = FakeGitHub()
    gh.create_repo(APP)
    tid = fx.ids["T6"]
    t = fx.store.get_ticket(tid)
    # The fixture's issue number has to exist on the fake: create issues up to it.
    while gh.create_issue(APP, "x") < t.issue_number:
        pass
    gh.comment_as(APP, t.issue_number, summary_marker(tid) + "\nforged", by="mallory")
    first = post_summary(gh, fx.store, tid)
    assert first.posted is True
    assert first.body.splitlines()[0] == summary_marker(tid)
    assert re.fullmatch(r"foreman-inator summary: [^\n]+", first.body.splitlines()[1])
    again = post_summary(gh, fx.store, tid)  # a retried action
    assert again.posted is False and again.comment == first.comment
    posted = gh.writes_named("post_comment")
    assert len(posted) == 1
    bodies = [c.body for c in gh.list_issue_comments(APP, t.issue_number) if c.author == BOT_LOGIN]
    assert bodies == [first.body]
