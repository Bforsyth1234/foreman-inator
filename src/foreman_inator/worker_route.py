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
router (``knn.route_knn``) is asked first; below ``knn_threshold`` accepted tickets, or on
any embedding failure, it returns None and the rules router (``routing.route``) decides.
The exploration decision (``explore.should_explore`` with ``explore_rate``; never for an
override) is made once, here, and stored as the ``explore:<ticket>`` cursor in the same
transaction as ``routed_tier`` and ``route_reason``; ``Worker.explore`` reads it, so a
restart or a config change gives the same answer. A later ``tier:N`` label before any
attempt routes again through the override path, which stores "0".

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
from typing import TYPE_CHECKING, Any

from foreman_inator import routing
from foreman_inator.db.store import Ticket
from foreman_inator.github.app_auth import GitHubNotFound
from foreman_inator.routing import explore, knn
from foreman_inator.routing import profile as prof
from foreman_inator.routing.escalation import DEFAULT_LIMITS, Limits
from foreman_inator.tickets import clarify
from foreman_inator.tickets.lifecycle import clarify_cursor

if TYPE_CHECKING:
    from foreman_inator.worker_queue import Job

log = logging.getLogger("foreman_inator.worker")

#: Clarification states after which the ticket is routed without asking.
CLARIFY_DONE = ("clear", "resumed")
#: #51: the ``poll_cursors`` row holding a ticket's (profile-shifted) start tier.
START_CURSOR_PREFIX = "start:"


def start_cursor(ticket_id: int) -> str:
    return f"{START_CURSOR_PREFIX}{int(ticket_id)}"


class RouteSteps:
    # Provided by Worker.
    store: Any
    config: Any
    api: Any
    proxy: Any

    # ------------------------------------------------------------------ clarify (#44)

    def clarify_state(self, t: Ticket) -> str | None:
        return self.store.get_cursor(t.repo, clarify_cursor(t.id))

    def clarify_parked(self, t: Ticket) -> bool:
        """Waiting on ``needs-info`` (free: a store read)."""
        return bool(self.config.clarify) and self.clarify_state(t) == "asked"

    def clarify_due(self, t: Ticket) -> bool:
        """The clarification step has not finished for ``t`` (route job needed)."""
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
        if not self.before_route(t):  # type: ignore[attr-defined]
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
        ``tier:N`` label stored meanwhile."""
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
        log.info(
            "ticket %d: routed to tier %d (%s)%s%s",
            t.id,
            r.tier,
            r.source,
            f"; profile {p.writer} starts on tier {start}" if start != r.tier else "",
            "; exploring one tier lower" if ex else "",
        )
        return cur

    def _knn_route(self, t: Ticket) -> routing.Route | None:
        """#23 once ``knn_threshold`` tickets are accepted; None falls back to the rules.
        ``route_knn`` never raises and stores the new ticket's vector, so a re-route after a
        crash reuses it (no second embedding call)."""
        return knn.route_knn(
            t,
            store=self.store,
            config=self.config,
            proxy=self.proxy,
            embedder=self.embedder,  # type: ignore[attr-defined]
            threshold=int(self.config.knn_threshold),
            k=int(self.config.knn_k),
            timeout=float(self.config.knn_timeout_seconds),
            allow_cloud_embedder=bool(self.config.allow_cloud_embedder),
        )

    # ------------------------------------------------------------------ profiles (#51)

    def repo_profile(self, ticket: Ticket) -> prof.Profile:
        """The ticket's repo profile now (no row, or NULL columns: ``normal``)."""
        return prof.profile_of(self.store.get_repo_settings(ticket.repo))

    def limits(self, ticket: Ticket) -> Limits | None:
        """The repo profile's escalation limits for the policy; None for today's rules
        (so the module constants decide, exactly as before)."""
        limits = prof.limits_for(self.repo_profile(ticket))
        return None if limits == DEFAULT_LIMITS else limits

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


__all__ = ["CLARIFY_DONE", "START_CURSOR_PREFIX", "RouteSteps", "start_cursor"]
