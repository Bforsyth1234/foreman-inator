"""One status comment on the issue while work runs (#77 / todo 81): ``StatusCommentSteps``
is mixed into ``Worker``.

``status_update(ticket, state, **facts)`` keeps ONE bot comment per ticket up to date:

* The comment carries a hidden marker, ``STATUS_MARKER`` (``<!-- foreman-inator:status
  ticket=N -->``). The first update for a ticket lists the issue's comments once and looks
  for a comment by the bot with that marker (a user quoting the marker does not count), so
  a restarted worker edits the comment it posted before instead of posting a second one.
  None found → one ``post_comment``. After that the comment id is remembered and each
  update is one ``update_comment`` (GitHub "Update an issue comment", see
  ``github.client``).
* At most one edit per state change: an update whose rendered text equals the last one
  this process wrote (or, after a restart, the comment's current body) makes no write.
  Callers call it only at state changes (attempt start, escalation, verify, review, PR
  opened, hand-over, drop), never per model turn, so a long attempt costs a handful of
  calls.
* The text is the worker's own: step, attempt number, tier, model name, start time (UTC)
  and PR number, plus a hand-over reason cleaned to one short line with no mentions and no
  HTML comment markers. No model output. A cost figure only when ``status_show_cost`` is on
  (issue comments on a public repo are public).
* Optional label ``WORKING_LABEL`` (``fman:working``) behind ``status_label``: added when
  an active state is written, removed at a terminal one. A bot-applied label never starts
  work (invariant 7b) and it is not ``agent``, so it cannot loop.
* Off with ``config.status_comment = False``, or per repo when the repo's settings row has
  ``status_comment`` False (read with ``getattr``, so rows without the column count as on).
* Failures (GitHub errors, a bad call) are logged and swallowed: a status comment never
  blocks or fails work.

The bot's own comments are already left out by author everywhere bot comments are:
``tickets.clarify.answers`` (a reply by the asking bot is never an answer) and
``tickets.prompt`` issue-comments context (``_same_login(author, bot_login)``), so the
marker needs no extra filtering there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from foreman_inator.github import GitHubNotFound

log = logging.getLogger("foreman_inator.worker")

STATUS_MARKER = "<!-- foreman-inator:status ticket={ticket_id} -->"
WORKING_LABEL = "fman:working"

#: Defaults for the config keys the wiring step adds.
STATUS_COMMENT = True
STATUS_LABEL = False
STATUS_SHOW_COST = False

REASON_MAX = 160

ACTIVE_STATES = frozenset({"attempt_started", "escalated", "verifying", "reviewing"})
TERMINAL_STATES = frozenset({"pr_opened", "handed_over", "dropped"})
STATES = ACTIVE_STATES | TERMINAL_STATES


def status_marker(ticket_id: int) -> str:
    return STATUS_MARKER.format(ticket_id=int(ticket_id))


def _clean(text: str, limit: int = REASON_MAX) -> str:
    """One short line safe to post as the bot: no HTML comments or markers, no
    @mentions, no newlines, clipped."""
    text = re.sub(r"<!--.*?(?:-->|$)", "", str(text), flags=re.DOTALL)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"@(?=[A-Za-z0-9])", "@​", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _hhmm(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%H:%M UTC")


def render_status(
    state: str,
    *,
    attempt: int | None = None,
    tier: int | None = None,
    model: str | None = None,
    started_at: datetime | None = None,
    pr_number: int | None = None,
    reason: str | None = None,
    cost_usd: float | None = None,
    show_cost: bool = STATUS_SHOW_COST,
) -> str:
    """The status line for ``state`` (one of ``STATES``), without the marker."""
    if state not in STATES:
        raise ValueError(f"unknown status state {state!r}")
    run = []
    if attempt is not None:
        run.append(f"attempt {int(attempt)}")
    if tier is not None:
        run.append(f"tier {int(tier)}" + (f" ({_clean(model, 80)})" if model else ""))
    what = " on ".join(run)
    started = _hhmm(started_at)
    since = f", started {started}" if started else ""
    if state == "attempt_started":
        text = f"foreman-inator is working on this: {what or 'an attempt'}{since}."
    elif state == "escalated":
        text = f"foreman-inator is working on this: escalated, {what or 'a new attempt'}{since}."
    elif state == "verifying":
        text = f"foreman-inator is working on this: {what or 'the attempt'}{since}, verifying."
    elif state == "reviewing":
        text = f"foreman-inator is working on this: {what or 'the attempt'}{since}, in review."
    elif state == "pr_opened":
        text = f"foreman-inator opened PR #{int(pr_number)}." if pr_number else "PR opened."
    elif state == "handed_over":
        why = _clean(reason) if reason else ""
        text = "foreman-inator handed this over" + (f": {why}." if why else ".")
    else:
        text = "foreman-inator stopped working on this (dropped)."
    if show_cost and cost_usd is not None:
        text += f" Spent so far: ${float(cost_usd):.2f}."
    return text


class StatusCommentSteps:
    # Provided by Worker.
    api: Any
    config: Any
    store: Any

    def _status_state(self) -> tuple[dict[int, int], dict[int, str], set[int]]:
        d = self.__dict__
        return (
            d.setdefault("_status_comment_ids", {}),
            d.setdefault("_status_last_body", {}),
            d.setdefault("_status_labeled", set()),
        )

    def _status_flag(self, name: str, default: bool) -> bool:
        return bool(getattr(self.config, name, default))

    def status_enabled(self, ticket: Any) -> bool:
        if not self._status_flag("status_comment", STATUS_COMMENT):
            return False
        try:
            row = self.store.get_repo_settings(ticket.repo)
        except Exception:
            return True
        return getattr(row, "status_comment", None) is not False

    def status_update(self, ticket: Any, state: str, **facts: Any) -> None:
        """Post or edit this ticket's one status comment (module docstring). Never raises."""
        try:
            if not self.status_enabled(ticket):
                return
            show_cost = self._status_flag("status_show_cost", STATUS_SHOW_COST)
            body = (
                f"{status_marker(ticket.id)}\n{render_status(state, show_cost=show_cost, **facts)}"
            )
        except Exception as exc:
            log.warning(
                "status comment for ticket %s skipped: %s: %s", ticket.id, type(exc).__name__, exc
            )
            return

        def write() -> None:
            try:
                self._status_write(ticket, state, body)
            except Exception as exc:
                log.warning(
                    "status comment for ticket %s failed: %s: %s",
                    ticket.id,
                    type(exc).__name__,
                    exc,
                )

        gh: Callable[..., Any] | None = getattr(self, "_gh", None)
        try:
            if gh is not None:
                gh(ticket, "status_comment", write)
            else:
                write()
        except Exception as exc:  # the span or the deferral itself failed
            log.warning(
                "status comment for ticket %s failed: %s: %s", ticket.id, type(exc).__name__, exc
            )

    def _status_write(self, ticket: Any, state: str, body: str) -> None:
        ids, last, labeled = self._status_state()
        if last.get(ticket.id) == body:
            return
        repo, number = ticket.repo, ticket.issue_number
        cid = ids.get(ticket.id)
        if cid is None:
            found = self._status_find(ticket)
            if found is not None:
                cid = found.id
                ids[ticket.id] = cid
                if (found.body or "") == body:
                    last[ticket.id] = body
                    return
        if cid is None:
            posted = self.api.post_comment(repo, number, body)
            ids[ticket.id] = posted.id
        else:
            try:
                self.api.update_comment(repo, cid, body)
            except GitHubNotFound:  # someone deleted it: post a fresh one
                ids[ticket.id] = self.api.post_comment(repo, number, body).id
        last[ticket.id] = body
        if self._status_flag("status_label", STATUS_LABEL):
            self._status_label(ticket, state, labeled)

    def _status_find(self, ticket: Any) -> Any | None:
        marker = status_marker(ticket.id)
        bot = (self.api.bot_login() or "").casefold()
        for c in self.api.list_issue_comments(ticket.repo, ticket.issue_number):
            if (c.author or "").casefold() == bot and marker in (c.body or ""):
                return c
        return None

    def _status_label(self, ticket: Any, state: str, labeled: set[int]) -> None:
        if state in ACTIVE_STATES and ticket.id not in labeled:
            self.api.add_labels(ticket.repo, ticket.issue_number, [WORKING_LABEL])
            labeled.add(ticket.id)
        elif state in TERMINAL_STATES:
            self.api.remove_label(ticket.repo, ticket.issue_number, WORKING_LABEL)
            labeled.discard(ticket.id)


__all__ = [
    "ACTIVE_STATES",
    "STATES",
    "STATUS_COMMENT",
    "STATUS_LABEL",
    "STATUS_MARKER",
    "STATUS_SHOW_COST",
    "TERMINAL_STATES",
    "WORKING_LABEL",
    "StatusCommentSteps",
    "render_status",
    "status_marker",
]
