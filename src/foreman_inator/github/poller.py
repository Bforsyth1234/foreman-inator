"""The poller: GitHub state in, typed lifecycle events out (todo #11).

This module does the lifecycle's only I/O: GitHub reads through ``GitHubAPI`` and cursor
reads/writes in the store. It never writes to GitHub. The events it returns are the
``GitHubEvent`` types in ``foreman_inator.tickets.lifecycle``; ``lifecycle.apply`` turns
each into a column change plus actions, which the worker carries out.

Use (the worker loop, #14)::

    poller = Poller(api, store, config)
    poller.process(handle)  # poll_once, then per event: one transaction of handle + ack,
                            # then commit the per-repo issue cursors

or by hand: ``result = poller.poll_once()``; for each event, inside one
``store.transaction()``: handle it and ``poller.ack(event)``; then ``poller.commit(result)``.

Idempotency, and why no event is lost
-------------------------------------
* Every event is level-triggered from GitHub state plus durable store state, never from
  memory, so a restarted worker (a new ``Poller``) derives the same pending events.
* Events whose effect is a column change (``Merged``, ``ClosedUnmerged``,
  ``AgentLabelRemoved``) or a stored field (``IssueEdited`` vs the ticket's title/request)
  stop being emitted as soon as the worker applies them. The others carry cursor writes
  in ``event.cursors`` (``agent:<n>`` = the labeled-event id a ticket was created from,
  ``tier:<n>`` = the tier label event already applied, ``review:<pr>`` = the highest
  review id handled). ``ack`` writes them; done in the same transaction as the handling,
  the effect and the "handled" mark commit together or not at all.
* The per-repo listing cursor ``issues_since`` is the newest ``updated_at`` seen in the
  listing (GitHub's clock, not ours), written only by ``commit`` after every event of the
  poll was handled. A crash before ``commit`` re-lists from the old cursor next time.
  Each listing asks for ``since = cursor - overlap`` (60 s), so an issue changed while a
  poll is in flight (or within GitHub's one-second timestamp resolution) is listed again;
  re-listed issues are harmless because of the dedupe above. The cursor also keeps the
  ``updated_at`` of each issue inside that window, so an issue re-listed only because of
  the overlap, with the same ``updated_at`` as when a committed poll processed it, is
  skipped without another request.
* PR state (merge, close, reviews) is read for every open ticket on every poll, because
  the client's issue listing drops pull requests; a review that lands mid-poll has a
  higher id than the review cursor and is picked up by the next poll.

Requests per repo per poll (``PollResult.requests``)
----------------------------------------------------
``1`` issue listing (one page per 100 changed issues) · ``+1`` ``list_issue_events`` per
*changed* issue that has an open ticket or is an ``agent``-labeled open candidate ·
``+1`` ``get_pr`` per open ticket with a PR · ``+1`` ``list_reviews`` per ticket in
testing. Only when a new human review arrives: ``+1`` ``get_branch_sha`` (head check),
and for request-changes on the head ``+1`` ``get_issue`` on the PR (kickback label),
``+1`` ``list_issue_events`` on the PR if a kickback label is present, and (#42) ``+1``
``list_review_comments`` for the review's line comments. Plus one ``bot_login``
per ``Poller``. An idle repo with one ticket in testing costs 3 requests per poll; with
none open, 1. First poll of a repo (no cursor): the listing is ``label=agent,
state=open`` and every open ticket not in it gets one ``get_issue``.

GitHub CI (#41, opt-in: ``Poller(..., ci=True)``)
------------------------------------------------
For each ticket in ``testing`` whose PR attempt passed its local checks and waits on CI
(``outcome`` and ``ci_status`` NULL, no live AI review yet): ``+1`` ``list_check_runs`` on
the PR head per poll. When the runs are red, once: ``+1`` ``get_branch_sha`` (base head)
and ``+1`` ``list_check_runs`` on it, and ``+1`` ``get_job_log_tail`` for the first red
job charged to the attempt (its 302 download is one more HTTP request, outside the API
rate limit). The decision is ``lifecycle.decide_ci`` with this poller's clock, counted
from the attempt's ``ended_at``; ``CiResult`` is emitted only once it is decided, and
stops once the worker stored ``attempts.ci_status`` (no cursor needed). A 403 on the
check runs (an App without ``checks: read``) logs one warning per ``Poller``, emits
``CiResult(state="no_permission")`` and skips CI reads for the rest of the process.

Base conflicts (#43, opt-in: ``Poller(..., conflicts=True)``)
------------------------------------------------------------
No extra requests: ``mergeable`` comes from the ``get_pr`` above. A ticket in ``testing``
whose PR has ``mergeable is False`` gives one ``BaseConflict`` per PR head (cursor
``conflict:<pr>`` = the head sha it was handled on); ``None`` (GitHub still computing) and
``True`` (including "behind but clean") give nothing. A new review event for the ticket in
the same poll wins (its attempt starts from the PR anyway); CI is not read for a ticket
that conflicts.

Clarifying questions (#44)
--------------------------
For each ``todo`` ticket whose clarification cursor (``lifecycle.clarify_cursor``) is
``asked`` (the worker posted its question and the ``needs-info`` label), with ``clarify``
on: ``+1`` ``list_issue_comments`` every poll, and, when the issue is not in this poll's
changed set, ``+1`` ``get_issue`` (its labels) and, only once the label is gone, ``+1``
``list_issue_events`` (who removed it). ``tickets.clarify.status`` decides; ``answered``
or ``released`` gives one ``NeedsInfoAnswered`` whose cursors move the clarification
cursor on (and store the Q&A text), so it is emitted once. Reading the waiting ticket's
comments and labels on every poll, instead of only when the issue's ``updated_at`` moved,
is deliberate: GitHub's REST docs do not say that a comment or a label removal bumps an
issue's ``updated_at`` (checked 2026-11-23, rule 12), so the listing alone is not relied on.

Edge cases (decisions, todo #11)
--------------------------------
* Only ``allowed_users`` count, for labels *and* reviews (7b); the bot's own reviews and
  labels never count (its AI review is a COMMENT review recorded in SQLite by #17).
* ``agent`` removed by an allowed user while the ticket is open → ``AgentLabelRemoved``
  (the ticket is dropped). Removed by anyone else → ignored.
* ``agent`` re-applied after the ticket was dropped or done → a new ticket, because the
  labeled event id differs from ``agent:<n>``. Reopening an issue without re-labeling
  does not start a new ticket (same labeled event).
* Issue edited after the ticket was created → ``IssueEdited`` (the next attempt uses the
  new text; a running attempt is not interrupted).
* Review on a commit that is not the PR head → ``ReviewSubmitted(on_head=False)``, which
  changes nothing. Reviews only count while the ticket is in ``testing``; reviews made
  while a new attempt runs are on an old commit by the time it reaches testing.
* Several new reviews in one poll: only the newest counts; the cursor covers all.
* Merge wins over close: a merged PR whose issue auto-closed is ``Merged``. A closed
  (unmerged) PR or a closed issue is ``ClosedUnmerged``. Close and merge are not
  actor-checked (merging needs write access; an issue's author may withdraw it).
* ``tier:N`` (N in 1–3) by an allowed user: the newest one wins; removing it does not
  re-route.

Head-to-head pairs (#34, ``ab:pair``, GH #16)
---------------------------------------------
Each side of a pair is its own ticket, so an issue can have two open tickets
(``store.open_tickets_for_issue``, side A first). ``ab:pair`` counts only if an allowed
user applied it (7b, the same ``_applied_labels`` path as ``agent``); cursor
``pair:<n>`` = the ``ab:pair`` labeled-event id already handled.

* ``agent`` + ``ab:pair`` → ``TicketRequested(pair=True)``. A label still on from an
  earlier ticket counts too (like ``tier:N``).
* ``ab:pair`` added later to an issue whose open ticket has no pair → ``PairRequested``
  (open tickets are never ``done``, so "until side A is done" holds).
* ``ab:pair`` re-added (a new labeled event) while the open ticket is already in a pair
  of any status → ``PairReapplied`` (defined here, once per labeled event). A ticket can
  be in only one pair ever, so it changes nothing; the worker answers it with one
  comment. ``lifecycle.apply`` has no transition for it (the worker must handle it
  before ``apply``; unwired, it is logged as an ignored event and acked).
* ``ab:pair`` removed by an allowed user while the pair is running → ``PairRemoved``
  for the open side B (an undecided side B; a decided pair has no open side B), only
  while side A is still open: once A was rejected/dropped, B just continues alone.
* Both open sides' PRs are read in the same poll, before the issue state, so the
  "Closes #n" issue close can't pre-empt the pick: a merged side → ``Merged(paired=True)``
  and ``PairLost`` for each unmerged open sibling. A sibling already ``done`` in the store
  (its ``Merged`` handled, this side's ``PairLost`` not yet) → ``PairLost`` again.
* One side's PR closed unmerged → ``ClosedUnmerged(what="pr", paired=True)`` for that
  side only; the other goes on (decision 4). Issue closed or ``agent`` removed → one
  ``ClosedUnmerged(what="issue")`` / ``AgentLabelRemoved`` per open side, ``paired=True``.
* The other per-ticket events (``IssueEdited``, ``TierOverride``, clarify, reviews, CI,
  conflicts) are derived per open side with the issue's events read once.
* A ticket whose pair was dropped is polled exactly as an unpaired ticket.
Requests for a paired issue: ``+1`` ``get_pr`` per open side with a PR (plus the usual
per-side review/CI reads); ``list_issue_events`` once per changed issue.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from foreman_inator.config import Config
from foreman_inator.db.store import OPEN_COLUMNS, Attempt, Pair, Store, Ticket
from foreman_inator.github.app_auth import GitHubError
from foreman_inator.github.client import (
    GitHubAPI,
    Issue,
    IssueEvent,
    MissingPermission,
    PullRequest,
)
from foreman_inator.tickets import clarify
from foreman_inator.tickets.lifecycle import (
    KICKBACK_TAGS,
    AgentLabelRemoved,
    BaseConflict,
    CiResult,
    ClosedUnmerged,
    GitHubEvent,
    IssueEdited,
    LineComment,
    Merged,
    NeedsInfoAnswered,
    PairLost,
    PairRemoved,
    PairRequested,
    ReviewSubmitted,
    TicketRequested,
    TierOverride,
    clarify_cursor,
    clarify_qa_cursor,
    count_rebase_attempts,
    decide_ci,
)

AGENT_LABEL = "agent"
PAIR_LABEL = "ab:pair"
REVIEW_SKIP_LABEL = "review:skip"
ISSUES_CURSOR = "issues_since"
DEFAULT_OVERLAP = timedelta(seconds=60)
#: Printed once per Poller when the App cannot read check runs (#41).
CI_PERMISSION_WARNING = (
    "GitHub CI is not checked: the App lacks the 'checks: read' (and 'actions: read') "
    "permission. Run `fman setup` to see how to add them; until then PRs go to AI review "
    "without waiting for CI."
)

log = logging.getLogger("foreman_inator.poller")

_TIER = re.compile(r"^tier:([123])$", re.IGNORECASE)
_KICKBACK = re.compile(r"^kickback:(\w+)$", re.IGNORECASE)
_HUMAN_VERDICTS = {"APPROVED": "approve", "CHANGES_REQUESTED": "changes"}


def agent_cursor(issue_number: int) -> str:
    return f"agent:{issue_number}"


def tier_cursor(issue_number: int) -> str:
    return f"tier:{issue_number}"


def review_cursor(pr_number: int) -> str:
    return f"review:{pr_number}"


def conflict_cursor(pr_number: int) -> str:
    return f"conflict:{pr_number}"


def pair_cursor(issue_number: int) -> str:
    """#34: the ``ab:pair`` labeled-event id already handled for this issue."""
    return f"pair:{issue_number}"


@dataclass(frozen=True)
class PairReapplied(GitHubEvent):
    """#34: ``ab:pair`` applied again (a new labeled event, by an allowed user) while the
    issue's open ticket ``ticket_id`` is already in pair ``pair_id`` (any status). A
    ticket can be in only one pair ever, so this is a no-op the worker answers with one
    comment. Not a lifecycle event: ``lifecycle.apply`` raises on it."""

    ticket_id: int
    pair_id: int
    labeled_by: str
    label_event_id: int


@dataclass
class PollResult:
    """One poll over every configured repo. Iterating yields the events, in order."""

    events: list[GitHubEvent] = field(default_factory=list)
    #: repo -> new ``issues_since`` value, written by ``Poller.commit``.
    issue_cursors: dict[str, str] = field(default_factory=dict)
    #: repo -> GitHub requests made for it this poll.
    requests: dict[str, int] = field(default_factory=dict)
    #: repo -> the error that stopped its poll (its events and cursor are discarded).
    errors: dict[str, Exception] = field(default_factory=dict)

    def __iter__(self) -> Iterator[GitHubEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)


class _Counted:
    """Wraps a GitHubAPI and counts calls (every client method is one or more requests)."""

    def __init__(self, api: GitHubAPI) -> None:
        self._api = api
        self.count = 0

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self._api, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.count += 1
            return fn(*args, **kwargs)

        return call


class Poller:
    def __init__(
        self,
        api: GitHubAPI,
        store: Store,
        config: Config,
        *,
        repos: Iterable[str] | None = None,
        agent_label: str = AGENT_LABEL,
        overlap: timedelta = DEFAULT_OVERLAP,
        ci: bool = False,
        conflicts: bool = False,
        clock: Callable[[], datetime] | None = None,
        pair_label: str = PAIR_LABEL,
    ) -> None:
        self.api = api
        self.store = store
        self.config = config
        self.repos = tuple(repos if repos is not None else config.repos)
        self.agent_label = agent_label
        #: #34: the head-to-head label (counts only when an allowed user applied it).
        self.pair_label = pair_label
        self.overlap = overlap
        #: #41: read GitHub CI for tickets waiting on it (off until the worker wires it).
        self.ci = ci
        #: #43: emit BaseConflict for a conflicting PR in testing (off until wired).
        self.conflicts = conflicts
        self.clock = clock or (lambda: datetime.now(UTC))
        self._bot: str | None = None
        self._ci_forbidden = False

    # ------------------------------------------------------------------ public API

    def poll_once(self) -> PollResult:
        """Read GitHub for every repo and return the pending events. Writes nothing."""
        if self._bot is None:
            self._bot = self.api.bot_login()
        result = PollResult()
        for repo in self.repos:
            api = _Counted(self.api)
            events: list[GitHubEvent] = []
            try:
                cursor = self._poll_repo(api, repo, events)
            except Exception as exc:  # one broken repo must not stall the others
                result.errors[repo] = exc
            else:
                result.events.extend(events)
                if cursor is not None:
                    result.issue_cursors[repo] = cursor
            result.requests[repo] = api.count
        return result

    def ack(self, event: GitHubEvent) -> None:
        """Mark ``event`` handled. Call inside the transaction that handled it."""
        for name, value in event.cursors:
            self.store.set_cursor(event.repo, value, name)

    def commit(self, result: PollResult) -> None:
        """Advance each repo's listing cursor. Call after every event was handled + acked."""
        with self.store.transaction():
            for repo, value in result.issue_cursors.items():
                self.store.set_cursor(repo, value, ISSUES_CURSOR)

    def process(self, handle: Callable[[GitHubEvent], None]) -> PollResult:
        """poll_once; per event one transaction of ``handle(event)`` + ``ack``; commit.

        If ``handle`` raises, that event's transaction rolls back, nothing is committed,
        and the exception propagates: the next poll derives the unhandled events again.
        ``handle`` must record every ``TicketRequested`` (a ``todo`` ticket) even when it
        cannot start work yet; "not started" is what ``todo`` means.
        """
        result = self.poll_once()
        for event in result.events:
            with self.store.transaction():
                handle(event)
                self.ack(event)
        self.commit(result)
        return result

    # ------------------------------------------------------------------ per repo

    def _poll_repo(self, api: Any, repo: str, out: list[GitHubEvent]) -> str | None:
        # #34: an issue with an ab:pair has two open tickets (side A first).
        numbers = {t.issue_number for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=repo)}
        open_tickets = {n: self.store.open_tickets_for_issue(repo, n) for n in sorted(numbers)}
        newest, seen = _load_cursor(self.store.get_cursor(repo, ISSUES_CURSOR))
        if newest is None:
            issues: list[Issue] = list(api.list_issues(repo, label=self.agent_label))
            listed = {i.number for i in issues}
            issues += [api.get_issue(repo, n) for n in sorted(open_tickets) if n not in listed]
        else:
            issues = list(api.list_issues(repo, state="all", since=newest - self.overlap))
        listed_issues = [i for i in issues if not i.is_pull_request]
        for issue in listed_issues:
            if newest is None or issue.updated_at > newest:
                newest = issue.updated_at
        # Re-listed only because of the overlap, unchanged since a committed poll: skip.
        changed = {
            i.number: i for i in listed_issues if seen.get(i.number) != i.updated_at.isoformat()
        }

        for number in sorted(changed):
            if number not in open_tickets:
                self._maybe_request(api, repo, changed[number], out)
        for number in sorted(open_tickets):
            self._issue_events(api, repo, open_tickets[number], changed.get(number), out)
        if newest is None:
            return None
        window = {
            i.number: i.updated_at.isoformat()
            for i in listed_issues
            if i.updated_at >= newest - self.overlap
        }
        return json.dumps({"since": newest.isoformat(), "seen": window}, sort_keys=True)

    def _maybe_request(self, api: Any, repo: str, issue: Issue, out: list[GitHubEvent]) -> None:
        if issue.state != "open" or not _has(issue.labels, self.agent_label):
            return
        events = api.list_issue_events(repo, issue.number)
        applied = _applied_labels(events, issue.labels)
        label_ev = applied.get(self.agent_label.lower())
        if label_ev is None or not self._allowed(label_ev.actor):
            return  # invariant 7b: only a label applied by an allowed user counts
        if self.store.get_cursor(repo, agent_cursor(issue.number)) == str(label_ev.id):
            return  # this labeling already produced a ticket (done or dropped since)
        cursors = [(agent_cursor(issue.number), str(label_ev.id))]
        tier = self._tier(applied)
        if tier is not None:
            cursors.append((tier_cursor(issue.number), str(tier[1].id)))
        pair_ev = self._pair_ev(applied)
        if pair_ev is not None:
            cursors.append((pair_cursor(issue.number), str(pair_ev.id)))
        skip_ev = applied.get(REVIEW_SKIP_LABEL)
        out.append(
            TicketRequested(
                repo,
                issue.number,
                title=issue.title,
                body=issue.body,
                labeled_by=label_ev.actor or "",
                label_event_id=label_ev.id,
                tier_override=tier[0] if tier else None,
                review_skip=skip_ev is not None and self._allowed(skip_ev.actor),
                pair=pair_ev is not None,
                cursors=tuple(cursors),
            )
        )

    def _issue_events(
        self,
        api: Any,
        repo: str,
        tickets: list[Ticket],
        issue: Issue | None,
        out: list[GitHubEvent],
    ) -> None:
        """Events for one issue's open tickets: one ticket, or the open sides of a pair."""
        pairs = [self.store.pair_for_ticket(t.id) for t in tickets]
        live = next((p for p in pairs if p is not None and p.status != "dropped"), None)
        if live is not None:
            self._pair_events(api, repo, tickets, live, issue, out)
            return
        for t, pair in zip(tickets, pairs, strict=True):
            self._ticket_events(api, repo, t, issue, out, pair=pair)

    def _ticket_events(
        self,
        api: Any,
        repo: str,
        t: Ticket,
        issue: Issue | None,
        out: list[GitHubEvent],
        *,
        pair: Pair | None = None,
    ) -> None:
        """An unpaired ticket (or one whose pair was dropped)."""
        n = t.issue_number
        pr = api.get_pr(repo, t.pr_number) if t.pr_number else None
        if pr is not None and pr.merged:
            out.append(Merged(repo, n, ticket_id=t.id, pr_number=pr.number, merged_at=pr.merged_at))
            return
        if issue is not None and issue.state == "closed":
            out.append(ClosedUnmerged(repo, n, ticket_id=t.id, pr_number=t.pr_number, what="issue"))
            return
        if pr is not None and pr.state == "closed":
            out.append(ClosedUnmerged(repo, n, ticket_id=t.id, pr_number=pr.number, what="pr"))
            return
        events: list[IssueEvent] | None = None
        applied: dict[str, IssueEvent] = {}
        if issue is not None:
            events = api.list_issue_events(repo, n)
            if not _has(issue.labels, self.agent_label):
                remover = _last_unlabel_actor(events, self.agent_label)
                if self._allowed(remover):
                    out.append(AgentLabelRemoved(repo, n, ticket_id=t.id, removed_by=remover or ""))
                    return
            applied = _applied_labels(events, issue.labels)
        self._side_events(api, repo, t, pr, issue, events, applied, out)
        self._pair_label_added(repo, t, pair, applied, out)

    def _pair_label_added(
        self,
        repo: str,
        t: Ticket,
        pair: Pair | None,
        applied: dict[str, IssueEvent],
        out: list[GitHubEvent],
    ) -> None:
        """#34: a new ``ab:pair`` labeling → PairRequested (no pair yet) or PairReapplied."""
        n = t.issue_number
        ev = self._pair_ev(applied)
        if ev is None or self.store.get_cursor(repo, pair_cursor(n)) == str(ev.id):
            return
        cursors = ((pair_cursor(n), str(ev.id)),)
        if pair is None:
            out.append(
                PairRequested(
                    repo,
                    n,
                    ticket_id=t.id,
                    labeled_by=ev.actor or "",
                    label_event_id=ev.id,
                    cursors=cursors,
                )
            )
        else:
            out.append(
                PairReapplied(
                    repo,
                    n,
                    ticket_id=t.id,
                    pair_id=pair.id,
                    labeled_by=ev.actor or "",
                    label_event_id=ev.id,
                    cursors=cursors,
                )
            )

    def _pair_events(
        self,
        api: Any,
        repo: str,
        sides: list[Ticket],
        pair: Pair,
        issue: Issue | None,
        out: list[GitHubEvent],
    ) -> None:
        """#34: the open sides of a running (or just decided) pair, both PRs read first."""
        n = pair.issue_number
        prs = {t.id: (api.get_pr(repo, t.pr_number) if t.pr_number else None) for t in sides}
        merged = [t for t in sides if (pr := prs[t.id]) is not None and pr.merged]
        winner: Ticket | None = merged[0] if merged else None
        if winner is None:  # the sibling's Merged was handled, this side's PairLost not yet
            open_ids = {t.id for t in sides}
            for tid in (pair.ticket_a, pair.ticket_b):
                if tid not in open_ids and (other := self.store.get_ticket(tid)).column == "done":
                    winner = other
        if winner is not None:
            for t in merged:
                pr = prs[t.id]
                assert pr is not None
                out.append(
                    Merged(
                        repo,
                        n,
                        ticket_id=t.id,
                        pr_number=pr.number,
                        merged_at=pr.merged_at,
                        paired=True,
                    )
                )
            for t in sides:
                if t not in merged:
                    out.append(
                        PairLost(
                            repo,
                            n,
                            ticket_id=t.id,
                            sibling_ticket_id=winner.id,
                            sibling_pr_number=winner.pr_number or 0,
                            pr_number=t.pr_number,
                        )
                    )
            return
        if issue is not None and issue.state == "closed":
            for t in sides:
                out.append(
                    ClosedUnmerged(
                        repo, n, ticket_id=t.id, pr_number=t.pr_number, what="issue", paired=True
                    )
                )
            return
        live: list[Ticket] = []
        for t in sides:
            pr = prs[t.id]
            if pr is not None and pr.state == "closed":  # decision 4: this side only
                out.append(
                    ClosedUnmerged(
                        repo, n, ticket_id=t.id, pr_number=pr.number, what="pr", paired=True
                    )
                )
            else:
                live.append(t)
        if not live:
            return
        events: list[IssueEvent] | None = None
        applied: dict[str, IssueEvent] = {}
        if issue is not None:
            events = api.list_issue_events(repo, n)
            if not _has(issue.labels, self.agent_label):
                remover = _last_unlabel_actor(events, self.agent_label)
                if self._allowed(remover):
                    for t in live:
                        out.append(
                            AgentLabelRemoved(
                                repo, n, ticket_id=t.id, removed_by=remover or "", paired=True
                            )
                        )
                    return
            a_open = any(t.id == pair.ticket_a for t in live)
            if pair.status == "running" and a_open and not _has(issue.labels, self.pair_label):
                # Side A already rejected/dropped: B is the only side left, so it just
                # carries on as a normal ticket (dropping it would orphan the issue).
                remover = _last_unlabel_actor(events, self.pair_label)
                if self._allowed(remover):
                    for t in [t for t in live if t.pair_side == "b"]:
                        out.append(
                            PairRemoved(
                                repo,
                                n,
                                ticket_id=t.id,
                                removed_by=remover or "",
                                pr_number=t.pr_number,
                            )
                        )
                        live.remove(t)
            applied = _applied_labels(events, issue.labels)
        for t in live:
            self._side_events(api, repo, t, prs[t.id], issue, events, applied, out)
        if live:
            self._pair_label_added(repo, live[0], pair, applied, out)

    def _side_events(
        self,
        api: Any,
        repo: str,
        t: Ticket,
        pr: PullRequest | None,
        issue: Issue | None,
        events: list[IssueEvent] | None,
        applied: dict[str, IssueEvent],
        out: list[GitHubEvent],
    ) -> None:
        """The per-ticket events of a ticket that is still open after this poll."""
        n = t.issue_number
        if issue is not None:
            if (issue.title, issue.body) != (t.title, t.request):
                out.append(IssueEdited(repo, n, ticket_id=t.id, title=issue.title, body=issue.body))
            tier = self._tier(applied)
            if tier is not None and self.store.get_cursor(repo, tier_cursor(n)) != str(tier[1].id):
                out.append(
                    TierOverride(
                        repo,
                        n,
                        ticket_id=t.id,
                        tier=tier[0],
                        labeled_by=tier[1].actor or "",
                        cursors=((tier_cursor(n), str(tier[1].id)),),
                    )
                )
        if t.column == "todo" and getattr(self.config, "clarify", False):
            self._clarify_event(api, repo, t, issue, events, out)
        if pr is not None and t.column == "testing":
            before = len(out)
            self._review_event(api, repo, t, pr, out)
            if self.conflicts and len(out) == before:
                self._conflict_event(repo, t, pr, out)
            if self.ci and len(out) == before:
                self._ci_event(api, repo, t, pr, out)

    def _clarify_event(
        self,
        api: Any,
        repo: str,
        t: Ticket,
        issue: Issue | None,
        events: list[IssueEvent] | None,
        out: list[GitHubEvent],
    ) -> None:
        """#44: a ticket waiting on ``needs-info`` that an allowed user answered or
        released (see the module docstring for the requests)."""
        if self.store.get_cursor(repo, clarify_cursor(t.id)) != "asked":
            return
        n = t.issue_number
        comments = api.list_issue_comments(repo, n)
        if issue is None:
            issue = api.get_issue(repo, n)
        present = _has(issue.labels, clarify.NEEDS_INFO_LABEL)
        if events is None:
            events = [] if present else api.list_issue_events(repo, n)
        st = clarify.status(
            enabled=True,
            ticket_id=t.id,
            labels=issue.labels,
            events=events,
            comments=comments,
            bot_login=self._bot or "",
            is_allowed=self._allowed,
        )
        if st.state not in ("answered", "released"):
            return
        if st.state == "answered" and st.asked is not None:
            replies = clarify.answers(comments, st.asked, self._allowed)
            by = (replies[-1].author or "") if replies else ""
        else:
            by = st.released_by or ""
        cursors = [(clarify_cursor(t.id), st.state)]
        if st.qa:
            cursors.append((clarify_qa_cursor(t.id), st.qa))
        out.append(
            NeedsInfoAnswered(
                repo,
                n,
                ticket_id=t.id,
                how=st.state,  # type: ignore[arg-type]
                by=by,
                qa=st.qa,
                cursors=tuple(cursors),
            )
        )

    def _review_event(
        self, api: Any, repo: str, t: Ticket, pr: PullRequest, out: list[GitHubEvent]
    ) -> None:
        seen = int(self.store.get_cursor(repo, review_cursor(pr.number)) or 0)
        new = [r for r in api.list_reviews(repo, pr.number) if r.id > seen]
        human = [r for r in new if r.state in _HUMAN_VERDICTS and self._allowed(r.author)]
        if not human:
            return
        review = max(human, key=lambda r: r.id)
        head = api.get_branch_sha(repo, pr.head_ref)
        on_head = review.commit_id is None or review.commit_id == head
        verdict = _HUMAN_VERDICTS[review.state]
        tag = label = None
        comments: tuple[LineComment, ...] = ()
        if verdict == "changes" and on_head:
            tag, label = self._kickback_tag(api, repo, pr.number)
            comments = tuple(
                LineComment(c.path, c.line, c.diff_hunk, c.body)
                for c in api.list_review_comments(repo, pr.number, review.id)
            )
        out.append(
            ReviewSubmitted(
                repo,
                t.issue_number,
                ticket_id=t.id,
                pr_number=pr.number,
                review_id=review.id,
                reviewer=review.author or "",
                verdict=verdict,  # type: ignore[arg-type]
                comment=review.body,
                kickback_tag=tag,
                kickback_label=label,
                commit_id=review.commit_id,
                on_head=on_head,
                line_comments=comments,
                cursors=((review_cursor(pr.number), str(max(r.id for r in new))),),
            )
        )

    def _conflict_event(
        self, repo: str, t: Ticket, pr: PullRequest, out: list[GitHubEvent]
    ) -> None:
        if pr.state != "open" or pr.mergeable is not False or not pr.head_sha:
            return
        if self.store.get_cursor(repo, conflict_cursor(pr.number)) == pr.head_sha:
            return  # this head's conflict was already handled (rebase started, or flagged)
        out.append(
            BaseConflict(
                repo,
                t.issue_number,
                ticket_id=t.id,
                pr_number=pr.number,
                head_sha=pr.head_sha,
                rebases_done=count_rebase_attempts(self.store.list_attempts(t.id)),
                cursors=((conflict_cursor(pr.number), pr.head_sha),),
            )
        )

    def _ci_event(
        self, api: Any, repo: str, t: Ticket, pr: PullRequest, out: list[GitHubEvent]
    ) -> None:
        att = self._waiting_on_ci(t)
        if att is None or not pr.head_sha:
            return

        def emit(state: str, **kw: Any) -> None:
            out.append(
                CiResult(
                    repo,
                    t.issue_number,
                    ticket_id=t.id,
                    attempt_id=att.id,
                    head_sha=pr.head_sha,
                    state=state,  # type: ignore[arg-type]
                    **kw,
                )
            )

        if self._ci_forbidden:
            emit("no_permission", note="CI not checked: the App lacks 'checks: read'")
            return
        try:
            runs = api.list_check_runs(repo, pr.head_sha)
        except MissingPermission:
            self._ci_forbidden = True
            log.warning(CI_PERMISSION_WARNING)
            emit("no_permission", note="CI not checked: the App lacks 'checks: read'")
            return
        now = self.clock()
        assert att.ended_at is not None
        decision = decide_ci(runs, waiting_since=att.ended_at, now=now)
        if decision.state == "needs_base":
            try:
                base_runs = api.list_check_runs(repo, api.get_branch_sha(repo, pr.base_ref))
            except GitHubError as exc:  # base unreadable: the red runs are charged
                log.info("CI on the base of %s#%d not read: %s", repo, pr.number, exc)
                base_runs = []
            decision = decide_ci(runs, waiting_since=att.ended_at, now=now, base_runs=base_runs)
        if not decision.decided:
            return
        output = None
        if decision.state == "failed":
            output = self._ci_failure_output(api, repo, runs, decision.failed)
        emit(decision.state, note=decision.note, failed_jobs=decision.failed, failure_output=output)

    def _waiting_on_ci(self, t: Ticket) -> Attempt | None:
        """The ticket's PR attempt, if it passed its local checks and CI has not been
        decided for it yet (and no AI review was stored for it)."""
        attempts = self.store.list_attempts(t.id)
        if not attempts:
            return None
        att = attempts[-1]
        if not att.tests_passed or att.outcome is not None or att.ended_at is None:
            return None
        if att.ci_status is not None or self.store.list_reviews(attempt_id=att.id, shadow=False):
            return None
        return att

    def _ci_failure_output(
        self, api: Any, repo: str, runs: Iterable[Any], failed: tuple[str, ...]
    ) -> str:
        red = [r for r in runs if r.name in failed and r.status == "completed"]
        head = "GitHub CI failed on the pull request: " + ", ".join(
            f"{r.name} ({r.conclusion})" for r in red
        )
        job = next((r for r in red if r.job_id is not None), None)
        if job is None:
            return head + "\n(no job log: these checks are not GitHub Actions jobs)"
        try:
            tail = api.get_job_log_tail(repo, job.job_id)
        except MissingPermission:
            return head + "\n(no job log: the App lacks 'actions: read')"
        except GitHubError as exc:
            return head + f"\n(no job log: {exc.status} {exc.message})"
        return f"{head}\nEnd of the log of job {job.name}:\n{tail}"

    # ------------------------------------------------------------------ helpers

    def _allowed(self, login: str | None) -> bool:
        """Invariant 7b: an allowed user, and never the App's own bot."""
        return login is not None and login != self._bot and self.config.is_allowed_user(login)

    def _pair_ev(self, applied: dict[str, IssueEvent]) -> IssueEvent | None:
        """#34 (7b): the event that applied ``ab:pair``, if it is on and an allowed user
        applied it."""
        ev = applied.get(self.pair_label.lower())
        return ev if ev is not None and self._allowed(ev.actor) else None

    def _tier(self, applied: dict[str, IssueEvent]) -> tuple[int, IssueEvent] | None:
        best: tuple[int, IssueEvent] | None = None
        for name, ev in applied.items():
            m = _TIER.match(name)
            if m and self._allowed(ev.actor) and (best is None or _key(ev) > _key(best[1])):
                best = (int(m.group(1)), ev)
        return best

    def _kickback_tag(self, api: Any, repo: str, pr_number: int) -> tuple[str | None, str | None]:
        labels = api.get_issue(repo, pr_number).labels
        if not any(_KICKBACK.match(lb) for lb in labels):
            return None, None
        applied = _applied_labels(api.list_issue_events(repo, pr_number), labels)
        best: IssueEvent | None = None
        for name, ev in applied.items():
            m = _KICKBACK.match(name)
            if (
                m
                and m.group(1).lower() in KICKBACK_TAGS
                and self._allowed(ev.actor)
                and (best is None or _key(ev) > _key(best))
            ):
                best = ev
        if best is None or best.label is None:
            return None, None
        return best.label.split(":", 1)[1].lower(), best.label


def _load_cursor(raw: str | None) -> tuple[datetime | None, dict[int, str]]:
    """``issues_since`` value: the newest ``updated_at`` listed, and the issues (number ->
    ``updated_at``) inside the overlap window that a committed poll already processed."""
    if raw is None:
        return None, {}
    data = json.loads(raw)
    return datetime.fromisoformat(data["since"]), {int(k): v for k, v in data["seen"].items()}


def _key(ev: IssueEvent) -> tuple[datetime, int]:
    return (ev.created_at, ev.id)


def _has(labels: Iterable[str], label: str) -> bool:
    return label.lower() in {x.lower() for x in labels}


def _applied_labels(events: Iterable[IssueEvent], current: Iterable[str]) -> dict[str, IssueEvent]:
    """Lower-cased label -> the ``labeled`` event that applied it, for labels on now."""
    state: dict[str, IssueEvent] = {}
    for ev in sorted(events, key=_key):
        if ev.label is None:
            continue
        name = ev.label.lower()
        if ev.event == "labeled":
            state[name] = ev
        elif ev.event == "unlabeled":
            state.pop(name, None)
    now = {x.lower() for x in current}
    return {k: v for k, v in state.items() if k in now}


def _last_unlabel_actor(events: Iterable[IssueEvent], label: str) -> str | None:
    actor = None
    for ev in sorted(events, key=_key):
        if ev.label is not None and ev.label.lower() == label.lower():
            actor = ev.actor if ev.event == "unlabeled" else None
    return actor


__all__ = [
    "AGENT_LABEL",
    "CI_PERMISSION_WARNING",
    "DEFAULT_OVERLAP",
    "ISSUES_CURSOR",
    "PAIR_LABEL",
    "REVIEW_SKIP_LABEL",
    "PairReapplied",
    "PollResult",
    "Poller",
    "agent_cursor",
    "conflict_cursor",
    "pair_cursor",
    "review_cursor",
    "tier_cursor",
]
