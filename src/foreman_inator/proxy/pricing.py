"""Model prices for the proxy's cost accounting (USD per million tokens).

Rules:

* A `local` endpoint always costs 0, whatever its model id.
* A cloud model id is looked up exactly (lower-cased), then with a trailing date suffix
  removed (``claude-sonnet-4-5-20250929`` -> ``claude-sonnet-4-5``).
* An unknown cloud model gets ``CONSERVATIVE_DEFAULT``: the highest list price of any
  model in the table below, so spend caps (#18) trip early rather than late. Its
  ``conservative`` flag is set so callers and spans can say the cost is a guess.

Sources (read 2026-11-23; re-check when a price looks wrong):

* Anthropic: https://platform.claude.com/docs/en/about-claude/pricing ("Model pricing",
  base input and output columns; cache and batch prices are not used: the proxy sees
  only OpenAI-style ``prompt_tokens`` / ``completion_tokens``).
* Z.ai GLM: https://docs.z.ai/guides/overview/pricing (input and output columns).
  NOTE: the GLM Coding Plan (base URL ``.../api/coding/paas/v4``) is a flat monthly fee,
  so its marginal cost is really 0. The table charges pay-as-you-go list prices anyway,
  on purpose: it over-counts toward the caps instead of letting tier 3 run uncapped.
  Whether plan calls should count as 0 is the user's decision (see #18).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from foreman_inator.config import ModelEndpoint

AS_OF = "2026-11-23"
ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
ZAI_SOURCE = "https://docs.z.ai/guides/overview/pricing"


@dataclass(frozen=True)
class Price:
    """List price per million tokens. ``long_*`` apply when a request's prompt is over
    ``long_input_threshold`` tokens (Claude Haiku 5.5 only, today)."""

    input_per_mtok: float
    output_per_mtok: float
    source: str
    as_of: str = AS_OF
    conservative: bool = False  # True: not a real price, a deliberately high guess
    long_input_threshold: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        rate_in, rate_out = self.input_per_mtok, self.output_per_mtok
        if self.long_input_threshold is not None and tokens_in > self.long_input_threshold:
            rate_in = self.long_input_per_mtok or rate_in
            rate_out = self.long_output_per_mtok or rate_out
        return (max(0, tokens_in) * rate_in + max(0, tokens_out) * rate_out) / 1_000_000


FREE = Price(0.0, 0.0, "local model: runs on this Mac, no per-token cost")


def _a(i: float, o: float, **kw: float | int) -> Price:
    return Price(i, o, ANTHROPIC_SOURCE, **kw)  # type: ignore[arg-type]


def _z(i: float, o: float) -> Price:
    return Price(i, o, ZAI_SOURCE)


PRICES: dict[str, Price] = {
    # Anthropic (Claude API ids)
    "claude-fable-5-2": _a(8, 40),
    "claude-mythos-5-2": _a(8, 40),
    "claude-fable-5-1": _a(10, 50),
    "claude-mythos-5-1": _a(10, 50),
    "claude-fable-5": _a(10, 50),
    "claude-mythos-5": _a(10, 50),
    "claude-opus-5-5": _a(4, 20),
    "claude-opus-5": _a(5, 25),
    "claude-opus-4-8": _a(5, 25),
    "claude-opus-4-7": _a(5, 25),
    "claude-opus-4-6": _a(5, 25),
    "claude-opus-4-5": _a(5, 25),
    "claude-sonnet-5-5": _a(2, 10),
    "claude-sonnet-5": _a(2, 10),
    "claude-sonnet-4-6": _a(3, 15),
    "claude-sonnet-4-5": _a(3, 15),
    "claude-haiku-5-5": _a(
        0.10,
        0.50,
        long_input_threshold=100_000,
        long_input_per_mtok=0.50,
        long_output_per_mtok=2.50,
    ),
    "claude-haiku-4-5": _a(1, 5),
    # Z.ai GLM (pay-as-you-go list prices; see the module note on the Coding Plan)
    "glm-5.3": _z(1.4, 4.4),
    "glm-5.3-flash": _z(0.15, 0.50),
    "glm-5.3-flashx": _z(0.37, 1.25),
    "glm-5.2": _z(1.4, 4.4),
    "glm-5.1": _z(1.4, 4.4),
    "glm-5": _z(1.0, 3.2),
    "glm-4.7": _z(0.6, 2.2),
    "glm-4.6": _z(0.6, 2.2),
    "glm-4.5": _z(0.6, 2.2),
    "glm-4.5-air": _z(0.2, 1.1),
    "glm-4.7-flash": _z(0.0, 0.0),
    "glm-4.5-flash": _z(0.0, 0.0),
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


def cost_usd(endpoint: ModelEndpoint, tokens_in: int, tokens_out: int) -> float:
    return price_for(endpoint).cost(tokens_in, tokens_out)


def estimate_tokens(text_or_bytes: str | bytes) -> int:
    """Rough token count when the upstream reports none: ~1 token per 3 characters
    (deliberately high, used for pre-flight spend estimates and missing usage)."""
    n = len(text_or_bytes)
    return (n + 2) // 3


__all__ = [
    "AS_OF",
    "CONSERVATIVE_DEFAULT",
    "FREE",
    "PRICES",
    "Price",
    "cost_usd",
    "estimate_tokens",
    "price_for",
]
