"""Repo profiles (todo #51, GH #28): per-repo ``writer`` and ``review`` settings.

Pure: no store, no GitHub, no clock. The worker reads a repo's ``RepoSettings`` (columns
``writer_mode`` / ``review_mode``, migration 008; NULL means ``normal``), turns it into a
``Profile`` with ``profile_of``, and asks this module what the profile changes:

* ``limits_for(profile)`` -> ``escalation.Limits``: the escalation counts, the start shift,
  the exploration rate and whether a cloud writer attempt waits for approval.
* ``shift_start(tier, tiers, limits, override=...)``: the tier the first attempt runs on.
* ``review_plan(profile)``: off, hard checks only (``light``) or the full AI review, and
  whether the ``review:skip`` label counts.

``writer`` (cost is what the other two settings are bought with):

========  ===========================================================================
thrifty   3 check attempts per segment on a tier before moving up; a cloud writer
          attempt waits for ``fman approve-spend`` (``Spend.writer_gate``); explore 25%
normal    today's rules (2 check attempts); explore at the config's ``explore_rate``
fast      starts one configured tier above the router's pick (capped at the top
          tier); the first failed check moves up; never explores
========  ===========================================================================

User-kickback rules are the same in all three.

``review``:

========  ===========================================================================
off       the ``review:skip`` path: checks pass -> PR marked ready, no review row
light     review checks 1-3 (tests, coverage, revert); verdict from hard findings
          alone: no run-app, no model call (``review_change(depth="light")``)
normal    today's AI review; ``review:skip`` counts
strict    the full review; ``review:skip`` is ignored; one AI kickback moves up
========  ===========================================================================

Presets: ``prototype`` = fast + off, ``personal`` = thrifty + light, ``default`` = normal +
normal, ``production`` = fast + strict.

A ``tier:N`` label is never shifted. ``routed_tier`` stays the router's own pick (#23's
kNN router learns from it); the shifted start is stored next to it and ``route_reason``
gains ``"; profile fast: tier 1 -> 2"``. Every attempt row records both modes.

Invariants: every mode keeps a finite attempt bound (7e: every ``Limits`` count is at
least 1); ``off`` and ``light`` run no model reviewer, so 7f is not in play; ``off`` marks
the PR ready with no review, as ``review:skip`` already does (7a).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, get_args

from foreman_inator.routing.escalation import DEFAULT_LIMITS, Limits

WriterMode = Literal["thrifty", "normal", "fast"]
ReviewMode = Literal["off", "light", "normal", "strict"]
WRITER_MODES: tuple[str, ...] = get_args(WriterMode)
REVIEW_MODES: tuple[str, ...] = get_args(ReviewMode)
#: ``review_change``'s depth: "full" (five checks + the model) or "light" (checks 1-3).
ReviewDepth = Literal["full", "light"]

#: Exploration rates (#23 reads them through ``Limits.explore_rate``). ``normal`` keeps
#: the config's ``explore_rate`` (default 0.10), so a repo with no profile is unchanged.
EXPLORE_RATE_THRIFTY = 0.25
EXPLORE_RATE_NORMAL = 0.10
EXPLORE_RATE_FAST = 0.0
#: thrifty: first try + two same-tier retries.
THRIFTY_CHECK_ATTEMPTS = 3


class ProfileError(ValueError):
    """An unknown preset or mode. The message lists the valid names."""


@dataclass(frozen=True)
class Profile:
    writer: WriterMode = "normal"
    review: ReviewMode = "normal"

    def __post_init__(self) -> None:
        if self.writer not in WRITER_MODES:
            raise ProfileError(
                f"unknown writer mode {self.writer!r}; valid: {', '.join(WRITER_MODES)}"
            )
        if self.review not in REVIEW_MODES:
            raise ProfileError(
                f"unknown review mode {self.review!r}; valid: {', '.join(REVIEW_MODES)}"
            )


DEFAULT = Profile()
PRESETS: dict[str, Profile] = {
    "prototype": Profile("fast", "off"),
    "personal": Profile("thrifty", "light"),
    "default": DEFAULT,
    "production": Profile("fast", "strict"),
}


def preset(name: str) -> Profile:
    """The preset called ``name`` (case-insensitive); ``ProfileError`` names the valid ones."""
    key = name.strip().lower()
    if key not in PRESETS:
        raise ProfileError(f"unknown profile {name!r}; valid: {', '.join(PRESETS)}")
    return PRESETS[key]


def resolve(
    preset_name: str | None = None,
    *,
    writer: str | None = None,
    review: str | None = None,
    current: Profile = DEFAULT,
) -> Profile:
    """``fman repo set-profile <repo> [<preset>] [--writer W] [--review R]``: start from the
    preset (or the repo's current profile), then apply ``--writer`` / ``--review``."""
    base = preset(preset_name) if preset_name else current
    return Profile(
        (writer.strip().lower() if writer else base.writer),  # type: ignore[arg-type]
        (review.strip().lower() if review else base.review),  # type: ignore[arg-type]
    )


def profile_of(settings: object | None) -> Profile:
    """A repo's profile from its ``RepoSettings`` row (or None): NULL columns are normal."""
    writer = getattr(settings, "writer_mode", None) or "normal"
    review = getattr(settings, "review_mode", None) or "normal"
    return Profile(writer, review)  # type: ignore[arg-type]


def preset_name(p: Profile) -> str | None:
    for name, q in PRESETS.items():
        if q == p:
            return name
    return None


def describe(p: Profile) -> str:
    """``"prototype (writer fast, review off)"``; ``"writer fast, review light"`` off-preset."""
    modes = f"writer {p.writer}, review {p.review}"
    name = preset_name(p)
    return f"{name} ({modes})" if name else modes


def pr_line(p: Profile) -> str:
    """The PR description line (wired by the runner into ``pr/publisher.py``)."""
    return f"Repo profile: {describe(p)}"


# --------------------------------------------------------------------------- limits


def limits_for(p: Profile) -> Limits:
    """The escalation limits for ``p``. ``default`` gives ``DEFAULT_LIMITS`` exactly."""
    limits = DEFAULT_LIMITS
    if p.writer == "thrifty":
        limits = Limits(
            max_check_attempts=THRIFTY_CHECK_ATTEMPTS,
            explore_rate=EXPLORE_RATE_THRIFTY,
            cloud_approval=True,
        )
    elif p.writer == "fast":
        limits = Limits(max_check_attempts=1, start_shift=1, explore_rate=EXPLORE_RATE_FAST)
    if p.review == "strict":
        limits = replace(limits, max_ai_kickbacks=1)
    return limits


def explore_rate(limits: Limits, config_rate: float) -> float:
    """The rate the explore draw uses: the profile's, else the config's (``normal``)."""
    return config_rate if limits.explore_rate is None else float(limits.explore_rate)


def shift_start(tier: int, tiers: Iterable[int], limits: Limits, *, override: bool = False) -> int:
    """The tier the first attempt runs on: ``limits.start_shift`` configured tiers above
    the router's pick, capped at the top configured tier. A ``tier:N`` override (or no
    shift, or no configured tiers) returns ``tier`` unchanged. The pick is first resolved
    as escalation does (the first configured tier at or above it)."""
    have = sorted(set(tiers))
    if override or limits.start_shift <= 0 or not have:
        return tier
    i = next((k for k, t in enumerate(have) if t >= tier), len(have) - 1)
    return have[min(i + limits.start_shift, len(have) - 1)]


def shift_reason(p: Profile, routed: int, start: int) -> str:
    """The ``route_reason`` suffix for a shift (empty when nothing moved)."""
    return f"; profile {p.writer}: tier {routed} -> {start}" if start != routed else ""


# --------------------------------------------------------------------------- review


@dataclass(frozen=True)
class ReviewPlan:
    """What the review stage does for one profile."""

    #: "off": hand over with no review; "light": checks 1-3, no model; "full": the review.
    kind: Literal["off", "light", "full"]
    #: Does an allowed user's ``review:skip`` label skip the review? (False for strict.)
    skip_label_counts: bool

    @property
    def depth(self) -> ReviewDepth:
        return "light" if self.kind == "light" else "full"


def review_plan(p: Profile) -> ReviewPlan:
    if p.review == "off":
        return ReviewPlan("off", skip_label_counts=True)
    if p.review == "light":
        return ReviewPlan("light", skip_label_counts=True)
    return ReviewPlan("full", skip_label_counts=p.review != "strict")


__all__ = [
    "DEFAULT",
    "EXPLORE_RATE_FAST",
    "EXPLORE_RATE_NORMAL",
    "EXPLORE_RATE_THRIFTY",
    "PRESETS",
    "REVIEW_MODES",
    "THRIFTY_CHECK_ATTEMPTS",
    "WRITER_MODES",
    "Profile",
    "ProfileError",
    "ReviewDepth",
    "ReviewMode",
    "ReviewPlan",
    "WriterMode",
    "describe",
    "explore_rate",
    "limits_for",
    "pr_line",
    "preset",
    "preset_name",
    "profile_of",
    "resolve",
    "review_plan",
    "shift_reason",
    "shift_start",
]
