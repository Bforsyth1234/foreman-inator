"""Retention (#56): what ``fman prune`` and the worker's daily prune delete, and when.

Everything here deletes user data, so every rule errs toward keeping:

* **Recordings and traces** (``recordings/<ticket>/``, ``traces/<ticket>/``): a file is
  deleted once its mtime is older than ``days`` (default 90), unless its ticket is
  *kept*: merged (``accepted_attempt_id`` set), still open (todo, in progress, testing),
  or with an attempt still running. ``tracing.recorder.prune`` does the deleting.
* **Chat recordings** (``recordings/chat/<session>.jsonl.gz``, #71): the same age rule,
  for ended sessions only (``ended``, ``discarded``, ``failed``) and recordings with no
  session row. A session still open, or opened as a PR, is kept (whether that PR merged
  is not tracked, so it is kept like a merged ticket). ``recorder.prune_chats`` deletes.
* **Scratch space** (``sandbox/<key>/``, ``opencode/attempts/<dir>/``): one entry per
  launch, normally removed by the launch itself; what is left is a crash's leftovers. An
  entry named after an attempt (``a12-writer``, ``r12-work``, ``a12-3f9a1c``) or a replay
  experiment (``replay-e4-...``) goes once that attempt has ended (or is unknown to the
  store) or that experiment is no longer running, and nothing in it changed for
  ``stale_hours`` (default 24). Never for a running attempt or experiment, never for an
  attempt of a merged ticket. An entry with no id in its name goes after ``days``.
* **Experiment work** (``replays/work/e<id>/``, verify logs of replay runs): once the
  experiment is not running and the entry is older than ``days``.
* **Package caches** (``cache/<owner>/<name>/``): only for repos no longer in the config,
  and only when a repo list is given (an empty list removes nothing). Never for a repo
  with an attempt running.

"Older than" is the newest mtime of the entry and its direct children. ``dry_run``
returns the same list and touches nothing. Errors are collected in the report, never
raised: one unreadable folder does not stop the rest.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from foreman_inator.db.store import CHAT_ENDED_STATES, OPEN_COLUMNS, Store
from foreman_inator.tracing import recorder

RETENTION_DAYS = recorder.RETENTION_DAYS
#: A scratch entry of an ended attempt is left alone this long after its last change
#: (a review runs after its attempt ended, under ``r<attempt>`` keys).
SCRATCH_STALE_HOURS = 24.0

_ATTEMPT_KEY = re.compile(r"^(?:a|r|rs)(\d+)(?:-|$)")
_REPLAY_KEY = re.compile(r"^replay-e(\d+)-")
_EXPERIMENT_WORK = re.compile(r"^e(\d+)$")


@dataclass(frozen=True)
class Removal:
    path: Path
    reason: str


@dataclass
class PruneReport:
    dry_run: bool
    days: float
    removed: list[Removal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """Human-readable output for ``fman prune`` (one line per path)."""
        verb = "would delete" if self.dry_run else "deleted"
        out = [f"{verb} {r.path}  ({r.reason})" for r in self.removed]
        if not self.removed:
            out.append(f"nothing to prune (retention {self.days:g} days)")
        else:
            out.append(f"{verb} {len(self.removed)} path(s) (retention {self.days:g} days)")
        out.extend(f"error: {e}" for e in self.errors)
        return out


@dataclass(frozen=True)
class _State:
    """What the store says, read once per run."""

    kept_tickets: frozenset[int]
    merged_tickets: frozenset[int]
    attempt_ticket: dict[int, int]  # attempt id -> ticket id (every attempt)
    running_attempts: frozenset[int]
    running_repos: frozenset[str]  # lowercased
    running_experiments: frozenset[int]

    def attempt_busy(self, attempt_id: int) -> bool:
        """Running, or one of a merged ticket's attempts: never swept."""
        if attempt_id in self.running_attempts:
            return True
        return self.attempt_ticket.get(attempt_id) in self.merged_tickets


def _state(store: Store) -> _State:
    tickets = store.list_tickets()
    merged = {t.id for t in tickets if t.accepted_attempt_id is not None}
    open_ = {t.id for t in tickets if t.column in OPEN_COLUMNS}
    repo_of = {t.id: t.repo.lower() for t in tickets}
    rows = store.query("SELECT id, ticket_id, ended_at FROM attempts")
    attempt_ticket = {int(r["id"]): int(r["ticket_id"]) for r in rows}
    running = {int(r["id"]) for r in rows if r["ended_at"] is None}
    running_tickets = {attempt_ticket[a] for a in running}
    return _State(
        kept_tickets=frozenset(merged | open_ | running_tickets),
        merged_tickets=frozenset(merged),
        attempt_ticket=attempt_ticket,
        running_attempts=frozenset(running),
        running_repos=frozenset(repo_of[t] for t in running_tickets if t in repo_of),
        running_experiments=frozenset(e.id for e in store.list_experiments(status="running")),
    )


def _kept_chats(store: Store) -> frozenset[int]:
    """Chat sessions whose recording stays: still open, or opened as a PR."""
    prunable = set(CHAT_ENDED_STATES) - {"opened_pr"}
    return frozenset(s.id for s in store.list_chat_sessions() if s.state not in prunable)


def _last_change(p: Path) -> float:
    """Newest mtime of ``p`` and its direct children (lstat: links are not followed)."""
    newest = p.lstat().st_mtime
    if p.is_dir() and not p.is_symlink():
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
        except OSError:
            pass
    return newest


def _force_writable(func, path, _exc) -> None:  # noqa: ANN001 - shutil's onexc signature
    """Tools leave read-only trees in scratch (Go's module cache): make writable, retry."""
    parent = os.path.dirname(path)
    for p in (parent, path):
        with contextlib.suppress(OSError):
            os.chmod(p, os.lstat(p).st_mode | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR)
    func(path)


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, onexc=_force_writable)
    else:
        p.unlink()


class _Run:
    def __init__(self, report: PruneReport, now: float) -> None:
        self.report = report
        self.now = now

    def older(self, p: Path, seconds: float) -> bool:
        return _last_change(p) < self.now - seconds

    def drop(self, p: Path, reason: str) -> None:
        if not self.report.dry_run:
            try:
                _remove(p)
            except OSError as exc:
                self.report.errors.append(f"{p}: {exc}")
                return
        self.report.removed.append(Removal(p, reason))

    def entries(self, root: Path) -> Iterable[Path]:
        if not root.is_dir() or root.is_symlink():
            return []
        try:
            return sorted(root.iterdir())
        except OSError as exc:
            self.report.errors.append(f"{root}: {exc}")
            return []


def _sweep_scratch(run: _Run, root: Path, st: _State, days: float, stale: float) -> None:
    """``sandbox/<key>`` and ``opencode/attempts/<dir>`` entries."""
    for p in run.entries(root):
        if m := _ATTEMPT_KEY.match(p.name):
            aid = int(m.group(1))
            if st.attempt_busy(aid) or not run.older(p, stale):
                continue
            ended = "ended" if aid in st.attempt_ticket else "unknown"
            run.drop(p, f"scratch of {ended} attempt {aid}")
        elif m := _REPLAY_KEY.match(p.name):
            eid = int(m.group(1))
            if eid in st.running_experiments or not run.older(p, stale):
                continue
            run.drop(p, f"scratch of finished experiment {eid}")
        elif run.older(p, days * 86400):
            run.drop(p, f"scratch older than {days:g} days")


def _sweep_replay_work(run: _Run, root: Path, st: _State, days: float) -> None:
    for p in run.entries(root):
        m = _EXPERIMENT_WORK.match(p.name)
        if m and int(m.group(1)) in st.running_experiments:
            continue
        if run.older(p, days * 86400):
            run.drop(p, f"experiment work older than {days:g} days")


def _sweep_caches(run: _Run, root: Path, st: _State, repos: Iterable[str] | None) -> None:
    wanted = {r.lower() for r in repos or ()}
    if not wanted:  # no repo list: cannot tell which caches are orphaned
        return
    for owner in run.entries(root):
        if not owner.is_dir() or owner.is_symlink():
            continue
        for repo_dir in run.entries(owner):
            name = f"{owner.name}/{repo_dir.name}"
            if name in wanted or name in st.running_repos:
                continue
            run.drop(repo_dir, f"package cache of {name}, no longer in the config")
        if not run.report.dry_run:
            with contextlib.suppress(OSError):
                owner.rmdir()  # only if now empty


def run(
    store: Store,
    home: str | Path,
    days: float = RETENTION_DAYS,
    dry_run: bool = False,
    now: float | None = None,
    *,
    repos: Iterable[str] | None = None,
    recordings_dir: str | Path | None = None,
    traces_dir: str | Path | None = None,
    stale_hours: float = SCRATCH_STALE_HOURS,
) -> PruneReport:
    """Prune ``home`` (the foreman-inator folder) by the rules in the module docstring.

    ``now`` is a Unix timestamp (default: the store's clock). ``recordings_dir`` and
    ``traces_dir`` default to ``home/recordings`` and ``home/traces``. ``repos`` is the
    config's repo list (for the package caches). ``days`` must be at least 1.
    """
    if days < 1:
        raise ValueError(f"retention must be at least 1 day, not {days:g}")
    home = Path(home)
    now = store.now().timestamp() if now is None else now
    report = PruneReport(dry_run=dry_run, days=days)
    st = _state(store)
    r = _Run(report, now)

    rec_dir = Path(recordings_dir) if recordings_dir is not None else home / "recordings"
    tr_dir = Path(traces_dir) if traces_dir is not None else home / "traces"
    try:
        gone = recorder.prune(
            rec_dir,
            merged=st.kept_tickets.__contains__,
            traces_dir=tr_dir,
            max_age_days=days,
            now=now,
            dry_run=dry_run,
        )
    except OSError as exc:
        report.errors.append(f"recordings/traces: {exc}")
        gone = []
    for p in gone:
        report.removed.append(Removal(p, f"unmerged ticket, older than {days:g} days"))
    try:
        gone = recorder.prune_chats(
            rec_dir,
            merged=_kept_chats(store).__contains__,
            max_age_days=days,
            now=now,
            dry_run=dry_run,
        )
    except OSError as exc:
        report.errors.append(f"chat recordings: {exc}")
        gone = []
    for p in gone:
        report.removed.append(Removal(p, f"ended chat session, older than {days:g} days"))

    stale = stale_hours * 3600
    _sweep_scratch(r, home / "sandbox", st, days, stale)
    _sweep_scratch(r, home / "opencode" / "attempts", st, days, stale)
    _sweep_replay_work(r, home / "replays" / "work", st, days)
    _sweep_caches(r, home / "cache", st, repos)
    return report


__all__ = ["RETENTION_DAYS", "SCRATCH_STALE_HOURS", "PruneReport", "Removal", "run"]
