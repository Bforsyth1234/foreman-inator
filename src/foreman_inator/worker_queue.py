"""The worker's queue and dispatch (todo #21): which job each ticket needs next.

``QueueSteps`` is mixed into ``Worker``. The queue is derived from the store on every
pass, never kept in memory, so a restarted worker picks up exactly where the dead one
stopped (``Worker`` docstring, "Crash safety"). The one piece of memory, the set of
reviews already finalized, is empty after a restart on purpose: every open ticket's
latest review is finalized once more, idempotently.

Per ticket (``job_for``), first match wins:

=================================================  ==========  =========================
State                                              Job         Lock gate (#19)
=================================================  ==========  =========================
latest review not finalized in this process        finalize    no
flagged (handed over, spend pause, no verify...)   (none)      —
``todo`` waiting on ``needs-info`` (#44)           (none)      —
``todo``, no ``routed_tier`` or clarify not done   route       no
last attempt still running                         (none)      —
``in_progress`` with a rebase pending (#43)        rebase      no
``in_progress``, last attempt passed, no outcome   publish     ``may_start``
``testing``, last attempt passed, no review row    review      no (waits at the CI gate)
``testing`` otherwise                              (none)      — (waits for you)
escalation gives a step (#16)                      attempt     ``may_start``, except a
                                                               hand-over
=================================================  ==========  =========================

Background work (wave 9): when a repo has no job running and none due, the shadow
reviews it owes the running challenger are queued on the shadow lane
(``worker_background``), which runs beside the job slots and never takes one.

A ticket whose lock gate fails *waits*: no attempt is counted and nothing is flagged; it
starts as soon as the holder releases. Per repo at most ``config.max_parallel_per_repo``
jobs run at once (one per ticket); mirror operations stay serialised per repo
(``SerialMirror``), whatever the cap.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from foreman_inator.db.store import Ticket
from foreman_inator.routing import escalation
from foreman_inator.runner.worktree import RepoMirror

log = logging.getLogger("foreman_inator.worker")

JOIN_TIMEOUT = 90.0  # shutdown: how long to wait for running jobs to unwind
BACKOFF_START, BACKOFF_MAX = 30.0, 900.0  # a repo whose job raised waits, doubling

JobKind = Literal["route", "attempt", "publish", "review", "finalize", "rebase"]
_ORDER = {"in_progress": 0, "testing": 1, "todo": 2}


@dataclass(frozen=True)
class Job:
    repo: str
    ticket_id: int
    kind: JobKind


@dataclass
class _Running:
    job: Job
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class SerialMirror(RepoMirror):
    """A ``RepoMirror`` whose mutating git operations hold one lock per repo (#19 note 4).

    Worktrees made through it keep a reference to it, so their ``remove()`` is
    serialised too, including the reviewer's scratch worktrees. Pushes and commits in a
    worktree are not: they touch only that worktree's own branch."""

    def __init__(self, *args: Any, serial: threading.RLock, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._serial = serial

    def ensure(self, token: str | None = None) -> Any:
        with self._serial:
            return super().ensure(token)

    def fetch(self, token: str | None = None) -> None:
        with self._serial:
            super().fetch(token)

    def prune(self) -> None:
        with self._serial:
            super().prune()

    def create(self, *args: Any, **kw: Any) -> Any:
        with self._serial:
            return super().create(*args, **kw)

    def scratch(self, *args: Any, **kw: Any) -> Any:
        with self._serial:
            return super().scratch(*args, **kw)

    def cleanup_issue(self, issue: int) -> None:
        with self._serial:
            super().cleanup_issue(issue)

    def _remove_path(self, path: Any) -> None:
        with self._serial:
            super()._remove_path(path)

    def _delete_local_branch(self, branch: str) -> None:
        with self._serial:
            super()._delete_local_branch(branch)

    def remove_scratch(self) -> int:
        """Remove leftover scratch worktrees (a reviewer killed mid-review). Call only
        when no job of this repo runs."""
        n = 0
        for p in sorted(self.repo_dir.glob("scratch-*")):
            self._remove_path(p)
            n += 1
        return n


class QueueSteps:
    # Provided by Worker.
    store: Any
    config: Any
    worktrees: Any
    locks: Any
    repos: tuple[str, ...]
    parallel: int
    clock: Any
    clone_url: Any
    _stop: threading.Event
    _lock: threading.Lock
    _finalized: set[int]

    def _init_queue(self) -> None:
        self._running: dict[int, _Running] = {}  # ticket id -> its running job
        self._cooldown: dict[str, float] = {}  # repo -> monotonic time it may work again
        self._backoff: dict[str, float] = {}  # repo -> last error delay
        self._sweep: set[str] = set()
        self._repo_locks: dict[str, threading.RLock] = {}
        self._waiting: dict[int, tuple[int, ...]] = {}  # ticket -> lock blockers (logging)

    # ------------------------------------------------------------------ mirrors

    def _mirror(self, repo: str) -> SerialMirror:
        with self._lock:
            serial = self._repo_locks.setdefault(repo, threading.RLock())
        return SerialMirror(
            self.worktrees.root,
            repo,
            self.clone_url(repo),
            identity=self.worktrees.identity,
            serial=serial,
        )

    # ------------------------------------------------------------------ the queue

    def next_job(self, repo: str, *, exclude: set[int] | None = None) -> Job | None:
        """The repo's next piece of work, derived from the store (crash safe)."""
        tickets = self.store.list_tickets(column=tuple(_ORDER), repo=repo)
        tickets.sort(key=lambda t: (_ORDER[t.column], t.id))
        for t in tickets:
            if exclude and t.id in exclude:
                continue
            kind = self.job_for(t)
            if kind is not None:
                return Job(repo, t.id, kind)
        return None

    def job_for(self, t: Ticket) -> JobKind | None:
        """What ``t`` needs next (the table in the module docstring), or None."""
        reviews = self.store.list_reviews(ticket_id=t.id, shadow=False)
        if reviews and reviews[-1].id not in self._finalized:
            return "finalize"
        if t.flag_reason:
            return None
        if t.column == "todo":
            if self.clarify_parked(t):  # type: ignore[attr-defined]
                return None
            if t.routed_tier is None or self.clarify_due(t):  # type: ignore[attr-defined]
                return "route"
        attempts = self.store.list_attempts(t.id)
        last = attempts[-1] if attempts else None
        if last is not None and last.ended_at is None:
            return None
        if t.column == "in_progress" and self.rebase_pending(t, attempts):  # type: ignore[attr-defined]
            return "rebase"
        if last is not None and last.outcome is None and last.tests_passed:
            if t.column == "in_progress":
                return "publish" if self._lock_gate(t) else None
            if t.column == "testing" and not any(r.attempt_id == last.id for r in reviews):
                return "review"
            return None
        if t.column == "testing":
            return None
        step = self.policy_for(t)  # type: ignore[attr-defined]
        if step is None:
            return None
        if isinstance(step, escalation.HandToUser) or self._lock_gate(t):
            return "attempt"
        return None

    def _lock_gate(self, t: Ticket) -> bool:
        """May ``t`` start (or publish) now? Locks its wanted files if so (#19)."""
        r = self.locks.may_start(t)
        if r.ok:
            if self._waiting.pop(t.id, None) is not None:
                log.info("ticket %d: file locks free; resuming", t.id)
            return True
        if self._waiting.get(t.id) != r.blockers:
            self._waiting[t.id] = r.blockers
            log.info(
                "ticket %d: waiting on files held by ticket(s) %s",
                t.id,
                ", ".join(f"#{b}" for b in r.blockers),
            )
        return False

    # ------------------------------------------------------------------ dispatch

    def _running_in(self, repo: str) -> list[_Running]:
        with self._lock:
            return [r for r in self._running.values() if r.job.repo == repo]

    def dispatch(self, *, inline: bool) -> None:
        for repo in self.repos:
            if self._stop.is_set():
                return
            if self.clock() < self._cooldown.get(repo, 0.0):
                continue
            self.resume_paused(repo)  # type: ignore[attr-defined]
            if repo in self._sweep and not self._running_in(repo):
                self._sweep.discard(repo)
                self._sweep_worktrees(repo)
            started: set[int] = set()
            while not self._stop.is_set():
                with self._lock:
                    busy = {tid for tid, r in self._running.items() if r.job.repo == repo}
                if len(busy) >= self.parallel or (inline and len(started) >= self.parallel):
                    break
                job = self.next_job(repo, exclude=busy | started)
                if job is None:
                    if not busy and not self._running_in(repo):
                        # Nothing live runs or is due: background work may go (#26).
                        self.queue_shadows(repo)  # type: ignore[attr-defined]
                    break
                started.add(job.ticket_id)
                run = _Running(job)
                with self._lock:
                    self._running[job.ticket_id] = run
                if inline:
                    self._run_job(run)
                else:
                    run.thread = threading.Thread(
                        target=self._run_job, args=(run,), name=f"fman-{job.kind}-{job.ticket_id}"
                    )
                    run.thread.start()

    def _run_job(self, run: _Running) -> None:
        job = run.job
        handler = {
            "route": self.route_job,  # type: ignore[attr-defined]
            "attempt": self.attempt_job,  # type: ignore[attr-defined]
            "publish": self.publish_job,  # type: ignore[attr-defined]
            "review": self.review_job,  # type: ignore[attr-defined]
            "finalize": self.finalize_job,  # type: ignore[attr-defined]
            "rebase": self.rebase_job,  # type: ignore[attr-defined]
        }[job.kind]
        try:
            handler(job, run.cancel)
            self._backoff.pop(job.repo, None)
        except Exception as exc:
            delay = min(BACKOFF_MAX, max(BACKOFF_START, self._backoff.get(job.repo, 0.0) * 2))
            self._backoff[job.repo] = delay
            self._cooldown[job.repo] = self.clock() + delay
            log.warning(
                "ticket %d: %s job failed (%s: %s); %s waits %.0fs",
                job.ticket_id,
                job.kind,
                type(exc).__name__,
                exc,
                job.repo,
                delay,
            )
        finally:
            with self._lock:
                self._running.pop(job.ticket_id, None)

    def hold_repo(self, repo: str, seconds: float | None = None) -> None:
        """Start no job for ``repo`` for a while (default: one poll interval)."""
        wait = float(self.config.poll_interval_seconds if seconds is None else seconds)
        self._cooldown[repo] = self.clock() + wait

    def _sweep_worktrees(self, repo: str) -> None:
        """Remove worktrees of issues with no open ticket (after merge, drop or a crash),
        and scratch worktrees a killed review left behind."""
        mirror = self._mirror(repo)
        issues: set[int] = set()
        for p in mirror.repo_dir.glob("issue-*-a*"):
            with contextlib.suppress(ValueError):
                issues.add(int(p.name.split("-")[1]))
        for n in sorted(issues):
            if self.store.find_open_ticket(repo, n) is None:
                mirror.cleanup_issue(n)
                log.info("%s#%d: worktrees removed", repo, n)
        if mirror.mirror_path.exists() and mirror.remove_scratch():
            log.info("%s: leftover review worktrees removed", repo)


__all__ = ["BACKOFF_MAX", "BACKOFF_START", "JOIN_TIMEOUT", "Job", "QueueSteps", "SerialMirror"]
