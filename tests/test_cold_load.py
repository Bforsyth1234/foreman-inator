"""Issue #52: the clarity check (and the router) wait for a cold model load.

A fake model server whose first reply is slow stands in for Ollama loading an unloaded
classifier; a fake ``/api/ps`` (through a real ``resources.ollama.OllamaWatch``) says
whether the model is loaded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets

import httpx
import pytest

from fakes.models import FakeModels
from foreman_inator.config import Config
from foreman_inator.proxy.server import ModelProxy
from foreman_inator.resources.ollama import OllamaWatch
from foreman_inator.routing import rules
from foreman_inator.routing.rules import load_state, route
from foreman_inator.tickets.clarify import Clear, NeedsInfo, assess

CONFIG = Config()
TIER1 = CONFIG.models_for_tier(1)[0]
CLOUD = CONFIG.models_for_tier(3)[0]
TIMEOUT = 0.3
SLOW = 1.0  # the cold first reply: longer than TIMEOUT, shorter than TIMEOUT + ALLOWANCE
ALLOWANCE = 4.0


class FakeTicket:
    def __init__(self, title="Fix it", request="It's broken.", repo="me/toy", id=4):
        self.id, self.title, self.request, self.repo = id, title, request, repo


class FakePs:
    """``/api/ps``: ``states`` is consumed one answer per call (the last one repeats).
    Each is "loaded", "cold" (Ollama up, model absent) or "down" (HTTP 500)."""

    def __init__(self, *states: str) -> None:
        self.states = list(states)
        self.calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        if state == "down":
            return httpx.Response(500)
        models = [{"name": TIER1.model, "size": 1, "size_vram": 1}] if state == "loaded" else []
        return httpx.Response(200, json={"models": models})

    def watch(self) -> OllamaWatch:
        return OllamaWatch(TIER1.base_url, ttl=0, transport=httpx.MockTransport(self.handle))


class ColdModel:
    """The fake model server; the first ``slow_replies`` chat replies take ``SLOW`` s."""

    def __init__(self, slow_replies: int = 1, delay: float = SLOW) -> None:
        self.fake = FakeModels()
        self.slow_left = slow_replies
        self.delay = delay
        self.started = 0  # chat calls received (a timed-out one never finishes in time)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            self.started += 1
            if self.slow_left > 0:
                self.slow_left -= 1
                await asyncio.sleep(self.delay)
        return await self.fake.handle(request)

    def chats(self) -> int:
        return self.started


@pytest.fixture
def cold():
    model = ColdModel()
    key = "fake-" + secrets.token_hex(8)  # generated at test time (invariant 7d)
    p = ModelProxy(CONFIG, transport=httpx.MockTransport(model.handle), key_resolver=lambda e: key)
    p.start()
    try:
        yield model, p
    finally:
        p.stop()


def _questions(model: ColdModel, n: int = 1) -> None:
    for _ in range(n):
        model.fake.queue_reply(json.dumps({"questions": ["Which page fails?"]}))


def test_unloaded_classifier_still_gets_a_real_verdict(cold, caplog):
    model, p = cold
    _questions(model)
    caplog.set_level(logging.INFO, logger="foreman_inator")
    r = assess(
        FakeTicket(),
        config=CONFIG,
        proxy=p,
        timeout=TIMEOUT,
        watch=FakePs("cold").watch(),
        cold_load_allowance=ALLOWANCE,
    )
    assert isinstance(r, NeedsInfo), r
    assert r.questions == ("Which page fails?",)
    assert f"waiting for cold load of {TIER1.model}" in caplog.text
    assert "clarify fallback" not in caplog.text
    assert model.chats() == 1


def test_without_a_watch_the_cold_load_times_out(cold):
    """The bug as seen (issue #52): no load check, the slow first reply is a fallback."""
    model, p = cold
    _questions(model)
    r = assess(FakeTicket(), config=CONFIG, proxy=p, timeout=TIMEOUT)
    assert isinstance(r, Clear) and r.is_fallback and "timed out" in r.reason


def test_loaded_model_gets_no_allowance_and_no_retry(cold, caplog):
    model, p = cold
    _questions(model, 2)
    caplog.set_level(logging.INFO, logger="foreman_inator")
    r = assess(
        FakeTicket(),
        config=CONFIG,
        proxy=p,
        timeout=TIMEOUT,
        watch=FakePs("loaded").watch(),
        cold_load_allowance=ALLOWANCE,
    )
    assert isinstance(r, Clear) and r.is_fallback and "timed out" in r.reason
    assert "cold load" not in caplog.text
    assert model.chats() == 1


def test_timeout_then_now_loaded_retries_once(cold, caplog):
    """Ollama could not be asked before the call; after the timeout it reports the model
    loaded, so the timeout was the load: one retry gets the verdict."""
    model, p = cold
    _questions(model, 2)
    caplog.set_level(logging.INFO, logger="foreman_inator")
    ps = FakePs("down", "loaded")
    r = assess(
        FakeTicket(),
        config=CONFIG,
        proxy=p,
        timeout=TIMEOUT,
        watch=ps.watch(),
        cold_load_allowance=ALLOWANCE,
    )
    assert isinstance(r, NeedsInfo), r
    assert model.chats() == 2
    assert f"waiting for cold load of {TIER1.model} once more" in caplog.text


def test_retry_is_capped_at_one():
    model = ColdModel(slow_replies=5, delay=2.0)  # every reply outlasts both budgets
    _questions(model, 5)
    key = "fake-" + secrets.token_hex(8)
    # Two local slots: the abandoned first call still holds one until its slow reply ends
    # (#34), and this test counts calls that reach the model, not queueing.
    p = ModelProxy(
        CONFIG,
        transport=httpx.MockTransport(model.handle),
        key_resolver=lambda e: key,
        max_local_inference=2,
    )
    p.start()
    try:
        r = assess(
            FakeTicket(),
            config=CONFIG,
            proxy=p,
            timeout=0.4,
            watch=FakePs("cold", "loaded").watch(),
            cold_load_allowance=0.4,
        )
    finally:
        p.stop()
    assert isinstance(r, Clear) and r.is_fallback and "timed out" in r.reason
    assert model.chats() == 2  # the first call and one retry, never more (7e)


def test_unreachable_ollama_gives_plain_timeout(cold):
    model, p = cold
    _questions(model, 2)
    r = assess(
        FakeTicket(),
        config=CONFIG,
        proxy=p,
        timeout=TIMEOUT,
        watch=FakePs("down").watch(),
        cold_load_allowance=ALLOWANCE,
    )
    assert isinstance(r, Clear) and r.is_fallback and "timed out" in r.reason
    assert model.chats() == 1


def test_router_waits_for_cold_load_too(cold, caplog):
    model, p = cold
    model.fake.queue_reply("small")
    caplog.set_level(logging.INFO, logger="foreman_inator")
    r = route(
        FakeTicket(),
        config=CONFIG,
        proxy=p,
        timeout=TIMEOUT,
        watch=FakePs("cold").watch(),
        cold_load_allowance=ALLOWANCE,
    )
    assert (r.source, r.label) == ("classifier", "small"), r
    assert f"waiting for cold load of {TIER1.model}" in caplog.text


def test_load_state():
    assert load_state(None, TIER1) == "unknown"
    assert load_state(FakePs("cold").watch(), TIER1) == "cold"
    assert load_state(FakePs("loaded").watch(), TIER1) == "loaded"
    assert load_state(FakePs("down").watch(), TIER1) == "unknown"
    ps = FakePs("cold")
    assert load_state(ps.watch(), CLOUD) == "unknown" and ps.calls == 0  # not Ollama's

    class Broken:
        def models(self):
            raise RuntimeError("boom")

    assert load_state(Broken(), TIER1) == "unknown"


def test_post_classifier_budgets():
    seen: list[float] = []

    def post(url, body, headers, timeout):
        seen.append(timeout)
        raise rules.RouterTimeout("timed out")

    with pytest.raises(rules.RouterTimeout):
        rules.post_classifier(
            "u",
            {},
            {},
            classifier=TIER1,
            timeout=1.0,
            watch=FakePs("cold", "cold").watch(),
            cold_load_allowance=9.0,
            post=post,
        )
    assert seen == [10.0]  # cold: the allowance; still not loaded after: no retry
