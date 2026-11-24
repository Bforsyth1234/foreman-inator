"""GitHub App authentication and the shared HTTP plumbing (errors, retry with backoff).

Shapes relied on (rule 12, read 2026-11-23):
- App JWT: RS256, claims ``iat`` (60 s in the past for clock drift), ``exp`` (no more than
  10 minutes ahead), ``iss`` (App ID or Client ID).
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
- ``POST /app/installations/{id}/access_tokens`` -> 201 ``{"token", "expires_at", ...}``;
  tokens last one hour; token length is not fixed (stateless ``ghs_`` format since 2026-04).
  https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
- Rate limits: honour ``retry-after``; else if ``x-ratelimit-remaining`` is 0 wait until
  ``x-ratelimit-reset`` (epoch seconds); else wait at least a minute, then back off
  exponentially, and give up after a fixed number of retries.
  https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"
USER_AGENT = "foreman-inator"

JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60  # under GitHub's 10-minute cap even with a slow clock
EXPIRY_MARGIN_SECONDS = 60  # refresh an installation token 1 minute before it expires

MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 3600.0
SECONDARY_RATE_LIMIT_WAIT = 60.0

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class GitHubError(Exception):
    """A GitHub API call failed (after retries, where retries apply)."""

    def __init__(self, status: int, message: str, method: str = "", url: str = "") -> None:
        super().__init__(f"{method} {url} -> {status}: {message}".strip())
        self.status = status
        self.message = message
        self.method = method
        self.url = url


class GitHubNotFound(GitHubError):
    """404 from GitHub."""


class GitHubRateLimited(GitHubError):
    """Still rate limited after the last retry."""


class ForbiddenOperation(Exception):
    """The worker tried something its invariants forbid (e.g. a branch outside ``fman/``)."""


def default_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }


def _is_rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    if resp.status_code != 403:
        return False
    if "retry-after" in resp.headers or resp.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "rate limit" in resp.text.lower()


def retry_delay(
    resp: httpx.Response, attempt: int, clock: Clock, *, idempotent: bool
) -> float | None:
    """Seconds to wait before retrying ``resp``, or None if it should not be retried."""
    if _is_rate_limited(resp):
        if "retry-after" in resp.headers:
            try:
                return min(max(float(resp.headers["retry-after"]), 0.0), MAX_WAIT_SECONDS)
            except ValueError:
                pass
        if resp.headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in resp.headers:
            try:
                wait = float(resp.headers["x-ratelimit-reset"]) - clock()
                return min(max(wait, 1.0), MAX_WAIT_SECONDS)
            except ValueError:
                pass
        return min(SECONDARY_RATE_LIMIT_WAIT * (2**attempt), MAX_WAIT_SECONDS)
    if resp.status_code >= 500 and idempotent:
        return float(min(2**attempt, 30))
    return None


def send_with_retry(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: Any = None,
    params: dict[str, Any] | None = None,
    clock: Clock = time.time,
    sleep: Sleep = time.sleep,
    idempotent: bool | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> httpx.Response:
    """Send one request, retrying rate limits always and 5xx/network errors when idempotent.

    Non-idempotent writes (POST) are not retried on 5xx: GitHub may have applied them, and a
    retry could post a duplicate comment or review.
    """
    if idempotent is None:
        idempotent = method.upper() in {"GET", "PUT", "PATCH", "DELETE", "HEAD"}
    for attempt in range(max_attempts):
        last = attempt == max_attempts - 1
        try:
            resp = http.request(method, url, headers=headers, json=json, params=params)
        except httpx.TransportError:
            if last or not idempotent:
                raise
            sleep(float(min(2**attempt, 30)))
            continue
        wait = retry_delay(resp, attempt, clock, idempotent=idempotent)
        if wait is None or last:
            if wait is not None and _is_rate_limited(resp):
                raise GitHubRateLimited(resp.status_code, _message(resp), method, url)
            return resp
        sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])
    return resp.text[:500]


def raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    cls = GitHubNotFound if resp.status_code == 404 else GitHubError
    raise cls(resp.status_code, _message(resp), resp.request.method, str(resp.request.url))


def load_private_key(private_key: str | Path) -> str:
    """Accept PEM text or a path to a PEM file."""
    if isinstance(private_key, str) and "-----BEGIN" in private_key:
        return private_key
    return Path(private_key).expanduser().read_text()


@dataclass(frozen=True)
class Installation:
    id: int
    account: str


class AppAuth:
    """Authenticates as the App itself (JWT). Used to mint installation tokens."""

    def __init__(
        self,
        app_id: int | str,
        private_key: str | Path,
        *,
        http: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        base_url: str = API_URL,
        clock: Clock = time.time,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.app_id = str(app_id)
        self._key = load_private_key(private_key)
        self._http = http or httpx.Client(transport=transport, timeout=30.0)
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.sleep = sleep

    def app_jwt(self) -> str:
        now = int(self.clock())
        payload = {
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self._key, algorithm="RS256")

    def _request(self, method: str, path: str, json: Any = None) -> Any:
        headers = default_headers() | {"Authorization": f"Bearer {self.app_jwt()}"}
        resp = send_with_retry(
            self._http,
            method,
            self.base_url + path,
            headers=headers,
            json=json,
            clock=self.clock,
            sleep=self.sleep,
        )
        raise_for_status(resp)
        return resp.json()

    def app_slug(self) -> str:
        """``GET /app``: the App's slug; its bot user is ``<slug>[bot]``."""
        return str(self._request("GET", "/app")["slug"])

    def list_installations(self) -> list[Installation]:
        data = self._request("GET", "/app/installations?per_page=100")
        return [Installation(id=int(i["id"]), account=i["account"]["login"]) for i in data]

    def create_installation_token(self, installation_id: int) -> tuple[str, float]:
        """Returns ``(token, expires_at_epoch_seconds)``."""
        data = self._request("POST", f"/app/installations/{installation_id}/access_tokens")
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        return str(data["token"]), expires


class InstallationAuth:
    """An installation access token, cached until 1 minute before it expires.

    ``token()`` is also what the worktree manager passes to ``git push`` over HTTPS.
    """

    def __init__(self, app: AppAuth, installation_id: int) -> None:
        self.app = app
        self.installation_id = int(installation_id)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def clock(self) -> Clock:
        return self.app.clock

    def token(self) -> str:
        with self._lock:
            if self._token is None or self.clock() >= self._expires_at - EXPIRY_MARGIN_SECONDS:
                self._token, self._expires_at = self.app.create_installation_token(
                    self.installation_id
                )
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
