"""The worker's resource snapshot (todo #60, issue #37): ``ResourceSteps`` is mixed into
``Worker``.

``write_resources()`` writes ``<home>/run/resources.json`` (``resources.snapshot``): the
models Ollama holds, the machine's memory and the proxy's ``local_slots()`` (in use, cap).
``fman metrics`` runs in another process and reads that file for its ``/resources`` panel
and the block on the main page. Call it once per poll. It never raises: a failure is
logged at debug level and the next poll tries again.

The Ollama asked is the first local model's (``worker_status.local_ollama_host``); with
only cloud models there is none and ``ollama`` is null. Tests replace
``resources_memory`` / ``resources_ollama`` (and pass ``path=``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from foreman_inator.resources import snapshot
from foreman_inator.resources.memory import read_memory
from foreman_inator.worker_status import local_ollama_host

log = logging.getLogger("foreman_inator.worker")


class ResourceSteps:
    # Provided by Worker.
    config: Any
    proxy: Any

    resources_memory = staticmethod(read_memory)
    resources_ollama = staticmethod(snapshot.ask_ollama)
    #: Where the snapshot goes; None means ``snapshot.default_path()``.
    resources_path: Path | None = None

    def _resource_slots(self) -> tuple[int, int] | None:
        fn = getattr(getattr(self, "proxy", None), "local_slots", None)
        if fn is None:
            return None
        try:
            in_use, cap = fn()
            return int(in_use), int(cap)
        except Exception as exc:  # noqa: BLE001
            log.debug("local_slots unavailable: %s", exc)
            return None

    def write_resources(self, *, path: Path | None = None) -> Path | None:
        """Write the snapshot; return its path, or None when the write failed."""
        try:
            cfg = getattr(self, "config", None)
            host = local_ollama_host(cfg) if cfg is not None else None
            data = snapshot.build(
                host,
                self._resource_slots(),
                memory=self.resources_memory,
                ollama=self.resources_ollama,
            )
            target = path or self.resources_path or snapshot.default_path()
            return snapshot.write(target, data)
        except Exception as exc:  # noqa: BLE001 - never stop the poll loop
            log.debug("resource snapshot not written: %s: %s", type(exc).__name__, exc)
            return None


__all__ = ["ResourceSteps"]
