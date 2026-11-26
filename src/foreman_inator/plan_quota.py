"""GLM Coding Plan quota (#85): estimated credits and pauses at z.ai's quota errors.

z.ai has no API for a plan's remaining quota (the dashboard shows it), so this module
works from what the worker itself sees:

* **Credits.** The proxy estimates each plan call's credits from its token counts
  (``proxy.pricing.plan_credits``) and ``Spend`` stores them on the call's ``plan = 1``
  ledger row (migration 014). ``usage`` sums them over the last 5 hours and the last 7
  days. Both are estimates: z.ai's 5-hour window starts at the first call after a reset
  and its weekly window at purchase, not on rolling local windows, and calls made
  outside this worker (another machine, the z.ai chat) are not seen.
* **Pauses.** A 429 quota error (``proxy.zai_quota``) pauses the plan's account until the
  reset time it names (or ``FALLBACK_RETRY`` from now when it names none; an expired
  plan until the user says it is renewed). The pause is kept in the store's
  ``poll_cursors`` table (repo ``""``, name ``plan_pause:<account>``) so a restart keeps
  it.

One account is one API key (``api_key_ref``, else the base URL): every model on it
shares the plan's quota, so credits are summed and a pause covers them all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from foreman_inator.proxy.pricing import PLAN_LIMITS
from foreman_inator.proxy.zai_quota import FALLBACK_RETRY, QuotaHit, plausible_reset

if TYPE_CHECKING:
    from collections.abc import Iterable

    from foreman_inator.config import ModelEndpoint
    from foreman_inator.db.store import Store

FIVE_HOURS = timedelta(hours=5)
WEEK = timedelta(days=7)
_CURSOR_REPO = ""  # poll_cursors rows not tied to a repo


def account_of(ep: ModelEndpoint) -> str:
    """The plan account a model's calls count against."""
    return ep.api_key_ref or ep.base_url


@dataclass(frozen=True)
class PlanPause:
    account: str
    code: int
    kind: str  # zai_quota.QuotaKind
    until: datetime | None  # UTC; None: until the user renews (code 1309)
    message: str


@dataclass(frozen=True)
class PlanCredits:
    """One plan account's estimated credits against its limits, for `fman status`."""

    models: tuple[str, ...]
    tier: str
    used_5h: float
    limit_5h: int
    used_week: float
    limit_week: int
    paused: PlanPause | None = None


class PlanQuota:
    def __init__(self, store: Store, models: Iterable[ModelEndpoint]):
        self.store = store
        self.accounts: dict[str, list[ModelEndpoint]] = {}
        for m in models:
            if m.plan_usd_month is not None:
                self.accounts.setdefault(account_of(m), []).append(m)

    # ------------------------------------------------------------------ pauses

    def pause(self, ep: ModelEndpoint, hit: QuotaHit, now: datetime) -> PlanPause:
        until = plausible_reset(hit.resets_at, now)
        if until is None and hit.kind != "expired":
            until = now + FALLBACK_RETRY
        p = PlanPause(account_of(ep), hit.code, hit.kind, until, hit.message)
        value = {
            "code": p.code,
            "kind": p.kind,
            "until": until.isoformat() if until else None,
            "message": p.message,
        }
        self.store.set_cursor(_CURSOR_REPO, json.dumps(value), _key(p.account))
        return p

    def paused(self, ep_or_account: ModelEndpoint | str, now: datetime) -> PlanPause | None:
        """The account's pause, or None (an elapsed pause is cleared)."""
        account = ep_or_account if isinstance(ep_or_account, str) else account_of(ep_or_account)
        raw = self.store.get_cursor(_CURSOR_REPO, _key(account))
        if not raw:
            return None
        try:
            v = json.loads(raw)
        except ValueError:
            return None
        until = datetime.fromisoformat(v["until"]) if v.get("until") else None
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if until is not None and until <= now:
            self.clear(account)
            return None
        return PlanPause(account, int(v["code"]), str(v["kind"]), until, str(v.get("message", "")))

    def clear(self, account: str) -> None:
        self.store.set_cursor(_CURSOR_REPO, "", _key(account))

    # ------------------------------------------------------------------ credits

    def usage(self, now: datetime) -> tuple[PlanCredits, ...]:
        """Each account that has a ``plan_tier``: credits in the last 5 h and 7 days."""
        out = []
        for account, models in self.accounts.items():
            tier = next((m.plan_tier for m in models if m.plan_tier), None)
            if tier is None:
                continue
            limit_5h, limit_week = PLAN_LIMITS[tier]
            names = tuple(m.name for m in models)
            used_5h = sum(self.store.plan_credits(n, since=now - FIVE_HOURS) for n in names)
            used_week = sum(self.store.plan_credits(n, since=now - WEEK) for n in names)
            out.append(
                PlanCredits(
                    names, tier, used_5h, limit_5h, used_week, limit_week, self.paused(account, now)
                )
            )
        return tuple(out)


def _key(account: str) -> str:
    return f"plan_pause:{account}"


__all__ = ["FIVE_HOURS", "WEEK", "PlanCredits", "PlanPause", "PlanQuota", "account_of"]
