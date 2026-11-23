"""The worker loop, v1 (todo #14, SPEC milestone 1): one tier, end to end.

``Worker`` joins the pieces built so far. Every collaborator is injected (GitHub API,
store, config, worktree manager, OpenCode driver, model proxy, publisher, poller, clock),
and ``run()`` at the bottom builds the real ones from ``config.load()`` and ``paths``.

Loop order (``tick``)
---------------------
1. **Poll and handle events** (main thread, quick). ``Poller.process`` turns GitHub state
   into lifecycle events; each is handled inside the poller's store transaction:
   ``lifecycle.apply`` gives the new column and the ``Action`` list, and store-only
   actions run there. GitHub writes an action needs (flag comment, close issue, clear
   the kickback label) are deferred until that event's transaction has committed, so a
   rolled-back event never leaves a comment behind.
2. **Dispatch work** (one job per repo at a time). The queue is derived from the store
   every time, never kept in memory: per repo, the oldest ``in_progress`` ticket needing
   work, then the oldest ``todo`` one; flagged tickets wait for the user. A job is an
   *attempt* (worktree → writer prompt → OpenCode → commit → verify) or a *publish*
   (push → draft PR → v1 ready). ``serve_forever`` runs each job on its own thread, so a
   20-minute attempt never blocks polling or other repos; ``run_once`` runs them inline.

No long work happens inside a SQLite transaction: transactions only wrap store writes.

State changes always go through ``lifecycle.apply``; what each ``Action`` does is the
``self.actions`` table. #21 swaps entries (``NEXT_ATTEMPT`` → escalation, ``AI_REVIEW`` →
the reviewer, ``ROUTE`` → the router, ``POST_SUMMARY`` → #20) and the ``policy``
function, not the loop.

Crash safety
------------
The worker can die anywhere; launchd restarts it. ``recover()`` (called by ``run()``
before the first tick) marks every attempt with no ``ended_at`` as ``superseded``; the
queue then re-runs it with the same trigger, at most ``MAX_INTERRUPTED`` times per
ticket before flagging. An attempt that passed its checks but whose PR step did not
finish is re-published: the push is a force-push of the same commit and the publisher
adopts an existing PR (by stored number or by branch), so no PR is opened twice. The
column moves to ``testing`` only after the PR is open and marked ready.

Shutdown: SIGINT/SIGTERM set the stop flag and every running job's cancel event; the
OpenCode driver aborts its session and kills its process group, verify kills its
command, the cut-short attempt is stored as ``superseded``, the proxy stops, exit 0.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from foreman_inator import tracing
from foreman_inator.config import Config
from foreman_inator.db.store import OPEN_COLUMNS, Attempt, Store, Ticket
from foreman_inator.github.client import GitHubAPI
from foreman_inator.github.poller import Poller
from foreman_inator.pr.publisher import Publisher
from foreman_inator.runner.worktree import WorktreeManager
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import Action, IllegalTransition
from foreman_inator.worker_actions import V1_READY_ON_CHECKS, ActionHandlers, Ctx, flag_comment
from foreman_inator.worker_attempt import AttemptSteps
from foreman_inator.worker_policy import (
    Step,
    next_step,
)

log = logging.getLogger("foreman_inator.worker")

JOIN_TIMEOUT = 90.0  # shutdown: how long to wait for running jobs to unwind
BACKOFF_START, BACKOFF_MAX = 30.0, 900.0  # a repo whose job raised waits, doubling

Policy = Callable[[list[Attempt]], Step | None]


@dataclass(frozen=True)
class Job:
    repo: str
    ticket_id: int
    kind: Literal["attempt", "publish"]


@dataclass
class _Running:
    job: Job
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class Worker(ActionHandlers, AttemptSteps):
    def __init__(
        self,
        *,
        api: GitHubAPI,
        store: Store,
        config: Config,
        worktrees: WorktreeManager,
        driver: Any,
        proxy: Any,
        recordings_dir: Path,
        traces_dir: Path,
        publisher: Publisher | None = None,
        poller: Poller | None = None,
        clone_url: Callable[[str], str] | None = None,
        policy: Policy = next_step,
        clock: Callable[[], float] = time.monotonic,
        attempt_timeout: float | None = None,
        verify_timeout: float = 20 * 60,
    ) -> None:
        self.api = api
        self.store = store
        self.config = config
        self.worktrees = worktrees
        self.driver = driver
        self.proxy = proxy
        self.recordings_dir = Path(recordings_dir)
        self.traces_dir = Path(traces_dir)
        self.publisher = publisher or Publisher(api, store)
        self.poller = poller or Poller(api, store, config)
        self.repos = self.poller.repos
        self.clone_url = clone_url or (lambda repo: f"https://github.com/{repo}.git")
        self.policy = policy
        self.clock = clock
        self.attempt_timeout = attempt_timeout
        self.verify_timeout = verify_timeout
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._running: dict[str, _Running] = {}
        self._cooldown: dict[str, float] = {}  # repo -> monotonic time it may work again
        self._backoff: dict[str, float] = {}  # repo -> last error delay
        self._sweep: set[str] = set()
        self._local = threading.local()
        self.actions: dict[Action, Callable[[Ctx], None]] = {
            Action.CREATE_TICKET: self._a_create_ticket,
            Action.APPLY_TIER_OVERRIDE: self._a_tier_override,
            Action.ROUTE: self._a_route,
            Action.START_ATTEMPT: self._a_start_attempt,
            Action.UPDATE_REQUEST: self._a_update_request,
            Action.OPEN_OR_UPDATE_DRAFT_PR: self._a_open_or_update_pr,
            Action.AI_REVIEW: self._a_ai_review,
            Action.MARK_READY_FOR_REVIEW: self._a_mark_ready,
            Action.RECORD_KICKBACK: self._a_record_kickback,
            Action.CLEAR_KICKBACK_LABEL: self._a_clear_kickback_label,
            Action.NEXT_ATTEMPT: self._a_next_attempt,
            Action.RECORD_APPROVAL: self._a_record_approval,
            Action.RECORD_ACCEPT: self._a_record_accept,
            Action.CLOSE_ISSUE: self._a_close_issue,
            Action.POST_SUMMARY: self._a_post_summary,
            Action.CANCEL_ATTEMPT: self._a_cancel_attempt,
            Action.RELEASE_LOCKS: self._a_release_locks,
            Action.CLEANUP_WORKTREE: self._a_cleanup_worktree,
            Action.FLAG_TICKET: self._a_flag_ticket,
        }

    # ================================================================== the loop

    def recover(self) -> list[int]:
        """Startup: attempts left running by a dead process become ``superseded``."""
        fixed: list[int] = []
        for t in self.store.list_tickets(column=OPEN_COLUMNS):
            for a in self.store.list_attempts(t.id):
                if a.ended_at is None:
                    self.store.finish_attempt(a.id, "superseded")
                    fixed.append(a.id)
                    log.info("ticket %d: attempt %d was interrupted; marked superseded", t.id, a.id)
        self._sweep.update(self.repos)
        return fixed

    def run_once(self) -> None:
        """One pass: poll and handle events, then at most one job per repo, inline."""
        self.tick(inline=True)

    def tick(self, *, inline: bool) -> None:
        self.poll_and_handle()
        self.dispatch(inline=inline)

    def serve_forever(self) -> None:
        """Poll every ``poll_interval_seconds``; start jobs as soon as a repo is idle."""
        interval = float(self.config.poll_interval_seconds)
        next_poll = 0.0
        log.info("worker started: %d repo(s), polling every %.0fs", len(self.repos), interval)
        while not self._stop.is_set():
            if self.clock() >= next_poll:
                self.poll_and_handle()
                next_poll = self.clock() + interval
            self.dispatch(inline=False)
            self._stop.wait(min(1.0, max(0.0, next_poll - self.clock())))
        self.shutdown()

    def request_stop(self, why: str = "stop requested") -> None:
        if not self._stop.is_set():
            log.info("worker stopping: %s", why)
        self._stop.set()
        with self._lock:
            for run in self._running.values():
                run.cancel.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def shutdown(self, timeout: float = JOIN_TIMEOUT) -> None:
        """Cancel running jobs and wait for them to unwind (each stores its attempt)."""
        self.request_stop("shutdown")
        with self._lock:
            threads = [r.thread for r in self._running.values() if r.thread is not None]
        deadline = time.monotonic() + timeout
        for th in threads:
            th.join(max(0.0, deadline - time.monotonic()))
            if th.is_alive():
                log.warning("job thread %s did not stop in time", th.name)

    # ------------------------------------------------------------------ events

    def poll_and_handle(self) -> None:
        self._local.pending, ready = [], []
        self._local.ready = ready
        try:
            result = self.poller.process(self._handle_event)
        except Exception as exc:
            log.warning("poll stopped by an error: %s: %s", type(exc).__name__, exc)
            result = None
            self._local.pending = []  # that event's transaction rolled back
        finally:
            ready.extend(self._local.pending)
            self._local.pending = self._local.ready = None
        for _ticket_id, name, effect in ready:
            try:
                effect()
            except Exception as exc:
                log.warning("GitHub write %s failed: %s: %s", name, type(exc).__name__, exc)
        if result is not None:
            for repo, exc in result.errors.items():
                log.warning("poll of %s failed: %s: %s", repo, type(exc).__name__, exc)

    def _handle_event(self, event: Any) -> None:
        """One poller event, inside its transaction (``Poller.process``)."""
        # The previous event's transaction committed: its GitHub writes may now run.
        self._local.ready.extend(self._local.pending)
        self._local.pending = []
        tid = getattr(event, "ticket_id", None)
        ticket = self.store.get_ticket(tid) if tid is not None else None
        column = ticket.column if ticket else None
        try:
            tr = lifecycle.apply(column, event)
        except IllegalTransition as exc:
            log.warning("ignored %s#%d: %s", event.repo, event.issue_number, exc)
            return
        if ticket is not None and tr.column != column:
            ticket = self.store.set_column(ticket.id, tr.column)
            self._log_move(ticket, column, tr.column, event)
        self._run_actions(Ctx(ticket, event), tr.actions)

    # ------------------------------------------------------------------ transitions

    def _commit(self, ticket_id: int, event: Any) -> tuple[Ticket, lifecycle.Transition] | None:
        """Apply a worker event to the ticket's *current* column and store the move."""
        with self.store.transaction():
            t = self.store.get_ticket(ticket_id)
            try:
                tr = lifecycle.apply(t.column, event)
            except IllegalTransition as exc:
                log.info("ticket %d: %s", ticket_id, exc)
                return None
            if tr.column != t.column:
                old = t.column
                t = self.store.set_column(ticket_id, tr.column)
                self._log_move(t, old, tr.column, event)
            return t, tr

    def _event(self, ticket_id: int, event: Any, **ctx: Any) -> Ticket | None:
        """Commit a worker event, then carry out its actions."""
        done = self._commit(ticket_id, event)
        if done is None:
            return None
        ticket, tr = done
        self._run_actions(Ctx(ticket, event, **ctx), tr.actions)
        return ticket

    def _run_actions(self, ctx: Ctx, actions: tuple[Action, ...]) -> None:
        with contextlib.ExitStack() as stack:
            if ctx.ticket is not None:
                stack.enter_context(self._trace(ctx.ticket))
            for action in actions:
                self.actions[action](ctx)
                if action is Action.CREATE_TICKET and ctx.ticket is not None:
                    t = ctx.ticket
                    tt = stack.enter_context(
                        tracing.ticket_span(
                            t.id, title=t.title, repo=t.repo, issue_number=t.issue_number
                        )
                    )
                    ctx.ticket = self.store.update_ticket(t.id, trace_id=tt.trace_id)
                if ctx.stop:
                    break

    @contextlib.contextmanager
    def _trace(self, ticket: Ticket) -> Iterator[None]:
        if ticket.trace_id:
            with tracing.ticket_span(ticket.id, trace_id=ticket.trace_id):
                yield
        else:
            yield

    def _log_move(self, t: Ticket, old: str | None, new: str, event: Any) -> None:
        log.info(
            "ticket %d (%s#%d): %s -> %s on %s",
            t.id,
            t.repo,
            t.issue_number,
            old,
            new,
            type(event).__name__,
        )

    def _gh(
        self,
        ticket: Ticket,
        name: str,
        fn: Callable[[], Any],
        *,
        attempt_id: int | None = None,
        pr_number: int | None = None,
    ) -> Any:
        """Run one GitHub write inside a ``github`` span; deferred while handling a poll
        event, until that event's transaction commits."""

        def effect() -> Any:
            with (
                self._trace(ticket),
                tracing.github_span(name, attempt_id=attempt_id, pr_number=pr_number),
            ):
                return fn()

        pending = getattr(self._local, "pending", None)
        if pending is not None:
            pending.append((ticket.id, name, effect))
            return None
        return effect()

    # ================================================================== the queue

    def next_job(self, repo: str) -> Job | None:
        """The repo's next piece of work, derived from the store (crash safe)."""
        tickets = self.store.list_tickets(column=("todo", "in_progress"), repo=repo)
        tickets.sort(key=lambda t: (t.column != "in_progress", t.id))
        for t in tickets:
            if t.flag_reason:
                continue
            attempts = self.store.list_attempts(t.id)
            last = attempts[-1] if attempts else None
            if last is not None and last.ended_at is None:
                return None  # an attempt is running: one ticket per repo at a time
            if last is not None and last.outcome is None and last.tests_passed:
                return Job(repo, t.id, "publish")
            if self.policy(attempts) is not None:
                return Job(repo, t.id, "attempt")
        return None

    def dispatch(self, *, inline: bool) -> None:
        for repo in self.repos:
            if self._stop.is_set():
                return
            with self._lock:
                if repo in self._running:
                    continue
            if self.clock() < self._cooldown.get(repo, 0.0):
                continue
            if repo in self._sweep:
                self._sweep.discard(repo)
                self._sweep_worktrees(repo)
            job = self.next_job(repo)
            if job is None:
                continue
            run = _Running(job)
            with self._lock:
                self._running[repo] = run
            if inline:
                self._run_job(run)
            else:
                run.thread = threading.Thread(
                    target=self._run_job, args=(run,), name=f"fman-job-{repo}"
                )
                run.thread.start()

    def _run_job(self, run: _Running) -> None:
        job = run.job
        try:
            if job.kind == "publish":
                self.publish_job(job, run.cancel)
            else:
                self.attempt_job(job, run.cancel)
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
                self._running.pop(job.repo, None)

    def hold_repo(self, repo: str, seconds: float | None = None) -> None:
        """Start no job for ``repo`` for a while (default: one poll interval)."""
        wait = float(self.config.poll_interval_seconds if seconds is None else seconds)
        self._cooldown[repo] = self.clock() + wait

    def _sweep_worktrees(self, repo: str) -> None:
        """Remove worktrees of issues with no open ticket (after merge, drop or a crash)."""
        mirror = self.worktrees.mirror(repo, self.clone_url(repo))
        issues: set[int] = set()
        for p in mirror.repo_dir.glob("issue-*-a*"):
            with contextlib.suppress(ValueError):
                issues.add(int(p.name.split("-")[1]))
        for n in sorted(issues):
            if self.store.find_open_ticket(repo, n) is None:
                mirror.cleanup_issue(n)
                log.info("%s#%d: worktrees removed", repo, n)


# ====================================================================== run()


def run(*, once: bool = False) -> int:
    """``fman run``: build the real collaborators (``worker_run``) and work until stopped."""
    from foreman_inator.worker_run import run_worker

    return run_worker(once=once)


__all__ = [
    "V1_READY_ON_CHECKS",
    "Ctx",
    "Job",
    "Worker",
    "flag_comment",
    "run",
]
