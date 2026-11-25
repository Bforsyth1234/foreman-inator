"""Chat spend (todo #71, docs/chat-design.md sections 5 and 8, option C; decision 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from foreman_inator.chat import ChatSession, Rung, tier_ladder
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.spend import (
    DEFAULT_CHAT_MONTHLY_USD,
    DEFAULT_CHAT_TURN_BUDGET_USD,
    Spend,
    SpendError,
    chat_cloud_fallback,
)

NY = "America/New_York"
LOCAL = ModelEndpoint("qwen", 1, "local", "qwen3", "http://127.0.0.1:11434/v1", "qwen")
SMALL = ModelEndpoint("tiny", 1, "local", "tiny", "http://127.0.0.1:11434/v1", "tiny")
GLM = ModelEndpoint("glm", 2, "cloud", "glm-x", "https://api.example.com/v1", "glm")
CLAUDE = ModelEndpoint("claude", 3, "cloud", "claude-x", "https://api.anthropic.com/v1", "c")
REPO = "me/toy"


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class ChatStore(Store):
    """A real Store that tags chat ledger rows on the side, so these tests hold before
    and after the store slot (C1a) adds ``record_spend(..., chat_session_id=)`` and the
    column (whose foreign key would need a ``chat_sessions`` row)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.chat_rows: list[tuple[int, int, float, datetime]] = []  # entry, session, $, at

    def record_spend(self, bucket, cost_usd, *, chat_session_id=None, **kw):
        entry = super().record_spend(bucket, cost_usd, **kw)
        if chat_session_id is not None:
            self.chat_rows.append((entry.id, chat_session_id, float(cost_usd), self.now()))
        return entry

    def chat_spend_total(self, since: datetime, until: datetime | None = None) -> float:
        return sum(
            c for _, _, c, at in self.chat_rows if since <= at and (until is None or at < until)
        )


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 23, 17, 0, tzinfo=UTC))


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> ChatStore:
    s = ChatStore(tmp_path / "fman.db", clock=clock)
    yield s
    s.close()


def make_spend(store, daily=5.0, monthly=50.0, **kw) -> Spend:
    caps = SpendCaps(daily_usd=daily, monthly_usd=monthly, experiments_monthly_usd=10.0)
    return Spend(store, Config(spend_caps=caps), tz=NY, **kw)


def ctx(session_id: int = 1, token: str = "t1"):
    return SimpleNamespace(
        kind="registered", bucket="live", chat_session_id=session_id, token=token
    )


def chat(spend: Spend, sid: int = 1, model: str = "claude", repo: str = REPO) -> None:
    spend.register_chat_session(sid, repo=repo, model=model, small_model="tiny")
    spend.start_chat_turn(sid)


def call(spend: Spend, ep: ModelEndpoint, est: float, actual: float | None, sid: int = 1) -> bool:
    ok = spend.hook(ep, est, ctx(sid), phase="estimate")
    if ok and actual is not None:
        spend.hook(ep, actual, ctx(sid), phase="actual")
    return ok


def session(tier: int = 1, repo: str = REPO, sid: int = 1) -> ChatSession:
    return ChatSession(sid, repo, "main", f"fman/chat/{sid}", "idle", tier, "qwen", "now")


LADDER = tier_ladder([LOCAL, GLM, CLAUDE])


# --------------------------------------------------------------------------- defaults


def test_defaults() -> None:
    assert DEFAULT_CHAT_MONTHLY_USD == 20.0
    assert DEFAULT_CHAT_TURN_BUDGET_USD == 1.50


def test_spend_defaults_and_params(store) -> None:
    sp = make_spend(store)
    assert sp.chat_monthly_usd == 20.0 and sp.chat_turn_budget_usd == 1.50
    sp = make_spend(store, chat_monthly_usd=3.0, chat_turn_budget_usd=None)
    assert sp.chat_monthly_usd == 3.0 and sp.chat_turn_budget_usd is None


# --------------------------------------------------------------------------- ledger


def test_chat_call_records_live_row_with_session(store) -> None:
    sp = make_spend(store)
    chat(sp, sid=7)
    assert call(sp, CLAUDE, 0.5, 0.25, sid=7)
    rows = store.query("SELECT bucket, cost_usd, model, ticket_id, attempt_id FROM spend_ledger")
    assert [dict(r) for r in rows] == [
        {
            "bucket": "live",
            "cost_usd": 0.25,
            "model": "claude",
            "ticket_id": None,
            "attempt_id": None,
        }
    ]
    assert [(s, c) for _, s, c, _ in store.chat_rows] == [(7, 0.25)]
    assert sp.chat_turn_spent(7) == pytest.approx(0.25)


def test_record_tags_the_real_store_column(tmp_path, clock) -> None:
    """With migration 012 the plain Store takes ``chat_session_id=`` (no fallback)."""
    plain = Store(tmp_path / "real.db", clock=clock)
    try:
        sid = plain.create_chat_session(REPO, base_branch="main", tier=1, model="claude").id
        sp = make_spend(plain)
        chat(sp, sid=sid)
        assert call(sp, CLAUDE, 0.5, 0.25, sid=sid)
        assert plain.chat_spend(sid) == pytest.approx(0.25)
        assert plain.chat_spend_total(since=clock.at - timedelta(days=1)) == 0.25
    finally:
        plain.close()


def test_local_chat_call_writes_nothing(store) -> None:
    sp = make_spend(store)
    chat(sp, model="qwen")
    assert call(sp, LOCAL, 0.0, 0.0)
    assert store.query("SELECT COUNT(*) AS n FROM spend_ledger")[0]["n"] == 0


# --------------------------------------------------------------------------- option C


def test_chat_counts_toward_monthly_cap(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=10.0, chat_monthly_usd=100.0)
    store.record_spend("live", 9.5, model="claude")  # ticket spend this month
    chat(sp)
    assert not call(sp, CLAUDE, 1.0, None)
    assert sp.refusal_for_chat(1).cap == "monthly"
    assert call(sp, CLAUDE, 0.5, 0.5)  # exactly at the cap is allowed
    # and chat spend blocks the next live ticket call
    assert not sp.allow(0.01)


def test_chat_sub_cap(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_monthly_usd=2.0)
    chat(sp)
    assert call(sp, CLAUDE, 1.0, 1.0)
    sp.start_chat_turn(1)
    assert call(sp, CLAUDE, 1.0, 1.0)
    sp.start_chat_turn(1)
    assert not call(sp, CLAUDE, 0.01, None)
    r = sp.refusal_for_chat(1)
    assert (r.cap, r.cap_usd, r.spent_usd, r.chat_session_id) == ("chat_monthly", 2.0, 2.0, 1)
    assert r.needs_approval
    # ticket spend does not count toward the chat sub-cap
    assert sp.allow(10.0)


def test_sub_cap_counts_in_flight_reservations(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_monthly_usd=1.0)
    chat(sp, sid=1)
    chat(sp, sid=2)
    assert sp.hook(CLAUDE, 0.6, ctx(1), phase="estimate")
    assert not sp.hook(CLAUDE, 0.6, ctx(2, token="t2"), phase="estimate")
    assert sp.refusal_for_chat(2).cap == "chat_monthly"


def test_daily_cap_applies_to_chat(store) -> None:
    sp = make_spend(store, daily=1.0, monthly=100.0, chat_monthly_usd=100.0)
    chat(sp)
    assert call(sp, CLAUDE, 1.0, 1.0)
    sp.start_chat_turn(1)
    assert not call(sp, CLAUDE, 0.1, None)
    assert sp.refusal_for_chat(1).cap == "daily"


# --------------------------------------------------------------------------- turn budget


def test_turn_budget_and_new_turn(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_monthly_usd=100.0)
    chat(sp)
    assert call(sp, CLAUDE, 1.0, 1.0)
    assert call(sp, CLAUDE, 0.5, 0.4)
    assert not call(sp, CLAUDE, 0.2, None)  # 1.4 + 0.2 > 1.50
    r = sp.refusal_for_chat(1)
    assert r.cap == "chat_turn" and not r.needs_approval
    assert r.spent_usd == pytest.approx(1.4) and r.cap_usd == 1.5
    sp.approve_chat(1)  # an approval never lifts the turn budget
    assert not call(sp, CLAUDE, 0.2, None)
    sp.start_chat_turn(1)  # the user sent another message
    assert sp.refusal_for_chat(1) is None
    assert call(sp, CLAUDE, 1.5, 0.1)


def test_turn_budget_counts_reservations(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_turn_budget_usd=1.0)
    chat(sp)
    assert sp.hook(CLAUDE, 0.7, ctx(), phase="estimate")
    assert not sp.hook(CLAUDE, 0.7, ctx(), phase="estimate")
    assert sp.refusal_for_chat(1).cap == "chat_turn"


def test_late_actual_from_old_turn_does_not_charge_new_turn(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0)
    chat(sp)
    assert sp.hook(CLAUDE, 1.0, ctx(), phase="estimate")
    sp.start_chat_turn(1)
    sp.hook(CLAUDE, 1.0, ctx(), phase="actual")
    assert sp.chat_turn_spent(1) == 0.0
    assert [c for _, _, c, _ in store.chat_rows] == [1.0]  # still in the ledger


# --------------------------------------------------------------------------- model check


def test_model_check(store) -> None:
    sp = make_spend(store)
    chat(sp, model="qwen")
    assert call(sp, LOCAL, 0.0, None)
    assert call(sp, SMALL, 0.0, None)  # small_model is always allowed
    assert not call(sp, CLAUDE, 0.1, None)  # a pricier model than the session's
    assert sp.refusal_for_chat(1).cap == "chat_model"
    assert not sp.refusal_for_chat(1).needs_approval
    assert store.query("SELECT COUNT(*) AS n FROM spend_ledger")[0]["n"] == 0


def test_tier_change_applies_to_next_turn(store) -> None:
    sp = make_spend(store)
    chat(sp, model="qwen")
    sp.set_chat_model(1, "claude")
    assert call(sp, LOCAL, 0.0, None)  # the running turn keeps its model
    assert not call(sp, CLAUDE, 0.1, None)
    sp.start_chat_turn(1)
    assert call(sp, CLAUDE, 0.1, 0.1)
    assert not call(sp, LOCAL, 0.0, None)


def test_unregistered_session_refused(store) -> None:
    sp = make_spend(store)
    assert not call(sp, LOCAL, 0.0, None, sid=9)
    assert not call(sp, CLAUDE, 0.1, None, sid=9)
    assert sp.refusal_for_chat(9).cap == "chat_model"
    with pytest.raises(SpendError):
        sp.approve_chat(9)
    chat(sp, sid=9)
    sp.end_chat_session(9)
    assert not call(sp, CLAUDE, 0.1, None, sid=9)


# --------------------------------------------------------------------------- local only


def test_local_only_repo_never_reaches_cloud(store) -> None:
    sp = make_spend(store)
    chat(sp)
    store.set_cloud(REPO, False)
    sp.approve_chat(1)
    assert not call(sp, CLAUDE, 0.1, None)
    r = sp.refusal_for_chat(1)
    assert r.cap == "local_only" and not r.needs_approval
    assert sp.chat_approval_pending(1)  # not consumed
    store.set_cloud(REPO, True)  # read on every call
    assert call(sp, CLAUDE, 0.1, 0.1)


def test_local_only_repo_still_runs_local(store) -> None:
    sp = make_spend(store)
    chat(sp, model="qwen")
    store.set_cloud(REPO, False)
    assert call(sp, LOCAL, 0.0, None)


def test_local_only_by_repo_not_global(store) -> None:
    sp = make_spend(store)
    chat(sp, sid=1, repo="me/secret")
    chat(sp, sid=2, repo=REPO)
    store.set_cloud("me/secret", False)
    assert not call(sp, CLAUDE, 0.1, None, sid=1)
    assert call(sp, CLAUDE, 0.1, 0.1, sid=2)


# --------------------------------------------------------------------------- approvals


def test_approval_covers_one_turn_up_to_approval_usd(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_monthly_usd=1.0, approval_usd=1.0)
    chat(sp)
    assert call(sp, CLAUDE, 1.0, 1.0)
    sp.start_chat_turn(1)
    assert not call(sp, CLAUDE, 0.4, None)
    sp.approve_chat(1)
    sp.approve_chat(1)  # twice before use is one
    sp.start_chat_turn(1)  # the refused message is re-sent
    assert call(sp, CLAUDE, 0.4, 0.4)
    assert not sp.chat_approval_pending(1)
    assert call(sp, CLAUDE, 0.6, 0.6)  # same turn, within the grant
    assert not call(sp, CLAUDE, 0.1, None)  # past approval_usd
    assert sp.refusal_for_chat(1).cap == "approval"
    sp.start_chat_turn(1)
    assert not call(sp, CLAUDE, 0.1, None)  # a new turn asks again
    assert sp.refusal_for_chat(1).cap == "chat_monthly"


def test_approval_is_per_session(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_monthly_usd=0.0)
    chat(sp, sid=1)
    chat(sp, sid=2)
    sp.approve_chat(1)
    assert not call(sp, CLAUDE, 0.1, None, sid=2)
    assert call(sp, CLAUDE, 0.1, 0.1, sid=1)


def test_chat_approval_does_not_touch_ticket_approvals(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_monthly_usd=0.0)
    chat(sp)
    sp.approve_chat(1)
    assert call(sp, CLAUDE, 0.1, 0.1)
    assert store.query("SELECT COUNT(*) AS n FROM spend_approvals")[0]["n"] == 0


# --------------------------------------------------------------------------- tickets unchanged


def test_ticket_calls_unaffected_by_chat_state(store) -> None:
    sp = make_spend(store, daily=5.0, monthly=50.0, chat_monthly_usd=0.0)
    t = store.create_ticket(repo=REPO, issue_number=1, title="t", request="r", base_branch="main")
    a = store.add_attempt(t.id, model="claude", tier=3, trigger="first")
    tctx = SimpleNamespace(kind="attempt", attempt_id=a.id, review_id=None, bucket="live")
    assert sp.hook(CLAUDE, 0.5, tctx, phase="estimate")  # chat sub-cap of 0 does not apply
    sp.hook(CLAUDE, 0.5, tctx, phase="actual")
    assert store.chat_rows == []
    row = store.query("SELECT ticket_id, attempt_id FROM spend_ledger")[0]
    assert (row["ticket_id"], row["attempt_id"]) == (t.id, a.id)


# --------------------------------------------------------------------------- fallback


def test_fallback_picks_first_cloud_rung_at_or_above_tier(store) -> None:
    sp = make_spend(store)
    chat(sp, model="qwen")
    assert chat_cloud_fallback(sp, store, session(tier=1), LADDER, store.now()) is GLM
    assert chat_cloud_fallback(sp, store, session(tier=3), LADDER, store.now()) is CLAUDE
    only_local = (Rung(1, LOCAL),)
    assert chat_cloud_fallback(sp, store, session(tier=1), only_local, store.now()) is None


def test_fallback_never_for_local_only_repo(store) -> None:
    sp = make_spend(store)
    chat(sp, model="qwen")
    store.set_cloud(REPO, False)
    assert chat_cloud_fallback(sp, store, session(), LADDER, store.now()) is None


@pytest.mark.parametrize(
    ("kw", "spent"),
    [
        ({"chat_monthly_usd": 1.0}, 1.0),  # chat sub-cap full
        ({"monthly": 1.0}, 1.0),  # monthly cap full
        ({"chat_turn_budget_usd": 1.0}, 1.0),  # this turn's budget used
    ],
)
def test_fallback_waits_when_a_cap_is_full(store, kw, spent) -> None:
    base = {"daily": 100.0, "monthly": 100.0}
    sp = make_spend(store, **{**base, **kw})
    chat(sp)
    assert call(sp, CLAUDE, spent, spent)
    assert chat_cloud_fallback(sp, store, session(), LADDER, store.now()) is None


def test_fallback_counts_estimate_and_is_pure(store) -> None:
    sp = make_spend(store, daily=100.0, monthly=100.0, chat_monthly_usd=1.0)
    chat(sp, model="qwen")
    now = store.now()
    assert chat_cloud_fallback(sp, store, session(), LADDER, now, estimate_usd=1.0) is GLM
    assert chat_cloud_fallback(sp, store, session(), LADDER, now, estimate_usd=1.01) is None
    assert sp.refusal_for_chat(1) is None
    assert store.query("SELECT COUNT(*) AS n FROM spend_ledger")[0]["n"] == 0


def test_fallback_endpoint_passes_the_gate(store) -> None:
    sp = make_spend(store)
    chat(sp, model="qwen")
    ep = chat_cloud_fallback(sp, store, session(), LADDER, store.now())
    sp.start_chat_turn(1, model=ep.name)
    assert call(sp, GLM, 0.1, 0.1)
    sp.start_chat_turn(1)  # next turn tries local again
    assert not call(sp, GLM, 0.1, None)
    assert call(sp, LOCAL, 0.0, None)
