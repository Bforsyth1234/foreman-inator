"""Which models Ollama has loaded in memory right now (todo #58, issue #35).

Adapter over Ollama's ``GET /api/ps``. Docs checked 2026-11-25:
https://docs.ollama.com/api/ps . The shape relied on is::

    {"models": [{"name": str, "model": str, "size": int, "size_vram": int,
                 "expires_at": str (ISO 8601), ...}]}

Other fields (``digest``, ``details``, ``context_length``) are ignored. If Ollama
changes this shape, only ``_parse`` here needs to change.

Public API (later waves read this from the worker status and the metrics view):

- ``loaded_models(host, *, transport=None)`` -> ``list[LoadedModel] | Unreachable``.
  One HTTP call, no caching. An unreachable or broken Ollama is an ``Unreachable``
  with a reason, never an empty list, so "nothing loaded" and "can't tell" differ.
- ``OllamaWatch(host, ttl=DEFAULT_TTL_SECONDS)``: the same answer cached for ``ttl``
  seconds. ``.models()`` returns it; ``.is_loaded(model)`` says whether ``model`` is
  loaded (False when Ollama is unreachable; check ``.models()`` to tell them apart).

``host`` may be a tier's ``base_url`` (``http://127.0.0.1:11434/v1``) or a bare
host; it is reduced with ``setup.deps.ollama_host``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from foreman_inator.setup.deps import ollama_host

DEFAULT_TTL_SECONDS = 5.0
TIMEOUT_SECONDS = 2.0
#: ``fman status``'s bound: a busy Ollama (a model loading, a long generation) can take
#: longer than 2 s to answer /api/ps, and that is "slow", not "unreachable".
STATUS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class LoadedModel:
    """One model Ollama holds in memory."""

    name: str
    size: int  # bytes, whole model
    size_vram: int  # bytes of ``size`` held in GPU memory
    expires_at: datetime | None  # when Ollama unloads it; None if missing or unparsable


@dataclass(frozen=True)
class Unreachable:
    """Ollama could not be asked, or its answer could not be read. ``slow``: it took the
    connection but sent no reply within the timeout (busy, not down)."""

    reason: str
    slow: bool = False


def _parse(data: object) -> list[LoadedModel]:
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("no 'models' list in /api/ps response")
    out: list[LoadedModel] = []
    for entry in data["models"]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if not name:
            continue
        expires: datetime | None = None
        raw = entry.get("expires_at")
        if isinstance(raw, str):
            try:
                expires = datetime.fromisoformat(raw)
            except ValueError:
                expires = None
        out.append(
            LoadedModel(
                name=str(name),
                size=int(entry.get("size") or 0),
                size_vram=int(entry.get("size_vram") or 0),
                expires_at=expires,
            )
        )
    return out


def loaded_models(
    host: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> list[LoadedModel] | Unreachable:
    """Ask Ollama at ``host`` which models are loaded. Never raises for network errors."""
    base = ollama_host(host)
    try:
        with httpx.Client(transport=transport, timeout=timeout) as http:
            resp = http.get(f"{base}/api/ps")
            resp.raise_for_status()
            return _parse(resp.json())
    except httpx.HTTPStatusError as exc:
        return Unreachable(f"Ollama at {base} answered HTTP {exc.response.status_code}")
    except httpx.ReadTimeout:
        return Unreachable(f"no reply from {base} in {timeout:g}s", slow=True)
    except httpx.HTTPError as exc:
        return Unreachable(f"Ollama not reachable at {base} ({exc.__class__.__name__})")
    except (ValueError, TypeError) as exc:
        return Unreachable(f"Ollama at {base} sent an unreadable /api/ps answer ({exc})")


def _same_model(wanted: str, loaded: str) -> bool:
    # Ollama names an untagged model "<name>:latest" (as setup.deps._matches does).
    if wanted == loaded:
        return True
    return ":" not in wanted and loaded == f"{wanted}:latest"


class OllamaWatch:
    """``loaded_models`` for one host, cached for ``ttl`` seconds."""

    def __init__(
        self,
        host: str,
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = ollama_host(host)
        self.ttl = ttl
        self._transport = transport
        self._clock = clock
        self._cached: list[LoadedModel] | Unreachable | None = None
        self._at = 0.0

    def models(self) -> list[LoadedModel] | Unreachable:
        """The loaded models (or ``Unreachable``), fetched at most once per ``ttl``."""
        now = self._clock()
        if self._cached is None or now - self._at >= self.ttl:
            self._cached = loaded_models(self.host, transport=self._transport)
            self._at = now
        return self._cached

    def is_loaded(self, model: str) -> bool:
        """True when ``model`` is loaded. False when not loaded or Ollama is unreachable."""
        current = self.models()
        if isinstance(current, Unreachable):
            return False
        return any(_same_model(model, m.name) for m in current)
