"""Post-merge quality signal (#24 / todo 47): what happened after the user merged.

A merge counts as "accepted" forever. ``scan`` looks back over tickets merged in the last
``SCAN_WINDOW_DAYS`` and records, once per (ticket, kind), in ``post_merge_signals``:

- ``reverted``: a commit on the ticket's base branch, after the merge, that reverts it.
  Either its message says ``This reverts commit <sha>`` with the PR's
  ``merge_commit_sha`` (``git revert``, or GitHub's Revert button merged as a merge
  commit), or (the sha differs when the revert PR was squash-merged) it contains GitHub's
  ``Revert "<PR title>"`` together with a reference to the PR number (``#N`` or
  ``owner/repo#N``, which the Revert button puts in the body as ``Reverts owner/repo#N``).
- ``followup``: a later accepted ticket in the same repo, filed within ``FOLLOWUP_DAYS``
  of the merge, whose owned files overlap this ticket's. Read from the database only.

GitHub calls are bounded: one ``get_pr`` per merged ticket in the window that has no
``reverted`` signal yet, and one ``list_commits`` per (repo, base branch) per scan, from
the oldest such merge on. A failing repo is reported in ``ScanReport.errors`` and skipped;
the rest of the scan goes on.

The display wave reads ``quality_by_model`` (counts per writer model) and
``Store.list_post_merge_signals`` (per ticket); ``LABELS`` holds the wording.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from foreman_inator.db.store import Store, Ticket, _ts
from foreman_inator.github.client import Commit

SCAN_WINDOW_DAYS = 14
FOLLOWUP_DAYS = 7
#: How many overlapping file names a follow-up's ``detail`` lists.
DETAIL_FILES = 5

LABELS: dict[str, str] = {
    "reverted": "accepted, then reverted",
    "followup": "accepted, then followed up",
}

_REVERTS_SHA = re.compile(r"This reverts commit ([0-9a-fA-F]{7,40})")


@dataclass(frozen=True)
class ScanReport:
    tickets: int  # merged tickets in the window
    recorded: list[tuple[int, str]] = field(default_factory=list)  # (ticket_id, kind), new
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelQuality:
    model: str
    accepted: int  # accepted tickets whose accepted attempt ran on this model
    reverted: int
    followup: int


# --------------------------------------------------------------------------- matching


def is_revert_of(message: str, *, merge_sha: str, pr_title: str, pr_number: int) -> bool:
    """True when a commit message reverts the PR (see module doc for the two forms)."""
    sha = merge_sha.lower()
    if sha:
        for m in _REVERTS_SHA.finditer(message):
            if sha.startswith(m.group(1).lower()):
                return True
    if pr_title and f'Revert "{pr_title}"' in message:
        ref = re.compile(rf"(?<![\w#])(?:[\w.-]+/[\w.-]+)?#{int(pr_number)}(?!\d)")
        return ref.search(message) is not None
    return False


def find_revert(
    commits: Iterable[Commit],
    *,
    merge_sha: str,
    pr_title: str,
    pr_number: int,
    merged_at: datetime | None,
) -> Commit | None:
    """The earliest commit after ``merged_at`` that reverts the PR, or None."""
    hits = [
        c
        for c in commits
        if c.sha.lower() != merge_sha.lower()
        and (merged_at is None or c.committed_at is None or c.committed_at >= merged_at)
        and is_revert_of(c.message, merge_sha=merge_sha, pr_title=pr_title, pr_number=pr_number)
    ]
    if not hits:
        return None
    return min(hits, key=lambda c: (c.committed_at is None, c.committed_at or datetime.min))


def find_followup(
    ticket: Ticket, candidates: Iterable[Ticket], *, days: float = FOLLOWUP_DAYS
) -> tuple[Ticket, tuple[str, ...]] | None:
    """The first later accepted ticket in the same repo, filed within ``days`` of
    ``ticket``'s merge, sharing owned files; with the shared files. None if there is none."""
    if ticket.done_at is None or not ticket.owned_files:
        return None
    mine = set(ticket.owned_files)
    end = ticket.done_at + timedelta(days=days)
    best: tuple[Ticket, tuple[str, ...]] | None = None
    for c in candidates:
        if c.id == ticket.id or c.repo != ticket.repo or c.accepted_attempt_id is None:
            continue
        if not (ticket.done_at < c.created_at <= end):
            continue
        shared = tuple(sorted(mine.intersection(c.owned_files)))
        if shared and (best is None or c.created_at < best[0].created_at):
            best = (c, shared)
    return best


# --------------------------------------------------------------------------- scan


def scan(
    store: Store,
    api: Any,
    *,
    now: datetime | None = None,
    window_days: float = SCAN_WINDOW_DAYS,
    followup_days: float = FOLLOWUP_DAYS,
) -> ScanReport:
    """Record new post-merge signals for tickets merged in the last ``window_days``."""
    now = now or store.now()
    merged = store.merged_tickets(since=now - timedelta(days=window_days))
    report = ScanReport(tickets=len(merged))
    if not merged:
        return report
    _scan_followups(store, merged, followup_days, report)
    have_revert = {s.ticket_id for s in store.list_post_merge_signals(kind="reverted")}
    groups: dict[tuple[str, str], list[Ticket]] = {}
    for t in merged:
        if t.id not in have_revert:
            groups.setdefault((t.repo, t.base_branch), []).append(t)
    for (repo, base), tickets in groups.items():
        try:
            _scan_reverts(store, api, repo, base, tickets, report)
        except Exception as exc:
            report.errors.append(f"{repo}@{base}: {type(exc).__name__}: {exc}")
    return report


def _scan_followups(
    store: Store, merged: Sequence[Ticket], days: float, report: ScanReport
) -> None:
    have = {s.ticket_id for s in store.list_post_merge_signals(kind="followup")}
    for t in merged:
        if t.id in have:
            continue
        hit = find_followup(t, merged, days=days)
        if hit is None:
            continue
        other, shared = hit
        files = ", ".join(shared[:DETAIL_FILES]) + (" ..." if len(shared) > DETAIL_FILES else "")
        detail = f"ticket {other.id} (issue #{other.issue_number}) touched {files}"
        if store.record_post_merge_signal(t.id, "followup", at=other.created_at, detail=detail):
            report.recorded.append((t.id, "followup"))


def _scan_reverts(
    store: Store, api: Any, repo: str, base: str, tickets: Sequence[Ticket], report: ScanReport
) -> None:
    prs: list[tuple[Ticket, Any]] = []
    for t in tickets:
        assert t.pr_number is not None
        try:
            prs.append((t, api.get_pr(repo, t.pr_number)))
        except Exception as exc:
            report.errors.append(f"{repo}#{t.pr_number}: {type(exc).__name__}: {exc}")
    if not prs:
        return
    starts = [pr.merged_at or t.done_at for t, pr in prs]
    since = min(s for s in starts if s is not None)
    commits = api.list_commits(repo, sha=base, since=since)
    for t, pr in prs:
        hit = find_revert(
            commits,
            merge_sha=getattr(pr, "merge_commit_sha", "") or "",
            pr_title=pr.title,
            pr_number=t.pr_number or pr.number,
            merged_at=pr.merged_at or t.done_at,
        )
        if hit is None:
            continue
        subject = hit.message.splitlines()[0] if hit.message else ""
        detail = f"{hit.sha[:12]} {subject}"[:200]
        at = hit.committed_at or store.now()
        if store.record_post_merge_signal(t.id, "reverted", at=at, detail=detail):
            report.recorded.append((t.id, "reverted"))


# --------------------------------------------------------------------------- reads


def quality_by_model(store: Store, *, since: datetime | None = None) -> list[ModelQuality]:
    """Per writer model of the accepted attempt: accepted tickets, and how many of them
    were later reverted / followed up. ``since`` filters on the merge time (done_at)."""
    sql = (
        "SELECT a.model AS model, COUNT(*) AS accepted,"
        " SUM(EXISTS (SELECT 1 FROM post_merge_signals s"
        "  WHERE s.ticket_id = t.id AND s.kind = 'reverted')) AS reverted,"
        " SUM(EXISTS (SELECT 1 FROM post_merge_signals s"
        "  WHERE s.ticket_id = t.id AND s.kind = 'followup')) AS followup"
        " FROM tickets t JOIN attempts a ON a.id = t.accepted_attempt_id"
        " WHERE t.\"column\" = 'done'"
    )
    params: list[Any] = []
    if since is not None:
        sql += " AND t.done_at >= ?"
        params.append(_ts(since))
    rows = store.query(sql + " GROUP BY a.model ORDER BY a.model", params)
    return [
        ModelQuality(r["model"], int(r["accepted"]), int(r["reverted"]), int(r["followup"]))
        for r in rows
    ]


__all__ = [
    "FOLLOWUP_DAYS",
    "LABELS",
    "SCAN_WINDOW_DAYS",
    "ModelQuality",
    "ScanReport",
    "find_followup",
    "find_revert",
    "is_revert_of",
    "quality_by_model",
    "scan",
]
