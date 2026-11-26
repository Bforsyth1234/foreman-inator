"""#85: GLM Coding Plan credits (estimated locally) and pauses at z.ai's 429 quota codes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fakes.models import FakeModels
from fakes.worker_env import Env, passing
from foreman_inator.config import Config, ConfigError, SpendCaps, default_models
from foreman_inator.config import from_dict as config_from_dict
from foreman_inator.config import to_dict as config_to_dict
from foreman_inator.db.store import Store
from foreman_inator.proxy import ModelProxy
from foreman_inator.proxy.pricing import is_peak, plan_credits
from foreman_inator.proxy.server import CallContext
from foreman_inator.proxy.zai_quota import ZAI_TZ, parse_quota_error, plausible_reset, reset_time
from foreman_inator.spend import FLAG_PLAN_QUOTA, Spend

# Tuesday 2026-11-24: 07:00 UTC is 15:00 in UTC+8 (peak); 12:00 UTC is 20:00 (off-peak).
PEAK = datetime(2026, 11, 24, 7, 0, tzinfo=UTC)
OFF_PEAK = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


# =========================================================================== credit math


def test_uncached_call_at_peak_is_full_rate():
    # (10,000 x 6.9 + 1,000 x 24) / 10,000
    assert plan_credits("glm-5.3", 10_000, 1_000, at=PEAK) == pytest.approx(9.3)


def test_cached_input_uses_the_cached_multiplier():
    # (2,000 x 6.9 + 8,000 x 1.7 + 1,000 x 24) / 10,000
    got = plan_credits("glm-5.3", 10_000, 1_000, cache_read=8_000, at=PEAK)
    assert got == pytest.approx(5.14)


def test_off_peak_is_half_rate():
    assert plan_credits("glm-5.3", 10_000, 1_000, at=OFF_PEAK) == pytest.approx(4.65)
    saturday_afternoon = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)  # 15:00 UTC+8, Saturday
    assert plan_credits("glm-5.3", 10_000, 1_000, at=saturday_afternoon) == pytest.approx(4.65)


def test_peak_hours_are_14_to_18_utc8_weekdays():
    assert is_peak(datetime(2026, 11, 24, 6, 0, tzinfo=UTC))  # 14:00
    assert not is_peak(datetime(2026, 11, 24, 5, 59, tzinfo=UTC))  # 13:59
    assert not is_peak(datetime(2026, 11, 24, 10, 0, tzinfo=UTC))  # 18:00
    assert not is_peak(datetime(2026, 11, 29, 7, 0, tzinfo=UTC))  # Sunday


def test_other_models_and_unknown_model():
    assert plan_credits("glm-5.4", 10_000, 1_000, at=PEAK) == pytest.approx(6.2)
    assert plan_credits("GLM-5.3-Flash", 10_000, 1_000, at=PEAK) == pytest.approx(1.55)
    assert plan_credits("glm-4.5", 10_000, 1_000, at=PEAK) is None


# =========================================================================== error parsing

NOW = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)


def quota_body(code: str | int, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode()


def test_1308_reset_time_without_a_zone_is_utc8():
    msg = "Usage limit reached for 5 hour. Your limit will reset at 2026-11-25 18:30:00"
    hit = parse_quota_error(429, quota_body("1308", msg))
    assert (hit.code, hit.kind) == (1308, "five_hour")
    assert hit.resets_at == datetime(2026, 11, 25, 10, 30, tzinfo=UTC)


def test_1310_weekly_and_other_reset_formats():
    hit = parse_quota_error(
        429, quota_body(1310, "Weekly/Monthly Limit Exhausted. Your limit will reset at "
                        "2026-11-28T00:00:00Z")
    )  # fmt: skip
    assert (hit.kind, hit.resets_at) == ("weekly", datetime(2026, 11, 28, tzinfo=UTC))
    epoch_ms = int(datetime(2026, 11, 25, 9, 0, tzinfo=UTC).timestamp() * 1000)
    assert reset_time(f"resets at {epoch_ms}") == datetime(2026, 11, 25, 9, tzinfo=UTC)
    assert reset_time("Your limit will reset at soon") is None
    past = reset_time("reset at 2026-11-20 10:00:00")
    assert past is not None and plausible_reset(past, NOW) is None  # in the past
    far = datetime(2026, 12, 25, tzinfo=UTC)
    assert plausible_reset(far, NOW) is None  # more than 8 days ahead


def test_non_quota_errors_are_ignored():
    assert parse_quota_error(429, quota_body("1302", "rate limit")) is None
    assert parse_quota_error(400, quota_body("1308", "x")) is None
    assert parse_quota_error(429, b"not json") is None
    hit = parse_quota_error(429, quota_body("1309", "Your GLM Coding Plan has expired"))
    assert (hit.kind, hit.resets_at) == ("expired", None)


# =========================================================================== config


def test_plan_tier_config():
    def with_glm(**kw) -> dict:
        data = config_to_dict(Config())
        data["models"] = [dict(m, **kw) if m["name"] == "glm" else m for m in data["models"]]
        return data

    cfg = config_from_dict(with_glm(plan_usd_month=18.0, plan_tier="lite"))
    assert cfg.model("glm").plan_tier == "lite"
    with pytest.raises(ConfigError, match="plan_tier must be one of"):
        config_from_dict(with_glm(plan_usd_month=18.0, plan_tier="ultra"))
    with pytest.raises(ConfigError, match="plan_tier needs plan_usd_month"):
        config_from_dict(with_glm(plan_tier="lite"))


# =========================================================================== spend + store


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


PLAN = replace(Config().model("glm"), plan_usd_month=18.0, plan_tier="lite")


def plan_config() -> Config:
    models = tuple(PLAN if m.name == "glm" else m for m in default_models())
    return Config(spend_caps=SpendCaps(), models=models)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Store:
    s = Store(tmp_path / "fman.db", clock=clock)
    yield s
    s.close()


def ctx(attempt_id: int | None = None) -> CallContext:
    kind = "attempt" if attempt_id is not None else "none"
    return CallContext(kind=kind, attempt_id=attempt_id)  # type: ignore[arg-type]


def ticket_attempt(store: Store) -> tuple[int, int]:
    t = store.create_ticket(
        repo="me/toy", issue_number=1, title="t", request="r", base_branch="main"
    )
    return t.id, store.add_attempt(t.id, model="glm", tier=2, trigger="first").id


def test_status_line_shows_credits_against_the_plan_limits(store, clock):
    spend = Spend(store, plan_config(), tz="UTC")
    clock.at = NOW - timedelta(days=2)  # in the week, not in the last 5 h
    spend.hook(PLAN, 0.5, ctx(), phase="actual", credits=100.0)
    clock.at = NOW - timedelta(hours=1)
    spend.hook(PLAN, 0.2, ctx(), phase="actual", credits=20.0)
    spend.hook(PLAN, 0.1, ctx(), phase="actual", credits=10.0)
    clock.at = NOW
    (c,) = spend.summary().credits
    assert (c.used_5h, c.limit_5h, c.used_week, c.limit_week) == (30.0, 2_000, 130.0, 10_000)
    line = spend.summary().status_line()
    assert "GLM credits (glm, lite, estimates): 30 of 2,000 in the last 5h" in line
    assert "130 of 10,000 this week" in line
    assert "starts at first use" in line and "at purchase" in line


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_proxy_stores_credits_and_pauses_the_plan_at_1308(store, clock):
    spend = Spend(store, plan_config(), tz="UTC")
    fake = FakeModels()
    proxy = ModelProxy(
        (PLAN,), spend_hook=spend.hook, transport=fake.transport, key_resolver=lambda ep: "sk-x"
    )
    tid, aid = ticket_attempt(store)
    body = {"model": "glm", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}
    reset = (NOW + timedelta(hours=2)).astimezone(ZAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app), base_url=proxy.url
    ) as c:
        fake.queue_reply("ok", tokens=(10_000, 1_000))
        r = await c.post(f"/a/{aid}/v1/chat/completions", json=body)
        assert r.status_code == 200
        (credits,) = [row["credits"] for row in store.query("SELECT credits FROM spend_ledger")]
        assert credits > 0  # the call's estimated credits are on its plan row
        fake.fail_next(429, {"error": {"code": "1308", "message": f"reset at {reset}"}})
        r = await c.post(f"/a/{aid}/v1/chat/completions", json=body)
        assert r.status_code == 429 and r.json()["error"]["code"] == "1308"
        sent = len(fake.requests)
        r = await c.post(f"/a/{aid}/v1/chat/completions", json=body)
        assert r.status_code == 429 and r.json()["error"]["code"] == "spend_cap_reached"
        assert len(fake.requests) == sent  # paused: nothing sent
    refusal = spend.refusal_for_attempt(aid)
    assert (refusal.cap, refusal.quota_code, refusal.ticket_id) == ("plan_quota", 1308, tid)
    assert refusal.needs_approval  # the worker pauses the ticket (a spend_cap pause)
    assert refusal.resets_at == NOW + timedelta(hours=2)
    assert refusal.flag_reason.startswith(f"{FLAG_PLAN_QUOTA} glm: 5-hour limit reached")
    # The pause survives a restart (kept in the store), and lifts at the reset time.
    again = Spend(store, plan_config(), tz="UTC")
    assert again.plan_gate(PLAN, tid) is not None
    ticket = replace(store.get_ticket(tid), flag_reason=refusal.flag_reason)
    assert again.resume_reason(ticket) is None
    assert "(paused: z.ai code 1308" in again.summary().status_line()
    clock.at = NOW + timedelta(hours=2, minutes=1)
    assert again.resume_reason(ticket) == "cap_clear"
    assert again.plan_gate(PLAN, tid) is None


def test_expired_plan_waits_for_the_user(store, clock):
    spend = Spend(store, plan_config(), tz="UTC")
    tid, aid = ticket_attempt(store)
    hit = parse_quota_error(429, quota_body("1309", "plan expired"))
    spend.hook(PLAN, 0.0, ctx(aid), phase="quota", quota=hit)
    refusal = spend.refusal_for_attempt(aid)
    ticket = replace(store.get_ticket(tid), flag_reason=refusal.flag_reason)
    clock.at = NOW + timedelta(days=30)
    assert spend.resume_reason(ticket) is None  # no reset time: only the user lifts it
    assert "expired" in spend.approval_comment(ticket, refusal)
    spend.approve(tid)
    assert spend.resume_reason(ticket) == "approved"
    assert spend.plan_gate(PLAN, tid) is None
    assert store.open_spend_approval(tid) is None  # the approval was used to lift it


# =========================================================================== the worker


def plan_env(tmp_path: Path, code: str, reset_in: timedelta) -> tuple[Env, dict[str, int]]:
    """An Env whose tier 2 GLM is a Lite Coding Plan, and whose fake z.ai answers the
    first GLM writer call with a 429 quota error ``code``."""
    models = tuple(
        replace(m, plan_usd_month=18.0, plan_tier="lite") if m.name == "glm" else m
        for m in default_models()
    )
    e = Env(tmp_path, models=models)
    reset = (datetime.now(UTC) + reset_in).astimezone(ZAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    seen = {"glm": 0, "fail": 1}
    inner = e.models.transport.handler

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if body.get("model") == "glm-5.3":
            seen["glm"] += 1
            if seen["fail"]:
                seen["fail"] -= 1
                msg = f"Usage limit reached. Your limit will reset at {reset}"
                return httpx.Response(429, content=quota_body(code, msg))
        return await inner(request)

    e.models.transport.handler = handler
    return e, seen


@pytest.mark.parametrize(
    ("code", "reset_in", "window"),
    [("1308", timedelta(hours=2), "5-hour"), ("1310", timedelta(days=3), "weekly")],
)
def test_quota_error_pauses_tier2_without_a_failed_attempt_or_escalation(
    tmp_path: Path, code: str, reset_in: timedelta, window: str
) -> None:
    e, seen = plan_env(tmp_path, code, reset_in)
    try:
        n = e.label("Fix it", "Change feature.", "tier:2")
        e.once(passing())
        t = e.ticket(n)
        (a1,) = e.attempts(n)
        assert (a1.model, a1.outcome) == ("glm", "superseded")
        assert t.flag_reason.startswith(f"{FLAG_PLAN_QUOTA} glm: {window} limit reached")
        assert e.worker.uncounted_attempts(t) == {a1.id}
        assert not e.worker.history(t).attempts  # not a failed attempt
        (note,) = [c for c in e.issue_comments(n) if "GLM Coding Plan" in c]
        assert f"{window} limit" in note and f"code {code}" in note
        # Still paused: no new attempt, nothing escalated to Claude, nothing sent to GLM.
        calls = seen["glm"]
        e.once(passing())
        assert len(e.attempts(n)) == 1 and seen["glm"] == calls
        assert "claude-opus" not in e.driver.endpoints
        # Another tier 2 ticket waits too, before any attempt starts.
        m = e.label("Other", "Change feature.", "tier:2")
        e.once(passing())
        assert not e.attempts(m) and e.ticket(m).flag_reason.startswith(FLAG_PLAN_QUOTA)
        assert seen["glm"] == calls
        # The reset passes: the ticket resumes on GLM by itself.
        e.spend._clock = lambda: datetime.now(UTC) + reset_in + timedelta(minutes=1)
        e.once(passing())
        a1, a2 = e.attempts(n)
        assert (a2.model, a2.trigger, a2.outcome) == ("glm", "first", None)
        assert e.ticket(n).flag_reason is None
        assert e.ticket(m).flag_reason is None
    finally:
        e.close()
