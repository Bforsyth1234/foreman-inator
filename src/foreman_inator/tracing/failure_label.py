"""Failure-step labeling: for every kickback, which step of the attempt went wrong (todo #27,
SPEC "Tracing" -> "Failure-step labeling").

``label_kickback(store, kickback_id, recordings_dir=, config=, proxy=) -> LabelResult``
labels one kickback row and writes ``kickbacks.failure_step`` / ``failure_step_index``
with ``store.set_failure_step``. It never raises, never waits much longer than ``timeout``
for the model, and is safe to call from a background job once the kickback row exists.
The order:

1. Already labeled -> nothing is done (``source="existing"``), unless ``force=True``.
2. ``reviewer_miss`` by rule, never by the model: a *user* kickback on an attempt whose
   non-shadow AI review verdict was ``approve`` (``is_reviewer_miss``). The model is not
   asked, and the step index is NULL (the review is not a row of the attempt recording).
   The schema holds one ``failure_step`` per kickback row, so the writer-side cause of
   such a kickback is not labeled: the miss is charged to the reviewer, which is what
   SPEC's "reviewer miss rate" counts. A writer-side label can still be had by calling
   ``label_kickback(..., force=True, skip_rule=True)``, which overwrites it.
3. Otherwise the attempt's recording is read (``<recordings>/<ticket>/<attempt>.jsonl.gz``,
   or the plain ``.jsonl`` a crash leaves). Missing or unreadable -> nothing is stored and
   the reason says so (the recording was pruned, or the attempt never started one).
4. ``summarize_attempt`` builds a compact, deterministic trace summary (below); one local
   model call with ``prompts/failure-label-v1.md`` picks one of the five model labels
   (``MODEL_LABELS``) and a step index; ``parse_answer`` accepts exactly one JSON object
   ``{"label": ..., "step": ...}`` naming a listed step, optionally after one ``<think>``
   block. Anything else (a sixth label, an unlisted step, prose, an HTTP error, a timeout,
   a cloud labeler not explicitly allowed) -> nothing is stored (NULL), never a guess.

Step index. ``failure_step_index`` is the 1-based position of a row in the recording's
rows *after the header line* (fde-kit ``RunRecord.rows``, file order): user, llm, tool and
result rows are all counted. For user, llm and tool rows this equals the row's own ``seq``
field, so a person can find it with ``zcat <attempt>.jsonl.gz | sed -n '<index+1>p'``
(line 1 is the header) and the replay viewer can show it as the row's ``seq``.
``step_row(record, index)`` maps an index back to its row. Only user, llm and tool rows are
offered to the model as steps.

Summary budget. Each step line is at most ``STEP_BUDGET`` (320) characters; the steps all
together at most ``TRACE_BUDGET`` (7,000) characters. Over budget, steps are dropped
lowest-value first (model text, then other tools, then reads and searches; edits, shell
commands and failed tool calls last), latest first within a class, and an "omitted" line
lists the dropped row numbers. With the issue (300 + 2,000), the kickback note (2,000),
line comments (1,500), the review line and the facts (at most ``FACTS_BUDGET`` 1,500), the
user message stays under about 15,000 characters.

Facts are deterministic hints, given to the model as facts and never used to pick a label
on their own: the verify result; a failing shell/test step with no later passing test run;
a file the kickback names that the attempt never read or edited (and whether a search
showed it); whether any test file was edited; the review's check results.

Invariant 7c: the call goes only through the model proxy (``proxy.context(label=
"failure_label")``) to a local model (``default_classifier(config)`` unless ``labeler`` is
given); a cloud labeler is refused unless ``allow_cloud=True``, and always (``allow_cloud``
or not) for a ticket whose repo is set local only (#49, ``cloud = false``; read with
``store.repo_allows_cloud`` when the store has it). The writer's own text is in
the recording and is shown here: this is analysis after the fact, not the reviewer, so
invariant 7f is not in play. All recorded and GitHub text is fenced and defused.

Tracing: the call runs inside a ``failure_label`` span (in the ticket's trace when the
ticket has a ``trace_id``, linked to the attempt span); the proxy's ``llm.call`` is its
child, with prompt version ``failure-label-v1@<hash[:8]>``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any, Literal

from fde_kit.prompts import PromptRef, load_prompt
from fde_kit.record import RunRecord

from foreman_inator import tracing
from foreman_inator.config import Config, ModelEndpoint
from foreman_inator.routing.rules import RouterError, classify, default_classifier
from foreman_inator.tickets.lifecycle import split_kickback_note
from foreman_inator.tickets.prompt import clip_head, clip_tail, defuse
from foreman_inator.tracing.recorder import load_recording, recording_path

log = logging.getLogger(__name__)

PROMPT_NAME = "failure-label-v1"
SPAN_NAME = "failure_label"
PROXY_LABEL = "failure_label"
#: The labels the model may pick. ``reviewer_miss`` is never among them (rule only).
MODEL_LABELS: tuple[str, ...] = (
    "misread_issue",
    "missed_file",
    "wrong_edit",
    "ignored_failure",
    "weak_tests",
)
RULE_LABEL = "reviewer_miss"
DEFAULT_TIMEOUT = 30.0  # seconds for the one model call

# Budgets (characters).
STEP_BUDGET = 320
TRACE_BUDGET = 7_000
FACTS_BUDGET = 1_500
TITLE_BUDGET = 300
ISSUE_BODY_BUDGET = 2_000
NOTE_BUDGET = 2_000
LINE_COMMENTS_BUDGET = 1_500
SHELL_TAIL = 160
MODEL_TEXT = 200
MAX_ANSWER_CHARS = 200
VERIFY_TAIL = 300

Source = Literal["existing", "rule", "model", "none"]

_SECTION = re.compile(r"^<!-- section: ([a-z_]+) -->$", re.MULTILINE)
_THINK = re.compile(r"^\s*<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_EXIT = re.compile(r"exit(?:ed)?(?: with)?(?: code| status)?[:= ]+(-?\d+)", re.IGNORECASE)
_FAIL = re.compile(
    r"\b[1-9]\d* (?:failed|failing|errors?)\b|^FAILED\b|^FAIL\b|npm ERR!|Traceback \(most recent",
    re.MULTILINE,
)
_TEST_CMD = re.compile(
    r"\b(?:pytest|unittest|jest|vitest|mocha|tox|nox|rspec|phpunit)\b|"
    r"\b(?:npm|pnpm|yarn|bun|cargo|go|swift|mix|dotnet|make|uv run|deno)\s+(?:run\s+)?test\b|"
    r"\bmake\s+check\b"
)
_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|spec|__tests__)/|(?:^|/)test_[^/]+$|_test\.\w+$|"
                        r"\.(?:test|spec)\.\w+$")  # fmt: skip
_CODE_EXT = (
    "py|pyi|js|jsx|ts|tsx|mjs|cjs|md|toml|json|yaml|yml|swift|go|rs|rb|java|kt|c|h|cc|cpp|"
    "hpp|cs|html|css|scss|sh|sql|txt|cfg|ini|lock|vue|svelte|php|ex|exs"
)
_NAMED_FILE = re.compile(rf"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:{_CODE_EXT}))(?![\w/])")
_COMMENT_HEAD = re.compile(r"^(\S+?)(?::\d+| \(whole file\))$")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\+\+\+ b/(.+)$", re.M)


class LabelError(Exception):
    """The labeler's answer was not acceptable; the message says why."""


@dataclass(frozen=True, slots=True)
class Step:
    """One numbered step of the summary. ``index`` is the row's position (see module doc)."""

    index: int
    kind: str  # prompt | model | read | search | edit | shell | tool
    text: str  # the line shown to the model, without the "[n] " prefix
    failed: bool = False
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TraceSummary:
    header: str
    steps: tuple[Step, ...]  # the steps shown (after budget trimming)
    omitted: tuple[int, ...]  # row indexes of steps dropped for the budget
    facts: tuple[str, ...]
    review: str | None

    @property
    def step_indexes(self) -> frozenset[int]:
        return frozenset(s.index for s in self.steps)

    def trace_text(self) -> str:
        lines = [self.header, "Steps ([n] = row n of the recording):"]
        lines += [f"[{s.index}] {s.text}" for s in self.steps]
        if self.omitted:
            lines.append(f"(omitted for length: rows {_ranges(self.omitted)})")
        if self.review:
            lines.append(self.review)
        return "\n".join(lines)

    def facts_text(self) -> str:
        if not self.facts:
            return "- (none)"
        return clip_head("\n".join(f"- {f}" for f in self.facts), FACTS_BUDGET)

    def text(self) -> str:
        """The whole summary as shown to the model (trace, then facts)."""
        return f"{self.trace_text()}\nFacts:\n{self.facts_text()}"


@dataclass(frozen=True, slots=True)
class LabelResult:
    kickback_id: int
    label: str | None  # what is now stored (None: nothing stored)
    step_index: int | None
    source: Source
    reason: str
    stored: bool = False  # True when this call wrote the row
    labeler: str | None = None
    prompt_version: str | None = None
    raw_answer: str | None = None
    summary: TraceSummary | None = field(default=None, repr=False)


# --------------------------------------------------------------------------- steps


def step_rows(record: RunRecord) -> list[tuple[int, dict[str, Any]]]:
    """Every row after the header with its step index (1-based position)."""
    return [(i, row) for i, row in enumerate(record.rows, start=1)]


def step_row(record: RunRecord, index: int) -> dict[str, Any] | None:
    """The recording row a stored ``failure_step_index`` points at, or None."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        return None
    return record.rows[index - 1] if index <= len(record.rows) else None


def find_recording(recordings_dir: str | Path, ticket_id: int, attempt_id: int) -> Path | None:
    """The attempt's finished recording, else the partial one a crash leaves, else None."""
    gz = recording_path(recordings_dir, ticket_id, attempt_id)
    if gz.is_file():
        return gz
    plain = gz.with_suffix("")
    return plain if plain.is_file() else None


def _one_line(text: Any, budget: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= budget else flat[: budget - 3].rstrip() + "..."


def _rel(path: str, worktree: str | None) -> str:
    p = path.strip()
    if worktree:
        root = worktree.rstrip("/") + "/"
        if p.rstrip("/") + "/" == root:
            return "."
        if p.startswith(root):
            p = p[len(root) :]
    return p.removeprefix("./")


def _input_paths(inp: dict[str, Any], worktree: str | None) -> list[str]:
    out: list[str] = []
    for key in ("filePath", "file_path", "path"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            out.append(_rel(v, worktree))
            break
    for key in ("patchText", "patch", "diff"):
        v = inp.get(key)
        if isinstance(v, str):
            for m in _PATCH_FILE.finditer(v):
                out.append(_rel((m.group(1) or m.group(2)).strip(), worktree))
    return list(dict.fromkeys(out))


def _lines(text: Any) -> int:
    return len(str(text).splitlines()) if text else 0


def _edit_size(inp: dict[str, Any]) -> str:
    if isinstance(inp.get("newString"), str) or isinstance(inp.get("oldString"), str):
        return f"-{_lines(inp.get('oldString'))} +{_lines(inp.get('newString'))} lines"
    if isinstance(inp.get("content"), str):
        return f"whole file, {_lines(inp['content'])} lines"
    if isinstance(inp.get("edits"), list):
        return f"{len(inp['edits'])} edits"
    return "change"


def _shell_status(row: dict[str, Any]) -> tuple[str, bool, str | None]:
    """(status text, failed?, matched failure snippet)."""
    content = str(row.get("content") or "")
    m = _EXIT.search(content[-2000:])
    code = int(m.group(1)) if m else None
    fail = _FAIL.search(content)
    snippet = _one_line(fail.group(0), 40) if fail else None
    if row.get("is_error"):
        return "ERROR", True, snippet
    if code is not None:
        return f"exit {code}", code != 0 or snippet is not None, snippet
    if snippet:
        return f"output says '{snippet}'", True, snippet
    return "exit status not recorded", False, None


def _kind_of(row: dict[str, Any]) -> str:
    kind = row.get("tool_kind")
    if not kind:
        from foreman_inator.runner.opencode import tool_kind  # lazy: heavy module

        kind = tool_kind(str(row.get("name") or ""))
    return str(kind)


def _tool_step(index: int, row: dict[str, Any], worktree: str | None) -> Step:
    name = str(row.get("name") or "tool")
    inp = row.get("input") if isinstance(row.get("input"), dict) else {}
    kind = _kind_of(row)
    paths = tuple(_input_paths(inp, worktree))
    err = bool(row.get("is_error"))
    content = str(row.get("content") or "")
    where = ", ".join(paths) or "?"
    if kind == "read":
        text = f"read {where}"
        if err:
            text += f" -> ERROR: {_one_line(content, 80)}"
        return Step(index, "read", text, err, paths)
    if kind == "search":
        pattern = inp.get("pattern") or inp.get("query") or ""
        scope = inp.get("path") or inp.get("include") or ""
        scope = _rel(str(scope), worktree) if scope else ""
        text = f"search {name}"
        if pattern:
            text += f" {json.dumps(_one_line(pattern, 60))}"
        if scope:
            text += f" in {scope}"
        found = [ln.strip() for ln in content.splitlines() if ln.strip()]
        text += f" -> {len(found)} result lines"
        files = _search_files(found, worktree)
        if files:
            text += f"; files: {', '.join(files[:6])}" + (" ..." if len(files) > 6 else "")
        return Step(index, "search", text, err, ())
    if kind == "edit":
        text = f"edit {where} ({_edit_size(inp)})"
        if err:
            text += f" -> ERROR: {_one_line(content, 80)}"
        return Step(index, "edit", text, err, paths)
    if kind == "shell":
        cmd = _one_line(inp.get("command") or "", 120)
        status, failed, _ = _shell_status(row)
        tail = _one_line(clip_tail(content, SHELL_TAIL).replace("\n", " | "), SHELL_TAIL + 40)
        text = f"shell `{cmd}` -> {status}"
        if tail:
            text += f"; tail: {json.dumps(tail)}"
        return Step(index, "shell", text, failed, ())
    text = f"{kind} {name}"
    if paths:
        text += f" {where}"
    if err:
        text += f" -> ERROR: {_one_line(content, 80)}"
    return Step(index, "tool", text, err, paths)


def _search_files(lines: list[str], worktree: str | None) -> list[str]:
    files: list[str] = []
    for ln in lines[:200]:
        head = ln.split(":", 1)[0].strip()
        if _NAMED_FILE.fullmatch(_rel(head, worktree)) or (worktree and head.startswith(worktree)):
            files.append(_rel(head, worktree))
    return list(dict.fromkeys(files))


def _steps(record: RunRecord, worktree: str | None) -> list[Step]:
    out: list[Step] = []
    for index, row in step_rows(record):
        kind = row.get("kind")
        if kind == "user":
            out.append(Step(index, "prompt", f"session prompt ({len(str(row.get('text') or '')):,}"
                                              " chars)"))  # fmt: skip
        elif kind == "llm":
            text = str(row.get("text") or "").strip()
            if text:
                out.append(
                    Step(index, "model", f"model says {json.dumps(_one_line(text, MODEL_TEXT))}")
                )
        elif kind == "tool":
            out.append(_tool_step(index, row, worktree))
    return [
        Step(s.index, s.kind, _one_line(defuse(s.text), STEP_BUDGET), s.failed, s.paths)
        for s in out
    ]


_DROP_ORDER = {"model": 0, "tool": 1, "prompt": 2, "read": 3, "search": 3, "edit": 4, "shell": 4}


def _trim(steps: list[Step], budget: int) -> tuple[list[Step], list[int]]:
    def size(ss: Iterable[Step]) -> int:
        return sum(len(s.text) + len(str(s.index)) + 4 for s in ss)

    keep = list(steps)
    dropped: list[int] = []
    order = sorted(
        keep, key=lambda s: (_DROP_ORDER.get(s.kind, 1) + (5 if s.failed else 0), -s.index)
    )
    for victim in order:
        if size(keep) <= budget:
            break
        keep.remove(victim)
        dropped.append(victim.index)
    return keep, sorted(dropped)


def _ranges(nums: Sequence[int]) -> str:
    out: list[str] = []
    start = prev = None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


# --------------------------------------------------------------------------- summary


def _yn(v: Any) -> str:
    return "unknown" if v is None else ("yes" if v else "no")


def review_line(review: Any) -> str:
    cov = review.changed_line_coverage
    cov_s = "unknown" if cov is None else f"{cov:.0f}%"
    parts = [
        f"AI review ({review.reviewer_model}): verdict {review.verdict}",
        f"tests passed: {_yn(review.tests_passed)}",
        f"changed-line coverage: {cov_s}",
        f"tests non-vacuous: {_yn(review.tests_nonvacuous)}",
        f"ran app: {review.ran_app or 'unknown'}",
    ]
    if review.tag:
        parts.append(f"tag: {review.tag}")
    line = "; ".join(parts)
    if review.note:
        line += f"; reviewer note: {json.dumps(_one_line(review.note, 400))}"
    return defuse(line)


def named_files(note: str | None) -> list[str]:
    """File paths a kickback note names: line-comment headers (``path:line``) first, then
    path-like words with a code extension in the body."""
    body, comments = split_kickback_note(note or "")
    out: list[str] = []
    for ln in (comments or "").splitlines():
        m = _COMMENT_HEAD.match(ln.strip())
        if m and _NAMED_FILE.fullmatch(m.group(1)):
            out.append(m.group(1).removeprefix("./"))
    out += [m.group(1).removeprefix("./") for m in _NAMED_FILE.finditer(body)]
    return list(dict.fromkeys(out))


def _same_file(a: str, b: str) -> bool:
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _facts(
    record: RunRecord,
    steps: list[Step],
    *,
    attempt: Any,
    note: str | None,
    review: Any,
    worktree: str | None,
) -> list[str]:
    facts: list[str] = []
    result = record.result
    # verify
    passed = (result or {}).get("tests_passed")
    if passed is None and attempt is not None:
        passed = attempt.tests_passed
    if passed is False:
        fact = "The repo's verify command failed after the session ended."
        tail = _verify_tail(attempt)
        if tail:
            fact += f" Output tail: {json.dumps(tail)}"
        facts.append(fact)
    elif passed is True:
        facts.append("The repo's verify command passed after the session ended.")
    if result is None:
        facts.append("The recording has no result row: the attempt stopped before it finished.")
    # failing shell steps with no later passing test run
    shells = [s for s in steps if s.kind == "shell"]
    rows = dict(step_rows(record))
    for s in shells:
        if not s.failed:
            continue
        cmd = str((rows[s.index].get("input") or {}).get("command") or "")
        later = [t for t in shells if t.index > s.index]
        rerun_ok = [
            t
            for t in later
            if not t.failed
            and (
                str((rows[t.index].get("input") or {}).get("command") or "") == cmd
                or (_TEST_CMD.search(cmd) and _TEST_CMD.search(
                    str((rows[t.index].get("input") or {}).get("command") or "")))
            )
        ]  # fmt: skip
        if rerun_ok:
            continue
        what = "a test run" if _TEST_CMD.search(cmd) else "a shell command"
        if not later:
            facts.append(f"Step {s.index} ({what}) failed and no shell command ran after it.")
        else:
            facts.append(f"Step {s.index} ({what}) failed and no later run of it passed.")
    # files the kickback names
    touched = [p for s in steps if s.kind in ("read", "edit") for p in s.paths]
    edited = [p for s in steps if s.kind == "edit" and not s.failed for p in s.paths]
    for name in named_files(note)[:8]:
        if any(_same_file(p, name) for p in touched):
            continue
        seen = [
            s.index
            for s in steps
            if s.kind == "search" and name in str(rows[s.index].get("content"))
        ]
        fact = f"The kickback names {name}; the attempt never read or edited it."
        if seen:
            fact += f" It appeared in search results at step {seen[0]}."
        facts.append(fact)
    # tests edited?
    if edited:
        tests = [p for p in edited if _TEST_PATH.search(p)]
        if not tests:
            facts.append("The attempt edited no test file.")
        elif set(tests) == set(edited):
            facts.append("The attempt edited only test files.")
    elif steps:
        facts.append("The attempt made no successful edit.")
    if review is not None and review.tests_nonvacuous is False:
        facts.append(
            "The AI review's revert check found the new tests pass without the source change."
        )
    return [defuse(f) for f in facts]


def _verify_tail(attempt: Any) -> str | None:
    path = getattr(attempt, "verify_output_path", None)
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")[-20_000:]
    except OSError:
        return None
    return _one_line(clip_tail(text, VERIFY_TAIL).replace("\n", " | "), VERIFY_TAIL + 60) or None


def summarize_attempt(
    record: RunRecord,
    *,
    attempt: Any = None,
    review: Any = None,
    note: str | None = None,
    trace_budget: int = TRACE_BUDGET,
) -> TraceSummary:
    """The compact trace summary of one attempt recording (see the module docstring).
    ``attempt`` is the store row (worktree for relative paths, verify output); ``review``
    the attempt's non-shadow review row; ``note`` the kickback note (files it names)."""
    h = record.header
    worktree = getattr(attempt, "worktree", None)
    steps = _steps(record, worktree)
    facts = _facts(record, steps, attempt=attempt, note=note, review=review, worktree=worktree)
    shown, omitted = _trim(steps, trace_budget)
    result = record.result
    bits = [
        f"Attempt {h.get('attempt_id')} (ticket {h.get('ticket_id')})",
        f"model {h.get('model') or getattr(attempt, 'model', '?')}",
        f"tier {h.get('tier') if h.get('tier') is not None else getattr(attempt, 'tier', '?')}",
    ]
    if attempt is not None:
        bits.append(f"trigger {attempt.trigger}")
    bits.append(f"{len(record.rows)} rows")
    if result is not None:
        bits.append(f"ended {result.get('outcome') or result.get('stopped_reason') or '?'}")
        if result.get("diff_lines") is not None:
            bits.append(f"{result.get('diff_lines')} diff lines")
    header = defuse(", ".join(str(b) for b in bits) + ".")
    return TraceSummary(
        header=header,
        steps=tuple(shown),
        omitted=tuple(omitted),
        facts=tuple(facts),
        review=review_line(review) if review is not None else None,
    )


# --------------------------------------------------------------------------- prompt


def load_template(name: str = PROMPT_NAME) -> PromptRef:
    """Load the labeling prompt from the package's ``prompts/`` folder (package data)."""
    folder = resources.files("foreman_inator") / "prompts"
    with resources.as_file(folder / f"{name}.md") as path:
        return load_prompt(str(path))


def prompt_version(ref: PromptRef) -> str:
    return f"{ref.name}@{ref.sha256[:8]}"


def _sections(text: str) -> dict[str, Template]:
    parts = _SECTION.split(text)
    out = {n: Template(b.strip("\n")) for n, b in zip(parts[1::2], parts[2::2], strict=True)}
    missing = {"system", "user"} - set(out)
    if missing:
        raise ValueError(f"failure-label template lacks section(s) {sorted(missing)}")
    return out


def build_messages(
    *,
    summary: TraceSummary,
    ticket: Any,
    kickback: Any,
    template: PromptRef,
) -> list[dict[str, str]]:
    """System rules, then the fenced issue, kickback note, facts and trace."""
    body, comments = split_kickback_note(kickback.note or "")
    s = _sections(template.text)
    user = s["user"].substitute(
        repo=defuse(clip_head(str(getattr(ticket, "repo", "") or ""), TITLE_BUDGET)),
        issue_title=defuse(
            clip_head(str(getattr(ticket, "title", "") or ""), TITLE_BUDGET).replace("\n", " ")
        ),
        issue_body=defuse(clip_head(str(getattr(ticket, "request", "") or ""), ISSUE_BODY_BUDGET))
        or "(no description)",
        kickback_by="the user" if kickback.by == "user" else "the AI reviewer",
        kickback_tag=kickback.tag or "(none)",
        kickback_note=defuse(clip_head(body, NOTE_BUDGET)) or "(no note)",
        line_comments=defuse(clip_head(comments or "", LINE_COMMENTS_BUDGET)) or "(none)",
        facts=summary.facts_text(),
        trace=summary.trace_text(),
        steps=", ".join(str(i) for i in sorted(summary.step_indexes)) or "(none)",
    )
    return [
        {"role": "system", "content": s["system"].substitute()},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- parsing


def parse_answer(text: Any, steps: Iterable[int]) -> tuple[str, int]:
    """``(label, step)`` from the labeler's answer. Accepted, and nothing else: one JSON
    object with exactly the keys ``label`` (one of ``MODEL_LABELS``) and ``step`` (an
    integer among ``steps``), surrounding whitespace allowed, optionally after one complete
    leading ``<think>...</think>`` block. Raises ``LabelError`` otherwise."""
    allowed = frozenset(steps)
    if not isinstance(text, str):
        raise LabelError("labeler returned no text")
    if len(text) > MAX_ANSWER_CHARS * 50:
        raise LabelError(f"answer too long ({len(text)} chars)")
    answer = _THINK.sub("", text, count=1).strip()
    if not answer:
        raise LabelError("empty answer")
    if len(answer) > MAX_ANSWER_CHARS:
        raise LabelError(f"answer too long ({len(answer)} chars)")
    try:
        obj = json.loads(answer)
    except ValueError as exc:
        raise LabelError(f"answer is not one JSON object: {_show(answer)}") from exc
    if not isinstance(obj, dict) or set(obj) != {"label", "step"}:
        raise LabelError(f"answer must have exactly label and step: {_show(answer)}")
    label, step = obj["label"], obj["step"]
    if not isinstance(label, str) or label not in MODEL_LABELS:
        raise LabelError(f"label not allowed: {_show(str(label))}")
    if isinstance(step, bool) or not isinstance(step, int):
        raise LabelError(f"step is not an integer: {_show(str(step))}")
    if step not in allowed:
        raise LabelError(f"step {step} is not a listed step")
    return label, step


def _show(text: str) -> str:
    flat = " ".join(text.split())
    return json.dumps(flat if len(flat) <= 60 else flat[:60] + "...", ensure_ascii=True)


# --------------------------------------------------------------------------- the rule


def approving_review(store: Any, attempt_id: int) -> Any | None:
    """The attempt's non-shadow AI review if its verdict was ``approve``, else None."""
    for r in store.list_reviews(attempt_id=attempt_id, shadow=False):
        if r.verdict == "approve":
            return r
    return None


def is_reviewer_miss(store: Any, kickback: Any) -> bool:
    """SPEC: "Reviewer approved a change you rejected": a user kickback on an attempt the
    (non-shadow) AI reviewer approved."""
    return kickback.by == "user" and approving_review(store, kickback.attempt_id) is not None


# --------------------------------------------------------------------------- label_kickback


@contextlib.contextmanager
def _trace(ticket: Any, attempt_id: int, kickback: Any) -> Iterator[Any]:
    trace_id = getattr(ticket, "trace_id", None)
    with contextlib.ExitStack() as stack:
        links = []
        if trace_id:
            if tracing.current_ticket() is None:
                stack.enter_context(tracing.ticket_span(ticket.id, trace_id=trace_id))
            with contextlib.suppress(Exception):
                links.append(tracing.attempt_link(trace_id, attempt_id))
        s = stack.enter_context(
            tracing.span(
                SPAN_NAME,
                {
                    "kickback.id": kickback.id,
                    "kickback.by": kickback.by,
                    "attempt.id": attempt_id,
                },
                links=links,
            )
        )
        yield s


def _annotate(s: Any, r: LabelResult) -> None:
    attrs = {
        "failure.source": r.source,
        "failure.reason": r.reason,
        "failure.step": r.label,
        "failure.step_index": r.step_index,
        "failure.labeler": r.labeler,
        "failure.prompt_version": r.prompt_version,
    }
    for k, v in attrs.items():
        if v is not None:
            s.set_attribute(k, v)


def label_kickback(
    store: Any,
    kickback_id: int,
    *,
    recordings_dir: str | Path,
    config: Config | Iterable[ModelEndpoint],
    proxy: Any,
    labeler: ModelEndpoint | None = None,
    allow_cloud: bool = False,
    force: bool = False,
    skip_rule: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    template: PromptRef | None = None,
) -> LabelResult:
    """Label one kickback and store it (see the module docstring). Never raises."""
    try:
        kickback = store.get_kickback(kickback_id)
    except Exception as exc:
        return LabelResult(kickback_id, None, None, "none", f"no kickback {kickback_id} ({exc})")
    if kickback.failure_step is not None and not force:
        return LabelResult(
            kickback_id,
            kickback.failure_step,
            kickback.failure_step_index,
            "existing",
            "already labeled; not relabeled (force=True to relabel)",
        )
    try:
        attempt = store.get_attempt(kickback.attempt_id)
        ticket = store.get_ticket(attempt.ticket_id)
    except Exception as exc:
        return LabelResult(kickback_id, None, None, "none", f"attempt or ticket missing ({exc})")
    try:
        with _trace(ticket, attempt.id, kickback) as s:
            result = _label(
                store,
                kickback,
                attempt=attempt,
                ticket=ticket,
                recordings_dir=recordings_dir,
                config=config,
                proxy=proxy,
                labeler=labeler,
                allow_cloud=allow_cloud,
                force=force,
                skip_rule=skip_rule,
                timeout=timeout,
                template=template,
            )
            _annotate(s, result)
    except Exception as exc:  # a background job must never die here
        log.exception("failure labeling of kickback %s failed", kickback_id)
        return LabelResult(kickback_id, None, None, "none", f"labeler error ({type(exc).__name__})")
    log.info(
        "kickback %s: failure step %s (step %s, %s: %s)",
        kickback_id,
        result.label,
        result.step_index,
        result.source,
        result.reason,
    )
    return result


def _repo_allows_cloud(store: Any, ticket: Any) -> bool:
    """#49: False when the ticket's repo is set local only. A store stand-in without the
    setting allows cloud (the ``allow_cloud`` gate still applies)."""
    check = getattr(store, "repo_allows_cloud", None)
    return True if check is None else bool(check(getattr(ticket, "repo", None)))


def _store(store: Any, kickback: Any, label: str, index: int | None, force: bool) -> bool:
    """Write unless someone labeled the row meanwhile (and we are not forcing)."""
    if not force and store.get_kickback(kickback.id).failure_step is not None:
        return False
    store.set_failure_step(kickback.id, label, index)
    return True


def _label(
    store: Any,
    kickback: Any,
    *,
    attempt: Any,
    ticket: Any,
    recordings_dir: str | Path,
    config: Config | Iterable[ModelEndpoint],
    proxy: Any,
    labeler: ModelEndpoint | None,
    allow_cloud: bool,
    force: bool,
    skip_rule: bool,
    timeout: float,
    template: PromptRef | None,
) -> LabelResult:
    kid = kickback.id
    review = approving_review(store, attempt.id)
    if not skip_rule and kickback.by == "user" and review is not None:
        stored = _store(store, kickback, RULE_LABEL, None, force)
        return LabelResult(
            kid,
            RULE_LABEL,
            None,
            "rule",
            f"user kickback on a change the AI reviewer approved (review {review.id})",
            stored=stored,
        )

    def none(why: str, **kw: Any) -> LabelResult:
        return LabelResult(kid, None, None, "none", why, **kw)

    path = find_recording(recordings_dir, ticket.id, attempt.id)
    if path is None:
        return none("recording missing (pruned, or the attempt never recorded); not labeled")
    try:
        record = load_recording(path)
    except Exception as exc:
        return none(f"recording unreadable ({type(exc).__name__}); not labeled")
    reviews = store.list_reviews(attempt_id=attempt.id, shadow=False)
    summary = summarize_attempt(
        record, attempt=attempt, review=reviews[0] if reviews else None, note=kickback.note
    )
    if not summary.steps:
        return none("recording has no steps; not labeled", summary=summary)
    models = config.models if isinstance(config, Config) else tuple(config)
    clf = labeler or default_classifier(models)
    if clf is None:
        return none("no local model configured to label with", summary=summary)
    if clf.is_cloud and not _repo_allows_cloud(store, ticket):
        return none(
            f"labeler {clf.name} is a cloud model and {ticket.repo} is local only "
            "(cloud = false); its code stays on the Mac (#49)",
            labeler=clf.name,
            summary=summary,
        )
    if clf.is_cloud and not allow_cloud:
        return none(
            f"labeler {clf.name} is a cloud model; traces stay on the Mac (7c)",
            labeler=clf.name,
            summary=summary,
        )
    if proxy is None:
        return none("no model proxy given", labeler=clf.name, summary=summary)
    ref = template or load_template()
    pv = prompt_version(ref)
    messages = build_messages(summary=summary, ticket=ticket, kickback=kickback, template=ref)
    raw: str | None = None
    try:
        with proxy.context(label=PROXY_LABEL, prompt_version=pv) as ctx:
            raw = classify(
                url=ctx.base_url,
                classifier=clf,
                messages=messages,
                prompt_version=pv,
                timeout=timeout,
            )
        label, step = parse_answer(raw, summary.step_indexes)
    except (RouterError, LabelError) as exc:
        return none(
            f"no label: {exc}",
            labeler=clf.name,
            prompt_version=pv,
            raw_answer=None if raw is None else raw[:MAX_ANSWER_CHARS],
            summary=summary,
        )
    stored = _store(store, kickback, label, step, force)
    return LabelResult(
        kid,
        label,
        step,
        "model",
        f"{pv} via {clf.name}" + ("" if stored else "; a label appeared meanwhile, kept it"),
        stored=stored,
        labeler=clf.name,
        prompt_version=pv,
        raw_answer=raw[:MAX_ANSWER_CHARS],
        summary=summary,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "MODEL_LABELS",
    "PROMPT_NAME",
    "RULE_LABEL",
    "SPAN_NAME",
    "LabelError",
    "LabelResult",
    "Step",
    "TraceSummary",
    "approving_review",
    "build_messages",
    "find_recording",
    "is_reviewer_miss",
    "label_kickback",
    "load_template",
    "named_files",
    "parse_answer",
    "prompt_version",
    "review_line",
    "step_row",
    "step_rows",
    "summarize_attempt",
]
