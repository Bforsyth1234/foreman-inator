"""Nearest-neighbour start rule and exploration (#23).

A real ``Store`` under ``tmp_path``, a real ``ModelProxy`` on a loopback socket, and
``FakeModels`` behind it. The fake's embeddings are replaced (subclass below) with a
deterministic bag-of-words vector, so similar issue text gives similar vectors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import time

import httpx
import pytest

from fakes.models import FakeModels, FakeRequest
from foreman_inator import tracing
from foreman_inator.config import Config, ModelEndpoint
from foreman_inator.db.store import Store
from foreman_inator.proxy.server import ModelProxy
from foreman_inator.routing import escalation, explore, knn
from foreman_inator.routing.knn import Neighbour, pick_tier, route_knn
from foreman_inator.routing.rules import Override

CONFIG = Config()
EMB = knn.embedding_endpoint()  # embeddinggemma on local Ollama
EMB_B = knn.embedding_endpoint("qwen3-embedding")
CLOUD_EMB = ModelEndpoint(
    "cloud-embed", 1, "cloud", "text-embedding-3-small", "https://api.example.com/v1", "x"
)
DIMS = 64


def bow(text: str, dims: int = DIMS) -> list[float]:
    vec = [0.0] * dims
    for word in re.findall(r"[a-z]+", text.lower()):
        h = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
        vec[h % dims] += 1.0
    return vec


class EmbeddingFake(FakeModels):
    """FakeModels whose /embeddings answer is a bag-of-words vector of the input."""

    def __init__(self) -> None:
        super().__init__()
        self.dims = DIMS
        self.delay = 0.0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/embeddings") or self.failures or self.raises:
            return await super().handle(request)
        body = json.loads(request.content)
        self.requests.append(FakeRequest(request.method, str(request.url), {}, body))
        if self.delay:
            await asyncio.sleep(self.delay)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [
            {"object": "embedding", "index": i, "embedding": bow(t, self.dims)}
            for i, t in enumerate(inputs)
        ]
        return self._json({"object": "list", "model": body["model"], "data": data})


def embed_requests(fake: FakeModels) -> list[FakeRequest]:
    return [r for r in fake.requests if r.url.endswith("/embeddings")]


@pytest.fixture
def fake() -> EmbeddingFake:
    return EmbeddingFake()


@pytest.fixture
def proxy(fake):
    key = "fake-" + secrets.token_hex(8)
    p = ModelProxy(
        [*CONFIG.models, EMB, EMB_B, CLOUD_EMB],
        transport=fake.transport,
        key_resolver=lambda e: key,
    ).start()
    try:
        yield p
    finally:
        p.stop()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "fman.db")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def spans():
    with tracing.capture_spans() as exporter:
        yield exporter


_issue = iter(range(1, 1_000_000))


def accepted(store, title, request, tiers, *, routed=None, triggers=None, kickback_first=False):
    """A ticket whose attempts ran on ``tiers`` (in order) and whose last was accepted."""
    t = store.create_ticket(
        repo="me/toy",
        issue_number=next(_issue),
        title=title,
        request=request,
        base_branch="main",
        routed_tier=routed if routed is not None else tiers[0],
    )
    triggers = triggers or ["first"] + ["escalation"] * (len(tiers) - 1)
    last = None
    for i, (tier, trig) in enumerate(zip(tiers, triggers, strict=True)):
        a = store.add_attempt(t.id, model=f"m{tier}", tier=tier, trigger=trig)
        final = i == len(tiers) - 1
        store.finish_attempt(a.id, None if final else "failed_checks")
        last = a
    if kickback_first:  # kicked back on the same tier, then accepted there
        store.set_attempt_outcome(last.id, "kicked_back")
        store.record_kickback(last.id, by="user", tag="incomplete", note="more")
        last = store.add_attempt(t.id, model=f"m{last.tier}", tier=last.tier, trigger="kickback")
    store.set_accepted_attempt(t.id, last.id)
    store.set_column(t.id, "done")
    return store.get_ticket(t.id)


SMALL = [
    ("Fix typo in README", "The README says teh instead of the."),
    ("Fix typo in docs", "A typo in the docs page: recieve."),
    ("Fix spelling in CLI help", "The help text has a spelling typo."),
]
LARGE = [
    ("Refactor database layer", "Refactor the storage layer across modules into a repository."),
    ("Rewrite scheduler architecture", "Redesign the scheduler architecture across services."),
    ("Migrate auth to new framework", "Migrate the whole authentication framework, many modules."),
]


def history(store, n_small=30, n_large=30):
    for i in range(n_small):
        title, body = SMALL[i % len(SMALL)]
        accepted(store, title, body, [1])
    for i in range(n_large):
        title, body = LARGE[i % len(LARGE)]
        accepted(store, title, body, [3])


def fill_vectors(store, proxy, embedder=EMB):
    while True:
        r = knn.backfill(store, proxy=proxy, embedder=embedder, batch_size=16)
        assert r.error is None
        if not r.remaining:
            return


def new_ticket(store, title, request):
    return store.create_ticket(
        repo="me/toy", issue_number=next(_issue), title=title, request=request, base_branch="main"
    )


# --------------------------------------------------------------------------- Verify bullet 1


def test_small_history_on_tier_1_routes_similar_ticket_to_tier_1(store, proxy, fake):
    history(store)
    fill_vectors(store, proxy)
    t = new_ticket(store, "Fix typo in README intro", "The README intro says teh.")
    r = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB)
    assert r is not None
    assert (r.tier, r.source, r.requested_tier, r.has_model) == (1, "knn", 1, True)
    ids = knn.matched_tickets(r)
    assert len(ids) == 10
    assert r.reason.startswith("knn: 10 nearest of 60 accepted tickets (embeddinggemma): ")
    for tid in ids:
        assert f"#{tid} t1" in r.reason
        assert store.get_ticket(tid).title in {s[0] for s in SMALL}
    assert "tier 1 accepted for 10 of 10 -> tier 1" in r.reason


def test_large_history_on_tier_3_routes_similar_ticket_to_tier_3(store, proxy):
    history(store)
    fill_vectors(store, proxy)
    t = new_ticket(store, "Refactor the storage layer", "Refactor storage across modules.")
    r = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB)
    assert r is not None and (r.tier, r.source) == (3, "knn")


def test_route_reason_and_span_list_matched_tickets(store, proxy, spans):
    history(store, 30, 25)
    fill_vectors(store, proxy)
    spans.clear()
    t = new_ticket(store, "Fix typo in docs", "recieve is a typo")
    r = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB)
    done = spans.get_finished_spans()
    (route_s,) = [s for s in done if s.name == "route"]
    (call,) = [s for s in done if s.name == "llm.call"]
    assert call.parent.span_id == route_s.context.span_id  # embedding inside `route`
    assert route_s.attributes["route.source"] == "knn"
    assert list(route_s.attributes["route.matched_tickets"]) == knn.matched_tickets(r)


# --------------------------------------------------------------------------- threshold


def test_below_threshold_falls_back_with_no_model_call(store, proxy, fake):
    history(store, 25, 24)  # 49 accepted
    fill_vectors(store, proxy)
    before = len(embed_requests(fake))
    t = new_ticket(store, "Fix typo", "teh")
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB) is None
    assert len(embed_requests(fake)) == before
    accepted(store, "Fix typo in README", "teh", [1])
    fill_vectors(store, proxy)
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB) is not None


def test_threshold_and_k_are_configurable(store, proxy):
    history(store, 4, 2)
    fill_vectors(store, proxy)
    t = new_ticket(store, "Fix typo in docs", "typo")
    r = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB, threshold=6, k=3)
    assert r is not None and len(knn.matched_tickets(r)) == 3 and r.tier == 1
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB, threshold=7) is None


def test_tickets_without_vectors_do_not_count(store, proxy):
    history(store)  # 60 accepted, none embedded yet
    t = new_ticket(store, "Fix typo", "teh")
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB) is None


# --------------------------------------------------------------------------- override, failure


def test_tier_override_wins_with_no_embedding_call(store, proxy, fake):
    history(store)
    fill_vectors(store, proxy)
    before = len(embed_requests(fake))
    t = new_ticket(store, "Fix typo in README", "teh")
    r = route_knn(
        t, store=store, config=CONFIG, proxy=proxy, embedder=EMB, override=Override(3, "me")
    )
    assert (r.tier, r.source) == (3, "override")
    assert "tier:3 label by me" in r.reason
    assert len(embed_requests(fake)) == before


@pytest.mark.parametrize("failure", ["http_500", "connect_error", "bad_json", "zero_vector"])
def test_embedding_failure_falls_back(store, proxy, fake, failure):
    history(store)
    fill_vectors(store, proxy)
    if failure == "http_500":
        fake.fail_next(500)
    elif failure == "connect_error":
        fake.raise_next(httpx.ConnectError("down"))
    elif failure == "bad_json":
        fake.fail_next(200, {"data": [{"index": 0, "embedding": "nope"}]})
    else:
        fake.dims = 0  # bow() of 0 dims → empty list: not a vector
    t = new_ticket(store, "Fix typo", "teh")
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB) is None


def test_embedding_timeout_falls_back_within_the_deadline(store, proxy, fake):
    history(store)
    fill_vectors(store, proxy)
    fake.delay = 5.0
    t = new_ticket(store, "Fix typo", "teh")
    t0 = time.monotonic()
    r = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB, timeout=0.3)
    assert r is None
    assert time.monotonic() - t0 < 2.0


def test_no_proxy_or_broken_store_never_raises(store, proxy, tmp_path):
    history(store)
    fill_vectors(store, proxy)
    t = new_ticket(store, "Fix typo", "teh")
    assert route_knn(t, store=store, config=CONFIG, proxy=None, embedder=EMB) is None
    store.close()
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB) is None


def test_cloud_embedding_model_is_refused_unless_allowed(store, proxy, fake):
    history(store)
    for tid in range(1, 61):  # vectors stored under the cloud model's name, written by hand
        knn.store_vector(
            store, tid, model=CLOUD_EMB.name, vector=knn.normalise(bow("typo")), digest="0" * 64
        )
    before = len(fake.requests)
    t = new_ticket(store, "Fix typo", "teh")
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=CLOUD_EMB) is None
    r = knn.backfill(store, proxy=proxy, embedder=CLOUD_EMB)
    assert r.embedded == () and "cloud" in r.error
    assert len(fake.requests) == before  # nothing sent anywhere (7c)


# --------------------------------------------------------------------------- vectors


def test_route_stores_the_new_tickets_vector_and_reuses_it(store, proxy, fake):
    history(store)
    fill_vectors(store, proxy)
    t = new_ticket(store, "Fix typo in README", "teh")
    n = len(embed_requests(fake))
    r1 = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB)
    assert len(embed_requests(fake)) == n + 1
    (row,) = store.query("SELECT model, dims FROM ticket_embeddings WHERE ticket_id = ?", (t.id,))
    assert (row["model"], row["dims"]) == ("embeddinggemma", DIMS)
    r2 = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB)
    assert len(embed_requests(fake)) == n + 1  # same text, same model: no second call
    assert r1 == r2


def test_changed_model_never_compares_against_old_vectors(store, proxy, fake):
    history(store)
    fill_vectors(store, proxy, EMB)
    t = new_ticket(store, "Fix typo in README", "teh")
    before = len(embed_requests(fake))
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB_B) is None
    assert len(embed_requests(fake)) == before  # old vectors do not count toward the threshold
    r = knn.backfill(store, proxy=proxy, embedder=EMB_B, batch_size=8)
    assert len(r.embedded) == 8 and r.remaining == 52
    fill_vectors(store, proxy, EMB_B)
    got = route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB_B)
    assert got is not None and "(qwen3-embedding)" in got.reason


def test_vectors_of_another_dimension_are_ignored(store, proxy, fake):
    history(store)
    fill_vectors(store, proxy)
    fake.dims = 32  # same model name now answers with 32-dim vectors
    t = new_ticket(store, "Fix typo", "teh")
    assert route_knn(t, store=store, config=CONFIG, proxy=proxy, embedder=EMB) is None


def test_backfill_batches_restart_and_idempotence(tmp_path, proxy, fake):
    path = tmp_path / "b.db"
    s = Store(path)
    for i in range(20):
        accepted(s, f"Fix typo {i}", "teh", [1])
    new_ticket(s, "open ticket", "not accepted")  # never backfilled
    r = knn.backfill(s, proxy=proxy, embedder=EMB, batch_size=8)
    assert (len(r.embedded), r.remaining, r.error) == (8, 12, None)
    assert len(embed_requests(fake)[-1].json["input"]) == 8
    s.close()
    s = Store(path)  # restart
    fake.fail_next(500)
    r = knn.backfill(s, proxy=proxy, embedder=EMB, batch_size=8)
    assert r.embedded == () and r.remaining == 12 and r.error
    assert s.query("SELECT COUNT(*) AS n FROM ticket_embeddings")[0]["n"] == 8  # nothing half
    for _ in range(3):
        knn.backfill(s, proxy=proxy, embedder=EMB, batch_size=8)
    n = len(embed_requests(fake))
    r = knn.backfill(s, proxy=proxy, embedder=EMB, batch_size=8)
    assert r == knn.BackfillResult((), 0)
    assert len(embed_requests(fake)) == n  # nothing left: no call
    s.close()


def test_backfill_reembeds_a_ticket_whose_text_changed(store, proxy):
    t = accepted(store, "Fix typo", "teh", [1])
    fill_vectors(store, proxy)
    with store.transaction() as conn:
        conn.execute("UPDATE tickets SET request = ? WHERE id = ?", ("edited body", t.id))
    r = knn.backfill(store, proxy=proxy, embedder=EMB)
    assert r.embedded == (t.id,)


# --------------------------------------------------------------------------- the neighbour rule


def nb(*tiers: int) -> list[Neighbour]:
    return [Neighbour(i + 1, 1.0 - i / 100, t) for i, t in enumerate(tiers)]


def test_pick_tier_majority_and_cheapest_on_ties():
    assert pick_tier(nb(1, 1, 1, 2, 2, 3, 3, 3, 3, 3)) == (3, 5)  # most votes wins
    assert pick_tier(nb(2, 2, 2, 1, 1, 1, 3, 3, 3, 3)) == (3, 4)
    assert pick_tier(nb(3, 3, 1, 1, 2, 2)) == (1, 2)  # three-way tie → cheapest
    assert pick_tier(nb(3, 3, 3, 2, 2, 2, 1)) == (2, 3)  # tie between 2 and 3 → 2
    assert pick_tier(nb(2)) == (2, 1)
    with pytest.raises(ValueError):
        pick_tier([])


def test_nearest_orders_by_similarity_then_older_ticket():
    q = [1.0, 0.0]
    pool = [(5, 1, [0.0, 1.0]), (9, 2, [1.0, 0.0]), (3, 3, [1.0, 0.0]), (4, 1, [0.6, 0.8])]
    assert [n.ticket_id for n in knn.nearest(q, pool, k=3)] == [3, 9, 4]


def _pool_tiers(store):
    vec_dims = store.query("SELECT dims FROM ticket_embeddings LIMIT 1")[0]["dims"]
    return {tid: tier for tid, tier, _ in knn.load_pool(store, model=EMB.name, dims=vec_dims)}


def test_escalated_neighbour_counts_at_its_accepted_tier(store, proxy):
    t = accepted(store, "Fix typo", "teh", [1, 1, 2])  # failed twice on 1, accepted on 2
    fill_vectors(store, proxy)
    assert _pool_tiers(store) == {t.id: 2}


def test_kickback_before_acceptance_counts_at_the_accepted_tier(store, proxy):
    t = accepted(store, "Fix typo", "teh", [1], kickback_first=True)
    fill_vectors(store, proxy)
    assert len(store.list_attempts(t.id)) == 2
    assert _pool_tiers(store) == {t.id: 1}


def test_explore_start_counts_where_accepted(store, proxy):
    ok = accepted(store, "Fix typo", "teh", [1], routed=2, triggers=["explore"])
    failed = accepted(
        store, "Fix typo", "teh", [1, 2], routed=2, triggers=["explore", "escalation"]
    )
    fill_vectors(store, proxy)
    assert _pool_tiers(store) == {ok.id: 1, failed.id: 2}


def test_profile_shifted_start_is_not_learned(store, proxy):
    """#51 ``fast``: router picked 1, the attempt started on 2 and was accepted there; that
    says nothing about tier 1, so the ticket is left out of the pool."""
    shifted = accepted(store, "Fix typo", "teh", [2], routed=1)
    routed = accepted(store, "Fix typo", "teh", [2], routed=2)
    legacy = accepted(store, "Fix typo", "teh", [3], routed=None)
    with store.transaction() as conn:
        conn.execute("UPDATE tickets SET routed_tier = NULL WHERE id = ?", (legacy.id,))
    fill_vectors(store, proxy)
    assert _pool_tiers(store) == {routed.id: 2, legacy.id: 3}
    assert knn.pool_size(store, model=EMB.name) == 2
    assert shifted.id not in _pool_tiers(store)


def test_unaccepted_and_dropped_tickets_are_not_neighbours(store, proxy):
    a = accepted(store, "Fix typo", "teh", [1])
    open_t = new_ticket(store, "Fix typo", "teh")
    knn.store_vector(
        store, open_t.id, model=EMB.name, vector=knn.normalise(bow("typo")), digest="0" * 64
    )
    fill_vectors(store, proxy)
    assert _pool_tiers(store) == {a.id: 1}


def test_unconfigured_winning_tier_is_skipped_upward(store, proxy):
    history(store, 30, 0)
    fill_vectors(store, proxy)
    models = tuple(m for m in CONFIG.models if m.tier != 1)
    t = new_ticket(store, "Fix typo in README", "teh")
    r = route_knn(t, store=store, config=models, proxy=proxy, embedder=EMB, threshold=30)
    assert (r.tier, r.requested_tier) == (2, 1)
    assert "tier 1 has no model configured, using tier 2" in r.reason


def test_migration_adds_the_embeddings_table(store):
    cols = [r["name"] for r in store.query("PRAGMA table_info(ticket_embeddings)")]
    assert cols == ["ticket_id", "model", "dims", "vector", "text_sha256", "created_at"]
    assert store.query("SELECT MAX(version) AS v FROM schema_migrations")[0]["v"] >= 4


def test_vector_roundtrip_is_unit_float32():
    v = knn.normalise([3.0, 4.0])
    assert v == [0.6, 0.8]
    blob = knn.pack(v)
    assert len(blob) == 8
    assert [round(x, 6) for x in knn.unpack(blob)] == [0.6, 0.8]
    with pytest.raises(knn.EmbedError):
        knn.normalise([0.0, 0.0])
    with pytest.raises(knn.EmbedError):
        knn.normalise([math.nan])


# --------------------------------------------------------------------------- exploration (Verify 2)

TIERS = (1, 2, 3)


def test_exploration_rate_is_about_10_percent_over_1000_draws():
    hits = sum(explore.should_explore(i, routed_tier=2, tiers=TIERS) for i in range(1, 1001))
    assert 70 <= hits <= 130, hits
    hits25 = sum(
        explore.should_explore(i, 0.25, routed_tier=3, tiers=TIERS) for i in range(1, 1001)
    )
    assert 210 <= hits25 <= 290, hits25


def test_exploration_never_goes_below_tier_1_and_never_from_the_lowest_tier():
    ladder = escalation.build_ladder(CONFIG.models)
    lowest = min(r.tier for r in ladder)
    for i in range(1, 1001):
        routed = (i % 3) + 1
        flag = explore.should_explore(i, 0.5, routed_tier=routed, tiers=CONFIG)
        if routed == lowest:
            assert flag is False
        step = escalation.next_step(
            escalation.History((), routed_tier=routed, explore=flag), ladder
        )
        assert step.tier >= 1
        assert (step.trigger == "explore") == flag
        if flag:
            assert step.tier == explore.explore_tier(routed, CONFIG) == routed - 1
    # a ladder whose lowest tier is 2: routed 2 never explores
    no_t1 = tuple(m for m in CONFIG.models if m.tier != 1)
    assert not any(explore.should_explore(i, 1.0, routed_tier=2, tiers=no_t1) for i in range(500))
    assert explore.explore_tier(3, no_t1) == 2
    assert explore.explore_tier(1, TIERS) is None
    assert explore.explore_tier(None, TIERS) is None


def test_exploration_is_deterministic_per_ticket_and_salt():
    a = [explore.should_explore(i, routed_tier=3, tiers=TIERS) for i in range(1, 300)]
    b = [explore.should_explore(i, routed_tier=3, tiers=TIERS) for i in range(1, 300)]
    c = [explore.should_explore(i, routed_tier=3, tiers=TIERS, salt="other") for i in range(1, 300)]
    assert a == b and a != c
    assert explore.explore_draw(42) == explore.explore_draw(42)
    assert 0.0 <= explore.explore_draw(42) < 1.0


def test_override_and_zero_rate_never_explore():
    assert not any(
        explore.should_explore(i, 1.0, routed_tier=3, tiers=TIERS, override=True)
        for i in range(500)
    )
    assert not any(explore.should_explore(i, 0.0, routed_tier=3, tiers=TIERS) for i in range(500))
    assert all(explore.should_explore(i, 1.0, routed_tier=3, tiers=TIERS) for i in range(500))


@pytest.mark.parametrize("bad", [-0.1, 1.1, math.nan, "0.1", True])
def test_bad_rate_is_rejected(bad):
    with pytest.raises(ValueError):
        explore.should_explore(1, bad, routed_tier=2, tiers=TIERS)


def test_decision_cursor_roundtrip(store):
    key = explore.explore_cursor(7)
    assert key == "explore:7"
    assert explore.decode(store.get_cursor("me/toy", key)) is False  # no row
    store.set_cursor("me/toy", explore.encode(True), key)
    assert explore.decode(store.get_cursor("me/toy", key)) is True
    store.set_cursor("me/toy", explore.encode(False), key)
    assert explore.decode(store.get_cursor("me/toy", key)) is False
