"""The machine's resources as a file the metrics page can read (todo #60, issue #37).

``fman metrics`` runs in its own process, so it cannot ask the worker's proxy how many
local-inference slots are in use. Instead the worker writes ``<home>/run/resources.json``
on every poll (``worker_resources.ResourceSteps``) and the metrics page reads it.

File shape (``VERSION`` 1)::

    {"version": 1,
     "written_at": 1764000000.0,                    # wall clock, seconds since the epoch
     "ollama": {"host": "http://127.0.0.1:11434",
                "models": [{"name", "size", "size_vram", "expires_at"}, ...]}
             | {"host": ..., "unreachable": "reason", "slow": false}
             | null,                                  # no local model configured
     "memory": {"used_bytes", "total_bytes", "free_pct", "pressure"},   # ints or null
     "local_slots": {"in_use": 0, "cap": 1} | null}   # null: no proxy to ask

Sources: ``resources.ollama.loaded_models`` (Ollama ``GET /api/ps``, docs checked
2026-11-25 at https://docs.ollama.com/api/ps), ``resources.memory.read_memory`` (``vm_stat``
and ``sysctl``, checked 2026-11-25) and ``ModelProxy.local_slots()``.

The write is atomic (a temp file in the same folder, mode 600, then ``os.replace``), so a
reader never sees half a file. ``load(path)`` never raises: a missing or unreadable file
is a ``Reading`` with ``error`` set. ``Reading.stale`` is true once the snapshot is older
than ``STALE_AFTER_SECONDS`` (5 minutes): the worker is probably not running.

Public API: ``build(...)``, ``write(path, data)``, ``load(path, now=)``, ``default_path()``,
``Reading``, ``STALE_AFTER_SECONDS``, ``RESOURCES_NAME``, ``VERSION``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foreman_inator import paths
from foreman_inator.resources.memory import MemoryState, read_memory
from foreman_inator.resources.ollama import TIMEOUT_SECONDS, Unreachable, loaded_models
from foreman_inator.setup.deps import ollama_host as reduce_host

VERSION = 1
RESOURCES_NAME = "resources.json"
STALE_AFTER_SECONDS = 300.0

#: The wall clock ``load`` measures ages on (tests replace it).
clock: Callable[[], float] = time.time

OllamaReader = Callable[[str], Any]  # host -> list[LoadedModel] | Unreachable


def default_path() -> Path:
    """``<home>/run/resources.json`` (the folder is created, mode 700)."""
    return paths.run_dir() / RESOURCES_NAME


def ask_ollama(host: str) -> Any:
    return loaded_models(host, timeout=TIMEOUT_SECONDS)


def _ollama_part(host: str | None, ask: OllamaReader) -> dict[str, Any] | None:
    if host is None:
        return None
    base = reduce_host(host)
    try:
        got = ask(host)
    except Exception as exc:  # noqa: BLE001 - a probe never stops the poll loop
        got = Unreachable(f"{exc.__class__.__name__}: {exc}")
    if isinstance(got, Unreachable):
        return {"host": base, "unreachable": got.reason, "slow": bool(got.slow)}
    return {
        "host": base,
        "models": [
            {
                "name": m.name,
                "size": int(m.size),
                "size_vram": int(m.size_vram),
                "expires_at": m.expires_at.isoformat() if m.expires_at else None,
            }
            for m in got
        ],
    }


def build(
    ollama_host: str | None,
    local_slots: tuple[int, int] | None,
    *,
    memory: Callable[[], MemoryState] = read_memory,
    ollama: OllamaReader = ask_ollama,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """The snapshot dict (see the module docstring). Never raises for a failed probe."""
    try:
        mem = memory()
        mem_part: dict[str, Any] = {
            "used_bytes": mem.used_bytes,
            "total_bytes": mem.total_bytes,
            "free_pct": mem.free_pct,
            "pressure": mem.pressure,
        }
    except Exception:  # noqa: BLE001
        mem_part = {"used_bytes": None, "total_bytes": None, "free_pct": None}
        mem_part["pressure"] = "unknown"
    slots = None
    if local_slots is not None:
        in_use, cap = local_slots
        slots = {"in_use": int(in_use), "cap": int(cap)}
    return {
        "version": VERSION,
        "written_at": float(now()),
        "ollama": _ollama_part(ollama_host, ollama),
        "memory": mem_part,
        "local_slots": slots,
    }


def write(path: str | Path, data: dict[str, Any]) -> Path:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``), mode 600."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".resources-", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, paths.SECRET_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


@dataclass(frozen=True)
class Reading:
    """What ``load`` found. ``data`` is None (and ``error`` set) when there is no usable
    snapshot; ``age_seconds`` is how long ago the worker wrote it."""

    data: dict[str, Any] | None
    age_seconds: float | None
    error: str | None = None

    @property
    def stale(self) -> bool:
        return self.age_seconds is not None and self.age_seconds > STALE_AFTER_SECONDS


def load(path: str | Path, *, now: Callable[[], float] | None = None) -> Reading:
    """Read the snapshot at ``path``; its age is measured on ``now`` (default ``clock``).
    Never raises."""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Reading(None, None, "no resource snapshot yet: the worker writes one each poll")
    except OSError as exc:
        return Reading(None, None, f"cannot read the resource snapshot ({exc.__class__.__name__})")
    try:
        data = json.loads(raw)
    except ValueError:
        return Reading(None, None, "the resource snapshot is not valid JSON")
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return Reading(None, None, "the resource snapshot has an unknown format")
    written = data.get("written_at")
    if not isinstance(written, int | float) or isinstance(written, bool):
        return Reading(None, None, "the resource snapshot has no time")
    current = (now or clock)()
    return Reading(data, max(0.0, float(current) - float(written)))


__all__ = [
    "RESOURCES_NAME",
    "STALE_AFTER_SECONDS",
    "VERSION",
    "Reading",
    "build",
    "default_path",
    "ask_ollama",
    "load",
    "write",
]
