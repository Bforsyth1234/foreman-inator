"""The local metrics page (todo #20, SPEC.md "Metrics view").

``create_app(store_path)`` is a Starlette app with:

* ``GET /``: one server-rendered HTML page: filter form (a plain GET form: model,
  repo, date range, trend bucket), the writer table (6 core metrics plus AI kickbacks
  per ticket, one row per writer model), the reviewer miss rate per reviewer model,
  writer + reviewer vs. strong writer alone, the two trend lines as inline SVG, and the
  latest closed tickets. No JavaScript, no external fonts, scripts or styles.
* ``GET /api/metrics``: the same numbers as JSON (both come from ``queries.report``).

Each request opens its own ``Store(path, read_only=True)`` (the worker writes
concurrently in another process) and closes it. Everything from the database is
HTML-escaped (issue titles are untrusted GitHub text). Requests whose ``Host`` header is
not a loopback name are refused (DNS-rebinding guard), and ``serve`` / ``MetricsServer``
bind to loopback only (invariant 7c: the page is local only).

Wave 9 (#29): ``full_app`` (what ``fman metrics`` serves) mounts the trace view (#28,
``/traces``, with each ticket's stored explore decision), ``/failures`` (failure steps per
writer variant, ``reviewer_miss`` per reviewer variant, the unlabeled share) and
``/experiments`` (champions, running and ended experiments with their replay comparison
and live judgment). All read-only, escaped, no JavaScript, behind the same loopback guard.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, is_dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from foreman_inator.db.store import Store
from foreman_inator.metrics import queries as q
from foreman_inator.pr.publisher import format_cost
from foreman_inator.pr.summary import format_duration
from foreman_inator.resources import snapshot as res

log = logging.getLogger("foreman_inator.metrics")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
NO_DATA = "no data"


def is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_name(header: str) -> str:
    header = header.strip()
    if header.startswith("["):  # [::1]:8642
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class LoopbackHostOnly:
    """Refuse requests whose Host header is not a loopback name (DNS rebinding)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1")
            if not is_loopback(_host_name(host)):
                resp = PlainTextResponse("metrics are served to localhost only", 403)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --- request parsing ------------------------------------------------------------------


class BadRequest(ValueError):
    pass


def parse_filter(params: Any) -> tuple[q.MetricsFilter, str]:
    """Query string → (filter, bucket). Raises ``BadRequest`` on a bad value."""

    def text(name: str) -> str | None:
        v = (params.get(name) or "").strip()
        return v or None

    def day(name: str) -> date | None:
        v = text(name)
        if v is None:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise BadRequest(f"{name} must be a date like 2026-11-23") from None

    bucket = text("bucket") or "week"
    if bucket not in q.BUCKETS:
        raise BadRequest(f"bucket must be one of {', '.join(q.BUCKETS)}")
    try:
        filt = q.MetricsFilter(
            model=text("model"), repo=text("repo"), since=day("since"), until=day("until")
        )
    except ValueError as e:
        raise BadRequest(str(e)) from None
    return filt, bucket


def jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


# --- formatting -----------------------------------------------------------------------

e = html.escape


def fmt_rate(r: q.Rate) -> str:
    if r.value is None:
        return NO_DATA
    return f"{r.value * 100:.0f}% <small>({r.numerator}/{r.denominator})</small>"


def fmt_mean(m: q.Mean, *, unit: str = "") -> str:
    if m.value is None:
        return NO_DATA
    return f"{m.value:.2f}{unit} <small>({m.total:g} over {m.count})</small>"


def fmt_cost(m: q.Mean) -> str:
    if m.value is None:
        return NO_DATA
    return f"{e(format_cost(m.value))} <small>({m.count} accepted)</small>"


def fmt_per_accepted(m: q.Mean, *, unit: str = "") -> str:
    """A per-accepted-ticket mean, shown like cost: value then the accepted count."""
    if m.value is None:
        return NO_DATA
    return f"{m.value:.1f}{e(unit)} <small>({m.count} accepted)</small>"


def fmt_kickbacks(k: q.KickbackStats) -> str:
    if k.value is None:
        return NO_DATA
    out = f"{k.value:.2f} <small>({k.total} over {k.tickets})</small>"
    if k.by_tag:
        tags = " · ".join(f"{e(tag)} {(k.per_tag[tag] or 0):.2f}" for tag in sorted(k.by_tag))
        out += f"<br><small>{tags}</small>"
    return out


def fmt_after_merge(n: int, of: int) -> str:
    """``n`` of ``of`` accepted tickets, with the share; "no data" with none accepted."""
    if not of:
        return NO_DATA
    return f"{n} <small>of {of} ({n / of * 100:.0f}%)</small>"


def fmt_duration(d: q.Duration) -> str:
    if d.median_seconds is None:
        return NO_DATA
    return f"{e(format_duration(d.median_seconds))} <small>(median of {d.count})</small>"


def fmt_bytes(n: Any) -> str:
    """Bytes as decimal GB (``21.3 GB``, as ``fman status`` shows Ollama), "-" if unknown."""
    if not isinstance(n, int | float) or isinstance(n, bool):
        return "-"
    return f"{n / 1e9:.1f} GB"


def fmt_age(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60} min"
    return f"{s // 3600} h"


# --- resources (#60) ------------------------------------------------------------------


def _as_of(reading: res.Reading) -> str:
    age = reading.age_seconds or 0.0
    text = f"as of {fmt_age(age)} ago"
    if reading.stale:
        return (
            f'<span class="error">{e(text)}: stale (older than '
            f"{int(res.STALE_AFTER_SECONDS // 60)} min), the worker may not be running</span>"
        )
    return f"<small>{e(text)}</small>"


def _ollama_summary(part: Any) -> str:
    if part is None:
        return "Ollama: no local model configured"
    if not isinstance(part, dict):
        return "Ollama: unknown"
    if "unreachable" in part:
        word = "slow" if part.get("slow") else "unreachable"
        return f"Ollama: {e(word)} ({e(str(part.get('unreachable')))})"
    models = [m for m in part.get("models") or [] if isinstance(m, dict)]
    total = sum(m.get("size") or 0 for m in models if isinstance(m.get("size"), int))
    return f"Ollama: {len(models)} loaded ({e(fmt_bytes(total))})"


def _memory_summary(mem: Any) -> str:
    if not isinstance(mem, dict):
        return "Memory: unknown"
    used, total = mem.get("used_bytes"), mem.get("total_bytes")
    text = f"Memory: {e(fmt_bytes(used))} of {e(fmt_bytes(total))} used"
    if isinstance(mem.get("free_pct"), int):
        text += f", {mem['free_pct']}% free"
    return text + f", pressure {e(str(mem.get('pressure') or 'unknown'))}"


def _slots_summary(slots: Any) -> str:
    if not isinstance(slots, dict):
        return "Local inference slots: unknown (no proxy)"
    return (
        f"Local inference slots: {e(str(slots.get('in_use')))} of {e(str(slots.get('cap')))} in use"
    )


def render_resources_block(reading: res.Reading) -> str:
    """The short resource block on the main page (links to ``/resources``)."""
    link = '<a href="/resources">details</a>'
    if reading.data is None:
        return (
            f'<p class="resources"><small>Resources: {e(reading.error or "")} · {link}</small></p>'
        )
    d = reading.data
    parts = [_ollama_summary(d.get("ollama")), _memory_summary(d.get("memory"))]
    parts.append(_slots_summary(d.get("local_slots")))
    return f'<p class="resources">{" · ".join(parts)} · {_as_of(reading)} · {link}</p>'


def render_resources(reading: res.Reading, *, nav: Sequence[tuple[str, str]] = ()) -> str:
    """The ``/resources`` page: loaded models, memory and local slots from the snapshot."""
    head = (
        "<h1>Resources</h1>"
        '<p class="lede">What the worker saw on its last poll: the models Ollama holds, '
        "the machine's memory and the local inference slots in use against the cap.</p>"
    )
    if reading.data is None:
        return render_page(
            "Resources", head + f'<p class="empty">{e(reading.error or "")}</p>', nav=nav
        )
    d = reading.data
    body = head + f"<p>{_as_of(reading)}</p>"
    body += f"<h2>Local inference slots</h2><p>{_slots_summary(d.get('local_slots'))}</p>"
    body += f"<h2>Memory</h2><p>{_memory_summary(d.get('memory'))}</p>"
    part = d.get("ollama")
    host = part.get("host") if isinstance(part, dict) else None
    body += "<h2>Loaded models</h2>"
    if host:
        body += f"<p><small>Ollama at {e(str(host))}</small></p>"
    models = part.get("models") if isinstance(part, dict) else None
    if isinstance(models, list) and models:
        rows = "".join(
            f"<tr><td>{e(str(m.get('name')))}</td><td>{e(fmt_bytes(m.get('size')))}</td>"
            f"<td>{e(fmt_bytes(m.get('size_vram')))}</td>"
            f"<td>{e(str(m.get('expires_at') or '-'))}</td></tr>"
            for m in models
            if isinstance(m, dict)
        )
        body += (
            '<div class="scroll"><table><thead><tr><th>Model</th><th>Size</th>'
            f"<th>In GPU memory</th><th>Unloads at</th></tr></thead><tbody>{rows}</tbody>"
            "</table></div>"
        )
    elif isinstance(models, list):
        body += '<p class="empty">No model loaded.</p>'
    else:
        body += f"<p>{_ollama_summary(part)}</p>"
    return render_page("Resources", body, nav=nav)


def resources_routes(
    resources_path: str | Path, *, nav: Sequence[tuple[str, str]] = ()
) -> list[BaseRoute]:
    """``GET /resources``: read-only, from the snapshot file the worker writes."""

    def page(request: Request) -> Response:
        return HTMLResponse(render_resources(res.load(resources_path), nav=nav))

    return [Route("/resources", page, methods=["GET"])]


# --- SVG charts -----------------------------------------------------------------------


def line_chart(
    points: Sequence[tuple[str, float | None]],
    *,
    label: str,
    fmt: Callable[[float], str],
    y_max: float | None = None,
) -> str:
    """An inline SVG line chart; None values leave a gap. Escapes every label."""
    if not any(v is not None for _, v in points):
        return f'<p class="empty">{e(label)}: no accepted tickets in this range.</p>'
    w, h, left, right, top, bottom = 640, 220, 64, 16, 16, 40
    pw, ph = w - left - right, h - top - bottom
    top_v = y_max if y_max is not None else max(v for _, v in points if v is not None)
    top_v = top_v or 1.0
    n = len(points)

    def x(i: int) -> float:
        return left + (pw / 2 if n == 1 else i * pw / (n - 1))

    def y(v: float) -> float:
        return top + ph - (v / top_v) * ph

    parts = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{e(label)}"'
        ' xmlns="http://www.w3.org/2000/svg">',
        f"<title>{e(label)}</title>",
    ]
    for frac in (0.0, 0.5, 1.0):
        gy = y(top_v * frac)
        parts.append(
            f'<line class="grid" x1="{left}" x2="{w - right}" y1="{gy:.1f}" y2="{gy:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{left - 6}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{e(fmt(top_v * frac))}</text>"
        )
    runs: list[list[str]] = [[]]
    for i, (_, v) in enumerate(points):
        if v is None:
            runs.append([])
        else:
            runs[-1].append(f"{x(i):.1f},{y(v):.1f}")
    for run in runs:
        if len(run) > 1:
            parts.append(f'<polyline class="line" points="{" ".join(run)}"/>')
    for i, (k, v) in enumerate(points):
        if v is not None:
            parts.append(
                f'<circle class="dot" cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.5">'
                f"<title>{e(k)}: {e(fmt(v))}</title></circle>"
            )
    shown = sorted({0, n - 1, (n - 1) // 2}) if n > 2 else list(range(n))
    for i in shown:
        anchor = "start" if i == 0 and n > 1 else "end" if i == n - 1 and n > 1 else "middle"
        parts.append(
            f'<text class="axis" x="{x(i):.1f}" y="{h - bottom + 18}" text-anchor="{anchor}">'
            f"{e(points[i][0])}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


# --- page -----------------------------------------------------------------------------

_CSS = """
:root { --bg: #fff; --fg: #1d1f23; --muted: #5d6470; --line: #e2e5ea; --accent: #2457c5;
  --accent-fg: #fff; --row: #f6f8fa; --card: #fff; --btn: #f1f3f6; --btn-hover: #e4e8ee;
  --danger: #c0392b; --radius: 8px; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0f1114; --fg: #e6e8eb; --muted: #9aa3af; --line: #2a2f37;
    --accent: #7aa2ff; --accent-fg: #0f1114; --row: #171a1f; --card: #15181c;
    --btn: #22262d; --btn-hover: #2c313a; --danger: #ff7b6b; } }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.4rem; margin: 0.4rem 0 0.2rem; }
h2 { font-size: 1.1rem; margin: 0 0 0.6rem; }
h3 { font-size: 0.95rem; margin: 1rem 0 0.4rem; }
p.lede, small, .empty { color: var(--muted); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
nav { display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; padding-bottom: 0.6rem;
  border-bottom: 1px solid var(--line); margin-bottom: 0.6rem; }
code { font: 0.88em ui-monospace, SFMono-Regular, Menlo, monospace; }
section.card { background: var(--card); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 1rem 1.1rem; margin: 1rem 0; overflow-x: auto; }
form { display: flex; flex-wrap: wrap; gap: 0.6rem 1rem; align-items: end;
  padding: 0.8rem; border: 1px solid var(--line); border-radius: var(--radius); }
form.inline, td form, li form { display: inline-flex; padding: 0; border: 0; gap: 0;
  margin: 0; vertical-align: middle; }
label { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.85rem;
  color: var(--muted); }
select, input { font: inherit; color: var(--fg); background: var(--bg);
  padding: 0.35rem 0.5rem; border: 1px solid var(--line); border-radius: 6px; }
select:focus, input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button { font: inherit; font-size: 0.85rem; color: var(--fg); background: var(--btn);
  border: 1px solid var(--line); border-radius: 6px; padding: 0.3rem 0.75rem;
  cursor: pointer; white-space: nowrap; }
button:hover { background: var(--btn-hover); }
form:not(.inline) > button:last-child { background: var(--accent); color: var(--accent-fg);
  border-color: var(--accent); }
td form, li form { margin-left: 0.3rem; }
.actions { display: flex; flex-wrap: nowrap; justify-content: flex-end;
  align-items: center; gap: 0.35rem; }
.actions form { margin: 0; }
.actions a { font-size: 0.85rem; padding: 0 0.3rem; }
form.danger button { color: var(--danger); }
form.danger button:hover { background: var(--danger); color: var(--accent-fg);
  border-color: var(--danger); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--line);
  vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover td, table tr:not(:first-child):hover td { background: var(--row); }
th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--muted); font-weight: 600; }
td.muted { color: var(--muted); font-size: 0.88rem; }
tr.all td { background: var(--row); font-weight: 600; }
.headline { font-size: 1.05rem; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1rem; }
svg.chart { width: 100%; height: auto; }
svg .grid { stroke: var(--line); stroke-width: 1; }
svg .axis { fill: var(--muted); font-size: 12px; }
svg .line { fill: none; stroke: var(--accent); stroke-width: 2; }
svg .dot { fill: var(--accent); }
.error { color: var(--danger); }
"""


def render_page(title: str, body: str, *, nav: Sequence[tuple[str, str]] = ()) -> str:
    """The shared layout. ``title`` and ``nav`` are escaped; ``body`` is trusted HTML."""
    links = [("/", "Metrics"), *nav]
    nav_html = "".join(f'<a href="{e(href)}">{e(text)}</a>' for href, text in links)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)}</title><style>{_CSS}</style></head>"
        f"<body><main><nav>{nav_html}</nav>{body}</main></body></html>"
    )


def _select(name: str, label: str, options: Sequence[str], current: str | None) -> str:
    opts = ['<option value="">all</option>']
    for o in options:
        sel = " selected" if o == current else ""
        opts.append(f'<option value="{e(o)}"{sel}>{e(o)}</option>')
    return f'<label>{e(label)}<select name="{e(name)}">{"".join(opts)}</select></label>'


def _form(filt: q.MetricsFilter, bucket: str, opts: q.FilterOptions) -> str:
    models = sorted(set(opts.writer_models) | set(opts.reviewer_models))
    if filt.model and filt.model not in models:
        models.append(filt.model)
    repos = list(opts.repos)
    if filt.repo and filt.repo not in repos:
        repos.append(filt.repo)
    since = filt.since.isoformat() if filt.since else ""
    until = filt.until.isoformat() if filt.until else ""
    buckets = "".join(
        f'<option value="{b}"{" selected" if b == bucket else ""}>{b}</option>' for b in q.BUCKETS
    )
    return (
        '<form method="get" action="/">'
        + _select("model", "Model", models, filt.model)
        + _select("repo", "Repo", repos, filt.repo)
        + f'<label>From<input type="date" name="since" value="{e(since)}"></label>'
        + f'<label>To<input type="date" name="until" value="{e(until)}"></label>'
        + f'<label>Trend by<select name="bucket">{buckets}</select></label>'
        + '<button type="submit">Apply</button> <a href="/">Clear</a></form>'
    )


def _writer_table(rep: q.Report) -> str:
    head = (
        "<tr><th>Writer model (start)</th><th>First-try accept</th><th>Kickbacks / ticket"
        "</th><th>Escalation rate</th><th>Cost / accepted ticket</th>"
        "<th>Attempt minutes / accepted ticket</th><th>Your reviews / accepted ticket</th>"
        "<th>Time to accept</th><th>Router hit rate</th><th>AI kickbacks / ticket</th>"
        "<th>Accepted on this model</th><th>Accepted, then reverted</th>"
        "<th>Accepted, then followed up</th></tr>"
    )

    def row(r: q.WriterRow, cls: str = "") -> str:
        name = "All models" if r.model is None else e(r.model)
        pm = r.post_merge
        return (
            f'<tr class="{cls}"><td>{name}</td><td>{fmt_rate(r.first_try_accept_rate)}</td>'
            f"<td>{fmt_kickbacks(r.kickbacks_per_ticket)}</td>"
            f"<td>{fmt_rate(r.escalation_rate)}</td>"
            f"<td>{fmt_cost(r.cost_per_accepted_ticket)}</td>"
            f"<td>{fmt_per_accepted(r.attempt_minutes_per_accepted_ticket, unit=' min')}</td>"
            f"<td>{fmt_per_accepted(r.user_reviews_per_accepted_ticket)}</td>"
            f"<td>{fmt_duration(r.time_to_accept)}</td>"
            f"<td>{fmt_rate(r.router_hit_rate)}</td>"
            f"<td>{fmt_mean(r.ai_kickbacks_per_ticket)}</td>"
            f"<td>{pm.accepted}</td>"
            f"<td>{fmt_after_merge(pm.reverted, pm.accepted)}</td>"
            f"<td>{fmt_after_merge(pm.followup, pm.accepted)}</td></tr>"
        )

    rows = [] if rep.filter.model else [row(rep.overall, "all")]
    rows += [row(w) for w in rep.writers]
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


def _reviewer_table(rep: q.Report) -> str:
    if not rep.reviewers:
        return '<p class="empty">No AI reviews in this range.</p>'
    rows = "".join(
        f"<tr><td>{e(r.reviewer_model or 'All reviewers')}</td>"
        f"<td>{fmt_rate(r.miss_rate)}</td></tr>"
        for r in rep.reviewers
    )
    return (
        '<div class="scroll"><table><tr><th>Reviewer model</th>'
        "<th>Miss rate (your kickbacks after its approval)</th></tr>"
        f"{rows}</table></div>"
    )


def _pipeline(rep: q.Report) -> str:
    p = rep.pipeline
    return (
        '<div class="scroll"><table><tr><th>Pipeline</th><th>Cost / accepted ticket</th></tr>'
        f"<tr><td>Writer (tier 1–2) + AI reviewer</td><td>{fmt_cost(p.writer_reviewer)}</td>"
        f"</tr><tr><td>Strong writer alone (tier 3 start, review cost left out)</td>"
        f"<td>{fmt_cost(p.strong_writer_alone)}</td></tr></table></div>"
    )


def _recent(rep: q.Report) -> str:
    if not rep.recent:
        return '<p class="empty">No closed tickets in this range.</p>'
    rows = "".join(
        f"<tr><td>{e(t.repo)}#{t.issue_number}</td><td>{e(t.title)}</td>"
        f"<td>{e(t.start_model)}</td>"
        f"<td>{e(t.accepted_model or '')}"
        f"{f' (tier {t.accepted_tier})' if t.accepted_tier else ''}</td>"
        f"<td>{t.attempts}</td><td>{e(format_cost(t.spend_usd))}</td>"
        f"<td>{e((t.done_at or '')[:10])}</td></tr>"
        for t in rep.recent
    )
    return (
        '<div class="scroll"><table><tr><th>Issue</th><th>Title</th><th>Started on</th>'
        "<th>Accepted on</th><th>Attempts</th><th>Cost</th><th>Merged</th></tr>"
        f"{rows}</table></div>"
    )


def render_report(
    rep: q.Report,
    opts: q.FilterOptions,
    *,
    nav: Sequence[tuple[str, str]] = (),
    resources: res.Reading | None = None,
) -> str:
    cost_points = [(p.bucket, p.cost_per_accepted) for p in rep.trend]
    local_points = [(p.bucket, p.local_share) for p in rep.trend]
    ls = rep.local_share
    headline = (
        f'<p class="headline">Solved locally: {fmt_rate(ls)} · Cost per accepted ticket: '
        f"{fmt_cost(rep.overall.cost_per_accepted_ticket)}</p>"
    )
    body = (
        "<h1>foreman-inator metrics</h1>"
        '<p class="lede">Which model should handle which kind of work, and what each '
        "accepted change costs. Dropped tickets are left out; failed exploration attempts "
        "never count against a model. The last three writer columns count tickets by the "
        "model that wrote the accepted change and what happened after you merged it.</p>"
        + _form(rep.filter, rep.bucket, opts)
        + headline
        + (render_resources_block(resources) if resources is not None else "")
        + "<h2>Writers</h2>"
        + _writer_table(rep)
        + "<h2>AI reviewers</h2>"
        + _reviewer_table(rep)
        + "<h2>Writer + reviewer vs. strong writer alone</h2>"
        + _pipeline(rep)
        + f"<h2>Trends (by {e(rep.bucket)} the ticket was created)</h2>"
        + '<div class="charts"><div><h3>Cost per accepted ticket</h3>'
        + line_chart(cost_points, label="Cost per accepted ticket", fmt=format_cost)
        + "</div><div><h3>Share of tickets solved locally</h3>"
        + line_chart(
            local_points,
            label="Share of tickets solved locally",
            fmt=lambda v: f"{v * 100:.0f}%",
            y_max=1.0,
        )
        + "</div></div><h2>Latest closed tickets</h2>"
        + _recent(rep)
    )
    return render_page("foreman-inator metrics", body, nav=nav)


# --- app ------------------------------------------------------------------------------


def open_store(store_path: str | Path) -> Store:
    """A read-only store for one request. Raises ``FileNotFoundError`` if there is none."""
    path = Path(store_path)
    if not path.exists():
        raise FileNotFoundError(path)
    return Store(path, read_only=True)


def metrics_routes(
    store_path: str | Path,
    *,
    nav: Sequence[tuple[str, str]] = (),
    resources_path: str | Path | None = None,
) -> list[BaseRoute]:
    """``GET /`` and ``GET /api/metrics``. With ``resources_path`` the page shows the
    short resource block (#60) read from that snapshot file."""

    def load(request: Request) -> tuple[q.Report, q.FilterOptions]:
        filt, bucket = parse_filter(request.query_params)
        store = open_store(store_path)
        try:
            return q.report(store, filt, bucket=bucket), q.filter_options(store)  # type: ignore[arg-type]
        finally:
            store.close()

    def page(request: Request) -> Response:
        try:
            rep, opts = load(request)
        except BadRequest as err:
            body = f'<h1>foreman-inator metrics</h1><p class="error">{e(str(err))}</p>'
            return HTMLResponse(render_page("Bad filter", body, nav=nav), 400)
        except (FileNotFoundError, sqlite3.OperationalError):
            body = "<h1>foreman-inator metrics</h1><p>No database yet: run the worker first.</p>"
            return HTMLResponse(render_page("No data", body, nav=nav), 503)
        reading = res.load(resources_path) if resources_path is not None else None
        return HTMLResponse(render_report(rep, opts, nav=nav, resources=reading))

    def api(request: Request) -> Response:
        try:
            rep, opts = load(request)
        except BadRequest as err:
            return JSONResponse({"error": str(err)}, 400)
        except (FileNotFoundError, sqlite3.OperationalError):
            return JSONResponse({"error": "no database yet"}, 503)
        return JSONResponse({"report": jsonable(rep), "options": jsonable(opts)})

    return [Route("/", page, methods=["GET"]), Route("/api/metrics", api, methods=["GET"])]


def create_app(
    store_path: str | Path,
    *,
    extra_routes: Sequence[BaseRoute] = (),
    nav: Sequence[tuple[str, str]] = (),
    resources_path: str | Path | None = None,
) -> ASGIApp:
    """The metrics app. ``extra_routes`` and ``nav`` are for later pages (#29);
    ``resources_path`` adds the resource block to the main page (#60)."""
    routes = metrics_routes(store_path, nav=nav, resources_path=resources_path)
    app = Starlette(routes=[*routes, *extra_routes])
    return LoopbackHostOnly(app)


# --- milestone 4 pages (#29) -----------------------------------------------------------

#: Nav links of the pages ``full_app`` adds (the trace view's first).
M4_NAV: tuple[tuple[str, str], ...] = (
    ("/traces", "Traces"),
    ("/failures", "Failure steps"),
    ("/experiments", "Experiments"),
    ("/resources", "Resources"),
)


def _steps_table(rows: Sequence[q.FailureSteps]) -> str:
    if not rows:
        return '<p class="empty">No kickbacks yet.</p>'
    labels = sorted({k for r in rows for k in r.steps})
    head = "".join(f"<th>{e(lb)}</th>" for lb in labels)
    body = "".join(
        f"<tr><td>{e(r.variant_id)}</td>"
        + "".join(f"<td>{r.steps.get(lb, 0)}</td>" for lb in labels)
        + f"<td>{r.total}</td><td>{fmt_rate(r.unlabeled_share)}</td></tr>"
        for r in rows
    )
    total = sum(r.total for r in rows)
    unlabeled = q.Rate(sum(r.unlabeled for r in rows), total)
    return (
        f'<p class="headline">Unlabeled: {fmt_rate(unlabeled)} of {total} kickback(s)</p>'
        '<div class="scroll"><table><tr><th>Writer variant</th>'
        f"{head}<th>Kickbacks</th><th>Unlabeled</th></tr>{body}</table></div>"
    )


def _misses_table(rows: Sequence[q.ReviewerMisses]) -> str:
    if not rows:
        return '<p class="empty">No judged AI approvals yet.</p>'
    body = "".join(f"<tr><td>{e(r.variant_id)}</td><td>{fmt_rate(r.rate)}</td></tr>" for r in rows)
    return (
        '<div class="scroll"><table><tr><th>Reviewer variant</th>'
        "<th>reviewer_miss (of its judged approvals)</th></tr>"
        f"{body}</table></div>"
    )


def render_failures(
    writers: Sequence[q.FailureSteps],
    reviewers: Sequence[q.ReviewerMisses],
    *,
    nav: Sequence[tuple[str, str]] = (),
) -> str:
    body = (
        "<h1>Failure steps per variant</h1>"
        '<p class="lede">Where kicked-back attempts went wrong, as labeled from their '
        "recordings (SPEC Tracing). Failed exploration attempts and dropped tickets are left "
        "out; reviewer_miss is charged to the reviewer that approved the change.</p>"
        "<h2>Writer variants</h2>"
        + _steps_table(writers)
        + "<h2>Reviewer variants</h2>"
        + _misses_table(reviewers)
    )
    return render_page("Failure steps", body, nav=nav)


def _experiment_rows(store: Store, min_cases: int, min_sample: int) -> list[dict[str, str]]:
    from foreman_inator.experiments import replay_runner as rr
    from foreman_inator.experiments import shadow

    out = []
    for x in reversed(store.list_experiments()):
        result = ""
        if x.mode == "replay":
            try:
                c = rr.compare(store, x.id, min_cases=min_cases)
                result = (
                    f"replay {c.state}{f' ({c.stop_reason})' if c.stop_reason else ''}: "
                    f"{c.verdict} ({c.reason}); {c.scored_cases}/{c.total_cases} cases, "
                    f"{c.wins} won, {c.losses} lost, {c.ties} tied"
                )
            except LookupError:
                result = "no replay run"
        elif x.mode == "shadow" and x.role == "reviewer":
            cmp_ = shadow.score_experiment(store, x.id)
            j = shadow.judge_live(cmp_, min_sample=min_sample)
            result = (
                f"live {j.status}: {cmp_.paired} paired, {j.decided_pairs} decided by you "
                f"(of {min_sample})" + (f"; {'; '.join(j.reasons)}" if j.reasons else "")
            )
        out.append(
            {
                "id": str(x.id),
                "role": x.role,
                "mode": x.mode,
                "status": x.status,
                "champion": x.champion_variant,
                "challenger": x.challenger_variant,
                "dates": f"{x.started_at:%Y-%m-%d}"
                + (f" → {x.ended_at:%Y-%m-%d}" if x.ended_at else ""),
                "result": result,
            }
        )
    return out


def render_experiments(
    champions: Sequence[str], rows: Sequence[dict[str, str]], *, nav: Sequence[tuple[str, str]]
) -> str:
    cols = ("id", "role", "mode", "status", "champion", "challenger", "dates", "result")
    names = ("#", "Role", "Mode", "Status", "Champion", "Challenger", "Dates", "Result")

    def table(items: Sequence[dict[str, str]]) -> str:
        if not items:
            return '<p class="empty">None.</p>'
        head = "".join(f"<th>{n}</th>" for n in names)
        body = "".join(
            "<tr>" + "".join(f"<td>{e(r[c])}</td>" for c in cols) + "</tr>" for r in items
        )
        return f'<div class="scroll"><table><tr>{head}</tr>{body}</table></div>'

    champs = "".join(f"<li>{e(line)}</li>" for line in champions)
    body = (
        "<h1>Experiments</h1>"
        '<p class="lede">A challenger becomes champion only after it wins on replay and '
        "holds up live, and only when you run <code>fman experiment promote</code>.</p>"
        '<p><a href="/experiments/manage">Start, stop, propose or promote experiments</a></p>'
        f"<h2>Champions</h2><ul>{champs}</ul>"
        "<h2>Running</h2>"
        + table([r for r in rows if r["status"] == "running"])
        + "<h2>Ended</h2>"
        + table([r for r in rows if r["status"] != "running"])
    )
    return render_page("Experiments", body, nav=nav)


def m4_routes(
    store_path: str | Path,
    *,
    min_cases: int = 10,
    min_sample: int = 30,
    nav: Sequence[tuple[str, str]] = M4_NAV,
) -> list[BaseRoute]:
    """``GET /failures`` and ``GET /experiments`` (read-only, from a read-only store)."""

    def no_db() -> Response:
        body = "<h1>foreman-inator</h1><p>No database yet: run the worker first.</p>"
        return HTMLResponse(render_page("No data", body, nav=nav), 503)

    def failures(request: Request) -> Response:
        try:
            store = open_store(store_path)
        except (FileNotFoundError, sqlite3.OperationalError):
            return no_db()
        try:
            html_ = render_failures(
                q.failure_steps_by_variant(store), q.reviewer_misses_by_variant(store), nav=nav
            )
        finally:
            store.close()
        return HTMLResponse(html_)

    def experiments(request: Request) -> Response:
        from foreman_inator.experiments import variants

        try:
            store = open_store(store_path)
        except (FileNotFoundError, sqlite3.OperationalError):
            return no_db()
        try:
            champions = variants.status_lines(store)
            rows = _experiment_rows(store, min_cases, min_sample)
        finally:
            store.close()
        return HTMLResponse(render_experiments(champions, rows, nav=nav))

    return [
        Route("/failures", failures, methods=["GET"]),
        Route("/experiments", experiments, methods=["GET"]),
    ]


def full_app(
    store_path: str | Path,
    *,
    recordings_dir: str | Path,
    traces_dir: str | Path | None,
    config: Any = None,
    resources_path: str | Path | None = None,
) -> ASGIApp:
    """The page ``fman metrics`` serves: the metrics page, the trace view (#28), the
    failure-step and experiment pages and ``/resources`` (#60, from ``resources_path``,
    default ``<home>/run/resources.json``), ``/settings`` (#73), ``/tiers`` (#74) and
    ``/activity`` (#76) with its Retry button (#79), all behind the loopback guard and all
    sharing one nav."""
    from foreman_inator.config import Config
    from foreman_inator.metrics import (
        activity_ui,
        experiments_ui,
        settings_ui,
        tiers_ui,
        trace_view,
    )
    from foreman_inator.routing import escalation, explore

    cfg = config if config is not None else Config()
    nav = (  # #67 experiments, wave 18: settings #73, tiers #74, activity #76
        *M4_NAV,
        experiments_ui.NAV_ENTRY,
        activity_ui.NAV_ENTRY,
        settings_ui.NAV_ENTRY,
        tiers_ui.NAV_ENTRY,
    )
    res_path = resources_path if resources_path is not None else res.default_path()
    ladder = escalation.build_ladder(cfg.models)
    plain = trace_view.store_rows_for(
        store_path, ladder=ladder, recordings_dir=recordings_dir, traces_dir=traces_dir
    )

    def rows_for(ticket_id: int) -> Any:
        """The trace view's rows with the ticket's stored explore decision (#23)."""
        rows = plain(ticket_id)
        if rows is None:
            return None
        try:
            store = open_store(store_path)
        except (FileNotFoundError, sqlite3.Error):
            return rows
        try:
            t = rows.ticket
            flag = explore.decode(store.get_cursor(t.repo, explore.explore_cursor(t.id)))
        finally:
            store.close()
        return replace(rows, explore=flag) if flag != rows.explore else rows

    routes = [
        *trace_view.trace_routes(
            recordings_dir, traces_dir, store_path=store_path, ladder=ladder,
            rows_for=rows_for, nav=nav,
        ),
        *m4_routes(
            store_path,
            min_cases=cfg.experiments.min_cases,
            min_sample=cfg.experiments.live_min_sample,
            nav=nav,
        ),
        *resources_routes(res_path, nav=nav),
        # #67/#69: start, stop, cancel, promote and propose (POSTs behind forms.FormGuard).
        # config.toml per request (a model removed after start is not offered); the
        # startup config when the file is missing or cannot be read.
        *experiments_ui.routes(
            store_path, load_config=_per_request_config(cfg) if config is not None else None,
            nav=nav,
        ),
        *settings_ui.routes(store_path, nav=nav),  # #73: edit config.toml (FormGuard)
        *tiers_ui.routes(store_path, nav=nav),  # #74: models per tier (FormGuard)
        # #76: what runs now; #79: its Retry button (FormGuard). config.toml is read per
        # request (like /settings and /tiers), so Retry decides with the current models/caps.
        *activity_ui.routes(
            store_path,
            load_config=None,
            nav=nav,
            resources_path=res_path,
            traces_dir=traces_dir,
            api_factory=retry_github,
        ),
    ]  # fmt: skip
    return create_app(store_path, extra_routes=routes, nav=nav, resources_path=res_path)


def _per_request_config(startup: Any) -> Any:
    """A loader that reads config.toml on each call (like /settings, /tiers and
    /activity), falling back to ``startup`` when the file is missing or invalid."""
    from foreman_inator import config as config_mod
    from foreman_inator import paths

    def load() -> Any:
        path = paths.config_path()
        if not path.exists():
            return startup
        try:
            return config_mod.load(path)
        except config_mod.ConfigError as exc:
            log.warning("experiments page: config.toml unreadable (%s); using startup config", exc)
            return startup

    return load


def retry_github(cfg: Any) -> Any:
    """The GitHub client ``fman retry`` builds (``/activity``'s Retry, #79): None without
    App credentials, or when GitHub cannot be reached (only a dropped ticket needs it)."""
    from foreman_inator import paths

    key_path = paths.app_key_path()
    if cfg.app_id is None or not cfg.installation_ids or not key_path.exists():
        return None
    from foreman_inator.worker_run import build_github

    try:
        return build_github(cfg, key_path)
    except Exception as exc:  # noqa: BLE001 - flagged tickets retry without it
        log.warning("retry: GitHub not reachable: %s", type(exc).__name__)
        return None


def metrics_url(host: str, port: int, **params: str) -> str:
    query = urlencode({k: v for k, v in params.items() if v})
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}/" + (f"?{query}" if query else "")


# --- serving --------------------------------------------------------------------------


class MetricsServer:
    """Serve the page on loopback from a background thread (the worker) or in the
    foreground (``run``, for ``fman metrics``). ``port=0`` picks a free port."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        app: ASGIApp | None = None,
    ) -> None:
        if not is_loopback(host):
            raise ValueError("the metrics page binds to loopback only (127.0.0.1 or ::1)")
        self.host, self.port = host, port
        self.app = app or create_app(store_path)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    @property
    def url(self) -> str:
        return metrics_url(self.host, self.port)

    def _bind(self) -> tuple[uvicorn.Server, socket.socket]:
        family = socket.AF_INET6 if ":" in self.host.strip("[]") else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host.strip("[]"), self.port))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        cfg = uvicorn.Config(
            self.app,
            log_level="warning",
            lifespan="off",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
        return uvicorn.Server(cfg), sock

    def run(self) -> None:
        """Serve in the foreground until interrupted (Ctrl-C)."""
        server, sock = self._bind()
        try:
            server.run(sockets=[sock])
        finally:
            sock.close()

    def start(self, timeout: float = 10.0) -> MetricsServer:
        if self._thread is not None:
            return self
        self._server, self._sock = self._bind()
        server, sock = self._server, self._sock
        self._thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(sockets=[sock])),
            name="fman-metrics",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("metrics server failed to start")
            time.sleep(0.01)
        return self

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
        if self._sock is not None:
            self._sock.close()
        self._server = self._thread = self._sock = None

    def __enter__(self) -> MetricsServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def serve(
    store_path: str | Path,
    *,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    config: Any = None,
    recordings_dir: str | Path | None = None,
    traces_dir: str | Path | None = None,
) -> None:
    """Serve the metrics page in the foreground (``fman metrics``). Loopback only. With
    ``recordings_dir``, the trace view and the failure-step and experiment pages too."""
    app = None
    if recordings_dir is not None:
        app = full_app(
            store_path, recordings_dir=recordings_dir, traces_dir=traces_dir, config=config
        )
    MetricsServer(store_path, host=host, port=port, app=app).run()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "BadRequest",
    "LoopbackHostOnly",
    "MetricsServer",
    "create_app",
    "fmt_bytes",
    "full_app",
    "is_loopback",
    "line_chart",
    "metrics_routes",
    "m4_routes",
    "metrics_url",
    "open_store",
    "parse_filter",
    "render_page",
    "render_report",
    "render_resources",
    "render_resources_block",
    "resources_routes",
    "serve",
]
