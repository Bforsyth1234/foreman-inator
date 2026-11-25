"""The worker's daily prune (#56): ``PruneSteps`` is mixed into ``Worker``.

``maybe_prune()`` runs ``prune.run`` at most once per ``PRUNE_INTERVAL_SECONDS`` of the
worker's clock (``Worker.clock``, monotonic; tests pass a fake one). The first call
prunes at once, so it belongs after ``recover()`` has marked a dead process's attempts
superseded (until then they count as running and are kept). The ages themselves are
measured on the store's clock (``Store.now``). Retention comes from
``config.retention_days`` (default ``prune.RETENTION_DAYS``, 90). A failure is logged and
waits for the next interval; it never stops the poll loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from foreman_inator import paths, prune

log = logging.getLogger("foreman_inator.worker")

PRUNE_INTERVAL_SECONDS = 24 * 3600.0


class PruneSteps:
    # Provided by Worker.
    store: Any
    config: Any
    clock: Any
    recordings_dir: Path
    traces_dir: Path

    _last_prune_at: float | None = None

    def retention_days(self) -> float:
        return float(getattr(self.config, "retention_days", prune.RETENTION_DAYS))

    def maybe_prune(self, *, home: Path | None = None) -> prune.PruneReport | None:
        """Prune if a day has passed on the worker clock since the last try; else None."""
        now = self.clock()
        last = self._last_prune_at
        if last is not None and now - last < PRUNE_INTERVAL_SECONDS:
            return None
        self._last_prune_at = now
        try:
            report = prune.run(
                self.store,
                home if home is not None else paths.home(),
                days=self.retention_days(),
                repos=tuple(getattr(self.config, "repos", ()) or ()),
                recordings_dir=self.recordings_dir,
                traces_dir=self.traces_dir,
            )
        except Exception as exc:
            log.warning("prune failed: %s: %s", type(exc).__name__, exc)
            return None
        if report.removed:
            log.info("pruned %d path(s) older than %g days", len(report.removed), report.days)
        for err in report.errors:
            log.warning("prune: %s", err)
        return report


__all__ = ["PRUNE_INTERVAL_SECONDS", "PruneSteps"]
