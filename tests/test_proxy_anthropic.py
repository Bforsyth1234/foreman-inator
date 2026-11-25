"""#52: the proxy's Anthropic Messages passthrough route and cached-token accounting.

Docs relied on (read 2026-11-25):
* https://platform.claude.com/docs/en/build-with-claude/prompt-caching: usage fields
  ``input_tokens`` / ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` /
  ``cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens``; total input is their sum.
* https://platform.claude.com/docs/en/build-with-claude/streaming: event flow, the
  ``message_delta`` usage is cumulative.
* https://platform.claude.com/docs/en/about-claude/pricing: cache hits Sonnet 5.5 $0.10,
  Opus 5.5 $0.20, Fable 5.2 $0.20 per MTok; writes 1.25x (5m) and 2x (1h) input.
* https://docs.z.ai/guides/overview/pricing: GLM-5.3 cached input $0.26 per MTok.
"""

from __future__ import annotations

import gzip
import json
import logging
import secrets
from pathlib import Path

import httpx
import pytest

from fakes.anthropic import FakeAnthropic
from foreman_inator import config as config_mod
from foreman_inator import tracing
from foreman_inator.config import ModelEndpoint, key_ref
from foreman_inator.db.store import Store
from foreman_inator.proxy import ModelProxy, pricing
from foreman_inator.proxy import server as server_mod
from foreman_inator.proxy.server import CallUsage, LocalSlots

pytestmark = pytest.mark.anyio

ANTHROPIC = "https://api.anthropic.com/v1"
OLLAMA = "http://127.0.0.1:11434/v1"


def claude(name: str, model: str) -> ModelEndpoint:
    return ModelEndpoint(
        name, 3, "cloud", model, ANTHROPIC, "claude", key_ref("anthropic"), wire="anthropic"
    )


MODELS = (
    claude("sonnet", "claude-sonnet-5-5"),
    claude("opus", "claude-opus-5-5"),
    claude("fable", "claude-fable-5-2"),
    ModelEndpoint(
        "keyless",
        3,
        "cloud",
        "claude-haiku-5-5",
        ANTHROPIC,
        "claude",
        "keychain:foreman-inator/missing",
        wire="anthropic",
    ),
    ModelEndpoint("local", 1, "local", "qwen3.8:27b", OLLAMA, "qwen"),
    ModelEndpoint(
        "glm", 3, "cloud", "glm-5.3", "https://api.z.ai/api/coding/paas/v4", "glm", key_ref("glm")
    ),
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Keys generated at test time; the real Keychain is never touched."""
    store = {
        ("foreman-inator", "anthropic"): "sk-ant-fake-" + secrets.token_hex(16),
        ("foreman-inator", "glm"): "glm-fake-" + secrets.token_hex(16),
    }
    monkeypatch.setattr(config_mod.keyring, "get_password", lambda s, n: store.get((s, n)))
    return store


@pytest.fixture
def spans():
    with tracing.capture_spans() as exporter:
        yield exporter


@pytest.fixture
def fake() -> FakeAnthropic:
    return FakeAnthropic()


def client_for(proxy: ModelProxy) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url=proxy.url)


def llm_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name == "llm.call"]


def raw_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def messages_body(model: str = "claude-sonnet-5-5", **extra) -> bytes:
    """What @ai-sdk/anthropic sends: odd spacing and key order kept, to check bytes."""
    body = {
        "model": model,
        "max_tokens": 1024,
        "system": [
            {"type": "text", "text": "You are a writer.", "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "add a test", "cache_control": {"type": "ephemeral"}}
                ],
            }
        ],
        **extra,
    }
    return json.dumps(body, indent=1).encode()


CACHED_USAGE = {
    "input_tokens": 1_000,
    "cache_read_input_tokens": 9_000,
    "cache_creation_input_tokens": 2_000,
    "cache_creation": {"ephemeral_5m_input_tokens": 2_000, "ephemeral_1h_input_tokens": 0},
}


# --------------------------------------------------------------------------- passthrough


async def test_request_and_stream_pass_through_byte_for_byte_with_x_api_key(
    fake, spans, fake_keychain
):
    key = fake_keychain[("foreman-inator", "anthropic")]
    fake.queue_reply("streamed answer", usage={"input_tokens": 20})
    proxy = ModelProxy(MODELS, transport=fake.transport)
    raw = messages_body(stream=True)
    async with client_for(proxy) as c:
        r = await c.post(
            "/a/9/v1/messages",
            content=raw,
            headers={
                "content-type": "application/json",
                "x-api-key": "fman-proxy",
                "authorization": "Bearer client-supplied",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "fine-grained-tool-streaming-2025-05-14",
                "x-custom": "dropped",
            },
        )
    assert r.status_code == 200
    up = fake.requests[-1]
    assert up.url == f"{ANTHROPIC}/messages"
    assert up.content == raw  # the body, byte-for-byte
    assert up.headers["x-api-key"] == key  # the real key, as x-api-key
    assert "authorization" not in up.headers  # never a bearer token on this route
    assert up.headers["anthropic-version"] == "2023-06-01"
    assert up.headers["anthropic-beta"] == "fine-grained-tool-streaming-2025-05-14"
    assert "x-custom" not in up.headers
    assert r.content == fake.sent[-1]  # the stream, byte-for-byte
    assert r.headers["content-type"].startswith("text/event-stream")
    (s,) = llm_spans(spans)
    assert s.attributes["llm.token_count.prompt"] == 20
    assert s.attributes["llm.token_count.completion"] == 7
    assert s.attributes["output.value"] == "streamed answer"


async def test_config_name_is_rewritten_and_version_defaulted(fake):
    fake.queue_reply("ok")
    proxy = ModelProxy(MODELS, transport=fake.transport)
    async with client_for(proxy) as c:
        r = await c.post("/v1/messages", content=messages_body("opus"))
    assert r.status_code == 200
    up = fake.requests[-1]
    assert up.json["model"] == "claude-opus-5-5"
    assert up.json["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert up.headers["anthropic-version"] == server_mod.ANTHROPIC_VERSION
    assert "stream_options" not in up.json  # an OpenAI field never reaches Anthropic


async def test_non_streaming_body_is_recorded_with_tool_calls(fake, spans, tmp_path):
    fake.queue_reply(
        "editing",
        tool_calls=[{"id": "tu_1", "name": "edit", "input": {"file": "x.py"}}],
        usage={"input_tokens": 30},
    )
    proxy = ModelProxy(MODELS, transport=fake.transport)
    with (
        tracing.ticket_span(1) as tt,
        tracing.attempt_span(2, variant_id="v", model="sonnet", tier=3, trigger="first"),
        tracing.Recorder(tmp_path / "rec", 1, 2, trace_id=tt.trace_id) as rec,
    ):
        async with client_for(proxy) as c:
            r = await c.post("/a/2/v1/messages", content=messages_body())
        assert r.status_code == 200
        assert r.content == fake.sent[-1]
    tracing.load_recording(rec.path)  # fde-kit reads it back
    (row,) = [x for x in raw_rows(rec.path) if x["kind"] == "llm"]
    assert row["text"] == "editing"
    assert row["tool_calls"] == [{"id": "tu_1", "name": "edit", "input": {"file": "x.py"}}]
    assert row["stop_reason"] == "tool_use"
    assert row["usage"] == {"input_tokens": 30, "output_tokens": 7}


async def test_streamed_tool_call_is_rebuilt_from_json_deltas(fake, tmp_path):
    fake.queue_reply(
        "",
        tool_calls=[{"id": "tu_2", "name": "bash", "input": {"cmd": "pytest -q"}}],
    )
    proxy = ModelProxy(MODELS, transport=fake.transport)
    with tracing.Recorder(tmp_path / "rec", 1, 3) as rec:
        async with client_for(proxy) as c:
            await c.post("/a/3/v1/messages", content=messages_body(stream=True))
    (row,) = [x for x in raw_rows(rec.path) if x["kind"] == "llm"]
    assert row["tool_calls"] == [{"id": "tu_2", "name": "bash", "input": {"cmd": "pytest -q"}}]
    assert row["stop_reason"] == "tool_use"


# --------------------------------------------------------------------------- cached tokens


def hand_cost(model: str, out: int, *, w5: int = 2_000, w1: int = 0) -> float:
    """1,000 plain + 9,000 cache-read + 2,000 cache-write input tokens, by hand."""
    rates = {  # input, output, cache hit (pricing page, 2026-11-25)
        "claude-sonnet-5-5": (2.0, 10.0, 0.10),
        "claude-opus-5-5": (4.0, 20.0, 0.20),
        "claude-fable-5-2": (8.0, 40.0, 0.20),
    }
    i, o, hit = rates[model]
    return (1_000 * i + 9_000 * hit + w5 * i * 1.25 + w1 * i * 2.0 + out * o) / 1e6


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("sonnet", "claude-sonnet-5-5"),
        ("opus", "claude-opus-5-5"),
        ("fable", "claude-fable-5-2"),
    ],
)
async def test_streamed_cache_counts_store_all_input_and_price_each_kind(
    fake, spans, tmp_path, name, model
):
    store = Store(tmp_path / "db.sqlite")
    store.migrate()
    ticket = store.create_ticket(
        repo="o/r", issue_number=1, title="t", request="r", base_branch="main"
    )
    attempt = store.add_attempt(ticket.id, model=name, tier=3, trigger="first")
    usage: list[CallUsage] = []
    fake.queue_reply("cached", usage=dict(CACHED_USAGE), output_tokens=500)
    proxy = ModelProxy(MODELS, transport=fake.transport, store=store, on_usage=usage.append)
    async with client_for(proxy) as c:
        r = await c.post(f"/a/{attempt.id}/v1/messages", content=messages_body(model, stream=True))
    assert r.status_code == 200
    got = store.get_attempt(attempt.id)
    assert got.tokens_in == 12_000  # input + cache reads + cache writes
    assert got.tokens_out == 500
    assert got.cost_usd == pytest.approx(hand_cost(model, 500))
    # and below the same token count at the uncached rate
    ep = next(m for m in MODELS if m.name == name)
    assert got.cost_usd < pricing.price_for(ep).cost(12_000, 500)
    (u,) = usage
    assert (u.cache_read_tokens, u.cache_write_tokens) == (9_000, 2_000)
    (s,) = llm_spans(spans)
    assert s.attributes["llm.token_count.prompt"] == 12_000
    assert s.attributes["llm.token_count.prompt_details.cache_read"] == 9_000
    assert s.attributes["llm.token_count.prompt_details.cache_write"] == 2_000
    store.close()


async def test_one_hour_writes_use_the_2x_multiplier_and_body_usage(fake):
    usage = dict(
        CACHED_USAGE,
        cache_creation={"ephemeral_5m_input_tokens": 500, "ephemeral_1h_input_tokens": 1_500},
    )
    fake.queue_reply("ok", usage=usage, output_tokens=100)
    got: list[CallUsage] = []
    proxy = ModelProxy(MODELS, transport=fake.transport, on_usage=got.append)
    async with client_for(proxy) as c:
        await c.post("/a/1/v1/messages", content=messages_body())  # not streamed
    assert got[0].tokens_in == 12_000
    assert got[0].cost_usd == pytest.approx(hand_cost("claude-sonnet-5-5", 100, w5=500, w1=1_500))


async def test_message_delta_usage_is_cumulative_and_replaces_start(fake):
    # The final message_delta repeats the input counts (web-search example in the docs).
    fake.queue_reply(
        "ok",
        usage={"input_tokens": 5, "cache_read_input_tokens": 0},
        delta_usage={"input_tokens": 50, "cache_read_input_tokens": 70, "output_tokens": 9},
    )
    got: list[CallUsage] = []
    proxy = ModelProxy(MODELS, transport=fake.transport, on_usage=got.append)
    async with client_for(proxy) as c:
        await c.post("/a/1/v1/messages", content=messages_body(stream=True))
    assert (got[0].tokens_in, got[0].tokens_out, got[0].cache_read_tokens) == (120, 9, 70)


def test_price_cost_with_cache_counts():
    sonnet = pricing.price_for(MODELS[0])
    plain = sonnet.cost(12_000, 0)
    assert plain == pytest.approx(12_000 * 2 / 1e6)  # no cache counts: as before #52
    assert sonnet.cost(12_000, 0, cache_read=9_000) == pytest.approx(
        (3_000 * 2 + 9_000 * 0.10) / 1e6
    )
    # A model with no cache rate (the conservative default) prices reads as input.
    unknown = pricing.CONSERVATIVE_DEFAULT
    assert unknown.cost(1_000, 0, cache_read=900) == pytest.approx(unknown.cost(1_000, 0))
    # Haiku 5.5's long-prompt rate counts cached tokens toward the threshold.
    haiku = pricing.PRICES["claude-haiku-5-5"]
    assert haiku.cost(200_000, 0, cache_read=150_000) == pytest.approx(
        (50_000 * 0.50 + 150_000 * 0.05) / 1e6
    )


# --------------------------------------------------------------------------- GLM (chat path)


def glm_transport(usage: dict, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "glm says"},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


GLM_CHAT = {"model": "glm", "messages": [{"role": "user", "content": "hi"}]}


async def test_glm_cached_tokens_are_priced_at_the_cached_rate(spans):
    seen: list[httpx.Request] = []
    usage = {
        "prompt_tokens": 1_200,
        "completion_tokens": 100,
        "total_tokens": 1_300,
        "prompt_tokens_details": {"cached_tokens": 800},
    }
    got: list[CallUsage] = []
    proxy = ModelProxy(MODELS, transport=glm_transport(usage, seen), on_usage=got.append)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/chat/completions", json=GLM_CHAT)
    assert r.status_code == 200
    assert "cache_control" not in json.loads(seen[0].content)  # no request change for GLM
    assert got[0].tokens_in == 1_200  # prompt_tokens already includes the cached ones
    assert got[0].cache_read_tokens == 800
    assert got[0].cost_usd == pytest.approx((400 * 1.4 + 800 * 0.26 + 100 * 4.4) / 1e6)
    (s,) = llm_spans(spans)
    assert s.attributes["llm.token_count.prompt_details.cache_read"] == 800


async def test_glm_without_prompt_tokens_details_costs_as_before():
    usage = {"prompt_tokens": 1_200, "completion_tokens": 100, "total_tokens": 1_300}
    got: list[CallUsage] = []
    proxy = ModelProxy(MODELS, transport=glm_transport(usage, []), on_usage=got.append)
    async with client_for(proxy) as c:
        await c.post("/a/1/v1/chat/completions", json=GLM_CHAT)
    assert got[0].cache_read_tokens == 0
    assert got[0].cost_usd == pytest.approx((1_200 * 1.4 + 100 * 4.4) / 1e6)


# --------------------------------------------------------------------------- recording, secrets


async def test_cache_counts_reach_a_recorder_that_takes_them(fake, monkeypatch):
    """The real Recorder.llm has no cache parameters yet (wiring); one that does gets them."""
    rows: list[dict] = []

    class Rec:
        def llm(self, *, cache_read_tokens: int = 0, cache_write_tokens: int = 0, **kw):
            rows.append({**kw, "cache_read": cache_read_tokens, "cache_write": cache_write_tokens})

    monkeypatch.setattr(server_mod.tracing, "get_recorder", lambda aid, role="attempt": Rec())
    fake.queue_reply("ok", usage=dict(CACHED_USAGE))
    proxy = ModelProxy(MODELS, transport=fake.transport)
    async with client_for(proxy) as c:
        await c.post("/a/4/v1/messages", content=messages_body(stream=True))
    assert (rows[0]["cache_read"], rows[0]["cache_write"]) == (9_000, 2_000)
    assert rows[0]["tokens_in"] == 12_000


async def test_key_never_leaks_on_the_messages_route(fake, spans, tmp_path, caplog, fake_keychain):
    caplog.set_level(logging.DEBUG)
    key = fake_keychain[("foreman-inator", "anthropic")]
    fake.queue_reply("answer", usage=dict(CACHED_USAGE))
    fake.fail_next(
        401, {"type": "error", "error": {"type": "authentication_error", "message": f"bad {key}"}}
    )
    proxy = ModelProxy(MODELS, transport=fake.transport)
    bodies = []
    with (
        tracing.ticket_span(1),
        tracing.attempt_span(2, variant_id="v", model="sonnet", tier=3, trigger="first"),
        tracing.Recorder(tmp_path / "rec", 1, 2) as rec,
    ):
        async with client_for(proxy) as c:
            for stream in (True, False):
                r = await c.post("/a/2/v1/messages", content=messages_body(stream=stream))
                bodies.append(r.content)
    assert all(r.headers["x-api-key"] == key for r in fake.requests)  # upstream only
    assert b"[redacted]" in bodies[0]  # the 401 came first; its echo of the key is cut
    everything = [json.dumps(dict(s.attributes), default=str) for s in spans.get_finished_spans()]
    everything += [s.status.description or "" for s in spans.get_finished_spans()]
    everything += [b.decode() for b in bodies]
    everything += [json.dumps(raw_rows(rec.path))]
    everything += [r.getMessage() for r in caplog.records]
    assert not any(key in text for text in everything)
    error_span, ok_span = llm_spans(spans)
    assert ok_span.attributes["llm.token_count.prompt_details.cache_read"] == 9_000
    assert "llm.token_count.prompt_details.cache_read" not in error_span.attributes


# --------------------------------------------------------------------------- fail closed


async def test_keyless_claude_endpoint_is_refused_and_nothing_is_sent(fake):
    proxy = ModelProxy(MODELS, transport=fake.transport)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/messages", content=messages_body("keyless"))
    assert r.status_code == 403
    assert r.json()["type"] == "error"
    assert r.json()["error"]["code"] == "cloud_key_missing"
    assert fake.requests == []


async def test_unknown_model_is_404_on_the_messages_route(fake):
    proxy = ModelProxy(MODELS, transport=fake.transport)
    async with client_for(proxy) as c:
        r = await c.post("/a/1/v1/messages", content=messages_body("claude-nope"))
    assert r.status_code == 404 and fake.requests == []


async def test_spend_cap_refusal_is_429_and_budget_hook_sees_both_phases(fake):
    phases: list[tuple[str, int | None]] = []

    def refuse(ep, cost, ctx, *, phase):
        phases.append((phase, ctx.attempt_id))
        return False

    proxy = ModelProxy(MODELS, transport=fake.transport, spend_hook=refuse)
    async with client_for(proxy) as c:
        r = await c.post("/a/5/v1/messages", content=messages_body())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "spend_cap_reached"
    assert fake.requests == []
    assert phases == [("estimate", 5)]

    phases.clear()
    allowed: list[tuple[str, float]] = []

    def allow(ep, cost, ctx, *, phase):
        allowed.append((phase, cost))
        return True

    fake.queue_reply("ok", usage=dict(CACHED_USAGE), output_tokens=500)
    proxy = ModelProxy(MODELS, transport=fake.transport, spend_hook=allow)
    async with client_for(proxy) as c:
        await c.post("/a/5/v1/messages", content=messages_body(stream=True))
    (est_phase, est), (act_phase, act) = allowed
    assert (est_phase, act_phase) == ("estimate", "actual")
    assert act == pytest.approx(hand_cost("claude-sonnet-5-5", 500))


async def test_local_writer_messages_call_holds_a_local_slot(fake):
    slots = LocalSlots(1)
    in_use: list[int] = []
    fake.on_request = lambda req: in_use.append(slots.in_use)
    fake.queue_reply("ok")
    proxy = ModelProxy(MODELS, transport=fake.transport, local_slots=slots)
    async with client_for(proxy) as c:
        r = await c.post("/a/6/v1/messages", content=messages_body("local"))
    assert r.status_code == 200
    assert fake.requests[-1].url == f"{OLLAMA}/messages"
    assert "x-api-key" not in fake.requests[-1].headers  # local: no key
    assert in_use == [1] and slots.in_use == 0
