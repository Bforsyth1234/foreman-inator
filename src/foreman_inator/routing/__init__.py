"""Routing: pick a ticket's start tier (rules, #15) and escalate after attempts (#16).

Re-exports router v1 (``rules``). ``escalation`` is imported by its own module path.
"""

from .rules import (
    FALLBACK_TIER,
    LABEL_TIERS,
    ROUTER_PROMPT_NAME,
    Override,
    Route,
    RouterError,
    load_router_template,
    parse_label,
    resolve_tier,
    route,
)

__all__ = [
    "FALLBACK_TIER",
    "LABEL_TIERS",
    "ROUTER_PROMPT_NAME",
    "Override",
    "Route",
    "RouterError",
    "load_router_template",
    "parse_label",
    "resolve_tier",
    "route",
]
