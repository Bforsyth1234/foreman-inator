"""The worker loop, v2 (todo #21, SPEC milestone 2): tiers, escalation, AI review, spend
caps, file locks, metrics.

``Worker`` joins the pieces. Every collaborator is injected (GitHub API, store, config,
worktree manager, OpenCode driver, model proxy, spend caps, file locks, publisher,
poller, clock); ``run()`` at the bottom builds the real ones (``worker_run``).

The flow (SPEC "Ticket lifecycle")
----------------------------------
clarify (#44) → route (#15) → write (OpenCode, sandboxed: #40) → verify (sandboxed) →
escalation (#16) → push + draft PR → GitHub CI gate (#41) → AI review (#17, its checks
sandboxed) or the ``review:skip`` hand-over → PR marked ready only on an AI approve or a
handover → your review (its line comments reach the next attempt: #42) → escalation
again. A base-branch conflict while the PR waits (#43) → a ``rebase`` attempt (capped at
2, then flagged) → the same gates again.

Loop order (``tick``)
---------------------
1. **Poll and handle events** (main thread, quick). ``Poller.process`` turns GitHub state
   into lifecycle events; each is handled inside the poller's store transaction:
   ``lifecycle.apply`` gives the new column and the ``Action`` list, and store-only
   actions run there. GitHub writes are deferred until that event's transaction commits.
   Nothing slow runs there: routing (a model call) is a job, not an action.
2. **Resume paused tickets** (``worker_pause``): a ticket paused at a spend cap resumes
   when the cap resets or ``fman approve-spend`` granted an approval.
3. **Dispatch work.** The queue is derived from the store every time, never kept in
   memory (``worker_queue``): per repo, up to ``config.max_parallel_per_repo`` tickets
   at once (default 1: one local GPU), each as far as its file locks (#19) permit; a
   ticket that wants a file another ticket holds waits, which is not a failure. Job
   kinds, in the order a ticket meets them:

   ``route``     the clarify check (#44), then the router's model call (a ``todo``
                 ticket with no ``routed_tier``, or whose clarification is not done)
   ``attempt``   worktree → writer prompt → OpenCode → commit → verify; or a hand-over
                 when escalation says the ladder is done
   ``publish``   push → draft PR → ``testing`` (an attempt that passed but did not publish)
   ``review``    the AI review of the PR's attempt (``testing``, no review row yet),
                 once the CI gate is open
   ``rebase``    replay the PR's change onto the moved base, resolve, verify (#43)
   ``finalize``  the GitHub side of a stored review: the COMMENT review, the PR body,
                 marking ready, the hand-over (after a crash, or right after the review)

   One job carries a ticket as far as it can go in one pass (route → first attempt →
   publish → review → finalize); the queue re-derives the next step otherwise.
   ``serve_forever`` runs each job on its own thread; ``run_once`` runs them inline.

State changes always go through ``lifecycle.apply``; what each ``Action`` does is the
``self.actions`` table (``worker_actions``).

Invariant 7a
------------
Only ``worker_review.ReviewSteps._mark_ready`` calls ``Publisher.mark_ready``, and it
refuses unless the attempt on the PR has a stored AI ``approve`` or the ticket is being
handed over (AI ``handed_to_user``, no independent reviewer, the ladder ran out with a
change that passed its checks, or ``review:skip`` by an allowed user). A hand-over always
posts its reason on the issue and in the PR body's hand-over section before the PR is
marked ready, and the ticket is flagged last, so a crash in between redoes it.

Crash safety
------------
The worker can die anywhere; launchd restarts it. ``recover()`` marks every attempt with
no ``ended_at`` as ``superseded`` (re-run with the same trigger, capped at three), sweeps
stale file locks and forgets which reviews were finalized, so every open ticket's latest
review is finalized once more: the COMMENT review and the issue comments carry hidden
markers and are looked up before posting, and marking ready is a no-op on a ready PR.
So after a restart the worker reaches the same state without a second review comment,
a second PR (the publisher adopts the open one), a double-counted attempt or a lost
hand-over. Killed during routing: ``routed_tier`` is still NULL, so it routes again.
Killed during the AI review: no review row, so the review runs again. Killed in a spend
pause: the flag is stored; it resumes on its own. Killed while waiting on a lock: the
wait is derived from the lock table each pass. Wave 6b: killed while waiting at the CI
gate: the gate is ``attempts.ci_status``, so the poller just reads CI again; killed after
a red CI result was stored but before the next attempt: escalation decides from the
stored ``failed_checks`` row; killed with a rebase pending: the pending mark is a store
cursor; killed during a rebase attempt: it is superseded and re-run within the rebase
cap; killed between posting a clarifying question and its label or cursor: the question's
marker is found on the issue, so no second model call or comment; killed after an answer
was seen but before ``needs-info`` was removed: the removal is redone.

Wave 6b (wired at the wave's close)
-----------------------------------
* #40 sandbox: ``worker_sandbox`` builds the policy for the three launch points
  (``_launch_writer``, ``_launch_verify``, ``_launch_review``); it fails closed (the ticket
  is flagged ``sandbox_unavailable`` before an attempt row exists; nothing retries).
* #41 CI: ``Poller(ci=True)``; ``RECORD_CI`` stores ``attempts.ci_status``; a red run is
  failed checks (escalation's bound applies); ``ci_gate`` holds the AI review and the
  ``review:skip`` hand-over; ``_mark_ready`` refuses a red or undecided attempt (7a).
* #42: a user kickback stores the review body plus its line comments.
* #43 conflicts: ``Poller(conflicts=True)``; ``START_REBASE`` → ``worker_rebase``;
  ``history`` leaves rebases out of escalation's counts (``lifecycle.rebase_uncounted``).
* #44 clarify: ``worker_route.before_route``; ``NeedsInfoAnswered`` resumes the ticket.
* #51 (repo profiles): ``policy_for(ticket)`` (here), ``review_skipped(ticket)``
  (``worker_review``) and ``_route_ticket`` (``worker_attempt``) are its three call sites.

Milestone 4 (wired in wave 9, #29)
----------------------------------
* #23 router: ``worker_route._route_ticket`` asks the kNN router first (past
  ``knn_threshold``), then the rules router; the explore decision is stored with the route
  and read back by ``explore``. ``knn.backfill`` runs once per poll on the local lane.
* #24 variants: ``ensure_defaults`` at start; the writer loads the champion's prompt name
  and registers every attempt's variant; the reviewer is the champion's model (when
  independent of the writer) with the champion's prompt, and every review registers its
  variant.
* #27 labels: every user and AI kickback is queued, after its transaction commits, on the
  single background "local lane" thread (``worker_background``); unlabeled kickbacks are
  queued again at start, at most ``LABEL_TRIES`` times each.
* #26 shadow: once a repo has no live work due, a running shadow experiment's challenger
  reviews each finalized PR attempt on the "shadow lane" thread (never a job slot), under
  the sandbox, costed to the experiments bucket, posting nothing.

Shutdown: SIGINT/SIGTERM set the stop flag and every running job's cancel event; the
OpenCode driver aborts its session and kills its process group, verify and the reviewer
kill their commands, a cut-short attempt is stored as ``superseded``, a cut-short review
stores nothing, the proxy stops, exit 0.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from foreman_inator import tracing
from foreman_inator.config import Config
from foreman_inator.db.store import OPEN_COLUMNS, Store, Ticket
from foreman_inator.github.client import GitHubAPI
from foreman_inator.github.poller import Poller
from foreman_inator.locks import Locks
from foreman_inator.pr.publisher import Publisher
from foreman_inator.routing import escalation
from foreman_inator.runner import sandbox
from foreman_inator.runner.worktree import WorktreeManager
from foreman_inator.spend import Spend
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import Action, IllegalTransition
from foreman_inator.worker_actions import ActionHandlers, Ctx, flag_comment
from foreman_inator.worker_attempt import AttemptSteps
from foreman_inator.worker_background import BackgroundSteps
from foreman_inator.worker_install import InstallSteps
from foreman_inator.worker_pause import PauseSteps
from foreman_inator.worker_prune import PruneSteps
from foreman_inator.worker_publish import PublishSteps
from foreman_inator.worker_queue import JOIN_TIMEOUT, Job, QueueSteps
from foreman_inator.worker_rebase import RebaseSteps
from foreman_inator.worker_review import ReviewSteps
from foreman_inator.worker_route import RouteSteps
from foreman_inator.worker_sandbox import SandboxSteps

log = logging.getLogger("foreman_inator.worker")

Policy = Callable[[escalation.History], escalation.Step | None]


class Worker(
    ActionHandlers,
    RouteSteps,
    InstallSteps,  # before AttemptSteps: it overrides prepare_worktree (#45)
    AttemptSteps,
    PublishSteps,
    RebaseSteps,
    ReviewSteps,
    PauseSteps,
    QueueSteps,
    SandboxSteps,
    BackgroundSteps,
    PruneSteps,
):
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
        spend: Spend | None = None,
        locks: Locks | None = None,
        clone_url: Callable[[str], str] | None = None,
        policy: Policy | None = None,
        clock: Callable[[], float] = time.monotonic,
        attempt_timeout: float | None = None,
        verify_timeout: float = 20 * 60,
        review_timeout: float | None = None,
    ) -> None:
        self.api = api
        self.store = store
        self.config = config
        self.worktrees = worktrees
        self.driver = driver
        self.proxy = proxy
        self.recordings_dir = Path(recordings_dir)
        self.traces_dir = Path(traces_dir)
        self.publisher = publisher or Publisher(
            api,
            store,
            sandbox_line=sandbox.describe(
                config.sandbox, dict(config.sandbox_network), dict(config.sandbox_install)
            ),
        )
        #: CI (#41) and base conflicts (#43) on: their handlers are in ``self.actions``.
        self.poller = poller or Poller(api, store, config, ci=True, conflicts=True)
        #: Must be the instance whose ``hook`` the proxy calls (``worker_run`` builds both).
        self.spend = spend or Spend(store, config)
        self.locks = locks or Locks(store)
        self.repos = self.poller.repos
        self.clone_url = clone_url or (lambda repo: f"https://github.com/{repo}.git")
        self.ladder = escalation.build_ladder(config.models)
        self.policy: Policy = policy or escalation.make_policy(self.ladder)
        self.clock = clock
        self._recovered = False  # set by recover(); gates the daily prune (#56)
        self.attempt_timeout = attempt_timeout
        self.verify_timeout = verify_timeout
        self.review_timeout = review_timeout
        self.parallel = int(config.max_parallel_per_repo)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._init_queue()
        self._finalized: set[int] = set()  # review ids whose GitHub side is done here
        self._local = threading.local()
        #: #23: the embedding model; the proxy must serve it (``Config.proxy_models``).
        self.embedder = config.embedder()
        self._init_background()
        self._ensure_variants()
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
            Action.RECORD_CI: self._a_record_ci,
            Action.START_REBASE: self._a_start_rebase,
        }

    # ================================================================== the loop

    def recover(self) -> list[int]:
        """Startup: attempts left running by a dead process become ``superseded``; stale
        locks are swept; every open ticket's latest review is finalized once more."""
        fixed: list[int] = []
        for t in self.store.list_tickets(column=OPEN_COLUMNS):
            for a in self.store.list_attempts(t.id):
                if a.ended_at is None:
                    self.store.finish_attempt(a.id, "superseded")
                    fixed.append(a.id)
                    log.info("ticket %d: attempt %d was interrupted; marked superseded", t.id, a.id)
        swept = self.locks.sweep()
        if swept:
            log.info("swept %d stale file lock(s)", swept)
        self._finalized.clear()
        self._sweep.update(self.repos)
        self._ensure_variants()
        self.requeue_unlabeled()
        self._recovered = True  # #56: the daily prune may run from now on
        return fixed

    def run_once(self) -> None:
        """One pass: poll and handle events, then up to the parallel cap of jobs per repo,
        inline (at most one job per ticket)."""
        self.tick(inline=True)

    def tick(self, *, inline: bool) -> None:
        self.poll_and_handle()
        self.dispatch(inline=inline)

    def serve_forever(self) -> None:
        """Poll every ``poll_interval_seconds``; start jobs as soon as a slot is free."""
        interval = float(self.config.poll_interval_seconds)
        next_poll = 0.0
        log.info(
            "worker started: %d repo(s), polling every %.0fs, up to %d ticket(s) per repo",
            len(self.repos),
            interval,
            self.parallel,
        )
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
        self.stop_background(max(1.0, deadline - time.monotonic()))

    # ------------------------------------------------------------------ policy

    def policy_for(self, ticket: Ticket) -> escalation.Step | None:
        """Escalation's decision for ``ticket`` from its stored rows (crash safe). #51's
        repo profiles plug their limits in here."""
        return self.policy(self.history(ticket))

    def history(self, ticket: Ticket) -> escalation.History:
        """What escalation reads. Attempts paused by a spend cap or a lock yield are left
        out (``uncounted_attempts``): they never count toward escalation or the
        interrupted cap; so are rebases (#43: failed or interrupted rebase attempts and
        the attempt each rebase replaced). ``routed_tier`` and the explore flag come from
        the ticket, so a restarted worker decides the same way."""
        attempts = self.store.list_attempts(ticket.id)
        skip = self.uncounted_attempts(ticket) | lifecycle.rebase_uncounted(attempts)
        return escalation.History(
            tuple(a for a in attempts if a.id not in skip),
            tuple(self.store.list_kickbacks(ticket_id=ticket.id)),
            tuple(self.store.list_reviews(ticket_id=ticket.id, shadow=False)),
            routed_tier=ticket.routed_tier,
            explore=self.explore(ticket),
        )

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
        self.queue_backfill()  # #23: at most one small embedding batch per poll, in the back
        if self._recovered:  # #56: after recover(), so a dead process's attempts are ended
            self.maybe_prune()

    def after_commit(self, ticket: Ticket, name: str, fn: Callable[[], Any]) -> None:
        """Run ``fn`` once the poll event's transaction has committed (now, outside one).
        For background work that must never start inside the poller's transaction."""
        pending = getattr(self._local, "pending", None)
        if pending is not None:
            pending.append((ticket.id, name, fn))
        else:
            fn()

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
        if isinstance(event, lifecycle.NeedsInfoAnswered) and ticket is not None:
            log.info("ticket %d: needs-info %s by %s", ticket.id, event.how, event.by)
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


# ====================================================================== run()


def run(*, once: bool = False) -> int:
    """``fman run``: build the real collaborators (``worker_run``) and work until stopped."""
    from foreman_inator.worker_run import run_worker

    return run_worker(once=once)


__all__ = ["Ctx", "Job", "Policy", "Worker", "flag_comment", "run"]
