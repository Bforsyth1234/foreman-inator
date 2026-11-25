"""#24 (todo 47), display part: "accepted, then reverted" and follow-up counts per model
on the metrics page and its JSON endpoint, and the kNN router never learns from a
reverted ticket.

The data layer (``post_merge_signals``, ``metrics.quality``) is tested in
``test_post_merge_quality.py``; here signals are recorded straight through the store.

Non-vacuous: drop the ``NOT EXISTS (... 'reverted')`` clause from ``_POOL_FROM`` in
``routing/knn.py`` and ``test_reverted_ticket_is_not_a_knn_neighbour`` fails (the
reverted ticket is back in the pool and the pool size is 3).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from foreman_inator.db.store import Store
from foreman_inator.metrics import quality
from foreman_inator.metrics import queries as q
from foreman_inator.metrics.web import create_app
from foreman_inator.routing import knn

APP, LIB = "acme/app", "acme/lib"
EVIL = "<b>evil</b>"
EMB = knn.embedding_endpoint()


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "fman.db"


@pytest.fixture
def store(path: Path, clock: Clock):
    s = Store(path, clock=clock)
    yield s
    s.close()


_issue = iter(range(1, 1_000_000))


def merged(
    store: Store,
    clock: Clock,
    *,
    model: str,
    repo: str = APP,
    tier: int = 1,
    start_model: str | None = None,
    created: datetime | None = None,
) -> int:
    """A done ticket accepted on ``model``; with ``start_model`` it started there first."""
    if created is not None:
        clock.t = created
    t = store.create_ticket(
        repo=repo,
        issue_number=next(_issue),
        title="Fix typo",
        request="teh",
        base_branch="main",
        routed_tier=tier,
    )
    if start_model is not None:
        a = store.add_attempt(t.id, model=start_model, tier=tier, trigger="first")
        store.finish_attempt(a.id, "failed_checks")
    a = store.add_attempt(
        t.id, model=model, tier=tier, trigger="escalation" if start_model else "first"
    )
    store.finish_attempt(a.id, None)
    store.set_accepted_attempt(t.id, a.id)
    clock.t += timedelta(hours=1)
    store.set_column(t.id, "done")
    return t.id


def signal(store: Store, ticket_id: int, kind: str) -> None:
    assert store.record_post_merge_signal(
        ticket_id, kind, at=store.now(), detail=f"{EVIL} reverts it"
    )


# --------------------------------------------------------------------------- queries


def test_counts_per_accepted_model_match_quality_by_model(store, clock):
    a1 = merged(store, clock, model="small")
    a2 = merged(store, clock, model="small")
    merged(store, clock, model="small")
    b1 = merged(store, clock, model="big", start_model="small")  # escalated: big wrote it
    open_t = store.create_ticket(
        repo=APP, issue_number=next(_issue), title="x", request="y", base_branch="main"
    )
    store.add_attempt(open_t.id, model="small", tier=1, trigger="first")
    signal(store, a1, "reverted")
    signal(store, a1, "followup")
    signal(store, a2, "followup")
    signal(store, b1, "reverted")

    small = q.post_merge_quality(store, q.MetricsFilter(model="small"))
    assert (small.accepted, small.reverted, small.followup) == (3, 1, 2)
    assert small.reverted_rate == q.Rate(1, 3)
    big = q.post_merge_quality(store, q.MetricsFilter(model="big"))
    assert (big.accepted, big.reverted, big.followup) == (1, 1, 0)
    every = q.post_merge_quality(store)
    assert (every.accepted, every.reverted, every.followup) == (4, 2, 2)
    # Same attribution as the data layer's per-model read.
    by_model = {
        m.model: (m.accepted, m.reverted, m.followup) for m in quality.quality_by_model(store)
    }
    assert by_model == {"small": (3, 1, 2), "big": (1, 1, 0)}


def test_repo_and_date_filters_apply(store, clock):
    a = merged(store, clock, model="small", created=datetime(2026, 11, 2, tzinfo=UTC))
    b = merged(store, clock, model="small", repo=LIB, created=datetime(2026, 11, 9, tzinfo=UTC))
    signal(store, a, "reverted")
    signal(store, b, "reverted")
    pm = q.post_merge_quality(store, q.MetricsFilter(repo=LIB))
    assert (pm.accepted, pm.reverted) == (1, 1)
    pm = q.post_merge_quality(store, q.MetricsFilter(until=date(2026, 11, 5)))
    assert (pm.accepted, pm.reverted) == (1, 1)
    pm = q.post_merge_quality(store, q.MetricsFilter(since=date(2026, 11, 20)))
    assert (pm.accepted, pm.reverted, pm.reverted_rate.value) == (0, 0, None)


def test_dropped_ticket_is_left_out(store, clock):
    t = merged(store, clock, model="small")
    signal(store, t, "reverted")
    store.set_column(t, "dropped")
    assert q.post_merge_quality(store).accepted == 0


# --------------------------------------------------------------------------- page and JSON


def test_writer_table_and_json_show_after_merge_counts(store, clock, path):
    r = merged(store, clock, model=EVIL)
    merged(store, clock, model=EVIL)
    f = merged(store, clock, model="small")
    signal(store, r, "reverted")
    signal(store, f, "followup")
    client = TestClient(create_app(path), base_url="http://127.0.0.1:8642")

    page = client.get("/")
    assert page.status_code == 200
    html = page.text
    assert "<th>Accepted, then reverted</th>" in html
    assert "<th>Accepted, then followed up</th>" in html
    assert EVIL not in html and "&lt;b&gt;evil&lt;/b&gt;" in html
    assert "1 <small>of 2 (50%)</small>" in html  # EVIL model: one of two reverted
    assert "1 <small>of 1 (100%)</small>" in html  # small: its one ticket followed up
    assert "1 <small>of 3 (33%)</small>" in html  # all models

    data = client.get("/api/metrics").json()["report"]
    rows = {w["model"]: w["post_merge"] for w in data["writers"]}
    assert rows[EVIL]["accepted"] == 2 and rows[EVIL]["reverted"] == 1
    assert rows[EVIL]["reverted_rate"]["value"] == 0.5
    assert rows["small"]["followup"] == 1 and rows["small"]["reverted"] == 0
    assert data["overall"]["post_merge"] == {
        "accepted": 3,
        "reverted": 1,
        "followup": 1,
        "reverted_rate": {"numerator": 1, "denominator": 3, "value": pytest.approx(1 / 3)},
        "followup_rate": {"numerator": 1, "denominator": 3, "value": pytest.approx(1 / 3)},
    }


def test_model_with_nothing_accepted_reads_no_data(store, clock, path):
    t = store.create_ticket(
        repo=APP, issue_number=next(_issue), title="x", request="y", base_branch="main"
    )
    store.add_attempt(t.id, model="small", tier=1, trigger="first")
    html = TestClient(create_app(path), base_url="http://127.0.0.1:8642").get("/").text
    assert "Accepted, then reverted" in html
    assert "0 <small>of 0" not in html


# --------------------------------------------------------------------------- kNN


def _vectors(store: Store, ids: list[int]) -> None:
    for tid in ids:
        knn.store_vector(
            store, tid, model=EMB.name, vector=knn.normalise([1.0, 0.0]), digest="0" * 64
        )


def test_reverted_ticket_is_not_a_knn_neighbour(store, clock):
    keep = merged(store, clock, model="m1")
    followed = merged(store, clock, model="m1")
    gone = merged(store, clock, model="m2", tier=2)
    _vectors(store, [keep, followed, gone])
    signal(store, gone, "reverted")
    signal(store, followed, "followup")  # a follow-up alone does not exclude

    pool = knn.load_pool(store, model=EMB.name, dims=2)
    assert {row[0] for row in pool} == {keep, followed}
    assert knn.pool_size(store, model=EMB.name) == 2
    assert knn.pool_size(store, model=EMB.name, dims=2) == 2


def test_backfill_does_not_embed_a_reverted_ticket(store, clock):
    keep = merged(store, clock, model="m1")
    gone = merged(store, clock, model="m1")
    signal(store, gone, "reverted")
    assert [tid for tid, _, _ in knn._stale(store, EMB.name)] == [keep]
