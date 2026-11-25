"""Chat foundations in the proxy and tracing (wave C1b, GH #60; docs/chat-design.md
sections 5 and 9, decision 2).

* ``proxy.context(chat_session_id=)``: no attempt usage, recorder ``(id, "chat")``, slot
  key ``("chat", id)``, ``chat.id`` / ``chat.turn`` on the span.
* Local slot priority: a queued chat call goes ahead of queued ticket calls (in process
  and across processes through the lock-file slots), never preempting a running call.
* The fallback hook: a chat call that waits ``chat_local_wait_seconds`` asks
  ``fallback(context)`` once; a cloud endpoint takes the call, None keeps it waiting.
* ``chat_recording_path``, chat recorders, ``prune_chats``, ``chat_span``,
  ``chat_turn_span``.

No real model, network or ``~/.foreman-inator``."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import time
from pathlib import Path

import httpx
import pytest

from fakes.models import FakeModels
from foreman_inator import config as config_mod
from foreman_inator import tracing
from foreman_inator.config import ModelEndpoint, key_ref
from foreman_inator.proxy.server import FALLBACK_HEADER, CallContext, LocalSlots, ModelProxy
from foreman_inator.tracing import otel, recorder

OLLAMA = "http://127.0.0.1:11434/v1"
LOCAL = ModelEndpoint("qwen-small", 1, "local", "qwen3.6:35b-a3b", OLLAMA, "qwen")
CLOUD = ModelEndpoint(
    "claude",
    3,
    "cloud",
    "claude-sonnet-5-5",
    "https://api.anthropic.com/v1",
    "claude",
    key_ref("anthropic"),
)
MODELS = (LOCAL, CLOUD)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    store = {("foreman-inator", "anthropic"): "sk-fake-" + secrets.token_hex(16)}
    monkeypatch.setattr(config_mod.keyring, "get_password", lambda s, n: store.get((s, n)))


@pytest.fixture
def spans():
    with tracing.capture_spans() as exporter:
        yield exporter


class Gated(httpx.AsyncBaseTransport):
    """FakeModels behind a gate for local (127.0.0.1) requests: each waits for
    `open_one()`. Cloud requests go straight through. `arrived` logs upstream model ids."""

    def __init__(self, fake: FakeModels):
        self.fake = fake
        self.arrived: asyncio.Queue[str] = asyncio.Queue()
        self._tokens: asyncio.Queue[None] = asyncio.Queue()

    def open_one(self) -> None:
        self._tokens.put_nowait(None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.arrived.put_nowait(f"{request.url.host}:{body['messages'][0]['content']}")
        if request.url.host == "127.0.0.1":
            await self._tokens.get()
        return await self.fake.transport.handle_async_request(request)


def chat(text: str = "hi", model: str = "qwen-small", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": text}], **extra}


def client_for(proxy: ModelProxy) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url=proxy.url)


def path(ctx: CallContext) -> str:
    return f"/c/{ctx.token}/v1/chat/completions"


async def arrival(gate: Gated, seconds: float = 3.0) -> str:
    return await asyncio.wait_for(gate.arrived.get(), seconds)


class FakeStore:
    def __init__(self) -> None:
        self.usage: list[tuple] = []

    def add_attempt_usage(self, attempt_id, **kw) -> None:
        self.usage.append((attempt_id, kw))


# --------------------------------------------------------------------------- the context


def test_chat_context_shape():
    proxy = ModelProxy(MODELS)
    with proxy.context(chat_session_id=5) as ctx:
        assert ctx.chat_session_id == 5 and ctx.is_chat
        assert ctx.slot_key == ("chat", 5)
        assert ctx.recorder_key == (5, "chat")
        assert ctx.recorder_role == "chat" and ctx.label == "chat"
        assert not ctx.counts_toward_attempt
        assert ctx.bucket == "live"
    with pytest.raises(ValueError), proxy.context(chat_session_id=5, attempt_id=5):
        pass
    # a ticket context is unchanged
    with proxy.context(attempt_id=5) as tctx:
        assert tctx.recorder_key == (5, "attempt") and tctx.slot_key is None
        assert tctx.counts_toward_attempt


@pytest.mark.anyio
async def test_chat_call_records_to_the_chat_recorder_not_the_attempt(spans, tmp_path):
    """A chat session 5 and an attempt 5 are both open: the chat's call goes to the chat
    recording only, adds no attempt usage, and its span carries chat.id and chat.turn,
    parented to the `chat` span."""
    store = FakeStore()
    usages = []
    proxy = ModelProxy(
        MODELS, store=store, on_usage=usages.append, transport=FakeModels().transport
    )
    with (
        recorder.chat_recorder(tmp_path, 5, model="qwen-small") as chat_rec,
        tracing.Recorder(tmp_path, 1, 5) as attempt_rec,
        otel.chat_span(5, repo="o/r") as ct,
        proxy.context(chat_session_id=5) as ctx,
    ):
        with otel.chat_turn_span(5, 1, tier=1, model="qwen-small"):
            async with client_for(proxy) as c:
                r = await c.post(path(ctx), json=chat())
        assert r.status_code == 200
        chat_path, attempt_path = chat_rec.path, attempt_rec.path
    assert store.usage == []
    assert usages[0].context.chat_session_id == 5
    chat_rows = [r["kind"] for r in recorder.load_recording(chat_path).rows]
    attempt_rows = [r["kind"] for r in recorder.load_recording(attempt_path).rows]
    assert chat_rows.count("llm") == 1
    assert attempt_rows.count("llm") == 0
    (call,) = [s for s in spans.get_finished_spans() if s.name == "llm.call"]
    (root,) = [s for s in spans.get_finished_spans() if s.name == "chat"]
    assert call.attributes["chat.id"] == 5 and call.attributes["chat.turn"] == 1
    assert "ticket.id" not in call.attributes
    assert call.parent.span_id == root.context.span_id
    assert format(call.context.trace_id, "032x") == ct.trace_id


@pytest.mark.anyio
async def test_chat_queue_wait_is_kept_under_its_own_key():
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        with proxy.context(chat_session_id=2) as ctx:
            holder = asyncio.create_task(c.post("/a/2/v1/chat/completions", json=chat("t")))
            await arrival(gate)
            queued = asyncio.create_task(c.post(path(ctx), json=chat("c")))
            await asyncio.sleep(0.3)
            assert proxy.chat_queue_wait(2) >= 0.2
            assert proxy.queue_wait(2) < 0.1  # attempt 2 is not chat session 2
            gate.open_one()
            await holder
            await arrival(gate)
            gate.open_one()
            assert (await queued).status_code == 200
    assert proxy.queue_wait(("chat", 2)) >= 0.2
    proxy.forget_queue_wait(("chat", 2))
    assert proxy.chat_queue_wait(2) == 0.0


# --------------------------------------------------------------------------- priority


@pytest.mark.anyio
async def test_queued_chat_call_goes_before_queued_ticket_calls():
    """Non-vacuity: with `release()` serving `self._waiters` before `self._priority`, the
    ticket call queued first reaches the model first and the order assertion fails."""
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        with proxy.context(chat_session_id=9) as ctx:
            running = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("t1")))
            assert await arrival(gate) == "127.0.0.1:t1"
            t2 = asyncio.create_task(c.post("/a/2/v1/chat/completions", json=chat("t2")))
            await asyncio.sleep(0.1)
            ch = asyncio.create_task(c.post(path(ctx), json=chat("chat")))
            await asyncio.sleep(0.2)
            assert gate.arrived.empty()  # nothing preempts the running call
            assert proxy.slots.waiting == 2
            gate.open_one()
            await running
            assert await arrival(gate) == "127.0.0.1:chat"
            gate.open_one()
            await ch
            assert await arrival(gate) == "127.0.0.1:t2"
            gate.open_one()
            assert (await t2).status_code == 200


@pytest.mark.anyio
async def test_free_slot_is_taken_by_a_ticket_when_no_chat_waits():
    slots = LocalSlots(1)
    await slots.acquire()
    assert slots.in_use == 1
    slots.release()
    await asyncio.wait_for(slots.acquire(), 1)
    slots.release()


@pytest.mark.anyio
async def test_cross_process_priority_waiter_beats_a_ticket(tmp_path):
    """Three `LocalSlots` on one lock dir stand in for three processes: the worker holding
    the slot, the worker's next ticket call, the chat process waiting with priority. When
    the slot frees, the ticket's next try (made before the chat's poll can run) defers,
    and the chat gets the slot.

    Non-vacuity: with the `priority_waiting()` check in `_try_files` removed, the ticket's
    try takes the free slot and the first `is False` assertion fails."""
    holder, ticket, chat_slots = (LocalSlots(1, lock_dir=tmp_path) for _ in range(3))
    await holder.acquire()
    ch = asyncio.create_task(chat_slots.acquire(priority=True))
    await asyncio.sleep(0.15)
    assert list(tmp_path.glob("local-priority-*.wait"))  # the chat announced itself
    assert ticket.priority_waiting()
    holder.release()
    assert ticket._try_files() is False  # a free slot, but a chat is waiting
    assert ticket.in_use == 0
    await asyncio.wait_for(ch, 2)
    assert chat_slots.in_use == 1
    assert not list(tmp_path.glob("local-priority-*.wait"))  # marker gone once granted
    polling = asyncio.create_task(ticket.acquire())
    await asyncio.sleep(0.15)
    assert not polling.done()  # never preempts the chat's running call
    chat_slots.release()
    await asyncio.wait_for(polling, 2)
    assert ticket.in_use == 1
    ticket.release()


@pytest.mark.anyio
async def test_stale_priority_marker_does_not_block_tickets(tmp_path):
    """A marker nobody holds a lock on (its process died) is removed and ignored."""
    stale = tmp_path / "local-priority-99999-dead.wait"
    stale.write_text("")
    slots = LocalSlots(1, lock_dir=tmp_path)
    await asyncio.wait_for(slots.acquire(), 1)
    assert not stale.exists()
    slots.release()


@pytest.mark.anyio
async def test_live_priority_marker_blocks_a_free_slot(tmp_path):
    marker = tmp_path / "local-priority-1-live.wait"
    fd = os.open(marker, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        slots = LocalSlots(1, lock_dir=tmp_path)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(slots.acquire(), 0.2)
        assert slots.in_use == 0
        await asyncio.wait_for(slots.acquire(priority=True), 1)  # a chat still gets it
        slots.release()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.anyio
async def test_cancelled_priority_waiter_removes_its_marker(tmp_path):
    holder, chat_slots = LocalSlots(1, lock_dir=tmp_path), LocalSlots(1, lock_dir=tmp_path)
    await holder.acquire()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(chat_slots.acquire(priority=True), 0.2)
    assert not list(tmp_path.glob("local-priority-*"))
    assert not list(tmp_path.glob(".local-priority-*"))
    holder.release()


# --------------------------------------------------------------------------- no probe


@pytest.mark.anyio
async def test_chat_keeps_priority_with_no_probe_to_hand_the_slot_to_a_ticket():
    """Review P1 (wave 19): a probe that acquired the slot with priority and released it
    handed the slot to the queued ticket call, so the chat call that followed ran after it.
    The probe API is gone; the chat engine just makes the call (the in-call 5 s timer is
    decision 2), and it is served before the ticket queued ahead of it.

    Non-vacuity: restoring `wait_for_local_async` fails the first assertion; driving the
    engine through it (probe, then call) puts t2 first."""
    assert not hasattr(ModelProxy, "wait_for_local_async")
    assert not hasattr(ModelProxy, "wait_for_local")
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        with proxy.context(chat_session_id=4) as ctx:
            running = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("t1")))
            assert await arrival(gate) == "127.0.0.1:t1"
            t2 = asyncio.create_task(c.post("/a/2/v1/chat/completions", json=chat("t2")))
            await asyncio.sleep(0.1)
            ch = asyncio.create_task(c.post(path(ctx), json=chat("chat")))
            await asyncio.sleep(0.1)
            gate.open_one()
            await running
            assert await arrival(gate) == "127.0.0.1:chat"
            gate.open_one()
            await ch
            assert await arrival(gate) == "127.0.0.1:t2"
            gate.open_one()
            assert (await t2).status_code == 200


# --------------------------------------------------------------------------- fallback


class Hook:
    """A spend hook recording every call."""

    def __init__(self, allow_cloud: bool = True):
        self.allow_cloud = allow_cloud
        self.calls: list[tuple[str, str, CallContext]] = []

    def __call__(self, ep, cost, ctx, *, phase):
        self.calls.append((ep.name, phase, ctx))
        return True if ep.kind == "local" else self.allow_cloud


@pytest.mark.anyio
async def test_busy_local_slot_falls_back_to_the_hooks_cloud_model(spans):
    """Non-vacuity: with the `switch` handed to `_wait_for_slot` left as None, the chat
    call keeps waiting behind the ticket and the `wait_for` below times out."""
    gate = Gated(FakeModels())
    asked: list[CallContext] = []
    hook = Hook()

    def fallback(ctx):
        asked.append(ctx)
        return CLOUD

    proxy = ModelProxy(
        MODELS,
        transport=gate,
        spend_hook=hook,
        fallback=fallback,
        chat_local_wait_seconds=0.2,
    )
    async with client_for(proxy) as c:
        with otel.chat_span(3), proxy.context(chat_session_id=3) as ctx:
            with otel.chat_turn_span(3, 4, tier=1, model="qwen-small"):
                ticket = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("t")))
                await arrival(gate)
                t0 = time.monotonic()
                r = await asyncio.wait_for(c.post(path(ctx), json=chat("c")), 2)
                waited = time.monotonic() - t0
            assert r.status_code == 200
            assert r.headers[FALLBACK_HEADER] == "claude"
            assert r.json()["model"] == "claude-sonnet-5-5"
            assert await arrival(gate) == "api.anthropic.com:c"
            assert 0.15 <= waited < 1.5
            assert proxy.local_slots() == (1, 1)  # the ticket still holds its slot
            gate.open_one()
            assert (await ticket).status_code == 200
    assert len(asked) == 1 and asked[0].chat_session_id == 3
    cloud_calls = [(n, ph) for n, ph, cx in hook.calls if cx.fallback_from == "qwen-small"]
    assert cloud_calls == [("claude", "estimate"), ("claude", "actual")]
    (call,) = [
        s
        for s in spans.get_finished_spans()
        if s.name == "llm.call" and s.attributes["llm.model_name"] == "claude"
    ]
    assert call.attributes["llm.local_fallback"] == "local slot busy, used claude"
    assert call.attributes["llm.fallback_from"] == "qwen-small"
    assert call.attributes["chat.turn"] == 4
    assert call.attributes["llm.queue_wait_s"] >= 0.15
    (turn,) = [s for s in spans.get_finished_spans() if s.name == "chat.turn"]
    (event,) = [e for e in turn.events if e.name == "local_fallback"]
    assert event.attributes["message"] == "local slot busy, used claude"


@pytest.mark.anyio
@pytest.mark.parametrize("answer", ["none", "local", "unknown", "refused", "raises"])
async def test_no_usable_fallback_keeps_waiting_for_local(answer):
    gate = Gated(FakeModels())
    asked = []
    other = ModelEndpoint("other", 3, "cloud", "x", "https://example.com/v1", "x", None)

    def fallback(ctx):
        asked.append(ctx)
        if answer == "raises":
            raise RuntimeError("boom")
        return {"none": None, "local": LOCAL, "unknown": other, "refused": CLOUD}[answer]

    proxy = ModelProxy(
        MODELS,
        transport=gate,
        spend_hook=Hook(allow_cloud=answer != "refused"),
        fallback=fallback,
        chat_local_wait_seconds=0.1,
    )
    async with client_for(proxy) as c:
        with proxy.context(chat_session_id=3) as ctx:
            ticket = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("t")))
            await arrival(gate)
            ch = asyncio.create_task(c.post(path(ctx), json=chat("c")))
            await asyncio.sleep(0.5)
            assert gate.arrived.empty() and not ch.done()
            gate.open_one()
            await ticket
            assert await arrival(gate) == "127.0.0.1:c"
            gate.open_one()
            r = await ch
    assert r.status_code == 200 and FALLBACK_HEADER not in r.headers
    assert len(asked) == 1  # asked once, not on every poll


@pytest.mark.anyio
async def test_ticket_calls_never_ask_the_fallback():
    gate = Gated(FakeModels())
    asked = []
    proxy = ModelProxy(
        MODELS,
        transport=gate,
        fallback=lambda ctx: asked.append(ctx) or CLOUD,
        chat_local_wait_seconds=0.05,
    )
    async with client_for(proxy) as c:
        first = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("t1")))
        await arrival(gate)
        second = asyncio.create_task(c.post("/a/2/v1/chat/completions", json=chat("t2")))
        await asyncio.sleep(0.4)
        assert gate.arrived.empty()
        gate.open_one()
        await first
        assert await arrival(gate) == "127.0.0.1:t2"
        gate.open_one()
        r = await second
    assert r.status_code == 200 and FALLBACK_HEADER not in r.headers
    assert asked == []


@pytest.mark.anyio
async def test_chat_with_a_free_slot_stays_local():
    asked = []
    proxy = ModelProxy(
        MODELS,
        transport=FakeModels().transport,
        fallback=lambda ctx: asked.append(ctx) or CLOUD,
        chat_local_wait_seconds=0.05,
    )
    async with client_for(proxy) as c:
        with proxy.context(chat_session_id=1) as ctx:
            r = await c.post(path(ctx), json=chat())
    assert r.status_code == 200 and r.json()["model"] == "qwen3.6:35b-a3b"
    assert asked == [] and FALLBACK_HEADER not in r.headers


# --------------------------------------------------------------------------- recordings


def test_chat_recorder_file_header_and_registry(tmp_path):
    assert recorder.chat_recording_path(tmp_path, 7) == tmp_path / "chat" / "7.jsonl.gz"
    with recorder.chat_recorder(tmp_path, 7, model="qwen-small", repo="o/r") as rec:
        assert tracing.get_recorder(7, "chat") is rec
        assert tracing.get_recorder(7) is None
        with tracing.Recorder(tmp_path, 1, 7) as att:  # same id, other role: no clash
            assert tracing.get_recorder(7) is att
        rec.user("add a test", by="user")
        rec.llm(text="x" * (recorder.CONTENT_LIMIT + 1), tokens_in=1, tokens_out=1)
    assert tracing.get_recorder(7, "chat") is None
    assert (tmp_path / "chat" / "7.files").is_dir()
    run = recorder.load_recording(tmp_path / "chat" / "7.jsonl.gz")
    header = run.header
    assert header["kind"] == "header" and header["run_id"] == "chat-7" == run.run_id
    assert header["role"] == "chat" and header["chat_session_id"] == 7
    assert header["ticket_id"] is None and header["attempt_id"] is None
    assert header["repo"] == "o/r"
    assert [r["kind"] for r in run.rows] == ["user", "llm", "result"]
    with pytest.raises(ValueError):
        tracing.Recorder(tmp_path, 1, 8, role="chat")


def _age(p: Path, days: float, now: float) -> None:
    t = now - days * 86400
    os.utime(p, (t, t))


def test_prune_chats(tmp_path):
    now = time.time()
    chat_dir = tmp_path / "chat"
    chat_dir.mkdir()
    for sid in (1, 2, 3):
        f = chat_dir / f"{sid}.jsonl.gz"
        f.write_bytes(b"x")
        _age(f, 100 if sid != 3 else 10, now)
    (chat_dir / "1.files").mkdir()
    (chat_dir / "1.files" / "0001-llm.txt").write_text("big")
    (chat_dir / "notes.txt").write_text("not a recording")
    old_partial = chat_dir / "4.jsonl"
    old_partial.write_text("{}")
    _age(old_partial, 100, now)

    dry = recorder.prune_chats(tmp_path, merged={2}, now=now, dry_run=True)
    assert sorted(p.name for p in dry) == ["1.files", "1.jsonl.gz", "4.jsonl"]
    assert (chat_dir / "1.jsonl.gz").exists()

    # the ticket rule never looks inside chat/
    assert recorder.prune(tmp_path, merged=set(), now=now + 10**9) == []

    deleted = recorder.prune_chats(tmp_path, merged=lambda sid: sid == 2, now=now)
    assert sorted(p.name for p in deleted) == ["1.files", "1.jsonl.gz", "4.jsonl"]
    assert sorted(p.name for p in chat_dir.iterdir()) == ["2.jsonl.gz", "3.jsonl.gz", "notes.txt"]


def test_prune_chats_keeps_an_open_recording(tmp_path):
    now = time.time()
    with recorder.chat_recorder(tmp_path, 5) as rec:
        _age(rec.partial_path, 200, now)
        assert recorder.prune_chats(tmp_path, merged=set(), now=now) == []
    assert recorder.prune_chats(tmp_path / "missing", merged=set()) == []


# --------------------------------------------------------------------------- spans


def test_chat_span_and_turns(spans):
    # even inside a ticket's block, a chat is its own trace
    with tracing.ticket_span(1), otel.chat_span(4, repo="o/r", base_branch="main") as ct:
        assert not ct.resumed
        assert otel.chat_turn(4) is None
        with otel.chat_turn_span(4, 1, tier=1, model="qwen-small"):
            assert otel.chat_turn(4) == 1
            assert otel.chat_turn_event(4, "note", {"a": 1, "b": None})
        assert otel.chat_turn(4) is None
        assert not otel.chat_turn_event(4, "note", {})
    finished = {s.name: s for s in spans.get_finished_spans()}
    root, turn = finished["chat"], finished["chat.turn"]
    assert root.parent is None
    assert format(root.context.span_id, "016x") == otel.chat_root_span_id(ct.trace_id)
    assert dict(root.attributes) == {"chat.id": 4, "chat.repo": "o/r", "chat.base_branch": "main"}
    assert turn.parent.span_id == root.context.span_id
    assert dict(turn.attributes) == {
        "chat.id": 4,
        "chat.turn": 1,
        "chat.tier": 1,
        "chat.model": "qwen-small",
    }
    assert [(e.name, dict(e.attributes)) for e in turn.events] == [("note", {"a": 1})]


def test_resumed_chat_span_joins_the_stored_trace(spans):
    with otel.chat_span(4) as first:
        pass
    with otel.chat_span(4, trace_id=first.trace_id.upper()) as again:
        assert again.resumed and again.trace_id == first.trace_id
        with otel.chat_turn_span(4, 2, tier=2, model="claude"):
            pass
    roots = [s for s in spans.get_finished_spans() if s.name == "chat"]
    assert len(roots) == 1
    (turn,) = [s for s in spans.get_finished_spans() if s.name == "chat.turn"]
    assert turn.parent.span_id == roots[0].context.span_id
    assert turn.context.trace_id == roots[0].context.trace_id
