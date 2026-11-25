"""The shared POST protection for every metrics page that writes (todo #67, GH #44).

The metrics app binds to loopback and has no login, so any website open in the same
browser can make the browser send a POST to it (cross-site request forgery). Every POST
route is registered through ``FormGuard.post_route``, which lets the handler run only when
all of these hold, and answers 403 (the handler never runs, so nothing changes) otherwise:

1. the ``Host`` header is a loopback name (DNS rebinding: a rebound attacker page sends
   its own name as ``Host``);
2. ``Sec-Fetch-Site``, when the browser sends it, is not ``cross-site``/``same-site``;
3. the ``Origin`` header (or, only when a browser sent none, the ``Referer``'s origin) is
   exactly the page's own origin: ``http://`` plus the ``Host`` header. A missing or
   ``null`` origin is refused: every current browser sends ``Origin`` on a form POST;
4. the body is ``application/x-www-form-urlencoded`` (what a plain HTML form sends; 415
   otherwise) of at most ``MAX_FORM_BYTES`` (413 otherwise);
5. its ``csrf_token`` field equals the guard's token (compared in constant time). The
   token is random per process (``secrets.token_urlsafe``), rendered only into this app's
   own pages by ``hidden_field`` / ``form``; another origin cannot read those pages.

GET routes stay side-effect free; only routes made by ``post_route`` write. Pages that
will reuse this (#73 settings, #74 tiers, #76 activity, #79 retry) take a ``FormGuard``
(``default_guard()`` is the one per-process guard) and build their forms with ``form``.

References (checked 2026-11-25):

* OWASP CSRF Prevention Cheat Sheet, "Verifying Origin With Standard Headers" and
  "Fetch Metadata headers": https://cheatsheetseries.owasp.org/cheatsheets/
  Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
* MDN ``Origin`` (browsers send it on POST): https://developer.mozilla.org/en-US/docs/
  Web/HTTP/Headers/Origin
* MDN ``Sec-Fetch-Site``: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/
  Sec-Fetch-Site
"""

from __future__ import annotations

import hmac
import html
import secrets
from collections.abc import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

TOKEN_FIELD = "csrf_token"
MAX_FORM_BYTES = 64 * 1024
MAX_FIELDS = 100
FORM_TYPE = "application/x-www-form-urlencoded"

#: A POST handler: the request and its parsed form (first value of each field; the
#: token field removed). Runs in the threadpool, so it may block on the database.
FormHandler = Callable[[Request, dict[str, str]], Response]


class Refused(Exception):
    """Why a POST was refused, with the HTTP status to answer."""

    def __init__(self, reason: str, status: int = 403) -> None:
        super().__init__(reason)
        self.status = status


def _is_loopback(host: str) -> bool:
    from foreman_inator.metrics.web import is_loopback

    return is_loopback(host)


def _host_name(host: str) -> str:
    return urlsplit(f"//{host}").hostname or ""


def own_origin(headers: Mapping[str, str]) -> str:
    """``http://<Host>`` when the Host header is a loopback name; raises ``Refused``."""
    host = (headers.get("host") or "").strip().lower()
    if not host or not _is_loopback(_host_name(host)):
        raise Refused("this page accepts changes from localhost only")
    return f"http://{host}"


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def check_origin(headers: Mapping[str, str]) -> None:
    """Steps 1-3 of the module docstring (headers only). Raises ``Refused``."""
    own = own_origin(headers)
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        raise Refused(f"refused a {site} request")
    origin = headers.get("origin")
    if origin is not None:
        got = _origin_of(origin) if origin.strip().lower() != "null" else None
    else:
        referer = headers.get("referer")
        got = _origin_of(referer) if referer else None
    if got is None:
        raise Refused("refused a request with no Origin")
    if got != own:
        raise Refused("refused a request from another origin")


def parse_form(body: bytes, content_type: str) -> dict[str, str]:
    """A urlencoded body's fields (first value wins). Raises ``Refused``. An empty body
    is an empty form (so a POST with nothing in it is refused for its missing token)."""
    if not body:
        return {}
    if content_type.split(";", 1)[0].strip().lower() != FORM_TYPE:
        raise Refused("expected a form post", 415)
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=MAX_FIELDS)
    except (UnicodeDecodeError, ValueError):
        raise Refused("unreadable form", 400) from None
    out: dict[str, str] = {}
    for k, v in pairs:
        out.setdefault(k, v)
    return out


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_FORM_BYTES:
        raise Refused("form too large", 413)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FORM_BYTES:
            raise Refused("form too large", 413)
        chunks.append(chunk)
    return b"".join(chunks)


class FormGuard:
    """One per-process CSRF token plus the checks above. ``token`` is for tests."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or secrets.token_urlsafe(32)

    def hidden_field(self) -> str:
        return f'<input type="hidden" name="{TOKEN_FIELD}" value="{html.escape(self.token)}">'

    def form(self, action: str, inner: str = "", *, button: str, cls: str = "") -> str:
        """A POST form to ``action`` carrying the token. ``inner`` is trusted HTML (escape
        what goes in it); ``action`` and ``button`` are escaped here."""
        klass = f' class="{html.escape(cls)}"' if cls else ""
        return (
            f'<form method="post" action="{html.escape(action)}"{klass}>{self.hidden_field()}'
            f'{inner}<button type="submit">{html.escape(button)}</button></form>'
        )

    def check_token(self, form: Mapping[str, str]) -> None:
        got = form.get(TOKEN_FIELD, "")
        if not got or not hmac.compare_digest(got.encode(), self.token.encode()):
            raise Refused("missing or wrong form token; reload the page and try again")

    async def verify(self, request: Request) -> dict[str, str]:
        """Every check; the form without its token field. Raises ``Refused``."""
        check_origin(request.headers)
        form = parse_form(await _read_body(request), request.headers.get("content-type", ""))
        self.check_token(form)
        form.pop(TOKEN_FIELD, None)
        return form

    def post_route(self, path: str, handler: FormHandler, *, name: str | None = None) -> Route:
        """A ``POST path`` route whose ``handler`` runs only after ``verify`` passed."""

        async def endpoint(request: Request) -> Response:
            try:
                form = await self.verify(request)
            except Refused as exc:
                return PlainTextResponse(f"Refused: {exc}", exc.status)
            return await run_in_threadpool(handler, request, form)

        return Route(path, endpoint, methods=["POST"], name=name)


_DEFAULT = FormGuard()  # made at import: one token per process


def default_guard() -> FormGuard:
    """The per-process guard every page shares."""
    return _DEFAULT


__all__ = [
    "FORM_TYPE",
    "MAX_FORM_BYTES",
    "TOKEN_FIELD",
    "FormGuard",
    "FormHandler",
    "Refused",
    "check_origin",
    "default_guard",
    "own_origin",
    "parse_form",
]
