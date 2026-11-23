"""Spend caps (#18): daily/monthly live caps, experiments budget, reservation, approval."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fakes.models import FakeModels
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.proxy import ModelProxy
from foreman_inator.proxy.server import CallContext
from foreman_inator.spend import FLAG_SPEND_CAP, Spend, SpendError

NY = "America/New_York"
LOCAL = ModelEndpoint("qwen", 1, "local", "qwen3.6:35b-a3b", "http://127.0.0.1:11434/v1", "qwen")
CLOUD = ModelEndpoint("claude", 3, "cloud", "claude-x", "https://api.anthropic.com/v1", "claude")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def set(self, iso: str) -> None:
        self.at = datetime.fromisoformat(iso)


@pytest.fixture
def clock() -> Clock:
    # 2026-11-23 12:00 in New York (EST, UTC-5)
    return Clock(datetime(2026, 11, 23, 17, 0, tzinfo=UTC))


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Store:
    s = Store(tmp_path / "fman.db", clock=clock)
    yield s
    s.close()


def make_spend(store: Store, daily=5.0, monthly=50.0, experiments=10.0, **kw) -> Spend:
    caps = SpendCaps(daily_usd=daily, monthly_usd=monthly, experiments_monthly_usd=experiments)
    return Spend(store, Config(spend_caps=caps), tz=kw.pop("tz", NY), **kw)


def ticket_attempt(store: Store, issue: int = 1) -> tuple[int, int]:
    t = store.create_ticket(
        repo="me/toy", issue_number=issue, title="t", request="r", base_branch="main"
    )
    a = store.add_attempt(t.id, model="claude", tier=3, trigger="first")
    return t.id, a.id


def ctx(attempt_id: int | None = None, bucket: str = "live", **kw) -> CallContext:
    kind = "attempt" if attempt_id is not None else "none"
    return CallContext(kind=kind, attempt_id=attempt_id, bucket=bucket, **kw)  # type: ignore[arg-type]


def call(spend: Spend, cost: float, c: CallContext, ep: ModelEndpoint = CLOUD) -> bool:
    """One whole call: estimate == actual."""
    ok = spend.hook(ep, cost, c, phase="estimate")
    if ok:
        spend.hook(ep, cost, c, phase="actual")
    return bool(ok)


def ledger_rows(store: Store) -> int:
    return store.query("SELECT COUNT(*) AS n FROM spend_ledger")[0]["n"]


# --------------------------------------------------------------------------- caps


def test_daily_cap_boundary_exactly_at_cap_is_allowed(store):
    spend = make_spend(store)
    spend.record(4.0, "live")
    assert spend.allow(1.0, "live")  # 4 + 1 == 5: reaching the cap exactly is allowed
    assert not spend.allow(1.01, "live")
    assert call(spend, 1.0, ctx())
    assert not spend.hook(CLOUD, 0.01, ctx(), phase="estimate")


def test_crossing_daily_cap_blocks_and_next_local_day_resets(store, clock):
    spend = make_spend(store)
    assert call(spend, 3.0, ctx())
    assert not call(spend, 2.5, ctx())  # 3 + 2.5 > 5
    clock.set("2026-11-24T04:59:00+00:00")  # 23:59 New York, same local day
    assert not spend.allow(2.5)
    clock.set("2026-11-24T05:00:00+00:00")  # local midnight
    assert call(spend, 2.5, ctx())
    assert spend.summary().live_today_usd == pytest.approx(2.5)


def test_monthly_cap_holds_across_days_and_resets_next_month(store, clock):
    spend = make_spend(store, daily=5.0, monthly=12.0)
    for day in (23, 24):
        clock.set(f"2026-11-{day}T17:00:00+00:00")
        assert call(spend, 4.5, ctx())
    clock.set("2026-11-25T17:00:00+00:00")  # new day: daily headroom is 5, monthly 3
    assert spend.allow(3.0)
    assert not spend.allow(3.5)
    r = spend.hook(CLOUD, 3.5, ctx(), phase="estimate")
    assert r is False
    clock.set("2026-11-30T23:00:00+00:00")  # 18:00 Nov 30 local: still November
    assert not spend.allow(3.5)
    clock.set("2026-12-01T05:00:00+00:00")  # local midnight Dec 1
    assert spend.allow(5.0)


def test_month_rollover_uses_local_time_not_utc(store, clock):
    spend = make_spend(store, daily=100.0, monthly=10.0)
    clock.set("2026-12-01T04:30:00+00:00")  # Nov 30 23:30 in New York
    spend.record(10.0, "live")
    assert not spend.allow(0.5)  # still November locally: the month is full
    clock.set("2026-12-01T05:30:00+00:00")  # Dec 1 00:30 local
    assert spend.allow(10.0)
    assert spend.summary().live_month_usd == 0.0


def test_day_is_local_not_utc(store, clock):
    spend = make_spend(store, tz="America/Los_Angeles")
    clock.set("2026-11-24T06:00:00+00:00")  # Nov 23 22:00 PST, already Nov 24 in UTC
    spend.record(5.0, "live")
    clock.set("2026-11-24T07:30:00+00:00")  # Nov 23 23:30 PST
    assert not spend.allow(0.1)
    clock.set("2026-11-24T08:00:00+00:00")  # PST midnight
    assert spend.allow(5.0)


@pytest.mark.parametrize(
    ("spent_at", "same_day_at", "next_day_at", "hours"),
    [
        # DST starts Sun 2026-03-08: the local day is 23 h (05:00Z .. 04:00Z)
        ("2026-03-08T06:00:00+00:00", "2026-03-09T03:59:00+00:00", "2026-03-09T04:00:00+00:00", 23),
        # DST ends Sun 2026-11-01: the local day is 25 h (04:00Z .. 05:00Z)
        ("2026-11-01T05:00:00+00:00", "2026-11-02T04:59:00+00:00", "2026-11-02T05:00:00+00:00", 25),
    ],
)
def test_dst_days(store, clock, spent_at, same_day_at, next_day_at, hours):
    spend = make_spend(store)
    clock.set(spent_at)
    start, end = spend.day_window()
    assert end - start == timedelta(hours=hours)
    spend.record(5.0, "live")
    clock.set(same_day_at)
    assert not spend.allow(0.01)
    clock.set(next_day_at)
    assert spend.allow(5.0)


def test_local_models_always_allowed_and_write_no_rows(store):
    spend = make_spend(store, daily=0.0, monthly=0.0, experiments=0.0)
    for _ in range(200):
        assert spend.hook(LOCAL, 0.0, ctx(), phase="estimate") is True
        spend.hook(LOCAL, 0.0, ctx(), phase="actual")
    assert ledger_rows(store) == 0
    assert not spend.hook(CLOUD, 0.01, ctx(), phase="estimate")  # cap 0: cloud is off


def test_zero_cost_cloud_actual_writes_no_row_and_releases_reservation(store):
    spend = make_spend(store)
    assert spend.hook(CLOUD, 5.0, ctx(), phase="estimate")
    assert not spend.allow(0.01)  # reserved
    spend.hook(CLOUD, 0.0, ctx(), phase="actual")  # refused upstream: nothing billed
    assert ledger_rows(store) == 0
    assert spend.allow(5.0)


# --------------------------------------------------------------------------- reservation


def test_parallel_estimates_reserve_headroom(store):
    spend = make_spend(store)
    a, b, c = ctx(), ctx(), ctx()
    assert spend.hook(CLOUD, 2.0, a, phase="estimate")
    assert spend.hook(CLOUD, 2.0, b, phase="estimate")
    assert not spend.hook(CLOUD, 2.0, c, phase="estimate")  # 4 reserved + 2 > 5
    spend.hook(CLOUD, 0.5, a, phase="actual")
    spend.hook(CLOUD, 0.5, b, phase="actual")
    assert spend.summary().reserved_live_usd == 0.0
    assert spend.hook(CLOUD, 2.0, c, phase="estimate")  # 1 spent + 2 <= 5


def test_threads_cannot_blow_through_the_cap(store):
    spend = make_spend(store)
    results: list[bool] = []
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        results.append(bool(spend.hook(CLOUD, 1.0, ctx(), phase="estimate")))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 5


def test_actual_over_estimate_is_recorded_in_full(store):
    spend = make_spend(store)
    tid, aid = ticket_attempt(store)
    c = ctx(aid)
    assert spend.hook(CLOUD, 1.0, c, phase="estimate")
    spend.hook(CLOUD, 1.75, c, phase="actual")  # overshoot is real spend
    (row,) = store.query("SELECT * FROM spend_ledger")
    assert (row["bucket"], row["cost_usd"], row["model"]) == ("live", 1.75, "claude")
    assert (row["ticket_id"], row["attempt_id"]) == (tid, aid)


def test_unsettled_reservation_expires(store, clock):
    spend = make_spend(store, reservation_ttl=timedelta(minutes=15))
    assert spend.hook(CLOUD, 5.0, ctx(), phase="estimate")
    assert not spend.allow(1.0)
    clock.at += timedelta(minutes=16)
    assert spend.allow(5.0)


def test_unknown_attempt_is_recorded_without_foreign_keys(store):
    spend = make_spend(store)
    assert call(spend, 1.0, ctx(9999, bucket="experiments"))
    (row,) = store.query("SELECT * FROM spend_ledger")
    assert (row["bucket"], row["attempt_id"], row["ticket_id"]) == ("experiments", None, None)


# --------------------------------------------------------------------------- experiments


def test_experiments_have_their_own_budget(store):
    spend = make_spend(store, daily=5.0, experiments=3.0)
    tid, aid = ticket_attempt(store)
    assert call(spend, 5.0, ctx())  # live cap full
    assert call(spend, 3.0, ctx(aid, bucket="experiments"))  # not blocked by live
    assert not spend.hook(CLOUD, 0.01, ctx(aid, bucket="experiments"), phase="estimate")
    s = spend.summary()
    assert (s.live_today_usd, s.experiments_month_usd) == (5.0, 3.0)
    assert s.experiments_blocked and s.live_blocked
    # an experiment refusal never asks the user and never uses an approval
    store.approve_spend(tid)
    assert not spend.hook(CLOUD, 0.01, ctx(aid, bucket="experiments"), phase="estimate")
    assert store.open_spend_approval(tid) is not None
    assert spend.refusal_for_ticket(tid) is None
    assert not spend.refusal_for_attempt(aid).needs_approval


def test_experiment_spend_does_not_draw_on_live_caps(store):
    spend = make_spend(store, daily=5.0, experiments=10.0)
    assert call(spend, 8.0, ctx(bucket="experiments"))
    assert spend.allow(5.0, "live")


# --------------------------------------------------------------------------- approval


def test_refused_at_cap_then_one_approval_grants_one_attempt(store):
    spend = make_spend(store)
    tid, aid = ticket_attempt(store)
    spend.record(5.0, "live")
    assert spend.blocked(tid) is not None  # worker: do not start the tier 3 attempt
    assert not spend.hook(CLOUD, 0.5, ctx(aid), phase="estimate")
    r = spend.refusal_for_attempt(aid)
    assert r is not None and r.needs_approval
    assert (r.ticket_id, r.attempt_id, r.cap, r.bucket) == (tid, aid, "daily", "live")
    assert spend.refusal_for_ticket(tid) == r
    assert r.flag_reason.startswith(FLAG_SPEND_CAP)

    ticket = store.flag_ticket(tid, r.flag_reason)
    text = spend.approval_comment(ticket, r)
    assert f"fman approve-spend {tid}" in text
    assert f"<!-- foreman-inator:flag ticket={tid} -->" in text
    assert "$5.00" in text
    assert spend.resume_reason(ticket) is None

    approval = spend.approve(tid)
    assert spend.approve(tid) == approval  # approving twice before use is one approval
    assert spend.resume_reason(ticket) == "approved"
    assert spend.blocked(tid) is None

    # the next attempt's first over-cap call consumes it; later calls of that attempt ride on it
    aid2 = store.add_attempt(tid, model="claude", tier=3, trigger="escalation").id
    assert call(spend, 1.0, ctx(aid2))
    assert store.open_spend_approval(tid) is None
    assert call(spend, 2.0, ctx(aid2))
    # a review of that attempt is covered too
    review = store.record_review(aid2, reviewer_model="glm", verdict="approve")
    assert call(spend, 1.0, CallContext(kind="registered", review_id=review.id))
    # the grant is one daily cap's worth: 4 used, 1 left
    assert not call(spend, 1.5, ctx(aid2))
    over = spend.refusal_for_attempt(aid2)
    assert over.cap == "approval" and over.cap_usd == 5.0
    assert "your last approval" in spend.approval_comment(ticket, over)
    # another attempt of the same ticket asks again
    aid3 = store.add_attempt(tid, model="claude", tier=3, trigger="escalation").id
    assert not call(spend, 0.1, ctx(aid3))
    assert spend.refusal_for_attempt(aid3).cap == "daily"


def test_approval_is_per_ticket(store):
    spend = make_spend(store)
    t1, a1 = ticket_attempt(store, 1)
    _, a2 = ticket_attempt(store, 2)
    spend.record(5.0, "live")
    spend.approve(t1)
    assert not call(spend, 1.0, ctx(a2))
    assert call(spend, 1.0, ctx(a1))


def test_approve_refuses_finished_or_unknown_tickets(store):
    spend = make_spend(store)
    tid, _ = ticket_attempt(store)
    store.set_column(tid, "in_progress")
    store.set_column(tid, "dropped")
    with pytest.raises(SpendError):
        spend.approve(tid)
    with pytest.raises(SpendError):
        spend.approve(424242)


def test_paused_ticket_may_resume_when_the_cap_resets(store, clock):
    spend = make_spend(store)
    tid, _ = ticket_attempt(store)
    spend.record(5.0, "live")
    ticket = store.flag_ticket(tid, f"{FLAG_SPEND_CAP}: daily cap $5.00 reached")
    assert spend.resume_reason(ticket) is None
    clock.set("2026-11-24T05:00:00+00:00")
    assert spend.resume_reason(ticket) == "cap_clear"
    other = store.flag_ticket(tid, "checks_failed: x")
    assert spend.resume_reason(other) is None


def test_summary_numbers(store):
    spend = make_spend(store, daily=5.0, monthly=50.0, experiments=10.0)
    spend.record(1.25, "live")
    spend.record(2.0, "experiments")
    assert spend.hook(CLOUD, 0.5, ctx(), phase="estimate")
    s = spend.summary()
    assert s.live_today_usd == 1.25 and s.live_month_usd == 1.25
    assert s.experiments_month_usd == 2.0
    assert s.reserved_live_usd == 0.5
    assert s.remaining_today_usd == pytest.approx(3.25)
    assert s.remaining_month_usd == pytest.approx(48.25)
    assert s.remaining_experiments_usd == 8.0
    assert not s.live_blocked
    assert "$1.25 of $5.00 today" in s.status_line()
    assert s.as_dict()["day_start"] == "2026-11-23T05:00:00+00:00"


# --------------------------------------------------------------------------- through the proxy


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_proxy_refuses_cloud_call_at_cap_before_sending(store):
    spend = make_spend(store, daily=0.05)
    fake = FakeModels()
    proxy = ModelProxy(
        (LOCAL, CLOUD),
        spend_hook=spend.hook,
        transport=fake.transport,
        key_resolver=lambda ep: "sk-fake-test",
    )
    tid, aid = ticket_attempt(store)
    body = {"model": "claude", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}
    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url=proxy.url) as c:
        fake.queue_reply("ok", tokens=(1000, 100))
        r = await c.post(f"/a/{aid}/v1/chat/completions", json=body)
        assert r.status_code == 200
        assert ledger_rows(store) == 1
        spend.record(0.05, "live")  # the day's cap is now used up
        sent = len(fake.requests)
        r = await c.post(f"/a/{aid}/v1/chat/completions", json=body)
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "spend_cap_reached"
        assert len(fake.requests) == sent  # nothing left the machine
        assert spend.refusal_for_attempt(aid).ticket_id == tid
        fake.queue_reply("local ok", tokens=(10, 10))
        r = await c.post(f"/a/{aid}/v1/chat/completions", json=dict(body, model="qwen"))
        assert r.status_code == 200
    assert ledger_rows(store) == 2
