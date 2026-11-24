"""The model proxy (#9): OpenAI-compatible, traced, recorded, priced.

See `server` for the URL scheme, the spend-hook signature and the fail-closed rules, and
`pricing` for where each price came from.
"""

from .pricing import CONSERVATIVE_DEFAULT, PRICES, Price, cost_usd, estimate_tokens, price_for
from .server import (
    CallContext,
    CallUsage,
    ModelProxy,
    SpendHook,
    error_response,
    no_spend_hook,
)

__all__ = [
    "CONSERVATIVE_DEFAULT",
    "PRICES",
    "CallContext",
    "CallUsage",
    "ModelProxy",
    "Price",
    "SpendHook",
    "cost_usd",
    "error_response",
    "estimate_tokens",
    "no_spend_hook",
    "price_for",
]
