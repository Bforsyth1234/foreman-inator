"""``fman replay <ticket>`` (todo #28, wired in #29): a recorded ticket's timeline, whether
its recording reproduces its trace, and whether today's logic decides as recorded."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from foreman_inator import paths
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.routing import escalation, explore
from foreman_inator.tracing import replay

# =========================================================================== fman replay


def replay_report(
    ticket_id: int,
    *,
    recordings_dir: Path,
    traces_dir: Path | None,
    store: Store | None = None,
    rows: replay.TicketRows | None = None,
    ladder: Any = (),
) -> replay.ReplayReport:
    """``replay.replay_ticket`` with the ticket's stored explore decision (#23)."""
    if rows is None and store is not None:
        t = store.get_ticket(ticket_id)
        rows = replay.ticket_rows(
            store,
            ticket_id,
            ladder=ladder,
            recordings_dir=recordings_dir,
            traces_dir=traces_dir,
            explore=explore.decode(store.get_cursor(t.repo, explore.explore_cursor(t.id))),
        )
    tl = replay.timeline(recordings_dir, ticket_id, traces_dir=traces_dir, rows=rows)
    diffs: list[str] = []
    if traces_dir is not None and replay.load_span_rows(traces_dir, ticket_id):
        diffs = replay.diff_trees(
            replay.replay_spans(recordings_dir, traces_dir, ticket_id),
            replay.recorded_span_tree(traces_dir, ticket_id),
        )
    return replay.ReplayReport(
        ticket_id, tl, tuple(diffs), replay.regress(rows) if rows is not None else None
    )


def replay_command(
    ticket: str,
    *,
    recordings_dir: Path | None = None,
    traces_dir: Path | None = None,
    rows_json: Path | None = None,
    config: Config | None = None,
    echo: Callable[[str], None] = print,
    port: int = 8642,
) -> int:
    """``fman replay <ticket>``: 0 when the recording replays its trace and the current
    logic agrees with it, 1 otherwise, 2 for a bad id."""
    from foreman_inator import config as config_mod

    tid = replay.parse_id(ticket)
    if tid is None:
        echo(f"not a ticket id: {ticket!r}")
        return 2
    rec = recordings_dir or paths.recordings_dir()
    traces = traces_dir or paths.traces_dir()
    if not replay.load_ticket(rec, tid):
        echo(f"no recordings for ticket {tid} in {rec}")
        return 1
    cfg = config or config_mod.load()
    ladder = escalation.build_ladder(cfg.models)
    rows = None
    if rows_json is not None:
        data = json.loads(Path(rows_json).read_text(encoding="utf-8"))
        rows = replay.rows_from_json(data, recordings_dir=rec, traces_dir=traces)
    store = None
    db = paths.db_path()
    if rows is None and db.exists():
        store = Store(db, read_only=True)
    try:
        if store is not None:
            try:
                store.get_ticket(tid)
            except Exception:
                store.close()
                store = None
        report = replay_report(tid, recordings_dir=rec, traces_dir=traces, store=store,
                               rows=rows, ladder=ladder)  # fmt: skip
    finally:
        if store is not None:
            store.close()
    for line in report.lines():
        echo(line)
    echo(f"Trace view: http://127.0.0.1:{port}/traces/{tid} (run `fman metrics`)")
    disagrees = report.regression is not None and not report.regression.agrees
    return 1 if report.tree_diffs or disagrees else 0


__all__ = ["replay_command", "replay_report"]
