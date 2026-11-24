"""The model proxy: one OpenAI-compatible server in front of Ollama and the cloud APIs.

Every model call from any coding agent (OpenCode), the router, the reviewer and the
replays goes through here and becomes one `llm.call` span, one recorder `llm` row (when
a recorder is open for that attempt), usage on the attempt and a spend-hook call.

URL scheme (the prefix carries the context; OpenAI paths follow ``/v1``):

    /a/<attempt_id>/v1/...   writer attempt; span under that attempt, recorder
                             (attempt_id, "attempt"), usage on the attempt, bucket "live".
                             `ModelProxy.base_url(attempt_id)` builds it; OpenCode's
                             provider baseURL points here (#10).
    /x/<attempt_id>/v1/...   the same for an experiment/replay attempt (#25): bucket
                             "experiments". `ModelProxy.base_url(id, bucket="experiments")`.
    /c/<token>/v1/...        a registered context, from `with proxy.context(...) as ctx:`
                             (`ctx.base_url`). It captures the caller's span and ticket at
                             registration, so calls are parented there even though the
                             proxy runs in another thread: the router calls inside
                             `route_span(...)` (#15); the reviewer passes
                             `review_id=` and `attempt_id=` (span under `review`, recorder
                             (attempt_id, "review")) (#17); embeddings (#23); any call
                             costed to `bucket="experiments"` (#25, shadow reviews #26).
                             The token stops working when the block exits (404).
    /v1/...                  no context: a root-level `llm.call`, bucket "live", no usage
                             row. Still traced and still fail-closed.

Endpoints under each prefix: ``POST chat/completions`` (streaming and not),
``POST completions``, ``POST embeddings``, ``GET models`` (answered locally from config).
The request's ``model`` is the config model *name* (or its upstream id); the proxy
rewrites it to the endpoint's upstream id before forwarding.

Fail closed (invariant 7c): an unknown model, a cloud model whose key cannot be resolved,
a cloud endpoint not on https, or a ``local`` endpoint not on loopback is refused with an
OpenAI-style error body and nothing is sent anywhere. The API key is added as
``Authorization: Bearer`` on the upstream request only; it never appears in spans,
recordings, logs or error bodies, and the client's own Authorization header is dropped.

Spend hook (#18 replaces the no-op default). Called twice per call:

    hook(endpoint: ModelEndpoint, cost_usd: float, context: CallContext, *, phase: str)
        -> bool | None

``phase="estimate"`` before anything is sent (``cost_usd`` is a deliberately high
estimate: prompt chars/3 tokens in, ``max_tokens``/``max_completion_tokens`` or 4,096
out). Returning False (or raising) refuses the call with HTTP 429 ``spend_cap_reached``.
``phase="actual"`` after the call, with the real cost; its return value is ignored.
The hook runs for local models too (cost 0) so it sees every call; it runs on the
proxy's event loop thread, so keep it quick and thread-safe.

Usage: with ``store=``, each finished call in an attempt context (``/a/``, ``/x/``, or
``/c/`` with ``attempt_id`` and no ``review_id``) runs ``store.add_attempt_usage``. Pass
``on_usage=`` to receive every call's `CallUsage` as well (the reviewer and #18 use it).

Streaming: upstream bytes are passed to the client unchanged as they arrive and teed to
build the recorded response and token counts. If the client sent no ``stream_options``,
the proxy adds ``{"include_usage": true}`` upstream so real token counts arrive (Ollama
and both cloud APIs support it). A client disconnect or upstream failure mid-stream still
produces the span (error status ``incomplete: ...``) and a recording row
(``stop_reason="incomplete"``), never an unhandled error.

Running: `start()` runs uvicorn on a background thread, bound to 127.0.0.1 (port 0 picks a
free port; `url` says which); `stop()` shuts it down. `proxy.app` is a plain ASGI app, so
tests can drive it with ``httpx.ASGITransport`` and no socket. Upstreams are reached with
one ``httpx.AsyncClient``; pass ``transport=`` to swap the network out (tests use
``tests/fakes/models.py``).
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import anyio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from foreman_inator import tracing
from foreman_inator.config import Config, ConfigError, ModelEndpoint

from . import pricing

log = logging.getLogger(__name__)

Bucket = Literal["live", "experiments"]
Phase = Literal["estimate", "actual"]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_MAX_OUT = 4096
UPSTREAM_PATHS = {"chat/completions", "completions", "embeddings"}


def _is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SpendHook(Protocol):
    def __call__(
        self, endpoint: ModelEndpoint, cost_usd: float, context: CallContext, *, phase: Phase
    ) -> bool | None: ...


def no_spend_hook(
    endpoint: ModelEndpoint, cost_usd: float, context: CallContext, *, phase: Phase
) -> bool:
    """The default: allow everything (#18 supplies the real caps)."""
    return True


@dataclass(frozen=True)
class CallContext:
    """Who a model call belongs to. Built from the URL prefix."""

    kind: Literal["attempt", "registered", "none"]
    attempt_id: int | None = None
    review_id: int | None = None
    bucket: Bucket = "live"
    label: str | None = None  # free text for logs, e.g. "route", "review", "embed"
    prompt_version: str | None = None
    recorder_role: str | None = None  # (attempt_id, role) for get_recorder; None = none
    token: str | None = None
    base_url: str | None = None  # set on contexts returned by ModelProxy.context()
    _captured: contextvars.Context | None = field(default=None, repr=False, compare=False)

    @property
    def counts_toward_attempt(self) -> bool:
        return self.attempt_id is not None and self.review_id is None


@dataclass(frozen=True)
class CallUsage:
    """One finished call, as given to ``on_usage``."""

    context: CallContext
    model: str  # config name
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    complete: bool
    usage_estimated: bool
    price_conservative: bool
    status_code: int


KeyResolver = Callable[[ModelEndpoint], str | None]


def _default_key(endpoint: ModelEndpoint) -> str | None:
    return endpoint.api_key()


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str, type_: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.code, self.message, self.type = status, code, message, type_


def error_response(status: int, code: str, message: str, type_: str = "invalid_request_error"):
    """An OpenAI-style error body."""
    return JSONResponse(
        {"error": {"message": message, "type": type_, "param": None, "code": code}},
        status_code=status,
    )


# --------------------------------------------------------------------------- response teeing


class _Tee:
    """Builds the recorded view (text, tool calls, usage, finish reason) of a response."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.done = False
        self._buf = b""
        self.raw_len = 0

    # streamed (SSE)
    def feed(self, chunk: bytes) -> None:
        self.raw_len += len(chunk)
        self._buf += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buf:
            event, self._buf = self._buf.split(b"\n\n", 1)
            for line in event.split(b"\n"):
                if line.startswith(b"data:"):
                    self._data(line[5:].strip())

    def _data(self, data: bytes) -> None:
        if data == b"[DONE]":
            self.done = True
            return
        try:
            obj = json.loads(data)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        for choice in obj.get("choices") or []:
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                self.text.append(delta["content"])
            if isinstance(choice.get("text"), str):  # legacy completions
                self.text.append(choice["text"])
            for tc in delta.get("tool_calls") or []:
                slot = self.tool_calls.setdefault(
                    tc.get("index", len(self.tool_calls)), {"id": "", "name": "", "args": ""}
                )
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["name"] = fn.get("name") or slot["name"]
                slot["args"] += fn.get("arguments") or ""

    # whole body
    def parse_body(self, body: bytes) -> None:
        self.raw_len = len(body)
        try:
            obj = json.loads(body)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        for i, choice in enumerate(obj.get("choices") or []):
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            msg = choice.get("message") or {}
            if isinstance(msg.get("content"), str):
                self.text.append(msg["content"])
            if isinstance(choice.get("text"), str):
                self.text.append(choice["text"])
            for j, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                self.tool_calls[i * 1000 + j] = {
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "args": fn.get("arguments") or "",
                }
        self.done = True

    def calls(self) -> list[dict[str, Any]]:
        out = []
        for _, tc in sorted(self.tool_calls.items()):
            try:
                args = json.loads(tc["args"]) if tc["args"] else {}
            except ValueError:
                args = {"_raw": tc["args"]}
            out.append({"id": tc["id"], "name": tc["name"] or "?", "input": args})
        return out


# --------------------------------------------------------------------------- the proxy


class ModelProxy:
    """OpenAI-compatible, traced, recorded, priced. See the module docstring."""

    def __init__(
        self,
        config: Config | Iterable[ModelEndpoint],
        *,
        store: Any = None,
        on_usage: Callable[[CallUsage], None] | None = None,
        spend_hook: SpendHook | None = None,
        key_resolver: KeyResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        host: str = DEFAULT_HOST,
        port: int = 0,
        upstream_timeout: float = 600.0,
    ):
        models = config.models if isinstance(config, Config) else tuple(config)
        self.models: dict[str, ModelEndpoint] = {m.name: m for m in models}
        self.store = store
        self.on_usage = on_usage
        self.spend_hook: SpendHook = spend_hook or no_spend_hook
        self.key_resolver = key_resolver or _default_key
        self._transport = transport
        if not _is_loopback(host):
            raise ValueError("the model proxy binds to loopback only")
        self.host = host
        self.port = port
        self.upstream_timeout = upstream_timeout
        self._contexts: dict[str, CallContext] = {}
        self._ctx_lock = threading.Lock()
        self._client: httpx.AsyncClient | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.app = self._build_app()

    # ------------------------------------------------------------------ URLs and contexts

    @property
    def url(self) -> str:
        """``http://127.0.0.1:<port>`` (the real port once started)."""
        return f"http://{self.host}:{self.port}"

    def base_url(self, attempt_id: int, *, bucket: Bucket = "live") -> str:
        """The OpenAI base URL (ends in ``/v1``) for a writer attempt."""
        prefix = "x" if bucket == "experiments" else "a"
        return f"{self.url}/{prefix}/{int(attempt_id)}/v1"

    @contextmanager
    def context(
        self,
        *,
        attempt_id: int | None = None,
        review_id: int | None = None,
        bucket: Bucket = "live",
        label: str | None = None,
        prompt_version: str | None = None,
        recorder_role: str | None = None,
    ) -> Iterator[CallContext]:
        """Register a call context; yields it with ``.base_url`` set. Captures the caller's
        current span and ticket (contextvars), so calls made through ``base_url`` while the
        block is open are parented to the span that was current here (e.g. ``route``).
        Recorder: ``(attempt_id, recorder_role)``; the role defaults to "review" when
        ``review_id`` is given and "attempt" otherwise."""
        token = secrets.token_urlsafe(12)
        if recorder_role is None and attempt_id is not None:
            recorder_role = "review" if review_id is not None else "attempt"
        ctx = CallContext(
            kind="registered",
            attempt_id=attempt_id,
            review_id=review_id,
            bucket=bucket,
            label=label,
            prompt_version=prompt_version,
            recorder_role=recorder_role,
            token=token,
            base_url=f"{self.url}/c/{token}/v1",
            _captured=contextvars.copy_context(),
        )
        with self._ctx_lock:
            self._contexts[token] = ctx
        try:
            yield ctx
        finally:
            with self._ctx_lock:
                self._contexts.pop(token, None)

    def _resolve_context(self, request: Request) -> CallContext:
        kind = request.path_params.get("_kind")
        pv = request.headers.get("x-fman-prompt-version")
        if kind in ("a", "x"):
            ctx = CallContext(
                kind="attempt",
                attempt_id=int(request.path_params["attempt_id"]),
                bucket="experiments" if kind == "x" else "live",
                recorder_role="attempt",
                label="attempt",
            )
        elif kind == "c":
            with self._ctx_lock:
                found = self._contexts.get(request.path_params["token"])
            if found is None:
                raise _Refused(404, "unknown_context", "unknown or expired proxy context")
            ctx = found
        else:
            ctx = CallContext(kind="none")
        return replace(ctx, prompt_version=pv) if pv else ctx

    # ------------------------------------------------------------------ app

    def _build_app(self) -> Starlette:
        methods = ["GET", "POST"]

        def route(path: str, kind: str) -> Route:
            async def endpoint(request: Request):
                request.path_params["_kind"] = kind
                return await self._handle(request)

            return Route(path, endpoint, methods=methods)

        return Starlette(
            routes=[
                route("/a/{attempt_id:int}/v1/{rest:path}", "a"),
                route("/x/{attempt_id:int}/v1/{rest:path}", "x"),
                route("/c/{token}/v1/{rest:path}", "c"),
                route("/v1/{rest:path}", "none"),
            ]
        )

    def _upstream(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self.upstream_timeout, connect=10.0),
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the upstream client (call on the loop that used it)."""
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    # ------------------------------------------------------------------ request handling

    def _endpoint_for(self, requested: Any) -> ModelEndpoint:
        if not isinstance(requested, str) or not requested:
            raise _Refused(400, "model_required", "request body must name a model")
        ep = self.models.get(requested)
        if ep is None:
            by_id = [m for m in self.models.values() if m.model == requested]
            ep = by_id[0] if len(by_id) == 1 else None
        if ep is None:
            raise _Refused(
                404,
                "model_not_found",
                f"model {requested!r} is not configured; refusing to forward it",
            )
        return ep

    def _check_endpoint(self, ep: ModelEndpoint) -> str | None:
        """Fail-closed checks (invariant 7c). Returns the API key (None for local)."""
        url = urlparse(ep.base_url)
        host = url.hostname or ""
        if ep.kind == "local":
            if url.scheme not in ("http", "https") or not _is_loopback(host):
                raise _Refused(
                    403,
                    "local_endpoint_not_loopback",
                    f"model {ep.name!r} is configured as local but its base_url is not on "
                    "this Mac; refusing to send the request",
                    "permission_error",
                )
            return None
        if ep.kind != "cloud":
            raise _Refused(403, "unknown_endpoint_kind", f"model {ep.name!r} has an unknown kind")
        if url.scheme != "https" or not host:
            raise _Refused(
                403,
                "cloud_endpoint_not_https",
                f"cloud model {ep.name!r} must use an https base_url",
                "permission_error",
            )
        try:
            key = self.key_resolver(ep)
        except ConfigError:
            key = None
        except Exception as exc:  # Keychain locked, backend missing, ...
            log.warning("proxy: key lookup for %s failed (%s)", ep.name, type(exc).__name__)
            key = None
        if not key:
            raise _Refused(
                403,
                "cloud_key_missing",
                f"cloud model {ep.name!r} has no API key in the Keychain; add it with "
                f"`fman config set-key <name>`. Nothing was sent.",
                "permission_error",
            )
        return key

    async def _handle(self, request: Request):
        t0_ns = time.time_ns()
        rest = request.path_params.get("rest", "").strip("/")
        try:
            ctx = self._resolve_context(request)
            if rest == "models" and request.method == "GET":
                return self._list_models()
            if rest not in UPSTREAM_PATHS or request.method != "POST":
                raise _Refused(404, "unknown_endpoint", f"no such endpoint: /v1/{rest}")
            raw = await request.body()
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise _Refused(400, "invalid_json", "request body is not valid JSON") from exc
            if not isinstance(body, dict):
                raise _Refused(400, "invalid_json", "request body must be a JSON object")
            ep = self._endpoint_for(body.get("model"))
            key = self._check_endpoint(ep)
            stream = bool(body.get("stream")) and rest != "embeddings"
            estimate = self._estimate(ep, raw, body, rest)
            self._preflight(ep, estimate, ctx)
        except _Refused as r:
            return error_response(r.status, r.code, r.message, r.type)

        upstream_body = dict(body, model=ep.model)
        if stream and "stream_options" not in upstream_body:
            upstream_body["stream_options"] = {"include_usage": True}
        headers = {"content-type": "application/json", "accept-encoding": "identity"}
        if stream:
            headers["accept"] = "text/event-stream"
        if key:
            headers["authorization"] = f"Bearer {key}"
        url = ep.base_url.rstrip("/") + "/" + rest
        call = _Call(self, ctx, ep, rest, body, t0_ns)
        client = self._upstream()
        req = client.build_request("POST", url, json=upstream_body, headers=headers)
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            call.finish(status=502, error=f"upstream unreachable: {type(exc).__name__}")
            return error_response(
                502,
                "upstream_unreachable",
                f"could not reach the upstream for model {ep.name!r} ({type(exc).__name__})",
                "api_error",
            )
        out_headers = {"content-type": resp.headers.get("content-type", "application/json")}
        if not stream or resp.status_code != 200:
            try:
                data = await resp.aread()
            except httpx.HTTPError as exc:
                await resp.aclose()
                call.finish(status=502, error=f"upstream read failed: {type(exc).__name__}")
                return error_response(502, "upstream_read_failed", "upstream failed", "api_error")
            await resp.aclose()
            call.tee.parse_body(data)
            err = None if resp.status_code < 400 else f"upstream HTTP {resp.status_code}"
            call.finish(status=resp.status_code, error=err)
            return Response(data, status_code=resp.status_code, headers=out_headers)
        return _TeeStream(resp, out_headers, call)

    def _list_models(self) -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": m.name, "object": "model", "created": 0, "owned_by": m.kind}
                    for m in self.models.values()
                ],
            }
        )

    def _estimate(self, ep: ModelEndpoint, raw: bytes, body: dict[str, Any], rest: str) -> float:
        tokens_in = pricing.estimate_tokens(raw)
        if rest == "embeddings":
            return pricing.cost_usd(ep, tokens_in, 0)
        out = body.get("max_completion_tokens") or body.get("max_tokens") or DEFAULT_MAX_OUT
        try:
            tokens_out = int(out)
        except (TypeError, ValueError):
            tokens_out = DEFAULT_MAX_OUT
        return pricing.cost_usd(ep, tokens_in, tokens_out)

    def _preflight(self, ep: ModelEndpoint, estimate: float, ctx: CallContext) -> None:
        try:
            allowed = self.spend_hook(ep, estimate, ctx, phase="estimate")
        except Exception as exc:
            log.warning("proxy: spend hook raised %s; refusing (fail closed)", type(exc).__name__)
            allowed = False
        if allowed is False:
            raise _Refused(
                429,
                "spend_cap_reached",
                f"spend cap reached for {ctx.bucket} spend; model {ep.name!r} call refused "
                "before it was sent. Approve more spend with `fman approve-spend`.",
                "insufficient_quota",
            )

    # ------------------------------------------------------------------ lifecycle

    def start(self, timeout: float = 10.0) -> ModelProxy:
        """Run the server on a background thread; returns when it accepts connections."""
        if self._thread is not None:
            return self
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(128)
        self._sock = sock
        self.port = sock.getsockname()[1]
        cfg = uvicorn.Config(
            self.app,
            log_level="warning",
            lifespan="off",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
        self._server = uvicorn.Server(cfg)

        async def serve() -> None:
            assert self._server is not None
            try:
                await self._server.serve(sockets=[sock])
            finally:
                await self.aclose()

        self._thread = threading.Thread(
            target=lambda: asyncio.run(serve()), name="fman-model-proxy", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("model proxy failed to start")
            time.sleep(0.01)
        return self

    def stop(self, timeout: float = 10.0) -> None:
        """Shut the server down and join its thread. Safe to call twice."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
        if self._sock is not None:
            self._sock.close()
        self._server = self._thread = self._sock = None

    def __enter__(self) -> ModelProxy:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


# --------------------------------------------------------------------------- one call


class _Call:
    """Bookkeeping for one upstream call: tee, then span + recording + usage + spend."""

    def __init__(
        self,
        proxy: ModelProxy,
        ctx: CallContext,
        ep: ModelEndpoint,
        rest: str,
        body: dict[str, Any],
        t0_ns: int,
    ):
        self.proxy, self.ctx, self.ep, self.rest, self.body, self.t0_ns = (
            proxy,
            ctx,
            ep,
            rest,
            body,
            t0_ns,
        )
        self.tee = _Tee()
        self._finished = False

    def _prompt_view(self) -> Any:
        if self.rest == "embeddings":
            return self.body.get("input")
        if "messages" in self.body:
            return self.body["messages"]
        return self.body.get("prompt")

    def finish(self, *, status: int, error: str | None = None, complete: bool = True) -> None:
        """Record everything. Never raises (bookkeeping must not break a response)."""
        if self._finished:
            return
        self._finished = True
        try:
            self._finish(status, error, complete)
        except Exception:  # pragma: no cover - defensive
            log.exception("proxy: recording a %s call failed", self.ep.name)

    def _finish(self, status: int, error: str | None, complete: bool) -> None:
        end_ns = time.time_ns()
        latency_ms = (end_ns - self.t0_ns) / 1e6
        tee = self.tee
        text = "".join(tee.text)
        usage = tee.usage or {}
        estimated = False
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens", 0 if self.rest == "embeddings" else None)
        if not isinstance(tokens_in, int):
            tokens_in = pricing.estimate_tokens(json.dumps(self._prompt_view(), default=str))
            estimated = True
        if not isinstance(tokens_out, int):
            tokens_out = pricing.estimate_tokens(text) if text else 0
            estimated = True
        if status >= 400 and tee.usage is None:
            tokens_in = tokens_out = 0  # refused upstream: nothing billed
        price = pricing.price_for(self.ep)
        cost = price.cost(tokens_in, tokens_out)
        ctx = self.ctx

        def emit() -> None:
            tracing.record_llm_call(
                attempt_id=ctx.attempt_id,
                review_id=ctx.review_id,
                model=self.ep.name,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_usd=cost,
                prompt_version=ctx.prompt_version,
                input=self._prompt_view(),
                output=text if self.rest != "embeddings" else None,
                start_time_ns=self.t0_ns,
                end_time_ns=end_ns,
                error=error,
            )

        try:
            if ctx._captured is not None:
                ctx._captured.copy().run(emit)
            else:
                emit()
        except Exception as exc:
            log.warning("proxy: llm.call span failed: %s", type(exc).__name__)

        if self.rest != "embeddings" and ctx.attempt_id is not None and ctx.recorder_role:
            rec = tracing.get_recorder(ctx.attempt_id, ctx.recorder_role)
            if rec is not None:
                if not complete:
                    stop = "incomplete"
                elif error:
                    stop = "error"
                else:
                    stop = {"tool_calls": "tool_use", "length": "max_tokens"}.get(
                        tee.finish_reason or "", "end_turn"
                    )
                try:
                    rec.llm(
                        text=text,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        model=self.ep.name,
                        stop_reason=stop,
                        tool_calls=tee.calls(),
                        request=self.body,
                        latency_ms=latency_ms,
                        cost_usd=cost,
                        prompt_version=ctx.prompt_version,
                    )
                except Exception as exc:
                    log.warning("proxy: recorder row failed: %s", type(exc).__name__)

        store = self.proxy.store
        if store is not None and ctx.counts_toward_attempt and (tokens_in or tokens_out or cost):
            try:
                store.add_attempt_usage(
                    ctx.attempt_id, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost
                )
            except Exception as exc:
                log.warning(
                    "proxy: usage for attempt %s not stored: %s", ctx.attempt_id, exc.__class__
                )

        record = CallUsage(
            context=ctx,
            model=self.ep.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=latency_ms,
            complete=complete,
            usage_estimated=estimated,
            price_conservative=price.conservative,
            status_code=status,
        )
        if self.proxy.on_usage is not None:
            try:
                self.proxy.on_usage(record)
            except Exception as exc:
                log.warning("proxy: on_usage callback failed: %s", type(exc).__name__)
        try:
            self.proxy.spend_hook(self.ep, cost, ctx, phase="actual")
        except Exception as exc:
            log.warning("proxy: spend hook (actual) failed: %s", type(exc).__name__)


class _TeeStream:
    """ASGI response that passes upstream bytes through unchanged while teeing them.

    Watches for ``http.disconnect`` while streaming (and treats a send error as one), so a
    client that goes away mid-stream ends the upstream read and still gets its call
    recorded, marked incomplete."""

    def __init__(self, upstream: httpx.Response, headers: dict[str, str], call: _Call):
        self.upstream, self.headers, self.call = upstream, headers, call

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        state = {"complete": False, "disconnected": False, "error": None}
        try:
            async with anyio.create_task_group() as tg:

                async def watch() -> None:
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            state["disconnected"] = True
                            tg.cancel_scope.cancel()
                            return

                tg.start_soon(watch)
                await self._pump(send, state)
                tg.cancel_scope.cancel()
        except Exception as exc:  # never let a stream failure escape to the server
            state["error"] = state["error"] or f"stream failed: {type(exc).__name__}"
        finally:
            with anyio.CancelScope(shield=True):
                with suppress(Exception):
                    await self.upstream.aclose()
            if state["disconnected"] and not state["complete"]:
                error = "incomplete: client disconnected"
            elif not state["complete"]:
                error = f"incomplete: {state['error'] or 'stream cancelled'}"
            else:
                error = None
            self.call.finish(status=200, error=error, complete=state["complete"])

    async def _pump(self, send: Any, state: dict[str, Any]) -> None:
        header_list = [(k.encode(), v.encode()) for k, v in self.headers.items()]
        header_list.append((b"cache-control", b"no-cache"))
        try:
            await send({"type": "http.response.start", "status": 200, "headers": header_list})
            try:
                async for chunk in self.upstream.aiter_bytes():
                    self.call.tee.feed(chunk)
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            except httpx.HTTPError as exc:
                state["error"] = f"upstream failed mid-stream: {type(exc).__name__}"
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            state["complete"] = True
        except OSError:  # the server reports a gone client this way (ASGI spec >= 2.4)
            state["disconnected"] = True


__all__ = [
    "CallContext",
    "CallUsage",
    "ModelProxy",
    "SpendHook",
    "error_response",
    "no_spend_hook",
]
