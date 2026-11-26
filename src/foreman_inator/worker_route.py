"""Before a ticket's first attempt: clarifying questions (#44), then routing (#15).

``RouteSteps`` is mixed into ``Worker``. The ``route`` job runs for a ``todo`` ticket
that has no ``routed_tier`` or whose clarification is still due (the queue decides,
``worker_queue``): ``before_route`` first, then ``routing.route`` inside the ticket's
trace (it opens its own ``route`` span), stored as ``routed_tier`` + ``route_reason``
unless a ``tier:N`` label routed the ticket meanwhile; then, in the same job, the first
attempt if the queue allows it.

Clarifying questions (``before_route``, todo #44)
-------------------------------------------------
State is the ``poll_cursors`` row ``lifecycle.clarify_cursor(ticket)``: ``clear`` |
``asked`` | ``answered`` | ``released`` | ``resumed`` (no row: not assessed yet), plus
``clarify_qa_cursor(ticket)`` holding the Q&A text for the writer prompt.

* ``config.clarify`` off → route (no request, no model call).
* ``clear`` / ``resumed`` → route. ``asked`` → parked (the queue does not even start a job
  for it, so a parked ticket costs nothing per pass; the poller watches the issue).
* ``answered`` (the poller saw an allowed user's reply: ``NeedsInfoAnswered``) → remove
  ``needs-info`` (idempotent), ``resumed``, route. ``released`` (an allowed user removed the
  label) → ``resumed``, route.
* no row → one ``list_issue_comments``. If the bot's question for this ticket is already
  there (a crash after posting it), put the label on and store ``asked``: no second
  model call, no second comment. Otherwise ``clarify.assess`` (one local model call,
  ``router_timeout_seconds``): ``Clear`` → ``clear``; ``NeedsInfo`` → post the question,
  add ``needs-info``, store ``asked``, in that order.

Routing (#15, #23; wired in wave 9)
-----------------------------------
A ``tier:N`` label is the route, with no model call. Otherwise the nearest-neighbour
router (``knn.route_knn``) is asked first (then, past its own gate, the classifier: #37
below); below ``knn_threshold`` accepted tickets, or on any embedding failure, it
returns None and the rules router (``routing.route``) decides.
The exploration decision (``explore.should_explore`` with ``explore_rate``; never for an
override) is made once, here, and stored as the ``explore:<ticket>`` cursor in the same
transaction as ``routed_tier`` and ``route_reason``; ``Worker.explore`` reads it, so a
restart or a config change gives the same answer. A later ``tier:N`` label before any
attempt routes again through the override path, which stores "0".

Classifier router (#37 / GitHub #19)
-------------------------------------
When kNN picked (``route.source == "knn"``, so its vector is stored) and
``classifier_router`` is on (default) and at least ``classifier_min_tickets`` (default
300) accepted tickets are in the pool, ``classifier.route_classifier`` may replace the
pick: only while its logistic regression beats the kNN rule on the chronological held-out
20% (a tie keeps kNN; the check is cached on the worker, ``classifier_gate``, and redone
at most daily). Its ``route_reason`` starts ``"classifier: "``. It makes no model call and
never raises, so a ``tier:N`` override (never reaches it), exploration, profiles, split
proposals (``label`` is set from the tier) and local-only repos behave as with kNN.

Repo profiles (#51)
-------------------
The repo's profile (``repo_profile``: its ``repo_settings`` row) changes two things here,
both decided once and stored in the routing transaction: the explore draw uses the
profile's rate (thrifty 25%, fast 0; ``normal`` keeps ``config.explore_rate``), and the
start is shifted (``profile.shift_start``: ``fast`` starts one configured tier up, capped
at the top; never a ``tier:N`` override). ``routed_tier`` stays the router's own pick
(#23 learns from it); the start goes in the ``start:<ticket>`` cursor (``start_tier``,
read into ``History.start_tier``) and ``route_reason`` gains ``"; profile fast: tier 1
-> 2"``. ``limits(ticket)`` is the profile's escalation limits for the policy (None for
today's rules).

Splitting a large issue (#50)
-----------------------------
When the router's own pick is tier 3 (``decompose.eligible``: the classifier said
``large``, or kNN wanted tier 3; never a ``tier:N`` label, never a router fallback), and
``decompose.skip_reason`` finds no reason not to (``split_large`` off, the repo profile's
writer mode ``fast``, no tier 3 model, or a cloud tier 3 model on a repo that may not use
cloud models: ``split_cloud_allowed(repo)``, default True), the route job reads the
issue's comments once and then:

* the bot's proposal for an *earlier* ticket of this issue is there: the user re-labeled
  the issue to run it as one ticket. No call; ``route_reason`` says so; tier 3 as before.
* its proposal for *this* ticket is there (a crash after posting): flag it, no call.
* none: ``decompose.propose`` (one tier 3 call through the proxy, inside the spend caps
  and the per-attempt budget). A ``Proposal`` is posted as one comment, then
  ``routed_tier`` (3, the router's pick) is stored with the flag ``split_proposed`` in
  the routing transaction, so the ticket is not worked. A ``Skipped`` (the call failed or
  was refused) is logged and the ticket runs on tier 3 as before, with the reason added
  to ``route_reason``.

Nothing creates a sub-issue or applies a label. A ticket whose flag is cleared later
runs on its stored tier 3 route.

ab:pair side B (#16)
--------------------
Side B of a pair (``ticket.pair_side == 'b'``) is never routed, clarified or split on
its own: it copies side A's route (``_copy_pair_route``). Once A has a ``routed_tier``,
B stores the same ``routed_tier``, ``route_reason`` prefixed ``"pair: "``, and A's
``start:``, ``explore:`` and clarification Q&A cursors, in one transaction (no router
call, no second question, no split proposal: ``split_proposed`` never applies to B).
Until A is routed B waits: the route job returns without an attempt, so B costs a store
read per pass. ``clarify_due`` / ``clarify_parked`` are False for B (A's clarification
covers the issue). Side B's opponent is pinned later, on its first attempt
(``experiments.pair.pick``, ``worker_attempt``).

Local-only repos (#49)
----------------------
The router and the clarify check only ever call a local classifier (``routing.route`` and
``clarify.assess`` are called without ``allow_cloud_classifier``), and for a repo with
``cloud = false`` the kNN router is given ``allow_cloud_embedder=False`` whatever the
config says, so its issue text never reaches a cloud embedder. ``limits`` caps its
escalation ladder at the local rungs (``escalation.with_cloud``).

Crash safety (routing): killed after ``route_knn`` but before that transaction, nothing
is stored (``routed_tier`` NULL), so the ticket is routed again; the new ticket's vector
was stored by ``route_knn``, so no second embedding call, and the explore draw is
deterministic per ticket. Nothing reaches GitHub during routing.

Crash safety: killed after the question was posted but before the label or the cursor →
the marker is found, the label added, ``asked`` stored. Killed after the answer was seen
but before the label was removed → ``answered`` is still stored, so the removal (a no-op
if done) is redone. One round per ticket (7e): nothing ever sets ``asked`` again after
``answered``/``released``/``resumed``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from foreman_inator import routing
from foreman_inator.db.store import Ticket
from foreman_inator.github.app_auth import GitHubNotFound
from foreman_inator.routing import classifier, explore, knn
from foreman_inator.routing import profile as prof
from foreman_inator.routing.escalation import DEFAULT_LIMITS, Limits, with_cloud
from foreman_inator.tickets import clarify, decompose
from foreman_inator.tickets.lifecycle import clarify_cursor, clarify_qa_cursor

if TYPE_CHECKING:
    from foreman_inator.worker_queue import Job

log = logging.getLogger("foreman_inator.worker")

#: Clarification states after which the ticket is routed without asking.
CLARIFY_DONE = ("clear", "resumed")
#: #50: module default for the ``split_large`` / ``split_timeout_seconds`` config keys.
DEFAULT_SPLIT_LARGE = decompose.DEFAULT_ENABLED
DEFAULT_SPLIT_TIMEOUT = decompose.DEFAULT_TIMEOUT
#: #51: the ``poll_cursors`` row holding a ticket's (profile-shifted) start tier.
START_CURSOR_PREFIX = "start:"
#: #16: what side B's ``route_reason`` starts with (the rest is side A's).
PAIR_ROUTE_PREFIX = "pair: "
#: #37: module defaults for the ``classifier_router`` / ``classifier_min_tickets`` keys.
DEFAULT_CLASSIFIER_ROUTER = classifier.DEFAULT_ENABLED
DEFAULT_CLASSIFIER_MIN_TICKETS = classifier.DEFAULT_MIN_TICKETS
_GATE_LOCK = threading.Lock()


def start_cursor(ticket_id: int) -> str:
    return f"{START_CURSOR_PREFIX}{int(ticket_id)}"


def _is_side_b(t: Ticket) -> bool:
    return getattr(t, "pair_side", None) == "b"


class RouteSteps:
    # Provided by Worker.
    store: Any
    config: Any
    api: Any
    proxy: Any
    #: #50/#49: ``repo -> bool``, False for a local-only repo (its issue is never sent to
    #: a cloud splitter). None: every repo may (the wiring step passes the #49 check).
    split_cloud_allowed: Callable[[str], bool] | None = None

    # ------------------------------------------------------------------ clarify (#44)

    def clarify_state(self, t: Ticket) -> str | None:
        return self.store.get_cursor(t.repo, clarify_cursor(t.id))

    def clarify_parked(self, t: Ticket) -> bool:
        """Waiting on ``needs-info`` (free: a store read). Never side B (#16)."""
        if _is_side_b(t):
            return False
        return bool(self.config.clarify) and self.clarify_state(t) == "asked"

    def clarify_due(self, t: Ticket) -> bool:
        """The clarification step has not finished for ``t`` (route job needed). Never
        side B (#16): side A's clarification covers the issue."""
        if _is_side_b(t):
            return False
        return bool(self.config.clarify) and self.clarify_state(t) not in CLARIFY_DONE

    def before_route(self, t: Ticket) -> bool:
        """#44: True to route ``t`` now, False to park it (see the module docstring)."""
        if not self.config.clarify:
            return True
        key = clarify_cursor(t.id)
        state = self.clarify_state(t)
        if state in CLARIFY_DONE:
            return True
        if state == "asked":
            return False
        if state in ("answered", "released"):
            if state == "answered":

                def unlabel() -> None:
                    with contextlib.suppress(GitHubNotFound):
                        self.api.remove_label(t.repo, t.issue_number, clarify.NEEDS_INFO_LABEL)

                self._gh(t, "needs_info_removed", unlabel)  # type: ignore[attr-defined]
            self.store.set_cursor(t.repo, "resumed", key)
            log.info("ticket %d: needs-info %s; resuming", t.id, state)
            return True
        comments = self.api.list_issue_comments(t.repo, t.issue_number)
        if clarify.find_question(comments, t.id, self.api.bot_login()) is not None:
            self._ask_label(t)
            log.info("ticket %d: its needs-info question was already posted; waiting", t.id)
            return False
        with self._trace(t):  # type: ignore[attr-defined]
            result = clarify.assess(
                t,
                config=self.config,
                proxy=self.proxy,
                timeout=float(self.config.router_timeout_seconds),
                watch=self.ollama_watch,  # type: ignore[attr-defined]
                cold_load_allowance=float(self.config.cold_load_timeout_seconds),
            )
        if isinstance(result, clarify.Clear):
            self.store.set_cursor(t.repo, "clear", key)
            log.info("ticket %d: clear to work on (%s)", t.id, result.reason)
            return True
        body = clarify.question_comment(t.id, result.questions)
        self._gh(  # type: ignore[attr-defined]
            t, "needs_info_asked", lambda: self.api.post_comment(t.repo, t.issue_number, body)
        )
        self._ask_label(t)
        log.info(
            "ticket %d: asked %d question(s) on the issue; waiting on needs-info",
            t.id,
            len(result.questions),
        )
        return False

    def _ask_label(self, t: Ticket) -> None:
        self._gh(  # type: ignore[attr-defined]
            t,
            "needs_info_labeled",
            lambda: self.api.add_labels(t.repo, t.issue_number, [clarify.NEEDS_INFO_LABEL]),
        )
        self.store.set_cursor(t.repo, "asked", clarify_cursor(t.id))

    # ------------------------------------------------------------------ route

    def route_job(self, job: Job, cancel: threading.Event) -> None:
        """Clarify (#44), then route (#15) unless a ``tier:N`` label already did."""
        t = self.store.get_ticket(job.ticket_id)
        if t.column != "todo" or t.flag_reason:
            return
        if _is_side_b(t):
            if t.routed_tier is None:
                t = self._copy_pair_route(t)
                if t.routed_tier is None:
                    return  # #16: side A is not routed yet; B waits, no attempt
        elif not self.before_route(t):  # type: ignore[attr-defined]
            log.debug("ticket %d: parked before routing", t.id)
            return
        if t.routed_tier is None:
            t = self._route_ticket(t)
        if cancel.is_set() or self.stopping:  # type: ignore[attr-defined]
            return
        if self.job_for(t) == "attempt":  # type: ignore[attr-defined]
            self.attempt_job(job, cancel)  # carry the ticket on in the same job

    def _route_ticket(self, t: Ticket, *, override: routing.Override | None = None) -> Ticket:
        """Route ``t`` and store the pick with its explore decision (see the module
        docstring, "Routing"). An override makes no model call (it runs inside a poll
        transaction); a classification does, so it runs in a job and keeps a pick that a
        ``tier:N`` label stored meanwhile. Side B of a pair (#16) copies side A's route
        instead (``_copy_pair_route``)."""
        if _is_side_b(t):
            return self._copy_pair_route(t)
        if override is not None:
            r = routing.route(t, config=self.config, proxy=None, override=override)
        else:
            with self._trace(t):  # type: ignore[attr-defined]
                r = self._knn_route(t) or routing.route(
                    t,
                    config=self.config,
                    proxy=self.proxy,
                    timeout=float(self.config.router_timeout_seconds),
                    watch=self.ollama_watch,  # type: ignore[attr-defined]
                    cold_load_allowance=float(self.config.cold_load_timeout_seconds),
                )
        p = self.repo_profile(t)
        limits = prof.limits_for(p)
        is_override = r.source == "override"
        start = prof.shift_start(
            r.tier, (m.tier for m in self.config.models), limits, override=is_override
        )
        reason = r.reason + prof.shift_reason(p, r.tier, start)
        flag: str | None = None
        if override is None and decompose.eligible(r):
            flag, note = self._split(t, r, p)
            reason += note
        ex = explore.should_explore(
            t.id,
            prof.explore_rate(limits, float(self.config.explore_rate)),
            routed_tier=start,
            tiers=self.config,
            override=is_override,
        )
        with self.store.transaction():
            cur = self.store.get_ticket(t.id)
            if override is None and cur.routed_tier is not None:
                return cur
            cur = self.store.update_ticket(t.id, routed_tier=r.tier, route_reason=reason)
            self.store.set_cursor(t.repo, explore.encode(ex), explore.explore_cursor(t.id))
            self.store.set_cursor(t.repo, str(start), start_cursor(t.id))
            if flag is not None:
                cur = self.store.flag_ticket(t.id, flag)
        if flag is not None:
            log.info("ticket %d: split proposed on the issue; flagged, not worked", t.id)
            return cur
        log.info(
            "ticket %d: routed to tier %d (%s)%s%s",
            t.id,
            r.tier,
            r.source,
            f"; profile {p.writer} starts on tier {start}" if start != r.tier else "",
            "; exploring one tier lower" if ex else "",
        )
        return cur

    # ------------------------------------------------------------------ pair (#16)

    def _copy_pair_route(self, t: Ticket) -> Ticket:
        """Side B: store side A's route (see the module docstring, "ab:pair side B").
        Returns B unchanged (``routed_tier`` None) while A is not routed, or when B has no
        pair row; a route B already has is kept."""
        a = self.store.sibling(t.id)
        if a is None or a.routed_tier is None:
            log.debug("ticket %d: pair side B waits for side A's route", t.id)
            return t
        with self.store.transaction():
            cur = self.store.get_ticket(t.id)
            if cur.routed_tier is not None:
                return cur
            reason = f"{PAIR_ROUTE_PREFIX}{a.route_reason or f'tier {a.routed_tier}'}"
            cur = self.store.update_ticket(t.id, routed_tier=a.routed_tier, route_reason=reason)
            for name in (start_cursor, explore.explore_cursor, clarify_qa_cursor):
                value = self.store.get_cursor(a.repo, name(a.id))
                if value is not None:
                    self.store.set_cursor(t.repo, value, name(t.id))
        log.info(
            "ticket %d: pair side B copies ticket %d's route (tier %d)", t.id, a.id, a.routed_tier
        )
        return cur

    # ------------------------------------------------------------------ split (#50)

    def _split(self, t: Ticket, r: routing.Route, p: prof.Profile) -> tuple[str | None, str]:
        """For a ``large`` ticket: ``(flag_reason, "")`` when a split was proposed (and
        posted), else ``(None, note for route_reason)``. Never raises; any failure runs
        the ticket as one, as before (see the module docstring)."""
        skip = decompose.skip_reason(
            repo=t.repo,
            models=self.config,
            enabled=bool(getattr(self.config, "split_large", DEFAULT_SPLIT_LARGE)),
            writer_mode=p.writer,
            cloud_allowed=self._cloud_allowed,
        )
        if skip is not None:
            log.info("ticket %d: not splitting: %s", t.id, skip)
            return None, "" if skip == "splitting is off" else f"; not split: {skip}"
        try:
            comments = self.api.list_issue_comments(t.repo, t.issue_number)
            found = decompose.find_proposal(comments, self.api.bot_login())
        except Exception as exc:
            log.warning("ticket %d: split skipped, comments unreadable (%s)", t.id, exc)
            return None, f"; split fallback: comments unreadable ({type(exc).__name__})"
        if found is not None:
            owner = decompose.proposal_ticket(found)
            if owner == t.id:  # posted before a crash: flag, no second call
                log.info("ticket %d: its split proposal was already posted; flagging", t.id)
                return decompose.flag_reason(), ""
            log.info("ticket %d: split already proposed (ticket %s); running as one", t.id, owner)
            return None, f"; split already proposed on the issue (ticket {owner}); running as one"
        with self._trace(t):  # type: ignore[attr-defined]
            result = decompose.propose(
                t,
                config=self.config,
                proxy=self.proxy,
                cloud_allowed=self._cloud_allowed,
                timeout=float(getattr(self.config, "split_timeout_seconds", DEFAULT_SPLIT_TIMEOUT)),
                watch=self.ollama_watch,  # type: ignore[attr-defined]
                cold_load_allowance=float(self.config.cold_load_timeout_seconds),
                attempt_budget_usd=self.config.attempt_budget_usd,
                max_input_tokens_cloud=self.config.attempt_max_input_tokens_cloud,
                max_input_tokens_local=self.config.attempt_max_input_tokens_local,
            )
        if isinstance(result, decompose.Skipped):
            log.warning("ticket %d: %s", t.id, result.reason)
            return None, f"; {result.reason}"
        if self.store.get_ticket(t.id).routed_tier is not None:  # a tier:N label meanwhile
            return None, ""
        body = decompose.proposal_comment(t.id, result)
        try:
            self._gh(  # type: ignore[attr-defined]
                t, "split_proposed", lambda: self.api.post_comment(t.repo, t.issue_number, body)
            )
        except Exception as exc:  # bounded (7e): no retry loop of model calls
            log.warning("ticket %d: split proposal not posted (%s); running as one", t.id, exc)
            return None, f"; split fallback: proposal not posted ({type(exc).__name__})"
        return decompose.flag_reason(result), ""

    def _cloud_allowed(self, repo: str) -> bool:
        check = self.split_cloud_allowed
        return True if check is None else bool(check(repo))

    def _knn_route(self, t: Ticket) -> routing.Route | None:
        """#23 once ``knn_threshold`` tickets are accepted; None falls back to the rules.
        ``route_knn`` never raises and stores the new ticket's vector, so a re-route after a
        crash reuses it (no second embedding call)."""
        r = knn.route_knn(
            t,
            store=self.store,
            config=self.config,
            proxy=self.proxy,
            embedder=self.embedder,  # type: ignore[attr-defined]
            threshold=int(self.config.knn_threshold),
            k=int(self.config.knn_k),
            timeout=float(self.config.knn_timeout_seconds),
            # #49: a local-only repo's issue text never goes to a cloud embedder.
            allow_cloud_embedder=bool(self.config.allow_cloud_embedder)
            and self.store.repo_allows_cloud(t.repo),
        )
        if r is None or not getattr(self.config, "classifier_router", DEFAULT_CLASSIFIER_ROUTER):
            return r
        return (
            classifier.route_classifier(
                t,
                store=self.store,
                config=self.config,
                embedder=self.embedder or knn.embedding_endpoint(),  # type: ignore[attr-defined]
                gate=self.classifier_gate(),
                knn_route=r,
                min_tickets=int(
                    getattr(self.config, "classifier_min_tickets", DEFAULT_CLASSIFIER_MIN_TICKETS)
                ),
                k=int(self.config.knn_k),
            )
            or r
        )

    def classifier_gate(self) -> classifier.Gate:
        """#37: this worker's cached held-out comparison (made on first use)."""
        with _GATE_LOCK:
            gate = self.__dict__.get("_classifier_gate")
            if gate is None:
                gate = self.__dict__["_classifier_gate"] = classifier.Gate()
            return gate

    # ------------------------------------------------------------------ profiles (#51)

    def repo_profile(self, ticket: Ticket) -> prof.Profile:
        """The ticket's repo profile now (no row, or NULL columns: ``normal``)."""
        return prof.profile_of(self.store.get_repo_settings(ticket.repo))

    def limits(self, ticket: Ticket) -> Limits | None:
        """The repo profile's escalation limits for the policy; None for today's rules
        (so the module constants decide, exactly as before). #49: a local-only repo's
        limits have ``cloud=False`` (the ladder stops at its last local rung)."""
        limits = prof.limits_for(self.repo_profile(ticket))
        current = None if limits == DEFAULT_LIMITS else limits
        return with_cloud(current, self.store.repo_allows_cloud(ticket.repo))

    def start_tier(self, ticket: Ticket) -> int | None:
        """The stored start (``start:<ticket>`` cursor, written with the route); None for
        tickets routed before #51 (escalation then starts on ``routed_tier``)."""
        value = self.store.get_cursor(ticket.repo, start_cursor(ticket.id))
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    def explore(self, ticket: Ticket) -> bool:
        """#23's stored decision (``explore:<ticket>`` cursor, written with the route). No
        row (routed before wave 9, or not routed yet) means no exploration."""
        return explore.decode(self.store.get_cursor(ticket.repo, explore.explore_cursor(ticket.id)))


__all__ = [
    "CLARIFY_DONE",
    "DEFAULT_CLASSIFIER_MIN_TICKETS",
    "DEFAULT_CLASSIFIER_ROUTER",
    "DEFAULT_SPLIT_LARGE",
    "DEFAULT_SPLIT_TIMEOUT",
    "PAIR_ROUTE_PREFIX",
    "START_CURSOR_PREFIX",
    "RouteSteps",
    "start_cursor",
]
