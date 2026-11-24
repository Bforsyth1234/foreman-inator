"""Spans for foreman-inator, on top of fde-kit's OpenTelemetry setup.

Span layout (SPEC "Tracing"; the ticket is the root):

    ticket                       ticket.id, ticket.title, ticket.repo, ticket.issue_number
    ├─ route                     route.tier, route.reason, route.matched_tickets
    ├─ attempt                   attempt.id, variant_id, attempt.model, attempt.tier, ...
    │  ├─ llm.call               llm.model_name, llm.prompt_version, llm.token_count.*, ...
    │  ├─ tool.call              tool.name, tool.kind, input.value, output.value, ...
    │  └─ verify                 verify.command, verify.tests_run, verify.tests_failed, ...
    ├─ review                    review.id, attempt.id, review.reviewer_model, review.verdict
    │  └─ review.check           review.check = tests | changed_line_coverage | test_revert
    │                                           | run_app | read_diff
    └─ github                    github.action; a kickback is a `kickback` event + a link to
                                 the attempt span it judges

Every span carries `ticket.id`. Content attributes (input.value, output.value) go through
`fde_kit.tracing.trace_content` (truncated at 4,000 chars).

One ticket, one trace, across process restarts. The tickets table stores only the trace id,
so span ids that must be found again are derived from it: the root `ticket` span's id is
`root_span_id(trace_id)` and each attempt's is `attempt_span_id(trace_id, attempt_id)`.
`ticket_span(ticket_id, trace_id=stored)` reopens a stored trace: it emits no new root, it
makes the original root the parent of everything started inside, so a later attempt joins
the trace it started in, hours or days later.

Providers. `init()` runs fde-kit's `init_tracing` (OTLP/HTTP to the local backend). If no
collector is listening (or tracing is disabled) fde-kit falls back to a no-op tracer; this
module then uses a local SDK provider with no network exporter instead, so trace ids stay
real (they go in the tickets table and the recordings) and nothing stalls. Everything here
gets its tracer from `tracer()`, never from the global provider directly, so tests swap
the provider with `capture_spans()` without touching OpenTelemetry's set-once global.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fde_kit.config import Settings
from fde_kit.tracing import init_tracing, trace_content
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Tracer as SDKTracer
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    Tracer,
)

log = logging.getLogger(__name__)

SERVICE_NAME = "foreman-inator"
CONTENT_LIMIT = 4000
DEFAULT_ENDPOINT = "http://localhost:6006/v1/traces"
REVIEW_CHECKS = ("tests", "changed_line_coverage", "test_revert", "run_app", "read_diff")


# --------------------------------------------------------------------------- provider state


class _ForcedIds(IdGenerator):
    """Random ids, except inside `_force_ids(...)`, where the next ids are the given ones."""

    def __init__(self, base: IdGenerator):
        self.base = base

    def generate_span_id(self) -> int:
        forced = _forced.get()
        if forced is not None and forced[1] is not None:
            return forced[1]
        return self.base.generate_span_id()

    def generate_trace_id(self) -> int:
        forced = _forced.get()
        if forced is not None and forced[0] is not None:
            return forced[0]
        return self.base.generate_trace_id()

    def is_trace_id_random(self) -> bool:
        return True


_forced: ContextVar[tuple[int | None, int | None] | None] = ContextVar("fman_ids", default=None)


@contextmanager
def _force_ids(trace_id: int | None, span_id: int | None) -> Iterator[None]:
    token = _forced.set((trace_id, span_id))
    try:
        yield
    finally:
        _forced.reset(token)


@dataclass
class _State:
    provider: TracerProvider | None = None
    tracer: Tracer | None = None
    initialised: bool = False
    traces_dir: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = _State()


def _local_provider() -> TracerProvider:
    return TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))


def _tracer_for(provider: TracerProvider) -> Tracer:
    t = provider.get_tracer(SERVICE_NAME)
    if isinstance(t, SDKTracer):
        t.id_generator = _ForcedIds(getattr(t, "id_generator", None) or RandomIdGenerator())
    return t


def use_provider(provider: TracerProvider | None) -> TracerProvider | None:
    """Make `provider` the one every span here goes to; returns the previous one.

    None means "nothing configured": the next `tracer()` call creates a local provider.
    Mainly for tests (see `capture_spans`); production code calls `init()`.
    """
    with _state.lock:
        prev = _state.provider
        _state.provider = provider
        _state.tracer = _tracer_for(provider) if provider is not None else None
        return prev


def tracer() -> Tracer:
    """The tracer for every foreman-inator span. Creates a local, export-less provider if
    neither `init()` nor `use_provider()` ran, so ids are always real."""
    t = _state.tracer
    if t is not None:
        return t
    with _state.lock:
        if _state.tracer is None:
            _state.provider = _state.provider or _local_provider()
            _state.tracer = _tracer_for(_state.provider)
        return _state.tracer


def init(
    *,
    endpoint: str | None = None,
    traces_dir: str | Path | None = None,
    enabled: bool = True,
    service_name: str = SERVICE_NAME,
) -> Tracer:
    """Set up tracing once per process; later calls return the same tracer.

    Exports over OTLP/HTTP to `endpoint` (Phoenix's default if None) through fde-kit's
    `init_tracing`. Safe when nothing listens there: fde-kit probes the port (0.5 s) and
    disables export; spans then still get real ids from a local provider. With
    `traces_dir`, every finished span is also appended as one JSON line to
    `<traces_dir>/<ticket_id>/<trace_id>.jsonl` (traces stay on the Mac).
    """
    if _state.initialised:
        return tracer()
    settings = Settings(
        otel_enabled=enabled,
        otel_endpoint=endpoint or DEFAULT_ENDPOINT,
        otel_service_name=service_name,
    )
    try:
        init_tracing(settings)
    except Exception as exc:  # tracing must never take an attempt down
        log.warning("tracing: fde-kit init failed, exporting nothing: %s", exc)
    global_provider = trace.get_tracer_provider()
    provider = global_provider if isinstance(global_provider, TracerProvider) else None
    if provider is None:
        provider = _local_provider()
    if traces_dir is not None:
        provider.add_span_processor(TraceFileProcessor(traces_dir))
        _state.traces_dir = Path(traces_dir)
    use_provider(provider)
    _state.initialised = True
    return tracer()


@contextmanager
def capture_spans(traces_dir: str | Path | None = None) -> Iterator[InMemorySpanExporter]:
    """Route every span to a fresh in-memory exporter for the duration of the block.

    For tests in any file; never touches OpenTelemetry's global provider:

        @pytest.fixture
        def spans():
            with capture_spans() as exporter:
                yield exporter

    `exporter.get_finished_spans()` returns the spans; `span_tree(...)` shows their shape.
    """
    exporter = InMemorySpanExporter()
    provider = _local_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    if traces_dir is not None:
        provider.add_span_processor(TraceFileProcessor(traces_dir))
    prev = use_provider(provider)
    try:
        yield exporter
    finally:
        use_provider(prev)
        provider.shutdown()


class TraceFileProcessor(SpanProcessor):
    """Appends each finished span as one JSON line to `<dir>/<ticket_id>/<trace_id>.jsonl`.
    Spans without `ticket.id` go under `<dir>/_none/`. Errors are logged, never raised."""

    def __init__(self, traces_dir: str | Path):
        self.dir = Path(traces_dir)
        self._lock = threading.Lock()

    def on_end(self, span: ReadableSpan) -> None:
        try:
            ctx = span.get_span_context()
            ticket = (span.attributes or {}).get("ticket.id")
            sub = self.dir / (str(ticket) if ticket is not None else "_none")
            row = json.loads(span.to_json(indent=None))
            with self._lock:
                sub.mkdir(parents=True, exist_ok=True)
                with (sub / f"{trace.format_trace_id(ctx.trace_id)}.jsonl").open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:  # pragma: no cover - disk errors
            log.warning("tracing: could not write span to %s: %s", self.dir, exc)


# --------------------------------------------------------------------------- ids


def _derive(trace_id: str, *parts: object) -> int:
    digest = hashlib.sha256(":".join([trace_id, *map(str, parts)]).encode()).digest()
    return int.from_bytes(digest[:8], "big") or 1


def root_span_id(trace_id: str) -> str:
    """The span id of a ticket's root `ticket` span, as 16 hex chars."""
    return trace.format_span_id(_derive(trace_id, "ticket"))


def attempt_span_id(trace_id: str, attempt_id: int) -> str:
    """The span id of attempt `attempt_id`'s `attempt` span in that trace, as 16 hex chars."""
    return trace.format_span_id(_derive(trace_id, "attempt", attempt_id))


def _remote(trace_id: str, span_id: int) -> SpanContext:
    return SpanContext(
        int(trace_id, 16), span_id, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED)
    )


def ticket_context(trace_id: str) -> Context:
    """An OTel Context whose parent is the stored ticket's root span."""
    return trace.set_span_in_context(
        NonRecordingSpan(_remote(trace_id, _derive(trace_id, "ticket")))
    )


def attempt_link(trace_id: str, attempt_id: int) -> Link:
    """A span link to an attempt's span (used by `github` spans that judge it)."""
    return Link(_remote(trace_id, _derive(trace_id, "attempt", attempt_id)))


# --------------------------------------------------------------------------- ticket


@dataclass(frozen=True)
class TicketTrace:
    ticket_id: int
    trace_id: str
    """32 hex chars; store this in tickets.trace_id."""
    resumed: bool


_ticket: ContextVar[TicketTrace | None] = ContextVar("fman_ticket", default=None)
_parents: dict[str, tuple[Context, int | None]] = {}
_parents_lock = threading.Lock()


def current_ticket() -> TicketTrace | None:
    return _ticket.get()


@contextmanager
def ticket_span(
    ticket_id: int,
    *,
    trace_id: str | None = None,
    title: str | None = None,
    repo: str | None = None,
    issue_number: int | None = None,
) -> Iterator[TicketTrace]:
    """Enter a ticket's trace. Yields a `TicketTrace`; store `.trace_id` in the tickets table.

    `trace_id=None` starts the trace: one root `ticket` span, open for the block. With a
    stored `trace_id` the trace is reopened: no new span, and everything started in the block
    is parented to the original root, so later attempts join the same trace.
    """
    if trace_id is not None:
        trace_id = trace_id.lower()
        info = TicketTrace(ticket_id, trace_id, resumed=True)
        ctx_token = otel_context.attach(ticket_context(trace_id))
        tk_token = _ticket.set(info)
        try:
            yield info
        finally:
            _ticket.reset(tk_token)
            otel_context.detach(ctx_token)
        return
    tid = secrets.randbits(128) or 1
    hex_tid = trace.format_trace_id(tid)
    info = TicketTrace(ticket_id, hex_tid, resumed=False)
    attrs = _clean(
        {
            "ticket.id": ticket_id,
            "ticket.title": title,
            "ticket.repo": repo,
            "ticket.issue_number": issue_number,
        }
    )
    with _force_ids(tid, _derive(hex_tid, "ticket")):
        span = tracer().start_span("ticket", context=Context(), attributes=attrs)
    tk_token = _ticket.set(info)
    try:
        with trace.use_span(span, end_on_exit=True):
            yield info
    finally:
        _ticket.reset(tk_token)


# --------------------------------------------------------------------------- child spans


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


def _with_ticket(attrs: dict[str, Any], ticket_id: int | None = None) -> dict[str, Any]:
    if ticket_id is None:
        info = _ticket.get()
        ticket_id = info.ticket_id if info else None
    return _clean({"ticket.id": ticket_id, **attrs})


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None, **kw: Any) -> Iterator[Span]:
    """Any child span of the current one, with `ticket.id` stamped."""
    with tracer().start_as_current_span(name, attributes=_with_ticket(attributes or {}), **kw) as s:
        yield s


@contextmanager
def route_span(tier: int, reason: str, matched_tickets: Iterable[int] = ()) -> Iterator[Span]:
    with span(
        "route",
        {
            "route.tier": tier,
            "route.reason": reason,
            "route.matched_tickets": list(matched_tickets),
        },
    ) as s:
        yield s


@contextmanager
def _registered(key: str, s: Span, ticket_id: int | None) -> Iterator[None]:
    ctx = trace.set_span_in_context(s)
    with _parents_lock:
        _parents[key] = (ctx, ticket_id)
    try:
        yield
    finally:
        with _parents_lock:
            _parents.pop(key, None)


@contextmanager
def attempt_span(
    attempt_id: int,
    *,
    variant_id: str,
    model: str,
    tier: int,
    trigger: str,
    branch: str | None = None,
    base_commit: str | None = None,
) -> Iterator[Span]:
    """The `attempt` span; call inside `ticket_span`. While open, `record_llm_call` and
    `record_tool_call` for this attempt id (from any thread or task) attach to it."""
    info = _ticket.get()
    parent = trace.get_current_span().get_span_context()
    trace_id = trace.format_trace_id(parent.trace_id) if parent.is_valid else None
    attrs = _with_ticket(
        {
            "attempt.id": attempt_id,
            "variant_id": variant_id,
            "attempt.model": model,
            "attempt.tier": tier,
            "attempt.trigger": trigger,
            "attempt.branch": branch,
            "attempt.base_commit": base_commit,
        }
    )
    sid = _derive(trace_id, "attempt", attempt_id) if trace_id else None
    with _force_ids(None, sid):
        s = tracer().start_span("attempt", attributes=attrs)
    with (
        trace.use_span(s, end_on_exit=True),
        _registered(f"attempt:{attempt_id}", s, info.ticket_id if info else None),
    ):
        yield s


@contextmanager
def review_span(
    review_id: int, *, attempt_id: int, reviewer_model: str, variant_id: str
) -> Iterator[Span]:
    """The `review` span. Set the verdict with `set_verdict(span, verdict)` before exit.
    Reviewer model calls attach to it via `record_llm_call(review_id=...)`."""
    info = _ticket.get()
    attrs = _with_ticket(
        {
            "review.id": review_id,
            "attempt.id": attempt_id,
            "review.reviewer_model": reviewer_model,
            "variant_id": variant_id,
        }
    )
    with (
        tracer().start_as_current_span("review", attributes=attrs) as s,
        _registered(f"review:{review_id}", s, info.ticket_id if info else None),
    ):
        yield s


def set_verdict(review: Span, verdict: str) -> None:
    review.set_attribute("review.verdict", verdict)


@contextmanager
def review_check_span(check: str, **attrs: Any) -> Iterator[Span]:
    """One child of `review` per check; `check` is one of REVIEW_CHECKS."""
    if check not in REVIEW_CHECKS:
        raise ValueError(f"unknown review check {check!r}; expected one of {REVIEW_CHECKS}")
    with span(
        "review.check", {"review.check": check, **{f"check.{k}": v for k, v in attrs.items()}}
    ) as s:
        yield s


@contextmanager
def verify_span(command: str) -> Iterator[Span]:
    """The `verify` span. Report with `set_verify_result(span, ...)`."""
    with span("verify", {"verify.command": command}) as s:
        yield s


def set_verify_result(
    s: Span,
    *,
    passed: bool,
    tests_run: int | None = None,
    tests_failed: int | None = None,
    output: str | None = None,
) -> None:
    s.set_attributes(
        _clean(
            {
                "verify.passed": passed,
                "verify.tests_run": tests_run,
                "verify.tests_failed": tests_failed,
                "output.value": trace_content(output, CONTENT_LIMIT) if output else None,
            }
        )
    )


@contextmanager
def github_span(
    action: str, *, attempt_id: int | None = None, pr_number: int | None = None
) -> Iterator[Span]:
    """A `github` span (`pr_opened`, `pr_updated`, `review_received`, ...). With `attempt_id`
    it links to that attempt's span, so a kickback finds the attempt it judges."""
    links = []
    info = _ticket.get()
    if attempt_id is not None and info is not None:
        links.append(attempt_link(info.trace_id, attempt_id))
    with span(
        "github",
        {"github.action": action, "attempt.id": attempt_id, "github.pr_number": pr_number},
        links=links,
    ) as s:
        yield s


def add_kickback(s: Span, *, attempt_id: int, by: str, tag: str, note: str | None = None) -> None:
    """Record a kickback as a `kickback` event. Pass the attempt span if it is still open,
    otherwise the `github` span of the review that carries it (which links to the attempt)."""
    s.add_event(
        "kickback",
        _clean(
            {
                "attempt.id": attempt_id,
                "kickback.by": by,
                "kickback.tag": tag,
                "kickback.note": trace_content(note, CONTENT_LIMIT) if note else None,
            }
        ),
    )


# --------------------------------------------------------------------------- after-the-fact spans


def _parent_for(attempt_id: int | None, review_id: int | None) -> tuple[Context | None, int | None]:
    key = f"review:{review_id}" if review_id is not None else f"attempt:{attempt_id}"
    with _parents_lock:
        found = _parents.get(key)
    if found is None:
        if attempt_id is not None or review_id is not None:
            log.debug("tracing: no open span for %s; using the current context", key)
        return None, None
    return found


def attempt_context(attempt_id: int) -> Context | None:
    """The Context of attempt `attempt_id`'s open span, or None."""
    return _parent_for(attempt_id, None)[0]


def _emit(
    name: str,
    attrs: dict[str, Any],
    *,
    attempt_id: int | None,
    review_id: int | None,
    start_time_ns: int | None,
    end_time_ns: int | None,
    error: str | None,
) -> SpanContext:
    ctx, ticket_id = _parent_for(attempt_id, review_id)
    s = tracer().start_span(
        name,
        context=ctx,
        attributes=_with_ticket(attrs, ticket_id),
        start_time=start_time_ns,
    )
    if error:
        s.set_status(Status(StatusCode.ERROR, error))
    s.end(end_time=end_time_ns)
    return s.get_span_context()


def record_llm_call(
    *,
    attempt_id: int | None = None,
    review_id: int | None = None,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: float,
    cost_usd: float,
    prompt_version: str | None = None,
    input: Any = None,
    output: Any = None,
    output_file: str | Path | None = None,
    start_time_ns: int | None = None,
    end_time_ns: int | None = None,
    error: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> SpanContext:
    """One finished `llm.call` span under the attempt (or review) with that id, from any
    thread or task. Times are epoch nanoseconds (`time.time_ns()`); default now.
    `attributes`: extra span attributes (None values dropped); they never replace the
    standard ones above."""
    return _emit(
        "llm.call",
        {
            **(attributes or {}),
            "attempt.id": attempt_id,
            "review.id": review_id,
            "llm.model_name": model,
            "llm.prompt_version": prompt_version,
            "llm.token_count.prompt": tokens_in,
            "llm.token_count.completion": tokens_out,
            "llm.token_count.total": tokens_in + tokens_out,
            "llm.latency_ms": float(latency_ms),
            "llm.cost_usd": float(cost_usd),
            "input.value": trace_content(input, CONTENT_LIMIT) if input is not None else None,
            "output.value": trace_content(output, CONTENT_LIMIT) if output is not None else None,
            "output.file": str(output_file) if output_file else None,
        },
        attempt_id=attempt_id,
        review_id=review_id,
        start_time_ns=start_time_ns,
        end_time_ns=end_time_ns,
        error=error,
    )


def record_tool_call(
    *,
    attempt_id: int,
    name: str,
    input: Any,
    output: Any,
    kind: str | None = None,
    is_error: bool = False,
    output_file: str | Path | None = None,
    start_time_ns: int | None = None,
    end_time_ns: int | None = None,
) -> SpanContext:
    """One finished `tool.call` span (`kind`: read, search, edit, shell, ...). Output is
    truncated on the span; `output_file` points at the full text next to the recording."""
    return _emit(
        "tool.call",
        {
            "attempt.id": attempt_id,
            "tool.name": name,
            "tool.kind": kind,
            "tool.is_error": is_error,
            "input.value": trace_content(input, CONTENT_LIMIT),
            "output.value": trace_content(output, CONTENT_LIMIT),
            "output.file": str(output_file) if output_file else None,
        },
        attempt_id=attempt_id,
        review_id=None,
        start_time_ns=start_time_ns,
        end_time_ns=end_time_ns,
        error="tool error" if is_error else None,
    )


# --------------------------------------------------------------------------- inspection


def span_tree(spans: Iterable[ReadableSpan]) -> list[tuple[int, str]]:
    """`(depth, name)` for each span, depth-first, children in start order. Spans whose
    parent is not in `spans` are roots. For tests and the trace view."""
    spans = list(spans)
    ids = {s.context.span_id for s in spans}
    kids: dict[int | None, list[ReadableSpan]] = {}
    for s in spans:
        parent = s.parent.span_id if s.parent is not None and s.parent.span_id in ids else None
        kids.setdefault(parent, []).append(s)
    out: list[tuple[int, str]] = []

    def walk(pid: int | None, depth: int) -> None:
        for s in sorted(kids.get(pid, []), key=lambda x: x.start_time or 0):
            out.append((depth, s.name))
            walk(s.context.span_id, depth + 1)

    walk(None, 0)
    return out
