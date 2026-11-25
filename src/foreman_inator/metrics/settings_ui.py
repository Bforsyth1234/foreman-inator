"""Edit settings from the metrics page (todo #73, GH #62).

``routes(store_path)`` adds to the metrics app:

* ``GET /settings``: one form for the config.toml settings (spend caps,
  ``writer_timeout_seconds``, ``max_parallel_per_repo``, ``max_open_prs_per_repo``,
  ``allowed_users``, the watched ``repos``, ``status_comment``) and one form per watched
  repo for its settings in the database (verify and install commands, profile, cloud
  on/off). Read-only;
* ``POST /settings``: reads config.toml as TOML, sets the submitted keys, and builds the
  new config with ``config.from_dict`` (exactly the validation ``config.load`` runs); a
  refusal shows ``config.load``'s own message (``"<path>: <error>"``) and the file is not
  touched. A valid form is written with ``config.save`` (as ``fman repo add`` does: the
  whole file is rewritten from the typed config, mode 600), and the page lists what
  changed (``worker_config_reload.changes``). A form loaded before the file last changed
  is refused (its hidden ``config_hash``), so a hand edit is never overwritten blind;
* ``POST /settings/repo``: one watched repo's settings, through the CLI's own functions:
  ``worker_status.set_verify_command`` (``fman repo set-verify``; unflags tickets waiting
  for one), ``Store.set_install_command`` (``set-install``), ``worker_status.set_profile``
  (``set-profile``), ``Store.set_cloud`` (``set-cloud``). Only changed values are written.

Every POST goes through ``forms.FormGuard`` (CSRF token, own loopback Origin/Host: 403
and no change otherwise). Every value from the file or the database is HTML-escaped; no
JavaScript. API keys are not on this page (config.toml holds only Keychain references).
The running worker re-reads config.toml when its mtime changes (``worker_config_reload``),
so a save applies from its next pass without a restart; the page says so. Repo settings
live in the database, which the worker reads at each decision.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
import sqlite3
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import BaseRoute, Route

from foreman_inator import config as config_mod
from foreman_inator import paths
from foreman_inator.config import Config, ConfigError
from foreman_inator.db.store import Store
from foreman_inator.metrics import forms, web
from foreman_inator.routing import profile as prof
from foreman_inator.worker_config_reload import changes

SETTINGS_PATH = "/settings"
REPO_PATH = "/settings/repo"
#: The page's nav entry (the wiring step adds it to ``web.full_app``'s nav).
NAV_ENTRY = (SETTINGS_PATH, "Settings")

RELOAD_NOTE = (
    "The running worker re-reads config.toml when the file changes, so a saved change"
    " applies from its next pass (no restart). Repo settings are read at each decision."
)

e = html.escape

#: (form field, label, path in config.toml). Numbers; parsed as TOML would type them.
NUMBER_FIELDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("daily_usd", "Daily cloud spend cap (USD)", ("spend_caps", "daily_usd")),
    ("monthly_usd", "Monthly cloud spend cap (USD)", ("spend_caps", "monthly_usd")),
    (
        "experiments_monthly_usd",
        "Monthly experiments budget (USD)",
        ("spend_caps", "experiments_monthly_usd"),
    ),
    ("writer_timeout_seconds", "Writer session timeout (seconds)", ("writer_timeout_seconds",)),
    ("max_parallel_per_repo", "Tickets at once per repo", ("max_parallel_per_repo",)),
    ("max_open_prs_per_repo", "Open PRs per repo (0 = no cap)", ("max_open_prs_per_repo",)),
)
#: (form field, label): one entry per line (or comma separated).
LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("allowed_users", "Allowed users (GitHub logins whose labels start work)"),
    ("repos", "Watched repos (owner/name)"),
)
#: (form field, label): checkboxes.
BOOL_FIELDS: tuple[tuple[str, str], ...] = (
    ("status_comment", "Keep a status comment on each issue while work runs"),
)

_INT_RE = re.compile(r"^[+-]?\d+$")
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class _Fail(Exception):
    def __init__(self, text: str, status: int) -> None:
        super().__init__(text)
        self.status = status


# =========================================================================== values


def _number(text: str) -> Any:
    """``text`` as TOML would type it: int, finite float, else the string itself (so
    ``config.from_dict`` refuses it with its own "wrong type" message)."""
    s = text.strip()
    if _INT_RE.match(s):
        return int(s)
    try:
        f = float(s)
    except ValueError:
        return text
    return f if math.isfinite(f) else text


def _items(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[\n,]", text) if p.strip()]


def _get(data: dict[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(k)  # type: ignore[assignment]
    return data


def _put(data: dict[str, Any], keys: Sequence[str], value: Any) -> None:
    for k in keys[:-1]:
        sub = data.get(k)
        if not isinstance(sub, dict):
            sub = data[k] = {}
        data = sub
    data[keys[-1]] = value


def form_values(cfg: Config) -> dict[str, str]:
    """The form's fields as the current config holds them."""
    data = config_mod.to_dict(cfg)
    out = {name: str(_get(data, keys)) for name, _, keys in NUMBER_FIELDS}
    for name, _ in LIST_FIELDS:
        out[name] = "\n".join(getattr(cfg, name))
    for name, _ in BOOL_FIELDS:
        out[name] = "on" if getattr(cfg, name) else ""
    return out


def apply_form(raw: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    """``raw`` (config.toml parsed as TOML) with the form's settings set. A field the
    form did not send is left as it is; an unchecked checkbox is sent as absent, so the
    form carries ``<name>__present`` for each checkbox."""
    out = dict(raw)
    if isinstance(out.get("spend_caps"), dict):
        out["spend_caps"] = dict(out["spend_caps"])
    for name, _, keys in NUMBER_FIELDS:
        if name in form:
            _put(out, keys, _number(form[name]))
    for name, _ in LIST_FIELDS:
        if name in form:
            out[name] = _items(form[name])
    for name, _ in BOOL_FIELDS:
        if f"{name}__present" in form:
            out[name] = form.get(name) == "on"
    return out


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return "missing"


def read_raw(path: Path) -> dict[str, Any]:
    """config.toml as TOML (``{}`` when missing). ``ConfigError`` with ``config.load``'s
    text when it is not valid TOML."""
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def build(path: Path, raw: dict[str, Any]) -> Config:
    """``raw`` validated exactly as ``config.load(path)`` would validate that file, with
    its message (``"<path>: <error>"``) on refusal."""
    try:
        return config_mod.from_dict(raw)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_form(path: Path, form: dict[str, str]) -> tuple[Config, list[str]]:
    """Validate and write the form; the change lines. Raises ``ConfigError`` (file not
    touched) or ``_Fail`` (stale page)."""
    seen = form.get("config_hash", "")
    if seen != file_hash(path):
        raise _Fail(
            "config.toml changed since this page was loaded; nothing was saved."
            " Look at the values below and save again.",
            409,
        )
    raw = read_raw(path)
    old = build(path, raw)
    new = build(path, apply_form(raw, form))
    lines = changes(old, new)
    if lines:
        config_mod.save(new, path)
    return new, lines


# =========================================================================== html


def _message(text: str | None, error: bool = False) -> str:
    if not text:
        return ""
    cls = ' class="error"' if error else ""
    return f"<p{cls}>{e(text)}</p>"


def _changed(lines: Sequence[str]) -> str:
    if not lines:
        return "<p>Nothing changed.</p>"
    return "<p>Saved. Changed:</p><ul>" + "".join(f"<li>{e(x)}</li>" for x in lines) + "</ul>"


def _config_form(guard: forms.FormGuard, values: dict[str, str], digest: str) -> str:
    parts = [f'<input type="hidden" name="config_hash" value="{e(digest)}">']
    for name, label, _ in NUMBER_FIELDS:
        parts.append(
            f'<p><label>{e(label)} <input type="text" name="{e(name)}"'
            f' value="{e(values.get(name, ""))}"></label></p>'
        )
    for name, label in LIST_FIELDS:
        parts.append(
            f'<p><label>{e(label)}<br><textarea name="{e(name)}" rows="4" cols="40">'
            f"{e(values.get(name, ''))}</textarea></label></p>"
        )
    for name, label in BOOL_FIELDS:
        checked = " checked" if values.get(name) == "on" else ""
        parts.append(
            f'<p><input type="hidden" name="{e(name)}__present" value="1">'
            f'<label><input type="checkbox" name="{e(name)}" value="on"{checked}>'
            f" {e(label)}</label></p>"
        )
    return "<h2>config.toml</h2>" + guard.form(SETTINGS_PATH, "".join(parts), button="Save")


def _options(name: str, options: Sequence[str], current: str) -> str:
    opts = "".join(
        f'<option value="{e(o)}"{" selected" if o == current else ""}>{e(o)}</option>'
        for o in options
    )
    return f'<select name="{e(name)}">{opts}</select>'


def repo_values(settings: Any) -> dict[str, str]:
    p = prof.profile_of(settings)
    return {
        "verify_command": getattr(settings, "verify_command", None) or "",
        "install_command": getattr(settings, "install_command", None) or "",
        "writer_mode": p.writer,
        "review_mode": p.review,
        "cloud": "on" if getattr(settings, "cloud", True) is not False else "off",
    }


def _repo_form(guard: forms.FormGuard, repo: str, v: dict[str, str]) -> str:
    inner = (
        f'<input type="hidden" name="repo" value="{e(repo)}">'
        f'<p><label>Verify command <input type="text" name="verify_command" size="40"'
        f' value="{e(v["verify_command"])}"></label> (empty: detect it)</p>'
        f'<p><label>Install command <input type="text" name="install_command" size="40"'
        f' value="{e(v["install_command"])}"></label> (empty: from the lockfiles)</p>'
        f"<p><label>Writer {_options('writer_mode', prof.WRITER_MODES, v['writer_mode'])}"
        f"</label> <label>Review {_options('review_mode', prof.REVIEW_MODES, v['review_mode'])}"
        f"</label> <label>Cloud models {_options('cloud', ('on', 'off'), v['cloud'])}</label>"
        " (off: local only)</p>"
    )
    return f"<h3>{e(repo)}</h3>" + guard.form(REPO_PATH, inner, button=f"Save {repo}")


# =========================================================================== routes


def routes(
    store_path: str | Path,
    *,
    config_path: str | Path | None = None,
    guard: forms.FormGuard | None = None,
    nav: Sequence[tuple[str, str]] | None = None,
) -> list[BaseRoute]:
    """The page's routes. ``config_path`` defaults to ``paths.config_path()`` (looked up
    per request); ``guard`` to the per-process ``forms.default_guard()``."""
    db = Path(store_path)
    guard = guard or forms.default_guard()
    nav = tuple(nav) if nav is not None else (*web.M4_NAV, NAV_ENTRY)

    def cfg_path() -> Path:
        return Path(config_path) if config_path is not None else paths.config_path()

    def page(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(web.render_page("Settings", body, nav=nav), status)

    def repo_section(cfg: Config) -> str:
        if not cfg.repos:
            return "<h2>Repos</h2><p>No watched repos yet.</p>"
        try:
            store = web.open_store(db)
        except (FileNotFoundError, sqlite3.OperationalError):
            return (
                "<h2>Repos</h2><p>No database yet: per-repo settings appear once the worker"
                " has run.</p>"
            )
        try:
            rows = {r: store.get_repo_settings(r) for r in cfg.repos}
        finally:
            store.close()
        parts = []
        for repo in cfg.repos:
            parts.append(_repo_form(guard, repo, repo_values(rows[repo])))
        return "<h2>Repos</h2>" + "".join(parts)

    def render(
        message: str = "",
        values: dict[str, str] | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        path = cfg_path()
        head = (
            "<h1>Settings</h1>"
            f'<p class="lede">{e(RELOAD_NOTE)}</p><p>File: <code>{e(str(path))}</code></p>'
        )
        try:
            cfg = config_mod.load(path)
        except ConfigError as exc:
            body = _message(str(exc), True) + (
                "<p>config.toml cannot be read as it is, so nothing can be saved here."
                " Fix it by hand (or restore it) and reload this page.</p>"
            )
            return page(head + message + body, status if status != 200 else 500)
        shown = values if values is not None else form_values(cfg)
        body = _config_form(guard, shown, file_hash(path)) + repo_section(cfg)
        return page(head + message + body, status)

    def settings(request: Request) -> Response:
        return render()

    def save(request: Request, form: dict[str, str]) -> Response:
        path = cfg_path()
        try:
            _new, lines = save_form(path, form)
        except _Fail as f:
            return render(_message(str(f), True), status=f.status)
        except ConfigError as exc:
            return render(
                _message(f"Not saved: {exc}", True) + "<p>config.toml was not changed.</p>",
                values={k: v for k, v in form.items() if k != "config_hash"},
                status=400,
            )
        return render(_changed(lines))

    def save_repo(request: Request, form: dict[str, str]) -> Response:
        from foreman_inator.worker_status import set_profile, set_verify_command

        repo = form.get("repo", "").strip()
        try:
            cfg = config_mod.load(cfg_path())
        except ConfigError as exc:
            return render(_message(str(exc), True), status=500)
        if not _REPO_RE.match(repo):
            return render(_message("repo must look like owner/name", True), status=400)
        if repo not in cfg.repos:
            msg = f"{repo!r} is not a watched repo in config.toml; nothing was saved."
            return render(_message(msg, True), status=400)
        if not db.exists():
            return render(_message("No database yet: run the worker first.", True), status=503)
        lines: list[str] = []
        with Store(db) as store:
            cur = repo_values(store.get_repo_settings(repo))
            want = {k: form.get(k, cur[k]).strip() for k in cur}
            try:
                p = prof.resolve(None, writer=want["writer_mode"], review=want["review_mode"])
            except prof.ProfileError as exc:
                return render(_message(f"Not saved: {exc}", True), status=400)
            if want["cloud"] not in ("on", "off"):
                return render(_message("Not saved: cloud must be on or off", True), status=400)
            if want["verify_command"] != cur["verify_command"]:
                cleared = set_verify_command(store, repo, want["verify_command"] or None)
                lines.append(_line("verify command", cur, want, "verify_command"))
                if cleared:
                    ids = ", ".join(f"#{i}" for i in cleared)
                    lines.append(f"unflagged ticket(s) {ids}; work resumes")
            if want["install_command"] != cur["install_command"]:
                store.set_install_command(repo, want["install_command"] or None)
                lines.append(_line("install command", cur, want, "install_command"))
            if (p.writer, p.review) != (cur["writer_mode"], cur["review_mode"]):
                set_profile(store, repo, None, writer=p.writer, review=p.review)
                was = f"writer {cur['writer_mode']}, review {cur['review_mode']}"
                lines.append(f"profile: {was} -> {prof.describe(p)}")
            if want["cloud"] != cur["cloud"]:
                store.set_cloud(repo, want["cloud"] == "on")
                lines.append(f"cloud models: {cur['cloud']} -> {want['cloud']}")
        return render(f"<h2>{e(repo)}</h2>" + _changed(lines))

    return [
        Route(SETTINGS_PATH, settings, methods=["GET"]),
        guard.post_route(SETTINGS_PATH, save),
        guard.post_route(REPO_PATH, save_repo),
    ]


def _line(label: str, cur: dict[str, str], want: dict[str, str], key: str) -> str:
    def show(v: str) -> str:
        return repr(v) if v else "(none)"

    return f"{label}: {show(cur[key])} -> {show(want[key])}"


__all__ = [
    "NAV_ENTRY",
    "REPO_PATH",
    "RELOAD_NOTE",
    "SETTINGS_PATH",
    "apply_form",
    "build",
    "form_values",
    "routes",
    "save_form",
]
