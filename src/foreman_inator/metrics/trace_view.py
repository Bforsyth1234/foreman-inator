"""The trace view: step through a recorded ticket on the local metrics page (todo #28).

Route handlers for #29 to mount next to the metrics page::

    create_app(store_path, extra_routes=trace_routes(recordings_dir, traces_dir,
               store_path=store_path, ladder=ladder), nav=NAV)

* ``GET /traces``: the tickets that have recordings (title and column from the store
  when one is given).
* ``GET /traces/{ticket}``: one page per ticket: the decision check (``replay.regress``:
  what the current router, escalation, review policy and lifecycle would decide, and
  where that differs from what was recorded), whether the replayed span tree matches the
  trace, then a timeline per recording (attempt or review), each a ``<details>`` block,
  each step another, with long text clipped behind a visible marker and the step a
  kickback's ``failure_step_index`` (#27) points at highlighted and opened.
* ``GET /traces/{ticket}/{attempt}``: the same page cut to one attempt and its review.

Each attempt's block also shows the machine's state the attempt span recorded (#59,
shown by #60): the ``resource.*`` attributes at its start and ``resource.end.*`` at its
end (memory used / total / free %, pressure, the models Ollama held), read from the
ticket's span files.

Server-rendered with ``web.render_page``; no JavaScript, nothing fetched from anywhere.
Everything that comes from a recording, a span file or the store (issue text, model
output, tool output, notes) is untrusted and HTML-escaped. Ticket and attempt ids must be
plain ASCII integers (``replay.parse_id``); anything else is a 404 before any path is
built, so a request can only read ``<recordings>/<int>/`` and ``<traces>/<int>/``. The
app's loopback guard (``web.LoopbackHostOnly``, applied by ``create_app``) keeps the
pages local (invariant 7c).
"""

from __future__ import annotations

import html
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import BaseRoute, Route

from foreman_inator.db.store import NotFoundError, Store
from foreman_inator.metrics.web import fmt_bytes, render_page
from foreman_inator.routing.escalation import Rung
from foreman_inator.tracing import replay

e = html.escape

#: The nav link #29 passes to ``create_app(nav=...)``.
NAV: tuple[tuple[str, str], ...] = (("/traces", "Traces"),)

RowsFor = Callable[[int], "replay.TicketRows | None"]

_STYLE = """<style>
ol.steps { padding-left: 1.2rem; }
ol.steps > li { margin: 0.25rem 0; }
li.error > details > summary { color: var(--danger); }
li.failure { outline: 2px solid var(--warn); border-radius: 6px; padding: 0.2rem 0.4rem; }
.failure-label { color: var(--warn); font-weight: 600; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--row);
  padding: 0.5rem; border-radius: 6px; max-height: 30rem; overflow: auto; }
.clipped { color: var(--muted); font-style: italic; }
details.recording { border: 1px solid var(--line); border-radius: 8px;
  padding: 0.4rem 0.8rem; margin: 0.6rem 0; }
details.recording > summary { font-weight: 600; cursor: pointer; }
.diff td { color: var(--danger); }
table.resources { width: auto; margin: 0.4rem 0; }
</style>"""


def store_rows_for(
    store_path: str | Path,
    *,
    ladder: Sequence[Rung],
    recordings_dir: str | Path,
    traces_dir: str | Path | None,
) -> RowsFor:
    """``ticket id -> TicketRows`` from a read-only store, or None when there is no store
    or no such ticket."""

    def rows_for(ticket_id: int) -> replay.TicketRows | None:
        path = Path(store_path)
        if not path.exists():
            return None
        try:
            store = Store(path, read_only=True)
        except sqlite3.Error:
            return None
        try:
            return replay.ticket_rows(
                store,
                ticket_id,
                ladder=ladder,
                recordings_dir=recordings_dir,
                traces_dir=traces_dir,
            )
        except (NotFoundError, sqlite3.Error):
            return None
        finally:
            store.close()

    return rows_for


# --------------------------------------------------------------------------- rendering


def _num(n: float | int | None) -> str:
    return "-" if n is None else f"{n:,}"


def _step(s: replay.Step, anchor: str) -> str:
    cls = ["step", f"kind-{s.kind}"]
    if s.is_error:
        cls.append("error")
    if s.failure_step:
        cls.append("failure")
    where = f"row {s.row_index}" if s.row_index is not None else "store"
    bits = [e(s.title)]
    if s.tokens_in is not None or s.tokens_out is not None:
        bits.append(f"{_num(s.tokens_in)} in / {_num(s.tokens_out)} out")
    if s.cost_usd:
        bits.append(f"${s.cost_usd:.4f}")
    label = ""
    if s.failure_step:
        label = f' <span class="failure-label">failure step: {e(s.failure_step)}</span>'
    inner = []
    if s.input:
        inner.append(f"<p><small>input</small></p><pre>{e(s.input)}</pre>")
    if s.text:
        inner.append(f"<pre>{e(s.text)}</pre>")
    if s.clipped:
        inner.append(f'<p class="clipped">[clipped: {s.clipped:,} more characters not shown]</p>')
    if not inner:
        inner.append("<p><small>(no content)</small></p>")
    opened = " open" if s.failure_step else ""
    return (
        f'<li id="{e(anchor)}" class="{" ".join(cls)}"><details{opened}>'
        f"<summary><small>{e(where)}</small> &middot; {' &middot; '.join(bits)}{label}</summary>"
        f"{''.join(inner)}</details></li>"
    )


# --------------------------------------------------------------------------- resources

RES_PREFIX = "resource."
RES_END_PREFIX = "resource.end."
#: (key under ``resource.`` / ``resource.end.``, label), in display order.
_RES_KEYS: tuple[tuple[str, str], ...] = (
    ("mem_used_bytes", "Memory used"),
    ("mem_total_bytes", "Memory total"),
    ("mem_free_pct", "Free memory"),
    ("mem_pressure", "Memory pressure"),
    ("ollama_loaded", "Ollama loaded"),
)

Resources = Mapping[int, Mapping[str, Any]]


def attempt_resources(traces_dir: str | Path | None, ticket_id: int) -> dict[int, dict[str, Any]]:
    """Attempt id → the ``resource.*`` attributes of its ``attempt`` span (none: absent)."""
    if traces_dir is None:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in replay.load_span_rows(traces_dir, ticket_id):
        if row.get("name") != "attempt":
            continue
        attrs = row.get("attributes") or {}
        aid = attrs.get("attempt.id") if isinstance(attrs, dict) else None
        if not isinstance(aid, int) or isinstance(aid, bool):
            continue
        got = {k: v for k, v in attrs.items() if isinstance(k, str) and k.startswith(RES_PREFIX)}
        if got:
            out.setdefault(aid, {}).update(got)
    return out


def _ollama_cell(value: Any) -> str:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return e(str(value))
    if isinstance(data, dict) and "unreachable" in data:
        return f"unreachable ({e(str(data['unreachable']))})"
    if isinstance(data, list):
        if not data:
            return "none"
        return ", ".join(
            f"{e(str(m.get('name')))} ({e(fmt_bytes(m.get('size')))})"
            for m in data
            if isinstance(m, dict)
        )
    return e(str(value))


def _res_cell(key: str, value: Any) -> str:
    if value is None:
        return "-"
    if key.endswith("_bytes"):
        return e(fmt_bytes(value))
    if key == "mem_free_pct":
        return f"{e(str(value))}%"
    if key == "ollama_loaded":
        return _ollama_cell(value)
    return e(str(value))


def _resources_table(attrs: Mapping[str, Any]) -> str:
    start = {k[len(RES_PREFIX) :]: v for k, v in attrs.items() if k.startswith(RES_PREFIX)}
    start = {k: v for k, v in start.items() if not k.startswith("end.")}
    end = {k[len(RES_END_PREFIX) :]: v for k, v in attrs.items() if k.startswith(RES_END_PREFIX)}
    known = [k for k, _ in _RES_KEYS]
    labels = dict(_RES_KEYS)
    extra = sorted((set(start) | set(end)) - set(known))
    rows = "".join(
        f"<tr><th>{e(labels.get(k, k))}</th><td>{_res_cell(k, start.get(k))}</td>"
        f"<td>{_res_cell(k, end.get(k))}</td></tr>"
        for k in [*known, *extra]
        if k in start or k in end
    )
    return (
        '<div class="scroll"><table class="resources"><thead><tr><th>Machine</th>'
        f"<th>At start</th><th>At end</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _recording(r: replay.RecordingTimeline, resources: Resources | None = None) -> str:
    what = "Attempt" if r.role == "attempt" else f"{r.role.capitalize()} of attempt"
    head = [f"{what} {r.attempt_id}", e(r.model or "?")]
    if r.tier is not None:
        head.append(f"tier {r.tier}")
    if r.trigger:
        head.append(e(r.trigger))
    head.append(e(r.outcome or ("running or interrupted" if r.partial else "no outcome")))
    if r.tokens_in or r.tokens_out:
        head.append(f"{_num(r.tokens_in)} + {_num(r.tokens_out)} tokens")
    if r.cost_usd:
        head.append(f"${r.cost_usd:.4f}")
    if r.failure_steps:
        head.append('<span class="failure-label">failure step marked</span>')
    # Attempts keep ``a<id>``; any other role gets a separator so ``fork_1`` of attempt 15
    # and ``fork_11`` of attempt 5 never share an anchor (both were ``fork_115``).
    prefix = f"a{r.attempt_id}" if r.role == "attempt" else f"{e(r.role)}-a{r.attempt_id}"
    steps = "".join(_step(s, f"{prefix}-{i}") for i, s in enumerate(r.steps))
    variant = f"<p><small>variant {e(r.variant_id)}</small></p>" if r.variant_id else ""
    res = ""
    attrs = (resources or {}).get(r.attempt_id) if r.role == "attempt" else None
    if attrs:
        res = _resources_table(attrs)
    return (
        f'<details class="recording" id="{prefix}" open><summary>{" &middot; ".join(head)}'
        f'</summary>{variant}{res}<ol class="steps">{steps}</ol></details>'
    )


def _decisions(reg: replay.Regression | None) -> str:
    if reg is None:
        return (
            "<h2>Decision check</h2><p class=empty>No store rows for this ticket, so the "
            "decisions cannot be checked.</p>"
        )
    if reg.agrees:
        return (
            f"<h2>Decision check</h2><p>The current logic agrees with history "
            f"({reg.decisions} decisions checked).</p>"
        )
    rows = "".join(
        f"<tr><td>{e(d.kind)}</td><td>{e(d.at)}</td><td>{e(d.recorded)}</td>"
        f"<td>{e(d.current)}</td></tr>"
        for d in reg.diffs
    )
    return (
        f"<h2>Decision check</h2><p class=error>{len(reg.diffs)} of {reg.decisions} "
        "decisions differ from what was recorded.</p>"
        '<div class="scroll"><table class="diff"><thead><tr><th>Decision</th><th>Where</th>'
        f"<th>Recorded</th><th>Current logic</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _tree(diffs: Sequence[str], has_trace: bool) -> str:
    if not has_trace:
        return (
            "<p><small>No span file for this ticket: timeline from the recordings only.</small></p>"
        )
    if not diffs:
        return "<p><small>Replayed span tree: identical to the trace.</small></p>"
    items = "".join(f"<li>{e(d)}</li>" for d in diffs)
    return f"<p class=error>Replayed span tree differs from the trace:</p><ul>{items}</ul>"


def render_ticket(
    ticket_id: int,
    report: replay.ReplayReport,
    rows: replay.TicketRows | None,
    *,
    has_trace: bool,
    attempt_id: int | None = None,
    nav: Sequence[tuple[str, str]] = NAV,
    resources: Resources | None = None,
) -> str:
    title = f"Ticket {ticket_id}"
    lede = ""
    if rows is not None:
        t = rows.ticket
        title += f": {t.title}"
        lede = (
            f'<p class="lede">{e(t.repo)} #{t.issue_number} &middot; {e(t.column)}'
            f" &middot; routed tier {e(str(t.routed_tier) if t.routed_tier else '-')}"
            + (f" &middot; flagged: {e(t.flag_reason)}" if t.flag_reason else "")
            + "</p>"
        )
    recs = report.timeline.recordings
    if attempt_id is not None:
        recs = tuple(r for r in recs if r.attempt_id == attempt_id)
        title += f", attempt {attempt_id}"
    body = (
        f"{_STYLE}<h1>{e(title)}</h1>{lede}"
        f'<p><a href="/traces">All recorded tickets</a></p>'
        f"{_decisions(report.regression)}{_tree(report.tree_diffs, has_trace)}"
        "<h2>Timeline</h2>"
        + ("".join(_recording(r, resources) for r in recs) or "<p class=empty>No recordings.</p>")
    )
    return render_page(title, body, nav=nav)


def render_list(
    tickets: Sequence[int], rows_for: RowsFor | None, *, nav: Sequence[tuple[str, str]] = NAV
) -> str:
    if not tickets:
        body = "<h1>Recorded tickets</h1><p class=empty>No recordings yet.</p>"
        return render_page("Recorded tickets", body, nav=nav)
    items = []
    for tid in sorted(tickets, reverse=True):
        rows = rows_for(tid) if rows_for else None
        extra = ""
        if rows is not None:
            t = rows.ticket
            extra = (
                f" &middot; {e(t.title)} &middot; {e(t.repo)} #{t.issue_number}"
                f" &middot; {e(t.column)}"
            )
        items.append(f'<li><a href="/traces/{tid}">Ticket {tid}</a>{extra}</li>')
    body = (
        "<h1>Recorded tickets</h1><p class=lede>Step through any recorded run; "
        "no model is called.</p><ul>" + "".join(items) + "</ul>"
    )
    return render_page("Recorded tickets", body, nav=nav)


def _not_found(what: str, nav: Sequence[tuple[str, str]]) -> Response:
    body = f"<h1>Not found</h1><p>{e(what)}</p>"
    return HTMLResponse(render_page("Not found", body, nav=nav), 404)


# --------------------------------------------------------------------------- routes


def trace_routes(
    recordings_dir: str | Path,
    traces_dir: str | Path | None = None,
    *,
    store_path: str | Path | None = None,
    ladder: Sequence[Rung] = (),
    rows_for: RowsFor | None = None,
    nav: Sequence[tuple[str, str]] = NAV,
) -> list[BaseRoute]:
    """The trace-view routes. ``store_path`` (with the ladder in force) adds titles and the
    decision check; ``rows_for`` replaces the store lookup (tests, a JSON case)."""
    if rows_for is None and store_path is not None:
        rows_for = store_rows_for(
            store_path, ladder=ladder, recordings_dir=recordings_dir, traces_dir=traces_dir
        )

    def listing(request: Request) -> Response:
        return HTMLResponse(render_list(replay.recorded_tickets(recordings_dir), rows_for, nav=nav))

    def page(request: Request) -> Response:
        tid = replay.parse_id(request.path_params.get("ticket"))
        if tid is None:
            return _not_found("A ticket id is a plain number.", nav)
        aid = None
        if "attempt" in request.path_params:
            aid = replay.parse_id(request.path_params["attempt"])
            if aid is None:
                return _not_found("An attempt id is a plain number.", nav)
        if not replay.load_ticket(recordings_dir, tid):
            return _not_found(f"No recordings for ticket {tid}.", nav)
        rows = rows_for(tid) if rows_for else None
        tl = replay.timeline(recordings_dir, tid, traces_dir=traces_dir, rows=rows)
        has_trace = traces_dir is not None and bool(replay.load_span_rows(traces_dir, tid))
        diffs: list[str] = []
        if has_trace:
            diffs = replay.diff_trees(
                replay.replay_spans(recordings_dir, traces_dir, tid),
                replay.recorded_span_tree(traces_dir, tid),  # type: ignore[arg-type]
            )
        report = replay.ReplayReport(
            tid, tl, tuple(diffs), replay.regress(rows) if rows is not None else None
        )
        if aid is not None and not any(r.attempt_id == aid for r in tl.recordings):
            return _not_found(f"No recording of attempt {aid} in ticket {tid}.", nav)
        resources = attempt_resources(traces_dir, tid) if has_trace else {}
        return HTMLResponse(
            render_ticket(
                tid, report, rows, has_trace=has_trace, attempt_id=aid, nav=nav,
                resources=resources,
            )
        )  # fmt: skip

    return [
        Route("/traces", listing, methods=["GET"]),
        Route("/traces/{ticket}", page, methods=["GET"]),
        Route("/traces/{ticket}/{attempt}", page, methods=["GET"]),
    ]


__all__ = [
    "NAV",
    "attempt_resources",
    "render_list",
    "render_ticket",
    "store_rows_for",
    "trace_routes",
]
