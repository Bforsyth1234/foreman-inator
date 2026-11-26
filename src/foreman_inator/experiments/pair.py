"""Head-to-head writer pairs (``ab:pair``, todo #34, GH #16): side B's opponent, its pin
on side B's first attempt, and the pair record (W-L, cost per win).

Each side of a pair is its own ticket; a ``pairs`` row links them (migration 016, the
store's Pair API). This module only chooses models and reads results: it never merges,
closes or gates anything. Pair results are SHOWN in experiment results and promote
output but never gate promotion (Decision 2): nothing here feeds ``split.judge``.

The opponent (``choose_opponent``, Decision 1)
----------------------------------------------
Side A is the normal ticket and starts on its rung of the ladder. Side B's opponent is:

1. the running writer experiment's challenger, when it can run on A's start tier under
   the same 7c rule ``split.pick`` uses: the challenger's model is a configured writer
   rung on the same tier as A's start rung (a pair never moves a side up or down the
   ladder through the experiment) and is not cloud when A's start rung is local (an
   experiment never sends a local ticket's code to the cloud);
2. else the next rung up the ladder from A's start;
3. else (A starts on the top rung) the rung below.

A local-only repo (``cloud = false``, #49) uses only local rungs, for the challenger as
well; with no second local model there is no opponent (None): the worker posts one
comment and keeps a single ticket. The worker calls this once, when it opens side B, and
stores the result in the pair row (``opponent_variant``, ``opponent_reason``,
``experiment_id``), so the pin below reads the row and a restart or a ladder change
afterwards cannot move it.

The pin (``pick``)
------------------
Side B's FIRST attempt only runs the opponent: ``ticket.pair_side == 'b'``, a running
pair, no counted attempt other than interrupted (``superseded``) ones (a re-run after a
crash is still the first attempt and keeps the pin), and a plain ``Retry`` (never an
``Escalate``). Every later attempt returns None, so retries, escalation and hand-over
(7e) run on side B's own history exactly as for any ticket. The pin is re-checked at run
time: a challenger must still be loadable (``split.load_challenger``: model configured,
prompt file unchanged), on the step's tier and not cloud when the step is local (7c); a
local-only repo never gets a cloud opponent. A refused pin returns None and side B runs
what escalation chose.

The record (``record`` / ``score``)
-----------------------------------
``record`` stores the decision through ``store.decide_pair``: the merged side wins and
the other is ``merged_other``; a side whose PR the user closed alone loses ``rejected``
and the survivor is the winner (Decision 4). ``score`` groups decided pairs by model
pair (side A's first-attempt variant, side B's opponent variant) into W-L and cost per
win, where a pair's cost is BOTH tickets' attempts plus their non-shadow reviews, so the
loser's spend is charged to the win.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from fde_kit.prompts import PromptRef

from foreman_inator.config import ConfigError, ModelEndpoint
from foreman_inator.experiments import split, variants
from foreman_inator.routing import escalation as esc

log = logging.getLogger("foreman_inator.experiments")

Reason = Literal["challenger", "next_rung", "lower_rung"]
LoserOutcome = Literal["merged_other", "rejected"]
REASONS: tuple[str, ...] = ("challenger", "next_rung", "lower_rung")
LOSER_OUTCOMES: tuple[str, ...] = ("merged_other", "rejected")


# =========================================================================== opponent


@dataclass(frozen=True, slots=True)
class Opponent:
    """Side B's model. ``variant``: the challenger's variant id, or the rung's model with
    the champion prompt when one was given (else None: the worker registers it)."""

    model: str
    tier: int
    reason: Reason
    variant: str | None = None
    experiment_id: int | None = None

    @property
    def label(self) -> str:
        """What the pair row stores as ``opponent_variant``."""
        return self.variant or self.model


def _model_of(variant: str) -> str:
    try:
        return variants.parse_variant_id(variant).model
    except variants.VariantError:
        return variant  # a bare model name


def _index(rungs: Sequence[esc.Rung], start: Any) -> int | None:
    """Position of A's start rung: a ``Rung``, a model name, or an index."""
    if isinstance(start, int) and not isinstance(start, bool):
        return start if 0 <= start < len(rungs) else None
    model = start if isinstance(start, str) else getattr(start, "model", None)
    tier = None if isinstance(start, str) else getattr(start, "tier", None)
    for i, r in enumerate(rungs):
        if r.model == model and (tier is None or r.tier == tier):
            return i
    return None


def _fits(ch: esc.Rung, a: esc.Rung, *, local_only: bool) -> bool:
    """7c, as ``split.pick``: same tier, never cloud when A is local; a local-only repo
    also needs a local challenger (a rung of unknown kind counts as cloud)."""
    if ch.tier != a.tier:
        return False
    if a.local and not ch.local:
        return False
    return ch.local or not local_only


def choose_opponent(
    ladder: Sequence[esc.Rung],
    a_start_rung: Any,
    *,
    experiment: Any = None,
    local_only: bool = False,
    prompt: PromptRef | None = None,
) -> Opponent | None:
    """Side B's opponent (Decision 1; see the module docstring). ``a_start_rung``: A's
    start ``Rung`` (or its model name, or its index in ``ladder``). ``experiment``: the
    running writer experiment (``id``, ``challenger_variant``), or None. ``prompt``: the
    champion prompt, to give rung opponents a variant id. None: no second model."""
    full = tuple(ladder)
    rungs = esc.local_rungs(full) if local_only else full
    a_full = _index(full, a_start_rung)
    if a_full is None:
        return None
    a = full[a_full]
    if experiment is not None and getattr(experiment, "challenger_variant", None):
        cv = str(experiment.challenger_variant)
        ch_model = _model_of(cv)
        ch = next((r for r in full if r.model == ch_model), None)
        if ch is not None and _fits(ch, a, local_only=local_only):
            return Opponent(ch.model, ch.tier, "challenger", cv, getattr(experiment, "id", None))
    idx = _index(rungs, a)
    if idx is None:
        return None
    for j, reason in ((idx + 1, "next_rung"), (idx - 1, "lower_rung")):
        if 0 <= j < len(rungs) and rungs[j].model != a.model:
            r = rungs[j]
            vid = variants.variant_id(r.model, prompt) if prompt is not None else None
            return Opponent(r.model, r.tier, reason, vid)  # type: ignore[arg-type]
    return None


def create_args(opponent: Opponent) -> dict[str, Any]:
    """The opponent's columns for ``store.create_pair`` (design field names)."""
    return {
        "opponent_variant": opponent.label,
        "opponent_reason": opponent.reason,
        "experiment_id": opponent.experiment_id,
    }


# =========================================================================== the pin


@dataclass(frozen=True, slots=True)
class PairPick:
    """What the pair changes for side B's first attempt (``template`` None: champion's)."""

    pair_id: int
    endpoint: ModelEndpoint
    template: PromptRef | None
    reason: str
    opponent_reason: Reason
    experiment_id: int | None = None


def pair_for(store: Any, ticket: Any) -> Any:
    """The ticket's pair row, or None (also when the store has no Pair API)."""
    fn = getattr(store, "pair_for_ticket", None)
    return None if fn is None else fn(ticket.id)


def pick(
    store: Any,
    config: Any,
    ticket: Any,
    step: Any,
    endpoint: ModelEndpoint,
    counted: Sequence[Any],
    *,
    local_only: bool = False,
) -> PairPick | None:
    """The opponent pin for side B's first attempt (see the module docstring)."""
    if getattr(ticket, "pair_side", None) != "b":
        return None
    if any(getattr(a, "outcome", None) != "superseded" for a in counted):
        return None  # not the first attempt: escalation decides
    if not isinstance(step, esc.Retry) or isinstance(step, esc.Escalate):
        return None
    pair = pair_for(store, ticket)
    if pair is None or getattr(pair, "status", None) != "running":
        return None
    reason = getattr(pair, "opponent_reason", None)
    label = getattr(pair, "opponent_variant", None)
    if reason not in REASONS or not label:
        return None
    exp_id = getattr(pair, "experiment_id", None)
    template: PromptRef | None = None
    if reason == "challenger":
        try:
            ch = split.load_challenger(config, label)
        except (ValueError, variants.VariantError) as exc:
            log.warning("pair #%s: challenger not runnable: %s", pair.id, exc)
            return None
        opp, template = ch.endpoint, ch.template
        if opp.tier != endpoint.tier or (opp.is_cloud and not endpoint.is_cloud):
            return None  # 7c: never a local ticket's code to the cloud via an experiment
    else:
        try:
            opp = config.model(_model_of(label))
        except ConfigError:
            log.warning("pair #%s: opponent %s is not configured", pair.id, label)
            return None
    if local_only and opp.is_cloud:
        return None  # #49: a local-only repo never runs a cloud opponent
    return PairPick(
        pair.id, opp, template, f"pair #{pair.id}: side B runs {label} ({reason})", reason, exp_id
    )


# =========================================================================== results


def record(store: Any, pair: Any, winner: Any, outcome: str) -> Any:
    """Store the pair's decision. ``winner``: the winning side's ticket (or its id);
    ``outcome``: the loser's, ``merged_other`` or ``rejected``. Returns the decided pair."""
    if outcome not in LOSER_OUTCOMES:
        raise ValueError(f"loser outcome must be one of {LOSER_OUTCOMES}, not {outcome!r}")
    wid = int(getattr(winner, "id", winner))
    if wid not in (pair.ticket_a, pair.ticket_b):
        raise ValueError(f"ticket {wid} is not a side of pair #{pair.id}")
    return store.decide_pair(pair.id, wid, outcome)


@dataclass(frozen=True, slots=True)
class PairRecord:
    """Decided pairs of one model pair. ``a``: side A's variant; ``b``: B's opponent."""

    a: str
    b: str
    a_wins: int
    b_wins: int
    a_cost_usd: float  # both sides' spend over the pairs A won
    b_cost_usd: float

    @property
    def decided(self) -> int:
        return self.a_wins + self.b_wins

    @property
    def a_cost_per_win(self) -> float | None:
        return self.a_cost_usd / self.a_wins if self.a_wins else None

    @property
    def b_cost_per_win(self) -> float | None:
        return self.b_cost_usd / self.b_wins if self.b_wins else None


def ticket_cost(store: Any, ticket_id: int) -> float:
    """A ticket's writer attempts plus their non-shadow reviews."""
    att = store.query(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM attempts WHERE ticket_id = ?", (ticket_id,)
    )[0]["c"]
    rev = store.query(
        "SELECT COALESCE(SUM(r.cost_usd), 0) AS c FROM reviews r"
        " JOIN attempts a ON a.id = r.attempt_id WHERE a.ticket_id = ? AND r.shadow = 0",
        (ticket_id,),
    )[0]["c"]
    return float(att or 0.0) + float(rev or 0.0)


def side_a_variant(store: Any, ticket_id: int) -> str:
    """Side A's first counted attempt's variant (else its model; else ``?``)."""
    rows = store.query(
        "SELECT variant_id, model FROM attempts WHERE ticket_id = ?"
        " AND (outcome IS NULL OR outcome != 'superseded') ORDER BY id LIMIT 1",
        (ticket_id,),
    )
    if not rows:
        return "?"
    return str(rows[0]["variant_id"] or rows[0]["model"])


def score(
    store: Any, *, experiment_id: int | None = None, repo: str | None = None
) -> list[PairRecord]:
    """W-L and cost per win per model pair over decided pairs (optionally one experiment's
    or one repo's), sorted by model pair."""
    acc: dict[tuple[str, str], list[float]] = {}
    for p in store.list_pairs():
        if getattr(p, "status", None) != "decided" or p.winner_ticket_id is None:
            continue
        if experiment_id is not None and getattr(p, "experiment_id", None) != experiment_id:
            continue
        if repo is not None and getattr(p, "repo", None) != repo:
            continue
        key = (side_a_variant(store, p.ticket_a), str(p.opponent_variant))
        row = acc.setdefault(key, [0, 0, 0.0, 0.0])
        cost = ticket_cost(store, p.ticket_a) + ticket_cost(store, p.ticket_b)
        k = 0 if p.winner_ticket_id == p.ticket_a else 1
        row[k] += 1
        row[2 + k] += cost
    return [
        PairRecord(a, b, int(v[0]), int(v[1]), float(v[2]), float(v[3]))
        for (a, b), v in sorted(acc.items())
    ]


def _cpw(x: float | None) -> str:
    return "-" if x is None else f"${x:.4f}"


def note(records: Sequence[PairRecord]) -> str:
    """Lines for an experiment's ``result_note`` / promote output (data only)."""
    if not records:
        return "pairs: none decided"
    return "\n".join(
        f"pair {r.a} vs {r.b}: {r.a_wins}-{r.b_wins}, cost per win "
        f"{_cpw(r.a_cost_per_win)} / {_cpw(r.b_cost_per_win)}"
        for r in records
    )


__all__ = [
    "LOSER_OUTCOMES",
    "REASONS",
    "Opponent",
    "PairPick",
    "PairRecord",
    "choose_opponent",
    "create_args",
    "note",
    "pair_for",
    "pick",
    "record",
    "score",
    "side_a_variant",
    "ticket_cost",
]
