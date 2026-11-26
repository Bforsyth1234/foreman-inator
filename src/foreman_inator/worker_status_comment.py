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

ab:pair (#34, GH #16, wave 23): the two sides of a pair are two tickets on one issue but
share ONE status comment, keyed by side A's marker (``ticket=<ticket_a>``, so a pair
added late turns A's existing comment into the pair's). It has one heading line per side
(``**Side A:** ...`` / ``**Side B:** ...``); an update rewrites only its own side's line
and keeps the other's (from memory, or after a restart from the comment's current body).
Once both sides have a PR, one note is posted once (``post_once``'s marker, key
``pair_prs``): "Two PRs: #x, #y. Merge the one you prefer; the other is closed." With
``status_label``, ``fman:working`` is removed only when neither side is active. An
unpaired ticket's comment is unchanged.

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

#: ``worker_actions.NOTE_MARKER``'s format (``post_once``), so either path finds the note.
NOTE_MARKER = "<!-- foreman-inator:note ticket={ticket_id} key={key} -->"
PAIR_NOTE_KEY = "pair_prs"
PAIR_NOTE = "Two PRs: #{a}, #{b}. Merge the one you prefer; the other is closed."
SIDE_HEADING = "**Side {side}:** "
SIDE_NOT_STARTED = "not started yet."

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


def side_line(side: str, text: str) -> str:
    return SIDE_HEADING.format(side=side) + text


def pair_body(ticket_a: int, side_a: str | None, side_b: str | None) -> str:
    """The pair's one comment: side A's marker, then one heading line per side."""
    return "\n".join(
        (
            status_marker(ticket_a),
            side_line("A", side_a or SIDE_NOT_STARTED),
            side_line("B", side_b or SIDE_NOT_STARTED),
        )
    )


def side_text(body: str | None, side: str) -> str | None:
    """A side's text from an existing comment body. Side A also takes the one line of an
    unpaired comment (a pair added after A's comment was posted)."""
    if not body:
        return None
    lines = [x for x in body.splitlines() if x.strip() and "<!--" not in x]
    heading = SIDE_HEADING.format(side=side)
    for x in lines:
        if x.startswith(heading):
            return x[len(heading) :] or None
    if side == "A" and lines and not any(x.startswith("**Side ") for x in lines):
        return lines[0]
    return None


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
            text = render_status(state, show_cost=show_cost, **facts)
            pair = self._status_pair(ticket)
            body = f"{status_marker(ticket.id)}\n{text}"
        except Exception as exc:
            log.warning(
                "status comment for ticket %s skipped: %s: %s", ticket.id, type(exc).__name__, exc
            )
            return

        def write() -> None:
            try:
                if pair is None:
                    self._status_write(ticket, state, body)
                else:
                    self._status_write_pair(ticket, pair, state, text, facts.get("pr_number"))
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

    def _status_find(self, ticket: Any, key: int | None = None) -> Any | None:
        marker = status_marker(ticket.id if key is None else key)
        bot = (self.api.bot_login() or "").casefold()
        for c in self.api.list_issue_comments(ticket.repo, ticket.issue_number):
            if (c.author or "").casefold() == bot and marker in (c.body or ""):
                return c
        return None

    # ------------------------------------------------------------- ab:pair (#34)

    def _status_pair(self, ticket: Any) -> Any | None:
        """The ticket's pair (any status), or None: unpaired, or a store without pairs."""
        find = getattr(self.store, "pair_for_ticket", None)
        if find is None:
            return None
        try:
            return find(ticket.id)
        except Exception:
            return None

    def _status_pair_state(self) -> tuple[dict[int, str], dict[int, str], set[int]]:
        d = self.__dict__
        return (
            d.setdefault("_status_side_text", {}),
            d.setdefault("_status_side_state", {}),
            d.setdefault("_status_pair_noted", set()),
        )

    def _status_write_pair(
        self, ticket: Any, pair: Any, state: str, text: str, pr_number: int | None
    ) -> None:
        """Rewrite this side's line of the pair's one comment (module docstring)."""
        ids, last, labeled = self._status_state()
        sides, states, _ = self._status_pair_state()
        key = int(pair.ticket_a)
        sides[ticket.id] = text
        states[ticket.id] = state
        repo, number = ticket.repo, ticket.issue_number
        cid = ids.get(key)
        prev = last.get(key)
        if cid is None:
            found = self._status_find(ticket, key)
            if found is not None:
                cid = found.id
                ids[key] = cid
                prev = found.body or ""
        for tid, side in ((pair.ticket_a, "A"), (pair.ticket_b, "B")):
            if tid not in sides:
                got = side_text(prev, side)
                if got is not None:
                    sides[tid] = got
        body = pair_body(key, sides.get(pair.ticket_a), sides.get(pair.ticket_b))
        if prev != body:
            if cid is None:
                ids[key] = self.api.post_comment(repo, number, body).id
            else:
                try:
                    self.api.update_comment(repo, cid, body)
                except GitHubNotFound:  # someone deleted it: post a fresh one
                    ids[key] = self.api.post_comment(repo, number, body).id
        last[key] = body
        if self._status_flag("status_label", STATUS_LABEL):
            self._status_pair_label(ticket, pair, state, labeled)
        if state == "pr_opened":
            self._status_pair_note(ticket, pair, pr_number)

    def _status_side_active(self, ticket_id: int) -> bool:
        """The side's last written state is active; unknown (a restart): in progress."""
        _, states, _ = self._status_pair_state()
        if ticket_id in states:
            return states[ticket_id] in ACTIVE_STATES
        try:
            return self.store.get_ticket(ticket_id).column == "in_progress"
        except Exception:
            return False

    def _status_pair_label(self, ticket: Any, pair: Any, state: str, labeled: set[int]) -> None:
        key = int(pair.ticket_a)
        if state in ACTIVE_STATES and key not in labeled:
            self.api.add_labels(ticket.repo, ticket.issue_number, [WORKING_LABEL])
            labeled.add(key)
        elif state in TERMINAL_STATES and not self._status_side_active(pair.other(ticket.id)):
            self.api.remove_label(ticket.repo, ticket.issue_number, WORKING_LABEL)
            labeled.discard(key)

    def _status_pair_note(self, ticket: Any, pair: Any, pr_number: int | None) -> None:
        """Post "Two PRs: #x, #y. ..." once both sides of a running pair have a PR."""
        _, _, noted = self._status_pair_state()
        if pair.id in noted:
            return
        try:
            fresh = self.store.get_pair(pair.id)
        except Exception:
            fresh = pair
        if getattr(fresh, "status", "running") != "running":
            return
        prs: dict[int, int | None] = {}
        for tid in (pair.ticket_a, pair.ticket_b):
            try:
                prs[tid] = self.store.get_ticket(tid).pr_number
            except Exception:
                prs[tid] = None
        if pr_number is not None:
            prs[ticket.id] = int(pr_number)
        a, b = prs[pair.ticket_a], prs[pair.ticket_b]
        if a is None or b is None:
            return
        marker = NOTE_MARKER.format(ticket_id=int(pair.ticket_a), key=PAIR_NOTE_KEY)
        bot = (self.api.bot_login() or "").casefold()
        for c in self.api.list_issue_comments(ticket.repo, ticket.issue_number):
            if (c.author or "").casefold() == bot and marker in (c.body or ""):
                noted.add(pair.id)
                return
        note = PAIR_NOTE.format(a=int(a), b=int(b))
        self.api.post_comment(ticket.repo, ticket.issue_number, f"{marker}\n{note}")
        noted.add(pair.id)

    def _status_label(self, ticket: Any, state: str, labeled: set[int]) -> None:
        if state in ACTIVE_STATES and ticket.id not in labeled:
            self.api.add_labels(ticket.repo, ticket.issue_number, [WORKING_LABEL])
            labeled.add(ticket.id)
        elif state in TERMINAL_STATES:
            self.api.remove_label(ticket.repo, ticket.issue_number, WORKING_LABEL)
            labeled.discard(ticket.id)


__all__ = [
    "ACTIVE_STATES",
    "PAIR_NOTE",
    "PAIR_NOTE_KEY",
    "STATES",
    "STATUS_COMMENT",
    "STATUS_LABEL",
    "STATUS_MARKER",
    "STATUS_SHOW_COST",
    "TERMINAL_STATES",
    "WORKING_LABEL",
    "StatusCommentSteps",
    "pair_body",
    "render_status",
    "side_text",
    "status_marker",
]
