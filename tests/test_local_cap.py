"""Global cap on concurrent local-model inference (#34, todo 57).

The proxy holds one `LocalSlots` for completion calls to `local` endpoints; cloud calls and
embeddings are not limited. Time queued is on the `llm.call` span (`llm.queue_wait_s`),
left out of its latency, and handed to the OpenCode driver as extra time so it never
counts against the writer timeout. No real model, network or `~/.foreman-inator`."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

import httpx
import pytest

from fakes.models import FakeModels
from fakes.opencode import FakeOpenCode, Script, say, sleep
from foreman_inator import config as config_mod
from foreman_inator import tracing
from foreman_inator.config import ModelEndpoint, key_ref
from foreman_inator.proxy.server import DEFAULT_MAX_LOCAL_INFERENCE, LocalSlots, ModelProxy
from foreman_inator.runner.opencode import OpenCodeDriver
from foreman_inator.runner.worktree import GitIdentity, WorktreeManager

OLLAMA = "http://127.0.0.1:11434/v1"
MODELS = (
    ModelEndpoint("qwen-small", 1, "local", "qwen3.6:35b-a3b", OLLAMA, "qwen"),
    ModelEndpoint("gemma", 2, "local", "gemma4:27b", OLLAMA, "gemma"),
    ModelEndpoint(
        "claude",
        3,
        "cloud",
        "claude-sonnet-5-5",
        "https://api.anthropic.com/v1",
        "claude",
        key_ref("anthropic"),
    ),
)


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
    """FakeModels behind a gate: each upstream request blocks until `open_one()`. Counts
    how many requests are in flight per upstream host (local = Ollama's 127.0.0.1)."""

    def __init__(self, fake: FakeModels, *, block: bool = True):
        self.fake = fake
        self.block = block
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.arrived: asyncio.Queue[str] = asyncio.Queue()
        self._tokens: asyncio.Queue[None] = asyncio.Queue()

    def open_one(self) -> None:
        self._tokens.put_nowait(None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.active[host] = self.active.get(host, 0) + 1
        self.peak[host] = max(self.peak.get(host, 0), self.active[host])
        self.arrived.put_nowait(json.loads(request.content)["model"])
        try:
            if self.block:
                await self._tokens.get()
            return await self.fake.transport.handle_async_request(request)
        finally:
            self.active[host] -= 1


def chat(model: str, **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "add a test"}], **extra}


def client_for(proxy: ModelProxy) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url=proxy.url)


async def until(pred, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def arrival(gate: Gated, seconds: float = 3.0) -> str:
    return await asyncio.wait_for(gate.arrived.get(), seconds)


# --------------------------------------------------------------------------- the cap


@pytest.mark.anyio
async def test_second_local_call_waits_for_the_first(spans):
    """Non-vacuity: with the `_wait_for_slot` block in `_handle` removed, both calls reach
    Ollama at once (peak 2) and the `gate.arrived.empty()` assertion fails."""
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        first = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("qwen-small")))
        assert await arrival(gate) == "qwen3.6:35b-a3b"
        second = asyncio.create_task(c.post("/a/2/v1/chat/completions", json=chat("gemma")))
        await asyncio.sleep(0.4)
        assert gate.arrived.empty()  # queued in the proxy, not sent
        assert proxy.local_slots() == (1, 1)
        assert proxy.slots.waiting == 1
        assert proxy.queue_wait(2) >= 0.3  # the live wait is visible while it lasts
        assert proxy.queue_wait(1) < 0.1
        gate.open_one()
        assert (await first).status_code == 200
        assert await arrival(gate) == "gemma4:27b"
        gate.open_one()
        assert (await second).status_code == 200
    assert gate.peak["127.0.0.1"] == 1
    assert proxy.local_slots() == (0, 1)
    waits = {
        s.attributes["llm.model_name"]: s.attributes.get("llm.queue_wait_s")
        for s in spans.get_finished_spans()
        if s.name == "llm.call"
    }
    assert waits["qwen-small"] < 0.1
    assert waits["gemma"] >= 0.3
    assert proxy.queue_wait(2) == pytest.approx(waits["gemma"], abs=0.01)


@pytest.mark.anyio
async def test_queue_wait_is_not_latency(spans):
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        first = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("qwen-small")))
        await arrival(gate)
        second = asyncio.create_task(c.post("/a/2/v1/chat/completions", json=chat("qwen-small")))
        await asyncio.sleep(0.5)
        gate.open_one()
        await first
        await arrival(gate)
        gate.open_one()
        await second
    (span,) = [
        s
        for s in spans.get_finished_spans()
        if s.name == "llm.call" and s.attributes.get("llm.queue_wait_s", 0) >= 0.4
    ]
    assert span.attributes["llm.latency_ms"] < 300  # the 0.5 s in the queue is not latency


@pytest.mark.anyio
async def test_cloud_calls_are_not_limited(spans):
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        local = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("qwen-small")))
        await arrival(gate)
        clouds = [
            asyncio.create_task(c.post(f"/a/{i}/v1/chat/completions", json=chat("claude")))
            for i in (2, 3)
        ]
        assert {await arrival(gate), await arrival(gate)} == {"claude-sonnet-5-5"}
        assert gate.peak["api.anthropic.com"] == 2
        for _ in range(3):
            gate.open_one()
        for t in [local, *clouds]:
            assert (await t).status_code == 200
    cloud = [s for s in spans.get_finished_spans() if s.attributes["llm.model_name"] == "claude"]
    assert all("llm.queue_wait_s" not in s.attributes for s in cloud)


@pytest.mark.anyio
async def test_embeddings_are_not_limited():
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        writer = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("qwen-small")))
        await arrival(gate)
        emb = asyncio.create_task(
            c.post("/v1/embeddings", json={"model": "qwen-small", "input": "x"})
        )
        await arrival(gate)  # reached the upstream while the writer holds the slot
        gate.open_one()
        gate.open_one()
        assert (await writer).status_code == 200 and (await emb).status_code == 200


@pytest.mark.anyio
async def test_a_stream_holds_its_slot_until_it_ends():
    fake = FakeModels()
    fake.stream_delay = 0.15
    fake.queue_reply("a b c d")
    gate = Gated(fake, block=False)
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        streamed = asyncio.create_task(
            c.post("/a/1/v1/chat/completions", json=chat("qwen-small", stream=True))
        )
        await arrival(gate)
        await asyncio.sleep(0.05)
        other = asyncio.create_task(c.post("/a/2/v1/chat/completions", json=chat("qwen-small")))
        await asyncio.sleep(0.2)
        assert gate.arrived.empty()  # headers are back but the stream is still running
        assert proxy.local_slots()[0] == 1
        assert (await streamed).status_code == 200
        assert (await other).status_code == 200
    assert proxy.local_slots() == (0, 1)


@pytest.mark.anyio
async def test_failed_upstream_gives_the_slot_back():
    fake = FakeModels()
    fake.raise_next(httpx.ConnectError("refused"))
    fake.fail_next(500)
    proxy = ModelProxy(MODELS, transport=fake.transport)
    async with client_for(proxy) as c:
        r1 = await c.post("/a/1/v1/chat/completions", json=chat("qwen-small"))
        r2 = await c.post("/a/1/v1/chat/completions", json=chat("qwen-small"))
        r3 = await c.post("/a/1/v1/chat/completions", json=chat("qwen-small"))
    assert (r1.status_code, r2.status_code, r3.status_code) == (502, 500, 200)
    assert proxy.local_slots() == (0, 1)


@pytest.mark.anyio
async def test_client_that_leaves_while_queued_never_reaches_the_upstream():
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        holder = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("qwen-small")))
        await arrival(gate)

        body = json.dumps(chat("gemma")).encode()
        left = asyncio.Event()
        sent: list[dict] = []
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if messages:
                return messages.pop(0)
            await left.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/a/2/v1/chat/completions",
            "raw_path": b"/a/2/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 5000),
            "server": ("127.0.0.1", proxy.port or 80),
        }
        queued = asyncio.create_task(proxy.app(scope, receive, send))
        await until(lambda: proxy.slots.waiting == 1)
        left.set()
        await asyncio.wait_for(queued, 3)
        assert proxy.slots.waiting == 0
        gate.open_one()
        assert (await holder).status_code == 200
    assert gate.arrived.empty()  # gemma never went upstream
    assert proxy.local_slots() == (0, 1)


@pytest.mark.anyio
async def test_cap_can_be_raised_and_shared_between_proxies():
    gate = Gated(FakeModels())
    slots = LocalSlots(2)
    a = ModelProxy(MODELS, transport=gate, local_slots=slots)
    b = ModelProxy(MODELS, transport=gate, local_slots=slots)
    assert a.local_slots() == b.local_slots() == (0, 2)
    async with client_for(a) as ca, client_for(b) as cb:
        tasks = [
            asyncio.create_task(ca.post("/a/1/v1/chat/completions", json=chat("qwen-small"))),
            asyncio.create_task(cb.post("/x/2/v1/chat/completions", json=chat("qwen-small"))),
        ]
        await arrival(gate)
        await arrival(gate)
        tasks.append(
            asyncio.create_task(cb.post("/x/3/v1/chat/completions", json=chat("qwen-small")))
        )
        await until(lambda: slots.waiting == 1)
        assert a.local_slots() == (2, 2)
        for _ in range(3):
            gate.open_one()
        for t in tasks:
            assert (await t).status_code == 200
    assert gate.peak["127.0.0.1"] == 2
    assert b.queue_wait(3) > 0 and a.queue_wait(3) == 0  # each proxy knows its own waits


def test_default_cap_is_one():
    assert DEFAULT_MAX_LOCAL_INFERENCE == 1
    assert ModelProxy(MODELS).local_slots() == (0, 1)
    with pytest.raises(ValueError):
        LocalSlots(0)


@pytest.mark.anyio
async def test_cancelled_waiter_does_not_leak_a_slot():
    slots = LocalSlots(1)
    await slots.acquire()
    waiter = asyncio.create_task(slots.acquire())
    await until(lambda: slots.waiting == 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slots.waiting == 0
    slots.release()
    assert slots.in_use == 0
    await asyncio.wait_for(slots.acquire(), 1)  # still usable
    slots.release()


@pytest.mark.anyio
async def test_waiter_granted_then_cancelled_gives_the_slot_back():
    slots = LocalSlots(1)
    await slots.acquire()
    waiter = asyncio.create_task(slots.acquire())
    await until(lambda: slots.waiting == 1)
    slots.release()  # handed to the waiter (scheduled), then cancelled before it runs
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slots.in_use == 0


# --------------------------------------------------------------------------- the driver

ENDPOINT = MODELS[0]


@pytest.fixture
def worktree(tmp_path: Path):
    import subprocess

    def git(*args: str, cwd: Path) -> None:
        env = {
            "GIT_AUTHOR_NAME": "Seed",
            "GIT_AUTHOR_EMAIL": "seed@example.invalid",
            "GIT_COMMITTER_NAME": "Seed",
            "GIT_COMMITTER_EMAIL": "seed@example.invalid",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path),
        }
        subprocess.run(["git", *args], cwd=cwd, check=True, env=env, capture_output=True)

    remote = tmp_path / "remote" / "acme" / "widgets.git"
    remote.mkdir(parents=True)
    git("init", "-q", "--bare", "--initial-branch=trunk", cwd=remote)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "--initial-branch=trunk", cwd=seed)
    (seed / "app.py").write_text("def hello():\n    return 'hi'\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "--no-gpg-sign", "-m", "first", cwd=seed)
    git("push", "-q", str(remote), "trunk", cwd=seed)
    mgr = WorktreeManager(tmp_path / "home" / "worktrees", identity=GitIdentity("bot", "b@x.y"))
    return mgr.create("acme/widgets", "trunk", 7, 1, clone_url=str(remote))


def drive(fake: FakeOpenCode, worktree, *, timeout: float, **kw):
    driver: OpenCodeDriver = fake.driver(abort_grace=5.0, **kw)
    return driver.run(
        attempt_id=7,
        worktree=worktree,
        prompt="Change hello().",
        endpoint=ENDPOINT,
        proxy_base_url="http://127.0.0.1:47999/a/7/v1",
        timeout=timeout,
    )


def test_queue_wait_extends_the_writer_deadline(tmp_path: Path, worktree):
    """Non-vacuity: with the `deadline = t0 + limit + run.queued_s` update removed from
    `_drive`, this run times out at 1.5 s like the control below."""
    asked: list[int] = []

    def queued(attempt_id: int) -> float:
        asked.append(attempt_id)
        return 10.0  # the attempt spent 10 s queued for a local slot

    fake = FakeOpenCode(tmp_path / "oc", Script([sleep(3.0), say("done")]))
    result = drive(fake, worktree, timeout=1.5, extra_time=queued)
    assert result.status == "finished", result.error
    assert set(asked) == {7}


def test_without_queue_wait_the_same_run_times_out(tmp_path: Path, worktree):
    fake = FakeOpenCode(tmp_path / "oc", Script([sleep(3.0), say("done")]))
    result = drive(fake, worktree, timeout=1.5)
    assert result.status == "timed_out"
    assert "queued" not in (result.error or "")


def test_timeout_message_names_the_queue_time(tmp_path: Path, worktree):
    fake = FakeOpenCode(tmp_path / "oc", Script([sleep(30.0), say("done")]))
    result = drive(fake, worktree, timeout=0.5, extra_time=lambda _a: 2.0)
    assert result.status == "timed_out"
    assert "plus 2s queued for a local-model slot" in (result.error or "")


def test_broken_extra_time_adds_nothing(tmp_path: Path, worktree):
    def boom(_a: int) -> float:
        raise RuntimeError("proxy gone")

    fake = FakeOpenCode(tmp_path / "oc", Script([sleep(3.0), say("done")]))
    result = drive(fake, worktree, timeout=1.0, extra_time=boom)
    assert result.status == "timed_out"
