"""``fman status`` and ``fman repo set-verify`` (todo #14).

``render_status`` reads the store only (open it with ``read_only=True``), so it works
whether or not the worker is running. ``set_verify_command`` saves a repo's verify
command and lifts the "no verify command" flag from that repo's open tickets, so the
running worker picks them up on its next pass.
"""

from __future__ import annotations

from collections.abc import Sequence

from foreman_inator.db.store import COLUMNS, Attempt, Store, Ticket
from foreman_inator.worker_policy import FLAG_NO_VERIFY

_TITLES = {
    "todo": "TODO",
    "in_progress": "IN PROGRESS",
    "testing": "TESTING (waiting for your review)",
    "done": "DONE",
    "dropped": "DROPPED",
}
TITLE_WIDTH = 40


def _attempt_text(attempts: Sequence[Attempt]) -> str:
    if not attempts:
        return "not started"
    a = attempts[-1]
    if a.ended_at is None:
        state = "running"
    elif a.outcome is None:
        state = "checks passed" if a.tests_passed else "finished"
    else:
        state = a.outcome.replace("_", " ")
    cost = f", ${a.cost_usd:.2f}" if a.cost_usd else ""
    return f"attempt {len(attempts)} ({a.trigger}, {a.model}, tier {a.tier}{cost}): {state}"


def _line(t: Ticket, attempts: Sequence[Attempt]) -> str:
    title = " ".join(t.title.split())
    if len(title) > TITLE_WIDTH:
        title = title[: TITLE_WIDTH - 1] + "…"
    parts = [
        f"  #{t.id:<4} {t.repo}#{t.issue_number:<5} {title:<{TITLE_WIDTH}}",
        _attempt_text(attempts),
    ]
    if t.pr_number is not None:
        parts.append(f"PR #{t.pr_number}")
    line = "  ".join(parts)
    if t.flag_reason:
        line += f"\n         FLAGGED: {t.flag_reason}"
    return line


def render_status(store: Store, *, closed_limit: int = 5) -> str:
    """Tickets by column with their current attempt, flags and PR numbers."""
    tickets = store.list_tickets()
    by_col: dict[str, list[Ticket]] = {c: [] for c in COLUMNS}
    for t in tickets:
        by_col[t.column].append(t)
    out: list[str] = []
    for col in COLUMNS:
        items = by_col[col]
        shown = items
        if col in ("done", "dropped") and closed_limit >= 0:
            shown = items[-closed_limit:] if closed_limit else []
        header = f"{_TITLES[col]} ({len(items)})"
        if len(shown) < len(items):
            header += f", latest {len(shown)}"
        out.append(header)
        out.extend(_line(t, store.list_attempts(t.id)) for t in shown)
        if not items:
            out.append("  (none)")
        out.append("")
    flagged = sum(1 for t in tickets if t.flag_reason and t.column not in ("done", "dropped"))
    out.append(f"{len(tickets)} ticket(s), {flagged} flagged for you.")
    return "\n".join(out)


def set_verify_command(store: Store, repo: str, command: str | None) -> list[int]:
    """Save ``repo``'s verify command (None clears it) and unflag the open tickets that
    were waiting for one. Returns their ids."""
    cleared: list[int] = []
    with store.transaction():
        store.set_verify_command(repo, command.strip() if command else None)
        if command and command.strip():
            for t in store.list_tickets(column=("todo", "in_progress", "testing"), repo=repo):
                if t.flag_reason and t.flag_reason.startswith(FLAG_NO_VERIFY):
                    store.clear_flag(t.id)
                    cleared.append(t.id)
    return cleared


__all__ = ["render_status", "set_verify_command"]
