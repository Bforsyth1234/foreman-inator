"""Attempt minutes and your review count next to dollars on the metrics page (todo #46).

A local model costs $0 per token, so cost per accepted ticket shows a local model that
took three attempts and two of your reviews as free. These tests check the two
per-accepted-ticket metrics that show its real price, per start model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from foreman_inator.db.store import Store
from foreman_inator.metrics import queries as q
from foreman_inator.metrics.web import create_app

REPO = "acme/app"
BASE = "http://127.0.0.1:8765"
F = q.MetricsFilter


def _ticket(s: Store, n: int, title: str = "t") -> int:
    t = s.create_ticket(repo=REPO, issue_number=n, title=title, request="r", base_branch="main")
    s.set_column(t.id, "in_progress")
    return t.id


def _attempt(s: Store, tid: int, model: str, tier: int, trigger: str, secs: float | None):
    a = s.add_attempt(tid, model=model, tier=tier, trigger=trigger)  # type: ignore[arg-type]
    if secs is not None:
        s.finish_attempt(a.id, None, seconds=secs)
    return a.id


def _accept(s: Store, tid: int, aid: int) -> None:
    s.set_accepted_attempt(tid, aid)
    s.set_column(tid, "done")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "m.db"
    s = Store(path)
    # L1: local model, three attempts (20 min total), you kicked back twice, $0.
    t = _ticket(s, 1)
    a1 = _attempt(s, t, "local", 1, "first", 300)
    s.record_kickback(a1, by="user", tag="wrong")
    a2 = _attempt(s, t, "local", 1, "kickback", 600)
    s.record_kickback(a2, by="user", tag=None)
    s.record_kickback(a2, by="reviewer", tag="incomplete")  # the AI's, not yours
    a3 = _attempt(s, t, "local", 1, "kickback", 300)
    _accept(s, t, a3)
    # L2: local model, one attempt with no recorded seconds, accepted first time.
    t = _ticket(s, 2)
    _accept(s, t, _attempt(s, t, "local", 1, "first", None))
    # C1: cloud model, one 2-minute attempt costing $1, accepted first time.
    t = _ticket(s, 3)
    a = _attempt(s, t, "cloud", 3, "first", 120)
    s.add_attempt_usage(a, cost_usd=1.0)
    _accept(s, t, a)
    # Open ticket: your kickback and its minutes don't count until it closes.
    t = _ticket(s, 4)
    a = _attempt(s, t, "local", 1, "first", 6000)
    s.record_kickback(a, by="user", tag="broke")
    # Dropped ticket: never counts.
    t = _ticket(s, 5)
    a = _attempt(s, t, "local", 1, "first", 6000)
    s.set_accepted_attempt(t, a)
    s.set_column(t, "dropped")
    s.close()
    return path


def test_attempt_minutes_per_accepted_ticket(db: Path) -> None:
    s = Store(db, read_only=True)
    local = q.attempt_minutes_per_accepted_ticket(s, F(model="local"))
    assert (local.total, local.count, local.value) == (20.0, 2, 10.0)
    cloud = q.attempt_minutes_per_accepted_ticket(s, F(model="cloud"))
    assert (cloud.total, cloud.count) == (2.0, 1)
    assert q.attempt_minutes_per_accepted_ticket(s).total == 22.0
    s.close()


def test_user_reviews_per_accepted_ticket(db: Path) -> None:
    s = Store(db, read_only=True)
    local = q.user_reviews_per_accepted_ticket(s, F(model="local"))
    # L1: 2 kickbacks + the accepting review = 3; L2: 1. The AI's kickback is left out.
    assert (local.total, local.count, local.value) == (4.0, 2, 2.0)
    cloud = q.user_reviews_per_accepted_ticket(s, F(model="cloud"))
    assert (cloud.total, cloud.count) == (1.0, 1)
    s.close()


def test_free_local_model_still_shows_its_price(db: Path) -> None:
    s = Store(db, read_only=True)
    rep = q.report(s)
    local = next(w for w in rep.writers if w.model == "local")
    assert local.cost_per_accepted_ticket.value == 0.0
    assert local.attempt_minutes_per_accepted_ticket.value == 10.0
    assert local.user_reviews_per_accepted_ticket.value == 2.0
    s.close()


def test_no_data_is_none_not_zero(tmp_path: Path) -> None:
    s = Store(tmp_path / "empty.db")
    assert q.attempt_minutes_per_accepted_ticket(s).value is None
    assert q.user_reviews_per_accepted_ticket(s).value is None
    s.close()


def test_page_and_json_show_minutes_and_reviews(db: Path) -> None:
    c = TestClient(create_app(db), base_url=BASE)
    body = c.get("/").text
    assert "Attempt minutes / accepted ticket" in body
    assert "Your reviews / accepted ticket" in body
    assert "10.0 min <small>(2 accepted)</small>" in body
    assert "2.0 <small>(2 accepted)</small>" in body
    rep = c.get("/api/metrics", params={"model": "local"}).json()["report"]
    row = rep["writers"][0]
    assert row["attempt_minutes_per_accepted_ticket"]["value"] == 10.0
    assert row["user_reviews_per_accepted_ticket"]["value"] == 2.0


def test_new_columns_escape_model_names(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    s = Store(path)
    t = _ticket(s, 1)
    _accept(s, t, _attempt(s, t, '"><img src=x onerror=alert(1)>', 1, "first", 60))
    s.close()
    body = TestClient(create_app(path), base_url=BASE).get("/").text
    assert "<img" not in body
    assert "1.0 min <small>(1 accepted)</small>" in body
