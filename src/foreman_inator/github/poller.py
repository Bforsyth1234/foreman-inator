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
and for request-changes ``+1`` ``get_issue`` on the PR (kickback label) and ``+1``
``list_issue_events`` on the PR if a kickback label is present. Plus one ``bot_login``
per ``Poller``. An idle repo with one ticket in testing costs 3 requests per poll; with
none open, 1. First poll of a repo (no cursor): the listing is ``label=agent,
state=open`` and every open ticket not in it gets one ``get_issue``.

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
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from foreman_inator.config import Config
from foreman_inator.db.store import OPEN_COLUMNS, Store, Ticket
from foreman_inator.github.client import GitHubAPI, Issue, IssueEvent, PullRequest
from foreman_inator.tickets.lifecycle import (
    KICKBACK_TAGS,
    AgentLabelRemoved,
    ClosedUnmerged,
    GitHubEvent,
    IssueEdited,
    Merged,
    ReviewSubmitted,
    TicketRequested,
    TierOverride,
)

AGENT_LABEL = "agent"
REVIEW_SKIP_LABEL = "review:skip"
ISSUES_CURSOR = "issues_since"
DEFAULT_OVERLAP = timedelta(seconds=60)

_TIER = re.compile(r"^tier:([123])$", re.IGNORECASE)
_KICKBACK = re.compile(r"^kickback:(\w+)$", re.IGNORECASE)
_HUMAN_VERDICTS = {"APPROVED": "approve", "CHANGES_REQUESTED": "changes"}


def agent_cursor(issue_number: int) -> str:
    return f"agent:{issue_number}"


def tier_cursor(issue_number: int) -> str:
    return f"tier:{issue_number}"


def review_cursor(pr_number: int) -> str:
    return f"review:{pr_number}"


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
    ) -> None:
        self.api = api
        self.store = store
        self.config = config
        self.repos = tuple(repos if repos is not None else config.repos)
        self.agent_label = agent_label
        self.overlap = overlap
        self._bot: str | None = None

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
        open_tickets = {
            t.issue_number: t for t in self.store.list_tickets(column=OPEN_COLUMNS, repo=repo)
        }
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
            self._ticket_events(api, repo, open_tickets[number], changed.get(number), out)
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
                cursors=tuple(cursors),
            )
        )

    def _ticket_events(
        self, api: Any, repo: str, t: Ticket, issue: Issue | None, out: list[GitHubEvent]
    ) -> None:
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
        if issue is not None:
            events = api.list_issue_events(repo, n)
            if not _has(issue.labels, self.agent_label):
                remover = _last_unlabel_actor(events, self.agent_label)
                if self._allowed(remover):
                    out.append(AgentLabelRemoved(repo, n, ticket_id=t.id, removed_by=remover or ""))
                    return
            if (issue.title, issue.body) != (t.title, t.request):
                out.append(IssueEdited(repo, n, ticket_id=t.id, title=issue.title, body=issue.body))
            tier = self._tier(_applied_labels(events, issue.labels))
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
        if pr is not None and t.column == "testing":
            self._review_event(api, repo, t, pr, out)

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
        if verdict == "changes" and on_head:
            tag, label = self._kickback_tag(api, repo, pr.number)
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
                cursors=((review_cursor(pr.number), str(max(r.id for r in new))),),
            )
        )

    # ------------------------------------------------------------------ helpers

    def _allowed(self, login: str | None) -> bool:
        """Invariant 7b: an allowed user, and never the App's own bot."""
        return login is not None and login != self._bot and self.config.is_allowed_user(login)

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
    "DEFAULT_OVERLAP",
    "ISSUES_CURSOR",
    "REVIEW_SKIP_LABEL",
    "PollResult",
    "Poller",
    "agent_cursor",
    "review_cursor",
    "tier_cursor",
]
