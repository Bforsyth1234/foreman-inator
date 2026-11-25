"""How much memory the Mac has free, and what Ollama holds, at an attempt's start and end
(todo #59, issue #36).

A slow or failed local attempt on a starved machine must not read as model quality, so
the attempt span carries the machine's memory state as span attributes (no schema change):

- ``resource.mem_used_bytes``: roughly Activity Monitor's "Memory Used": app memory
  (anonymous pages minus purgeable) + wired + compressor pages, from ``vm_stat``;
- ``resource.mem_total_bytes``: ``hw.memsize``;
- ``resource.mem_free_pct``: ``kern.memorystatus_level`` (the kernel's free-memory
  percentage, the number ``memory_pressure`` prints);
- ``resource.mem_pressure``: ``normal`` / ``warn`` / ``critical`` from
  ``kern.memorystatus_vm_pressure_level`` (1 / 2 / 4), else ``unknown``;
- ``resource.ollama_loaded``: JSON, the models Ollama holds (``resources.ollama``):
  ``[{"name", "size", "size_vram"}, ...]``, or ``{"unreachable": reason}``.

At the attempt's end the same keys are written again under ``resource.end.``
(``resource.end.mem_used_bytes`` ...). A value that could not be read is left out (or
``unknown`` for the pressure); reading never raises and never fails an attempt.

Sources checked 2026-11-25 on macOS 26 (Darwin 25.6): ``man vm_stat`` (header line
"page size of N bytes", one ``<name>: <count>.`` line per counter) and ``sysctl``.
``memory_pressure`` is not run: its man page documents it as a tool that *applies*
pressure; its ``-Q`` query mode is undocumented, and the sysctls give the same numbers.
On Linux (CI) the commands are missing and only the Ollama key is set.

Public API: ``read_memory(run=...) -> MemoryState``, ``snapshot(ollama_host, ...) ->
dict`` (the span attributes), and ``attempt_snapshots(span, ollama_host, snap=...)``, a
context manager that writes the start attributes on entry and the ``resource.end.`` ones
on exit (also when the attempt raises).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from foreman_inator.resources.ollama import Unreachable, loaded_models

log = logging.getLogger("foreman_inator.resources")

TIMEOUT_SECONDS = 2.0
PREFIX = "resource."
END_PREFIX = "resource.end."
_SYSCTLS = ("hw.memsize", "kern.memorystatus_vm_pressure_level", "kern.memorystatus_level")
_PRESSURE = {1: "normal", 2: "warn", 4: "critical"}

#: Runs a command and returns its stdout; raises (OSError, CalledProcessError,
#: TimeoutExpired, ...) when it cannot.
Runner = Callable[[Sequence[str]], str]


def _run(cmd: Sequence[str]) -> str:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, check=True, timeout=TIMEOUT_SECONDS
    ).stdout


@dataclass(frozen=True)
class MemoryState:
    used_bytes: int | None
    total_bytes: int | None
    free_pct: int | None
    pressure: str  # normal | warn | critical | unknown


def parse_vm_stat(text: str) -> int | None:
    """Used bytes from ``vm_stat`` output, or None when it cannot be read."""
    m = re.search(r"page size of (\d+) bytes", text)
    if not m:
        return None
    page = int(m.group(1))
    pages: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        value = value.strip().rstrip(".")
        if sep and value.isdigit():
            pages[name.strip().strip('"')] = int(value)
    wired = pages.get("Pages wired down")
    if wired is None:
        return None
    if "Anonymous pages" in pages:
        app = pages["Anonymous pages"] - pages.get("Pages purgeable", 0)
    else:  # older vm_stat without the anonymous counter
        app = pages.get("Pages active", 0)
    compressed = pages.get("Pages occupied by compressor", 0)
    return max(app, 0) * page + (wired + compressed) * page


def parse_sysctl(text: str) -> tuple[int | None, str, int | None]:
    """(total bytes, pressure, free %) from ``sysctl -n`` of ``_SYSCTLS``, in order."""
    lines = [ln.strip() for ln in text.splitlines()]
    nums = [int(ln) if ln.isdigit() else None for ln in lines] + [None] * len(_SYSCTLS)
    total, level, free = nums[: len(_SYSCTLS)]
    pressure = _PRESSURE.get(level, "unknown") if level is not None else "unknown"
    return total, pressure, free


def read_memory(run: Runner = _run) -> MemoryState:
    """The machine's memory now. Never raises; unreadable parts are None / ``unknown``."""
    try:
        used = parse_vm_stat(run(["vm_stat"]))
    except Exception as exc:  # noqa: BLE001 - a probe must never fail an attempt
        log.debug("vm_stat unavailable: %s", exc)
        used = None
    try:
        total, pressure, free = parse_sysctl(run(["sysctl", "-n", *_SYSCTLS]))
    except Exception as exc:  # noqa: BLE001
        log.debug("sysctl unavailable: %s", exc)
        total, pressure, free = None, "unknown", None
    return MemoryState(used, total, free, pressure)


def ollama_json(host: str | None, *, transport: httpx.BaseTransport | None = None) -> str:
    """``resource.ollama_loaded``: the loaded models as JSON, or why they are unknown."""
    if host is None:
        return json.dumps({"unreachable": "no local model configured"})
    try:
        got = loaded_models(host, transport=transport, timeout=TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        got = Unreachable(f"{exc.__class__.__name__}: {exc}")
    if isinstance(got, Unreachable):
        return json.dumps({"unreachable": got.reason})
    return json.dumps([{"name": m.name, "size": m.size, "size_vram": m.size_vram} for m in got])


def snapshot(
    ollama_host: str | None,
    *,
    run: Runner = _run,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """The span attributes (unprefixed keys: ``mem_used_bytes`` ...), unreadable ones left out."""
    mem = read_memory(run)
    attrs: dict[str, Any] = {
        "mem_used_bytes": mem.used_bytes,
        "mem_total_bytes": mem.total_bytes,
        "mem_free_pct": mem.free_pct,
        "mem_pressure": mem.pressure,
        "ollama_loaded": ollama_json(ollama_host, transport=transport),
    }
    return {k: v for k, v in attrs.items() if v is not None}


def local_ollama_host(endpoint: Any, models: Sequence[Any] = ()) -> str | None:
    """The Ollama to ask: the writer's own when it is local, else the first local model's."""
    if getattr(endpoint, "kind", None) == "local":
        return endpoint.base_url
    for m in models:
        if getattr(m, "kind", None) == "local":
            return m.base_url
    return None


def _write(span: Any, prefix: str, snap: Callable[[str | None], dict[str, Any]], host: Any):
    try:
        for key, value in snap(host).items():
            span.set_attribute(prefix + key, value)
    except Exception as exc:  # noqa: BLE001 - never fail an attempt over a probe
        log.debug("resource snapshot failed: %s", exc)


@contextmanager
def attempt_snapshots(
    span: Any,
    ollama_host: str | None,
    *,
    snap: Callable[[str | None], dict[str, Any]] = snapshot,
) -> Iterator[None]:
    """Write ``resource.*`` on entry and ``resource.end.*`` on exit (also on an exception)."""
    _write(span, PREFIX, snap, ollama_host)
    try:
        yield
    finally:
        _write(span, END_PREFIX, snap, ollama_host)
