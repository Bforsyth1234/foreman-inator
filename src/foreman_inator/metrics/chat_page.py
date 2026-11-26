"""The ``/chat`` page: the browser front end of chat sessions (todo #75, GH #64).

Design: ``docs/chat-design.md`` section 11 (row C3a of section 14) and the user's
decisions of 2026-11-25. ``routes(store_path, manager_factory=...)`` adds:

* ``GET /chat``: start a session (watched repos, "local only" marked; base branch; start
  tier) and the list of sessions (read from the database at ``store_path``, read-only);
* ``POST /chat/start``: ``ChatManager.start``, then 303 to ``/chat/<id>``;
* ``GET /chat/<id>``: the session: tier and model, the cost line (session, chat sub-cap,
  month) and the per-message budget, files it holds and lock warnings (decision 6),
  the transcript so far (server-rendered from the session's event buffer, or the
  recording for a session no longer open here), the message box and the buttons. A
  session that is not open is read-only plus "Open as draft PR" / "Discard";
* ``GET /chat/<id>/events``: ``text/event-stream`` from ``ChatManager.events``, replaying
  after ``Last-Event-ID`` (or ``?after=``). Read-only, no side effects. A session with
  nothing more to say answers 204, which tells ``EventSource`` to stop reconnecting;
* ``POST /chat/<id>/send`` (``text``), ``/tier`` (``direction=up|down`` or ``tier=N``),
  ``/abort``, ``/approve``, ``/pr`` (``title``), ``/discard`` (needs ``confirm=yes``).

Every POST is a ``forms.FormGuard.post_route`` (CSRF token, own loopback Origin/Host,
form-urlencoded, 403 and no change otherwise) and is a plain HTML form: with JavaScript
off it answers 303 back to the page (or the page with the error), and a busy session's
page refreshes itself (``<noscript>`` meta refresh), so the page works without the
script. Buttons go through ``ChatManager.command``, so their replies and errors are
events like the terminal's commands.

The one inline script (no external scripts, no framework) opens an ``EventSource``,
appends model text with ``textContent`` (never ``innerHTML``: model output is untrusted)
and upgrades the forms to ``fetch`` with the same urlencoded body (token included; the
browser adds its own ``Origin``). Each response carries a fresh nonce and
``Content-Security-Policy: default-src 'none'; script-src 'nonce-<n>'; style-src
'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none';
frame-ancestors 'none'``, so an injected ``<script>`` without the nonce never runs. Every
server-rendered string (repo names, transcript, errors) is ``html.escape``d.

References (checked 2026-11-25):

* WHATWG HTML, "Server-sent events" (``Last-Event-ID``, ``retry``, comment lines, and a
  204 response stopping reconnection): https://html.spec.whatwg.org/multipage/
  server-sent-events.html
* CSP Level 3, nonces (``script-src 'nonce-...'``, base64 value): https://www.w3.org/TR/
  CSP3/#grammardef-nonce-source and MDN https://developer.mozilla.org/en-US/docs/Web/
  HTTP/Headers/Content-Security-Policy/script-src
* Starlette ``StreamingResponse``: https://www.starlette.io/responses/#streamingresponse
"""

from __future__ import annotations

import contextlib
import gzip
import html
import json
import secrets
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import BaseRoute, Route

from foreman_inator import chat
from foreman_inator.metrics import forms, web

CHAT_PATH = "/chat"
#: The page's nav entry (the wiring step adds it to ``web.full_app``'s nav).
NAV_ENTRY = (CHAT_PATH, "Chat")

HEARTBEAT_SECONDS = 15.0  # an SSE comment this often keeps the connection open
RETRY_MS = 2000  # EventSource reconnect delay
REFRESH_SECONDS = 3  # the no-JavaScript page refreshes this often while a turn runs
SECRETS_NOTE = (
    "What you type here goes to the model and into the session's recording on this Mac."
    " Do not paste keys or passwords."
)

#: A factory for the process's one ``ChatManager`` (called once, on first use).
ManagerFactory = Callable[[], Any]

e = html.escape


def csp(nonce: str) -> str:
    """The ``/chat`` pages' Content-Security-Policy (design section 11)."""
    return (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )


def new_nonce() -> str:
    return secrets.token_urlsafe(18)


# =========================================================================== transcript


def _blocks(events: Sequence[chat.ChatEvent]) -> list[tuple[str, Any]]:
    """Events -> transcript blocks, the same grouping the script does: ``("you", text)``,
    ``("reply", {part: text})``, ``(note_class, text)``."""
    out: list[tuple[str, Any]] = []
    reply: dict[str, str] | None = None
    for ev in events:
        d = ev.data
        if ev.type == "text":
            if reply is None:
                reply = {}
                out.append(("reply", reply))
            part = str(d.get("part", ""))
            text = str(d.get("text", ""))
            reply[part] = text if d.get("mode") == "replace" else reply.get(part, "") + text
            continue
        line = _note(ev)
        if ev.type == "user":
            out.append(("you", str(d.get("text", ""))))
        elif line is not None:
            out.append(line)
        else:
            continue
        reply = None
    return out


def _note(ev: chat.ChatEvent) -> tuple[str, str] | None:
    d = ev.data
    if ev.type == "tool":
        err = " (error)" if d.get("is_error") else ""
        summary = f": {d['summary']}" if d.get("summary") else ""
        return "tool", f"tool {d.get('name', '')}{summary}{err}"
    if ev.type == "refused":
        return "err", f"refused: {d.get('message') or d.get('cap', '')} - {d.get('hint', '')}"
    if ev.type == "error":
        return "err", str(d.get("message", "error"))
    if ev.type == "info":
        return "info", str(d.get("text", ""))
    if ev.type in ("warning", "fallback"):
        return "warn", str(d.get("message", ""))
    if ev.type == "status" and d.get("state") == "retry":
        return "info", "the provider is retrying"
    if ev.type == "end":
        return "info", f"session ended: {d.get('reason', '')}"
    return None


def _recorded(recordings_dir: Path, session_id: int) -> list[tuple[str, Any]]:
    """A session no longer open here: its messages read back from the recording (the
    user rows and the model's text), the same blocks as live events."""
    from foreman_inator.tracing.recorder import chat_recording_path

    path = chat_recording_path(recordings_dir, session_id)
    out: list[tuple[str, Any]] = []
    for p in (path, path.with_suffix("")):  # finished (.gz) or still open (.jsonl)
        try:
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    text = row.get("text")
                    if not isinstance(text, str) or not text:
                        continue
                    if row.get("kind") == "user":
                        out.append(("you", text))
                    elif row.get("kind") == "llm":
                        out.append(("reply", {"": text}))
            return out
        except OSError:
            continue
    return out


def render_blocks(blocks: Sequence[tuple[str, Any]]) -> str:
    parts = []
    for kind, value in blocks:
        if kind == "you":
            parts.append(f'<div class="msg you"><span class="who">you</span><pre>{e(value)}</pre>'
                         "</div>")  # fmt: skip
        elif kind == "reply":
            texts = "".join(f'<pre class="part">{e(t)}</pre>' for t in value.values())
            parts.append(f'<div class="msg reply"><span class="who">model</span>{texts}</div>')
        else:
            parts.append(f'<div class="note {e(kind)}">{e(value)}</div>')
    return "".join(parts)


# =========================================================================== the script

#: The one inline script (design section 11). Constant text: nothing is interpolated into
#: it; the session id and the replay point come from escaped ``data-`` attributes.
SCRIPT = r"""
(function () {
  "use strict";
  var log = document.getElementById("log");
  if (!log || !window.EventSource || !window.fetch) return;
  var sid = log.getAttribute("data-session");
  var after = log.getAttribute("data-after") || "0";
  var approve = document.getElementById("approve");
  var reply = null, parts = {};
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function add(n) { log.appendChild(n); n.scrollIntoView({block: "end"}); return n; }
  function note(cls, text) { reply = null; add(el("div", "note " + cls, String(text || ""))); }
  function setText(id, text) { var n = document.getElementById(id); if (n) n.textContent = text; }
  function busy(on) {
    var b = document.querySelectorAll("[data-idle-only]");
    for (var i = 0; i < b.length; i++) b[i].disabled = on;
    setText("state", on ? "busy" : "idle");
  }
  var es = new EventSource("/chat/" + encodeURIComponent(sid) + "/events?after=" +
                           encodeURIComponent(after));
  function on(type, fn) {
    es.addEventListener(type, function (m) {
      var d = {};
      try { d = JSON.parse(m.data) || {}; } catch (err) { return; }
      fn(d);
    });
  }
  on("user", function (d) {
    reply = null;
    var b = add(el("div", "msg you"));
    b.appendChild(el("span", "who", "you"));
    b.appendChild(el("pre", "", String(d.text || "")));
  });
  on("text", function (d) {
    if (!reply) {
      reply = add(el("div", "msg reply"));
      reply.appendChild(el("span", "who", "model"));
      parts = {};
    }
    var key = String(d.part || ""), p = parts[key];
    if (!p) { p = parts[key] = reply.appendChild(el("pre", "part", "")); }
    if (d.mode === "replace") p.textContent = String(d.text || "");
    else p.textContent += String(d.text || "");
    p.scrollIntoView({block: "end"});
  });
  on("tool", function (d) {
    note("tool", "tool " + (d.name || "") + (d.summary ? ": " + d.summary : "") +
         (d.is_error ? " (error)" : ""));
  });
  on("status", function (d) {
    if (d.state === "busy") { busy(true); if (approve) approve.hidden = true; }
    else if (d.state === "idle") busy(false);
    else if (d.state === "retry") note("info", "the provider is retrying");
  });
  on("tier", function (d) {
    setText("tier", "tier " + d.tier + " · " + d.model + " (" + d.kind + ")");
  });
  on("cost", function (d) { setText("cost", d.text || ""); });
  on("refused", function (d) {
    note("err", "refused: " + (d.message || d.cap || "") + " - " + (d.hint || ""));
    if (approve && d.approvable) approve.hidden = false;
  });
  on("error", function (d) { note("err", d.message || "error"); });
  on("info", function (d) { note("info", d.text); });
  on("warning", function (d) { note("warn", d.message); setText("lockwarn", d.message || ""); });
  on("fallback", function (d) { note("warn", d.message); });
  on("end", function (d) {
    note("info", "session ended: " + (d.reason || ""));
    es.close();
    var f = document.querySelectorAll("[data-live]");
    for (var i = 0; i < f.length; i++) f[i].hidden = true;
  });
  var forms = document.querySelectorAll("form[data-js]");
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener("submit", function (ev) {
      var f = ev.currentTarget;
      ev.preventDefault();
      fetch(f.action, {method: "POST", body: new URLSearchParams(new FormData(f)),
                       headers: {"Accept": "application/json"}, credentials: "same-origin"})
        .then(function (r) {
          return r.text().then(function (t) {
            var d = null;
            try { d = JSON.parse(t); } catch (err) { d = null; }
            if (!r.ok) note("err", (d && d.error) || t || ("HTTP " + r.status));
            else if (f.getAttribute("data-js") === "send") f.reset();
          });
        })
        .catch(function () { note("err", "could not reach the page server"); });
    });
  }
})();
"""

_STYLE = """
.log { display: flex; flex-direction: column; gap: 0.5rem; margin: 0.6rem 0; }
.msg { border: 1px solid var(--line); border-radius: var(--radius); padding: 0.5rem 0.7rem; }
.msg.you { background: var(--row); }
.msg .who { font-size: 0.75rem; text-transform: uppercase; color: var(--muted); }
.msg pre { white-space: pre-wrap; word-break: break-word; margin: 0.2rem 0 0;
  font: 0.92em ui-monospace, SFMono-Regular, Menlo, monospace; }
.note { font-size: 0.88rem; color: var(--muted); padding-left: 0.7rem; }
.note.err { color: var(--danger); }
.note.warn, .banner { color: var(--danger); font-weight: 600; }
.banner { border: 1px solid var(--danger); border-radius: var(--radius); padding: 0.5rem; }
textarea { font: inherit; color: var(--fg); background: var(--bg); width: 100%;
  min-height: 5rem; padding: 0.4rem; border: 1px solid var(--line); border-radius: 6px; }
label.wide { flex: 1 1 100%; }
.badge { font-size: 0.75rem; border: 1px solid var(--line); border-radius: 999px;
  padding: 0 0.45rem; color: var(--muted); }
"""


# =========================================================================== helpers


class _Lazy:
    """The process's one manager, built on first use (its ``recover`` runs then)."""

    def __init__(self, factory: ManagerFactory) -> None:
        self._factory = factory
        self._value: Any = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        with self._lock:
            if self._value is None:
                self._value = self._factory()
            return self._value


class _Unavailable(Exception):
    pass


def _clean(mgr: Any, text: str) -> str:
    """7d: errors shown on the page go through the manager's scrubber too."""
    scrub = getattr(mgr, "scrub", None)
    if callable(scrub):
        with contextlib.suppress(Exception):
            return str(scrub(text))
    return text


def _money(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "?"


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _options(pairs: Sequence[tuple[str, str]], current: str = "") -> str:
    return "".join(
        f'<option value="{e(v)}"{" selected" if v == current else ""}>{e(t)}</option>'
        for v, t in pairs
    )


# =========================================================================== routes


def routes(
    store_path: str | Path,
    *,
    manager_factory: ManagerFactory,
    guard: forms.FormGuard | None = None,
    nav: Sequence[tuple[str, str]] | None = None,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> list[BaseRoute]:
    """The page's routes. ``manager_factory()`` returns this process's ``ChatManager``
    (called once, on first use); ``store_path`` is read read-only for the session list
    and turn table; ``guard`` defaults to ``forms.default_guard()``."""
    db = Path(store_path)
    guard = guard or forms.default_guard()
    nav = tuple(nav) if nav is not None else (*web.M4_NAV, NAV_ENTRY)
    managers = _Lazy(manager_factory)

    def manager() -> Any:
        try:
            return managers.get()
        except Exception as exc:
            raise _Unavailable(f"chat is unavailable: {type(exc).__name__}: {exc}") from exc

    def read(fn: Callable[[Any], Any], default: Any) -> Any:
        try:
            store = web.open_store(db)
        except (FileNotFoundError, sqlite3.Error):
            return default
        try:
            return fn(store)
        except sqlite3.Error:
            return default
        finally:
            store.close()

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        nonce = new_nonce()
        full = f"<style>{_STYLE}</style>{body}"
        full += f'<script nonce="{e(nonce)}">{SCRIPT}</script>' if 'id="log"' in body else ""
        resp = HTMLResponse(web.render_page(title, full, nav=nav), status)
        resp.headers["Content-Security-Policy"] = csp(nonce)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    def message(text: str, error: bool = True) -> str:
        cls = "error" if error else "lede"
        return f'<p class="{cls}" role="alert">{e(text)}</p>' if text else ""

    # ----------------------------------------------------------------- GET /chat

    def index_body(mgr: Any, error: str = "") -> str:
        cfg = mgr.config
        repos = tuple(getattr(cfg, "repos", ()))
        local = read(lambda s: s.local_only_repos(), frozenset())
        ladder = chat.tier_ladder(getattr(cfg, "models", ()))
        start_tier = getattr(mgr, "start_tier", chat.DEFAULT_START_TIER)
        head = (
            '<h1>Chat</h1><p class="lede">Interactive coding sessions in their own worktree,'
            " through the same tiers, spend caps and sandbox as tickets. One message is one"
            " turn; a session ends as a draft PR or is discarded.</p>"
        )
        if not repos:
            start = '<p class="empty">No watched repos yet: add one on Settings.</p>'
        else:
            repo_opts = _options([(r, f"{r} (local only)" if r in local else r) for r in repos])
            tiers = [("", f"default (tier {start_tier})")] + [
                (str(r.tier), f"tier {r.tier}: {r.endpoint.name} ({r.endpoint.kind})")
                for r in ladder
            ]
            start = (
                f'<form method="post" action="{CHAT_PATH}/start">{guard.hidden_field()}'
                f'<label>Repo<select name="repo">{repo_opts}</select></label>'
                '<label>Base branch<input name="base" placeholder="default branch"></label>'
                f'<label>Start tier<select name="tier">{_options(tiers)}</select></label>'
                '<button type="submit">Start session</button></form>'
                "<small>Local-only repos use local models only; tier 1 is local by default."
                "</small>"
            )
        sessions = read(lambda s: s.list_chat_sessions(), [])
        rows = "".join(
            f'<tr><td><a href="{CHAT_PATH}/{int(s.id)}">#{int(s.id)}</a></td><td>{e(s.repo)}</td>'
            f"<td>{e(s.state)}</td><td>tier {int(s.tier)} · {e(s.model)}</td>"
            f'<td class="muted">{e(str(s.started_at))}</td></tr>'
            for s in sessions
        )
        table = (
            '<div class="scroll"><table><tr><th>Session</th><th>Repo</th><th>State</th>'
            f"<th>Tier</th><th>Started</th></tr>{rows}</table></div>"
            if rows
            else '<p class="empty">No chat sessions yet.</p>'
        )
        return (
            head + message(error)
            + f'<section class="card"><h2>Start a session</h2>{start}</section>'
            + f'<section class="card"><h2>Sessions</h2>{table}</section>'
        )  # fmt: skip

    def index(request: Request) -> Response:
        try:
            return page("Chat", index_body(manager()))
        except _Unavailable as exc:
            return page("Chat", "<h1>Chat</h1>" + message(str(exc)), 503)

    def start(request: Request, form: dict[str, str]) -> Response:
        try:
            mgr = manager()
        except _Unavailable as exc:
            return page("Chat", "<h1>Chat</h1>" + message(str(exc)), 503)
        repo = form.get("repo", "").strip()
        base = form.get("base", "").strip() or None
        tier_text = form.get("tier", "").strip()
        if tier_text and not tier_text.isdigit():
            return page("Chat", index_body(mgr, "start tier must be a number"), 400)
        try:
            session = mgr.start(repo, base_branch=base, tier=int(tier_text) if tier_text else None)
        except chat.ChatError as exc:
            return page("Chat", index_body(mgr, _clean(mgr, str(exc))), 409)
        return RedirectResponse(f"{CHAT_PATH}/{int(session.id)}", 303)

    # ----------------------------------------------------------------- GET /chat/<id>

    def session_body(mgr: Any, sid: int, error: str = "") -> str:
        st = mgr.status(sid)
        cost = mgr.cost(sid)
        cfg = mgr.config
        live = st["state"] in chat.LIVE_STATES
        final = st["state"] in chat.FINAL_STATES
        busy = st["state"] == "busy"
        events = list(mgr.events(sid, after=0, timeout=0))
        blocks = _blocks(events) if events else _recorded(mgr.recordings_dir, sid)
        last_seq = events[-1].seq if events else 0
        out = [f"<h1>Chat #{int(sid)} · {e(st['repo'])}</h1>", message(error)]
        if busy:  # the refresh-based view without JavaScript
            out.append(f'<noscript><meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
                       "</noscript>")  # fmt: skip
        if getattr(cfg, "sandbox", "on") == "off":  # 7g: always shown
            from foreman_inator.runner import sandbox as sb

            line = sb.describe("off", dict(getattr(cfg, "sandbox_network", ())))
            out.append(f'<p class="banner">{e(line)}</p>')
        badges = []
        if st.get("local_only"):
            badges.append('<span class="badge">local only</span>')
        if st.get("network"):
            badges.append('<span class="badge">chat network on (443/80)</span>')
        kind = f" ({st['kind']})" if st.get("kind") else ""
        budget = getattr(mgr, "turn_budget_usd", chat.DEFAULT_TURN_BUDGET_USD)
        out.append(
            '<section class="card">'
            f"<p><code>{e(st['branch'])}</code> from <code>{e(st['base_branch'])}</code> · "
            f'<span id="state">{e(st["state"])}</span> {" ".join(badges)}</p>'
            f'<p class="headline">Next message: <span id="tier">tier {int(st["tier"])} · '
            f"{e(st['model'])}{e(kind)}</span></p>"
            f'<p>Cost: <span id="cost">{e(cost.get("text", ""))}</span> · per-message budget '
            f"{e(_money(budget))}</p>" + locks_html(st, events) + "</section>"
        )
        out.append(
            f'<section class="card"><h2>Transcript</h2><div id="log" class="log" '
            f'data-session="{int(sid)}" data-after="{int(last_seq)}">{render_blocks(blocks)}'
            "</div>"
        )
        if live:
            out.append(live_forms(mgr, sid, events, busy))
        out.append("</section>")
        if not final:
            out.append(end_forms(sid, busy, live))
        out.append(turns_table(sid))
        out.append(f'<p><a href="{CHAT_PATH}">All chat sessions</a></p>')
        return "".join(out)

    def locks_html(st: dict[str, Any], events: Sequence[chat.ChatEvent]) -> str:
        held = list(st.get("locks") or ())
        warn = next((ev.data for ev in reversed(events) if ev.type == "warning"), None)
        text = str(warn.get("message", "")) if warn else ""
        out = ""
        if held:
            out += f"<p><small>Files this chat holds: {e(', '.join(map(str, held)))}</small></p>"
        return out + f'<p class="note warn" id="lockwarn">{e(text)}</p>'

    def approvable(events: Sequence[chat.ChatEvent]) -> bool:
        """The last turn was refused at a cap that an approval can lift."""
        for ev in reversed(events):
            if ev.type == "refused":
                return bool(ev.data.get("approvable"))
            if ev.type in ("user", "end") or (
                ev.type == "status" and ev.data.get("state") == "busy"
            ):
                return False
        return False

    def form(action: str, inner: str, button: str, *, js: str = "1", attrs: str = "",
             cls: str = "", btn_attrs: str = "") -> str:  # fmt: skip
        klass = f' class="{e(cls)}"' if cls else ""
        return (
            f'<form method="post" action="{e(action)}" data-js="{e(js)}"{klass}{attrs}>'
            f"{guard.hidden_field()}{inner}"
            f'<button type="submit"{btn_attrs}>{e(button)}</button></form>'
        )

    def live_forms(mgr: Any, sid: int, events: Sequence[chat.ChatEvent], busy: bool) -> str:
        base = f"{CHAT_PATH}/{int(sid)}"
        idle_only = " data-idle-only" + (" disabled" if busy else "")
        spend = getattr(mgr, "spend", None)
        up_to = getattr(spend, "approval_usd", None)
        allow = "Allow this message" + (f" (up to {_money(up_to)})" if up_to is not None else "")
        hidden = "" if approvable(events) else " hidden"
        send = form(
            f"{base}/send",
            '<label class="wide">Message (/help lists the commands)'
            '<textarea name="text" required></textarea></label>',
            "Send",
            js="send",
            attrs=" data-live",
            btn_attrs=idle_only,
        )
        tier = (
            '<div class="actions" data-live>'
            + form(f"{base}/tier", '<input type="hidden" name="direction" value="down">',
                   "Tier down", cls="inline")
            + form(f"{base}/tier", '<input type="hidden" name="direction" value="up">',
                   "Move up a tier", cls="inline")
            + form(f"{base}/abort", "", "Abort", cls="inline")
            + "</div>"
        )  # fmt: skip
        approve = form(f"{base}/approve", "", allow, attrs=f' id="approve"{hidden} data-live',
                       cls="inline")  # fmt: skip
        return (
            approve
            + send
            + f"<p><small>{e(SECRETS_NOTE)} A tier change applies to the next message only."
            "</small></p>" + tier
        )

    def end_forms(sid: int, busy: bool, live: bool) -> str:
        base = f"{CHAT_PATH}/{int(sid)}"
        idle_only = " data-idle-only" + (" disabled" if busy else "")
        pr = form(
            f"{base}/pr",
            '<label>PR title<input name="title" placeholder="first line of the first message">'
            "</label>",
            "Open as draft PR",
            js="",
            btn_attrs=idle_only,
        )
        discard = form(
            f"{base}/discard",
            '<label><span><input type="checkbox" name="confirm" value="yes" required> yes,'
            " delete this session's worktree and branch (nothing is pushed)</span></label>",
            "Discard",
            js="",
            cls="danger",
            btn_attrs=idle_only,
        )
        note = "" if live else '<p class="lede">This session is not open: read-only.</p>'
        return (
            '<section class="card"><h2>Finish</h2>' + note
            + "<p><small>A draft PR, not reviewed by the AI reviewer; nothing merges without"
            " you.</small></p>" + pr + discard + "</section>"
        )  # fmt: skip

    def turns_table(sid: int) -> str:
        turns = read(lambda s: s.list_chat_turns(sid), [])
        if not turns:
            return ""
        rows = "".join(
            f"<tr><td>{int(t.seq)}</td><td>tier {int(t.tier)} · {e(t.model)}</td>"
            f"<td>{e(t.status or 'running')}</td><td>{e(_money(t.cost_usd))}</td>"
            f"<td>{int(t.tool_calls)}</td></tr>"
            for t in turns
        )
        return (
            '<section class="card"><h2>Turns</h2><div class="scroll"><table><tr><th>#</th>'
            "<th>Tier</th><th>Status</th><th>Cost</th><th>Tools</th></tr>"
            f"{rows}</table></div></section>"
        )

    def session_page(request: Request, sid: int, error: str = "", status: int = 200) -> Response:
        try:
            mgr = manager()
            mgr.get(sid)
        except _Unavailable as exc:
            return page("Chat", "<h1>Chat</h1>" + message(str(exc)), 503)
        except chat.ChatError as exc:
            return page("Chat", "<h1>Chat</h1>" + message(_clean(mgr, str(exc))), 404)
        return page(f"Chat #{sid}", session_body(mgr, sid, error), status)

    def session_get(request: Request) -> Response:
        return session_page(request, int(request.path_params["sid"]))

    # ----------------------------------------------------------------- SSE

    async def events(request: Request) -> Response:
        sid = int(request.path_params["sid"])
        try:
            mgr = await run_in_threadpool(manager)
            row = await run_in_threadpool(mgr.get, sid)
        except _Unavailable as exc:
            return Response(str(exc), 503, media_type="text/plain")
        except chat.ChatError:
            return Response("no such chat session", 404, media_type="text/plain")
        after = _after(request)
        pending = list(mgr.events(sid, after=after, timeout=0))
        if row.state not in chat.LIVE_STATES and not pending:
            return Response(status_code=204)  # nothing more: EventSource stops

        async def body() -> AsyncIterator[str]:
            seq = after
            yield f"retry: {RETRY_MS}\n\n"
            while True:
                gen = mgr.events(sid, after=seq, timeout=heartbeat)
                while True:
                    ev = await run_in_threadpool(next, gen, None)
                    if ev is None:
                        break
                    seq = ev.seq
                    yield ev.to_sse()
                    if ev.type == "end":
                        return
                if await request.is_disconnected():
                    return
                state = (await run_in_threadpool(mgr.get, sid)).state
                if state not in chat.LIVE_STATES:
                    return  # the reconnect gets 204
                yield ": keepalive\n\n"

        nonce = new_nonce()
        headers = {
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Content-Security-Policy": csp(nonce),
        }
        return StreamingResponse(body(), media_type="text/event-stream", headers=headers)

    # ----------------------------------------------------------------- POSTs

    def act(request: Request, run: Callable[[Any, int], Any]) -> Response:
        """Run one action on the session; 303 back (or JSON for the script)."""
        sid = int(request.path_params["sid"])
        try:
            mgr = manager()
        except _Unavailable as exc:
            return _fail(request, sid, str(exc), 503)
        try:
            mgr.get(sid)
        except chat.ChatError as exc:
            return _fail(request, sid, _clean(mgr, str(exc)), 404)
        try:
            run(mgr, sid)
        except _BadInput as exc:
            return _fail(request, sid, str(exc), 400)
        except chat.ChatError as exc:  # busy, not open, at the top tier, ...
            return _fail(request, sid, _clean(mgr, str(exc)), 409)
        if _wants_json(request):
            return JSONResponse({"ok": True})
        return RedirectResponse(f"{CHAT_PATH}/{sid}", 303)

    def _fail(request: Request, sid: int, text: str, status: int) -> Response:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": text}, status)
        if status in (404, 503):
            return page("Chat", "<h1>Chat</h1>" + message(text), status)
        return session_page(request, sid, text, status)

    def send(request: Request, form_: dict[str, str]) -> Response:
        text = form_.get("text", "")

        def run(mgr: Any, sid: int) -> None:
            if not text.strip():
                raise _BadInput("type a message first")
            mgr.send(sid, text)

        return act(request, run)

    def tier(request: Request, form_: dict[str, str]) -> Response:
        direction = form_.get("direction", "").strip().lower()
        number = form_.get("tier", "").strip()

        def run(mgr: Any, sid: int) -> None:
            if direction in ("up", "down"):
                arg = direction
            elif number.isdigit():
                arg = number
            else:
                raise _BadInput("tier takes direction=up|down or tier=N")
            mgr.command(sid, chat.Command("tier", arg))

        return act(request, run)

    def simple(name: str) -> Callable[[Request, dict[str, str]], Response]:
        def handler(request: Request, form_: dict[str, str]) -> Response:
            return act(request, lambda mgr, sid: mgr.command(sid, chat.Command(name)))  # type: ignore[arg-type]

        return handler

    def pr(request: Request, form_: dict[str, str]) -> Response:
        title = form_.get("title", "").strip()
        return act(request, lambda mgr, sid: mgr.command(sid, chat.Command("pr", title)))

    def discard(request: Request, form_: dict[str, str]) -> Response:
        confirmed = form_.get("confirm", "").strip().lower() == "yes"

        def run(mgr: Any, sid: int) -> None:
            if not confirmed:
                raise _BadInput("tick the box to confirm the discard")
            mgr.command(sid, chat.Command("discard", "yes"))

        return act(request, run)

    sid_path = CHAT_PATH + "/{sid:int}"
    return [
        Route(CHAT_PATH, index, methods=["GET"]),
        guard.post_route(f"{CHAT_PATH}/start", start),
        Route(sid_path, session_get, methods=["GET"]),
        Route(f"{sid_path}/events", events, methods=["GET"]),
        guard.post_route(f"{sid_path}/send", send),
        guard.post_route(f"{sid_path}/tier", tier),
        guard.post_route(f"{sid_path}/abort", simple("abort")),
        guard.post_route(f"{sid_path}/approve", simple("approve")),
        guard.post_route(f"{sid_path}/pr", pr),
        guard.post_route(f"{sid_path}/discard", discard),
    ]


class _BadInput(Exception):
    pass


def _after(request: Request) -> int:
    """The replay point: ``Last-Event-ID`` (a reconnect) else ``?after=`` else 0."""
    for raw in (request.headers.get("last-event-id"), request.query_params.get("after")):
        if raw and raw.strip().isdigit():
            return int(raw.strip())
    return 0


__all__ = ["CHAT_PATH", "NAV_ENTRY", "SCRIPT", "csp", "render_blocks", "routes"]
