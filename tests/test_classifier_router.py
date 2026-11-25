"""Classifier router (todo #37 / GitHub #19): logistic regression over stored embeddings,
used only past ``classifier_min_tickets`` and only while it beats kNN on the
chronological held-out 20% (a tie keeps kNN), the check cached for a day.

Unit tests on synthetic vectors and a real ``Store`` under ``tmp_path``; one scenario on
``fakes.worker_env`` (whose fake embedder answers ``[0.1, 0.2, 0.3]`` for every text).
"""

from __future__ import annotations

import dataclasses
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fakes.worker_env import REPO, Env, failing
from foreman_inator import tracing
from foreman_inator import worker_route as wr
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.routing import classifier as clf
from foreman_inator.routing import knn
from foreman_inator.routing.rules import Route
from foreman_inator.tickets import decompose

EMB = knn.embedding_endpoint()
CONFIG = Config()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "fman.db", clock=clock)
    try:
        yield s
    finally:
        s.close()


def noisy(rnd: random.Random, tier: int, dims: int = 8, *, hi: int = 3) -> list[float]:
    """The tier is the sign of dimension 0 (small); the other dimensions are large noise,
    so cosine neighbours are close to random while a linear model separates them."""
    v = [rnd.gauss(0, 1) for _ in range(dims)]
    v[0] = rnd.uniform(0.05, 0.3) * (1 if tier == 1 else -1)
    return knn.normalise(v)


def clustered(rnd: random.Random, tier: int, dims: int = 8) -> list[float]:
    """Tight clusters: both kNN and the classifier get every one right."""
    v = [rnd.gauss(0, 0.01) for _ in range(dims)]
    v[tier] += 1.0
    return knn.normalise(v)


_issue = iter(range(1, 1_000_000))


def seed(store: Store, tier: int, vec: list[float], *, repo: str = "me/toy", model=EMB.name):
    t = store.create_ticket(
        repo=repo,
        issue_number=next(_issue),
        title=f"old {tier}",
        request="old work",
        base_branch="main",
        routed_tier=tier,
    )
    a = store.add_attempt(t.id, model=f"m{tier}", tier=tier, trigger="first")
    store.finish_attempt(a.id, None)
    store.set_accepted_attempt(t.id, a.id)
    store.set_column(t.id, "done")
    knn.store_vector(store, t.id, model=model, vector=vec, digest="0" * 64)
    return t


def examples(make, n: int, *, seed_: int = 7, hi: int = 3) -> list[clf.Example]:
    rnd = random.Random(seed_)
    out = []
    for i in range(n):
        tier = 1 if i % 2 else hi
        out.append(clf.Example(i + 1, tier, make(rnd, tier)))
    return out


# --------------------------------------------------------------------------- the model


def test_train_separates_a_linear_signal_that_knn_misses():
    ex = examples(noisy, 160)
    train, test = clf.chronological_split(ex)
    model = clf.train(train)
    clf_ok = sum(model.predict(e.vector) == e.tier for e in test)
    knn_ok = sum(clf.knn_predict(train, e.vector, 10) == e.tier for e in test)
    assert clf_ok >= 0.9 * len(test)
    assert clf_ok > knn_ok
    probs = model.probabilities(test[0].vector)
    assert set(probs) == {1, 3} and abs(sum(probs.values()) - 1.0) < 1e-9


def test_training_is_deterministic_and_handles_one_class():
    ex = examples(noisy, 40)
    assert clf.train(ex) == clf.train(ex)
    only = [clf.Example(i, 2, e.vector) for i, e in enumerate(ex)]
    m = clf.train(only)
    assert m.classes == (2,) and m.predict(ex[0].vector) == 2


def test_chronological_split_holds_out_the_newest_fifth():
    ex = [clf.Example(i, 1, [1.0, 0.0]) for i in (5, 1, 9, 3, 7, 2, 8, 4, 6, 10)]
    train, test = clf.chronological_split(ex)
    assert [e.ticket_id for e in test] == [9, 10]
    assert [e.ticket_id for e in train] == list(range(1, 9))


# --------------------------------------------------------------------------- the gate


def test_compare_uses_the_classifier_only_when_strictly_better():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    win = clf.compare(examples(noisy, 160), k=10, embed_model="e", dims=8, now=now)
    assert win.use_classifier and win.classifier_correct > win.knn_correct
    tie = clf.compare(examples(clustered, 60), k=5, embed_model="e", dims=8, now=now)
    assert tie.classifier_correct == tie.knn_correct == tie.test == 12
    assert not tie.use_classifier  # a tie goes to kNN


def knn_route(tier: int = 2) -> Route:
    return Route(tier=tier, reason="knn: ...", source="knn", requested_tier=tier)


def query_ticket(store: Store, vec: list[float]):
    t = store.create_ticket(
        repo="me/toy", issue_number=next(_issue), title="new", request="new", base_branch="main"
    )
    digest = knn.text_digest(knn.request_text(t))
    knn.store_vector(store, t.id, model=EMB.name, vector=vec, digest=digest)
    return t


def route(store, t, gate, *, min_tickets=100, r=None):
    return clf.route_classifier(
        t,
        store=store,
        config=CONFIG,
        embedder=EMB,
        gate=gate,
        knn_route=r or knn_route(),
        min_tickets=min_tickets,
        k=10,
    )


def test_routes_past_min_tickets_when_it_wins_with_reason_and_span(store):
    rnd = random.Random(3)
    for i in range(160):
        tier = 1 if i % 2 else 3
        seed(store, tier, noisy(rnd, tier))
    gate = clf.Gate()
    t = query_ticket(store, noisy(rnd, 1))
    assert route(store, t, gate, min_tickets=161) is None  # 160 < 161: no training
    assert gate.computed == 0
    with tracing.capture_spans() as spans:
        r = route(store, t, gate, min_tickets=160)
    assert r is not None and r.source == "classifier" and r.tier == 1
    assert r.reason.startswith("classifier: logistic regression over 160 accepted tickets")
    assert "knn wanted tier 2" in r.reason and r.label == "small"
    (span,) = [s for s in spans.get_finished_spans() if s.name == "route"]
    assert span.attributes["route.source"] == "classifier"
    assert span.attributes["route.classifier.held_out"] == 32


def test_reverted_tickets_are_excluded(store):
    rnd = random.Random(3)
    old = [seed(store, 1 if i % 2 else 3, noisy(rnd, 1 if i % 2 else 3)) for i in range(160)]
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO post_merge_signals (ticket_id, kind, at, recorded_at)"
            " VALUES (?, 'reverted', ?, ?)",
            (old[0].id, "2026-11-25T00:00:00+00:00", "2026-11-25T00:00:00+00:00"),
        )
    t = query_ticket(store, noisy(rnd, 1))
    assert route(store, t, clf.Gate(), min_tickets=160) is None  # 159 left
    assert [e.ticket_id for e in clf.load_examples(store, model=EMB.name, dims=8)][0] == old[1].id


def test_a_tie_keeps_knn_and_the_check_is_cached_for_a_day(store, clock, monkeypatch):
    rnd = random.Random(5)
    for i in range(60):
        tier = 1 if i % 2 else 3
        seed(store, tier, clustered(rnd, tier))
    gate = clf.Gate()
    t = query_ticket(store, clustered(rnd, 1))
    assert route(store, t, gate, min_tickets=60) is None
    assert gate.computed == 1 and gate.cached is not None and not gate.cached.use_classifier
    clock.now += timedelta(hours=23)
    assert route(store, t, gate, min_tickets=60) is None
    assert gate.computed == 1  # cached: no second comparison within the day
    clock.now += timedelta(hours=2)
    route(store, t, gate, min_tickets=60)
    assert gate.computed == 2  # a day later: recomputed


def test_no_stored_vector_or_not_a_knn_pick_keeps_knn(store):
    rnd = random.Random(3)
    for i in range(160):
        seed(store, 1 if i % 2 else 3, noisy(rnd, 1 if i % 2 else 3))
    gate = clf.Gate()
    bare = store.create_ticket(
        repo="me/toy", issue_number=next(_issue), title="x", request="y", base_branch="main"
    )
    assert route(store, bare, gate, min_tickets=10) is None
    t = query_ticket(store, noisy(rnd, 1))
    rules = Route(tier=2, reason="rules: ...", source="classifier", label="normal")
    assert route(store, t, gate, min_tickets=10, r=rules) is None
    assert gate.computed == 0


def test_never_raises(store, monkeypatch):
    rnd = random.Random(3)
    for i in range(20):
        seed(store, 1 if i % 2 else 3, noisy(rnd, 1 if i % 2 else 3))
    t = query_ticket(store, noisy(rnd, 1))

    def boom(*a, **k):
        raise RuntimeError("bad")

    monkeypatch.setattr(clf, "compare", boom)
    assert route(store, t, clf.Gate(), min_tickets=10) is None


def test_a_tier_3_pick_stays_split_eligible():
    r = Route(tier=3, reason="classifier: ...", source="classifier", label="large")
    assert decompose.eligible(r)
    assert not decompose.eligible(dataclasses.replace(r, label="normal", tier=2))


# --------------------------------------------------------------------------- the worker


def seed_env(env: Env, n: int) -> None:
    """``n`` accepted 3-dim tickets: tier 1 when dimension 0 is positive, else tier 2.
    The fake embedder's query (0.1, 0.2, 0.3) has a positive dimension 0."""
    rnd = random.Random(11)
    for i in range(n):
        tier = 1 if i % 2 else 2
        v = [rnd.uniform(0.02, 0.15) * (1 if tier == 1 else -1)]
        v += [rnd.choice((-1, 1)) * rnd.uniform(0.5, 2.0) for _ in range(2)]
        model = env.config.models_for_tier(tier)[0].name
        t = env.store.create_ticket(
            repo=REPO, issue_number=900 + i, title=f"Old {i}", request="Old.", base_branch="main"
        )
        a = env.store.add_attempt(t.id, model=model, tier=tier, trigger="first")
        env.store.finish_attempt(a.id, None, tests_passed=True)
        env.store.update_ticket(t.id, routed_tier=tier)
        env.store.set_accepted_attempt(t.id, a.id)
        env.store.set_column(t.id, "done")
        knn.store_vector(
            env.store,
            t.id,
            model=env.config.embedding_model,
            vector=knn.normalise(v),
            digest="0" * 64,
        )


def make_env(tmp_path: Path, monkeypatch, *, enabled: bool = True, **kw) -> Env:
    monkeypatch.setattr(wr, "DEFAULT_CLASSIFIER_MIN_TICKETS", 80)
    monkeypatch.setattr(wr, "DEFAULT_CLASSIFIER_ROUTER", enabled)
    e = Env(tmp_path, knn_threshold=3, knn_k=5, **kw)
    # Once the wiring adds the Config keys, set them the same way.
    fields = {f.name for f in dataclasses.fields(e.config)}
    extra = {
        k: v
        for k, v in {"classifier_min_tickets": 80, "classifier_router": enabled}.items()
        if k in fields
    }
    if extra:
        e.config = e.worker.config = dataclasses.replace(e.config, **extra)
    return e


def test_worker_routes_with_the_classifier(tmp_path: Path, monkeypatch) -> None:
    e = make_env(tmp_path, monkeypatch)
    try:
        seed_env(e, 120)
        n = e.label("Typo", "Fix the typo.")
        e.once(failing())
        t = e.ticket(n)
        assert t.route_reason.startswith("classifier: logistic regression over 120"), t.route_reason
        assert t.routed_tier == 1
        assert e.route_calls == 0
        (a,) = e.attempts(n)
        assert (a.tier, a.trigger) == (1, "first")
    finally:
        e.close()


def test_worker_keeps_knn_when_off_or_below_min(tmp_path: Path, monkeypatch) -> None:
    e = make_env(tmp_path / "off", monkeypatch, enabled=False)
    try:
        seed_env(e, 120)
        n = e.label("Typo", "Fix the typo.")
        e.once(failing())
        assert e.ticket(n).route_reason.startswith("knn:")
    finally:
        e.close()
    f = make_env(tmp_path / "few", monkeypatch)
    try:
        seed_env(f, 60)  # below 80
        n = f.label("Typo", "Fix the typo.")
        f.once(failing())
        assert f.ticket(n).route_reason.startswith("knn:")
        assert f.worker.classifier_gate().computed == 0
    finally:
        f.close()
