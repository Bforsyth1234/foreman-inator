"""``fman export <ticket> --redact``: one ticket's trace, recordings and store rows, with the
user's work taken out, for a bug report (todo #30; SPEC "Open source and distribution").

Raw traces and recordings contain the user's code (invariant 7c keeps them on the Mac).
An export is the one thing meant to leave the machine, so it is redacted by ALLOW-LIST:
every field has a rule, and a value is kept only when its rule says so. There is no raw
mode: without ``--redact`` the command refuses to run.

**Kept**: structure (row kinds and order, span names, span/trace ids and parent links,
event names), timestamps and durations, token counts, cost, model and variant ids,
prompt versions (``name@hash``), tiers, triggers, outcomes, verdicts, tags, check
statuses, booleans, exit/error flags, tool names and kinds, ids and counts, sizes.

**Replaced** (by a placeholder ``<redacted N chars #hhhhhhhh>``): the issue title and
body, prompts, model output and reasoning, the model request, tool inputs and outputs,
file contents, diffs, commit messages, verify and CI output, review notes and check
details, kickback notes and line comments, clarification Q&A, route and flag reasons,
error messages and stack traces, and every string under a key the allow-list does not
know. ``N`` is the original's length; ``h`` is the first 8 hex digits of
HMAC-SHA256(salt, original) with a random 32-byte salt made for this export, held in
memory only and never written (not in the manifest): equal strings inside one export
get equal placeholders, but nothing can be matched across exports or checked against a
guessed string.

**Pseudonymised** (stable within one export, numbered by first appearance): repos
(``repo-1``), logins (``user-1``), file paths (``path-3.py``; only a short extension is
kept), branches (``branch-1``; the worker's own ``fman/issue-<n>/a<m>`` and
``main``/``master``/``trunk``/``develop`` are kept), commits (``commit-1``), unknown keys
inside unknown values (``key-1``), function names in coverage findings (``fn-1``).

**Unknown keys and shapes**: a string becomes a placeholder; a number is dropped (set to
``null``: a number under a key nobody declared can still carry a port, a line number or a
phone number, and nothing reads it); booleans and ``null`` are kept (one bit); a nested
dict's keys become ``key-N`` and its values follow the same rules. Unparseable lines are
dropped and counted.

Three model answers are *decisions*, and are reduced to the decision instead of a
placeholder so ``fman replay`` still agrees on an export: the router's classifier answer
(kept only as the bare label ``small``/``normal``/``large`` when ``rules.parse_label``
accepts it), the reviewer's verdict JSON (``verdict`` and ``tag`` kept, ``note``
replaced) and the review checks' summaries (the fixed status phrase kept, the rest
replaced, uncovered functions pseudonymised). Span ``input.value``/``output.value`` of a
recorded ``llm.call``/``tool.call`` are rewritten to exactly what replay rebuilds from
the redacted recording, so the replayed tree still matches the trace.

Layout of the export (a ``.tar.gz`` with one top folder ``fman-export-t<id>/``, or a
directory chosen with ``-o DIR``), the same as the worker's home:

    recordings/<ticket>/<attempt>[.<role>].jsonl[.gz]    every recording, crashed ones too
    recordings/<ticket>/<attempt>[.<role>].files/<n>.txt side files (full long outputs)
    traces/<ticket>/<trace_id>.jsonl                     span files
    traces/<ticket>/attempt-<n>-<verify|ci>.log          verify and CI logs
    rows.json                                            the ticket's store rows
    MANIFEST.json                                        version, rules, counts (no salt)

Files under the ticket's folders that match none of these names are not copied (counted
in the manifest). Symlinks are never followed. Sources are opened read-only (the store
with ``mode=ro``) and nothing under them is written. Output files are mode 600 and
directories 700; the default output is ``~/Downloads`` (or ``~``), never the repo or the
foreman-inator home, and an output inside the foreman-inator home or a source folder is
refused.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import tarfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fde_kit.tracing import trace_content

from foreman_inator import __version__
from foreman_inator.db.store import TAGS
from foreman_inator.routing import rules
from foreman_inator.tracing.otel import CONTENT_LIMIT
from foreman_inator.tracing.replay import parse_id

FORMAT = "foreman-inator-export/1"
SALT_BYTES = 32
HASH_HEX = 8

REFUSE_RAW = (
    "fman export only writes redacted exports: recordings and traces contain your code, "
    "prompts and tool output, and an export is meant to leave this Mac. Run it again "
    "with --redact. (There is no raw mode.)"
)


class ExportError(Exception):
    """A refusal or a bad input; the message is for the user."""


# =========================================================================== the redactor

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}", re.ASCII)
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:\-]{0,63}", re.ASCII)
_HEX = re.compile(r"(?:0x)?[0-9a-fA-F]{1,64}", re.ASCII)
_EXT = re.compile(r"\.[a-z0-9]{1,8}", re.ASCII)
_KEY = re.compile(r"[a-z_][a-z0-9_]{0,47}(?:\.[a-z0-9_]{1,47}){0,4}", re.ASCII)
_RUN_ID = re.compile(r"t[0-9]{1,18}-a[0-9]{1,18}(?:-[a-z][a-z_\-]{0,31})?", re.ASCII)
_WORKER_BRANCH = re.compile(r"fman/issue-[0-9]{1,18}/a[0-9]{1,18}", re.ASCII)
_BASE_BRANCHES = frozenset({"main", "master", "trunk", "develop"})
_ACTORS = frozenset({"", "user", "reviewer", "worker", "model", "system", "assistant"})
_STRUCTURAL_FILE = re.compile(r"[0-9]{1,8}-[a-z]{1,16}\.txt|attempt-[0-9]{1,18}-[a-z]{1,16}\.log")

Rule = Callable[["Redactor", Any], Any]


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


class Redactor:
    """Holds one export's salt, pseudonym tables, counts and the span content map."""

    def __init__(self, salt: bytes | None = None) -> None:
        self._salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
        self._pseudo: dict[str, dict[str, str]] = {}
        self.counts: Counter[str] = Counter()
        # trace_content(original) -> what replay rebuilds from the redacted recording
        self.content: dict[str, str] = {}
        self.review_answers = False  # inside a review recording: llm text is a verdict

    # -- primitives

    def digest(self, text: str) -> str:
        raw = text.encode("utf-8", "surrogatepass")
        return hmac.new(self._salt, raw, hashlib.sha256).hexdigest()[:HASH_HEX]

    def text(self, value: Any) -> Any:
        """Any value as a placeholder (``None`` and booleans pass; ``""`` stays ``""``)."""
        if value is None or isinstance(value, bool):
            return value
        if not isinstance(value, str):
            value = _canonical(value)
        if value == "":
            return ""
        self.counts["text"] += 1
        return f"<redacted {len(value)} chars #{self.digest(value)}>"

    def pseudo(self, kind: str, value: Any, *, ext: bool = False) -> Any:
        if value is None or value == "":
            return value
        value = value if isinstance(value, str) else _canonical(value)
        table = self._pseudo.setdefault(kind, {})
        name = table.get(value)
        if name is None:
            name = f"{kind}-{len(table) + 1}"
            if ext:
                suffix = PurePosixPath(value).suffix.lower()
                if _EXT.fullmatch(suffix):
                    name += suffix
            table[value] = name
            self.counts[f"pseudonym.{kind}"] += 1
        return name

    def unknown(self, value: Any) -> Any:
        """The default for anything the allow-list does not name."""
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            self.counts["number_dropped"] += 1
            return None
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list | tuple):
            return [self.unknown(v) for v in value]
        if isinstance(value, dict):
            return {self.pseudo("key", str(k)): self.unknown(v) for k, v in value.items()}
        return self.text(value)

    def apply(self, rule: Any, value: Any) -> Any:
        if isinstance(rule, Schema):
            return rule.apply(self, value)
        return rule(self, value)

    def note_content(self, original: Any, redacted: Any) -> None:
        if original is None:
            return
        self.content.setdefault(
            trace_content(original, CONTENT_LIMIT), trace_content(redacted, CONTENT_LIMIT)
        )

    def pseudonym_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in sorted(self._pseudo.items())}


@dataclass(frozen=True)
class Schema:
    """A dict whose known keys have rules. Unknown keys keep an identifier-shaped name
    (code-defined attribute/field names) with their value redacted by ``unknown``; with
    ``strict_keys`` (model-chosen keys, e.g. tool inputs) the name becomes ``key-N``."""

    rules: Mapping[str, Any]
    strict_keys: bool = False

    def apply(self, r: Redactor, value: Any) -> Any:
        if not isinstance(value, dict):
            return r.unknown(value)
        out: dict[str, Any] = {}
        for k, v in value.items():
            rule = self.rules.get(k) if isinstance(k, str) else None
            if rule is not None:
                out[k] = r.apply(rule, v)
                continue
            r.counts["unknown_key"] += 1
            name = (
                k if (not self.strict_keys and isinstance(k, str) and _KEY.fullmatch(k)) else None
            )
            out[name or r.pseudo("key", str(k))] = r.unknown(v)
        return out


def list_of(rule: Any) -> Rule:
    def f(r: Redactor, v: Any) -> Any:
        if not isinstance(v, list | tuple):
            return r.unknown(v)
        return [r.apply(rule, x) for x in v]

    return f


# -- value rules -------------------------------------------------------------------


def TEXT(r: Redactor, v: Any) -> Any:
    return r.text(v)


def INT(r: Redactor, v: Any) -> Any:
    if v is None or (isinstance(v, int) and not isinstance(v, bool)):
        return v
    return r.unknown(v)


def NUM(r: Redactor, v: Any) -> Any:
    if v is None or (isinstance(v, int | float) and not isinstance(v, bool)):
        return v
    return r.unknown(v)


def BOOL(r: Redactor, v: Any) -> Any:
    if v is None or isinstance(v, bool):
        return v
    return r.unknown(v)


def SCALAR(r: Redactor, v: Any) -> Any:
    """A declared option such as a tool's ``offset`` or ``replaceAll``: numbers and
    booleans kept, anything else redacted."""
    if v is None or isinstance(v, bool | int | float):
        return v
    return r.unknown(v)


def _matching(pattern: re.Pattern[str]) -> Rule:
    def f(r: Redactor, v: Any) -> Any:
        if v is None:
            return v
        if isinstance(v, str) and pattern.fullmatch(v):
            return v
        return r.unknown(v)

    return f


TOKEN = _matching(_TOKEN)  # model / variant / prompt / span names (code or config)

#: OpenCode's built-in tools (opencode.ai/docs/tools) and the reviewer's check rows. A
#: tool name is chosen by the model, so any other name becomes ``tool-N``.
KNOWN_TOOLS = frozenset(
    {
        "bash", "edit", "write", "read", "grep", "glob", "list", "patch", "multiedit",
        "todowrite", "todoread", "task", "webfetch", "websearch", "question", "lsp",
        "invalid", "skill", "apply_patch",
    }
    | {f"review.{c}" for c in ("tests", "changed_line_coverage", "test_revert", "run_app",
                                "read_diff")}
)  # fmt: skip


def TOOL_NAME(r: Redactor, v: Any) -> Any:
    if v is None or (isinstance(v, str) and v in KNOWN_TOOLS):
        return v
    return r.pseudo("tool", v)


def CALL_ID(r: Redactor, v: Any) -> Any:
    """Tool-call ids (model or provider made): ``call-N``, the same in the model's call
    and the tool row that answers it."""
    return r.pseudo("call", v)


CODE = _matching(_CODE)  # enum values: outcome, verdict, trigger, tag, status, ...
HEX = _matching(_HEX)  # trace and span ids
RUN_ID = _matching(_RUN_ID)


def TIME(r: Redactor, v: Any) -> Any:
    if v is None:
        return v
    if isinstance(v, str):
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            return v
        except ValueError:
            pass
    r.counts["time_dropped"] += 1
    return None  # loaders parse *_at fields; a placeholder would break them


def REPO(r: Redactor, v: Any) -> Any:
    return r.pseudo("repo", v)


def USER(r: Redactor, v: Any) -> Any:
    if v is None or (isinstance(v, str) and v in _ACTORS):
        return v
    return r.pseudo("user", v)


def COMMIT(r: Redactor, v: Any) -> Any:
    return r.pseudo("commit", v)


def BRANCH(r: Redactor, v: Any) -> Any:
    if v is None or (isinstance(v, str) and (v in _BASE_BRANCHES or _WORKER_BRANCH.fullmatch(v))):
        return v
    return r.pseudo("branch", v)


def PATH(r: Redactor, v: Any) -> Any:
    if isinstance(v, list | tuple):
        return [PATH(r, x) for x in v]
    return r.pseudo("path", v, ext=True)


def FILEREF(r: Redactor, v: Any) -> Any:
    """A path to one of the worker's own files (side file, verify or CI log): only its
    structural basename is kept, never the directories (they hold the user's home)."""
    if isinstance(v, str):
        base = PurePosixPath(v).name
        if _STRUCTURAL_FILE.fullmatch(base):
            return base
    return PATH(r, v)


def FLAG(r: Redactor, v: Any) -> Any:
    """``flag_reason``: the code before the first ``:`` is kept (replay reads it)."""
    if not isinstance(v, str):
        return r.unknown(v)
    code, sep, rest = v.partition(":")
    if not _CODE.fullmatch(code):
        return r.text(v)
    return code + (sep + " " + r.text(rest.strip()) if sep and rest.strip() else sep)


def CONTENT(r: Redactor, v: Any) -> Any:
    """A span's ``input.value``/``output.value``: what replay rebuilds from the redacted
    recording when it came from one, a placeholder otherwise."""
    if isinstance(v, str) and v in r.content:
        r.counts["text"] += 1
        return r.content[v]
    return r.text(v)


def ROUTE_ANSWER(r: Redactor, v: Any) -> Any:
    try:
        return rules.parse_label(v)
    except Exception:
        return r.text(v)


_VERDICT_KEYS = frozenset({"verdict", "tag", "note"})
_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def VERDICT_ANSWER(r: Redactor, v: Any) -> Any:
    """A reviewer's answer: the verdict JSON with its note replaced, when it is one."""
    if not isinstance(v, str):
        return r.unknown(v)
    raw = v.strip()
    m = _FENCE.fullmatch(raw)
    if m:
        raw = m.group(1).strip()
    try:
        obj = json.loads(raw)
    except ValueError:
        return r.text(v)
    if not isinstance(obj, dict) or set(obj) != _VERDICT_KEYS:
        return r.text(v)
    verdict, tag = obj["verdict"], obj["tag"]
    if verdict not in ("approve", "request_changes") or (tag is not None and tag not in TAGS):
        return r.text(v)
    r.counts["text"] += 1
    return json.dumps({"verdict": verdict, "tag": tag, "note": r.text(obj["note"]) or ""})


def ANSWER(r: Redactor, v: Any) -> Any:
    """Model text: a verdict inside a review recording, otherwise a placeholder."""
    return VERDICT_ANSWER(r, v) if r.review_answers else r.text(v)


# Review check summaries (review/checks.py): the fixed status phrase is kept, so the
# replay's hard-finding rebuild (tracing/replay.py `_review_checks`) still reads them.
_CHECK_PHRASES = sorted(
    (
        "Tests and build: passed",
        "Tests and build: FAILED",
        "Tests and build: ",
        "Changed-line coverage: unknown",
        "Changed-line coverage: ",
        "Revert check: the changed tests fail when the source change is reverted (good).",
        "Revert check: FAILED.",
        "Revert check: unknown",
        "Revert check: ",
        "Run the change: not runnable",
        "Run the change: FAILED.",
        "Run the change: ",
    ),
    key=len,
    reverse=True,
)
_UNCOVERED = re.compile(r"New code no test executes: (.+?)\.(?:\s|$)")


def CHECK_SUMMARY(r: Redactor, v: Any) -> Any:
    if not isinstance(v, str):
        return r.unknown(v)
    head = next((p for p in _CHECK_PHRASES if v.startswith(p)), None)
    if head is None:
        return r.text(v)
    rest = v[len(head) :]
    gap = "" if head.endswith(" ") else " "
    out = head + (gap + r.text(rest.strip()) if rest.strip() else rest)
    m = _UNCOVERED.search(v)
    if m:
        names = []
        for item in m.group(1).split(","):
            path, _, fn = item.strip().rpartition(":")
            names.append(f"{PATH(r, path)}:{r.pseudo('fn', fn)}" if path else r.pseudo("fn", fn))
        out += " New code no test executes: " + ", ".join(names) + "."
    return out


# =========================================================================== schemas

#: OpenCode tool parameters (opencode.ai/docs/tools); anything else is a ``key-N``.
TOOL_INPUT = Schema(
    {
        **dict.fromkeys(("filePath", "file_path", "path", "filename", "paths", "cwd"), PATH),
        **dict.fromkeys(
            (
                "command",
                "pattern",
                "include",
                "glob",
                "content",
                "oldString",
                "newString",
                "description",
                "query",
                "url",
                "prompt",
                "text",
                "subagent_type",
                "todos",
                "patchText",
            ),
            TEXT,
        ),
        **dict.fromkeys(("offset", "limit", "timeout", "replaceAll", "line", "format"), SCALAR),
    },
    strict_keys=True,
)

_TOOL_CALL = Schema({"id": CALL_ID, "name": TOOL_NAME, "input": TOOL_INPUT})
_CONTENT_BLOCK = Schema(
    {"type": CODE, "text": ANSWER, "id": CALL_ID, "name": TOOL_NAME, "input": TOOL_INPUT}
)


def _message_content(r: Redactor, v: Any) -> Any:
    if isinstance(v, list):
        return [r.apply(Schema({"type": CODE, "text": TEXT}), x) for x in v]
    return r.text(v)


_REQUEST = Schema(
    {
        "model": TOKEN,
        "stream": BOOL,
        "stream_options": Schema({"include_usage": BOOL}),
        **dict.fromkeys(
            (
                "temperature",
                "top_p",
                "max_tokens",
                "max_completion_tokens",
                "n",
                "seed",
                "presence_penalty",
                "frequency_penalty",
            ),
            NUM,
        ),
        "messages": list_of(
            Schema(
                {
                    "role": CODE,
                    "content": _message_content,
                    "name": TOKEN,
                    "tool_call_id": CALL_ID,
                    "tool_calls": list_of(
                        Schema(
                            {
                                "id": CALL_ID,
                                "type": CODE,
                                "function": Schema({"name": TOOL_NAME, "arguments": TEXT}),
                            }
                        )
                    ),
                }
            )
        ),
        "tools": list_of(
            Schema(
                {
                    "type": CODE,
                    "function": Schema(
                        {"name": TOOL_NAME, "description": TEXT, "parameters": TEXT}
                    ),
                }
            )
        ),
        "tool_choice": TEXT,
    }
)

_COMMON_ROW = {"kind": CODE, "seq": INT, "ts": TIME, "turn": INT}

#: Recording rows (tracing/recorder.py), by ``kind``.
ROW_SCHEMAS: Mapping[str, Schema] = {
    "header": Schema(
        {
            **_COMMON_ROW,
            "version": INT,
            "run_id": RUN_ID,
            "model": TOKEN,
            "ticket_id": INT,
            "attempt_id": INT,
            "role": CODE,
            "trace_id": HEX,
            "base_commit": COMMIT,
            "head_commit": COMMIT,
            "variant_id": TOKEN,
            "tier": INT,
            "started_at": TIME,
        }
    ),
    "user": Schema({**_COMMON_ROW, "text": TEXT, "actor": CODE, "by": USER}),
    "llm": Schema(
        {
            **_COMMON_ROW,
            "actor": CODE,
            "text": ANSWER,
            "stop_reason": CODE,
            "tool_calls": list_of(_TOOL_CALL),
            "raw_content": list_of(_CONTENT_BLOCK),
            "usage": Schema(
                {
                    k: INT
                    for k in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "reasoning_tokens",
                    )
                }
            ),
            "model": TOKEN,
            "request": _REQUEST,
            "latency_ms": NUM,
            "cost_usd": NUM,
            "prompt_version": TOKEN,
            "output_file": FILEREF,
        }
    ),
    "tool": Schema(
        {
            **_COMMON_ROW,
            "tool_use_id": CALL_ID,
            "name": TOOL_NAME,
            "input": TOOL_INPUT,
            "content": TEXT,  # CHECK_SUMMARY for review_check rows (see _redact_row)
            "is_error": BOOL,
            "sources": list_of(TEXT),
            "approval": TEXT,
            "tool_kind": CODE,
            "output_file": FILEREF,
        }
    ),
    "result": Schema(
        {
            **_COMMON_ROW,
            "output": TEXT,
            "stopped_reason": CODE,
            "turns": INT,
            "total_ms": NUM,
            "sources": list_of(TEXT),
            "cited": list_of(TEXT),
            "fabricated_citations": list_of(TEXT),
            "outcome": CODE,
            "tests_passed": BOOL,
            "diff_lines": INT,
            "diff_files": INT,
            "head_commit": COMMIT,
            "tag": CODE,
            "error": TOKEN,
            "error_message": TEXT,
        }
    ),
}
_UNKNOWN_ROW = Schema(_COMMON_ROW)

#: Span attributes (tracing/otel.py and every ``set_attribute`` in the package).
SPAN_ATTRIBUTES = Schema(
    {
        **dict.fromkeys(
            (
                "ticket.id",
                "ticket.issue_number",
                "attempt.id",
                "review.id",
                "kickback.id",
                "attempt.tier",
                "attempt.diff_lines",
                "route.tier",
                "route.requested_tier",
                "github.pr_number",
                "verify.tests_run",
                "verify.tests_failed",
                "llm.token_count.prompt",
                "llm.token_count.completion",
                "llm.token_count.total",
                "clarify.questions",
                "failure.step_index",
                "embed.count",
            ),
            INT,
        ),
        **dict.fromkeys(
            ("llm.latency_ms", "llm.cost_usd", "review.cost_usd", "check.percent"), NUM
        ),
        **dict.fromkeys(
            (
                "route.has_model",
                "tool.is_error",
                "verify.passed",
                "review.shadow",
                "check.reused",
                "exception.escaped",
            ),
            BOOL,
        ),
        **dict.fromkeys(
            (
                "variant_id",
                "attempt.model",
                "llm.model_name",
                "llm.prompt_version",
                "review.reviewer_model",
                "review.prompt",
                "route.classifier",
                "route.prompt_version",
                "route.embed_model",
                "clarify.classifier",
                "clarify.prompt_version",
                "failure.labeler",
                "failure.prompt_version",
                "embed.model",
                "exception.type",
            ),
            TOKEN,
        ),
        **dict.fromkeys(
            (
                "attempt.trigger",
                "attempt.outcome",
                "route.source",
                "route.label",
                "tool.kind",
                "review.verdict",
                "review.tag",
                "review.check",
                "check.status",
                "github.action",
                "kickback.by",
                "kickback.tag",
                "clarify.result",
                "failure.source",
                "failure.step",
            ),
            CODE,
        ),
        **dict.fromkeys(
            (
                "ticket.title",
                "route.reason",
                "verify.command",
                "check.reason",
                "kickback.note",
                "clarify.reason",
                "failure.reason",
                "exception.message",
                "exception.stacktrace",
            ),
            TEXT,
        ),
        "ticket.repo": REPO,
        "tool.name": TOOL_NAME,
        "attempt.branch": BRANCH,
        "attempt.base_commit": COMMIT,
        "route.matched_tickets": list_of(INT),
        "check.summary": CHECK_SUMMARY,
        "input.value": CONTENT,
        "output.value": CONTENT,
        "output.file": FILEREF,
    }
)

_SPAN_CONTEXT = Schema(
    {"trace_id": HEX, "span_id": HEX, "trace_state": _matching(re.compile(r"\[\]"))}
)
_RESOURCE = Schema(
    {
        "attributes": Schema(
            {
                "service.name": TOKEN,
                "telemetry.sdk.language": TOKEN,
                "telemetry.sdk.name": TOKEN,
                "telemetry.sdk.version": TOKEN,
                "service.instance.id": lambda r, v: r.pseudo("instance", v),
            }
        ),
        "schema_url": TEXT,
    }
)


def _span_schema(attributes: Schema) -> Schema:
    return Schema(
        {
            "name": TOKEN,
            "context": _SPAN_CONTEXT,
            "kind": TOKEN,
            "parent_id": HEX,
            "start_time": TIME,
            "end_time": TIME,
            "status": Schema({"status_code": CODE, "description": TEXT}),
            "attributes": attributes,
            "events": list_of(
                Schema({"name": TOKEN, "timestamp": TIME, "attributes": SPAN_ATTRIBUTES})
            ),
            "links": list_of(Schema({"context": _SPAN_CONTEXT, "attributes": SPAN_ATTRIBUTES})),
            "resource": _RESOURCE,
        }
    )


SPAN = _span_schema(SPAN_ATTRIBUTES)
_ROUTE_LLM = _span_schema(Schema({**SPAN_ATTRIBUTES.rules, "output.value": ROUTE_ANSWER}))

#: Store rows (db/store.py dataclasses), in ``replay.rows_to_json``'s layout.
_TICKET = Schema(
    {
        **dict.fromkeys(
            ("id", "issue_number", "pr_number", "accepted_attempt_id", "routed_tier"), INT
        ),
        **dict.fromkeys(("created_at", "done_at", "flagged_at"), TIME),
        "title": TEXT,
        "request": TEXT,
        "repo": REPO,
        "base_branch": BRANCH,
        "owned_files": list_of(PATH),
        "column": CODE,
        "source": CODE,
        "route_reason": TEXT,
        "trace_id": HEX,
        "flag_reason": FLAG,
    }
)
_ATTEMPT = Schema(
    {
        **dict.fromkeys(
            ("id", "ticket_id", "tier", "diff_files", "diff_lines", "tokens_in", "tokens_out"), INT
        ),
        **dict.fromkeys(("cost_usd", "seconds"), NUM),
        **dict.fromkeys(("tests_passed", "build_passed", "ci_passed"), BOOL),
        **dict.fromkeys(("started_at", "ended_at"), TIME),
        **dict.fromkeys(("trigger", "outcome", "ci_status"), CODE),
        "model": TOKEN,
        "variant_id": TOKEN,
        "worktree": PATH,
        "branch": BRANCH,
        "base_commit": COMMIT,
        "verify_output_path": FILEREF,
    }
)
_KICKBACK = Schema(
    {
        **dict.fromkeys(("id", "attempt_id", "failure_step_index"), INT),
        **dict.fromkeys(("by", "tag", "failure_step"), CODE),
        "note": TEXT,
        "created_at": TIME,
    }
)
_REVIEW = Schema(
    {
        **dict.fromkeys(("id", "attempt_id", "tokens_in", "tokens_out"), INT),
        **dict.fromkeys(("changed_line_coverage", "cost_usd"), NUM),
        **dict.fromkeys(("tests_passed", "tests_nonvacuous", "shadow"), BOOL),
        **dict.fromkeys(("verdict", "ran_app", "tag"), CODE),
        "reviewer_model": TOKEN,
        "variant_id": TOKEN,
        "note": TEXT,
        "created_at": TIME,
    }
)
ROWS = Schema(
    {
        "ticket": _TICKET,
        "ladder": list_of(Schema({"tier": INT, "model": TOKEN})),
        "attempts": list_of(_ATTEMPT),
        "kickbacks": list_of(_KICKBACK),
        "reviews": list_of(_REVIEW),
        "shadow_reviews": list_of(_REVIEW),
        "uncounted": list_of(INT),
        "explore": BOOL,
    }
)


# =========================================================================== sources

_RECORDING = re.compile(r"([0-9]{1,18})(?:\.([a-z][a-z_\-]{0,31}))?\.jsonl(\.gz)?", re.ASCII)
_SIDE_DIR = re.compile(r"[0-9]{1,18}(?:\.[a-z][a-z_\-]{0,31})?\.files", re.ASCII)
_SIDE_FILE = re.compile(r"[0-9]{1,8}-[a-z]{1,16}\.txt", re.ASCII)
_SPAN_FILE = re.compile(r"[0-9a-f]{32}\.jsonl", re.ASCII)
_LOG_FILE = re.compile(r"attempt-[0-9]{1,18}-[a-z]{1,16}\.log", re.ASCII)


def _ticket_dir(root: Path, ticket_id: int) -> Path | None:
    """``root/<id>`` if it is a real directory directly under ``root`` (no symlink)."""
    d = root / str(ticket_id)
    if not d.is_dir() or d.is_symlink():
        return None
    if d.resolve().parent != root.resolve():
        return None
    return d


def _files(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if not p.is_symlink())


def _read(p: Path) -> bytes:
    """Read without following a symlink (the file was checked; O_NOFOLLOW closes the race)."""
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _jsonl(data: bytes, r: Redactor) -> list[dict[str, Any]]:
    out = []
    for ln in data.decode("utf-8", "replace").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            r.counts["line_dropped"] += 1
            continue
        if not isinstance(row, dict):
            r.counts["line_dropped"] += 1
            continue
        out.append(row)
    return out


def side_file(r: Redactor, data: bytes) -> bytes:
    """A side file (the full text of a long output): one placeholder line, the same one
    the recording row with that text gets."""
    return (str(r.text(data.decode("utf-8", "replace"))) + "\n").encode()


def log_file(r: Redactor, data: bytes) -> bytes:
    """A verify or CI log: one placeholder line."""
    return (str(r.text(data.decode("utf-8", "replace"))) + "\n").encode()


def _dump_jsonl(rows: Sequence[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows).encode()


# =========================================================================== export


@dataclass
class ExportResult:
    path: Path
    manifest: dict[str, Any]


def _redact_row(r: Redactor, row: dict[str, Any]) -> dict[str, Any]:
    kind = row.get("kind")
    schema = ROW_SCHEMAS.get(kind) if isinstance(kind, str) else None
    if schema is None:
        r.counts["unknown_row"] += 1
        return _UNKNOWN_ROW.apply(r, row)
    out = schema.apply(r, row)
    if kind == "tool" and row.get("tool_kind") == "review_check":
        out["content"] = CHECK_SUMMARY(r, row.get("content"))
    if kind == "llm":
        r.note_content(row.get("text") or "", out.get("text") or "")
    elif kind == "tool" and row.get("tool_kind") != "review_check":
        r.note_content(row.get("input"), out.get("input"))
        r.note_content(row.get("content") or "", out.get("content") or "")
    return out


def _recordings(r: Redactor, d: Path, out: dict[str, bytes], prefix: str) -> None:
    for p in _files(d):
        m = _RECORDING.fullmatch(p.name)
        if m and p.is_file():
            raw = _read(p)
            if m.group(3):
                try:
                    raw = gzip.decompress(raw)
                except (OSError, EOFError):
                    r.counts["file_unreadable"] += 1
                    continue
            r.review_answers = (m.group(2) or "").startswith("review")
            try:
                rows = [_redact_row(r, row) for row in _jsonl(raw, r)]
            finally:
                r.review_answers = False
            data = _dump_jsonl(rows)
            out[f"{prefix}/{p.name}"] = gzip.compress(data, mtime=0) if m.group(3) else data
            r.counts["file.recording"] += 1
        elif _SIDE_DIR.fullmatch(p.name) and p.is_dir():
            for f in _files(p):
                if _SIDE_FILE.fullmatch(f.name) and f.is_file():
                    out[f"{prefix}/{p.name}/{f.name}"] = side_file(r, _read(f))
                    r.counts["file.side"] += 1
                else:
                    r.counts["file_skipped"] += 1
        else:
            r.counts["file_skipped"] += 1


def _traces(r: Redactor, d: Path, out: dict[str, bytes], prefix: str) -> None:
    for p in _files(d):
        if not p.is_file():
            r.counts["file_skipped"] += 1
        elif _SPAN_FILE.fullmatch(p.name):
            rows = _jsonl(_read(p), r)
            names = {
                str((x.get("context") or {}).get("span_id")): x.get("name")
                for x in rows
                if isinstance(x.get("context"), dict)
            }
            red = []
            for x in rows:
                parent = names.get(str(x.get("parent_id")))
                schema = _ROUTE_LLM if (x.get("name"), parent) == ("llm.call", "route") else SPAN
                red.append(schema.apply(r, x))
            out[f"{prefix}/{p.name}"] = _dump_jsonl(red)
            r.counts["file.span"] += 1
        elif _LOG_FILE.fullmatch(p.name):
            out[f"{prefix}/{p.name}"] = log_file(r, _read(p))
            r.counts["file.log"] += 1
        else:
            r.counts["file_skipped"] += 1


def _row_json(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _row_json(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list | tuple):
        return [_row_json(x) for x in obj]
    if isinstance(obj, frozenset | set):
        return sorted(obj)
    return obj


def store_rows(store: Any, ticket_id: int, *, ladder: Sequence[Any] = ()) -> dict[str, Any] | None:
    """The ticket's store rows in ``replay.rows_to_json``'s layout (so ``fman replay
    --rows`` reads them), plus its shadow reviews. None when the store has no such ticket."""
    from foreman_inator.db.store import NotFoundError
    from foreman_inator.routing import explore
    from foreman_inator.tracing import replay

    try:
        ticket = store.get_ticket(ticket_id)
    except NotFoundError:
        return None
    flag = store.get_cursor(ticket.repo, explore.explore_cursor(ticket_id))
    data = replay.rows_to_json(
        replay.ticket_rows(store, ticket_id, ladder=ladder, explore=explore.decode(flag))
    )
    data["shadow_reviews"] = _row_json(store.list_reviews(ticket_id=ticket_id, shadow=True))
    return data


def _fman_home() -> Path:
    from foreman_inator import paths

    override = os.environ.get(paths.ENV_HOME)
    return (Path(override).expanduser() if override else Path.home() / ".foreman-inator").resolve()


def default_output(ticket_id: int, now: datetime | None = None) -> Path:
    """``~/Downloads/fman-export-t<id>-<UTC time>.tar.gz`` (``~`` without Downloads)."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    folder = Path.home() / "Downloads"
    return (folder if folder.is_dir() else Path.home()) / f"fman-export-t{ticket_id}-{stamp}.tar.gz"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def git_work_tree(path: Path) -> Path | None:
    """The git work tree ``path`` would land in, if any."""
    for parent in [path.resolve(), *path.resolve().parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _is_archive(path: Path) -> bool:
    return path.name.endswith((".tar.gz", ".tgz"))


def _check_output(out: Path, sources: Sequence[Path]) -> None:
    for root in [_fman_home(), *sources]:
        if _inside(out, root):
            raise ExportError(
                f"refusing to write the export inside {root}: choose a folder outside "
                "the foreman-inator home and the source folders"
            )
    if not out.parent.is_dir():
        raise ExportError(f"no such folder: {out.parent}")
    if _is_archive(out):
        if out.exists() or out.is_symlink():
            raise ExportError(f"{out} already exists; not overwriting it")
    elif out.exists() and (not out.is_dir() or out.is_symlink() or any(out.iterdir())):
        raise ExportError(f"{out} exists and is not an empty folder")


def _manifest(r: Redactor, ticket_id: int, files: Mapping[str, bytes], rows: bool) -> dict:
    c = r.counts
    return {
        "format": FORMAT,
        "tool": "foreman-inator",
        "version": __version__,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ticket_id": ticket_id,
        "redacted": True,
        "redaction": {
            "method": "allow-list: every field has a rule; anything not on the list is replaced",
            "placeholder": "<redacted N chars #h>: N = the original's length, h = the first "
            f"{HASH_HEX} hex digits of HMAC-SHA256(salt, original)",
            "salt": f"{SALT_BYTES} random bytes made for this export, kept in memory only and "
            "discarded; not written anywhere, so hashes match only inside this export",
            "kept": [
                "structure: row kinds and order, span names, trace/span ids, parent links, events",
                "timestamps and durations",
                "token counts, cost, latency",
                "model and variant ids, prompt versions, tiers, triggers",
                "outcomes, verdicts, tags, check statuses, booleans, error flags",
                "tool names and kinds, ids, counts and sizes",
                "the router's label and the reviewer's verdict/tag (decisions, not text)",
            ],
            "replaced": [
                "issue title and body, prompts, model output and reasoning, model requests",
                "tool inputs and outputs, file contents, diffs, verify and CI output",
                "review notes and check details, kickback notes and line comments",
                "clarification Q&A, route and flag reasons, error messages",
                "every string under a key not on the allow-list",
            ],
            "pseudonymised": "repo-N, user-N, path-N.ext, branch-N, commit-N, key-N, fn-N",
            "unknown_numbers": "dropped (null); unknown booleans and nulls kept",
        },
        "counts": {
            "values_replaced": c["text"],
            "pseudonyms": r.pseudonym_counts(),
            "unknown_keys": c["unknown_key"],
            "unknown_rows": c["unknown_row"],
            "numbers_dropped": c["number_dropped"],
            "times_dropped": c["time_dropped"],
            "lines_dropped": c["line_dropped"],
            "files": {
                "recordings": c["file.recording"],
                "side_files": c["file.side"],
                "span_files": c["file.span"],
                "logs": c["file.log"],
                "rows": int(rows),
                "skipped_unknown": c["file_skipped"],
                "unreadable": c["file_unreadable"],
            },
        },
        "files": [{"path": k, "bytes": len(v)} for k, v in sorted(files.items())],
        "open_with": (
            f"fman replay {ticket_id} --recordings <export>/recordings "
            "--traces <export>/traces" + (" --rows <export>/rows.json" if rows else "")
        ),
    }


def build_export(
    ticket_id: int,
    *,
    recordings_dir: str | Path,
    traces_dir: str | Path,
    rows: Mapping[str, Any] | None = None,
    salt: bytes | None = None,
) -> dict[str, bytes]:
    """The redacted export in memory: ``{relative path: bytes}``, MANIFEST.json included.
    ``rows``: the ticket's store rows (``store_rows``) or None. ``salt`` is for tests;
    by default a fresh random one, never stored."""
    tid = parse_id(ticket_id)
    if tid is None:
        raise ExportError(f"not a ticket id: {ticket_id!r}")
    r = Redactor(salt)
    out: dict[str, bytes] = {}
    rec_root, tr_root = Path(recordings_dir), Path(traces_dir)
    rd = _ticket_dir(rec_root, tid) if rec_root.is_dir() else None
    td = _ticket_dir(tr_root, tid) if tr_root.is_dir() else None
    if rd is not None:  # recordings first: they fill the span content map
        _recordings(r, rd, out, f"recordings/{tid}")
    if td is not None:
        _traces(r, td, out, f"traces/{tid}")
    if rows is not None:
        out["rows.json"] = (json.dumps(ROWS.apply(r, dict(rows)), indent=1) + "\n").encode()
    if not out:
        raise ExportError(f"nothing recorded for ticket {tid}")
    out["MANIFEST.json"] = (
        json.dumps(_manifest(r, tid, out, rows is not None), indent=1) + "\n"
    ).encode()
    return out


def _write_dir(out: Path, files: Mapping[str, bytes]) -> None:
    out.mkdir(mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    for rel, data in sorted(files.items()):
        target = out / rel
        for parent in reversed(target.relative_to(out).parents[:-1]):
            d = out / parent
            if not d.is_dir():
                d.mkdir(mode=0o700)
                os.chmod(d, 0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)


def _write_archive(out: Path, top: str, files: Mapping[str, bytes]) -> None:
    """A gzipped tar with no owner names, ids or original file name in it."""
    tmp = out.with_name(f".{out.name}.{secrets.token_hex(4)}.partial")
    now = int(time.time())

    def info(name: str, *, size: int = 0, is_dir: bool = False) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(name)
        ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
        ti.mode, ti.size, ti.mtime = (0o700 if is_dir else 0o600), size, now
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with (
            os.fdopen(fd, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
        ):
            dirs = {top}
            for rel in files:
                parts = PurePosixPath(rel).parts[:-1]
                dirs.update(f"{top}/{'/'.join(parts[: i + 1])}" for i in range(len(parts)))
            for d in sorted(dirs):
                tar.addfile(info(d, is_dir=True))
            for rel, data in sorted(files.items()):
                tar.addfile(info(f"{top}/{rel}", size=len(data)), io.BytesIO(data))
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def export_ticket(
    ticket_id: int | str,
    *,
    recordings_dir: str | Path,
    traces_dir: str | Path,
    out: str | Path | None = None,
    db_path: str | Path | None = None,
    rows: Mapping[str, Any] | None = None,
    ladder: Sequence[Any] = (),
    redact: bool = True,
    salt: bytes | None = None,
) -> ExportResult:
    """Write the redacted export of one ticket to ``out`` (a ``.tar.gz``/``.tgz`` file, or
    otherwise a new or empty folder; default ``default_output``). Store rows come from
    ``rows``, or from ``db_path`` opened read-only. ``redact=False`` is refused."""
    if not redact:
        raise ExportError(REFUSE_RAW)
    tid = parse_id(ticket_id)
    if tid is None:
        raise ExportError(f"not a ticket id: {ticket_id!r} (digits only)")
    target = Path(out).expanduser() if out is not None else default_output(tid)
    _check_output(target, [Path(recordings_dir), Path(traces_dir)])
    if rows is None and db_path is not None and Path(db_path).is_file():
        from foreman_inator.db.store import Store

        with contextlib.closing(Store(db_path, read_only=True)) as store:
            rows = store_rows(store, tid, ladder=ladder)
    files = build_export(
        tid, recordings_dir=recordings_dir, traces_dir=traces_dir, rows=rows, salt=salt
    )
    if _is_archive(target):
        _write_archive(target, f"fman-export-t{tid}", files)
    else:
        _write_dir(target, files)
    return ExportResult(target, json.loads(files["MANIFEST.json"]))


__all__ = [
    "REFUSE_RAW",
    "ExportError",
    "ExportResult",
    "Redactor",
    "build_export",
    "default_output",
    "export_ticket",
    "git_work_tree",
    "store_rows",
]
