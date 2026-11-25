"""resources.ollama: GET /api/ps through an httpx.MockTransport (no real Ollama)."""

from __future__ import annotations

from datetime import datetime

import httpx

from foreman_inator.resources import LoadedModel, OllamaWatch, Unreachable, loaded_models

PS = {
    "models": [
        {
            "name": "qwen3:8b",
            "model": "qwen3:8b",
            "size": 6591830464,
            "digest": "c6eb",
            "details": {"format": "gguf"},
            "expires_at": "2025-10-17T16:47:07.93355-07:00",
            "size_vram": 5333539264,
            "context_length": 4096,
        },
        {"name": "nomic-embed-text:latest", "size": 10, "size_vram": 0, "expires_at": "bad"},
    ]
}


def _transport(body=PS, status=200, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_parses_loaded_models_and_uses_host_of_base_url():
    seen: list[str] = []
    got = loaded_models("http://127.0.0.1:11434/v1", transport=_transport(seen=seen))
    assert seen == ["http://127.0.0.1:11434/api/ps"]
    assert isinstance(got, list)
    assert got[0] == LoadedModel(
        "qwen3:8b",
        6591830464,
        5333539264,
        datetime.fromisoformat("2025-10-17T16:47:07.93355-07:00"),
    )
    assert got[1].name == "nomic-embed-text:latest" and got[1].expires_at is None


def test_empty_list_is_zero_models_not_unreachable():
    assert loaded_models("http://h:1", transport=_transport(body={"models": []})) == []


def test_connection_error_is_unreachable_not_zero_models():
    def refuse(req):
        raise httpx.ConnectError("refused", request=req)

    got = loaded_models("http://127.0.0.1:11434", transport=httpx.MockTransport(refuse))
    assert isinstance(got, Unreachable)
    assert "127.0.0.1:11434" in got.reason and "ConnectError" in got.reason


def test_http_error_and_bad_shape_are_unreachable():
    assert isinstance(loaded_models("http://h:1", transport=_transport(status=500)), Unreachable)
    assert isinstance(loaded_models("http://h:1", transport=_transport(body={"x": 1})), Unreachable)
    bad = httpx.MockTransport(lambda req: httpx.Response(200, text="not json"))
    assert isinstance(loaded_models("http://h:1", transport=bad), Unreachable)


def test_watch_caches_for_ttl_then_refetches():
    seen: list[str] = []
    now = [100.0]
    w = OllamaWatch("http://h:1/v1", ttl=5.0, transport=_transport(seen=seen), clock=lambda: now[0])
    w.models()
    now[0] = 104.9
    w.is_loaded("qwen3:8b")
    assert len(seen) == 1
    now[0] = 105.0
    w.models()
    assert len(seen) == 2


def test_is_loaded_matches_name_and_untagged_latest():
    w = OllamaWatch("http://h:1", transport=_transport())
    assert w.is_loaded("qwen3:8b")
    assert w.is_loaded("nomic-embed-text")
    assert not w.is_loaded("qwen3")  # untagged means :latest, not any tag
    assert not w.is_loaded("llama3:70b")


def test_is_loaded_false_when_unreachable():
    w = OllamaWatch("http://h:1", transport=_transport(status=503))
    assert isinstance(w.models(), Unreachable)
    assert w.is_loaded("qwen3:8b") is False
