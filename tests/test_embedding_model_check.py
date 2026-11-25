"""#51: ``fman setup`` checks the embedding model is pulled, and a 404 from the
embeddings call names the model and the pull command.

Ollama is an httpx.MockTransport for the setup check; the backfill goes through a real
``ModelProxy`` on a loopback socket to ``FakeModels`` (as in test_knn.py).
"""

from __future__ import annotations

import dataclasses
import logging
import secrets

import httpx
import pytest

from fakes.models import FakeModels
from foreman_inator import config
from foreman_inator.db.store import Store
from foreman_inator.proxy.server import ModelProxy
from foreman_inator.routing import knn
from foreman_inator.setup import deps
from foreman_inator.worker_background import BackgroundSteps

EMB = knn.embedding_endpoint()  # embeddinggemma on local Ollama
# Ollama's answer for a model that is not pulled (seen in the live run of 2026-11-24).
NOT_PULLED = {
    "error": {
        "message": 'model "embeddinggemma" not found, try pulling it first',
        "type": "api_error",
        "param": None,
        "code": None,
    }
}


def ollama(pulled: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": m, "model": m} for m in pulled]})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- setup check


def test_missing_embedding_model_is_reported_with_the_pull_command():
    checks = deps.check_models(
        config.Config(), transport=ollama(["inkling-2-mini:latest", "qwen3.8:27b"])
    )
    by = {c.name: c for c in checks}
    emb = by["embedding model: embeddinggemma"]
    assert not emb.ok and emb.detail == "not pulled"
    assert emb.fix == "ollama pull embeddinggemma"
    assert "run: ollama pull embeddinggemma" in deps.format_report(checks)
    assert by["model tier 1: inkling-2-mini"].ok and by["model tier 2: qwen3.8:27b"].ok


def test_pulled_embedding_model_is_ok_and_tags_read_once():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        names = ["inkling-2-mini:latest", "qwen3.8:27b", "embeddinggemma:latest"]
        return httpx.Response(200, json={"models": [{"name": m} for m in names]})

    checks = deps.check_models(config.Config(), transport=httpx.MockTransport(handler))
    assert all(c.ok for c in checks)
    assert "embedding model: embeddinggemma" in {c.name for c in checks}
    assert len(seen) == 1  # one host, one /api/tags read


def test_check_all_includes_the_embedding_model():
    checks = deps.check_all(
        config.Config(),
        which=lambda name: None,
        transport=ollama(["inkling-2-mini:latest", "qwen3.8:27b"]),
    )
    assert any(c.name == "embedding model: embeddinggemma" and not c.ok for c in checks)


def test_cloud_embedding_model_is_not_checked_in_ollama():
    cfg = dataclasses.replace(
        config.Config(),
        embedding_base_url="https://api.example.com/v1",
        allow_cloud_embedder=True,
        embedding_api_key_ref="keychain:foreman-inator/embed",
    )
    checks = deps.check_models(cfg, transport=ollama(["inkling-2-mini:latest", "qwen3.8:27b"]))
    assert not any(c.name.startswith("embedding model") for c in checks)


def test_unreachable_ollama_embedding_check_says_start_then_pull():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    checks = deps.check_models(config.Config(), transport=httpx.MockTransport(down))
    emb = next(c for c in checks if c.name.startswith("embedding model"))
    assert not emb.ok and "not reachable" in emb.detail
    assert emb.fix.endswith("ollama pull embeddinggemma")


# --------------------------------------------------------------------------- backfill 404


@pytest.fixture
def fake() -> FakeModels:
    return FakeModels()


@pytest.fixture
def proxy(fake):
    key = "fake-" + secrets.token_hex(8)
    p = ModelProxy(
        [*config.Config().models, EMB], transport=fake.transport, key_resolver=lambda e: key
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


def accepted_ticket(store, n: int):
    t = store.create_ticket(
        repo="me/toy", issue_number=n, title=f"Fix {n}", request="typo", base_branch="main"
    )
    a = store.add_attempt(t.id, model="m1", tier=1, trigger="first")
    store.finish_attempt(a.id, None)
    store.set_accepted_attempt(t.id, a.id)
    return t


def test_backfill_404_names_the_model_and_the_pull_command(store, proxy, fake):
    accepted_ticket(store, 1)
    accepted_ticket(store, 2)
    fake.fail_next(404, NOT_PULLED)
    r = knn.backfill(store, proxy=proxy, embedder=EMB)
    assert r.embedded == () and r.remaining == 2
    assert "embeddinggemma" in r.error
    assert "ollama pull embeddinggemma" in r.error


def test_other_http_errors_keep_the_bare_status(store, proxy, fake):
    accepted_ticket(store, 1)
    fake.fail_next(500)
    r = knn.backfill(store, proxy=proxy, embedder=EMB)
    assert r.error == "HTTP 500"


def test_a_404_the_proxy_answers_itself_is_not_called_a_missing_model(store, proxy, fake):
    accepted_ticket(store, 1)
    fake.fail_next(404, {"error": {"message": "gone", "code": "unknown_context"}})
    r = knn.backfill(store, proxy=proxy, embedder=EMB)
    assert r.error == "HTTP 404"


def test_worker_backfill_warning_names_the_model(store, proxy, fake, caplog):
    accepted_ticket(store, 1)
    fake.fail_next(404, NOT_PULLED)

    class Steps(BackgroundSteps):
        def __init__(self) -> None:
            self.store, self.proxy, self.embedder = store, proxy, EMB
            self.config = config.Config()
            self.clock = lambda: 0.0
            self._backfill_delay = self._backfill_after = 0.0

    with caplog.at_level(logging.WARNING, logger="foreman_inator.worker"):
        Steps()._backfill_once()
    warning = [r.getMessage() for r in caplog.records if "knn backfill failing" in r.getMessage()]
    assert len(warning) == 1
    assert "ollama pull embeddinggemma" in warning[0]
