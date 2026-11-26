"""Model prices for the proxy's cost accounting (USD per million tokens).

Rules:

* A `local` endpoint always costs 0, whatever its model id.
* A cloud model id is looked up exactly (lower-cased), then with a trailing date suffix
  removed (``claude-sonnet-4-5-20250929`` -> ``claude-sonnet-4-5``).
* An unknown cloud model gets ``CONSERVATIVE_DEFAULT``: the highest list price of any
  model in the table below, so spend caps (#18) trip early rather than late. Its
  ``conservative`` flag is set so callers and spans can say the cost is a guess.

Sources (read 2026-11-23; re-check when a price looks wrong):

* Anthropic: https://platform.claude.com/docs/en/about-claude/pricing ("Model pricing":
  base input, output and "Cache hits and refreshes" columns; "Prompt caching": a 5-minute
  cache write is 1.25x the base input price, a 1-hour write 2x; read again 2026-11-25
  for #52). Cache hits are 0.1x on most models, 0.05x on Opus 5.5 and Sonnet 5.5,
  0.025x on Fable/Mythos 5.1 and 5.2; the table below gives the per-model hit price.
  Batch prices are not used.
* Z.ai GLM: https://docs.z.ai/guides/overview/pricing (input, "Cached Input" and output
  columns; read again 2026-11-25 for #52). Caching is implicit
  (https://docs.z.ai/guides/capabilities/cache): the response's
  ``usage.prompt_tokens_details.cached_tokens`` are priced at the cached rate, and
  ``prompt_tokens`` is taken to include them. "Cached Input Storage" is listed as
  "Limited-time Free" (2026-11-25), so storage is priced at 0.
  NOTE: the GLM Coding Plan (base URL ``.../api/coding/paas/v4``) is a flat monthly fee,
  so its marginal cost is really 0. The table charges pay-as-you-go list prices anyway,
  on purpose: it over-counts toward the caps instead of letting tier 3 run uncapped.
  Whether plan calls should count as 0 is the user's decision (see #18).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from foreman_inator.config import ModelEndpoint

AS_OF = "2026-11-25"
ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
ZAI_SOURCE = "https://docs.z.ai/guides/overview/pricing"


@dataclass(frozen=True)
class Price:
    """List price per million tokens. ``long_*`` apply when a request's prompt is over
    ``long_input_threshold`` tokens (Claude Haiku 5.5 only, today; the prompt length
    counts cache reads and writes too, per the Anthropic pricing page).

    Prompt caching (#52): ``cache_read_per_mtok`` is the price of a cached input token
    (None: priced as plain input, as before caching was counted).
    ``cache_write_5m_mult`` / ``cache_write_1h_mult`` multiply the input rate for tokens
    written to the cache (None: priced as plain input; Z.ai has no write charge)."""

    input_per_mtok: float
    output_per_mtok: float
    source: str
    as_of: str = AS_OF
    conservative: bool = False  # True: not a real price, a deliberately high guess
    long_input_threshold: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None
    cache_read_per_mtok: float | None = None
    long_cache_read_per_mtok: float | None = None
    cache_write_5m_mult: float | None = None
    cache_write_1h_mult: float | None = None

    def cost(
        self,
        tokens_in: int,
        tokens_out: int,
        *,
        cache_read: int = 0,
        cache_write_5m: int = 0,
        cache_write_1h: int = 0,
    ) -> float:
        """USD for one call. ``tokens_in`` is ALL input tokens, cache reads and writes
        included; the cache counts say how many of them were read from or written to the
        prompt cache. With no cache counts this is the plain list price."""
        rate_in, rate_out = self.input_per_mtok, self.output_per_mtok
        read_rate = self.cache_read_per_mtok
        if self.long_input_threshold is not None and tokens_in > self.long_input_threshold:
            rate_in = self.long_input_per_mtok or rate_in
            rate_out = self.long_output_per_mtok or rate_out
            read_rate = self.long_cache_read_per_mtok or read_rate
        tokens_in, tokens_out = max(0, tokens_in), max(0, tokens_out)
        read = min(max(0, cache_read), tokens_in)
        w5 = min(max(0, cache_write_5m), tokens_in - read)
        w1 = min(max(0, cache_write_1h), tokens_in - read - w5)
        plain = tokens_in - read - w5 - w1
        total = plain * rate_in + tokens_out * rate_out
        total += read * (rate_in if read_rate is None else read_rate)
        total += w5 * rate_in * (self.cache_write_5m_mult or 1.0)
        total += w1 * rate_in * (self.cache_write_1h_mult or 1.0)
        return total / 1_000_000


FREE = Price(0.0, 0.0, "local model: runs on this Mac, no per-token cost")


#: Anthropic cache write multipliers ("Prompt caching" on the pricing page, 2026-11-25).
CLAUDE_CACHE_WRITE_5M = 1.25
CLAUDE_CACHE_WRITE_1H = 2.0


def _a(i: float, o: float, read: float, **kw: float | int) -> Price:
    return Price(
        i,
        o,
        ANTHROPIC_SOURCE,
        cache_read_per_mtok=read,
        cache_write_5m_mult=CLAUDE_CACHE_WRITE_5M,
        cache_write_1h_mult=CLAUDE_CACHE_WRITE_1H,
        **kw,  # type: ignore[arg-type]
    )


def _z(i: float, o: float, cached: float | None = None) -> Price:
    return Price(i, o, ZAI_SOURCE, cache_read_per_mtok=cached)


PRICES: dict[str, Price] = {
    # Anthropic (Claude API ids): input, output, cache hit per MTok
    "claude-fable-5-2": _a(8, 40, 0.20),
    "claude-mythos-5-2": _a(8, 40, 0.20),
    "claude-fable-5-1": _a(10, 50, 0.25),
    "claude-mythos-5-1": _a(10, 50, 0.25),
    "claude-fable-5": _a(10, 50, 1.0),
    "claude-mythos-5": _a(10, 50, 1.0),
    "claude-opus-5-5": _a(4, 20, 0.20),
    "claude-opus-5": _a(5, 25, 0.50),
    "claude-opus-4-8": _a(5, 25, 0.50),
    "claude-opus-4-7": _a(5, 25, 0.50),
    "claude-opus-4-6": _a(5, 25, 0.50),
    "claude-opus-4-5": _a(5, 25, 0.50),
    "claude-sonnet-5-5": _a(2, 10, 0.10),
    "claude-sonnet-5": _a(2, 10, 0.20),
    "claude-sonnet-4-6": _a(3, 15, 0.30),
    "claude-sonnet-4-5": _a(3, 15, 0.30),
    "claude-haiku-5-5": _a(
        0.10,
        0.50,
        0.01,
        long_input_threshold=100_000,
        long_input_per_mtok=0.50,
        long_output_per_mtok=2.50,
        long_cache_read_per_mtok=0.05,
    ),
    "claude-haiku-4-5": _a(1, 5, 0.10),
    # Z.ai GLM (pay-as-you-go list prices; see the module note on the Coding Plan):
    # input, output, cached input per MTok
    "glm-5.3": _z(1.4, 4.4, 0.26),
    "glm-5.3-flash": _z(0.15, 0.50, 0.03),
    "glm-5.3-flashx": _z(0.37, 1.25, 0.075),
    "glm-5.2": _z(1.4, 4.4, 0.26),
    "glm-5.1": _z(1.4, 4.4, 0.26),
    "glm-5": _z(1.0, 3.2, 0.2),
    "glm-4.7": _z(0.6, 2.2, 0.11),
    "glm-4.6": _z(0.6, 2.2, 0.11),
    "glm-4.5": _z(0.6, 2.2, 0.11),
    "glm-4.5-air": _z(0.2, 1.1, 0.03),
    "glm-4.7-flash": _z(0.0, 0.0, 0.0),
    "glm-4.5-flash": _z(0.0, 0.0, 0.0),
}

# Highest input and output list price above (Claude Fable 5 / 5.1), marked as a guess.
CONSERVATIVE_DEFAULT = Price(
    max(p.input_per_mtok for p in PRICES.values()),
    max(p.output_per_mtok for p in PRICES.values()),
    "CONSERVATIVE DEFAULT: model not in the price table; charged at the highest list "
    "price known so spend caps trip early. Add the model to proxy/pricing.py.",
    conservative=True,
)

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def price_for(endpoint: ModelEndpoint) -> Price:
    """The price that applies to calls to ``endpoint``."""
    if endpoint.kind == "local":
        return FREE
    model = endpoint.model.lower()
    found = PRICES.get(model) or PRICES.get(_DATE_SUFFIX.sub("", model))
    return found or CONSERVATIVE_DEFAULT


def cost_usd(endpoint: ModelEndpoint, tokens_in: int, tokens_out: int, **cache: int) -> float:
    """Cost of a call; ``cache`` takes ``Price.cost``'s cache counts. The spend preflight
    passes none, so its estimate stays at the full input price (#52 decision 8)."""
    return price_for(endpoint).cost(tokens_in, tokens_out, **cache)


def estimate_tokens(text_or_bytes: str | bytes) -> int:
    """Rough token count when the upstream reports none: ~1 token per 3 characters
    (deliberately high, used for pre-flight spend estimates and missing usage)."""
    n = len(text_or_bytes)
    return (n + 2) // 3


# ----------------------------------------------------------------- GLM Coding Plan credits
#
# Source: https://docs.z.ai/devpack/overview ("Usage Credit Allowance" and the per-model
# multipliers; read 2026-11-25). The plan meters credits, not dollars:
#   credits = (uncached input x in_mult + cached input x cached_mult + output x out_mult)
#             / 10,000
# Off-peak calls are charged 50%; peak is Monday to Friday 14:00-18:00 UTC+8. The
# GLM-5.3-Flash 50% promotion (2026-11-24 to 2026-12-24) is not modelled, so the estimate
# runs high rather than low. z.ai has no API for the remaining quota (dashboard only),
# so these are local estimates.

PLAN_SOURCE = "https://docs.z.ai/devpack/overview"
PLAN_AS_OF = "2026-11-25"
PLAN_CREDIT_DIVISOR = 10_000
PLAN_OFF_PEAK_RATE = 0.5
PLAN_PEAK_TZ = timezone(timedelta(hours=8))  # Singapore Standard Time
PLAN_PEAK_HOURS = (14, 18)  # [start, end) local hours, Monday to Friday


@dataclass(frozen=True)
class CreditRate:
    """GLM Coding Plan credit multipliers per token type."""

    input_mult: float
    cached_mult: float
    output_mult: float


PLAN_CREDITS: dict[str, CreditRate] = {
    "glm-5.4": CreditRate(4.6, 1.1, 16),
    "glm-5.3": CreditRate(6.9, 1.7, 24),
    "glm-5.3-flash": CreditRate(1.15, 0.28, 4),
}

#: Credit allowance per plan tier: (per 5 hours, per week).
PLAN_LIMITS: dict[str, tuple[int, int]] = {
    "lite": (2_000, 10_000),
    "pro": (12_000, 60_000),
    "max": (28_000, 140_000),
}


def is_peak(at: datetime) -> bool:
    """True during the plan's peak hours (Mon-Fri 14:00-18:00 UTC+8)."""
    local = (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(PLAN_PEAK_TZ)
    start, end = PLAN_PEAK_HOURS
    return local.weekday() < 5 and start <= local.hour < end


def credit_rate(model: str) -> CreditRate | None:
    m = model.lower()
    return PLAN_CREDITS.get(m) or PLAN_CREDITS.get(_DATE_SUFFIX.sub("", m))


def plan_credits(
    model: str, tokens_in: int, tokens_out: int, *, cache_read: int = 0, at: datetime
) -> float | None:
    """Estimated Coding Plan credits for one call; None for a model not in the table.
    ``tokens_in`` is all input, cached included (as ``Price.cost``)."""
    rate = credit_rate(model)
    if rate is None:
        return None
    tokens_in, tokens_out = max(0, tokens_in), max(0, tokens_out)
    cached = min(max(0, cache_read), tokens_in)
    raw = (
        (tokens_in - cached) * rate.input_mult
        + cached * rate.cached_mult
        + tokens_out * rate.output_mult
    ) / PLAN_CREDIT_DIVISOR
    return raw if is_peak(at) else raw * PLAN_OFF_PEAK_RATE


__all__ = [
    "AS_OF",
    "CLAUDE_CACHE_WRITE_1H",
    "CLAUDE_CACHE_WRITE_5M",
    "CONSERVATIVE_DEFAULT",
    "FREE",
    "PLAN_CREDITS",
    "PLAN_LIMITS",
    "PRICES",
    "CreditRate",
    "Price",
    "cost_usd",
    "credit_rate",
    "estimate_tokens",
    "is_peak",
    "plan_credits",
    "price_for",
]
