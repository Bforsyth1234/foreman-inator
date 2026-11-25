"""The worker's daily post-merge quality scan (#24): ``QualitySteps`` is mixed into ``Worker``.

``maybe_scan_quality()`` runs ``metrics.quality.scan`` at most once per
``QUALITY_INTERVAL_SECONDS`` of the worker's clock (``Worker.clock``, monotonic; tests pass
a fake one). The first call scans at once. Ages (the 14-day window, the 7-day follow-up
window) are measured on the store's clock. The scan only reads GitHub and writes
``post_merge_signals``; a failure is logged and waits for the next interval, it never stops
the poll loop.
"""

from __future__ import annotations

import logging
from typing import Any

from foreman_inator.metrics import quality

log = logging.getLogger("foreman_inator.worker")

QUALITY_INTERVAL_SECONDS = 24 * 3600.0


class QualitySteps:
    # Provided by Worker.
    store: Any
    api: Any
    clock: Any

    _last_quality_scan_at: float | None = None

    def maybe_scan_quality(
        self,
        *,
        window_days: float = quality.SCAN_WINDOW_DAYS,
        followup_days: float = quality.FOLLOWUP_DAYS,
    ) -> quality.ScanReport | None:
        """Scan if a day has passed on the worker clock since the last try; else None."""
        now = self.clock()
        last = self._last_quality_scan_at
        if last is not None and now - last < QUALITY_INTERVAL_SECONDS:
            return None
        self._last_quality_scan_at = now
        try:
            report = quality.scan(
                self.store, self.api, window_days=window_days, followup_days=followup_days
            )
        except Exception as exc:
            log.warning("post-merge scan failed: %s: %s", type(exc).__name__, exc)
            return None
        for ticket_id, kind in report.recorded:
            log.info("ticket %d: %s", ticket_id, quality.LABELS.get(kind, kind))
        for err in report.errors:
            log.warning("post-merge scan: %s", err)
        return report


__all__ = ["QUALITY_INTERVAL_SECONDS", "QualitySteps"]
