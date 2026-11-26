"""z.ai GLM Coding Plan quota errors (#85): the one place that knows their shape.

Source: https://docs.z.ai/api-reference/api-code (read 2026-11-25). A plan call past
its quota gets HTTP 429 with ``{"error": {"code": "1308", "message": "..."}}`` (the code
is a string; an int is accepted too):

* 1308: "Usage limit reached for {number} {unit}. Your limit will reset at
  {next_flush_time}" (the 5-hour window).
* 1310: "Weekly/Monthly Limit Exhausted. Your limit will reset at {next_flush_time}".
* 1309: the plan expired; it works again only after the user renews it.
* 1316-1321: other limit-reached cases ("... for the past 5 hours" / "... past 7 days
  ... Resets at {next_flush_time}").

The docs give ``{next_flush_time}`` only as a placeholder: no example, format or zone.
``reset_time`` therefore accepts ``YYYY-MM-DD HH:MM[:SS]`` (with ``T``, an optional
fraction and an optional ``Z`` / ``+HH:MM`` zone; no zone is taken as UTC+8, z.ai's
own time zone for its peak hours) or a Unix timestamp in seconds or milliseconds. A
time it cannot read gives None. ``plausible_reset`` (applied by ``plan_quota``, on the
worker's clock) also drops one in the past or more than 8 days away; the caller then
retries after ``FALLBACK_RETRY``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

QuotaKind = Literal["five_hour", "weekly", "expired"]

#: z.ai's own zone, assumed for a reset time given without one.
ZAI_TZ = timezone(timedelta(hours=8))
#: When the reset time is missing or unreadable, try again after this long.
FALLBACK_RETRY = timedelta(hours=1)
_MAX_AHEAD = timedelta(days=8)

QUOTA_CODES: dict[int, QuotaKind] = {
    1308: "five_hour",
    1309: "expired",
    1310: "weekly",
    1316: "five_hour",
    1317: "weekly",
    1318: "five_hour",
    1319: "weekly",
    1320: "five_hour",
    1321: "weekly",
}

_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"\s*(Z|UTC|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)
_EPOCH = re.compile(r"\b(\d{10}|\d{13})\b")


@dataclass(frozen=True)
class QuotaHit:
    """A plan call refused by z.ai for quota."""

    code: int
    kind: QuotaKind
    message: str
    resets_at: datetime | None  # UTC; None: expired, or the time could not be read


def parse_quota_error(status: int, data: bytes | str | Any) -> QuotaHit | None:
    """The quota hit in an upstream error response, or None for any other error."""
    if status != 429:
        return None
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None
    try:
        code = int(err.get("code"))
    except (TypeError, ValueError):
        return None
    kind = QUOTA_CODES.get(code)
    if kind is None:
        return None
    message = str(err.get("message") or "")[:500]
    resets = None if kind == "expired" else reset_time(message)
    return QuotaHit(code, kind, message, resets)


def reset_time(message: str) -> datetime | None:
    """The reset instant (UTC) named in a quota message, or None (see the module doc)."""
    found: datetime | None = None
    if m := _DATETIME.search(message):
        y, mo, d, h, mi, s, zone = m.groups()
        try:
            found = datetime(
                int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=_zone(zone)
            )
        except ValueError:
            found = None
    elif m := _EPOCH.search(message):
        n = int(m.group(1))
        found = datetime.fromtimestamp(n / 1000 if n > 10**11 else n, tz=UTC)
    if found is None:
        return None
    return found.astimezone(UTC)


def plausible_reset(resets_at: datetime | None, now: datetime) -> datetime | None:
    """``resets_at`` if it is after ``now`` and at most 8 days ahead, else None."""
    if resets_at is None or resets_at <= now or resets_at - now > _MAX_AHEAD:
        return None
    return resets_at


def _zone(text: str | None) -> timezone:
    if not text:
        return ZAI_TZ
    if text.upper() in ("Z", "UTC"):
        return UTC
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


__all__ = [
    "FALLBACK_RETRY",
    "QUOTA_CODES",
    "QuotaHit",
    "QuotaKind",
    "parse_quota_error",
    "plausible_reset",
    "reset_time",
]
