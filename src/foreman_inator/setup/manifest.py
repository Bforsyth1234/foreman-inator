"""Register the user's own GitHub App through GitHub's app-manifest flow.

Shapes relied on (rule 12, read 2026-11-23):
- Flow: POST a form with field ``manifest`` (JSON string) to
  ``https://github.com/settings/apps/new?state=<s>`` (personal account) or
  ``https://github.com/organizations/<org>/settings/apps/new?state=<s>``. After
  "Create GitHub App", GitHub redirects to the manifest's ``redirect_url`` with
  ``?code=<code>&state=<s>``. The whole flow must finish within one hour.
  https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
- Manifest keys: ``name``, ``url`` (required), ``hook_attributes``, ``redirect_url``,
  ``callback_urls``, ``setup_url``, ``description``, ``public``, ``default_events``,
  ``default_permissions``, ``request_oauth_on_install``, ``setup_on_update``. By default we
  send no ``hook_attributes`` and no ``default_events``: the worker polls, and an App may be
  registered with webhooks off and no webhook URL. Only when the user configured a webhook
  relay (todo #33, ``relay_url``) does the manifest gain ``hook_attributes`` (``url``: where
  GitHub POSTs, ``active``: deliver, default true) and ``default_events`` (read 2026-11-25).
  GitHub creates the webhook secret itself and returns it as ``webhook_secret`` from the
  conversion; the manifest cannot set one.
  https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/using-webhooks-with-github-apps
- ``POST /app-manifests/{code}/conversions`` (no auth) -> 201 with ``id``, ``slug``,
  ``owner`` (``login``, ``type``), ``html_url``, ``client_id``, ``client_secret``,
  ``webhook_secret``, ``pem``, ``permissions``.
  https://docs.github.com/en/rest/apps/apps#create-a-github-app-from-a-manifest
- ``GET /installation/repositories`` (installation token) -> ``total_count``,
  ``repositories[].full_name``.
  https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation
- ``GET /app/installations/{id}`` (App JWT) -> ``permissions``, ``repository_selection``
  (``all`` | ``selected``), ``account.login``, ``account.type``, ``html_url`` (read
  2026-11-24). GitHub's install page defaults to "All repositories" (seen live, #56).
  https://docs.github.com/en/rest/apps/apps#get-an-installation-for-the-authenticated-app

Everything GitHub-shaped lives in this file, so a change in the flow touches only it.
The browser opener, the HTTP transport, the clock and sleep are injectable for tests.
"""

from __future__ import annotations

import html
import json
import secrets
import socketserver
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from foreman_inator.config import Config
from foreman_inator.github.app_auth import (
    API_URL,
    AppAuth,
    Installation,
    default_headers,
    raise_for_status,
)
from foreman_inator.github.relay import check_relay_url

GITHUB_URL = "https://github.com"
HOMEPAGE_URL = "https://github.com/Bforsyth1234/foreman-inator"
APP_DESCRIPTION = (
    "Personal foreman-inator worker: turns labeled issues into draft PRs written by "
    "local and cloud models. Runs on the owner's Mac; polls, no webhook."
)
APP_DESCRIPTION_RELAY = (
    "Personal foreman-inator worker: turns labeled issues into draft PRs written by "
    "local and cloud models. Runs on the owner's Mac; polls, and the owner's webhook "
    "relay triggers an early poll."
)

# SPEC "Architecture and GitHub mapping": exactly these, nothing more. ``checks`` and
# ``actions`` (read) were added by #41 to read CI on the PR head and its job logs
# (docs.github.com/en/rest/authentication/permissions-required-for-github-apps, read
# 2026-11-23: list check runs for a ref needs Checks read; download job logs needs
# Actions read).
PERMISSIONS: dict[str, str] = {
    "issues": "write",
    "pull_requests": "write",
    "contents": "write",
    "metadata": "read",
    "checks": "read",
    "actions": "read",
}
#: Permissions added after the first release, with why: an App registered earlier lacks
#: them and the worker skips that step with one warning (#41).
ADDED_PERMISSIONS: dict[str, tuple[str, str]] = {
    "checks": ("read", "read GitHub CI results for the PR head"),
    "actions": ("read", "read the log of a failing Actions job"),
}
_LEVEL = {"read": 1, "write": 2}

#: Events a relay-enabled App subscribes to (todo #33): what the poller reacts to (labels,
#: edits, comments, PR state and reviews). Each is covered by PERMISSIONS: ``issues`` and
#: ``issue_comment`` need Issues, the ``pull_request*`` events need Pull requests
#: (https://docs.github.com/en/webhooks/webhook-events-and-payloads, read 2026-11-25).
#: A delivery only nudges an early poll (github/relay.py); it never decides anything.
RELAY_EVENTS: tuple[str, ...] = (
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
)

CALLBACK_PATH = "/callback"
START_PATH = "/start"
MAX_APP_NAME = 34  # GitHub's limit on App names
CALLBACK_TIMEOUT_SECONDS = 30 * 60.0  # the code itself is only valid for an hour
INSTALL_TIMEOUT_SECONDS = 15 * 60.0
INSTALL_POLL_SECONDS = 3.0

Opener = Callable[[str], Any]
Clock = Callable[[], float]
Sleep = Callable[[float], None]


class ManifestFlowError(RuntimeError):
    """The manifest flow did not complete (timeout, bad state, GitHub refused)."""


def default_app_name() -> str:
    """App names are unique across GitHub, so add a short random suffix."""
    return f"foreman-inator-{secrets.token_hex(3)}"


def build_manifest(
    redirect_url: str, name: str | None = None, *, relay_url: str | None = None
) -> dict[str, Any]:
    """The manifest GitHub registers: private App, SPEC permissions, no webhook.

    With ``relay_url`` (the user's own webhook relay, todo #33) GitHub delivers webhooks
    there: ``hook_attributes`` and ``default_events`` are added, nothing else changes."""
    name = name or default_app_name()
    if len(name) > MAX_APP_NAME:
        raise ValueError(f"App name {name!r} is longer than {MAX_APP_NAME} characters")
    built: dict[str, Any] = {
        "name": name,
        "url": HOMEPAGE_URL,
        "description": APP_DESCRIPTION,
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": dict(PERMISSIONS),
    }
    if relay_url:
        built["description"] = APP_DESCRIPTION_RELAY
        built["hook_attributes"] = {"url": check_relay_url(relay_url), "active": True}
        built["default_events"] = list(RELAY_EVENTS)
    return built


def new_app_url(state: str, org: str | None = None) -> str:
    """Where the manifest form is POSTed (personal account, or an organization)."""
    base = (
        f"{GITHUB_URL}/organizations/{quote(org, safe='')}/settings/apps/new"
        if org
        else f"{GITHUB_URL}/settings/apps/new"
    )
    return f"{base}?state={quote(state, safe='')}"


def install_url(slug: str) -> str:
    """The page where the user picks the repos to install the App on."""
    return f"{GITHUB_URL}/apps/{quote(slug, safe='')}/installations/new"


def start_page(manifest: dict[str, Any], action: str) -> str:
    """A page that POSTs the manifest to GitHub on load (a browser can't be opened to a POST)."""
    value = html.escape(json.dumps(manifest), quote=True)
    target = html.escape(action, quote=True)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>foreman-inator setup</title></head>"
        "<body onload='document.forms[0].submit()'>"
        f"<form method='post' action='{target}'>"
        f"<input type='hidden' name='manifest' value='{value}'>"
        "<p>Sending you to GitHub to create your private foreman-inator App...</p>"
        "<button type='submit'>Continue to GitHub</button>"
        "</form></body></html>"
    )


_DONE_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>foreman-inator</title></head>"
    "<body><p>App created. You can close this tab and return to the terminal.</p></body></html>"
)


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)``, which can block for many
    seconds on macOS (seen on GitHub's macOS runners). A loopback server has no use for it.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class CallbackServer:
    """One-shot server on 127.0.0.1: serves the start page, then waits for ``?code=``."""

    def __init__(
        self,
        *,
        org: str | None = None,
        app_name: str | None = None,
        relay_url: str | None = None,
    ) -> None:
        self.state = secrets.token_urlsafe(24)
        self._code: str | None = None
        self._event = threading.Event()
        self._server = _LoopbackHTTPServer(("127.0.0.1", 0), self._handler())
        host, port = self._server.server_address[:2]
        self.base_url = f"http://127.0.0.1:{port}"
        self.manifest = build_manifest(self.base_url + CALLBACK_PATH, app_name, relay_url=relay_url)
        self.action = new_app_url(self.state, org)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def start_url(self) -> str:
        return self.base_url + START_PATH

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep the terminal quiet
                pass

            def _send(self, status: int, body: str) -> None:
                raw = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path == START_PATH:
                    self._send(200, start_page(outer.manifest, outer.action))
                    return
                if url.path != CALLBACK_PATH:
                    self._send(404, "not found")
                    return
                query = parse_qs(url.query)
                state = query.get("state", [""])[0]
                code = query.get("code", [""])[0]
                if not secrets.compare_digest(state, outer.state) or not code:
                    self._send(400, "state mismatch or missing code; run `fman setup` again")
                    return
                outer._code = code
                outer._event.set()
                self._send(200, _DONE_PAGE)

        return Handler

    def __enter__(self) -> CallbackServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait_for_code(self, timeout: float = CALLBACK_TIMEOUT_SECONDS) -> str:
        if not self._event.wait(timeout):
            raise ManifestFlowError(f"no reply from GitHub within {int(timeout)} s")
        assert self._code is not None
        return self._code


@dataclass(frozen=True)
class AppCredentials:
    """What setup keeps from the conversion response (the client secret is dropped).

    ``webhook_secret`` is kept so setup can store it in the Keychain for the relay
    (todo #33); it is never printed (see ``__repr__``) and is "" when GitHub sent none."""

    app_id: int
    slug: str
    pem: str
    owner_login: str
    owner_type: str  # "User" or "Organization"
    html_url: str
    permissions: dict[str, str]
    webhook_secret: str = ""

    def __repr__(self) -> str:  # never print the key
        return (
            f"AppCredentials(app_id={self.app_id}, slug={self.slug!r}, owner={self.owner_login!r})"
        )


def convert_code(
    code: str,
    *,
    transport: httpx.BaseTransport | None = None,
    base_url: str = API_URL,
) -> AppCredentials:
    """``POST /app-manifests/{code}/conversions``: trade the one-time code for the App."""
    with httpx.Client(transport=transport, timeout=30.0) as http:
        resp = http.post(
            f"{base_url.rstrip('/')}/app-manifests/{quote(code, safe='')}/conversions",
            headers=default_headers(),
        )
    raise_for_status(resp)
    data = resp.json()
    owner = data.get("owner") or {}
    return AppCredentials(
        app_id=int(data["id"]),
        slug=str(data["slug"]),
        pem=str(data["pem"]),
        owner_login=str(owner.get("login") or owner.get("slug") or ""),
        owner_type=str(owner.get("type") or ""),
        html_url=str(data.get("html_url") or ""),
        permissions={str(k): str(v) for k, v in (data.get("permissions") or {}).items()},
        webhook_secret=str(data.get("webhook_secret") or ""),
    )


def wait_for_installations(
    auth: AppAuth,
    *,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
    interval: float = INSTALL_POLL_SECONDS,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> list[Installation]:
    """Poll ``GET /app/installations`` until the user has installed the App somewhere."""
    deadline = clock() + timeout
    while True:
        found = auth.list_installations()
        if found or clock() >= deadline:
            return found
        sleep(interval)


def installation_repos(auth: AppAuth, installation_id: int) -> list[str]:
    """``owner/name`` of every repo the installation can see (paginated)."""
    token, _ = auth.create_installation_token(installation_id)
    headers = default_headers() | {"Authorization": f"Bearer {token}"}
    repos: list[str] = []
    page = 1
    while True:
        resp = auth._http.get(
            f"{auth.base_url}/installation/repositories",
            params={"per_page": 100, "page": page},
            headers=headers,
        )
        raise_for_status(resp)
        data = resp.json()
        batch = [str(r["full_name"]) for r in data.get("repositories", [])]
        repos.extend(batch)
        if not batch or len(repos) >= int(data.get("total_count", len(repos))):
            return repos
        page += 1


def missing_permissions(granted: dict[str, str]) -> dict[str, str]:
    """``PERMISSIONS`` entries ``granted`` lacks or holds at a lower level."""
    return {
        k: v for k, v in PERMISSIONS.items() if _LEVEL.get(granted.get(k, ""), 0) < _LEVEL.get(v, 0)
    }


@dataclass(frozen=True)
class InstallationDetails:
    """What ``GET /app/installations/{id}`` says about one installation (#41, #56)."""

    id: int
    permissions: dict[str, str]
    #: "all" (GitHub's default on the install page) or "selected" (the user picked the
    #: repos one by one); "" when GitHub did not say.
    repository_selection: str = ""
    account: str = ""
    account_type: str = ""  # "User" or "Organization"
    html_url: str = ""  # the installation's settings page (Repository access is there)


def installation_details(auth: AppAuth, installation_id: int) -> InstallationDetails:
    """``GET /app/installations/{id}`` (App JWT). Fields relied on: ``permissions``,
    ``repository_selection`` (``all`` | ``selected``), ``account.login``, ``account.type``,
    ``html_url``. https://docs.github.com/en/rest/apps/apps#get-an-installation-for-the-authenticated-app
    (read 2026-11-23 for #41, 2026-11-24 for #56)."""
    data = auth._request("GET", f"/app/installations/{int(installation_id)}")
    account = data.get("account") or {}
    return InstallationDetails(
        id=int(installation_id),
        permissions={str(k): str(v) for k, v in (data.get("permissions") or {}).items()},
        repository_selection=str(data.get("repository_selection") or ""),
        account=str(account.get("login") or account.get("slug") or ""),
        account_type=str(account.get("type") or ""),
        html_url=str(data.get("html_url") or ""),
    )


def installation_permissions(auth: AppAuth, installation_id: int) -> dict[str, str]:
    """The permissions that installation has granted (see ``installation_details``)."""
    return installation_details(auth, installation_id).permissions


def installation_settings_url(installation_id: int, *, org: str | None = None) -> str:
    """The installation's settings page, where "Repository access" chooses between
    "All repositories" and "Only select repositories". GitHub's own ``html_url`` is
    preferred when known; this is its form for a personal account (``org=None``) or an
    organization."""
    owner = f"/organizations/{quote(org, safe='')}" if org else ""
    return f"{GITHUB_URL}{owner}/settings/installations/{int(installation_id)}"


def repository_access_text(details: Sequence[InstallationDetails | int]) -> str:
    """How to narrow (or widen) which repos an installation can see, on GitHub."""
    urls: list[str] = []
    for d in details:
        if isinstance(d, InstallationDetails):
            org = d.account if d.account_type == "Organization" else None
            urls.append(d.html_url or installation_settings_url(d.id, org=org))
        else:
            urls.append(installation_settings_url(d))
    lines = [
        "To change which repos the App can see, open the installation's settings, under"
        " 'Repository access' choose 'Only select repositories', pick the repos and"
        " 'Save':"
    ]
    lines += [f"  {u}" for u in urls] or [f"  {GITHUB_URL}/settings/installations"]
    if not all(isinstance(d, InstallationDetails) and d.account_type for d in details):
        lines.append(
            "  (an installation on an organization is at"
            f" {GITHUB_URL}/organizations/<org>/settings/installations/<id>)"
        )
    return "\n".join(lines)


def permissions_upgrade_text(
    app_slug: str | None,
    missing: dict[str, str],
    *,
    org: str | None = None,
    installation_ids: Sequence[int] = (),
) -> str:
    """What ``fman setup`` prints for an App registered before a permission was added
    (empty when nothing is missing)."""
    if not missing:
        return ""
    slug = quote(app_slug or "<your-app>", safe="")
    settings = (
        f"{GITHUB_URL}/organizations/{quote(org, safe='')}/settings/apps/{slug}/permissions"
        if org
        else f"{GITHUB_URL}/settings/apps/{slug}/permissions"
    )
    lines = ["Your GitHub App is missing permissions this version of foreman-inator uses:"]
    for name, level in missing.items():
        why = ADDED_PERMISSIONS.get(name, (level, "needed by the worker"))[1]
        label = name.replace("_", " ").capitalize()
        lines.append(f"  - {label}: {level} (to {why})")
    lines += [
        "To add them:",
        f"  1. Open {settings}",
        "  2. Under 'Repository permissions', set each one above to the level shown, then"
        " 'Save changes'.",
        "  3. GitHub asks each installation to accept the new permissions: open the"
        " installation's page and click 'Accept new permissions'"
        + (
            ":"
            if installation_ids
            else f" ({GITHUB_URL}/settings/installations, or your org's settings)."
        ),
    ]
    lines += [f"     {GITHUB_URL}/settings/installations/{int(i)}" for i in installation_ids]
    lines.append(
        "Until then the worker skips the CI step with one warning and PRs still get the AI"
        " review; nothing else changes."
    )
    return "\n".join(lines)


def _merge(old: tuple[Any, ...], new: list[Any]) -> tuple[Any, ...]:
    out = list(old)
    for item in new:
        if item not in out:
            out.append(item)
    return tuple(out)


def apply_app(cfg: Config, creds: AppCredentials, allowed_users: Sequence[str] = ()) -> Config:
    """Record a newly created App. The creating account is allowed unless it is an org."""
    users = list(allowed_users)
    if creds.owner_type != "Organization" and creds.owner_login:
        users.insert(0, creds.owner_login)
    return replace(
        cfg,
        app_id=creds.app_id,
        app_slug=creds.slug,
        allowed_users=_merge(cfg.allowed_users, users),
    )


def add_allowed_users(cfg: Config, users: Sequence[str]) -> Config:
    return replace(cfg, allowed_users=_merge(cfg.allowed_users, list(users)))


def apply_installations(
    cfg: Config, installations: list[Installation], repos: Sequence[str] = ()
) -> Config:
    """Record installation ids (existing entries kept, new ones appended), and append
    ``repos`` when given. Since #56 ``fman setup`` passes no repos here: which repos the
    worker works on is the user's choice (``setup.choose_repos``), never everything the
    installation can see."""
    return replace(
        cfg,
        installation_ids=_merge(cfg.installation_ids, [i.id for i in installations]),
        repos=_merge(cfg.repos, list(repos)),
    )


__all__ = [
    "ADDED_PERMISSIONS",
    "PERMISSIONS",
    "RELAY_EVENTS",
    "AppCredentials",
    "CallbackServer",
    "InstallationDetails",
    "ManifestFlowError",
    "add_allowed_users",
    "apply_app",
    "apply_installations",
    "build_manifest",
    "convert_code",
    "default_app_name",
    "install_url",
    "installation_details",
    "installation_permissions",
    "installation_repos",
    "installation_settings_url",
    "missing_permissions",
    "new_app_url",
    "permissions_upgrade_text",
    "repository_access_text",
    "start_page",
    "wait_for_installations",
]
