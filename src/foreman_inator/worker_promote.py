"""``fman experiment status`` and ``fman experiment promote`` (todo #29).

The promotion rule is described in ``worker_experiments`` ("Experiments and promotion").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from foreman_inator.config import Config
from foreman_inator.db.store import Experiment, Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import shadow, variants
from foreman_inator.experiments import split as split_mod
from foreman_inator.worker_experiments import ExperimentError, _newest, _role

log = logging.getLogger("foreman_inator.experiments")


# =========================================================================== status


def _shadow_summary(store: Store, exp: Experiment, min_sample: int) -> str:
    try:
        c = shadow.score_experiment(store, exp.id)
    except Exception as exc:
        return f"not scored ({type(exc).__name__})"
    j = shadow.judge_live(c, min_sample=min_sample)
    why = f" ({'; '.join(j.reasons)})" if j.reasons else ""
    return (
        f"{c.paired} paired PR(s), {j.decided_pairs} decided by you (of {min_sample});"
        f" live: {j.status}{why}"
    )


def _replay_summary(store: Store, exp: Experiment, min_cases: int) -> str:
    try:
        st = rr.replay_status(store, exp.id, min_cases=min_cases)
    except LookupError:
        return "no replay run"
    c = st.comparison
    stop = f" ({st.stop_reason})" if st.stop_reason else ""
    return (
        f"{st.state}{stop}, {st.done_sides}/{st.total_sides} sides; {c.scored_cases} case(s)"
        f" scored, {c.wins}-{c.losses}-{c.ties}; replay: {c.verdict} ({c.reason})"
    )


def summary(store: Store, exp: Experiment, config: Config) -> str:
    if exp.mode == "replay":
        return _replay_summary(store, exp, config.experiments.min_cases)
    if exp.mode == "shadow" and exp.role == "reviewer":
        return _shadow_summary(store, exp, config.experiments.live_min_sample)
    if exp.mode == "split" and exp.role == "writer":
        try:
            return split_mod.summary(store, exp.id, min_sample=config.experiments.live_min_sample)
        except Exception as exc:
            return f"not scored ({type(exc).__name__})"
    if exp.mode == "seeded":  # #35: the note's first line; the details print it whole
        lines = (exp.result_note or "").splitlines()
        return lines[0] if lines else "not finished"
    return exp.result_note or ""


def status_lines(store: Store, config: Config, experiment_id: int | None = None) -> list[str]:
    """``fman experiment status [id]`` (reads only)."""
    if experiment_id is not None:
        exp = store.get_experiment(experiment_id)
        lines = [
            f"Experiment #{exp.id}: {exp.role} {exp.mode}, {exp.status}",
            f"  champion   {exp.champion_variant}",
            f"  challenger {exp.challenger_variant}",
            f"  origin     {_origin(exp)}",  # #49: who wrote the challenger, and its cutoff
            f"  started {exp.started_at:%Y-%m-%d %H:%M}"
            + (f", ended {exp.ended_at:%Y-%m-%d %H:%M}" if exp.ended_at else ""),
            f"  {summary(store, exp, config)}",
        ]
        if exp.mode in ("replay", "seeded") and exp.result_note:
            lines += ["", *exp.result_note.splitlines()]
        return lines
    lines = [f"Champion {line}" for line in variants.status_lines(store)]
    exps = store.list_experiments()
    running = [e for e in exps if e.status == "running"]
    ended = [e for e in exps if e.status != "running"][-10:]
    lines.append(f"Running experiments ({len(running)}):")
    lines += [_line(store, e, config) for e in running] or ["  (none)"]
    if ended:
        lines.append("Ended (latest 10):")
        lines += [_line(store, e, config) for e in ended]
    return lines


def _origin(e: Experiment) -> str:
    origin = getattr(e, "origin", "human") or "human"
    cutoff = getattr(e, "cutoff_at", None)
    if origin == "llm":
        origin = "LLM-drafted (approved by you)"
    return origin + (f", cutoff {cutoff:%Y-%m-%d}" if cutoff is not None else "")


def _line(store: Store, e: Experiment, config: Config) -> str:
    tag = f" [{_origin(e)}]" if getattr(e, "origin", "human") == "llm" else ""
    return (
        f"  #{e.id:<4} {e.role:<8} {e.mode:<6} {e.status:<8} {e.challenger_variant}"
        f" vs {e.champion_variant}{tag}: {summary(store, e, config)}"
    )


# =========================================================================== promote


@dataclass(frozen=True)
class PromoteResult:
    outcome: str  # promoted | rejected | refused
    message: str
    experiments: tuple[int, ...] = ()


def _target(store: Store, target: str) -> tuple[rr.Role, str]:
    if target.isdigit():
        exp = store.get_experiment(int(target))
        return _role(exp.role), exp.challenger_variant
    v = variants.lookup(store, target)
    if v is None:
        raise ExperimentError(f"no experiment or registered variant {target!r}")
    return _role(v.role), v.id


def _end(store: Store, exps: list[Experiment], status: str, why: str) -> tuple[int, ...]:
    for e in exps:
        note = None
        if e.mode == "replay":
            note = f"{e.result_note or ''}\n{status}: {why}".strip()
        store.end_experiment(e.id, status, note)  # type: ignore[arg-type]
        log.info("experiment %d: %s (%s)", e.id, status, why)
    return tuple(e.id for e in exps)


def promote(
    store: Store, config: Config, target: str, *, replay_only: bool = False
) -> PromoteResult:
    """SPEC's promotion rule; see the module docstring. Nothing else changes a champion."""
    role, vid = _target(store, target)
    if variants.lookup(store, vid) is None:
        return PromoteResult("refused", f"{vid} is not a registered variant")
    current = variants.champion(store, role)
    if current is not None and current.id == vid:
        return PromoteResult("refused", f"{vid} is already the {role} champion")
    if role == "reviewer" and replay_only:
        return PromoteResult(
            "refused", "--replay-only is for writers; a reviewer must also hold up in shadow"
        )
    rep = _newest(store, role, vid, "replay")
    sh = _newest(store, role, vid, "shadow") if role == "reviewer" else None
    sp = _newest(store, role, vid, "split") if role == "writer" else None  # #34
    ours = [e for e in (rep, sh, sp) if e is not None]
    if rep is None:
        return PromoteResult(
            "refused",
            f"no replay of {vid} against the champion: run "
            f"`fman experiment run --role {role} --challenger {vid}` first",
        )
    if current is None or rep.champion_variant != current.id:
        return PromoteResult(
            "refused",
            f"replay #{rep.id} compares against {rep.champion_variant}, not the current "
            f"champion {current.id if current else '(none)'}; run a new replay",
        )
    c = rr.compare(store, rep.id, min_cases=config.experiments.min_cases)
    if c.verdict == "does_not_win" and c.state == "finished":
        ids = _end(store, ours, "rejected", f"loses on replay ({c.reason})")
        return PromoteResult("rejected", f"Rejected {vid}: it does not win on replay ({c.reason}).",
                             ids)  # fmt: skip
    if c.state != "finished" or c.verdict != "wins":
        return PromoteResult(
            "refused",
            f"replay #{rep.id} is {c.state} with verdict {c.verdict} ({c.reason}); it must "
            "finish and win (resume it with `fman experiment run --all`)",
        )
    replay_part = f"wins replay #{rep.id} ({c.reason}; {c.wins}-{c.losses}-{c.ties})"
    if role == "writer":
        live_part = None
        why = "it has no traffic split against the current champion"
        if sp is not None and sp.champion_variant == current.id:
            _, j = split_mod.write_comparison(
                store, sp.id, min_sample=config.experiments.live_min_sample
            )
            why = f"split #{sp.id}: {'; '.join(j.reasons)}"
            if j.status == "fails":
                ids = _end(store, ours, "rejected", f"fails live in {why}")
                return PromoteResult("rejected", f"Rejected {vid}: it does not hold live ({why}).",
                                     ids)  # fmt: skip
            if j.status == "holds":
                live_part = f"holds live in split #{sp.id}"
        if live_part is None:
            if not replay_only:
                return PromoteResult(
                    "refused",
                    f"{vid} {replay_part}, but is not yet shown to hold live ({why}). Run a "
                    f"traffic split (`fman experiment split start {vid}`), or pass "
                    "--replay-only to promote it on the replay alone.",
                )
            live_part = f"promoted on replay only (--replay-only; {why})"
    else:
        if sh is None:
            return PromoteResult(
                "refused", f"{vid} {replay_part}, but has no shadow experiment to hold live in"
            )
        _, j = shadow.write_comparison(store, sh.id, min_sample=config.experiments.live_min_sample)
        why = "; ".join(j.reasons)
        if j.status == "fails":
            ids = _end(store, ours, "rejected", f"fails live in shadow #{sh.id} ({why})")
            return PromoteResult("rejected", f"Rejected {vid}: it does not hold live ({why}).", ids)
        if j.status != "holds":
            return PromoteResult(
                "refused", f"{vid} {replay_part}; shadow #{sh.id}: {why}. Not promoted yet."
            )
        live_part = f"holds live in shadow #{sh.id} ({j.decided_pairs} decided PRs)"
    note = f"promoted by `fman experiment promote`: {replay_part}; {live_part}"
    with store.transaction():
        variants.set_champion(store, role, vid, note=note)
        ids = _end(store, ours, "promoted", note)
    log.info("%s champion is now %s (%s)", role, vid, note)
    return PromoteResult("promoted", f"{role} champion is now {vid}: {replay_part}; {live_part}.",
                         ids)  # fmt: skip


__all__ = ["PromoteResult", "promote", "status_lines", "summary"]
