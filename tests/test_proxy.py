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
        assert p.source.startswith("https://") and p.as_of == "2026-11-23"
    haiku = pricing.PRICES["claude-haiku-5-5"]
    assert haiku.cost(200_000, 0) == pytest.approx(0.1)  # long-prompt rate
