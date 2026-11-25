"""Model proxy (#9): passthrough, tracing, recording, usage, pricing, fail-closed."""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path

import httpx
import pytest

from fakes.models import FakeModels
from foreman_inator import config as config_mod
from foreman_inator import tracing
from foreman_inator.config import ModelEndpoint, key_ref
from foreman_inator.db.store import Store
from foreman_inator.proxy import CONSERVATIVE_DEFAULT, ModelProxy, pricing
from foreman_inator.proxy.server import CallContext, CallUsage

pytestmark = pytest.mark.anyio

OLLAMA = "http://127.0.0.1:11434/v1"
MODELS = (
    ModelEndpoint("qwen-small", 1, "local", "qwen3.6:35b-a3b", OLLAMA, "qwen"),
    ModelEndpoint(
        "glm", 3, "cloud", "glm-5.3", "https://api.z.ai/api/coding/paas/v4", "glm", key_ref("glm")
    ),
    ModelEndpoint(
        "claude",
        3,
        "cloud",
        "claude-sonnet-5-5",
        "https://api.anthropic.com/v1",
        "claude",
        key_ref("anthropic"),
    ),
    ModelEndpoint("mystery", 3, "cloud", "mystery-1", "https://llm.example.com/v1", "x", None),
    # Built directly (Config would reject it): a "local" model that is not on this Mac.
    ModelEndpoint("sneaky", 1, "local", "qwen3.6:35b-a3b", "http://10.1.2.3:11434/v1", "qwen"),
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Keys generated at test time; the real Keychain is never touched."""
    store: dict[tuple[str, str], str] = {
        ("foreman-inator", "anthropic"): "sk-fake-" + secrets.token_hex(16),
    }
    monkeypatch.setattr(config_mod.keyring, "get_password", lambda s, n: store.get((s, n)))
    monkeypatch.setattr(
        config_mod.keyring, "set_password", lambda s, n, v: store.__setitem__((s, n), v)
    )
    return store


@pytest.fixture
def spans():
    with tracing.capture_spans() as exporter:
        yield exporter


@pytest.fixture
def fake() -> FakeModels:
    return FakeModels()


def make_proxy(fake: FakeModels, **kw) -> ModelProxy:
    return ModelProxy(MODELS, transport=fake.transport, **kw)


def client_for(proxy: ModelProxy) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url=proxy.url)


def llm_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name == "llm.call"]


def chat(model: str = "qwen-small", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "add a test"}], **extra}


# --------------------------------------------------------------------------- passthrough


async def test_non_streaming_passes_through_byte_for_byte(fake, spans):
    fake.queue_reply("done it", tokens=(120, 30))
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/a/42/v1/chat/completions", json=chat())
    assert r.status_code == 200
    assert r.content == fake.sent[-1]  # byte-for-byte
    up = fake.requests[-1]
    assert up.url == f"{OLLAMA}/chat/completions"
    assert up.json["model"] == "qwen3.6:35b-a3b"  # config name rewritten to the upstream id
    assert "authorization" not in up.headers  # local: no key
    (s,) = llm_spans(spans)
    assert s.attributes["llm.token_count.prompt"] == 120
    assert s.attributes["llm.token_count.completion"] == 30
    assert s.attributes["llm.cost_usd"] == 0.0


async def test_streaming_passes_through_byte_for_byte(fake, spans):
    fake.queue_reply(
        "streamed answer here",
        tool_calls=[{"id": "call_1", "name": "read", "arguments": '{"path": "a.py"}'}],
        tokens=(200, 40),
    )
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/a/42/v1/chat/completions", json=chat(stream=True))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.content == fake.sent[-1]  # byte-for-byte
    assert r.content.endswith(b"data: [DONE]\n\n")
    # usage requested upstream so real counts arrive
    assert fake.requests[-1].json["stream_options"] == {"include_usage": True}
    (s,) = llm_spans(spans)
    assert s.attributes["llm.token_count.prompt"] == 200
    assert s.attributes["llm.token_count.completion"] == 40
    assert s.attributes["output.value"] == "streamed answer here"
    assert s.status.is_ok


async def test_client_stream_options_are_left_alone(fake, spans):
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post(
            "/v1/chat/completions",
            json=chat(stream=True, stream_options={"include_usage": False}),
        )
    assert r.content == fake.sent[-1]
    assert fake.requests[-1].json["stream_options"] == {"include_usage": False}
    (s,) = llm_spans(spans)  # no usage reported: estimated from the text, still a span
    assert s.attributes["llm.token_count.completion"] > 0


async def test_models_endpoint_lists_config_without_upstream(fake):
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.get("/a/1/v1/models")
    assert r.status_code == 200
    assert {m["id"] for m in r.json()["data"]} == {m.name for m in MODELS}
    assert fake.requests == []


async def test_embeddings_pass_through_and_are_traced(fake, spans):
    proxy = make_proxy(fake)
    with proxy.context(label="embed") as ctx:
        async with client_for(proxy) as c:
            r = await c.post(
                ctx.base_url.removeprefix(proxy.url) + "/embeddings",
                json={"model": "qwen-small", "input": ["one", "two"]},
            )
    assert r.content == fake.sent[-1]
    assert fake.requests[-1].url == f"{OLLAMA}/embeddings"
    (s,) = llm_spans(spans)
    assert s.attributes["llm.token_count.prompt"] == 10
    assert s.attributes["llm.token_count.completion"] == 0


# --------------------------------------------------------------------------- attempt context


async def test_call_under_attempt_42_is_traced_recorded_and_counted(fake, spans, tmp_path):
    usage: list[CallUsage] = []
    fake.queue_reply(
        "patched it",
        tool_calls=[{"id": "c1", "name": "edit", "arguments": '{"file": "x.py"}'}],
        tokens=(50, 9),
    )
    proxy = make_proxy(fake, on_usage=usage.append)
    with (
        tracing.ticket_span(7, title="t") as tt,
        tracing.attempt_span(42, variant_id="v", model="qwen-small", tier=1, trigger="first"),
        tracing.Recorder(tmp_path / "rec", 7, 42, trace_id=tt.trace_id) as rec,
    ):
        async with client_for(proxy) as c:
            r = await c.post(
                "/a/42/v1/chat/completions",
                json=chat(stream=True),
                headers={"x-fman-prompt-version": "writer-v1@abcd1234"},
            )
        assert r.status_code == 200
    s = llm_spans(spans)[0]
    attempt = next(x for x in spans.get_finished_spans() if x.name == "attempt")
    assert s.attributes["attempt.id"] == 42
    assert s.attributes["ticket.id"] == 7
    assert s.attributes["llm.prompt_version"] == "writer-v1@abcd1234"
    assert s.parent.span_id == attempt.context.span_id
    tracing.load_recording(rec.path)  # fde-kit can read it back
    raw = [t for t in _raw_rows(rec.path) if t["kind"] == "llm"]
    assert raw[0]["usage"] == {"input_tokens": 50, "output_tokens": 9}
    assert raw[0]["text"] == "patched it"
    assert raw[0]["tool_calls"] == [{"id": "c1", "name": "edit", "input": {"file": "x.py"}}]
    assert raw[0]["stop_reason"] == "tool_use"
    assert raw[0]["model"] == "qwen-small"
    assert usage[0].context.attempt_id == 42 and usage[0].context.bucket == "live"
    assert (usage[0].tokens_in, usage[0].tokens_out) == (50, 9)


def _raw_rows(path: Path) -> list[dict]:
    import gzip

    with gzip.open(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def test_usage_is_added_to_the_attempt_in_the_store(fake, spans, tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    ticket = store.create_ticket(
        repo="o/r", issue_number=1, title="t", request="r", base_branch="main"
    )
    attempt = store.add_attempt(ticket.id, model="claude", tier=3, trigger="first")
    fake.queue_reply("ok", tokens=(1_000_000, 100_000))
    proxy = make_proxy(fake, store=store)
    async with client_for(proxy) as c:
        r = await c.post(f"/a/{attempt.id}/v1/chat/completions", json=chat("claude"))
    assert r.status_code == 200
    got = store.get_attempt(attempt.id)
    assert (got.tokens_in, got.tokens_out) == (1_000_000, 100_000)
    assert got.cost_usd == pytest.approx(2.0 + 1.0)  # Sonnet 5.5: $2 in, $10 out per MTok
    store.close()


# --------------------------------------------------------------------------- route / review


async def test_registered_context_parents_to_the_route_span(fake, spans):
    proxy = make_proxy(fake)
    with (
        tracing.ticket_span(3),
        tracing.route_span(1, "rules"),
        proxy.context(label="route") as ctx,
    ):
        assert ctx.base_url.startswith(f"{proxy.url}/c/")
        async with client_for(proxy) as c:
            r = await c.post(
                ctx.base_url.removeprefix(proxy.url) + "/chat/completions", json=chat()
            )
        assert r.status_code == 200
    route = next(s for s in spans.get_finished_spans() if s.name == "route")
    (s,) = llm_spans(spans)
    assert s.parent.span_id == route.context.span_id
    assert s.attributes["ticket.id"] == 3
    # the token stops working once the block exits
    async with client_for(proxy) as c:
        r = await c.post(ctx.base_url.removeprefix(proxy.url) + "/chat/completions", json=chat())
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_context"


async def test_review_call_attaches_to_review_span_and_review_recorder(fake, spans, tmp_path):
    proxy = make_proxy(fake)
    with (
        tracing.ticket_span(5),
        tracing.review_span(9, attempt_id=4, reviewer_model="claude", variant_id="r"),
        tracing.Recorder(tmp_path / "rec", 5, 4, role="review") as rec,
        proxy.context(review_id=9, attempt_id=4, label="review") as ctx,
    ):
        async with client_for(proxy) as c:
            r = await c.post(
                ctx.base_url.removeprefix(proxy.url) + "/chat/completions", json=chat("claude")
            )
        assert r.status_code == 200
    review = next(s for s in spans.get_finished_spans() if s.name == "review")
    (s,) = llm_spans(spans)
    assert s.parent.span_id == review.context.span_id
    assert s.attributes["review.id"] == 9
    assert [r["kind"] for r in _raw_rows(rec.path)].count("llm") == 1


async def test_experiment_prefix_and_context_use_experiments_bucket(fake):
    seen: list[tuple[str, float, CallContext, str]] = []

    def hook(ep, cost, ctx, *, phase):
        seen.append((ep.name, cost, ctx, phase))
        return True

    proxy = make_proxy(fake, spend_hook=hook)
    assert proxy.base_url(8, bucket="experiments").endswith("/x/8/v1")
    async with client_for(proxy) as c:
        await c.post("/x/8/v1/chat/completions", json=chat())
    assert [(n, p, ctx.bucket, ctx.attempt_id) for n, _, ctx, p in seen] == [
        ("qwen-small", "estimate", "experiments", 8),
        ("qwen-small", "actual", "experiments", 8),
    ]


# --------------------------------------------------------------------------- fail closed


async def test_cloud_model_without_key_fails_closed(fake, spans):
    proxy = make_proxy(fake)  # no "glm" key in the fake Keychain
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat("glm"))
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["code"] == "cloud_key_missing"
    assert "glm" in err["message"]
    assert fake.requests == []  # nothing left the machine
    assert llm_spans(spans) == []


async def test_cloud_model_whose_key_lookup_raises_fails_closed(fake, monkeypatch):
    def boom(service, name):
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(config_mod.keyring, "get_password", boom)
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat("claude"))
    assert r.status_code == 403
    assert fake.requests == []


async def test_unknown_model_is_refused_and_not_forwarded(fake):
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat("gpt-9"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
    assert fake.requests == []


async def test_local_model_off_loopback_is_refused(fake):
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat("sneaky"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "local_endpoint_not_loopback"
    assert fake.requests == []


async def test_spend_hook_can_refuse_before_sending(fake, spans):
    calls = []

    def hook(ep, cost, ctx, *, phase):
        calls.append((phase, cost))
        return False

    proxy = make_proxy(fake, spend_hook=hook)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat("claude", max_tokens=1000))
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "spend_cap_reached"
    assert fake.requests == []
    assert calls[0][0] == "estimate" and calls[0][1] >= 1000 * 10 / 1_000_000
    assert len(calls) == 1  # nothing sent, so no "actual" call


async def test_spend_hook_that_raises_refuses(fake):
    def hook(ep, cost, ctx, *, phase):
        raise RuntimeError("db down")

    proxy = make_proxy(fake, spend_hook=hook)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat())
    assert r.status_code == 429
    assert fake.requests == []


async def test_key_never_leaks(fake, spans, tmp_path, caplog, fake_keychain):
    caplog.set_level(logging.DEBUG)
    key = fake_keychain[("foreman-inator", "anthropic")]
    fake.queue_reply("cloud answer", tokens=(10, 5))
    fake.fail_next(401, {"error": {"message": "bad key"}})
    proxy = make_proxy(fake)
    bodies = []
    with (
        tracing.ticket_span(1),
        tracing.attempt_span(2, variant_id="v", model="claude", tier=3, trigger="first"),
        tracing.Recorder(tmp_path / "rec", 1, 2) as rec,
    ):
        async with client_for(proxy) as c:
            for _ in range(2):
                r = await c.post(
                    "/a/2/v1/chat/completions",
                    json=chat("claude", stream=True),
                    headers={"authorization": "Bearer client-supplied"},
                )
                bodies.append(r.content)
            # refused calls too
            bodies.append((await c.post("/a/2/v1/chat/completions", json=chat("glm"))).content)
    # the key goes upstream as a bearer token, and only there
    assert fake.requests[0].headers["authorization"] == f"Bearer {key}"
    assert fake.requests[0].url == "https://api.anthropic.com/v1/chat/completions"
    everything = [json.dumps(dict(s.attributes), default=str) for s in spans.get_finished_spans()]
    everything += [s.status.description or "" for s in spans.get_finished_spans()]
    everything += [b.decode() for b in bodies]
    everything += [rec.path.read_bytes().hex(), json.dumps(_raw_rows(rec.path))]
    everything += [r.getMessage() for r in caplog.records]
    assert not any(key in text for text in everything)
    assert not any(key.encode().hex() in text for text in everything)


async def test_upstream_unreachable_gives_error_and_span(fake, spans):
    fake.raise_next(httpx.ConnectError("connection refused"))
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unreachable"
    (s,) = llm_spans(spans)
    assert not s.status.is_ok


async def test_upstream_error_status_passes_through(fake, spans):
    fake.fail_next(500, {"error": {"message": "model crashed"}})
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=chat(stream=True))
    assert r.status_code == 500
    assert r.content == fake.sent[-1]
    (s,) = llm_spans(spans)
    assert not s.status.is_ok


# --------------------------------------------------------------------------- real socket


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_start_stop_and_disconnect_mid_stream(fake, spans, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    fake.hold_stream = threading.Event()
    fake.queue_reply("a long streamed answer that never finishes", tokens=(30, 8))
    proxy = ModelProxy(MODELS, transport=fake.transport, port=0)
    with (
        tracing.ticket_span(11),
        tracing.attempt_span(12, variant_id="v", model="qwen-small", tier=1, trigger="first"),
        tracing.Recorder(tmp_path / "rec", 11, 12) as rec,
        proxy,
    ):
        assert proxy.port != 0
        base = proxy.base_url(12)
        assert base == f"http://127.0.0.1:{proxy.port}/a/12/v1"
        # a full call over the socket first
        fake.queue_reply("full", tokens=(1, 1))
        with httpx.Client() as c:
            assert c.get(f"{base}/models").status_code == 200
        # then a stream the client abandons after the first chunk
        with (
            httpx.Client(timeout=5) as c,
            c.stream("POST", f"{base}/chat/completions", json=chat(stream=True)) as r,
        ):
            first = next(r.iter_raw())
            assert first.startswith(b"data: ")
        assert _wait_for(lambda: len(llm_spans(spans)) == 1), "no span after disconnect"
        fake.hold_stream.set()
        rows = [x for x in _partial_rows(rec) if x["kind"] == "llm"]
        assert rows and rows[0]["stop_reason"] == "incomplete"
    (s,) = llm_spans(spans)
    assert s.attributes["attempt.id"] == 12
    assert "incomplete" in (s.status.description or "")
    assert proxy._thread is None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], [r.getMessage() for r in errors]


def _partial_rows(rec) -> list[dict]:
    with rec.partial_path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- pricing


def test_pricing_local_free_known_cloud_and_conservative_default():
    local, glm, claude, mystery, _ = MODELS
    assert pricing.cost_usd(local, 10**6, 10**6) == 0.0
    assert pricing.price_for(glm).input_per_mtok == 1.4
    assert pricing.cost_usd(claude, 10**6, 10**6) == pytest.approx(12.0)
    dated = ModelEndpoint("d", 3, "cloud", "claude-sonnet-4-5-20250929", "https://x.io", "c")
    assert pricing.price_for(dated).input_per_mtok == 3
    unknown = pricing.price_for(mystery)
    assert unknown is CONSERVATIVE_DEFAULT and unknown.conservative
    assert unknown.input_per_mtok >= max(p.input_per_mtok for p in pricing.PRICES.values())
    for p in pricing.PRICES.values():
        assert p.source.startswith("https://") and p.as_of == "2026-11-25"
    haiku = pricing.PRICES["claude-haiku-5-5"]
    assert haiku.cost(200_000, 0) == pytest.approx(0.1)  # long-prompt rate


# --------------------------------------------------------------------------- refused parameters

DEPRECATED = {
    "error": {
        "code": "invalid_request_error",
        "message": "`temperature` is deprecated for this model.",
        "type": "invalid_request_error",
        "param": None,
    }
}


def _prefix(proxy: ModelProxy, kind: str, stack) -> str:
    if kind == "c":
        return stack.enter_context(proxy.context(label="review")).base_url.removeprefix(proxy.url)
    return {"a": "/a/5/v1", "x": "/x/5/v1", "none": "/v1"}[kind]


@pytest.mark.parametrize("kind", ["a", "x", "c", "none"])
async def test_refused_temperature_is_dropped_resent_once_and_learned(fake, spans, caplog, kind):
    import contextlib

    caplog.set_level(logging.INFO)
    fake.refuse_param("temperature", times=None, body=DEPRECATED)
    proxy = make_proxy(fake)
    body = chat("claude", temperature=0, max_tokens=1500, stream=False)
    with contextlib.ExitStack() as stack:
        base = _prefix(proxy, kind, stack)
        async with client_for(proxy) as c:
            r = await c.post(f"{base}/chat/completions", json=body)
            assert r.status_code == 200
            assert r.content == fake.sent[-1]  # the successful body, byte for byte
            assert r.headers["x-fman-dropped-params"] == "temperature"
            first, second = fake.requests
            assert first.json["temperature"] == 0 and "temperature" not in second.json
            assert {k: v for k, v in first.json.items() if k != "temperature"} == second.json
            (s,) = llm_spans(spans)
            assert s.attributes["llm.dropped_params"] == "temperature"
            assert s.attributes["llm.resends"] == 1
            assert s.status.is_ok and "llm.error.message" not in s.attributes
            # the second call drops it up front: one upstream request, no temperature
            r2 = await c.post(f"{base}/chat/completions", json=body)
    assert r2.status_code == 200 and r2.headers["x-fman-dropped-params"] == "temperature"
    assert len(fake.requests) == 3 and "temperature" not in fake.requests[2].json
    s2 = llm_spans(spans)[-1]
    assert s2.attributes["llm.dropped_params"] == "temperature"
    assert "llm.resends" not in s2.attributes
    assert proxy.refused_params(MODELS[2]) == {"temperature"}
    learned = [x for x in caplog.records if "refuses 'temperature'" in x.getMessage()]
    assert len(learned) == 1  # one log line, the first time the pair is learned


async def test_learned_drop_is_per_endpoint(fake, spans):
    fake.refuse_param("temperature", times=1, body=DEPRECATED)
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        await c.post("/v1/chat/completions", json=chat("claude", temperature=0))
        r = await c.post("/v1/chat/completions", json=chat("qwen-small", temperature=0))
    assert fake.requests[-1].json["temperature"] == 0  # the local model was never refused
    assert "x-fman-dropped-params" not in r.headers


async def test_resend_reserves_and_settles_spend_once_and_records_one_row(fake, spans, tmp_path):
    from foreman_inator.config import Config, SpendCaps
    from foreman_inator.spend import Spend

    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    ticket = store.create_ticket(
        repo="o/r", issue_number=1, title="t", request="r", base_branch="main"
    )
    attempt = store.add_attempt(ticket.id, model="claude", tier=3, trigger="first")
    spend = Spend(store, Config(spend_caps=SpendCaps(daily_usd=5.0, monthly_usd=50.0)))
    phases: list[tuple[str, float]] = []
    usage: list[CallUsage] = []

    def hook(ep, cost, ctx, *, phase):
        phases.append((phase, cost))
        return spend.hook(ep, cost, ctx, phase=phase)

    fake.refuse_param("temperature", times=None, body=DEPRECATED)
    fake.queue_reply("cloud answer", tokens=(1000, 100))
    proxy = make_proxy(fake, spend_hook=hook, store=store, on_usage=usage.append)
    with (
        tracing.ticket_span(ticket.id),
        tracing.attempt_span(attempt.id, variant_id="v", model="claude", tier=3, trigger="first"),
        tracing.Recorder(tmp_path / "rec", ticket.id, attempt.id) as rec,
    ):
        async with client_for(proxy) as c:
            r = await c.post(
                f"/a/{attempt.id}/v1/chat/completions",
                json=chat("claude", temperature=0, max_tokens=1500),
            )
    assert r.status_code == 200 and len(fake.requests) == 2
    assert [p for p, _ in phases] == ["estimate", "actual"]  # no second estimate
    assert spend._state.reservations == {}  # nothing left reserved
    assert store.query("SELECT COUNT(*) AS n FROM spend_ledger")[0]["n"] == 1
    expected = 1000 * 2 / 1e6 + 100 * 10 / 1e6  # only the answered try is billed
    assert phases[1][1] == pytest.approx(expected)
    assert len(usage) == 1 and usage[0].status_code == 200
    assert store.get_attempt(attempt.id).tokens_in == 1000
    assert len(llm_spans(spans)) == 1
    rows = [x for x in _raw_rows(rec.path) if x["kind"] == "llm"]
    assert len(rows) == 1 and rows[0]["text"] == "cloud answer"
    assert rows[0]["request"]["temperature"] == 0  # the call as the client made it
    store.close()


async def test_streaming_refusal_is_resent_and_streamed_unchanged(fake, spans):
    fake.refuse_param("temperature", times=1, body=DEPRECATED)
    fake.queue_reply("streamed after the resend", tokens=(20, 4))
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post(
            "/a/3/v1/chat/completions", json=chat("claude", temperature=0, stream=True)
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-fman-dropped-params"] == "temperature"
    # the refusal arrived as a plain JSON 400 before any stream bytes
    assert json.loads(fake.sent[0]) == DEPRECATED
    assert r.content == fake.sent[-1] and r.content.endswith(b"data: [DONE]\n\n")
    assert len(fake.requests) == 2 and "temperature" not in fake.requests[1].json
    assert fake.requests[1].json["stream_options"] == {"include_usage": True}
    (s,) = llm_spans(spans)
    assert s.attributes["output.value"] == "streamed after the resend"
    assert s.attributes["llm.resends"] == 1 and s.status.is_ok


@pytest.mark.parametrize(
    "error",
    [
        # vague
        {"message": "Invalid request.", "type": "invalid_request_error"},
        # a parameter that changes what is asked
        {"message": "`max_tokens` is deprecated for this model.", "code": "invalid_request_error"},
        {"message": "'stop' is not supported with this model.", "code": "unsupported_parameter"},
        # structured param not on the allow-list wins over a message naming temperature
        {
            "message": "`temperature` is not supported here.",
            "param": "stop",
            "code": "unsupported_parameter",
        },
        # a bad value, not a refused parameter
        {"message": "`temperature` must be between 0 and 1.", "code": "invalid_request_error"},
        # several sent parameters named: ambiguous
        {"message": "`temperature` and `top_p` are not supported together.", "param": None},
        # a parameter the client did not send
        {"message": "`seed` is not supported for this model.", "param": None},
    ],
)
async def test_other_400s_pass_through_unchanged_without_resend(fake, spans, error):
    fake.fail_next(400, {"error": error})
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post(
            "/a/1/v1/chat/completions",
            json=chat("claude", temperature=0, top_p=1, stop=["x"], max_tokens=10),
        )
    assert r.status_code == 400 and r.content == fake.sent[-1]
    assert len(fake.requests) == 1
    assert "x-fman-dropped-params" not in r.headers
    assert proxy.refused_params(MODELS[2]) == frozenset()


async def test_a_refusal_that_is_not_a_400_is_not_resent(fake, spans):
    fake.refuse_param("temperature", status=500, body=DEPRECATED)
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/v1/chat/completions", json=chat("claude", temperature=0))
    assert r.status_code == 500 and len(fake.requests) == 1


@pytest.mark.parametrize("code", ["unsupported_parameter", "unsupported_value"])
async def test_structured_refusal_is_matched_on_param_and_code(fake, spans, code):
    body = {
        "error": {
            "message": "Unsupported value: 'top_p' does not support 0.5 with this model.",
            "type": "invalid_request_error",
            "param": "top_p",
            "code": code,
        }
    }
    fake.refuse_param("top_p", body=body)
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post("/v1/chat/completions", json=chat("claude", top_p=0.5))
    assert r.status_code == 200 and r.headers["x-fman-dropped-params"] == "top_p"
    assert "top_p" not in fake.requests[1].json


async def test_resends_are_capped_per_call(fake, spans):
    for p in ("temperature", "top_p", "seed"):
        fake.refuse_param(
            p,
            times=None,
            body={"error": {"message": f"`{p}` is not supported for this model."}},
        )
    proxy = make_proxy(fake)
    async with client_for(proxy) as c:
        r = await c.post(
            "/v1/chat/completions", json=chat("claude", temperature=0, top_p=1, seed=7)
        )
    assert len(fake.requests) == 3  # the first try and two resends, then pass through
    assert r.status_code == 400 and r.content == fake.sent[-1]
    assert r.headers["x-fman-dropped-params"] == "temperature,top_p"
    (s,) = llm_spans(spans)
    assert s.attributes["llm.resends"] == 2 and not s.status.is_ok


# --------------------------------------------------------------------------- provider error text


async def test_provider_error_text_is_on_the_span_and_recording_and_bounded(fake, spans, tmp_path):
    long = "This model is overloaded right now. " + "x" * 3000
    fake.fail_next(529, {"type": "error", "error": {"type": "overloaded_error", "message": long}})
    proxy = make_proxy(fake)
    with (
        tracing.ticket_span(1),
        tracing.attempt_span(2, variant_id="v", model="claude", tier=3, trigger="first"),
        tracing.Recorder(tmp_path / "rec", 1, 2) as rec,
    ):
        async with client_for(proxy) as c:
            r = await c.post("/a/2/v1/chat/completions", json=chat("claude"))
    assert r.status_code == 529 and r.content == fake.sent[-1]  # passed through unchanged
    (s,) = llm_spans(spans)
    msg = s.attributes["llm.error.message"]
    assert msg.startswith("This model is overloaded right now.") and len(msg) <= 500
    assert s.status.description == f"upstream HTTP 529: {msg}"
    assert "output.value" not in s.attributes or s.attributes["output.value"] == ""
    (row,) = [x for x in _raw_rows(rec.path) if x["kind"] == "llm"]
    assert row["stop_reason"] == "error" and row["text"] == ""
    assert row["raw_content"] == [{"type": "error", "text": msg}]


async def test_non_json_error_body_is_kept_as_text(fake, spans):
    async def html(request):
        return httpx.Response(502, content=b"<html>Bad gateway\n</html>")

    proxy = ModelProxy(MODELS, transport=httpx.MockTransport(html))
    async with client_for(proxy) as c:
        r = await c.post("/v1/chat/completions", json=chat())
    assert r.status_code == 502 and r.content == b"<html>Bad gateway\n</html>"
    (s,) = llm_spans(spans)
    assert s.attributes["llm.error.message"] == "<html>Bad gateway </html>"


async def test_upstream_echoing_the_key_leaks_it_nowhere(
    fake, spans, tmp_path, caplog, fake_keychain
):
    caplog.set_level(logging.DEBUG)
    key = fake_keychain[("foreman-inator", "anthropic")]
    other = "sk-ant-other-" + secrets.token_hex(12)
    echo = (
        f"invalid x-api-key: {key}. You sent Authorization: Bearer {key} "
        f'(headers {{"authorization": "Bearer {other}", "x-api-key": "{other}"}})'
    )
    fake.fail_next(401, {"error": {"type": "authentication_error", "message": echo}})
    fake.fail_next(400, {"error": {"message": f"`temperature` bad for key {key}"}})
    proxy = make_proxy(fake)
    bodies = []
    with (
        tracing.ticket_span(1),
        tracing.attempt_span(2, variant_id="v", model="claude", tier=3, trigger="first"),
        tracing.Recorder(tmp_path / "rec", 1, 2) as rec,
    ):
        async with client_for(proxy) as c:
            for stream in (False, True):
                r = await c.post("/a/2/v1/chat/completions", json=chat("claude", stream=stream))
                bodies.append(r.content)
    assert fake.requests[0].headers["authorization"] == f"Bearer {key}"
    assert b"[redacted]" in bodies[0]  # the rest of the error still reaches the client
    everything = [json.dumps(dict(s.attributes), default=str) for s in spans.get_finished_spans()]
    everything += [s.status.description or "" for s in spans.get_finished_spans()]
    everything += [b.decode() for b in bodies]
    everything += [rec.path.read_bytes().hex(), json.dumps(_raw_rows(rec.path))]
    everything += [r.getMessage() for r in caplog.records]
    assert not any(key in text for text in everything)
    # anything else that looks like a credential is scrubbed from what the proxy stores
    # (the client's own copy of the error body is the provider's, minus our key)
    stored = [t for t in everything if t not in {b.decode() for b in bodies}]
    assert not any(other in text for text in stored)
    msgs = [s.attributes["llm.error.message"] for s in llm_spans(spans)]
    assert "invalid x-api-key: [redacted]" in msgs[0]


def test_refused_param_matcher_rules():
    from foreman_inator.proxy.server import DROPPABLE_PARAMS, refused_param

    sent = {"model": "m", "messages": [], "temperature": 0, "max_tokens": 5}
    assert refused_param(400, DEPRECATED, sent) == "temperature"
    assert refused_param(400, DEPRECATED, {"model": "m"}) is None  # not sent
    assert refused_param(422, DEPRECATED, sent) is None
    assert refused_param(400, {"error": "temperature unsupported"}, sent) is None
    assert DROPPABLE_PARAMS.isdisjoint(
        {"model", "messages", "tools", "max_tokens", "max_completion_tokens", "stop"}
    )
