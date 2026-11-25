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


# --------------------------------------------------------------------------- who queues


@pytest.mark.anyio
async def test_one_shot_calls_do_not_queue_behind_a_writer():
    """Router / clarify / review contexts (registered, no writer run) and bare /v1 calls
    skip the slot: their own short deadline would run out behind a writer stream.
    Non-vacuity: with the `ctx.slot_key is not None` condition dropped from `_handle`,
    neither call reaches Ollama while the writer holds the slot."""
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        writer = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("qwen-small")))
        assert await arrival(gate) == "qwen3.6:35b-a3b"
        with proxy.context(label="route") as ctx:
            path = ctx.base_url.removeprefix(proxy.url) + "/chat/completions"
            routed = asyncio.create_task(c.post(path, json=chat("gemma")))
            assert await arrival(gate) == "gemma4:27b"  # sent while the writer holds it
            bare = asyncio.create_task(c.post("/v1/chat/completions", json=chat("gemma")))
            assert await arrival(gate) == "gemma4:27b"
            assert proxy.local_slots() == (1, 1) and proxy.slots.waiting == 0
            for _ in range(3):
                gate.open_one()
            for t in (writer, routed, bare):
                assert (await t).status_code == 200
    assert proxy.local_slots() == (0, 1)


@pytest.mark.anyio
async def test_a_registered_writer_run_queues_and_its_wait_is_kept():
    """A replay's writer context (`writer_run=`) queues like `/a/<id>` does. Non-vacuity:
    with `slot_key` returning None for registered contexts, the replay call is sent at
    once and `gate.arrived.empty()` fails."""
    gate = Gated(FakeModels())
    proxy = ModelProxy(MODELS, transport=gate)
    async with client_for(proxy) as c:
        holder = asyncio.create_task(c.post("/a/1/v1/chat/completions", json=chat("qwen-small")))
        await arrival(gate)
        with proxy.context(bucket="experiments", label="replay", writer_run=-5) as ctx:
            path = ctx.base_url.removeprefix(proxy.url) + "/chat/completions"
            replay = asyncio.create_task(c.post(path, json=chat("gemma")))
            await asyncio.sleep(0.3)
            assert gate.arrived.empty()
            assert proxy.queue_wait(-5) >= 0.2
            gate.open_one()
            assert (await holder).status_code == 200
            await arrival(gate)
            gate.open_one()
            assert (await replay).status_code == 200
    proxy.forget_queue_wait(-5)
    assert proxy.queue_wait(-5) == 0


# --------------------------------------------------------------------------- cross-process


@pytest.mark.anyio
async def test_file_slots_on_one_dir_share_the_cap(tmp_path: Path):
    """Two `LocalSlots` on the same lock dir (as the worker and `fman experiment run`
    are): with cap 1 the second acquire blocks until the first releases. Non-vacuity:
    with the `lock_dir` branch of `acquire` removed (each instance its own in-process
    cap), `b.acquire()` returns at once and `assert not second.done()` fails."""
    a = LocalSlots(1, lock_dir=tmp_path / "run")
    b = LocalSlots(1, lock_dir=tmp_path / "run")
    await a.acquire()
    second = asyncio.create_task(b.acquire())
    await asyncio.sleep(0.3)
    assert not second.done()
    assert b.waiting == 1 and a.in_use == 1 and b.in_use == 0
    a.release()
    await asyncio.wait_for(second, 2)
    assert b.in_use == 1 and a.in_use == 0 and b.waiting == 0
    b.release()
    assert (tmp_path / "run" / "local-slot-0.lock").exists()
    await asyncio.wait_for(a.acquire(), 1)  # free again
    a.release()


@pytest.mark.anyio
async def test_file_slots_honour_a_cap_above_one_and_a_cancel(tmp_path: Path):
    a = LocalSlots(2, lock_dir=tmp_path)
    b = LocalSlots(2, lock_dir=tmp_path)
    await a.acquire()
    await asyncio.wait_for(b.acquire(), 1)  # the second file
    third = asyncio.create_task(a.acquire())
    await until(lambda: a.waiting == 1)
    third.cancel()
    with pytest.raises(asyncio.CancelledError):
        await third
    assert (a.in_use, a.waiting, b.in_use) == (1, 0, 1)
    a.release()
    b.release()
    a.release()  # nothing held: a no-op
    assert a.in_use == b.in_use == 0


def test_file_slots_are_held_across_processes_and_dropped_when_one_dies(tmp_path: Path):
    """A real second process holds slot 0; this process waits, then gets it once that
    process exits (the OS drops its flock, so a crash never leaks a slot)."""
    import subprocess
    import sys
    import time

    code = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(float(sys.argv[2]))\n"
    )
    tmp_path.mkdir(exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path / "local-slot-0.lock"), "0.8"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        slots = LocalSlots(1, lock_dir=tmp_path)
        t0 = time.monotonic()
        asyncio.run(asyncio.wait_for(slots.acquire(), 5))
        assert time.monotonic() - t0 >= 0.3  # waited for the other process
        assert slots.in_use == 1
        slots.release()
    finally:
        child.kill()
        child.wait()
