"""Background work that never takes a live job slot (todo #29, milestone 4).

``BackgroundSteps`` is mixed into ``Worker``. Three kinds of work run here, on two
single-thread lanes, so none of it ever delays or blocks a live attempt or the champion
review (they run on the queue's job threads, ``worker_queue``):

=================  ==============  =====================================================
Work               Lane            When it is queued
=================  ==============  =====================================================
failure label      ``local_lane``  right after each user kickback (once the poll event's
(#27)                              transaction committed: ``Worker.after_commit``) and
                                   each AI kickback (after the review transaction); at
                                   start, every unlabeled kickback with tries left
kNN backfill       ``local_lane``  once per poll (``Worker.poll_and_handle``); one small
(#23)                              batch, no call when nothing is stale; after a failure
                                   it waits 5 minutes, doubling up to 6 hours
shadow review      ``shadow_lane`` once a repo has no live job running or due
(#26)                              (``worker_queue.dispatch``): each open ticket's latest
                                   finalized counted review whose attempt the running
                                   shadow experiment's challenger has not reviewed yet
=================  ==============  =====================================================

The local lane serialises the small local model's calls (labels, embeddings); the shadow
lane runs one challenger review at a time. A lane keeps each queued key once, so
re-queueing on every pass is free. Every task catches its own errors: a failure is
logged and forgotten (the next pass may queue it again where that is safe).

Failure labels (#27): ``failure_label.label_kickback`` (never raises, idempotent; local
model only unless ``allow_cloud_labeler``). Each try is counted first in the
``label-tries:<kickback>`` cursor; a kickback gets at most ``LABEL_TRIES`` tries in its
life, so an answer the model keeps getting wrong stays NULL instead of looping (7e).
Killed while a label is computed: nothing was stored (the row is written last), so the
start-up requeue labels it again (its second and last try); no ticket state changes and
nothing is posted.

Shadow reviews (#26): ``shadow.shadow_review`` with the worker's sandboxed launcher
(``_launch_shadow``: its own scratch key, so it cannot clash with the champion's), the
experiments bucket and ``shadow_timeout_s``. It never posts and never touches the
ticket, attempt or ready state; it stores a ``shadow = 1`` row or a recorded skip. When
the champion's ``ReviewOutcome`` is still in memory, its checks 1-4 are passed in
(``precomputed``), so the shadow only re-reads the diff (check 5); after a restart they
are run again. Killed mid-shadow: nothing is stored (a cut-short review is not a
verdict), so it runs again later; the live ticket is not affected.

Shutdown: ``stop_background`` sets each lane's cancel event (the shadow review kills its
check commands), drops queued work and waits a bounded time for the running task.
"""

from __future__ import annotations

import collections
import logging
import threading
from collections.abc import Callable
from typing import Any

from foreman_inator.db.store import OPEN_COLUMNS, Ticket
from foreman_inator.experiments import shadow, variants
from foreman_inator.review import reviewer as rv
from foreman_inator.routing import knn
from foreman_inator.runner import sandbox
from foreman_inator.tracing import failure_label

log = logging.getLogger("foreman_inator.worker")

#: Tries per kickback, ever (the immediate one plus one after a restart).
LABEL_TRIES = 2
#: Unlabeled kickbacks queued again at start, newest first.
LABELS_AT_START = 50
#: A failing kNN backfill waits this long, doubling up to the max (seconds).
BACKFILL_BACKOFF, BACKFILL_BACKOFF_MAX = 5 * 60.0, 6 * 3600.0
#: Champion review outcomes kept in memory for the shadow's ``precomputed`` checks.
KEEP_OUTCOMES = 64


def label_tries_cursor(kickback_id: int) -> str:
    return f"label-tries:{int(kickback_id)}"


class Lane:
    """One daemon thread running queued tasks in order; each key is queued once."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.cancel = threading.Event()
        self._cv = threading.Condition()
        self._queue: collections.deque[tuple[str, Callable[[], Any]]] = collections.deque()
        self._keys: set[str] = set()
        self._busy = False
        self._stopped = False
        self._thread: threading.Thread | None = None

    def submit(self, key: str, fn: Callable[[], Any]) -> bool:
        """Queue ``fn`` unless ``key`` is queued or running, or the lane stopped."""
        with self._cv:
            if self._stopped or key in self._keys:
                return False
            self._keys.add(key)
            self._queue.append((key, fn))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
            self._cv.notify_all()
            return True

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                key, fn = self._queue.popleft()
                self._busy = True
            try:
                fn()
            except Exception as exc:  # a background task never takes the worker down
                log.warning("%s: %s failed (%s: %s)", self.name, key, type(exc).__name__, exc)
            finally:
                with self._cv:
                    self._keys.discard(key)
                    self._busy = False
                    self._cv.notify_all()

    @property
    def idle(self) -> bool:
        with self._cv:
            return not self._queue and not self._busy

    def drain(self, timeout: float = 30.0) -> bool:
        """Wait until nothing is queued or running (tests, ``fman run --once``)."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._queue and not self._busy, timeout)

    def stop(self, timeout: float = 10.0) -> None:
        with self._cv:
            self._stopped = True
            self.cancel.set()
            self._queue.clear()
            self._keys.clear()
            self._cv.notify_all()
            th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
            if th.is_alive():
                log.warning("%s did not stop in time", self.name)


class BackgroundSteps:
    # Provided by Worker.
    store: Any
    config: Any
    proxy: Any
    spend: Any
    recordings_dir: Any
    embedder: Any
    _finalized: set[int]

    def _init_background(self) -> None:
        self.local_lane = Lane("fman-local")
        self.shadow_lane = Lane("fman-shadow")
        self._champion_outcomes: collections.OrderedDict[int, rv.ReviewOutcome] = (
            collections.OrderedDict()
        )
        self._backfill_delay = 0.0  # seconds; 0 while the backfill works
        self._backfill_after = 0.0  # worker clock time before which no backfill is queued
        #: The labeler (tests swap it to simulate a kill mid-label).
        self.label_fn: Callable[..., Any] = failure_label.label_kickback

    def stop_background(self, timeout: float = 10.0) -> None:
        self.local_lane.stop(timeout)
        self.shadow_lane.stop(timeout)

    def drain_background(self, timeout: float = 60.0) -> bool:
        return self.local_lane.drain(timeout) and self.shadow_lane.drain(timeout)

    def _ensure_variants(self) -> None:
        """#24: register the configured variants; set a champion where a role has none."""
        try:
            variants.ensure_defaults(self.store, self.config)
        except Exception as exc:
            log.warning("variants: defaults not registered (%s: %s)", type(exc).__name__, exc)

    # ------------------------------------------------------------------ labels (#27)

    def queue_label(self, repo: str, kickback_id: int) -> None:
        if not self.config.failure_labels:
            return
        self.local_lane.submit(
            f"label:{kickback_id}", lambda: self._label_one(repo, int(kickback_id))
        )

    def _label_one(self, repo: str, kickback_id: int) -> None:
        key = label_tries_cursor(kickback_id)
        tries = int(self.store.get_cursor(repo, key) or 0)
        if tries >= LABEL_TRIES or self.store.get_kickback(kickback_id).failure_step:
            return
        self.store.set_cursor(repo, str(tries + 1), key)
        self.label_fn(
            self.store,
            kickback_id,
            recordings_dir=self.recordings_dir,
            config=self.config,
            proxy=self.proxy,
            allow_cloud=bool(self.config.allow_cloud_labeler),
            timeout=float(self.config.failure_label_timeout_seconds),
        )

    def requeue_unlabeled(self) -> int:
        """At start: queue unlabeled kickbacks that have tries left (newest first)."""
        if not self.config.failure_labels:
            return 0
        rows = self.store.query(
            "SELECT k.id AS id, t.repo AS repo FROM kickbacks k"
            " JOIN attempts a ON a.id = k.attempt_id JOIN tickets t ON t.id = a.ticket_id"
            " WHERE k.failure_step IS NULL ORDER BY k.id DESC LIMIT ?",
            (LABELS_AT_START,),
        )
        n = 0
        for r in rows:
            tries = int(self.store.get_cursor(r["repo"], label_tries_cursor(r["id"])) or 0)
            if tries < LABEL_TRIES:
                self.queue_label(r["repo"], int(r["id"]))
                n += 1
        if n:
            log.info("failure labels: %d unlabeled kickback(s) queued", n)
        return n

    # ------------------------------------------------------------------ kNN backfill (#23)

    def queue_backfill(self) -> None:
        if self.clock() < self._backfill_after:  # type: ignore[attr-defined]
            return
        self.local_lane.submit("knn-backfill", self._backfill_once)

    def _backfill_once(self) -> None:
        r = knn.backfill(
            self.store,
            proxy=self.proxy,
            embedder=self.embedder,
            allow_cloud_embedder=bool(self.config.allow_cloud_embedder),
        )
        if r.error is not None:
            # No loop (7e): a failing embedder (not installed, say) is retried less and
            # less often, and said once per change, not once per poll.
            delay = min(BACKFILL_BACKOFF_MAX, max(BACKFILL_BACKOFF, self._backfill_delay * 2))
            if not self._backfill_delay:
                log.warning("knn backfill failing (%s); retrying every %.0f min at most",
                            r.error, delay / 60)  # fmt: skip
            self._backfill_delay = delay
            self._backfill_after = self.clock() + delay  # type: ignore[attr-defined]
            return
        if self._backfill_delay:
            log.info("knn backfill working again")
        self._backfill_delay, self._backfill_after = 0.0, 0.0
        if r.embedded:
            log.info("knn: embedded %d accepted ticket(s), %d to go", len(r.embedded), r.remaining)

    # ------------------------------------------------------------------ shadow reviews (#26)

    def remember_champion(self, attempt_id: int, outcome: rv.ReviewOutcome) -> None:
        self._champion_outcomes[attempt_id] = outcome
        while len(self._champion_outcomes) > KEEP_OUTCOMES:
            self._champion_outcomes.popitem(last=False)

    def queue_shadows(self, repo: str) -> int:
        """Queue the shadow reviews ``repo`` owes the running challenger (see the module
        docstring). Called only when the repo has no live work running or due."""
        if not self.config.experiments.shadow or self.stopping:  # type: ignore[attr-defined]
            return 0
        ch = shadow.find_challenger(self.store, self.config)
        if ch is None:
            return 0
        started = self.store.get_experiment(ch.experiment_id).started_at
        n = 0
        for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=repo):
            reviews = self.store.list_reviews(ticket_id=t.id, shadow=False)
            if not reviews:
                continue
            r = reviews[-1]
            if r.id not in self._finalized or r.created_at < started:
                continue
            done = any(
                s.variant_id == ch.variant_id
                for s in self.store.list_reviews(attempt_id=r.attempt_id, shadow=True)
            )
            if done or shadow.recorded_skip(self.store, repo, ch.variant_id, r.attempt_id):
                continue
            key = f"shadow:{ch.variant_id}:a{r.attempt_id}"
            if self.shadow_lane.submit(key, lambda t=t, r=r: self._shadow_one(t.id, r, ch)):
                n += 1
        return n

    def _launch_shadow(self, **kw: Any) -> rv.ReviewOutcome:
        """The shadow's process launch point: like ``_launch_review`` (#40), with its own
        sandbox scratch key so it never shares the champion's."""
        import shutil

        t = self.store.get_ticket(kw["ticket_id"])
        key = f"rs{kw['attempt_id']}"
        scratch = self._sandbox_scratch(f"{key}-work")  # type: ignore[attr-defined]
        policy = self._sandbox_policy(t, scratch, key, writer=False)  # type: ignore[attr-defined]
        try:
            with sandbox.active(policy):
                return rv.review_change(**kw)
        finally:
            self._drop_scratch(policy)  # type: ignore[attr-defined]
            shutil.rmtree(scratch, ignore_errors=True)

    def _shadow_skip(self, t: Ticket, attempt_id: int, ch: shadow.Challenger, why: str) -> None:
        """Record a skip the way ``shadow_review`` does, so it is never retried (7e)."""
        self.store.set_cursor(t.repo, why[:1000], shadow.skip_key(ch.variant_id, attempt_id))
        log.info("ticket %d: shadow review of attempt %d skipped (%s)", t.id, attempt_id, why)

    def _shadow_one(self, ticket_id: int, review: Any, ch: shadow.Challenger) -> None:
        t: Ticket = self.store.get_ticket(ticket_id)
        att = self.store.get_attempt(review.attempt_id)
        try:
            why = self.sandbox_ready(self._mirror(t.repo).repo_dir)  # type: ignore[attr-defined]
            if why is not None:
                self._shadow_skip(t, att.id, ch, f"sandbox_unavailable: {why}")
                return
            writer = self._endpoint_named(att)  # type: ignore[attr-defined]
            mirror = self._mirror(t.repo)  # type: ignore[attr-defined]
            head = self._head_sha(mirror, t, att)  # type: ignore[attr-defined]
        except Exception as exc:  # e.g. the head commit is gone: recorded, not retried
            self._shadow_skip(t, att.id, ch, f"error: {type(exc).__name__}: {str(exc)[:300]}")
            return
        settings = self.store.get_repo_settings(t.repo)
        champion = self._champion_outcomes.get(att.id)
        extra: dict[str, Any] = {}
        if champion is not None and champion.read_diff != "skipped" and champion.tests:
            extra["precomputed"] = (
                champion.tests,
                champion.coverage,
                champion.revert,
                champion.run_app,
            )
        with self._trace(t):  # type: ignore[attr-defined]
            res = shadow.shadow_review(
                store=self.store,
                issue=rv.IssueText(t.issue_number, t.title, t.request),
                change=rv.ChangeRef(mirror, att.base_commit or "", head),
                writer=writer,
                challenger=ch.endpoint,
                challenger_prompt=ch.prompt_name,
                challenger_variant=ch.variant_id,
                proxy=self.proxy,
                attempt_id=att.id,
                ticket_id=t.id,
                experiment_id=ch.experiment_id,
                champion=champion,
                launch=self._launch_shadow,
                spend=self.spend,
                verify_override=settings.verify_command if settings else None,
                recordings_dir=self.recordings_dir,
                timeout=float(self.config.experiments.shadow_timeout_s),
                cancel=self.shadow_lane.cancel,
                **extra,
            )
        log.info(
            "ticket %d: shadow review of attempt %d by %s: %s",
            t.id,
            att.id,
            ch.variant_id,
            res.verdict or res.reason or res.status,
        )


__all__ = ["LABEL_TRIES", "BackgroundSteps", "Lane", "label_tries_cursor"]
