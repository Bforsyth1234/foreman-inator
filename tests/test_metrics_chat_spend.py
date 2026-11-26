"""Chat spend next to ticket spend in the metrics (#60, docs/chat-design.md section 5,
row C3c): the spend split, the chat sub-cap, chat sessions/turns, the derived cloud
fallback count, ``/api/metrics`` and the ``fman status`` chat line. Chats never reach a
writer, reviewer or router metric (decision: chats don't feed the router, section 6).

Fixture (UTC, fake clock): November 2026.
  11-02 10:00 ticket T1 (acme/app) live $1.00          11-02 10:05 experiments $0.40
  11-03 09:00 chat S1 (acme/app, tier 3 "cloud") turn 1: ledger "cloud" $0.50
  11-03 09:30 chat S1 turn 2 on "small" (local): fallback ledger "cloud" $0.25
  11-04 08:00 chat S2 (acme/lib, tier 1 "small") turn 1: no ledger row (local, $0)
  11-04 08:30 chat S2 turn 2 on "small", still running
  11-10 10:00 ticket T2 (acme/lib) live $2.00
  11-04 08:10 plan row (chat S2, between its turns) $9.00, never counted
  10-31 23:00 chat S1-era row outside November: chat $3.00 (counts in the range, not
  the November sub-cap)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from foreman_inator.db.store import Store
from foreman_inator.metrics import queries as q
from foreman_inator.metrics.web import create_app

APP, LIB = "acme/app", "acme/lib"
NOV = (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def at(self, month: int, day: int, hour: int, minute: int = 0) -> None:
        self.t = datetime(2026, month, day, hour, minute, tzinfo=UTC)


@pytest.fixture
def fx(tmp_path: Path) -> tuple[Store, Clock]:
    clock = Clock()
    s = Store(tmp_path / "f.db", clock=clock)
    # Oct 31: a chat session's early spend (outside November).
    s0 = s.create_chat_session(APP, base_branch="main", tier=3, model="cloud")
    s.record_spend("live", 3.00, model="cloud", chat_session_id=s0.id)

    clock.at(11, 2, 10)
    t1 = s.create_ticket(repo=APP, issue_number=1, title="t1", request="r", base_branch="main")
    s.add_attempt(t1.id, model="small", tier=1, trigger="first")
    s.record_spend("live", 1.00, model="cloud", ticket_id=t1.id)
    clock.at(11, 2, 10, 5)
    s.record_spend("experiments", 0.40, model="cloud", ticket_id=t1.id)

    clock.at(11, 3, 9)
    s1 = s.create_chat_session(APP, base_branch="main", tier=3, model="cloud")
    turn = s.add_chat_turn(s1.id, tier=3, model="cloud")
    clock.at(11, 3, 9, 1)
    s.record_spend("live", 0.50, model="cloud", chat_session_id=s1.id)
    clock.at(11, 3, 9, 2)
    s.finish_chat_turn(turn.id, "finished", cost_usd=0.50)
    clock.at(11, 3, 9, 30)
    turn = s.add_chat_turn(s1.id, tier=1, model="small")
    clock.at(11, 3, 9, 31)  # local slot busy: the proxy sent the call to "cloud"
    s.record_spend("live", 0.25, model="cloud", chat_session_id=s1.id)
    clock.at(11, 3, 9, 32)
    s.finish_chat_turn(turn.id, "finished", cost_usd=0.25)

    clock.at(11, 4, 8)
    s2 = s.create_chat_session(LIB, base_branch="main", tier=1, model="small")
    turn = s.add_chat_turn(s2.id, tier=1, model="small")
    clock.at(11, 4, 8, 5)
    s.finish_chat_turn(turn.id, "aborted")
    clock.at(11, 4, 8, 10)  # between turns, so in no turn's window
    s.record_spend("live", 9.00, model="glm", chat_session_id=s2.id, plan=True)
    clock.at(11, 4, 8, 30)
    s.add_chat_turn(s2.id, tier=1, model="small")  # running

    clock.at(11, 10, 10)
    t2 = s.create_ticket(repo=LIB, issue_number=2, title="t2", request="r", base_branch="main")
    s.add_attempt(t2.id, model="cloud", tier=3, trigger="first")
    s.record_spend("live", 2.00, model="cloud", ticket_id=t2.id)
    clock.at(11, 20, 12)
    return s, clock


def test_split_whole_range(fx: tuple[Store, Clock]) -> None:
    s, _ = fx
    sp = q.spend_split(s, chat_monthly_usd=20.0, month=NOV)
    assert sp.tickets_usd == pytest.approx(3.00)
    assert sp.chat_usd == pytest.approx(3.75)  # 3.00 (Oct) + 0.50 + 0.25; plan row out
    assert sp.experiments_usd == pytest.approx(0.40)
    assert sp.live_usd == pytest.approx(6.75)
    assert sp.total_usd == pytest.approx(7.15)
    assert sp.chat_sessions == 3
    assert sp.chat_turns == 4
    assert sp.chat_turns_by_status == {"aborted": 1, "finished": 2, "running": 1}
    assert sp.chat_fallback_turns == 1  # S1 turn 2 only
    cap = sp.chat_subcap
    assert cap.used_usd == pytest.approx(0.75)  # November only
    assert cap.cap_usd == 20.0
    assert cap.remaining_usd == pytest.approx(19.25)
    assert cap.share == pytest.approx(0.75 / 20.0)


def test_chat_matches_store_chat_spend_total(fx: tuple[Store, Clock]) -> None:
    """The page's chat figure is the number the sub-cap check reads (#75 Verify:
    cost shown matches the spend table)."""
    s, _ = fx
    filt = q.MetricsFilter(since=date(2026, 11, 1), until=date(2026, 11, 30))
    sp = q.spend_split(s, filt, month=NOV)
    assert sp.chat_usd == pytest.approx(s.chat_spend_total(*NOV))
    assert sp.chat_usd == pytest.approx(sp.chat_subcap.used_usd)


def test_split_date_and_repo_filters(fx: tuple[Store, Clock]) -> None:
    s, _ = fx
    nov3 = q.MetricsFilter(since=date(2026, 11, 3), until=date(2026, 11, 3))
    sp = q.spend_split(s, nov3, month=NOV)
    assert (sp.tickets_usd, sp.chat_usd, sp.experiments_usd) == (0.0, 0.75, 0.0)
    assert (sp.chat_sessions, sp.chat_turns, sp.chat_fallback_turns) == (1, 2, 1)

    lib = q.spend_split(s, q.MetricsFilter(repo=LIB), month=NOV)
    assert lib.tickets_usd == pytest.approx(2.00)
    assert lib.chat_usd == 0.0  # S2's only row is a plan row
    assert lib.experiments_usd == 0.0  # T1's experiment row is acme/app
    assert (lib.chat_sessions, lib.chat_turns, lib.chat_fallback_turns) == (1, 2, 0)
    # the model filter is ignored by the split
    assert q.spend_split(s, q.MetricsFilter(model="nope"), month=NOV).tickets_usd == 3.0


def test_fallback_needs_a_different_model(fx: tuple[Store, Clock]) -> None:
    """A cloud turn's own ledger rows are not a fallback; a local turn's cloud row is."""
    s, clock = fx
    before = q.spend_split(s, month=NOV).chat_fallback_turns
    sess = s.create_chat_session(APP, base_branch="main", tier=3, model="cloud")
    turn = s.add_chat_turn(sess.id, tier=3, model="cloud")
    s.record_spend("live", 0.10, model="cloud", chat_session_id=sess.id)
    s.finish_chat_turn(turn.id, "finished")
    assert q.spend_split(s, month=NOV).chat_fallback_turns == before
    clock.at(11, 21, 9)
    turn = s.add_chat_turn(sess.id, tier=1, model="small")
    s.record_spend("live", 0.10, model="cloud", chat_session_id=sess.id)
    assert q.spend_split(s, month=NOV).chat_fallback_turns == before + 1  # running turn too


def test_spend_trend_by_week(fx: tuple[Store, Clock]) -> None:
    s, _ = fx
    pts = q.spend_trend(s, bucket="week")
    assert [p.bucket for p in pts] == ["2026-10-26", "2026-11-02", "2026-11-09"]
    assert (pts[0].tickets_usd, pts[0].chat_usd) == (0.0, 3.0)
    assert pts[1].tickets_usd == pytest.approx(1.0)
    assert pts[1].chat_usd == pytest.approx(0.75)
    assert pts[1].experiments_usd == pytest.approx(0.40)
    assert (pts[2].tickets_usd, pts[2].chat_usd) == (2.0, 0.0)
    with pytest.raises(ValueError):
        q.spend_trend(s, bucket="year")  # type: ignore[arg-type]


def test_chat_status_line(fx: tuple[Store, Clock]) -> None:
    s, _ = fx
    day = (datetime(2026, 11, 3, tzinfo=UTC), datetime(2026, 11, 4, tzinfo=UTC))
    st = q.chat_status(s, day=day, month=NOV, chat_monthly_usd=0.75)
    assert st.chat_today_usd == pytest.approx(0.75)
    assert st.chat_month_usd == pytest.approx(0.75)
    assert st.tickets_today_usd == 0.0
    assert st.tickets_month_usd == pytest.approx(3.0)
    assert st.chat_blocked
    assert st.line() == (
        "chat $0.75 today, $0.75 of $0.75 this month (tickets $0.00 today, $3.00 this month)"
        " [chat sub-cap reached]"
    )
    assert not q.chat_status(s, day=day, month=NOV).chat_blocked  # default $20


def test_subcap_defaults_to_the_utc_month_of_now(fx: tuple[Store, Clock]) -> None:
    s, _ = fx
    cap = q.chat_subcap(s)
    assert cap.month_start.startswith("2026-11-01T00:00:00")
    assert cap.used_usd == pytest.approx(0.75)
    assert cap.cap_usd == q.DEFAULT_CHAT_MONTHLY_USD
    assert q.ChatSubCap("a", "b", 1.0, 0.0).share is None
    assert q.utc_month(datetime(2026, 12, 5, tzinfo=UTC))[1] == datetime(2027, 1, 1, tzinfo=UTC)


def test_chat_never_feeds_writer_or_router_metrics(tmp_path: Path) -> None:
    """Decision (section 6): a chat adds nothing to any ticket metric or filter option."""
    clock = Clock()
    clock.at(11, 2, 10)
    s = Store(tmp_path / "c.db", clock=clock)
    t = s.create_ticket(
        repo=APP, issue_number=1, title="t", request="r", base_branch="main", routed_tier=1
    )
    s.add_attempt(t.id, model="small", tier=1, trigger="first")
    s.record_spend("live", 1.0, model="small", ticket_id=t.id)
    before = q.report(s, month=NOV)
    sess = s.create_chat_session(APP, base_branch="main", tier=2, model="chatonly")
    turn = s.add_chat_turn(sess.id, tier=2, model="chatonly")
    s.record_spend("live", 5.0, model="chatonly", chat_session_id=sess.id)
    s.finish_chat_turn(turn.id, "finished", cost_usd=5.0)
    after = q.report(s, month=NOV)
    assert after.overall == before.overall
    assert after.writers == before.writers
    assert after.reviewers == before.reviewers
    assert after.trend == before.trend and after.local_share == before.local_share
    assert "chatonly" not in q.filter_options(s).writer_models
    assert after.spend is not None and before.spend is not None
    assert after.spend.tickets_usd == before.spend.tickets_usd == 1.0
    assert after.spend.chat_usd == 5.0


def test_api_metrics_has_the_split(fx: tuple[Store, Clock]) -> None:
    s, _ = fx
    path = s.path
    s.close()
    body = TestClient(create_app(path), base_url="http://127.0.0.1:8642").get("/api/metrics")
    assert body.status_code == 200
    rep = body.json()["report"]
    assert rep["spend"]["tickets_usd"] == pytest.approx(3.0)
    assert rep["spend"]["chat_usd"] == pytest.approx(3.75)
    assert rep["spend"]["chat_fallback_turns"] == 1
    assert rep["spend"]["chat_subcap"]["cap_usd"] == q.DEFAULT_CHAT_MONTHLY_USD
    assert [p["bucket"] for p in rep["spend_trend"]][0] == "2026-10-26"


def test_unfinished_turn_window_ends_at_the_next_turn(tmp_path: Path) -> None:
    """A turn left without ``ended_at`` (a crash) does not claim the next turn's rows."""
    clock = Clock()
    clock.at(11, 5, 9)
    s = Store(tmp_path / "w.db", clock=clock)
    sess = s.create_chat_session(APP, base_branch="main", tier=1, model="small")
    s.add_chat_turn(sess.id, tier=1, model="small")  # never finished
    clock.at(11, 5, 10)
    s.add_chat_turn(sess.id, tier=3, model="cloud")
    s.record_spend("live", 0.30, model="cloud", chat_session_id=sess.id)
    assert q.spend_split(s, month=NOV).chat_fallback_turns == 0
