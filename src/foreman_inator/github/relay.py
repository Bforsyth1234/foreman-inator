"""Webhook relay client: a verified GitHub delivery nudges an immediate poll (todo #33).

The worker needs no public URL: GitHub POSTs webhooks to a relay the user configured
(``relay_url``), and this client holds a server-sent-events (SSE) stream open to it. Each
delivery that arrives is checked against ``X-Hub-Signature-256`` with the App's webhook
secret from the Keychain (``relay_secret_ref``). A delivery that verifies does exactly one
thing: ``nudge(repo)``, which asks the worker to poll that repo now instead of at the next
interval. Nothing in a payload is trusted for a decision: the poller re-reads GitHub and
stays the sole authority, including the allowed-users rule (SPEC 7b). A forged, replayed
or unsigned event can at most cause one extra poll.

No relay configured = no client = nothing changes. Errors never leave ``run``: the client
logs, waits with capped exponential backoff and reconnects; polling continues regardless.

Shapes relied on (rule 12, read 2026-11-25):

- smee.io server (https://github.com/probot/smee.io, ``lib/server.js``): ``GET /<channel>``
  with ``Accept: text/event-stream`` (exactly that value) opens the stream. It writes
  ``event: ready`` and, every 30 s, ``event: ping`` (``data: {}``); a delivery is a message
  with no ``event:`` field whose ``data`` is one JSON object: every request header
  (lowercased by Node) as a top-level key, plus ``body`` (the parsed JSON body), ``query``
  and ``timestamp``. ``POST /<channel>`` is where GitHub delivers.
- smee-client (https://github.com/probot/smee-client, ``index.ts``): forwards
  ``JSON.stringify(data.body)`` as the body and the other keys as headers. The raw bytes
  GitHub signed are therefore not on the wire; we verify against the same compact
  re-serialization (``json.dumps(..., separators=(",", ":"), ensure_ascii=False)``), which
  matches GitHub's compact JSON for ordinary payloads. A self-hosted relay may instead send
  the exact body as a string under ``raw_body`` (docs/relay.md); that is tried first. A
  payload that does not verify either way is dropped (the next poll still sees it).
- GitHub delivery headers
  (https://docs.github.com/en/webhooks/webhook-events-and-payloads#delivery-headers):
  ``X-GitHub-Event``, ``X-GitHub-Delivery`` (GUID), ``X-Hub-Signature-256``.
- Signature (https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries):
  ``sha256=`` + hex HMAC-SHA256 of the UTF-8 body keyed with the webhook secret, compared
  in constant time. Test vector: secret ``It's a Secret to Everybody``, payload
  ``Hello, World!`` -> ``757107ea...3e17``.

Everything relay-shaped lives in this file. Transport, sleep and the secret loader are
injectable for tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("foreman_inator.relay")

#: The Keychain item ``fman setup`` stores the App's webhook secret under.
DEFAULT_SECRET_NAME = "relay-webhook-secret"
SIGNATURE_HEADER = "x-hub-signature-256"
EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"
BACKOFF_START_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 300.0
#: smee pings every 30 s; a stream silent for this long is treated as dead.
READ_TIMEOUT_SECONDS = 90.0
#: GitHub caps payloads at 25 MB; a bigger SSE message is dropped unparsed.
MAX_MESSAGE_BYTES = 26 * 1024 * 1024
#: Events that only check the hook itself.
_IGNORED_EVENTS = frozenset({"ping"})
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SIG_RE = re.compile(r"^sha256=[0-9a-f]{64}$")

Nudge = Callable[[str], None]
SecretLoader = Callable[[], str]


class RelayConfigError(ValueError):
    """The relay URL is not usable (not https, or http to a non-loopback host)."""


def check_relay_url(url: str) -> str:
    """Accept ``https://host/...``, or ``http://`` to a loopback host (a local relay)."""
    parsed = urlparse(url.strip())
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parsed.hostname or not (
        parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
    ):
        raise RelayConfigError(
            f"relay_url must be an https:// URL (http:// only for localhost); got {url!r}"
        )
    return url.strip()


# --------------------------------------------------------------------------- signature


def sign(secret: str | bytes, body: bytes) -> str:
    """``sha256=<hex>`` as GitHub computes it."""
    key = secret.encode() if isinstance(secret, str) else secret
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(secret: str | bytes, body: bytes, header: str | None) -> bool:
    """True when ``header`` is GitHub's signature of ``body`` (constant-time compare)."""
    if not secret or not header or not _SIG_RE.match(header.strip()):
        return False
    return hmac.compare_digest(sign(secret, body), header.strip())


def body_candidates(data: dict[str, Any]) -> list[bytes]:
    """The byte strings a delivery's signature may cover, most exact first."""
    out: list[bytes] = []
    raw = data.get("raw_body")
    if isinstance(raw, str):
        out.append(raw.encode("utf-8"))
    if "body" in data:
        # JSON.stringify equivalent (what smee-client forwards).
        out.append(
            json.dumps(data["body"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
    return out


# --------------------------------------------------------------------------- SSE


@dataclass(frozen=True)
class SSEMessage:
    event: str  # "message" when the stream gave no ``event:`` field
    data: str
    id: str = ""


def parse_sse(lines: Iterable[str], *, max_bytes: int = MAX_MESSAGE_BYTES) -> Iterator[SSEMessage]:
    """The WHATWG event-stream format: ``field: value`` lines, a blank line dispatches,
    ``:`` starts a comment, multiple ``data`` lines join with ``\\n``. An over-long message
    is skipped whole."""
    event, data, ident, size, too_big = "", [], "", 0, False
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data and not too_big:
                yield SSEMessage(event or "message", "\n".join(data), ident)
            event, data, size, too_big = "", [], 0, False
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "data":
            size += len(value) + 1
            if size > max_bytes:
                too_big = True
                data = []
            elif not too_big:
                data.append(value)
        elif name == "event":
            event = value
        elif name == "id" and "\0" not in value:
            ident = value


# --------------------------------------------------------------------------- delivery


@dataclass(frozen=True)
class Delivery:
    """What a verified delivery is allowed to tell us: which repo to poll."""

    repo: str
    event: str
    delivery_id: str


def _header(data: dict[str, Any], name: str) -> str | None:
    for k, v in data.items():
        if isinstance(k, str) and k.lower() == name and isinstance(v, str):
            return v
    return None


def verified_delivery(message: str, secret: str | bytes) -> Delivery | None:
    """Parse one relay message; return its repo only if the signature verifies.

    The repo name comes from the signed payload but is used only to pick which repo to
    poll early; nothing else in the payload is read."""
    try:
        data = json.loads(message)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    signature = _header(data, SIGNATURE_HEADER)
    if not any(verify_signature(secret, b, signature) for b in body_candidates(data)):
        return None
    event = _header(data, EVENT_HEADER) or ""
    if event in _IGNORED_EVENTS:
        return None
    body = data.get("body")
    repo_obj = body.get("repository") if isinstance(body, dict) else None
    repo = repo_obj.get("full_name") if isinstance(repo_obj, dict) else None
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        return None
    return Delivery(repo=repo, event=event, delivery_id=_header(data, DELIVERY_HEADER) or "")


# --------------------------------------------------------------------------- client


class RelayClient:
    """Holds the SSE stream open and calls ``nudge(repo)`` for each verified delivery.

    ``repos`` (``owner/name``) limits nudges to repos the worker works on (compared
    case-insensitively); empty means any. ``run`` blocks until ``stop``; ``start`` runs it
    on a daemon thread."""

    def __init__(
        self,
        url: str,
        secret_loader: SecretLoader,
        nudge: Nudge,
        *,
        repos: Sequence[str] = (),
        transport: httpx.BaseTransport | None = None,
        backoff_start: float = BACKOFF_START_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        self.url = check_relay_url(url)
        self._secret_loader = secret_loader
        self._nudge = nudge
        self._repos = {r.lower(): r for r in repos}
        self._transport = transport
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._read_timeout = read_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.nudges = 0
        self.rejected = 0
        self.connections = 0

    # ---- lifecycle

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="fman-relay", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        """Connect, read, and on any failure back off and reconnect, until ``stop``."""
        delay = self._backoff_start
        while not self._stop.is_set():
            healthy = False
            try:
                healthy = self._connect_once()
            except Exception as exc:  # never crash the worker
                log.warning("relay: %s; reconnecting in %.0fs", _describe(exc), delay)
            if self._stop.is_set():
                break
            if healthy:
                delay = self._backoff_start
            self._stop.wait(delay)
            delay = min(self._backoff_max, delay * 2)

    # ---- one connection

    def _connect_once(self) -> bool:
        """One stream. True when the relay answered with a stream (resets the backoff)."""
        try:
            secret = self._secret_loader()
        except Exception as exc:
            log.warning("relay: webhook secret unavailable (%s); not connecting", _describe(exc))
            return False
        if not secret:
            log.warning("relay: webhook secret is empty; not connecting")
            return False
        timeout = httpx.Timeout(30.0, read=self._read_timeout)
        with (
            httpx.Client(transport=self._transport, timeout=timeout) as http,
            http.stream(
                "GET",
                self.url,
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            ) as resp,
        ):
            if resp.status_code != 200:
                log.warning("relay: %s answered HTTP %d", _host(self.url), resp.status_code)
                return False
            self.connections += 1
            log.info("relay: connected to %s", _host(self.url))
            for msg in parse_sse(self._lines(resp)):
                if self._stop.is_set():
                    break
                self._handle(msg, secret)
            return True

    def _lines(self, resp: httpx.Response) -> Iterator[str]:
        for line in resp.iter_lines():
            if self._stop.is_set():
                return
            yield line
        yield ""  # flush a final message without a trailing blank line

    def _handle(self, msg: SSEMessage, secret: str) -> None:
        if msg.event != "message":  # ready, ping
            return
        delivery = verified_delivery(msg.data, secret)
        if delivery is None:
            self.rejected += 1
            log.info("relay: dropped an event that did not verify (or names no repo)")
            return
        repo = delivery.repo
        if self._repos:
            known = self._repos.get(repo.lower())
            if known is None:
                log.debug("relay: event for %s, not a configured repo", repo)
                return
            repo = known
        self.nudges += 1
        log.info("relay: %s event for %s; polling it now", delivery.event or "?", repo)
        try:
            self._nudge(repo)
        except Exception as exc:
            log.warning("relay: nudge(%s) failed: %s", repo, _describe(exc))


def _host(url: str) -> str:
    """Log the host only: a smee channel path is a bearer secret for reading events."""
    return urlparse(url).hostname or "relay"


def _describe(exc: BaseException) -> str:
    return type(exc).__name__


def keychain_secret_loader(ref: str) -> SecretLoader:
    """Read the webhook secret behind ``ref`` (``keychain:foreman-inator/<name>``) on each
    connect, through the shared Keychain helper (read only)."""

    def load() -> str:
        from foreman_inator.config import resolve_key

        return resolve_key(ref)

    return load


def start_relay(
    relay_url: str | None,
    relay_secret_ref: str | None,
    nudge: Nudge,
    *,
    repos: Sequence[str] = (),
    transport: httpx.BaseTransport | None = None,
) -> RelayClient | None:
    """Start the client when a relay is configured; ``None`` (and nothing started) when
    not, or when the URL is unusable (logged, never raised: polling carries on)."""
    if not relay_url:
        return None
    if not relay_secret_ref:
        log.warning("relay: relay_url is set but relay_secret_ref is not; relay disabled")
        return None
    try:
        client = RelayClient(
            relay_url,
            keychain_secret_loader(relay_secret_ref),
            nudge,
            repos=repos,
            transport=transport,
        )
    except RelayConfigError as exc:
        log.warning("relay disabled: %s", exc)
        return None
    client.start()
    return client


__all__ = [
    "DEFAULT_SECRET_NAME",
    "Delivery",
    "RelayClient",
    "RelayConfigError",
    "SSEMessage",
    "body_candidates",
    "check_relay_url",
    "keychain_secret_loader",
    "parse_sse",
    "sign",
    "start_relay",
    "verified_delivery",
    "verify_signature",
]
