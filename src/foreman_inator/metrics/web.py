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

Room for #29: pass ``extra_routes`` (more Starlette routes, e.g. the trace view) and
``nav`` links to ``create_app``; ``render_page`` gives new pages the same layout.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, is_dataclass
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


def fmt_kickbacks(k: q.KickbackStats) -> str:
    if k.value is None:
        return NO_DATA
    out = f"{k.value:.2f} <small>({k.total} over {k.tickets})</small>"
    if k.by_tag:
        tags = " · ".join(f"{e(tag)} {(k.per_tag[tag] or 0):.2f}" for tag in sorted(k.by_tag))
        out += f"<br><small>{tags}</small>"
    return out


def fmt_duration(d: q.Duration) -> str:
    if d.median_seconds is None:
        return NO_DATA
    return f"{e(format_duration(d.median_seconds))} <small>(median of {d.count})</small>"


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
:root { --bg: #fff; --fg: #1d1f23; --muted: #5d6470; --line: #d8dce3; --accent: #2457c5;
  --row: #f5f7fa; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #15171b; --fg: #e6e8eb; --muted: #9aa3af; --line: #2e333b;
    --accent: #7aa2ff; --row: #1c1f24; } }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.4rem; margin: 0.4rem 0 0.2rem; }
h2 { font-size: 1.1rem; margin: 1.6rem 0 0.5rem; }
p.lede, small, .empty { color: var(--muted); }
nav a { margin-right: 1rem; color: var(--accent); }
form { display: flex; flex-wrap: wrap; gap: 0.6rem 1rem; align-items: end;
  padding: 0.8rem; border: 1px solid var(--line); border-radius: 8px; }
label { display: flex; flex-direction: column; font-size: 0.85rem; color: var(--muted); }
select, input, button { font: inherit; padding: 0.25rem 0.4rem; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { font-size: 0.85rem; color: var(--muted); font-weight: 600; }
tr.all td { background: var(--row); font-weight: 600; }
.headline { font-size: 1.05rem; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1rem; }
svg.chart { width: 100%; height: auto; }
svg .grid { stroke: var(--line); stroke-width: 1; }
svg .axis { fill: var(--muted); font-size: 12px; }
svg .line { fill: none; stroke: var(--accent); stroke-width: 2; }
svg .dot { fill: var(--accent); }
.error { color: #c0392b; }
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
        "</th><th>Escalation rate</th><th>Cost / accepted ticket</th><th>Time to accept"
        "</th><th>Router hit rate</th><th>AI kickbacks / ticket</th></tr>"
    )

    def row(r: q.WriterRow, cls: str = "") -> str:
        name = "All models" if r.model is None else e(r.model)
        return (
            f'<tr class="{cls}"><td>{name}</td><td>{fmt_rate(r.first_try_accept_rate)}</td>'
            f"<td>{fmt_kickbacks(r.kickbacks_per_ticket)}</td>"
            f"<td>{fmt_rate(r.escalation_rate)}</td>"
            f"<td>{fmt_cost(r.cost_per_accepted_ticket)}</td>"
            f"<td>{fmt_duration(r.time_to_accept)}</td>"
            f"<td>{fmt_rate(r.router_hit_rate)}</td>"
            f"<td>{fmt_mean(r.ai_kickbacks_per_ticket)}</td></tr>"
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
    rep: q.Report, opts: q.FilterOptions, *, nav: Sequence[tuple[str, str]] = ()
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
        "never count against a model.</p>"
        + _form(rep.filter, rep.bucket, opts)
        + headline
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
    store_path: str | Path, *, nav: Sequence[tuple[str, str]] = ()
) -> list[BaseRoute]:
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
        return HTMLResponse(render_report(rep, opts, nav=nav))

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
) -> ASGIApp:
    """The metrics app. ``extra_routes`` and ``nav`` are for later pages (#29)."""
    app = Starlette(routes=[*metrics_routes(store_path, nav=nav), *extra_routes])
    return LoopbackHostOnly(app)


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


def serve(store_path: str | Path, *, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> None:
    """Serve the metrics page in the foreground (``fman metrics``). Loopback only."""
    MetricsServer(store_path, host=host, port=port).run()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "BadRequest",
    "LoopbackHostOnly",
    "MetricsServer",
    "create_app",
    "is_loopback",
    "line_chart",
    "metrics_routes",
    "metrics_url",
    "open_store",
    "parse_filter",
    "render_page",
    "render_report",
    "serve",
]
