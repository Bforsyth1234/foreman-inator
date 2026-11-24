"""The AI reviewer (todo #17): five checks in SPEC order, one verdict.

``review_change(...)`` reviews one change (``base_sha..head_sha`` in the repo mirror) and
returns a ``ReviewOutcome``. Inside one ``review`` span it runs, each in its own
``review.check`` child span (``tracing.REVIEW_CHECKS`` order):

1. ``tests``                 rerun verify in a scratch worktree at the change (checks.py)
2. ``changed_line_coverage`` coverage of the changed Python lines, or unknown (checks.py)
3. ``test_revert``           revert the source, keep the tests, expect failure (checks.py)
4. ``run_app``               run the change when safely runnable, else not_runnable
5. ``read_diff``             the model call with ``prompts/reviewer-v1.md``

Verdict policy (assistant decision, 2026-11-23):

* Family rule first: a reviewer whose ``family`` equals the writer's is refused with
  ``FamilyConflict`` before anything runs (invariant 7f); the worker hands the ticket to
  the user. ``pick_reviewer(config, writer)`` raises ``NoIndependentReviewer`` likewise.
* Check 1 fails -> ``request_changes``/``broke``; checks 2-5 are skipped (recorded as
  skipped spans). Check 1 times out or cannot run to the end -> ``handed_to_user``.
* Otherwise checks 2-4 run, and ``checks.hard_findings`` decides: vacuous tests, a new
  function no test executes, or a run regression -> ``request_changes`` with the first
  finding's tag, every finding in the note, and the model call skipped. The model can
  never overrule a hard finding, because it is never asked.
* No hard finding -> check 5. The reply must parse strictly into approve /
  request_changes + tag (store TAGS) + note. A malformed reply is retried once with a
  correction message, then the review is ``handed_to_user``; never approve.
* An empty diff -> ``request_changes``/``incomplete`` without a model call.
* The whole review is bounded by ``timeout`` (injectable, with ``clock``). Running out,
  a model/proxy error (including a spend-cap refusal) or any unexpected error ends the
  review as ``handed_to_user`` with the reason in the note. Scratch worktrees and the temp
  folder are always removed.

Invariant 7f: check 5's input is exactly ``ReviewInput`` (issue title and body, the diff
bounded with a visible truncation marker, the changed-file list, the checks 1-4
summaries). ``review_change`` is keyword-only with no ``**kwargs`` and takes no writer
transcript, reasoning, final message or writer prompt; only the writer's
``ModelEndpoint`` (for the family check).

Posting: the bot opened the PR, so GitHub only accepts a COMMENT review from it. The
structured verdict lives in the ``reviews`` row (and a ``kickbacks`` row with
``by="reviewer"`` for request_changes); ``ReviewOutcome.comment_body`` is the text for
the comment review, which #21 posts. Nothing here talks to GitHub. A shadow review
(``shadow=True``, #26) is stored with ``shadow=1``, gets no kickback row and must not be
posted.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import re
import secrets
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template
from threading import Event
from typing import Any, Protocol

import httpx
from fde_kit.prompts import PromptRef, load_prompt

from foreman_inator import tracing
from foreman_inator.config import Config, ModelEndpoint
from foreman_inator.db.store import TAGS, Kickback, Review, Store
from foreman_inator.proxy import pricing
from foreman_inator.runner import verify
from foreman_inator.runner.worktree import RepoMirror, Worktree
from foreman_inator.tickets.prompt import clip_head, defuse

from . import checks as C

REVIEWER_PROMPT_NAME = "reviewer-v1"
DEFAULT_TIMEOUT_S = 30 * 60.0
"""Whole-review bound: checks 1-4 plus the model call."""
CHECK_TIMEOUT_S = 20 * 60.0
"""Per test run (checks 1-3), never more than what is left of the review's bound."""
MODEL_MAX_TOKENS = 1_500

TITLE_BUDGET = 300
ISSUE_BODY_BUDGET = 12_000
DIFF_BUDGET = 60_000
CHANGED_FILES_MAX = 200
NOTE_BUDGET = 1_500
COMMENT_MARKER = "<!-- foreman-inator:ai-review -->"

_SECTION = re.compile(r"^<!-- section: ([a-z_]+) -->$", re.MULTILINE)
_provisional = itertools.count(1)


# --------------------------------------------------------------------------- errors


class ReviewError(RuntimeError):
    """The review cannot be run at all; the worker hands the ticket to the user."""


class FamilyConflict(ReviewError):
    """The reviewer is the same model family as the writer (invariant 7f)."""


class NoIndependentReviewer(ReviewError):
    """No configured reviewer is from a different family than the writer."""


class MalformedVerdict(ValueError):
    """The model's reply is not a valid verdict. ``problem`` says why (sent back once)."""

    def __init__(self, problem: str):
        super().__init__(problem)
        self.problem = problem


class ModelCallError(RuntimeError):
    """The proxy or upstream refused or failed the reviewer's model call."""


class _OutOfTime(Exception):
    def __init__(self, where: str):
        super().__init__(where)
        self.where = where


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class IssueText:
    """The issue as GitHub has it. The only free text from the ticket the reviewer sees."""

    number: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class ChangeRef:
    """The change under review: ``base_sha..head_sha`` in ``mirror`` (the worker commits the
    writer's change onto the attempt branch in the mirror, so both commits are there)."""

    mirror: RepoMirror
    base_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class ReviewInput:
    """Exactly what check 5 sees (invariant 7f). There is no field for anything else."""

    issue: IssueText
    diff: str  # already bounded, with a visible truncation marker
    changed_files: tuple[str, ...]
    checks: tuple[str, ...]  # checks 1-4 summaries, in order

    @classmethod
    def build(
        cls, issue: IssueText, diff: str, changed_files: Sequence[str], checks: Sequence[str]
    ) -> ReviewInput:
        return cls(
            IssueText(issue.number, issue.title, issue.body),
            clip_head(diff, DIFF_BUDGET),
            tuple(changed_files),
            tuple(checks),
        )


@dataclass(frozen=True)
class ModelVerdict:
    verdict: str  # approve | request_changes
    tag: str | None
    note: str


@dataclass(frozen=True)
class ModelReply:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


class ReviewModel(Protocol):
    """How check 5 reaches the model. The default posts to the proxy context's base URL."""

    def complete(
        self, *, base_url: str, model: ModelEndpoint, messages: list[dict[str, str]], timeout: float
    ) -> ModelReply: ...


class ProxyReviewModel:
    """Non-streaming ``chat/completions`` through the model proxy (so it is traced,
    recorded and spend-checked like every other call)."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def complete(
        self, *, base_url: str, model: ModelEndpoint, messages: list[dict[str, str]], timeout: float
    ) -> ModelReply:
        body = {
            "model": model.name,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "max_tokens": MODEL_MAX_TOKENS,
        }
        try:
            with httpx.Client(transport=self.transport, timeout=max(1.0, timeout)) as client:
                resp = client.post(f"{base_url}/chat/completions", json=body)
        except httpx.TimeoutException:
            raise _OutOfTime("read_diff") from None
        except httpx.HTTPError as exc:
            raise ModelCallError(f"model call failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            code = ""
            with contextlib.suppress(ValueError, AttributeError, TypeError):
                code = resp.json()["error"].get("code") or ""
            raise ModelCallError(f"model call refused: HTTP {resp.status_code} {code}".strip())
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelCallError("model reply is not a chat completion") from None
        usage = data.get("usage") or {}
        t_in = usage.get("prompt_tokens")
        t_out = usage.get("completion_tokens")
        if not isinstance(t_in, int):
            t_in = pricing.estimate_tokens(json.dumps(messages))
        if not isinstance(t_out, int):
            t_out = pricing.estimate_tokens(text)
        cost = pricing.price_for(model).cost(t_in, t_out)
        return ModelReply(text, t_in, t_out, cost)


# --------------------------------------------------------------------------- outcome


@dataclass(frozen=True)
class ReviewOutcome:
    """Everything one review produced. ``review``/``kickback`` are set when a store was given."""

    verdict: str  # store VERDICTS
    tag: str | None
    note: str
    tests: C.TestsCheck | None
    coverage: C.CoverageCheck | None
    revert: C.RevertCheck | None
    run_app: C.RunAppCheck | None
    read_diff: str  # "approve" | "request_changes" | "skipped" | "malformed" | "error"
    findings: tuple[C.HardFinding, ...]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model_calls: int
    reviewer_model: str
    variant_id: str
    prompt_name: str
    prompt_hash: str
    seconds: float
    comment_body: str
    shadow: bool = False
    review: Review | None = None
    kickback: Kickback | None = None

    # The reviews-table columns.
    @property
    def tests_passed(self) -> bool | None:
        return self.tests.passed if self.tests else None

    @property
    def changed_line_coverage(self) -> float | None:
        return self.coverage.percent if self.coverage else None

    @property
    def tests_nonvacuous(self) -> bool | None:
        return self.revert.nonvacuous if self.revert else None

    @property
    def ran_app(self) -> str | None:
        return self.run_app.ran_app if self.run_app else None


# --------------------------------------------------------------------------- reviewer pick


def check_independent(reviewer: ModelEndpoint, writer: ModelEndpoint) -> None:
    """Raise ``FamilyConflict`` when the reviewer is the writer's family (invariant 7f)."""
    if reviewer.family.strip().lower() == writer.family.strip().lower():
        raise FamilyConflict(
            f"reviewer {reviewer.name!r} is the same model family ({reviewer.family!r}) as "
            f"writer {writer.name!r}; an independent reviewer is required"
        )


def pick_reviewer(config: Config, writer: ModelEndpoint) -> ModelEndpoint:
    """``config.reviewer_for(writer)`` or ``NoIndependentReviewer``."""
    found = config.reviewer_for(writer)
    if found is None:
        raise NoIndependentReviewer(
            f"no reviewer in reviewer_models has a family other than {writer.family!r}"
        )
    check_independent(found, writer)
    return found


# --------------------------------------------------------------------------- prompt


def load_reviewer_template(name: str = REVIEWER_PROMPT_NAME) -> PromptRef:
    """Load a reviewer prompt from the package's ``prompts/`` folder (package data)."""
    folder = resources.files("foreman_inator") / "prompts"
    with resources.as_file(folder / f"{name}.md") as path:
        return load_prompt(str(path))


def _sections(text: str) -> dict[str, Template]:
    parts = _SECTION.split(text)
    out = {n: Template(b.strip("\n")) for n, b in zip(parts[1::2], parts[2::2], strict=True)}
    missing = {"system", "user", "retry"} - set(out)
    if missing:
        raise ValueError(f"reviewer template lacks sections {sorted(missing)}")
    return out


def build_messages(review_input: ReviewInput, template: PromptRef) -> list[dict[str, str]]:
    """The system and user messages for check 5, from ``review_input`` alone."""
    if not isinstance(review_input, ReviewInput):
        raise TypeError("build_messages takes a ReviewInput")
    s = _sections(template.text)
    issue = review_input.issue
    files = list(review_input.changed_files)
    shown = [f"- {defuse(clip_head(p, 200))}" for p in files[:CHANGED_FILES_MAX]]
    if len(files) > CHANGED_FILES_MAX:
        shown.append(f"[... truncated: {len(files) - CHANGED_FILES_MAX} more files not shown ...]")
    user = s["user"].substitute(
        issue_number=issue.number,
        issue_title=defuse(clip_head(issue.title.strip(), TITLE_BUDGET)),
        issue_body=defuse(clip_head(issue.body.strip(), ISSUE_BODY_BUDGET)) or "(no description)",
        changed_count=len(files),
        changed_files="\n".join(shown) or "(none)",
        checks="\n".join(f"{i}. {c}" for i, c in enumerate(review_input.checks, 1)),
        diff=defuse(review_input.diff) or "(empty diff)",
    )
    return [
        {"role": "system", "content": s["system"].substitute()},
        {"role": "user", "content": user},
    ]


def parse_verdict(text: str) -> ModelVerdict:
    """Strict: one JSON object with exactly ``verdict``, ``tag``, ``note`` (an optional
    ```json fence around it is tolerated). Raises ``MalformedVerdict``."""
    raw = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
    except ValueError:
        raise MalformedVerdict("it is not a single JSON object") from None
    if not isinstance(obj, dict):
        raise MalformedVerdict("it is not a JSON object")
    if set(obj) != {"verdict", "tag", "note"}:
        raise MalformedVerdict('the keys must be exactly "verdict", "tag" and "note"')
    verdict, tag, note = obj["verdict"], obj["tag"], obj["note"]
    if verdict not in ("approve", "request_changes"):
        raise MalformedVerdict('"verdict" must be "approve" or "request_changes"')
    if not isinstance(note, str) or not note.strip():
        raise MalformedVerdict('"note" must be a non-empty string')
    if verdict == "approve" and tag is not None:
        raise MalformedVerdict('"tag" must be null when approving')
    if verdict == "request_changes" and tag not in TAGS:
        raise MalformedVerdict(f'"tag" must be one of {", ".join(TAGS)} when requesting changes')
    return ModelVerdict(verdict, tag, clip_head(note.strip(), NOTE_BUDGET))


# --------------------------------------------------------------------------- comment body

_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
_VERDICT_TITLE = {
    "approve": "approve",
    "request_changes": "changes requested",
    "handed_to_user": "handed to you",
}


def _safe(text: str) -> str:
    """Model or tool text for a GitHub comment: no @mentions, no HTML comment markers."""
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return _MENTION.sub("@​", text)


def render_comment(
    *,
    verdict: str,
    tag: str | None,
    note: str,
    reviewer_model: str,
    variant_id: str,
    check_lines: Sequence[tuple[str, str]],
    shadow: bool = False,
) -> str:
    """The comment-review body (#21 posts it; GitHub refuses APPROVE from the PR's author)."""
    lines = [
        COMMENT_MARKER,
        f"### AI review: {_VERDICT_TITLE.get(verdict, verdict)}",
        "",
        f"Reviewer `{_safe(reviewer_model)}` (`{_safe(variant_id)}`). foreman-inator records this "
        "verdict itself; the bot cannot approve or request changes on its own pull request.",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, result in check_lines:
        lines.append(f"| {name} | {_safe(result).replace('|', '\\|').replace(chr(10), ' ')} |")
    if tag:
        lines += ["", f"**Tag:** {tag}"]
    if note:
        lines.append("")
        lines.extend("> " + line for line in _safe(note.strip()).splitlines())
    if shadow:
        lines += ["", "_Shadow review: stored for comparison only, never posted._"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- the review


#: Checks 1-4 of a review already run on the same change: (tests, coverage, revert,
#: run_app), as ``ReviewOutcome`` carries them (#29; the shadow reviewer reuses them).
Precomputed = tuple[
    "C.TestsCheck", "C.CoverageCheck | None", "C.RevertCheck | None", "C.RunAppCheck | None"
]


def review_change(
    *,
    issue: IssueText,
    change: ChangeRef,
    writer: ModelEndpoint,
    reviewer: ModelEndpoint,
    proxy: Any,
    attempt_id: int,
    ticket_id: int | None = None,
    store: Store | None = None,
    shadow: bool = False,
    bucket: str = "live",
    prompt_name: str = REVIEWER_PROMPT_NAME,
    verify_override: str | None = None,
    recordings_dir: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    check_timeout: float = CHECK_TIMEOUT_S,
    python: str = "python3",
    model_client: ReviewModel | None = None,
    cancel: Event | None = None,
    clock: Callable[[], float] = time.monotonic,
    precomputed: Precomputed | None = None,
) -> ReviewOutcome:
    """Review ``change`` against ``issue``. See the module docstring for the policy.

    ``precomputed`` (#29, the shadow reviewer): results of checks 1-4 already run on this
    exact change (``(tests, coverage, revert, run_app)`` of another review's outcome).
    They are reused instead of run again (each check span says ``check.reused``); the
    head scratch worktree is still made for the diff, and the verdict policy (hard
    findings first, then check 5) is unchanged.

    Call it inside ``tracing.ticket_span(ticket_id, trace_id=...)`` so the ``review`` span
    joins the ticket's trace. ``proxy`` is the running ``ModelProxy`` (its ``context()``
    gives the reviewer's base URL). With ``store``, the review row (and for
    request_changes, unless ``shadow``, a reviewer kickback) is written; with
    ``recordings_dir`` and ``ticket_id``, the review is recorded next to the attempt.
    ``prompt_name`` picks a challenger prompt (#26); ``bucket`` is the spend bucket the
    model call is costed to (``"experiments"`` for replays and shadow reviews). Raises
    ``FamilyConflict``, and the store's error if the row cannot be written (one non-shadow
    review per attempt)."""
    check_independent(reviewer, writer)
    template = load_reviewer_template(prompt_name)
    variant_id = f"{reviewer.name}/{template.name}@{template.sha256[:8]}"
    provisional = -next(_provisional)  # span key until the row exists (ids are positive)
    role = "review-shadow" if shadow else "review"
    run = _Run(
        issue=issue,
        change=change,
        reviewer=reviewer,
        template=template,
        variant_id=variant_id,
        proxy=proxy,
        attempt_id=attempt_id,
        review_key=provisional,
        role=role,
        bucket=bucket,
        verify_override=verify_override,
        timeout=timeout,
        check_timeout=check_timeout,
        python=python,
        client=model_client or ProxyReviewModel(),
        cancel=cancel,
        clock=clock,
        precomputed=precomputed,
    )
    recorder = None
    if recordings_dir is not None and ticket_id is not None:
        recorder = tracing.Recorder(
            recordings_dir,
            ticket_id,
            attempt_id,
            role=role,
            base_commit=change.base_sha,
            model=reviewer.name,
            variant_id=variant_id,
            head_commit=change.head_sha,
        )
    run.recorder = recorder
    outcome: ReviewOutcome | None = None
    try:
        with tracing.review_span(
            provisional, attempt_id=attempt_id, reviewer_model=reviewer.name, variant_id=variant_id
        ) as rspan:
            rspan.set_attribute("review.shadow", shadow)
            rspan.set_attribute("review.prompt", f"{template.name}@{template.sha256[:12]}")
            try:
                outcome = run.execute(shadow=shadow)
            finally:
                run.cleanup()
            tracing.set_verdict(rspan, outcome.verdict)
            if outcome.tag:
                rspan.set_attribute("review.tag", outcome.tag)
            rspan.set_attribute("review.cost_usd", outcome.cost_usd)
            if store is not None:
                outcome = record_outcome(store, attempt_id, outcome)
                if outcome.review is not None:
                    rspan.set_attribute("review.id", outcome.review.id)
    finally:
        if recorder is not None:
            if outcome is None:
                recorder.close("error", stopped_reason="error")
            else:
                recorder.close(outcome.verdict, output=outcome.comment_body, tag=outcome.tag)
    return outcome


def record_outcome(store: Store, attempt_id: int, outcome: ReviewOutcome) -> ReviewOutcome:
    """Write the ``reviews`` row, plus a ``kickbacks`` row (``by="reviewer"``) for
    request_changes on a non-shadow review. Returns the outcome with both attached."""
    review = store.record_review(
        attempt_id,
        reviewer_model=outcome.reviewer_model,
        verdict=outcome.verdict,  # type: ignore[arg-type]
        variant_id=outcome.variant_id,
        tests_passed=outcome.tests_passed,
        changed_line_coverage=outcome.changed_line_coverage,
        tests_nonvacuous=outcome.tests_nonvacuous,
        ran_app=outcome.ran_app,  # type: ignore[arg-type]
        tag=outcome.tag,  # type: ignore[arg-type]
        note=outcome.note,
        tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out,
        cost_usd=outcome.cost_usd,
        shadow=outcome.shadow,
    )
    kickback = None
    if outcome.verdict == "request_changes" and not outcome.shadow:
        kickback = store.record_kickback(
            attempt_id,
            by="reviewer",
            tag=outcome.tag,  # type: ignore[arg-type]
            note=outcome.note,
        )
    return dataclasses.replace(outcome, review=review, kickback=kickback)


class _Run:
    """One review's state. Private: ``review_change`` is the API."""

    def __init__(self, **kw: Any):
        self.__dict__.update(kw)
        self.recorder: tracing.Recorder | None = None
        self.t0 = self.clock()
        self.deadline = self.t0 + self.timeout
        self.token = secrets.token_hex(4)
        self.scratches: list[Worktree] = []
        self.work = C.scratch_work_dir()
        self.tests: C.TestsCheck | None = None
        self.coverage: C.CoverageCheck | None = None
        self.revert: C.RevertCheck | None = None
        self.run_app: C.RunAppCheck | None = None
        self.read_diff = "skipped"
        self.findings: list[C.HardFinding] = []
        self.tokens_in = self.tokens_out = self.calls = 0
        self.cost = 0.0

    # -- plumbing ---------------------------------------------------------------

    def left(self, where: str) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0 or (self.cancel is not None and self.cancel.is_set()):
            raise _OutOfTime(where)
        return remaining

    def budget(self, where: str) -> float:
        return min(self.check_timeout, self.left(where))

    def scratch(self, suffix: str) -> Worktree:
        wt = self.change.mirror.scratch(self.change.head_sha, f"review-{self.token}-{suffix}")
        self.scratches.append(wt)
        return wt

    def cleanup(self) -> None:
        for wt in self.scratches:
            with contextlib.suppress(Exception):
                wt.remove(delete_branch=False)
        self.scratches.clear()
        C.remove_work_dir(self.work)

    def record_tool(self, check: str, command: str | None, content: str) -> None:
        if self.recorder is not None:
            self.recorder.tool(
                name=f"review.{check}",
                input={"command": command or ""},
                content=content,
                tool_kind="review_check",
            )

    @contextlib.contextmanager
    def check_span(self, name: str) -> Iterator[Any]:
        with tracing.review_check_span(name) as s:
            yield s

    def skip(self, name: str, reason: str) -> None:
        with self.check_span(name) as s:
            s.set_attribute("check.status", "skipped")
            s.set_attribute("check.reason", reason)

    def report(self, s: Any, name: str, result: Any) -> None:
        s.set_attribute("check.status", result.status)
        s.set_attribute("check.summary", result.summary())
        self.record_tool(
            name,
            getattr(result, "command", None),
            result.summary()
            + ("\n" + result.output_tail if getattr(result, "output_tail", "") else ""),
        )

    # -- the checks -----------------------------------------------------------

    def execute(self, *, shadow: bool) -> ReviewOutcome:
        try:
            return self._execute(shadow)
        except _OutOfTime as exc:
            why = (
                "cancelled"
                if self.cancel is not None and self.cancel.is_set()
                else (f"the review ran out of time ({self.timeout:.0f}s) during {exc.where}")
            )
            return self.finish("handed_to_user", None, f"Review could not complete: {why}.", shadow)
        except ModelCallError as exc:
            return self.finish("handed_to_user", None, f"Review could not complete: {exc}.", shadow)
        except Exception as exc:  # a review that cannot complete never approves
            return self.finish(
                "handed_to_user",
                None,
                f"Review could not complete: {type(exc).__name__}: {str(exc)[:500]}",
                shadow,
            )

    def _execute(self, shadow: bool) -> ReviewOutcome:
        head = self.scratch("head")
        ranged = dataclasses.replace(head, base_sha=self.change.base_sha)
        diff = ranged.diff_text()
        changed = ranged.changed_files()
        names = tracing.REVIEW_CHECKS

        pre = self.precomputed
        # 1. tests
        with self.check_span(names[0]) as s:
            if pre is not None:
                self.tests = pre[0]
                self.reused(s, names[0], self.tests)
            else:
                command = verify.detect(head.path, self.verify_override, python=self.python)
                self.tests = C.check_tests(
                    head.path,
                    command,
                    timeout=self.budget(names[0]),
                    output_path=self.work / "verify.log",
                    cancel=self.cancel,
                )
                self.report(s, names[0], self.tests)
        if self.tests.status == "timed_out":
            self.left(names[0])  # out of the whole bound -> _OutOfTime
            for n in names[1:]:
                self.skip(n, "tests did not finish")
            return self.finish(
                "handed_to_user",
                None,
                f"Review could not complete: the tests did not finish ({self.tests.reason}).",
                shadow,
            )
        if self.tests.status == "failed":
            for n in names[1:]:
                self.skip(n, "tests fail")
            self.findings = C.hard_findings(self.tests, None, None, None)
            return self.finish_findings(shadow)

        if not diff.strip():
            for n in names[1:]:
                self.skip(n, "empty change")
            return self.finish("request_changes", "incomplete", "The change is empty.", shadow)

        if pre is not None:
            return self._execute_reused(shadow, diff, changed)

        # 2. changed-line coverage
        with self.check_span(names[1]) as s:
            self.coverage = C.check_coverage(
                head.path,
                diff,
                timeout=self.budget(names[1]),
                work_dir=self.work / "coverage",
                python=self.python,
                cancel=self.cancel,
            )
            self.report(s, names[1], self.coverage)
            if self.coverage.percent is not None:
                s.set_attribute("check.percent", self.coverage.percent)

        # 3. revert check
        reverted: Worktree | None = None
        with self.check_span(names[2]) as s:
            budget = self.budget(names[2])
            # check_revert returns "unknown" before touching anything when there is
            # nothing to revert or no tests to run, so the head worktree is safe then.
            if any(C.is_runnable_test(p) for p in changed) and any(
                not C.is_test_path(p) for p in changed
            ):
                reverted = self.scratch("revert")
            self.revert = C.check_revert(
                (reverted or head).path,
                self.change.base_sha,
                changed,
                timeout=budget,
                output_path=self.work / "revert.log",
                python=self.python,
                cancel=self.cancel,
            )
            self.report(s, names[2], self.revert)

        # 4. run the change
        with self.check_span(names[3]) as s:
            self.left(names[3])
            base_dir = self._base_source(reverted, changed)
            self.run_app = C.check_run_app(
                head.path,
                base_dir,
                timeout=min(C.RUN_APP_TIMEOUT_S, self.left(names[3])),
                work_dir=self.work / "run",
                python=self.python,
                cancel=self.cancel,
            )
            self.report(s, names[3], self.run_app)

        return self._verdict(shadow, diff, changed)

    def _execute_reused(self, shadow: bool, diff: str, changed: Sequence[str]) -> ReviewOutcome:
        """Checks 2-4 from ``precomputed`` (check 1 was taken by ``_execute``)."""
        names = tracing.REVIEW_CHECKS
        _, self.coverage, self.revert, self.run_app = self.precomputed
        results = (self.coverage, self.revert, self.run_app)
        for name, result in zip(names[1:4], results, strict=True):
            if result is None:
                self.skip(name, "not run by the review it was reused from")
                continue
            with self.check_span(name) as s:
                self.reused(s, name, result)
        return self._verdict(shadow, diff, changed)

    def reused(self, s: Any, name: str, result: Any) -> None:
        s.set_attribute("check.reused", True)
        self.report(s, name, result)

    def _verdict(self, shadow: bool, diff: str, changed: Sequence[str]) -> ReviewOutcome:
        names = tracing.REVIEW_CHECKS
        self.findings = C.hard_findings(self.tests, self.coverage, self.revert, self.run_app)
        if self.findings:
            self.skip(names[4], "a hard check failed: " + self.findings[0].check)
            return self.finish_findings(shadow)

        # 5. read the diff against the issue
        with self.check_span(names[4]) as s:
            verdict = self.ask_model(diff, changed)
            s.set_attribute("check.status", self.read_diff)
        if verdict is None:
            return self.finish(
                "handed_to_user",
                None,
                "Review could not complete: the reviewer model gave no valid verdict in two tries.",
                shadow,
            )
        return self.finish(verdict.verdict, verdict.tag, verdict.note, shadow)

    def _base_source(self, reverted: Worktree | None, changed: Sequence[str]) -> Path | None:
        """A tree with the base's source (and the change's tests), for check 4's comparison."""
        if not any(not C.is_test_path(p) for p in changed):
            return None
        if reverted is not None and self.revert is not None and self.revert.reverted:
            return reverted.path
        wt = self.scratch("base")
        C.revert_sources(wt.path, self.change.base_sha, changed)
        return wt.path

    def ask_model(self, diff: str, changed: Sequence[str]) -> ModelVerdict | None:
        summaries = [r.summary() for r in (self.tests, self.coverage, self.revert, self.run_app)]
        review_input = ReviewInput.build(self.issue, diff, changed, summaries)
        messages = build_messages(review_input, self.template)
        retry = _sections(self.template.text)["retry"]
        if self.recorder is not None:
            self.recorder.user("\n\n".join(m["content"] for m in messages), by="reviewer")
        with self.proxy.context(
            attempt_id=self.attempt_id,
            review_id=self.review_key,
            bucket=self.bucket,
            label="review",
            prompt_version=f"{self.template.name}@{self.template.sha256[:12]}",
            recorder_role=self.role,
        ) as ctx:
            for attempt in (1, 2):
                reply = self.client.complete(
                    base_url=ctx.base_url,
                    model=self.reviewer,
                    messages=messages,
                    timeout=self.left("read_diff"),
                )
                self.calls += 1
                self.tokens_in += reply.tokens_in
                self.tokens_out += reply.tokens_out
                self.cost += reply.cost_usd
                try:
                    verdict = parse_verdict(reply.text)
                except MalformedVerdict as exc:
                    self.read_diff = "malformed"
                    if attempt == 2:
                        return None
                    messages = [
                        *messages,
                        {"role": "assistant", "content": reply.text[:4000]},
                        {"role": "user", "content": retry.substitute(problem=exc.problem)},
                    ]
                    continue
                self.read_diff = verdict.verdict
                return verdict
        return None

    # -- results ----------------------------------------------------------------

    def finish_findings(self, shadow: bool) -> ReviewOutcome:
        note = "\n\n".join(f.note for f in self.findings)
        return self.finish("request_changes", self.findings[0].tag, note, shadow)

    def finish(self, verdict: str, tag: str | None, note: str, shadow: bool) -> ReviewOutcome:
        note = clip_head(note.strip(), 4000)
        rows: list[tuple[str, str]] = []
        labels = ("1. Tests and build", "2. Changed-line coverage", "3. Revert check", "4. Run it")
        for label, result in zip(
            labels, (self.tests, self.coverage, self.revert, self.run_app), strict=True
        ):
            rows.append((label, result.summary() if result is not None else "skipped"))
        rows.append(("5. Read the diff", self.read_diff))
        body = render_comment(
            verdict=verdict,
            tag=tag,
            note=note,
            reviewer_model=self.reviewer.name,
            variant_id=self.variant_id,
            check_lines=rows,
            shadow=shadow,
        )
        return ReviewOutcome(
            verdict=verdict,
            tag=tag,
            note=note,
            tests=self.tests,
            coverage=self.coverage,
            revert=self.revert,
            run_app=self.run_app,
            read_diff=self.read_diff,
            findings=tuple(self.findings),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            cost_usd=self.cost,
            model_calls=self.calls,
            reviewer_model=self.reviewer.name,
            variant_id=self.variant_id,
            prompt_name=self.template.name,
            prompt_hash=self.template.sha256,
            seconds=self.clock() - self.t0,
            comment_body=body,
            shadow=shadow,
        )
