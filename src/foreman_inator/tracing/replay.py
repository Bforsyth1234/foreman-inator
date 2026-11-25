"""Replay of recorded tickets (todo #28, SPEC "Tracing" → "Recording and replay").

Two of SPEC's three uses of a recording live here; neither makes a model call, opens a
socket or writes to the store. (The third, fork from failure, is #36: ``tracing/fork.py``.
Its results are recordings too, ``<attempt>.fork_<n>.jsonl.gz`` next to the attempt's, so
the timeline and the trace view show them; they are never span-tree or regression input.)

**Playback.** ``replay_spans(recordings_dir, traces_dir, ticket_id)`` rebuilds a ticket's
span tree from its recordings (``<recordings>/<ticket>/<attempt>[.<role>].jsonl[.gz]``,
DONE #5) plus the local span files (``<traces>/<ticket>/<trace_id>.jsonl``):

* The *content* spans, every ``llm.call`` and ``tool.call`` under an ``attempt`` or a
  ``review`` that has a recording, are dropped from the span file and re-made from the
  recording's ``llm`` and ``tool`` rows, in row order, with the attributes the proxy and
  the OpenCode driver put on them (model, tokens, cost, prompt version, tool name/kind,
  error flag, input and output through the same ``trace_content`` truncation).
* The ``attempt`` and ``review`` spans take their identifying attributes from the
  recording's header and result rows (attempt id, model, tier, variant, base commit,
  outcome; reviewer model, verdict).
* Everything a recording does not hold (the ``ticket`` root, ``route`` and its classifier
  call, ``verify``, ``review.check``, ``github`` spans and their kickback events) is the
  *skeleton*, taken from the span file as it is.

Order inside a parent follows SPEC's span layout: in an attempt the model and tool turns
come first, in recording order, then the skeleton children (``verify``); in a review the
check spans come first (checks 1-5), then the reviewer's model calls, which all happen in
check 5. ``diff_trees(replayed, recorded_span_tree(...))`` compares name, parent, order and
the attributes in ``KEY_ATTRIBUTES``; an empty list means the recording reproduces the
trace exactly. With no span file, ``replay_spans`` still works from the recordings alone
(a synthetic root, attempts, reviews and their content).

``timeline(...)`` is the model behind the trace view (``metrics/trace_view.py``): per
recording, the ordered steps (prompt, model turns with tokens and cost, tool calls with
input and bounded output, verify, review checks, the verdict, kickbacks), each with its
``row_index``: the 0-based index into ``RunRecord.rows`` (the header is not a row; for
every row but the result, ``row_index == seq - 1``). A kickback's
``failure_step_index`` (#27) is the 1-based position of the row after the header (the
row's ``seq``; ``failure_label.step_row``), so it marks the step whose
``row_index == failure_step_index - 1`` (reconciled in wave 9, #29).

**Regression.** ``regress(rows)`` takes a ticket's recorded store rows (``TicketRows``:
attempts, kickbacks, reviews, routed tier, the ladder, uncounted attempts, plus the
classifier answer and the review inputs found in the trace and recordings) and asks the
*current* decision code what it would have done at each step:

* ``routing.rules`` (``parse_label`` → ``LABEL_TIERS`` → ``resolve_tier``, or the
  ``tier:N`` override) with the recorded classifier answer → the start tier;
* ``routing.escalation.next_step`` over each prefix of the history, built the way
  ``Worker.history()`` builds it (set-aside and rebase-replaced attempts left out) → the
  next attempt's trigger, tier and model, or the hand-over at the end;
* ``review.checks.hard_findings`` with the recorded check results, then
  ``review.reviewer.parse_verdict`` with the recorded model answer → verdict and tag;
* ``tickets.lifecycle.apply`` over the events the rows imply → the final column.

It returns a ``Regression`` whose ``diffs`` list every decision that differs from what was
recorded. No diffs: the current logic agrees with history.

Known limit: OpenCode's own agent loop cannot be re-executed from a recording (its tools
ran against a worktree that is gone); fork from failure (#36) restarts a fresh session at
the failure commit instead.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fde_kit.record import RunRecord
from fde_kit.tracing import trace_content

from foreman_inator.db.store import Attempt, Kickback, Review, Store, Ticket
from foreman_inator.review import checks as review_checks
from foreman_inator.review.reviewer import MalformedVerdict, parse_verdict
from foreman_inator.routing import escalation, rules
from foreman_inator.tickets import lifecycle
from foreman_inator.tracing.otel import CONTENT_LIMIT

#: Characters of one step's text the timeline keeps; the rest is counted in ``clipped``.
STEP_TEXT_LIMIT = 2000
#: Largest id accepted from outside (a URL, a CLI argument).
MAX_ID_DIGITS = 18

_ID = re.compile(r"[0-9]{1,18}", re.ASCII)
_RECORDING = re.compile(r"([0-9]{1,18})(?:\.([a-z][a-z0-9_]{0,31}))?\.jsonl(\.gz)?", re.ASCII)
_ROLE_ORDER = {"attempt": 0, "review": 1}
#: Role of a fork-from-failure recording (#36): ``fork_<n>``, n = 1, 2, ... per attempt.
FORK_ROLE_PREFIX = "fork_"
_FORK_ROLE = re.compile(r"fork_([1-9][0-9]{0,5})", re.ASCII)


def fork_role(n: int) -> str:
    """The recording role of an attempt's ``n``-th fork (1-based)."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 999_999:
        raise ValueError(f"not a fork number: {n!r}")
    return f"{FORK_ROLE_PREFIX}{n}"


def is_fork_role(role: str | None) -> bool:
    """True for a fork-from-failure recording's role (``fork_<n>``)."""
    return isinstance(role, str) and _FORK_ROLE.fullmatch(role) is not None


def parse_id(text: Any) -> int | None:
    """``text`` as a ticket or attempt id: ASCII digits only, at most 18 of them; else
    None. (``int()`` alone would accept ``" 7"``, ``"+7"``, ``"7_0"`` and non-ASCII
    digits.) Every id that reaches a path here has been through this."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text if text >= 0 else None
    if not isinstance(text, str) or not _ID.fullmatch(text):
        return None
    return int(text)


# =========================================================================== recordings


@dataclass(frozen=True)
class AttemptRecording:
    """One recording file of a ticket: an attempt (``role="attempt"``) or its review."""

    attempt_id: int
    role: str
    path: Path
    record: RunRecord
    partial: bool
    """True for a plain ``.jsonl``: the attempt is running, or its process died."""

    @property
    def header(self) -> dict[str, Any]:
        return self.record.header

    @property
    def result(self) -> dict[str, Any] | None:
        return self.record.result


def _read_record(path: Path) -> RunRecord:
    """``RunRecord.load`` semantics, reading a gzip in memory (no temp file)."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError(f"{path.name}: empty recording")
    header = json.loads(lines[0])
    if not isinstance(header, dict) or header.get("kind") != "header":
        raise ValueError(f"{path.name}: line 1 must be the header row")
    return RunRecord(header=header, rows=[json.loads(ln) for ln in lines[1:]])


def _ticket_dir(root: str | Path, ticket_id: int) -> Path:
    tid = parse_id(ticket_id)
    if tid is None:
        raise ValueError(f"not a ticket id: {ticket_id!r}")
    base = Path(root)
    d = base / str(tid)
    if d.resolve().parent != base.resolve():  # belt and braces; tid is digits only
        raise ValueError(f"ticket directory escapes {base}")
    return d


def recorded_tickets(recordings_dir: str | Path) -> list[int]:
    """Ticket ids that have at least one recording, ascending."""
    root = Path(recordings_dir)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        tid = parse_id(d.name)
        if (
            tid is not None
            and d.is_dir()
            and any(_RECORDING.fullmatch(p.name) for p in d.iterdir())
        ):
            out.append(tid)
    return sorted(out)


def load_ticket(recordings_dir: str | Path, ticket_id: int) -> list[AttemptRecording]:
    """Every recording of a ticket, by attempt id then role (attempt before review). A
    finished ``.jsonl.gz`` wins over a ``.jsonl`` of the same attempt and role; files
    that do not parse are skipped."""
    d = _ticket_dir(recordings_dir, ticket_id)
    if not d.is_dir():
        return []
    found: dict[tuple[int, str], tuple[bool, Path]] = {}
    for p in d.iterdir():
        m = _RECORDING.fullmatch(p.name)
        if m is None or not p.is_file():
            continue
        key = (int(m.group(1)), m.group(2) or "attempt")
        gz = m.group(3) is not None
        if key not in found or (gz and not found[key][0]):
            found[key] = (gz, p)
    out = []
    for (aid, role), (gz, p) in found.items():
        try:
            rec = _read_record(p)
        except (OSError, ValueError, EOFError):
            continue
        out.append(AttemptRecording(aid, role, p, rec, partial=not gz))
    return sorted(out, key=lambda r: (r.attempt_id, _ROLE_ORDER.get(r.role, 9), r.role))


# =========================================================================== span files


@dataclass
class SpanNode:
    """One span, from a span file (``source="recorded"``/``"skeleton"``) or re-made from a
    recording row (``source="replayed"``, ``row_index`` set)."""

    name: str
    span_id: str
    parent_id: str | None
    start: str = ""
    end: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    status: str = "UNSET"
    children: list[SpanNode] = field(default_factory=list)
    source: str = "recorded"
    row_index: int | None = None
    line: int = 0


def load_span_rows(traces_dir: str | Path, ticket_id: int) -> list[dict[str, Any]]:
    """Every span line of a ticket's span files, file by file (name order), line order."""
    d = _ticket_dir(traces_dir, ticket_id)
    if not d.is_dir():
        return []
    rows = []
    for p in sorted(d.glob("*.jsonl")):
        if not p.is_file():
            continue
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(row, dict) and "name" in row:
                    rows.append(row)
    return rows


def _hex(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).lower()
    return s[2:] if s.startswith("0x") else s


def _node(row: dict[str, Any], line: int) -> SpanNode:
    ctx = row.get("context") or {}
    return SpanNode(
        name=str(row.get("name")),
        span_id=_hex(ctx.get("span_id")) or f"line{line}",
        parent_id=_hex(row.get("parent_id")),
        start=str(row.get("start_time") or ""),
        end=str(row.get("end_time") or ""),
        attributes=dict(row.get("attributes") or {}),
        events=list(row.get("events") or []),
        links=[
            _hex((lk.get("context") or {}).get("span_id")) or "" for lk in row.get("links") or []
        ],
        status=str((row.get("status") or {}).get("status_code") or "UNSET"),
        line=line,
    )


def build_tree(nodes: Iterable[SpanNode]) -> list[SpanNode]:
    """Link nodes into trees; spans whose parent is absent are roots. Children are in
    start order (ties: end time, then file line), as ``otel.span_tree`` orders them."""
    nodes = list(nodes)
    by_id = {n.span_id: n for n in nodes}
    for n in nodes:
        n.children = []
    roots = []
    for n in nodes:
        parent = by_id.get(n.parent_id) if n.parent_id else None
        (parent.children if parent is not None and parent is not n else roots).append(n)
    key = lambda n: (n.start, n.end, n.line)  # noqa: E731
    for n in nodes:
        n.children.sort(key=key)
    return sorted(roots, key=key)


def recorded_span_tree(traces_dir: str | Path, ticket_id: int) -> list[SpanNode]:
    """The ticket's span tree exactly as the span files hold it."""
    return build_tree(_node(r, i) for i, r in enumerate(load_span_rows(traces_dir, ticket_id)))


def walk(roots: Sequence[SpanNode]) -> Iterable[tuple[int, SpanNode]]:
    """``(depth, node)`` depth-first in child order."""
    stack = [(0, n) for n in reversed(roots)]
    while stack:
        depth, n = stack.pop()
        yield depth, n
        stack.extend((depth + 1, c) for c in reversed(n.children))


# =========================================================================== playback

#: Attributes compared per span name (the ones a recording reproduces or that identify the
#: span); every span is also compared by name, parent and position.
KEY_ATTRIBUTES: Mapping[str, tuple[str, ...]] = {
    "ticket": ("ticket.id",),
    "route": ("route.tier", "route.source"),
    "attempt": (
        "attempt.id",
        "attempt.model",
        "attempt.tier",
        "variant_id",
        "attempt.base_commit",
        "attempt.outcome",
    ),
    "llm.call": (
        "attempt.id",
        "llm.model_name",
        "llm.prompt_version",
        "llm.token_count.prompt",
        "llm.token_count.completion",
        "llm.cost_usd",
        "output.value",
    ),
    "tool.call": (
        "attempt.id",
        "tool.name",
        "tool.kind",
        "tool.is_error",
        "input.value",
        "output.value",
    ),
    "verify": ("verify.passed",),
    "review": ("attempt.id", "review.reviewer_model", "variant_id", "review.verdict"),
    "review.check": ("review.check",),
    "github": ("github.action", "attempt.id"),
}

_CONTENT = frozenset({"llm.call", "tool.call"})


def _llm_attrs(row: dict[str, Any], owner: dict[str, Any]) -> dict[str, Any]:
    usage = row.get("usage") or {}
    tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    attrs = {
        **owner,
        "llm.model_name": row.get("model"),
        "llm.prompt_version": row.get("prompt_version"),
        "llm.token_count.prompt": tin,
        "llm.token_count.completion": tout,
        "llm.token_count.total": tin + tout,
        "llm.latency_ms": float(row.get("latency_ms") or 0.0),
        "llm.cost_usd": float(row.get("cost_usd") or 0.0),
        "output.value": trace_content(row.get("text") or "", CONTENT_LIMIT),
    }
    return {k: v for k, v in attrs.items() if v is not None}


def _tool_attrs(row: dict[str, Any], owner: dict[str, Any]) -> dict[str, Any]:
    attrs = {
        **owner,
        "tool.name": row.get("name"),
        "tool.kind": row.get("tool_kind"),
        "tool.is_error": bool(row.get("is_error")),
        "input.value": trace_content(row.get("input"), CONTENT_LIMIT),
        "output.value": trace_content(row.get("content") or "", CONTENT_LIMIT),
    }
    return {k: v for k, v in attrs.items() if v is not None}


def _content_nodes(rec: AttemptRecording, parent_id: str, owner: dict[str, Any]) -> list[SpanNode]:
    """The content spans one recording reproduces, in row order. Review check rows are
    ``review.check`` spans' records, not ``tool.call`` spans, so they are not replayed."""
    out = []
    for i, row in enumerate(rec.record.rows):
        kind = row.get("kind")
        if kind == "llm":
            name, attrs = "llm.call", _llm_attrs(row, owner)
        elif kind == "tool" and row.get("tool_kind") != "review_check":
            name, attrs = "tool.call", _tool_attrs(row, owner)
        else:
            continue
        out.append(
            SpanNode(
                name=name,
                span_id=f"replay-{rec.role}-{rec.attempt_id}-{i}",
                parent_id=parent_id,
                attributes=attrs,
                status="ERROR" if (kind == "tool" and row.get("is_error")) else "UNSET",
                source="replayed",
                row_index=i,
            )
        )
    return out


def _attempt_attrs(rec: AttemptRecording) -> dict[str, Any]:
    h, res = rec.header, rec.result or {}
    attrs = {
        "ticket.id": h.get("ticket_id"),
        "attempt.id": h.get("attempt_id"),
        "attempt.model": h.get("model"),
        "attempt.tier": h.get("tier"),
        "variant_id": h.get("variant_id"),
        "attempt.base_commit": h.get("base_commit"),
        "attempt.outcome": res.get("outcome") or None,
    }
    return {k: v for k, v in attrs.items() if v is not None}


def _review_attrs(rec: AttemptRecording) -> dict[str, Any]:
    h, res = rec.header, rec.result or {}
    attrs = {
        "ticket.id": h.get("ticket_id"),
        "attempt.id": h.get("attempt_id"),
        "review.reviewer_model": h.get("model"),
        "variant_id": h.get("variant_id"),
        "review.verdict": res.get("outcome") or None,
    }
    return {k: v for k, v in attrs.items() if v is not None}


def _owner_span(nodes: Iterable[SpanNode], rec: AttemptRecording) -> SpanNode | None:
    """The span a recording belongs to: the ``attempt`` span with its id, or the
    (non-shadow, unless the role says shadow) ``review`` span of that attempt."""
    want = "attempt" if rec.role == "attempt" else "review"
    shadow = rec.role != "review" and rec.role != "attempt"
    found = None
    for n in nodes:
        if n.name != want or n.attributes.get("attempt.id") != rec.attempt_id:
            continue
        if want == "review" and bool(n.attributes.get("review.shadow")) != shadow:
            continue
        found = n  # the last one: a re-run after an interruption replaces the earlier
    return found


def replay_spans(
    recordings_dir: str | Path, traces_dir: str | Path | None, ticket_id: int
) -> list[SpanNode]:
    """Rebuild the ticket's span tree by playing its recordings back into the skeleton
    from its span files (module docstring). No model calls, no writes."""
    recs = [
        r
        for r in load_ticket(recordings_dir, ticket_id)
        if (not r.partial or r.result) and not is_fork_role(r.role)
    ]  # a fork (#36) ran after the fact: it has no span in the ticket's trace
    rows = load_span_rows(traces_dir, ticket_id) if traces_dir is not None else []
    nodes = [_node(r, i) for i, r in enumerate(rows)]
    if not nodes:
        return _replay_without_spans(recs, ticket_id)
    owners: dict[str, tuple[AttemptRecording, SpanNode]] = {}
    for rec in recs:
        span = _owner_span(nodes, rec)
        if span is not None:
            owners[span.span_id] = (rec, span)
    skeleton = [
        n for n in nodes if not (n.name in _CONTENT and n.parent_id in owners)
    ]  # content under a recorded owner is re-made from the recording below
    for n in skeleton:
        n.source = "skeleton"
    roots = build_tree(skeleton)
    for rec, span in owners.values():
        span.source = "replayed"
        span.attributes = {
            **span.attributes,
            **(_attempt_attrs(rec) if span.name == "attempt" else _review_attrs(rec)),
        }
        owner = {"ticket.id": rec.header.get("ticket_id"), "attempt.id": rec.attempt_id}
        content = _content_nodes(rec, span.span_id, owner)
        if span.name == "attempt":
            span.children = [*content, *span.children]
        else:
            span.children = [*span.children, *content]
    return roots


def _replay_without_spans(recs: list[AttemptRecording], ticket_id: int) -> list[SpanNode]:
    """No span file: a synthetic ``ticket`` root with the attempts and reviews."""
    root = SpanNode("ticket", f"replay-ticket-{ticket_id}", None, source="replayed")
    root.attributes = {"ticket.id": ticket_id}
    for rec in recs:
        is_attempt = rec.role == "attempt"
        sid = f"replay-{rec.role}-{rec.attempt_id}"
        node = SpanNode(
            "attempt" if is_attempt else "review",
            sid,
            root.span_id,
            attributes=_attempt_attrs(rec) if is_attempt else _review_attrs(rec),
            source="replayed",
        )
        owner: dict[str, Any] = {"ticket.id": ticket_id}
        if is_attempt:
            owner["attempt.id"] = rec.attempt_id
        node.children = _content_nodes(rec, sid, owner)
        root.children.append(node)
    return [root]


def tree_shape(roots: Sequence[SpanNode]) -> list[tuple[int, str]]:
    """``(depth, name)`` depth-first, like ``otel.span_tree``."""
    return [(d, n.name) for d, n in walk(roots)]


def _signature(roots: Sequence[SpanNode]) -> list[tuple[int, str, str, tuple[Any, ...]]]:
    out = []
    names = {n.span_id: n.name for _, n in walk(roots)}
    for depth, n in walk(roots):
        keys = KEY_ATTRIBUTES.get(n.name, ())
        attrs = tuple((k, n.attributes.get(k)) for k in keys)
        out.append((depth, n.name, names.get(n.parent_id or "", "-"), attrs))
    return out


def diff_trees(replayed: Sequence[SpanNode], recorded: Sequence[SpanNode]) -> list[str]:
    """Differences in name, parent, order and ``KEY_ATTRIBUTES``, position by position
    (depth-first). Empty: identical."""
    a, b = _signature(replayed), _signature(recorded)
    out = []
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x == y:
            continue
        if x is None or y is None:
            what = "replay has an extra span" if y is None else "replay is missing a span"
            s = x or y
            out.append(f"#{i}: {what}: {s[1]} at depth {s[0]} under {s[2]}")
        elif x[:3] != y[:3]:
            out.append(
                f"#{i}: replayed {x[1]} (depth {x[0]}, under {x[2]}), "
                f"recorded {y[1]} (depth {y[0]}, under {y[2]})"
            )
        else:
            bad = [
                f"{k}: {v!r} != {w!r}" for (k, v), (_, w) in zip(x[3], y[3], strict=True) if v != w
            ]
            out.append(f"#{i}: {x[1]}: " + "; ".join(bad))
        if len(out) >= 50:
            out.append("... more differences not shown")
            break
    return out


# =========================================================================== timeline


@dataclass(frozen=True)
class Step:
    """One step of a recording's timeline. Every text field is untrusted (model output,
    tool output, issue text): escape it wherever it is shown."""

    kind: str
    """prompt | model | tool | verify | check | verdict | kickback | result"""
    title: str
    row_index: int | None
    """Index into ``RunRecord.rows`` (None: from the store or the span file)."""
    text: str = ""
    clipped: int = 0
    """Characters of ``text`` left out (``STEP_TEXT_LIMIT``)."""
    input: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    is_error: bool = False
    failure_step: str | None = None
    """Set when a kickback's ``failure_step_index`` points at this step (#27)."""


@dataclass(frozen=True)
class RecordingTimeline:
    attempt_id: int
    role: str
    model: str
    tier: int | None
    variant_id: str | None
    trigger: str | None
    outcome: str | None
    partial: bool
    steps: tuple[Step, ...]
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    @property
    def failure_steps(self) -> tuple[Step, ...]:
        return tuple(s for s in self.steps if s.failure_step)


@dataclass(frozen=True)
class TicketTimeline:
    ticket_id: int
    recordings: tuple[RecordingTimeline, ...]


def _clip(text: Any, limit: int = STEP_TEXT_LIMIT) -> tuple[str, int]:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s, 0
    return s[:limit], len(s) - limit


def _verify_spans(traces_dir: str | Path | None, ticket_id: int) -> dict[int, dict[str, Any]]:
    """Attempt id → the attributes of its ``verify`` span."""
    if traces_dir is None:
        return {}
    rows = load_span_rows(traces_dir, ticket_id)
    attempt_of = {
        _hex((r.get("context") or {}).get("span_id")): (r.get("attributes") or {}).get("attempt.id")
        for r in rows
        if r.get("name") == "attempt"
    }
    out = {}
    for r in rows:
        if r.get("name") == "verify":
            aid = attempt_of.get(_hex(r.get("parent_id")))
            if isinstance(aid, int):
                out[aid] = dict(r.get("attributes") or {})
    return out


def _steps(rec: AttemptRecording, verify: dict[str, Any] | None) -> list[Step]:
    steps: list[Step] = []
    for i, row in enumerate(rec.record.rows):
        kind = row.get("kind")
        if kind == "user":
            text, cut = _clip(row.get("text") or "")
            steps.append(Step("prompt", "Prompt", i, text, cut))
        elif kind == "llm":
            usage = row.get("usage") or {}
            calls = ", ".join(str(c.get("name")) for c in row.get("tool_calls") or [])
            text, cut = _clip(row.get("text") or "")
            title = f"Model turn {row.get('turn', '?')}" + (f" (calls {calls})" if calls else "")
            steps.append(
                Step(
                    "model",
                    title,
                    i,
                    text,
                    cut,
                    tokens_in=int(usage.get("input_tokens") or 0),
                    tokens_out=int(usage.get("output_tokens") or 0),
                    cost_usd=float(row.get("cost_usd") or 0.0),
                    is_error=row.get("stop_reason") in ("error", "incomplete"),
                )
            )
        elif kind == "tool":
            name = str(row.get("name") or "tool")
            text, cut = _clip(row.get("content") or "")
            inp, _ = _clip(row.get("input"), 500)
            if row.get("tool_kind") == "review_check":
                check = name.removeprefix("review.")
                steps.append(Step("check", f"Check: {check}", i, text, cut, input=inp))
            else:
                kind_s = row.get("tool_kind")
                title = f"Tool {name}" + (f" ({kind_s})" if kind_s else "")
                steps.append(
                    Step("tool", title, i, text, cut, input=inp, is_error=bool(row.get("is_error")))
                )
        elif kind == "result":
            outcome = row.get("outcome") or row.get("stopped_reason") or ""
            if rec.role == "attempt" or is_fork_role(rec.role):
                passed = row.get("tests_passed")
                detail = []
                if verify:
                    for k in ("verify.command", "verify.tests_run", "verify.tests_failed"):
                        if verify.get(k) is not None:
                            detail.append(f"{k.removeprefix('verify.')}: {verify[k]}")
                    out = verify.get("output.value")
                    if out:
                        detail.append(str(out))
                text, cut = _clip("\n".join(detail))
                if passed is not None or verify:
                    title = "Verify: " + ("passed" if passed else "failed")
                    steps.append(Step("verify", title, i, text, cut, is_error=passed is False))
                steps.append(Step("result", f"Outcome: {outcome}", i))
            else:
                text, cut = _clip(row.get("output") or "")
                tag = row.get("tag")
                title = f"Verdict: {outcome}" + (f" ({tag})" if tag else "")
                steps.append(Step("verdict", title, i, text, cut, is_error=outcome != "approve"))
    return steps


def timeline(
    recordings_dir: str | Path,
    ticket_id: int,
    *,
    traces_dir: str | Path | None = None,
    rows: TicketRows | None = None,
) -> TicketTimeline:
    """The viewer's model of a ticket (module docstring). With ``rows``, each attempt also
    gets its trigger and kickback steps, and a kickback's ``failure_step_index`` marks the
    step it points at in that attempt's recording."""
    verify = _verify_spans(traces_dir, ticket_id)
    attempts = {a.id: a for a in rows.attempts} if rows else {}
    kicks: dict[int, list[Kickback]] = {}
    for k in rows.kickbacks if rows else ():
        kicks.setdefault(k.attempt_id, []).append(k)
    out = []
    for rec in load_ticket(recordings_dir, ticket_id):
        steps = _steps(rec, verify.get(rec.attempt_id) if rec.role == "attempt" else None)
        if rec.role == "attempt":
            for k in kicks.get(rec.attempt_id, ()):
                if k.failure_step and k.failure_step_index is not None:
                    steps = [
                        replace(s, failure_step=k.failure_step)
                        if s.row_index == k.failure_step_index - 1 and s.kind != "result"
                        else s
                        for s in steps
                    ]
                note, cut = _clip(k.note or "")
                title = f"Kickback by {k.by}" + (f" ({k.tag})" if k.tag else "")
                if k.failure_step:
                    title += f"; failure step: {k.failure_step}"
                steps.append(Step("kickback", title, None, note, cut, is_error=True))
        h, res = rec.header, rec.result or {}
        model_steps = [s for s in steps if s.kind == "model"]
        att = attempts.get(rec.attempt_id)
        out.append(
            RecordingTimeline(
                attempt_id=rec.attempt_id,
                role=rec.role,
                model=str(h.get("model") or ""),
                tier=h.get("tier"),
                variant_id=h.get("variant_id"),
                trigger=_fork_trigger(h)
                if is_fork_role(rec.role)
                else (att.trigger if att is not None and rec.role == "attempt" else None),
                outcome=res.get("outcome") or None,
                partial=rec.partial,
                steps=tuple(steps),
                tokens_in=sum(s.tokens_in or 0 for s in model_steps),
                tokens_out=sum(s.tokens_out or 0 for s in model_steps),
                cost_usd=round(sum(s.cost_usd or 0.0 for s in model_steps), 6),
            )
        )
    return TicketTimeline(ticket_id, tuple(out))


def _fork_trigger(header: dict[str, Any]) -> str:
    """What a fork recording's header says it restarted (#36), for the timeline."""
    out = f"fork of attempt {header.get('attempt_id')}"
    idx = header.get("fork_step_index")
    if idx is not None:
        out += f" at step {idx}"
    if header.get("fork_failure_step"):
        out += f" ({header['fork_failure_step']})"
    changed = [str(header[k]) for k in ("fork_model", "fork_prompt") if header.get(k)]
    if changed:
        out += ", with " + " and ".join(changed)
    return out


# =========================================================================== store rows


@dataclass(frozen=True)
class RouteInput:
    """What the router saw, from the ``route`` span: the classifier's raw answer (the
    ``output.value`` of its ``llm.call``), or the ``tier:N`` override."""

    answer: str | None = None
    override_tier: int | None = None


@dataclass(frozen=True)
class ReviewInput:
    """What the reviewer's verdict policy saw, from the review recording: each check's
    recorded summary (``review.<check>`` tool rows) and the model's answers (llm rows)."""

    checks: Mapping[str, str] = field(default_factory=dict)
    answers: tuple[str, ...] = ()


@dataclass(frozen=True)
class TicketRows:
    """A ticket's recorded store rows plus the decision inputs found next to them."""

    ticket: Ticket
    ladder: escalation.Ladder
    attempts: tuple[Attempt, ...]
    """All attempts, oldest first (including uncounted ones)."""
    kickbacks: tuple[Kickback, ...] = ()
    reviews: tuple[Review, ...] = ()
    """Non-shadow reviews, as ``Worker.history()`` reads them."""
    uncounted: frozenset[int] = frozenset()
    """Set-aside attempts (spend pauses, lock restarts: ``worker:uncounted:<ticket>``)."""
    explore: bool = False
    route: RouteInput | None = None
    review_inputs: Mapping[int, ReviewInput] = field(default_factory=dict)
    """By review id."""

    def counted(self) -> tuple[Attempt, ...]:
        """The attempts ``Worker.history()`` passes to escalation."""
        skip = self.uncounted | lifecycle.rebase_uncounted(self.attempts)
        return tuple(a for a in self.attempts if a.id not in skip)

    def history(self, upto: int | None = None) -> escalation.History:
        """``Worker.history()``'s History, over the first ``upto`` counted attempts."""
        counted = self.counted()
        prefix = counted if upto is None else counted[:upto]
        ids = {a.id for a in prefix}
        return escalation.History(
            prefix,
            tuple(k for k in self.kickbacks if k.attempt_id in ids),
            tuple(r for r in self.reviews if r.attempt_id in ids and not r.shadow),
            routed_tier=self.ticket.routed_tier,
            explore=self.explore,
        )


def _route_input(traces_dir: str | Path | None, ticket_id: int) -> RouteInput | None:
    if traces_dir is None:
        return None
    rows = load_span_rows(traces_dir, ticket_id)
    routes = [r for r in rows if r.get("name") == "route"]
    if not routes:
        return None
    route = routes[0]  # the first route decision is the one the first attempt used
    attrs = route.get("attributes") or {}
    if attrs.get("route.source") == "override":
        tier = attrs.get("route.requested_tier", attrs.get("route.tier"))
        return RouteInput(override_tier=int(tier) if tier is not None else None)
    sid = _hex((route.get("context") or {}).get("span_id"))
    for r in rows:
        if r.get("name") == "llm.call" and _hex(r.get("parent_id")) == sid:
            out = (r.get("attributes") or {}).get("output.value")
            return RouteInput(answer=out if isinstance(out, str) else None)
    return RouteInput(answer=None)


def _review_inputs(
    recordings_dir: str | Path | None, ticket_id: int, reviews: Sequence[Review]
) -> dict[int, ReviewInput]:
    if recordings_dir is None:
        return {}
    by_attempt = {
        r.attempt_id: r for r in load_ticket(recordings_dir, ticket_id) if r.role == "review"
    }
    out = {}
    for rv in reviews:
        rec = by_attempt.get(rv.attempt_id)
        if rec is None or rv.shadow:
            continue
        checks = {
            str(r.get("name", "")).removeprefix("review."): str(r.get("content") or "")
            for r in rec.record.tool_rows()
            if r.get("tool_kind") == "review_check"
        }
        answers = tuple(str(r.get("text") or "") for r in rec.record.llm_rows())
        out[rv.id] = ReviewInput(checks, answers)
    return out


def ticket_rows(
    store: Store,
    ticket_id: int,
    *,
    ladder: Sequence[escalation.Rung],
    recordings_dir: str | Path | None = None,
    traces_dir: str | Path | None = None,
    explore: bool = False,
) -> TicketRows:
    """Read a ticket's rows from the store (reads only; a read-only store is fine), plus
    the route and review inputs from its traces and recordings. ``ladder``: the one in
    force (the store does not keep it; ``fman replay`` passes the current config's)."""
    from foreman_inator.worker_pause import PauseSteps  # the worker's own reader

    ticket = store.get_ticket(ticket_id)
    reviews = tuple(store.list_reviews(ticket_id=ticket_id, shadow=False))
    return TicketRows(
        ticket=ticket,
        ladder=tuple(ladder),
        attempts=tuple(store.list_attempts(ticket_id)),
        kickbacks=tuple(store.list_kickbacks(ticket_id=ticket_id)),
        reviews=reviews,
        uncounted=PauseSteps.uncounted_attempts(SimpleNamespace(store=store), ticket),  # type: ignore[arg-type]
        explore=explore,
        route=_route_input(traces_dir, ticket_id),
        review_inputs=_review_inputs(recordings_dir, ticket_id, reviews),
    )


# --- JSON (the checked-in regression cases) ------------------------------------------


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, tuple | list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


def _row_dict(obj: Any) -> dict[str, Any]:
    return {f.name: _jsonable(getattr(obj, f.name)) for f in fields(obj)}


def _from_dict(cls: type, data: Mapping[str, Any]) -> Any:
    kw = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        if isinstance(v, str) and f.name.endswith("_at"):
            v = datetime.fromisoformat(v)
        elif isinstance(v, list):
            v = tuple(v)
        kw[f.name] = v
    return cls(**kw)


def rows_to_json(rows: TicketRows) -> dict[str, Any]:
    """The store rows of a ticket as JSON (route and review inputs are not included:
    they are re-read from the trace and recordings by ``rows_from_json``)."""
    return {
        "ticket": _row_dict(rows.ticket),
        "ladder": [{"tier": r.tier, "model": r.model} for r in rows.ladder],
        "attempts": [_row_dict(a) for a in rows.attempts],
        "kickbacks": [_row_dict(k) for k in rows.kickbacks],
        "reviews": [_row_dict(r) for r in rows.reviews],
        "uncounted": sorted(rows.uncounted),
        "explore": rows.explore,
    }


def rows_from_json(
    data: Mapping[str, Any],
    *,
    recordings_dir: str | Path | None = None,
    traces_dir: str | Path | None = None,
) -> TicketRows:
    ticket = _from_dict(Ticket, data["ticket"])
    reviews = tuple(_from_dict(Review, r) for r in data.get("reviews", ()))
    return TicketRows(
        ticket=ticket,
        ladder=tuple(escalation.Rung(int(r["tier"]), str(r["model"])) for r in data["ladder"]),
        attempts=tuple(_from_dict(Attempt, a) for a in data.get("attempts", ())),
        kickbacks=tuple(_from_dict(Kickback, k) for k in data.get("kickbacks", ())),
        reviews=reviews,
        uncounted=frozenset(int(i) for i in data.get("uncounted", ())),
        explore=bool(data.get("explore", False)),
        route=_route_input(traces_dir, ticket.id),
        review_inputs=_review_inputs(recordings_dir, ticket.id, reviews),
    )


# =========================================================================== regression


@dataclass(frozen=True)
class DecisionDiff:
    """One decision where the current logic and history disagree."""

    kind: str
    """route | next_attempt | end | review | lifecycle"""
    at: str
    """Where in the ticket's history, e.g. ``after attempt 3``."""
    recorded: str
    current: str

    def line(self) -> str:
        return f"{self.kind} {self.at}: recorded {self.recorded}; current logic {self.current}"


@dataclass(frozen=True)
class Regression:
    ticket_id: int
    decisions: int
    """How many decisions were compared."""
    diffs: tuple[DecisionDiff, ...]

    @property
    def agrees(self) -> bool:
        return not self.diffs

    def lines(self) -> list[str]:
        head = f"ticket {self.ticket_id}: {self.decisions} decisions checked, "
        if self.agrees:
            return [head + "the current logic agrees with history"]
        return [head + f"{len(self.diffs)} differ", *("  " + d.line() for d in self.diffs)]


def _describe(step: escalation.Step | None) -> str:
    if step is None:
        return "nothing to start"
    if isinstance(step, escalation.HandToUser):
        return f"hand to user ({step.flag})"
    return f"{step.trigger} on tier {step.tier} ({step.model})"


def _describe_attempt(a: Attempt) -> str:
    return f"{a.trigger} on tier {a.tier} ({a.model})"


def _route_diffs(rows: TicketRows) -> tuple[int, list[DecisionDiff]]:
    r = rows.route
    if r is None or rows.ticket.routed_tier is None:
        return 0, []
    tiers = frozenset(x.tier for x in rows.ladder)
    if r.override_tier is not None:
        wanted, why = r.override_tier, f"override tier:{r.override_tier}"
    else:
        try:
            label = rules.parse_label(r.answer)
            wanted, why = rules.LABEL_TIERS[label], f"classified {label}"
        except rules.RouterError as exc:
            wanted, why = rules.FALLBACK_TIER, f"fallback ({exc})"
    tier, _note, _has = rules.resolve_tier(wanted, tiers)
    if tier == rows.ticket.routed_tier:
        return 1, []
    return 1, [
        DecisionDiff(
            "route", "at the start", f"tier {rows.ticket.routed_tier}", f"tier {tier} ({why})"
        )
    ]


#: ``flag_reason`` prefixes that escalation's ``HandToUser`` produces.
ESCALATION_FLAGS = frozenset(
    {
        escalation.FLAG_CHECKS_FAILED,
        escalation.FLAG_KICKBACK_CAP,
        escalation.FLAG_INTERRUPTED,
        escalation.FLAG_KICKBACK_WRONG,
        escalation.FLAG_AI_KICKBACK_CAP,
        escalation.FLAG_EXPLORE_FAILED,
        escalation.FLAG_NO_MODEL,
    }
)


def _escalation_diffs(rows: TicketRows) -> tuple[int, list[DecisionDiff]]:
    counted = rows.counted()
    n, out = 0, []
    for i, actual in enumerate(counted):
        if actual.trigger == "rebase":
            continue  # started by lifecycle.START_REBASE (#43), not by escalation
        step = escalation.next_step(rows.history(i), rows.ladder)
        n += 1
        at = "at the start" if i == 0 else f"after attempt {counted[i - 1].id}"
        same = (
            isinstance(step, escalation.Retry)
            and step.trigger == actual.trigger
            and step.tier == actual.tier
            and (step.model is None or step.model == actual.model)
        )
        if not same:
            out.append(DecisionDiff("next_attempt", at, _describe_attempt(actual), _describe(step)))
    if not counted:
        return n, out
    # What happened after the last attempt: accepted / waiting (nothing to start), or a
    # hand-over (the ticket's flag), compared with what next_step says now.
    step = escalation.next_step(rows.history(), rows.ladder)
    n += 1
    flag = (rows.ticket.flag_reason or "").split(":", 1)[0] or None
    if flag not in ESCALATION_FLAGS:
        flag = None  # a pause (spend cap) or another stop: not an escalation decision
    last = counted[-1]
    if last.outcome == "accepted" or (last.ended_at is not None and last.outcome is None):
        recorded = "nothing to start"
    elif flag is not None:
        recorded = f"hand to user ({flag})"
    elif last.ended_at is None:
        recorded = "nothing to start"
    else:
        return n, out  # the ticket is mid-flight: the next attempt has not been recorded yet
    current = _describe(step)
    if isinstance(step, escalation.HandToUser) and flag is not None and step.flag == flag:
        current = recorded
    if current != recorded:
        out.append(DecisionDiff("end", f"after attempt {last.id}", recorded, current))
    return n, out


_SUMMARY_FAILED = {
    "tests": "Tests and build: FAILED",
    "test_revert": "Revert check: FAILED",
    "run_app": "Run the change: FAILED",
}
_UNCOVERED = re.compile(r"New code no test executes: (.+?)\.(?:\s|$)")


def _review_checks(rv: Review, inp: ReviewInput) -> list[review_checks.HardFinding]:
    """Rebuild checks 1-4 from the recorded summaries (falling back to the review row)
    and ask the current ``hard_findings``."""
    s = inp.checks
    tests = cov = revert = run_app = None
    if "tests" in s or rv.tests_passed is not None:
        failed = (
            s["tests"].startswith(_SUMMARY_FAILED["tests"])
            if "tests" in s
            else rv.tests_passed is False
        )
        tests = review_checks.TestsCheck(
            "failed" if failed else "passed", not failed, command="(recorded)"
        )
    if "changed_line_coverage" in s or rv.changed_line_coverage is not None:
        m = _UNCOVERED.search(s.get("changed_line_coverage", ""))
        funcs = tuple(x.strip() for x in m.group(1).split(",")) if m else ()
        cov = review_checks.CoverageCheck(
            "failed" if funcs else "passed", rv.changed_line_coverage, uncovered_functions=funcs
        )
    if "test_revert" in s or rv.tests_nonvacuous is not None:
        if "test_revert" in s:
            text = s["test_revert"]
            nonvac = (
                False
                if text.startswith(_SUMMARY_FAILED["test_revert"])
                else True
                if "(good)" in text
                else None
            )
        else:
            nonvac = rv.tests_nonvacuous
        revert = review_checks.RevertCheck("passed" if nonvac else "unknown", nonvac)
    if "run_app" in s or rv.ran_app is not None:
        regression = s.get("run_app", "").startswith(_SUMMARY_FAILED["run_app"])
        run_app = review_checks.RunAppCheck(
            "failed" if regression else "passed",
            rv.ran_app or "not_runnable",
            command="(recorded)",
            regression=regression,
        )
    return review_checks.hard_findings(tests, cov, revert, run_app)


def _with_recording(rows: TicketRows) -> frozenset[int]:
    """Attempts whose review inputs were found (their review recording exists)."""
    ids = {rv.id: rv.attempt_id for rv in rows.reviews}
    return frozenset(ids[i] for i in rows.review_inputs if i in ids)


def _review_diffs(rows: TicketRows) -> tuple[int, list[DecisionDiff]]:
    n, out = 0, []
    for rv in rows.reviews:
        if rv.shadow:
            continue
        inp = rows.review_inputs.get(rv.id, ReviewInput())
        findings = _review_checks(rv, inp)
        if findings:
            verdict, tag = "request_changes", findings[0].tag
        elif inp.answers:
            try:
                mv = parse_verdict(inp.answers[-1])
                verdict, tag = mv.verdict, mv.tag
            except MalformedVerdict:
                verdict, tag = "handed_to_user", None
        elif rv.verdict == "request_changes" and rv.attempt_id in _with_recording(rows):
            # The checks alone no longer force a change, and no model answer was recorded
            # (the model was not asked then): the current policy would decide otherwise.
            verdict, tag = "ask the model (no hard finding)", None
        else:
            continue  # nothing recorded to decide from (e.g. a light review, no model)
        n += 1
        if (verdict, tag) != (rv.verdict, rv.tag):
            out.append(
                DecisionDiff(
                    "review",
                    f"of attempt {rv.attempt_id}",
                    f"{rv.verdict} ({rv.tag})" if rv.tag else rv.verdict,
                    f"{verdict} ({tag})" if tag else verdict,
                )
            )
    return n, out


def implied_events(rows: TicketRows) -> list[Any]:
    """The lifecycle events the rows imply, in order (the poller's events are not
    stored): the request, then per attempt its start, checks, CI, AI review, the user's
    kickbacks, and finally a merge, a drop or a hand-over."""
    t = rows.ticket
    repo, num = t.repo, t.issue_number
    override = rows.route.override_tier if rows.route else None
    ev: list[Any] = [
        lifecycle.TicketRequested(repo, num, t.title, t.request, "recorded", 0, override)
    ]
    reviews = {r.attempt_id: r for r in rows.reviews if not r.shadow}
    kicks: dict[int, list[Kickback]] = {}
    for k in rows.kickbacks:
        kicks.setdefault(k.attempt_id, []).append(k)
    rebases = 0
    for a in rows.attempts:
        if a.trigger == "rebase":
            ev.append(lifecycle.BaseConflict(repo, num, t.id, t.pr_number or 0, "", rebases))
            rebases += 1
        ev.append(lifecycle.AttemptStarted(t.id, a.trigger))
        if a.ended_at is None or (a.outcome == "superseded" and not a.tests_passed):
            continue
        if not a.tests_passed:
            ev.append(lifecycle.ChecksFailed(t.id))
            continue
        rv = reviews.get(a.id)
        ev.append(lifecycle.ChecksPassed(t.id, review_skip=rv is None))
        if a.ci_status == "failed":
            ev.append(lifecycle.CiResult(repo, num, t.id, a.id, "", "failed"))
            continue
        if rv is not None:
            ev.append(lifecycle.AIReviewed(t.id, rv.verdict))
            if rv.verdict == "request_changes":
                continue
        for k in kicks.get(a.id, ()):
            if k.by == "user":
                ev.append(
                    lifecycle.ReviewSubmitted(
                        repo,
                        num,
                        t.id,
                        t.pr_number or 0,
                        k.id,
                        "recorded",
                        "changes",
                        k.note or "",
                        kickback_tag=k.tag,
                    )
                )
                break
        if a.outcome == "accepted":
            ev.append(
                lifecycle.ReviewSubmitted(
                    repo, num, t.id, t.pr_number or 0, 0, "recorded", "approve", ""
                )
            )
            ev.append(lifecycle.Merged(repo, num, t.id, t.pr_number or 0))
    if t.column == "dropped":
        ev.append(lifecycle.ClosedUnmerged(repo, num, t.id, t.pr_number, "issue"))
    elif t.flag_reason and t.column in lifecycle.OPEN:
        ev.append(lifecycle.HandedToUser(t.id, t.flag_reason))
    return ev


def _lifecycle_diffs(rows: TicketRows) -> tuple[int, list[DecisionDiff]]:
    column: str | None = None
    for i, event in enumerate(implied_events(rows)):
        try:
            column = lifecycle.apply(column, event).column
        except lifecycle.IllegalTransition:
            return i + 1, [
                DecisionDiff(
                    "lifecycle",
                    f"at event {i + 1} ({type(event).__name__})",
                    "a legal transition",
                    f"illegal in column {column}",
                )
            ]
    n = len(implied_events(rows))
    if column != rows.ticket.column:
        return n, [
            DecisionDiff(
                "lifecycle", "at the end", f"column {rows.ticket.column}", f"column {column}"
            )
        ]
    return n, []


def regress(rows: TicketRows) -> Regression:
    """Run the recorded ticket through the current router, escalation, review-policy and
    lifecycle code; list every decision that differs from what was recorded."""
    total, diffs = 0, []
    for part in (_route_diffs, _escalation_diffs, _review_diffs, _lifecycle_diffs):
        n, d = part(rows)
        total += n
        diffs.extend(d)
    return Regression(rows.ticket.id, total, tuple(diffs))


# =========================================================================== one call


@dataclass(frozen=True)
class ReplayReport:
    """What ``fman replay <ticket>`` prints (``lines()``)."""

    ticket_id: int
    timeline: TicketTimeline
    tree_diffs: tuple[str, ...]
    regression: Regression | None

    def lines(self) -> list[str]:
        out = [f"ticket {self.ticket_id}: {len(self.timeline.recordings)} recordings"]
        for r in self.timeline.recordings:
            what = "attempt" if r.role == "attempt" else r.role
            out.append(
                f"  {what} {r.attempt_id}: {r.model} tier {r.tier if r.tier is not None else '-'}"
                f", {len(r.steps)} steps, {r.tokens_in}+{r.tokens_out} tokens, "
                f"${r.cost_usd:.4f}, {r.outcome or ('running' if r.partial else '-')}"
            )
            for s in r.failure_steps:
                out.append(f"    failure step {s.failure_step} at row {s.row_index}: {s.title}")
        if self.tree_diffs:
            out.append(f"span tree: {len(self.tree_diffs)} differences from the trace")
            out.extend("  " + d for d in self.tree_diffs)
        else:
            out.append("span tree: replay matches the trace")
        if self.regression is not None:
            out.extend(self.regression.lines())
        return out


def replay_ticket(
    ticket_id: int,
    *,
    recordings_dir: str | Path,
    traces_dir: str | Path | None,
    store: Store | None = None,
    ladder: Sequence[escalation.Rung] = (),
) -> ReplayReport:
    """Everything replay knows about one ticket, for ``fman replay`` and the trace view."""
    rows = None
    if store is not None:
        rows = ticket_rows(
            store, ticket_id, ladder=ladder, recordings_dir=recordings_dir, traces_dir=traces_dir
        )
    tl = timeline(recordings_dir, ticket_id, traces_dir=traces_dir, rows=rows)
    tree_diffs: list[str] = []
    if traces_dir is not None and load_span_rows(traces_dir, ticket_id):
        tree_diffs = diff_trees(
            replay_spans(recordings_dir, traces_dir, ticket_id),
            recorded_span_tree(traces_dir, ticket_id),
        )
    return ReplayReport(
        ticket_id, tl, tuple(tree_diffs), regress(rows) if rows is not None else None
    )


__all__ = [
    "KEY_ATTRIBUTES",
    "STEP_TEXT_LIMIT",
    "AttemptRecording",
    "FORK_ROLE_PREFIX",
    "DecisionDiff",
    "RecordingTimeline",
    "Regression",
    "ReplayReport",
    "ReviewInput",
    "RouteInput",
    "SpanNode",
    "Step",
    "TicketRows",
    "TicketTimeline",
    "build_tree",
    "diff_trees",
    "fork_role",
    "implied_events",
    "is_fork_role",
    "load_span_rows",
    "load_ticket",
    "parse_id",
    "recorded_span_tree",
    "recorded_tickets",
    "regress",
    "replay_spans",
    "replay_ticket",
    "rows_from_json",
    "rows_to_json",
    "ticket_rows",
    "timeline",
    "tree_shape",
    "walk",
]
