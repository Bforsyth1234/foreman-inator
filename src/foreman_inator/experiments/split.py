"""Writer traffic split (todo #34, GH #16): a share of live first attempts runs the challenger.

SPEC "Experiments": "10-20% of live tickets go to the challenger; you review as normal",
used "after a challenger wins on replay". The promotion rule then needs the challenger
to hold up live: its live accept rate must not fall below the champion's and its cost
per accepted ticket must not be higher. ``ab:pair`` (two PRs for one issue) is not built
here: it needs a data-model rework (two live attempts and PRs per ticket).

Experiment rows: ``experiments`` with ``role = 'writer'``, ``mode = 'split'`` and
``traffic_share`` (default ``DEFAULT_SHARE``, 15%). Only the oldest running split whose
``champion_variant`` is the current writer champion gets traffic.

Which attempts are split (``pick``)
-----------------------------------
Only a ticket's **first** attempt: escalation's plain ``Retry`` with trigger ``first`` and
no counted attempt yet. Never a retry (same-tier retries keep the trigger ``first``, so
the counted history decides), never a kickback, escalation or explore attempt, never a
re-run after a crash, never a rebase, and never a ticket routed by a ``tier:N`` label
(``route_reason`` starts with ``override:``). The first attempt must also be one the
champion would run: the step's model is the champion variant's model. And:

* the challenger won on replay: the newest running replay of it is against the current
  champion, finished, with verdict ``wins`` (``replay_won``);
* the challenger's model is configured, on the same tier as the step (a split never
  moves a ticket up or down the ladder), and is not cloud when the step's model is local
  (an experiment never sends a local ticket's code to the cloud, 7c);
* the challenger's prompt file is unchanged on disk (its hash is in the variant id).

When all that holds the ticket is **in** the experiment: its side is fixed by a stable
hash, ``sha256(salt:experiment:ticket) < traffic_share`` -> challenger, and is stored as
the ``split:<experiment>:t<ticket>`` cursor (``"champion"`` / ``"challenger"``) before the
attempt row, so a restart, a set-aside re-run and the scoring all agree. The challenger
side runs the challenger's model and prompt, and the attempt's ``variant_id`` is the
challenger's id. The repo profile still applies to it (a cloud challenger meets the same
``Spend.writer_gate``, including the thrifty approval; the per-attempt budget, #58, is
enforced by the proxy on whatever model runs).

The retry right after a challenger first attempt goes back to the champion: a plain
``Retry`` on the challenger's model (a model challenger has its own rung on the ladder)
when the ticket's only counted, non-superseded attempt is the challenger's, runs on the
champion's model instead (``pick`` returns the champion side for it). Only that one
retry: later retries and escalations follow the ladder as usual, even when the ladder
later reaches the challenger's rung, so every count stays bounded (7e).

Judging (``score`` / ``judge``)
-------------------------------
Per side, the tickets in the experiment and their first non-superseded ``first`` attempt
on that side's variant. Decided: outcome ``accepted``, ``kicked_back`` or
``failed_checks`` (still in review: not decided). Accept rate = accepted / decided; cost
per accepted = (the decided attempts' cost + their non-shadow reviews' cost) / accepted.
Both are first-attempt figures: retries run the champion on either side. ``judge`` needs
``min_sample`` decided attempts on each side, then ``holds`` when the challenger's accept
rate is not below the champion's and its cost per accepted is not above it (a challenger
with no accepted attempt fails); otherwise ``fails``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Literal

from fde_kit.prompts import PromptRef

from foreman_inator import prompts
from foreman_inator.config import ConfigError, ModelEndpoint
from foreman_inator.db.store import Attempt, Experiment, Store, Ticket
from foreman_inator.experiments import variants
from foreman_inator.routing import escalation as esc

log = logging.getLogger("foreman_inator.experiments")

DEFAULT_SHARE = 0.15
MAX_SHARE = 0.5  # more than half is not a split
DEFAULT_SALT = "foreman-inator/split-v1"
CURSOR_PREFIX = "split:"
DEFAULT_MIN_SAMPLE = 30  # decided first attempts per side (config: live_min_sample)
DECIDED = ("accepted", "kicked_back", "failed_checks")

Side = Literal["champion", "challenger"]
Status = Literal["holds", "fails", "insufficient"]


# =========================================================================== the draw


def split_draw(experiment_id: int, ticket_id: int, salt: str = DEFAULT_SALT) -> float:
    """The ticket's fixed draw in [0, 1) for this experiment."""
    digest = hashlib.sha256(f"{salt}:{int(experiment_id)}:{int(ticket_id)}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


def side_for(experiment: Experiment, ticket_id: int) -> Side:
    share = DEFAULT_SHARE if experiment.traffic_share is None else experiment.traffic_share
    return "challenger" if split_draw(experiment.id, ticket_id) < share else "champion"


def cursor_name(experiment_id: int, ticket_id: int) -> str:
    return f"{CURSOR_PREFIX}{int(experiment_id)}:t{int(ticket_id)}"


def stored_side(store: Store, ticket: Ticket, experiment_id: int) -> Side | None:
    value = store.get_cursor(ticket.repo, cursor_name(experiment_id, ticket.id))
    return value if value in ("champion", "challenger") else None  # type: ignore[return-value]


def check_share(share: float) -> float:
    share = float(share)
    if not 0.0 < share <= MAX_SHARE:
        raise ValueError(f"traffic share must be above 0 and at most {MAX_SHARE:.0%}")
    return share


# =========================================================================== eligibility


def running_splits(store: Store) -> list[Experiment]:
    """Running writer split experiments, oldest first."""
    return [e for e in store.list_experiments(role="writer", status="running") if e.mode == "split"]


def newest_replay(store: Store, challenger: str) -> Experiment | None:
    found = [
        e
        for e in store.list_experiments(role="writer", status="running")
        if e.mode == "replay" and e.challenger_variant == challenger
    ]
    return found[-1] if found else None


def replay_won(store: Store, challenger: str, champion: str, *, min_cases: int = 10) -> bool:
    """The newest running replay of ``challenger`` is against ``champion``, finished, and
    the challenger ``wins`` (``replay_runner.compare``)."""
    from foreman_inator.experiments import replay_runner as rr

    rep = newest_replay(store, challenger)
    if rep is None or rep.champion_variant != champion:
        return False
    try:
        c = rr.compare(store, rep.id, min_cases=min_cases)
    except LookupError:
        return False
    return c.state == "finished" and c.verdict == "wins"


def active_split(store: Store, *, min_cases: int = 10) -> Experiment | None:
    """The split that gets traffic: the oldest running one against the current writer
    champion whose challenger won on replay."""
    champ = variants.champion(store, "writer")
    if champ is None:
        return None
    for e in running_splits(store):
        if e.champion_variant != champ.id:
            continue
        if replay_won(store, e.challenger_variant, champ.id, min_cases=min_cases):
            return e
    return None


@dataclass(frozen=True, slots=True)
class Challenger:
    endpoint: ModelEndpoint
    template: PromptRef
    variant_id: str


def load_challenger(config: Any, variant_id: str) -> Challenger:
    """The challenger's configured model and its prompt file (refused when the file on disk
    is not the one in the id). Raises ``ValueError``."""
    p = variants.parse_variant_id(variant_id)
    try:
        endpoint = config.model(p.model)
    except ConfigError as exc:
        raise ValueError(str(exc)) from None
    try:
        ref = prompts.load(p.prompt_name)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"prompt {p.prompt_name!r}: {exc}") from None
    if ref.sha256[:8] != p.hash8:
        raise ValueError(f"{p.prompt_name}.md changed since {variant_id} was registered")
    return Challenger(endpoint, ref, variants.variant_id(p.model, ref))


def is_override(ticket: Ticket) -> bool:
    """Routed by a ``tier:N`` label (``rules.route``'s reason starts with ``override:``)."""
    return (ticket.route_reason or "").startswith("override:")


# =========================================================================== the pick


@dataclass(frozen=True, slots=True)
class Pick:
    """What a split changes for one attempt. ``template`` None: the champion's prompt."""

    experiment_id: int
    side: Side
    endpoint: ModelEndpoint
    template: PromptRef | None
    reason: str


def pick(
    store: Store,
    config: Any,
    ticket: Ticket,
    step: Any,
    endpoint: ModelEndpoint,
    counted: tuple[Attempt, ...] | list[Attempt],
    *,
    min_cases: int = 10,
) -> Pick | None:
    """The split's say over this attempt (see the module docstring); None: none."""
    if is_override(ticket):
        return None
    exp = active_split(store, min_cases=min_cases)
    if exp is None:
        return None
    try:
        champ = variants.parse_variant_id(exp.champion_variant)
    except variants.VariantError:
        return None
    if counted:
        return _back_to_champion(store, config, ticket, step, exp, champ.model, counted)
    if not _plain_retry(step) or getattr(step, "trigger", None) != "first":
        return None  # an explore start (or anything else): not a plain first attempt
    if endpoint.name != champ.model:
        return None  # the champion would not run this ticket
    try:
        ch = load_challenger(config, exp.challenger_variant)
    except (ValueError, variants.VariantError) as exc:
        log.warning("split #%d: challenger not runnable: %s", exp.id, exc)
        return None
    if ch.endpoint.tier != endpoint.tier:
        return None
    if ch.endpoint.is_cloud and not endpoint.is_cloud:
        return None  # 7c: an experiment never sends a local ticket to the cloud
    side = stored_side(store, ticket, exp.id) or side_for(exp, ticket.id)
    store.set_cursor(ticket.repo, side, cursor_name(exp.id, ticket.id))
    if side == "champion":
        return Pick(exp.id, side, endpoint, None, f"split #{exp.id}: champion side")
    return Pick(
        exp.id, side, ch.endpoint, ch.template, f"split #{exp.id}: challenger {ch.variant_id}"
    )


def _plain_retry(step: Any) -> bool:
    return isinstance(step, esc.Retry) and not isinstance(step, esc.Escalate)


def _back_to_champion(
    store: Store,
    config: Any,
    ticket: Ticket,
    step: Any,
    exp: Experiment,
    champ_model: str,
    counted: tuple[Attempt, ...] | list[Attempt],
) -> Pick | None:
    """The plain retry on the challenger's model directly after the challenger's first
    attempt runs on the champion's model (with the champion's prompt). Only that one
    retry: once the ticket has more than that one counted attempt, the ladder decides,
    even on the challenger's own rung (7e: swapping every later retry would reset the
    per-rung counts each time and, with the challenger's rung above the champion's,
    escalation would never end)."""
    if stored_side(store, ticket, exp.id) != "challenger":
        return None
    if not _plain_retry(step):
        return None  # escalations follow the ladder
    done = [a for a in counted if a.outcome != "superseded"]
    if len(done) != 1 or done[0].variant_id != exp.challenger_variant:
        return None  # not the retry right after the challenger's first attempt
    try:
        ch_model = variants.parse_variant_id(exp.challenger_variant).model
    except variants.VariantError:
        return None
    if ch_model == champ_model or getattr(step, "model", None) != ch_model:
        return None
    try:
        endpoint = config.model(champ_model)
    except ConfigError:
        return None
    return Pick(exp.id, "champion", endpoint, None, f"split #{exp.id}: retry on the champion")


# =========================================================================== judging


@dataclass(frozen=True, slots=True)
class SideStats:
    variant: str
    tickets: int  # tickets assigned to this side
    decided: int
    accepted: int
    cost_usd: float  # decided attempts + their non-shadow reviews

    @property
    def accept_rate(self) -> float | None:
        return self.accepted / self.decided if self.decided else None

    @property
    def cost_per_accepted(self) -> float | None:
        return self.cost_usd / self.accepted if self.accepted else None


@dataclass(frozen=True, slots=True)
class SplitComparison:
    experiment_id: int
    champion: SideStats
    challenger: SideStats


@dataclass(frozen=True, slots=True)
class SplitJudgment:
    status: Status
    min_sample: int
    reasons: tuple[str, ...]


def assignments(store: Store, experiment_id: int) -> dict[int, Side]:
    """ticket id -> side, from the stored cursors."""
    prefix = f"{CURSOR_PREFIX}{int(experiment_id)}:t"
    rows = store.query(
        "SELECT name, value FROM poll_cursors WHERE substr(name, 1, ?) = ?", (len(prefix), prefix)
    )
    out: dict[int, Side] = {}
    for r in rows:
        tail = str(r["name"])[len(prefix) :]
        if tail.isdigit() and r["value"] in ("champion", "challenger"):
            out[int(tail)] = r["value"]
    return out


def _side(store: Store, variant: str, tickets: list[int]) -> SideStats:
    decided = accepted = 0
    cost = 0.0
    for tid in tickets:
        rows = store.query(
            "SELECT id, outcome, cost_usd FROM attempts WHERE ticket_id = ? AND variant_id = ?"
            " AND \"trigger\" = 'first' AND (outcome IS NULL OR outcome != 'superseded')"
            " ORDER BY id LIMIT 1",
            (tid, variant),
        )
        if not rows or rows[0]["outcome"] not in DECIDED:
            continue
        a = rows[0]
        decided += 1
        accepted += a["outcome"] == "accepted"
        rev = store.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM reviews"
            " WHERE attempt_id = ? AND shadow = 0",
            (a["id"],),
        )[0]["c"]
        cost += float(a["cost_usd"] or 0.0) + float(rev or 0.0)
    return SideStats(variant, len(tickets), decided, accepted, cost)


def score(store: Store, experiment_id: int) -> SplitComparison:
    exp = store.get_experiment(experiment_id)
    if exp.mode != "split":
        raise ValueError(f"experiment {experiment_id} is a {exp.mode} experiment, not a split")
    by_side: dict[str, list[int]] = {"champion": [], "challenger": []}
    for tid, side in sorted(assignments(store, experiment_id).items()):
        by_side[side].append(tid)
    return SplitComparison(
        experiment_id,
        _side(store, exp.champion_variant, by_side["champion"]),
        _side(store, exp.challenger_variant, by_side["challenger"]),
    )


def judge(c: SplitComparison, *, min_sample: int = DEFAULT_MIN_SAMPLE) -> SplitJudgment:
    """SPEC's live half of the promotion rule for a writer (see the module docstring)."""
    if min_sample < 1:
        raise ValueError("min_sample must be at least 1")
    ch, co = c.challenger, c.champion
    short = [f"{s} {x.decided} decided" for s, x in (("champion", co), ("challenger", ch))
             if x.decided < min_sample]  # fmt: skip
    if short:
        return SplitJudgment(
            "insufficient", min_sample, (f"{'; '.join(short)} ({min_sample} needed per side)",)
        )
    reasons: list[str] = []
    if (ch.accept_rate or 0.0) < (co.accept_rate or 0.0):
        reasons.append(
            f"live accept rate {ch.accept_rate or 0:.0%} is below the champion's "
            f"{co.accept_rate or 0:.0%}"
        )
    if ch.cost_per_accepted is None:
        reasons.append("no accepted first attempt, so no cost per accepted ticket")
    elif co.cost_per_accepted is not None and ch.cost_per_accepted > co.cost_per_accepted:
        reasons.append(
            f"cost per accepted ticket ${ch.cost_per_accepted:.4f} is above the champion's "
            f"${co.cost_per_accepted:.4f}"
        )
    return SplitJudgment("fails" if reasons else "holds", min_sample, tuple(reasons))


def _fmt(s: SideStats) -> str:
    rate = "-" if s.accept_rate is None else f"{s.accept_rate:.0%}"
    cpa = "-" if s.cost_per_accepted is None else f"${s.cost_per_accepted:.4f}"
    return (
        f"{s.variant}: {s.tickets} ticket(s), {s.decided} decided, {s.accepted} accepted"
        f" ({rate}), cost per accepted {cpa}"
    )


def comparison_note(c: SplitComparison, j: SplitJudgment) -> str:
    why = f" ({'; '.join(j.reasons)})" if j.reasons else ""
    return "\n".join(
        [
            f"live: {j.status}{why}",
            f"  champion   {_fmt(c.champion)}",
            f"  challenger {_fmt(c.challenger)}",
        ]
    )


def summary(store: Store, experiment_id: int, *, min_sample: int = DEFAULT_MIN_SAMPLE) -> str:
    """One line for ``fman experiment status``."""
    c = score(store, experiment_id)
    j = judge(c, min_sample=min_sample)
    why = f" ({'; '.join(j.reasons)})" if j.reasons else ""
    return (
        f"champion {c.champion.accepted}/{c.champion.decided} accepted, challenger "
        f"{c.challenger.accepted}/{c.challenger.decided}; live: {j.status}{why}"
    )


def write_comparison(
    store: Store, experiment_id: int, *, min_sample: int = DEFAULT_MIN_SAMPLE
) -> tuple[SplitComparison, SplitJudgment]:
    """Score the split and store the comparison in its ``result_note`` (does not end it)."""
    c = score(store, experiment_id)
    j = judge(c, min_sample=min_sample)
    store.set_experiment_note(experiment_id, comparison_note(c, j))
    return c, j


__all__ = [
    "CURSOR_PREFIX",
    "DEFAULT_SHARE",
    "MAX_SHARE",
    "Challenger",
    "Pick",
    "SideStats",
    "SplitComparison",
    "SplitJudgment",
    "active_split",
    "assignments",
    "check_share",
    "comparison_note",
    "cursor_name",
    "is_override",
    "judge",
    "load_challenger",
    "newest_replay",
    "pick",
    "replay_won",
    "running_splits",
    "score",
    "side_for",
    "split_draw",
    "stored_side",
    "summary",
    "write_comparison",
]
