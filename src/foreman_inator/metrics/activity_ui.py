"""The Activity page: what the agents are working on right now (todo #76, GH #65), with
a Retry button on each flagged ticket (todo #79, GH #73).

``routes(store_path, ...)`` adds to the metrics app:

* ``GET /activity``: read-only. The same data as ``worker_status.render_status`` (``fman
  status``): tickets by state with their latest attempt, flags, file-lock waits, spend
  approvals, needs-info, GitHub CI and open-PR-cap waits, and the summary lines; plus,
  for each running attempt, the repo and issue link, model and tier, current step
  (``step_of``: writing, verify, publish, CI, review), elapsed time against
  ``writer_timeout_seconds``, tokens and cost so far (the attempt row's running totals,
  which the proxy adds to per call) and a link to its trace (``/traces/<ticket>/<attempt>``);
  and the loaded local models and memory from the resource snapshot
  (``resources.snapshot``). A ``<meta http-equiv="refresh">`` reloads it every
  ``refresh_seconds``; there is no JavaScript. Everything from the store or the snapshot
  is escaped (issue titles and flag reasons come from GitHub or model output).
* ``POST /activity/retry`` (fields ``ticket``, ``tier`` = ``same`` | ``up``): through
  ``forms.FormGuard`` (CSRF token, own loopback Origin/Host; 403 otherwise, nothing
  changes), it calls ``retry.retry(store, config, ticket_id, tier=..., now=..., api=...)``
  exactly as ``fman retry <ticket> [--up]`` does. A refusal is shown with its reason (409)
  and changes nothing; a second click while the first retry is still queued is retry's
  own ``already_queued`` no-op, so a double submit queues once. The answer page refreshes
  to ``GET /activity``, so a reload never posts again.

``api_factory(config)`` (default: none) gives retry a GitHub client, which only a dropped
ticket needs; the page offers Retry on flagged open tickets, so without one a dropped
ticket's retry is refused with retry's own reason.
"""

from __future__ import annotations

import html
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import BaseRoute, Route

from foreman_inator.config import Config, ConfigError
from foreman_inator.db.store import COLUMNS, OPEN_COLUMNS, Attempt, Store, Ticket
from foreman_inator.locks import Locks
from foreman_inator.metrics import forms, web
from foreman_inator.pr.publisher import format_cost
from foreman_inator.pr.summary import format_duration
from foreman_inator.resources import snapshot as res
from foreman_inator.spend import FLAG_SPEND_CAP

ACTIVITY_PATH = "/activity"
RETRY_PATH = "/activity/retry"
#: The page's nav entry (the wiring step adds it to ``web.full_app``'s nav).
NAV_ENTRY = (ACTIVITY_PATH, "Activity")
#: Seconds between reloads (``<meta http-equiv="refresh">``).
REFRESH_SECONDS = 5
#: ``Config.writer_timeout_seconds``'s default, used when no config could be read.
WRITER_TIMEOUT_SECONDS = 1200.0
#: Closed tickets shown per column, as ``fman status`` (``closed_limit``).
CLOSED_LIMIT = 5
TIER_CHOICES = {"same": "same tier", "up": "move up a tier"}

e = html.escape


def _ws() -> Any:
    from foreman_inator import worker_status

    return worker_status


def _retry_mod() -> Any:
    """``foreman_inator.retry``, looked up at call time (tests patch ``retry``)."""
    from foreman_inator import retry

    return retry


# =========================================================================== data


@dataclass(frozen=True)
class Running:
    """One attempt in flight, or its ticket's step right after it (publish, CI, review)."""

    ticket: Ticket
    attempt: Attempt
    number: int  # the attempt's number on its ticket (1-based, as ``fman status``)
    step: str
    elapsed_seconds: float
    files_held: int


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def _verify_log(traces_dir: Path | None, a: Attempt) -> Path | None:
    """Where the worker writes the attempt's verify output (``worker_attempt``); the file
    appears when verify starts."""
    if traces_dir is None:
        return None
    return traces_dir / str(a.ticket_id) / f"attempt-{a.id}-verify.log"


def step_of(
    store: Store, t: Ticket, attempts: Sequence[Attempt], traces_dir: Path | None = None
) -> str | None:
    """The ticket's current step, or None when nothing of the worker's is running:
    ``writing`` / ``verify`` (the latest attempt has not ended), ``publish`` (it passed
    its checks, no PR step yet), ``CI`` (GitHub CI on the PR head), ``review`` (the AI
    review). A flagged or closed ticket, or one waiting on you, has no step."""
    if t.column not in OPEN_COLUMNS or t.flag_reason or not attempts:
        return None
    a = attempts[-1]
    if a.ended_at is None:
        log = _verify_log(traces_dir, a)
        return "verify" if log is not None and log.exists() else "writing"
    if a.outcome is not None or not a.tests_passed:
        return None
    if t.column == "in_progress":
        return "publish"
    if t.column == "testing" and not store.list_reviews(attempt_id=a.id, shadow=False):
        return "CI" if a.ci_status is None else "review"
    return None


def running(store: Store, *, now: datetime, traces_dir: Path | None = None) -> list[Running]:
    """Every open ticket with a step, oldest attempt first."""
    locks = Locks(store)
    out = []
    for t in store.list_tickets(column=OPEN_COLUMNS):
        attempts = store.list_attempts(t.id)
        step = step_of(store, t, attempts, traces_dir)
        if step is None:
            continue
        a = attempts[-1]
        since = a.started_at if a.ended_at is None else a.ended_at
        elapsed = max(0.0, (now - _aware(since)).total_seconds())
        out.append(Running(t, a, len(attempts), step, elapsed, len(locks.held(t))))
    return sorted(out, key=lambda r: r.attempt.started_at)


# =========================================================================== render


def issue_link(t: Ticket) -> str:
    url = f"https://github.com/{quote(t.repo, safe='/')}/issues/{int(t.issue_number)}"
    return f'<a href="{e(url)}">{e(t.repo)}#{int(t.issue_number)}</a>'


def _title(t: Ticket) -> str:
    return e(" ".join(t.title.split()))


def _elapsed(r: Running, timeout: float) -> str:
    text = format_duration(r.elapsed_seconds)
    if r.step not in ("writing", "verify"):
        return f"{e(text)} <small>since the attempt ended</small>"
    out = f"{e(text)} of {e(format_duration(timeout))}"
    if r.elapsed_seconds > timeout:
        return f'<span class="error">{out} (past the writer timeout)</span>'
    return out


def _running_table(rows: Sequence[Running], timeout: float) -> str:
    head = "<h2>Running now</h2>"
    if not rows:
        return head + '<p class="empty">No attempt is running.</p>'
    body = "".join(
        f"<tr><td>#{r.ticket.id}</td><td>{issue_link(r.ticket)} {_title(r.ticket)}</td>"
        f"<td>{e(r.attempt.model)}</td><td>{int(r.attempt.tier)}</td>"
        f"<td>{e(r.step)} <small>(attempt {r.number}, {e(r.attempt.trigger)})</small></td>"
        f"<td>{_elapsed(r, timeout)}</td>"
        f"<td>{int(r.attempt.tokens_in or 0) + int(r.attempt.tokens_out or 0):,}</td>"
        f"<td>{e(format_cost(float(r.attempt.cost_usd or 0.0)))}</td>"
        f"<td>{r.files_held}</td>"
        f'<td><a href="/traces/{r.ticket.id}/{r.attempt.id}">trace</a></td></tr>'
        for r in rows
    )
    return head + (
        '<div class="scroll"><table class="running"><thead><tr><th>Ticket</th><th>Issue</th>'
        "<th>Model</th><th>Tier</th><th>Step</th><th>Elapsed</th><th>Tokens</th>"
        f"<th>Cost</th><th>Files locked</th><th></th></tr></thead><tbody>{body}</tbody>"
        "</table></div>"
    )


def retry_forms(guard: forms.FormGuard, t: Ticket) -> str:
    """The two Retry buttons for a flagged ticket (same tier, move up a tier)."""
    return "".join(
        guard.form(
            RETRY_PATH,
            f'<input type="hidden" name="ticket" value="{int(t.id)}">'
            f'<input type="hidden" name="tier" value="{e(choice)}">',
            button=f"Retry ({label})",
            cls="retry",
        )
        for choice, label in TIER_CHOICES.items()
    )


def _flagged_table(guard: forms.FormGuard, tickets: Sequence[Ticket]) -> str:
    head = "<h2>Flagged for you</h2>"
    if not tickets:
        return head + '<p class="empty">Nothing is flagged.</p>'
    body = "".join(
        f"<tr><td>#{t.id}</td><td>{issue_link(t)} {_title(t)}</td>"
        f"<td>{e(t.flag_reason or '')}</td><td>{retry_forms(guard, t)}</td></tr>"
        for t in tickets
    )
    return head + (
        '<div class="scroll"><table class="flagged"><thead><tr><th>Ticket</th><th>Issue</th>'
        f"<th>Flag</th><th>Retry</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def _ticket_notes(
    store: Store,
    t: Ticket,
    attempts: Sequence[Attempt],
    waits: dict[int, tuple[int, ...]],
    cap: int | None,
) -> list[str]:
    """``fman status``'s FLAGGED / WAITING lines for one ticket (``worker_status._line``)."""
    ws = _ws()
    if t.column in ("done", "dropped"):
        return []
    notes = []
    if t.flag_reason and t.flag_reason.startswith(FLAG_SPEND_CAP):
        notes.append(f"WAITING: spend approval ({t.flag_reason}); run `fman approve-spend {t.id}`")
    elif t.flag_reason:
        notes.append(f"FLAGGED: {t.flag_reason}")
    blockers = waits.get(t.id)
    if blockers:
        held = ", ".join(f"#{b}" for b in blockers)
        notes.append(
            f"WAITING: on files held by ticket(s) {held}{ws._release_hint(blockers, store)}"
        )
    notes.extend(f"WAITING: {why}" for why in ws._waits(t, attempts, store, cap))
    return notes


def _columns(store: Store, config: Config | None, closed_limit: int) -> tuple[str, str]:
    """The tickets-by-state section and the summary line, as ``render_status`` counts."""
    ws = _ws()
    tickets = store.list_tickets()
    locks = Locks(store)
    waits = {
        w.ticket_id: w.blockers
        for repo in sorted({t.repo for t in tickets})
        for w in locks.waiting(repo)
    }
    budget = config.attempt_budget_usd if config is not None else None
    cap = int(config.max_open_prs_per_repo) if config is not None else None
    out = ["<h2>Tickets by state</h2>"]
    flagged = waiting = 0
    for col in COLUMNS:
        items = [t for t in tickets if t.column == col]
        shown = items
        if col in ("done", "dropped") and closed_limit >= 0:
            shown = items[-closed_limit:] if closed_limit else []
        header = f"{ws._TITLES[col]} ({len(items)})"
        if len(shown) < len(items):
            header += f", latest {len(shown)}"
        shown_ids = {x.id for x in shown}
        out.append(f'<h3 class="column" id="col-{e(col)}">{e(header)}</h3>')
        rows = []
        for t in items:
            attempts = store.list_attempts(t.id)
            notes = _ticket_notes(store, t, attempts, waits, cap)
            if t.column not in ("done", "dropped"):
                spend = (t.flag_reason or "").startswith(FLAG_SPEND_CAP)
                flagged += bool(t.flag_reason) and not spend
                waiting += bool(t.id in waits or spend or ws._waits(t, attempts, store, cap))
            if t.id not in shown_ids:
                continue
            pr = f"PR #{t.pr_number}" if t.pr_number is not None else ""
            extra = "".join(f'<br><small class="note">{e(n)}</small>' for n in notes)
            rows.append(
                f'<tr data-ticket="{t.id}"><td>#{t.id}</td><td>{issue_link(t)}</td>'
                f"<td>{_title(t)}{extra}</td>"
                f"<td>{e(ws._attempt_text(attempts, budget))}</td><td>{e(pr)}</td></tr>"
            )
        if rows:
            out.append(
                '<div class="scroll"><table><thead><tr><th>Ticket</th><th>Issue</th>'
                "<th>Title</th><th>Attempt</th><th>PR</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div>"
            )
        else:
            out.append('<p class="empty">(none)</p>')
    summary = f"{len(tickets)} ticket(s), {flagged} flagged for you, {waiting} waiting."
    return "".join(out), summary


def _summary_lines(store: Store, config: Config | None, summary: str) -> str:
    """The lines ``render_status`` ends with (no Ollama probe: the snapshot covers it)."""
    ws = _ws()
    repos = sorted({t.repo for t in store.list_tickets()})
    lines = [summary, *ws._champion_lines(store), *ws.profile_lines(store, repos)]
    lines.extend(ws.local_only_lines(store))
    if config is not None:
        from foreman_inator.runner import sandbox
        from foreman_inator.spend import Spend

        lines.append(ws.repos_line(config))
        lines.append(Spend(store, config).summary().status_line())
        line = sandbox.describe(
            config.sandbox, dict(config.sandbox_network), dict(config.sandbox_install)
        )  # type: ignore[arg-type]
        lines.append(f"!!! {line} !!!" if config.sandbox == "off" else line)
    return '<ul class="summary">' + "".join(f"<li>{e(x)}</li>" for x in lines) + "</ul>"


def _resources(reading: res.Reading | None) -> str:
    """Loaded local models and memory from the worker's resource snapshot."""
    if reading is None:
        return ""
    out = "<h2>Local models and memory</h2>" + web.render_resources_block(reading)
    part = reading.data.get("ollama") if reading.data else None
    models = part.get("models") if isinstance(part, dict) else None
    if isinstance(models, list) and models:
        out += "<ul>" + "".join(
            f"<li>{e(str(m.get('name')))} ({e(web.fmt_bytes(m.get('size')))})</li>"
            for m in models
            if isinstance(m, dict)
        ) + "</ul>"  # fmt: skip
    return out


def render_activity(
    store: Store,
    *,
    guard: forms.FormGuard,
    now: datetime,
    config: Config | None = None,
    writer_timeout_seconds: float | None = None,
    reading: res.Reading | None = None,
    traces_dir: Path | None = None,
    nav: Sequence[tuple[str, str]] = (),
    message: str | None = None,
    error: bool = False,
    closed_limit: int = CLOSED_LIMIT,
    refresh_seconds: int = REFRESH_SECONDS,
) -> str:
    """The whole page. ``message`` is a retry's answer (escaped), shown first."""
    timeout = writer_timeout_seconds
    if timeout is None:
        timeout = config.writer_timeout_seconds if config is not None else WRITER_TIMEOUT_SECONDS
    rows = running(store, now=now, traces_dir=traces_dir)
    flagged = [t for t in store.list_tickets(column=OPEN_COLUMNS) if t.flag_reason]
    columns, summary = _columns(store, config, closed_limit)
    body = "<h1>Activity</h1>"
    if message:
        body += f'<p class="{"error" if error else "message"}">{e(message)}</p>'
    body += (
        f'<p class="lede">What the worker is doing now, as of {e(f"{now:%H:%M:%S}")} UTC; '
        f"this page reloads every {int(refresh_seconds)} seconds.</p>"
        + _running_table(rows, float(timeout))
        + _flagged_table(guard, flagged)
        + columns
        + _summary_lines(store, config, summary)
        + _resources(reading)
    )
    page = web.render_page("Activity", body, nav=nav)
    # Reload as a GET of the page itself, so the page shown after a POST never re-posts.
    meta = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}; url={ACTIVITY_PATH}">'
    return page.replace('<meta charset="utf-8">', f'<meta charset="utf-8">{meta}', 1)


# =========================================================================== routes


def routes(
    store_path: str | Path,
    *,
    load_config: Callable[[], Config] | None = None,
    guard: forms.FormGuard | None = None,
    nav: Sequence[tuple[str, str]] | None = None,
    resources_path: str | Path | None = None,
    traces_dir: str | Path | None = None,
    api_factory: Callable[[Config], Any] | None = None,
    now: Callable[[], datetime] | None = None,
    refresh_seconds: int = REFRESH_SECONDS,
) -> list[BaseRoute]:
    """``GET /activity`` and ``POST /activity/retry``. ``load_config`` defaults to
    ``config.load`` (read on each request, like the CLI); ``guard`` to
    ``forms.default_guard()``; ``resources_path`` to the snapshot's default path."""
    from foreman_inator import config as config_mod

    path = Path(store_path)
    guard = guard or forms.default_guard()
    nav = tuple(nav) if nav is not None else (*web.M4_NAV, NAV_ENTRY)
    cfg_of = load_config or config_mod.load
    res_path = Path(resources_path) if resources_path is not None else res.default_path()
    tdir = Path(traces_dir) if traces_dir is not None else None
    clock = now or (lambda: datetime.now(UTC))

    def config() -> tuple[Config | None, str | None]:
        try:
            return cfg_of(), None
        except (ConfigError, OSError) as exc:
            return None, f"config.toml: {exc}"

    def plain(title: str, text: str, status: int) -> HTMLResponse:
        body = f'<h1>{e(title)}</h1><p class="error">{e(text)}</p>'
        body += f'<p><a href="{ACTIVITY_PATH}">Back to activity</a></p>'
        return HTMLResponse(web.render_page(title, body, nav=nav), status)

    def show(message: str | None = None, error: bool = False, status: int = 200) -> Response:
        cfg, cfg_err = config()
        try:
            store = web.open_store(path)
        except (FileNotFoundError, sqlite3.OperationalError):
            return plain("Activity", "No database yet: run the worker first.", 503)
        try:
            text = render_activity(
                store, guard=guard, now=clock(), config=cfg, reading=res.load(res_path),
                traces_dir=tdir, nav=nav, message=message or cfg_err,
                error=error or (message is None and cfg_err is not None),
                refresh_seconds=refresh_seconds,
            )  # fmt: skip
        finally:
            store.close()
        return HTMLResponse(text, status)

    def page(request: Request) -> Response:
        return show()

    def do_retry(request: Request, form: dict[str, str]) -> Response:
        ref = form.get("ticket", "").strip()
        choice = form.get("tier", "same").strip()
        if not ref.isdigit():
            return plain("Retry", "ticket must be a ticket id", 400)
        if choice not in TIER_CHOICES:
            return plain("Retry", "tier must be 'same' or 'up'", 400)
        cfg, cfg_err = config()
        if cfg is None:
            return plain("Retry", cfg_err or "config.toml could not be read", 500)
        if not path.exists():
            return plain("Retry", "No database yet: run the worker first.", 503)
        api = api_factory(cfg) if api_factory is not None else None
        with Store(path) as store:  # the CLI's call: `fman retry <id> [--up]`
            result = _retry_mod().retry(
                store, cfg, int(ref), tier=None if choice == "same" else "up",
                now=clock(), api=api,
            )  # fmt: skip
        if result.ok:
            return show(result.reason)
        return show(f"Retry refused: {result.reason}", error=True, status=409)

    return [
        Route(ACTIVITY_PATH, page, methods=["GET"]),
        guard.post_route(RETRY_PATH, do_retry),
    ]


__all__ = [
    "ACTIVITY_PATH",
    "CLOSED_LIMIT",
    "NAV_ENTRY",
    "REFRESH_SECONDS",
    "RETRY_PATH",
    "WRITER_TIMEOUT_SECONDS",
    "Running",
    "issue_link",
    "render_activity",
    "retry_forms",
    "routes",
    "running",
    "step_of",
]
