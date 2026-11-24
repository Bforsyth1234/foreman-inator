"""Replay viewer and recorded tickets as worker regression tests (todo #28).

The recorded cases under ``tests/recordings/`` are synthetic. ``make_cases`` below wrote
them, with the real ``Recorder``, the real span helpers (``capture_spans`` with a traces
dir, so the span files are written by the real ``TraceFileProcessor``) and a real store;
``test_checked_in_cases_match_the_generator`` checks that it still produces them.
Regenerate after a deliberate format change with::

    uv run python tests/test_replay.py --regenerate
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from foreman_inator import tracing
from foreman_inator.config import ModelEndpoint
from foreman_inator.db.store import Store
from foreman_inator.metrics import trace_view
from foreman_inator.metrics.web import create_app
from foreman_inator.review import checks as C
from foreman_inator.routing import escalation, rules
from foreman_inator.routing.escalation import Rung
from foreman_inator.tickets import lifecycle
from foreman_inator.tracing import replay

CASES = Path(__file__).parent / "recordings"
BASE_URL = "http://127.0.0.1:8642"

REPO = "example/widgets"
BASE = "b0a5e01"
HEAD = "c0de0042"
WRITER_PROMPT = "writer-v1@0a1b2c3d"
REVIEW_PROMPT = "reviewer-v1@9f8c16780000"
LADDER = (
    Rung(1, "inkling-2-mini"),
    Rung(2, "qwen3.8-27b"),
    Rung(3, "glm-5"),
    Rung(3, "claude-sonnet"),
)
REVIEWER = "claude-sonnet"
HOSTILE = '<script>alert("pwned")</script><b>bold</b><img src=x onerror=alert(1)>'
APPROVE = json.dumps({"verdict": "approve", "tag": None, "note": "Does what the issue asks."})
LONG_OUTPUT = "</pre>" + HOSTILE + "\n" + "\n".join(f"line {i:04d}: ok" for i in range(220))


# =========================================================================== the generator


class _Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(minutes=1)
        return self.t


class _Ns:
    """Strictly increasing epoch nanoseconds for content spans."""

    def __init__(self) -> None:
        import time

        self._time = time
        self.last = 0

    def __call__(self, step: int = 1_000) -> int:
        self.last = max(self._time.time_ns(), self.last + step)
        return self.last


class _Gen:
    def __init__(self, store: Store, rec_dir: Path) -> None:
        self.store = store
        self.rec_dir = rec_dir
        self.ns = _Ns()
        self.column: str | None = None
        self.ticket: Any = None

    # -- lifecycle

    def move(self, event: Any) -> None:
        tr = lifecycle.apply(self.column, event)
        if self.ticket is not None and tr.column != self.column:
            self.store.set_column(self.ticket.id, tr.column)
        self.column = tr.column

    # -- rows + spans, as the proxy and the OpenCode driver write them

    def llm(self, rec, attempt_id, *, model, text, calls=(), tin, tout, cost, pv, review=None):
        t0 = self.ns()
        t1 = self.ns()
        tracing.record_llm_call(
            attempt_id=attempt_id,
            review_id=review,
            model=model,
            tokens_in=tin,
            tokens_out=tout,
            latency_ms=(t1 - t0) / 1e6,
            cost_usd=cost,
            prompt_version=pv,
            input="(prompt messages)",
            output=text,
            start_time_ns=t0,
            end_time_ns=t1,
        )
        rec.llm(
            text=text,
            tokens_in=tin,
            tokens_out=tout,
            model=model,
            stop_reason="tool_use" if calls else "end_turn",
            tool_calls=calls,
            request={"model": model, "messages": [{"role": "user", "content": "(prompt)"}]},
            latency_ms=(t1 - t0) / 1e6,
            cost_usd=cost,
            prompt_version=pv,
        )
        if review is None:
            self.store.add_attempt_usage(attempt_id, tokens_in=tin, tokens_out=tout, cost_usd=cost)

    def tool(self, rec, attempt_id, *, name, input, output, kind, is_error=False):
        t0 = self.ns()
        t1 = self.ns()
        row = rec.tool(
            name=name,
            input=input,
            content=output,
            is_error=is_error,
            tool_use_id=f"call-{rec.attempt_id}-{t0 % 1000}",
            tool_kind=kind,
        )
        tracing.record_tool_call(
            attempt_id=attempt_id,
            name=name,
            input=input,
            output=output,
            kind=kind,
            is_error=is_error,
            output_file=(row or {}).get("output_file"),
            start_time_ns=t0,
            end_time_ns=t1,
        )
        return row

    def attempt(self, trace_id, *, model, tier, trigger, turns, passed, verify, tag_row=None):
        """One writer attempt. ``turns``: [(text, [(tool name, kind, input, output), ...])]."""
        t = self.ticket
        n = len(self.store.list_attempts(t.id)) + 1
        variant = f"{model}/{WRITER_PROMPT}"
        a = self.store.add_attempt(
            t.id,
            model=model,
            tier=tier,
            trigger=trigger,
            variant_id=variant,
            branch=f"fman/issue-{t.issue_number}/a{n}",
            base_commit=BASE,
        )
        self.move(lifecycle.AttemptStarted(t.id, trigger))
        marked = None
        with (
            tracing.attempt_span(
                a.id,
                variant_id=variant,
                model=model,
                tier=tier,
                trigger=trigger,
                branch=a.branch,
                base_commit=BASE,
            ) as span,
            tracing.Recorder(
                self.rec_dir,
                t.id,
                a.id,
                trace_id=trace_id,
                base_commit=BASE,
                model=model,
                variant_id=variant,
                tier=tier,
            ) as rec,
        ):
            rec.user(
                f"Issue #{t.issue_number}: {t.title}\n\n{t.request}\n\nRun `pytest -q`.",
                by="worker",
            )
            for i, (text, tools) in enumerate(turns, 1):
                calls = [
                    {"id": f"c{i}{j}", "name": tl[0], "input": tl[2]} for j, tl in enumerate(tools)
                ]
                self.llm(
                    rec,
                    a.id,
                    model=model,
                    text=text,
                    calls=calls,
                    tin=900 + 150 * i,
                    tout=60 + 20 * i,
                    cost=0.0,
                    pv=WRITER_PROMPT,
                )
                for name, kind, inp, out in tools:
                    row = self.tool(rec, a.id, name=name, input=inp, output=out, kind=kind)
                    if tag_row is not None and (name, kind) == tag_row and marked is None:
                        marked = row["seq"]  # #27's failure_step_index: the 1-based seq
            with tracing.verify_span("pytest -q") as v:
                tracing.set_verify_result(
                    v,
                    passed=passed,
                    tests_run=verify[0],
                    tests_failed=verify[1],
                    output=None if passed else verify[2],
                )
            outcome = None if passed else "failed_checks"
            a = self.store.finish_attempt(
                a.id, outcome, tests_passed=passed, diff_files=1, diff_lines=14
            )
            span.set_attribute("attempt.outcome", outcome or "checks_passed")
            span.set_attribute("attempt.diff_lines", 14)
            rec.close(
                outcome or "checks_passed", tests_passed=passed, diff_lines=14, head_commit=HEAD
            )
        if passed:
            self.move(lifecycle.ChecksPassed(t.id))
            if t.pr_number is None:
                with tracing.github_span("pr_publish", attempt_id=a.id):
                    pass
                self.ticket = t = self.store.update_ticket(t.id, pr_number=40 + t.id)
        else:
            self.move(lifecycle.ChecksFailed(t.id))
        return a, marked

    def review(self, a, *, coverage, answer=None, verdict, tag=None, note):
        """One AI review: checks 1-4 as plain code, check 5 asks the model only when no
        hard finding (the real reviewer's order and rows)."""
        t = self.ticket
        variant = f"{REVIEWER}/{REVIEW_PROMPT}"
        results = {
            "tests": C.TestsCheck("passed", True, command="pytest -q", tests_run=6),
            "changed_line_coverage": coverage,
            "test_revert": C.RevertCheck("passed", True, tests=("tests/test_greet.py",)),
            "run_app": C.RunAppCheck("passed", "yes", command="greet --help"),
        }
        findings = C.hard_findings(*results.values())
        with (
            tracing.review_span(
                -a.id, attempt_id=a.id, reviewer_model=REVIEWER, variant_id=variant
            ) as rs,
            tracing.Recorder(
                self.rec_dir,
                t.id,
                a.id,
                role="review",
                base_commit=BASE,
                model=REVIEWER,
                variant_id=variant,
                head_commit=HEAD,
            ) as rec,
        ):
            rs.set_attribute("review.shadow", False)
            for name, result in results.items():
                with tracing.review_check_span(name) as s:
                    s.set_attribute("check.status", result.status)
                    s.set_attribute("check.summary", result.summary())
                    rec.tool(
                        name=f"review.{name}",
                        input={"command": getattr(result, "command", None) or ""},
                        content=result.summary(),
                        tool_kind="review_check",
                    )
            with tracing.review_check_span("read_diff") as s:
                if findings:
                    s.set_attribute("check.status", "skipped")
                    s.set_attribute("check.reason", "a hard check failed: " + findings[0].check)
                else:
                    rec.user(f"Issue: {t.title}\n\n{t.request}\n\n(diff)", by="reviewer")
                    self.llm(
                        rec,
                        a.id,
                        model=REVIEWER,
                        text=answer,
                        tin=2400,
                        tout=80,
                        cost=0.0123,
                        pv=REVIEW_PROMPT,
                        review=-a.id,
                    )
                    s.set_attribute("check.status", "ok")
            tracing.set_verdict(rs, verdict)
            if tag:
                rs.set_attribute("review.tag", tag)
            rv = self.store.record_review(
                a.id,
                reviewer_model=REVIEWER,
                verdict=verdict,
                variant_id=variant,
                tests_passed=True,
                changed_line_coverage=coverage.percent,
                tests_nonvacuous=True,
                ran_app="yes",
                tag=tag,
                note=note,
                tokens_in=0 if findings else 2400,
                tokens_out=0 if findings else 80,
                cost_usd=0.0 if findings else 0.0123,
            )
            if verdict == "request_changes":
                self.store.record_kickback(a.id, by="reviewer", tag=tag, note=note)
                self.store.set_attempt_outcome(a.id, "kicked_back")
            rs.set_attribute("review.id", rv.id)
            rec.close(verdict, output=f"AI review: {verdict}\n\n{note}", tag=tag)
        self.move(lifecycle.AIReviewed(t.id, verdict))

    def user_kickback(self, a, *, tag, note, failure_step=None, index=None):
        t = self.ticket
        with tracing.github_span("review_received", attempt_id=a.id, pr_number=t.pr_number) as s:
            tracing.add_kickback(s, attempt_id=a.id, by="user", tag=tag, note=note)
        self.store.record_kickback(
            a.id, by="user", tag=tag, note=note, failure_step=failure_step, failure_step_index=index
        )
        self.store.set_attempt_outcome(a.id, "kicked_back")
        self.move(
            lifecycle.ReviewSubmitted(
                REPO,
                t.issue_number,
                t.id,
                t.pr_number,
                900 + a.id,
                "maintainer",
                "changes",
                note,
                kickback_tag=tag,
            )
        )

    def merge(self, a):
        t = self.ticket
        self.move(
            lifecycle.ReviewSubmitted(
                REPO, t.issue_number, t.id, t.pr_number, 990, "maintainer", "approve", "LGTM"
            )
        )
        with tracing.github_span("pr_merged", pr_number=t.pr_number):
            pass
        self.store.set_accepted_attempt(t.id, a.id)
        self.move(lifecycle.Merged(REPO, t.issue_number, t.id, t.pr_number))


GREET_PY = 'def greet(name):\n    return f"Hello, {name}"\n'
COV_GAP = C.CoverageCheck(
    "failed",
    60.0,
    measured_lines=10,
    covered_lines=6,
    uncovered_functions=("greet.py:verbose_banner",),
)
COV_FULL = C.CoverageCheck("passed", 100.0, measured_lines=12, covered_lines=12)


def _escalation_ticket(g: _Gen) -> None:
    """Ticket 1: classified small → tier 1; checks fail; retry passes; the AI reviewer
    kicks back (a hard finding, no model call); the next attempt is approved by the
    reviewer, then kicked back by the user as wrong (failure step marked); escalated to
    tier 2, approved and merged."""
    st = g.store
    title, body = "Add a --verbose flag to greet", "Print the greeting twice when --verbose is set."
    g.move(lifecycle.TicketRequested(REPO, 7, title, body, "maintainer", 1))
    g.ticket = t = st.create_ticket(
        repo=REPO, issue_number=7, title=title, request=body, base_branch="main"
    )
    with tracing.ticket_span(t.id, title=title, repo=REPO, issue_number=7) as tt:
        g.ticket = t = st.update_ticket(t.id, trace_id=tt.trace_id)
        reason = (
            "rules: router-v1 classified the issue as small -> tier 1 (classifier inkling-2-mini)"
        )
        with tracing.route_span(2, "routing") as rs:
            tracing.record_llm_call(  # the classifier call, as the proxy traces it
                model="inkling-2-mini",
                tokens_in=310,
                tokens_out=1,
                latency_ms=120.0,
                cost_usd=0.0,
                prompt_version="router-v1@267fee05",
                input="(router prompt)",
                output="small",
            )
            rs.set_attributes(
                {
                    "route.tier": 1,
                    "route.reason": reason,
                    "route.source": "classifier",
                    "route.label": "small",
                    "route.requested_tier": 1,
                    "route.has_model": True,
                }
            )
        g.ticket = t = st.update_ticket(t.id, routed_tier=1, route_reason=reason)
        read = ("read", "read", {"filePath": "greet.py"}, GREET_PY)
        edit = ("edit", "edit", {"filePath": "greet.py"}, "Edited greet.py (+6 -1)")
        test_fail = ("bash", "shell", {"command": "pytest -q"}, "1 failed, 5 passed")
        test_ok = ("bash", "shell", {"command": "pytest -q"}, "6 passed")
        g.attempt(
            tt.trace_id,
            model="inkling-2-mini",
            tier=1,
            trigger="first",
            turns=[
                ("Reading the command.", [read]),
                ("Adding the flag.", [edit, test_fail]),
                ("Done; one test still fails.", []),
            ],
            passed=False,
            verify=(6, 1, "FAILED tests/test_greet.py::test_verbose"),
        )
        a2, _ = g.attempt(
            tt.trace_id,
            model="inkling-2-mini",
            tier=1,
            trigger="first",
            turns=[("Fixing the failing test.", [read, edit, test_ok]), ("Done.", [])],
            passed=True,
            verify=(6, 0, ""),
        )
        g.review(
            a2,
            coverage=COV_GAP,
            verdict="request_changes",
            tag="incomplete",
            note="New code that no test executes: greet.py:verbose_banner. Add tests that call it.",
        )
        a3, idx = g.attempt(
            tt.trace_id,
            model="inkling-2-mini",
            tier=1,
            trigger="ai_kickback",
            turns=[("Adding a test for the banner.", [read, edit, test_ok]), ("Done.", [])],
            passed=True,
            verify=(7, 0, ""),
            tag_row=("edit", "edit"),
        )
        g.review(
            a3,
            coverage=COV_FULL,
            answer=APPROVE,
            verdict="approve",
            note="Does what the issue asks.",
        )
        g.user_kickback(
            a3,
            tag="wrong",
            note="It should print the banner, not repeat the greeting.",
            failure_step="wrong_edit",
            index=idx,
        )
        a4, _ = g.attempt(
            tt.trace_id,
            model="qwen3.8-27b",
            tier=2,
            trigger="escalation",
            turns=[
                ("Reading the issue again.", [read]),
                ("Printing a banner.", [edit, test_ok]),
                ("Done.", []),
            ],
            passed=True,
            verify=(7, 0, ""),
        )
        g.review(
            a4,
            coverage=COV_FULL,
            answer=APPROVE,
            verdict="approve",
            note="Does what the issue asks.",
        )
        g.merge(a4)


def _hostile_ticket(g: _Gen) -> None:
    """Ticket 2: markup and a script tag in the issue, the model's answer and a long tool
    output; routed by a tier:2 label (the real router's override path); one attempt,
    approved and merged."""
    st = g.store
    title = f"Escape the title {HOSTILE}"
    body = f"The page shows {HOSTILE} unescaped."
    g.move(lifecycle.TicketRequested(REPO, 8, title, body, "maintainer", 2, tier_override=2))
    g.ticket = t = st.create_ticket(
        repo=REPO, issue_number=8, title=title, request=body, base_branch="main"
    )
    with tracing.ticket_span(t.id, title=title, repo=REPO, issue_number=8) as tt:
        g.ticket = t = st.update_ticket(t.id, trace_id=tt.trace_id)
        models = [
            ModelEndpoint(
                r.model, r.tier, "local", r.model, "http://127.0.0.1:1/v1", r.model.split("-")[0]
            )
            for r in LADDER
        ]
        route = rules.route(t, config=models, override=rules.Override(2, "maintainer"))
        g.ticket = t = st.update_ticket(t.id, routed_tier=route.tier, route_reason=route.reason)
        read = ("read", "read", {"filePath": "page.html"}, f"<h1>{HOSTILE}</h1>")
        grep = ("grep", "search", {"pattern": "<script>"}, LONG_OUTPUT)
        a, _ = g.attempt(
            tt.trace_id,
            model="qwen3.8-27b",
            tier=2,
            trigger="first",
            turns=[(f"I will escape {HOSTILE}.", [read, grep]), ("Escaped with html.escape.", [])],
            passed=True,
            verify=(3, 0, ""),
        )
        g.review(a, coverage=COV_FULL, answer=APPROVE, verdict="approve", note=f"Fine. {HOSTILE}")
        g.merge(a)


def make_cases(root: Path) -> None:
    """Write the synthetic cases under ``root``: ``recordings/<ticket>/*.jsonl`` (plain
    JSONL, gunzipped from the real recorder's output), ``traces/<ticket>/<trace>.jsonl``
    and ``rows/<ticket>.json`` (``replay.rows_to_json`` of the store rows)."""
    for sub in ("recordings", "traces", "rows"):
        shutil.rmtree(root / sub, ignore_errors=True)
    rec_dir, traces = root / "recordings", root / "traces"
    work = root / ".work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    clock = _Clock()
    store = Store(work / "store.db", clock=clock)
    # Each migration's applied_at ticks the clock; pin the time after migrating to what
    # the checked-in cases were generated with (5 migrations), so a new migration (006,
    # #45) does not shift every timestamp in them.
    clock.t = datetime(2026, 1, 5, 9, 5, tzinfo=UTC)
    try:
        with tracing.capture_spans(traces):
            for build in (_escalation_ticket, _hostile_ticket):
                build(_Gen(store, rec_dir))
        (root / "rows").mkdir()
        for t in store.list_tickets():
            rows = replay.ticket_rows(store, t.id, ladder=LADDER)
            (root / "rows" / f"{t.id}.json").write_text(
                json.dumps(replay.rows_to_json(rows), indent=1, sort_keys=True) + "\n"
            )
    finally:
        store.close()
        shutil.rmtree(work, ignore_errors=True)
    for gz in rec_dir.rglob("*.jsonl.gz"):
        gz.with_suffix("").write_bytes(gzip.decompress(gz.read_bytes()))
        gz.unlink()


# =========================================================================== fixtures


@pytest.fixture
def cases(tmp_path: Path) -> Path:
    """A copy of the checked-in cases, with each recording gzipped as the recorder
    leaves a finished attempt (``<attempt>.jsonl.gz``)."""
    root = tmp_path / "cases"
    shutil.copytree(CASES, root, ignore=shutil.ignore_patterns("README.md"))
    for p in (root / "recordings").rglob("*.jsonl"):
        p.with_name(p.name + ".gz").write_bytes(gzip.compress(p.read_bytes()))
        p.unlink()
    return root


def _rows(root: Path, ticket_id: int) -> replay.TicketRows:
    data = json.loads((root / "rows" / f"{ticket_id}.json").read_text())
    return replay.rows_from_json(
        data, recordings_dir=root / "recordings", traces_dir=root / "traces"
    )


TICKETS = (1, 2)


# =========================================================================== the cases


def test_checked_in_cases_are_small_synthetic_and_documented() -> None:
    files = [p for p in CASES.rglob("*") if p.is_file()]
    assert (CASES / "README.md").is_file()
    assert sum(p.stat().st_size for p in files) < 300_000
    assert {p.suffix for p in files} <= {".jsonl", ".json", ".md"}
    bad = re.compile(
        r"/Users/|/home/|C:\\\\|ghp_|gho_|github_pat_|\bsk-[A-Za-z0-9]|AKIA[0-9A-Z]{12}"
        r"|PRIVATE KEY|xox[abprs]-|Bearer [A-Za-z0-9]"
    )
    for p in files:
        hits = bad.findall(p.read_text(encoding="utf-8"))
        assert not hits, f"{p.relative_to(CASES)} has {hits}"
    assert replay.recorded_tickets(CASES / "recordings") == list(TICKETS)


def _normal_rows(rec_dir: Path, tid: int) -> list[Any]:
    out = []
    for r in replay.load_ticket(rec_dir, tid):
        rows = [r.header, *r.record.rows]
        out.append(
            [
                {
                    k: v
                    for k, v in row.items()
                    if k
                    not in ("ts", "started_at", "total_ms", "trace_id", "latency_ms", "tool_use_id")
                }
                for row in rows
            ]
        )
    return out


def test_checked_in_cases_match_the_generator(tmp_path: Path) -> None:
    """Provenance: the checked-in files are what ``make_cases`` writes (modulo ids and
    timestamps), so they are synthetic and in today's formats."""
    make_cases(tmp_path)
    for tid in TICKETS:
        assert _normal_rows(tmp_path / "recordings", tid) == _normal_rows(CASES / "recordings", tid)
        assert json.loads((tmp_path / "rows" / f"{tid}.json").read_text()) | {
            "ticket": None
        } == json.loads((CASES / "rows" / f"{tid}.json").read_text()) | {"ticket": None}
        assert not replay.diff_trees(
            replay.recorded_span_tree(tmp_path / "traces", tid),
            replay.recorded_span_tree(CASES / "traces", tid),
        )


# =========================================================================== Verify bullet 1


@pytest.mark.parametrize("tid", TICKETS)
def test_replaying_a_recording_reproduces_its_span_tree_exactly(cases: Path, tid: int) -> None:
    replayed = replay.replay_spans(cases / "recordings", cases / "traces", tid)
    recorded = replay.recorded_span_tree(cases / "traces", tid)
    assert replay.diff_trees(replayed, recorded) == []
    assert replay.tree_shape(replayed) == replay.tree_shape(recorded)
    # The content really came from the recording, not from the span file.
    made = [
        n for _, n in replay.walk(replayed) if n.source == "replayed" and n.row_index is not None
    ]
    tools = [n for _, n in replay.walk(replayed) if n.name == "tool.call"]
    assert tools and all(n in made for n in tools)
    assert len([n for n in made if n.name == "llm.call"]) == sum(
        len(r.record.llm_rows()) for r in replay.load_ticket(cases / "recordings", tid)
    )
    # Same shape as the tracing module's own view of those spans.
    assert replay.tree_shape(recorded)[0] == (0, "ticket")


def test_the_span_tree_layout_is_spec_s(cases: Path) -> None:
    shape = replay.tree_shape(replay.replay_spans(cases / "recordings", cases / "traces", 1))
    top = [name for depth, name in shape if depth == 1]
    assert top[0] == "route" and top.count("attempt") == 4 and top.count("review") == 3
    assert "github" in top
    review_kids = []
    for i, (depth, name) in enumerate(shape):
        if name == "review" and depth == 1:
            kids = []
            for d, n in shape[i + 1 :]:
                if d <= 1:
                    break
                if d == 2:
                    kids.append(n)
            review_kids.append(kids)
    assert review_kids[0] == ["review.check"] * 5  # hard finding: no model call
    assert review_kids[1] == ["review.check"] * 5 + ["llm.call"]


def test_a_lost_or_changed_recording_row_shows_as_a_tree_difference(cases: Path) -> None:
    path = cases / "recordings" / "1" / "3.jsonl.gz"
    lines = gzip.decompress(path.read_bytes()).decode().splitlines()
    tool = next(i for i, ln in enumerate(lines) if json.loads(ln)["kind"] == "tool")
    changed = json.loads(lines[tool])
    changed["content"] = "something else"
    path.write_bytes(
        gzip.compress(
            ("\n".join([*lines[:tool], json.dumps(changed), *lines[tool + 1 :]]) + "\n").encode()
        )
    )
    recorded = replay.recorded_span_tree(cases / "traces", 1)
    diffs = replay.diff_trees(
        replay.replay_spans(cases / "recordings", cases / "traces", 1), recorded
    )
    assert any("output.value" in d for d in diffs)
    del lines[tool]
    path.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    diffs = replay.diff_trees(
        replay.replay_spans(cases / "recordings", cases / "traces", 1), recorded
    )
    assert diffs


def test_replay_works_from_recordings_alone(cases: Path) -> None:
    roots = replay.replay_spans(cases / "recordings", None, 1)
    shape = replay.tree_shape(roots)
    assert shape[0] == (0, "ticket")
    assert [n for d, n in shape if d == 1] == [
        "attempt",
        "attempt",
        "review",
        "attempt",
        "review",
        "attempt",
        "review",
    ]
    assert sum(1 for _, n in shape if n == "tool.call") == 12


def test_replay_calls_no_model_opens_no_socket_and_writes_nothing(
    cases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*a: Any, **k: Any) -> None:
        raise AssertionError("replay tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    before = {p: p.stat().st_mtime_ns for p in cases.rglob("*")}
    for tid in TICKETS:
        replay.replay_spans(cases / "recordings", cases / "traces", tid)
        replay.timeline(cases / "recordings", tid, traces_dir=cases / "traces")
        replay.regress(_rows(cases, tid))
    assert {p: p.stat().st_mtime_ns for p in cases.rglob("*")} == before


# =========================================================================== Verify bullet 2


@pytest.mark.parametrize("tid", TICKETS)
def test_the_current_logic_agrees_with_the_recorded_set(cases: Path, tid: int) -> None:
    reg = replay.regress(_rows(cases, tid))
    assert reg.diffs == ()
    assert reg.agrees
    assert reg.decisions >= (8 if tid == 1 else 4)


def test_a_deliberate_escalation_rule_change_shows_as_a_decision_diff(
    cases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change the rule "checks fail → retry once on the same tier, then move up" to "move
    up on the first failure": the recorded ticket 1 retried on tier 1, so the replay must
    say so, with no model call."""
    monkeypatch.setattr(escalation, "MAX_CHECK_ATTEMPTS", 1)
    reg = replay.regress(_rows(cases, 1))
    assert not reg.agrees
    first = reg.diffs[0]
    assert (first.kind, first.at) == ("next_attempt", "after attempt 1")
    assert first.recorded == "first on tier 1 (inkling-2-mini)"
    assert first.current == "escalation on tier 2 (qwen3.8-27b)"
    assert any("decisions checked, " in line and "differ" in line for line in reg.lines())
    # Ticket 2 never failed its checks: the change does not touch it.
    assert replay.regress(_rows(cases, 2)).agrees


def test_a_ladder_change_shows_as_a_decision_diff(cases: Path) -> None:
    rows = _rows(cases, 1)
    no_tier2 = replace_ladder(rows, (LADDER[0], LADDER[2], LADDER[3]))
    reg = replay.regress(no_tier2)
    assert any(d.kind == "next_attempt" and "tier 3 (glm-5)" in d.current for d in reg.diffs)


def replace_ladder(rows: replay.TicketRows, ladder: tuple[Rung, ...]) -> replay.TicketRows:
    import dataclasses

    return dataclasses.replace(rows, ladder=ladder)


def test_a_router_change_shows_as_a_route_diff(
    cases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rules, "LABEL_TIERS", {"small": 2, "normal": 2, "large": 3})
    reg = replay.regress(_rows(cases, 1))
    assert reg.diffs[0].kind == "route"
    assert reg.diffs[0].current.startswith("tier 2")
    # The tier:2 override on ticket 2 asks no classifier: unaffected.
    assert replay.regress(_rows(cases, 2)).agrees


def test_a_review_policy_change_shows_as_a_review_diff(
    cases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(C, "hard_findings", lambda *a: [])
    reg = replay.regress(_rows(cases, 1))
    assert [d.kind for d in reg.diffs] == ["review"]
    assert reg.diffs[0].at == "of attempt 2"
    assert reg.diffs[0].recorded == "request_changes (incomplete)"


def test_a_lifecycle_change_shows_as_a_lifecycle_diff(
    cases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = lifecycle.apply

    def merge_only_from_testing(column, event):
        if isinstance(event, lifecycle.Merged):
            return lifecycle.Transition("testing")
        return real(column, event)

    monkeypatch.setattr(lifecycle, "apply", merge_only_from_testing)
    reg = replay.regress(_rows(cases, 1))
    assert [(d.kind, d.recorded, d.current) for d in reg.diffs] == [
        ("lifecycle", "column done", "column testing")
    ]


def _store_ticket(tmp_path: Path) -> tuple[Store, Any]:
    store = Store(tmp_path / "w.db", clock=_Clock())
    t = store.create_ticket(repo=REPO, issue_number=3, title="t", request="r", base_branch="main")
    t = store.update_ticket(t.id, routed_tier=1)
    return store, t


def test_histories_are_built_like_worker_history(tmp_path: Path) -> None:
    """Set-aside attempts (``worker:uncounted:<ticket>``) and the attempt a rebase replaced
    are left out of the history the way ``Worker.history()`` leaves them out."""
    store, t = _store_ticket(tmp_path)
    a1 = store.add_attempt(t.id, model="inkling-2-mini", tier=1, trigger="first")
    store.finish_attempt(a1.id, "failed_checks", tests_passed=False)
    paused = []
    for _ in range(4):  # four spend pauses: more than the interrupted cap
        p = store.add_attempt(t.id, model="inkling-2-mini", tier=1, trigger="first")
        store.finish_attempt(p.id, "superseded", tests_passed=False)
        paused.append(p.id)
    store.set_cursor(REPO, json.dumps(paused), f"worker:uncounted:{t.id}")
    a6 = store.add_attempt(t.id, model="inkling-2-mini", tier=1, trigger="first")
    store.finish_attempt(a6.id, "failed_checks", tests_passed=False)
    a7 = store.add_attempt(t.id, model="qwen3.8-27b", tier=2, trigger="escalation")
    store.finish_attempt(a7.id, None, tests_passed=True)
    rb = store.add_attempt(t.id, model="qwen3.8-27b", tier=2, trigger="rebase")
    store.finish_attempt(rb.id, None, tests_passed=True)
    store.set_column(t.id, "testing")
    rows = replay.ticket_rows(store, t.id, ladder=LADDER)
    store.close()
    assert rows.uncounted == set(paused)
    assert [a.id for a in rows.counted()] == [a1.id, a6.id, rb.id]  # rb replaced a7
    reg = replay.regress(rows)
    assert reg.agrees, reg.lines()
    # Non-vacuous: counting the set-aside attempts hands the ticket over after the fourth.
    import dataclasses

    naive = replay.regress(dataclasses.replace(rows, uncounted=frozenset()))
    assert [d.current for d in naive.diffs] == ["hand to user (interrupted)"]


def test_rows_round_trip_through_json(cases: Path, tmp_path: Path) -> None:
    rows = _rows(cases, 1)
    again = replay.rows_from_json(json.loads(json.dumps(replay.rows_to_json(rows))))
    assert again.attempts == rows.attempts and again.kickbacks == rows.kickbacks
    assert again.reviews == rows.reviews and again.ticket == rows.ticket
    assert rows.route == replay.RouteInput(answer="small")
    assert _rows(cases, 2).route == replay.RouteInput(override_tier=2)
    first_review = rows.reviews[0]
    assert "verbose_banner" in rows.review_inputs[first_review.id].checks["changed_line_coverage"]
    assert rows.review_inputs[rows.reviews[1].id].answers == (APPROVE,)


# =========================================================================== timeline


def test_timeline_steps_carry_row_indexes_and_the_failure_step(cases: Path) -> None:
    rows = _rows(cases, 1)
    tl = replay.timeline(cases / "recordings", 1, traces_dir=cases / "traces", rows=rows)
    assert [(r.attempt_id, r.role) for r in tl.recordings] == [
        (1, "attempt"),
        (2, "attempt"),
        (2, "review"),
        (3, "attempt"),
        (3, "review"),
        (4, "attempt"),
        (4, "review"),
    ]
    a1 = tl.recordings[0]
    assert [s.kind for s in a1.steps] == [
        "prompt",
        "model",
        "tool",
        "model",
        "tool",
        "tool",
        "model",
        "verify",
        "result",
    ]
    rec = replay.load_ticket(cases / "recordings", 1)[0].record
    for s in a1.steps:
        if s.kind not in ("verify", "result"):
            assert rec.rows[s.row_index]["seq"] == s.row_index + 1
    assert a1.steps[-2].title == "Verify: failed" and "tests_failed: 1" in a1.steps[-2].text
    assert a1.tokens_in == 1050 + 1200 + 1350 and a1.trigger == "first"
    a3 = tl.recordings[3]
    (marked,) = a3.failure_steps
    kick = next(k for k in rows.kickbacks if k.by == "user")
    assert marked.row_index == kick.failure_step_index - 1  # #27 stores the 1-based seq
    assert marked.failure_step == "wrong_edit" and marked.title == "Tool edit (edit)"
    assert a3.steps[-1].kind == "kickback" and "wrong_edit" in a3.steps[-1].title
    review = tl.recordings[2]
    assert [s.kind for s in review.steps] == ["check"] * 4 + ["verdict"]


def test_long_outputs_are_bounded_in_the_timeline(cases: Path) -> None:
    tl = replay.timeline(cases / "recordings", 2)
    grep = next(s for s in tl.recordings[0].steps if s.title.startswith("Tool grep"))
    assert len(grep.text) == replay.STEP_TEXT_LIMIT
    assert grep.clipped == len(LONG_OUTPUT) - replay.STEP_TEXT_LIMIT


def test_replay_ticket_report(cases: Path, tmp_path: Path) -> None:
    rep = replay.ReplayReport(
        1,
        replay.timeline(cases / "recordings", 1, rows=_rows(cases, 1)),
        (),
        replay.regress(_rows(cases, 1)),
    )
    text = "\n".join(rep.lines())
    assert "failure step wrong_edit at row" in text
    assert "span tree: replay matches the trace" in text
    assert "the current logic agrees with history" in text
    # Through the store, as `fman replay` will call it.
    store, _t = _store_ticket(tmp_path)
    report = replay.replay_ticket(
        1,
        recordings_dir=cases / "recordings",
        traces_dir=cases / "traces",
        store=store,
        ladder=LADDER,
    )
    assert report.tree_diffs == () and report.regression is not None
    store.close()


# =========================================================================== the viewer


@pytest.fixture
def client(cases: Path, tmp_path: Path) -> TestClient:
    rows = {tid: _rows(cases, tid) for tid in TICKETS}
    routes = trace_view.trace_routes(cases / "recordings", cases / "traces", rows_for=rows.get)
    app = create_app(tmp_path / "none.db", extra_routes=routes, nav=trace_view.NAV)
    return TestClient(app, base_url=BASE_URL)


def test_list_page(client: TestClient) -> None:
    r = client.get("/traces")
    assert r.status_code == 200
    assert 'href="/traces/1"' in r.text and 'href="/traces/2"' in r.text
    assert 'href="/traces">Traces</a>' in r.text
    assert "<script" not in r.text


def test_ticket_page_shows_the_timeline_and_marks_the_failure_step(client: TestClient) -> None:
    r = client.get("/traces/1")
    assert r.status_code == 200
    assert r.text.count('<details class="recording"') == 7
    assert "The current logic agrees with history" in r.text
    assert "Replayed span tree: identical to the trace" in r.text
    (li,) = re.findall(r'<li id="a3-\d+" class="[^"]*failure[^"]*"><details open>', r.text)
    assert "failure step: wrong_edit" in r.text
    assert "Verify: failed" in r.text and "Verdict: request_changes (incomplete)" in r.text


def test_ticket_page_escapes_everything_from_recordings(client: TestClient) -> None:
    r = client.get("/traces/2")
    assert r.status_code == 200
    assert "<script" not in r.text and "<img" not in r.text and "<b>bold" not in r.text
    assert "&lt;script&gt;alert(&quot;pwned&quot;)&lt;/script&gt;" in r.text
    assert "&lt;/pre&gt;" in r.text  # a tool output cannot close the <pre> it sits in
    assert "[clipped: " in r.text and "more characters not shown]" in r.text
    assert not re.search(r"(src|href)=\"?(https?:)?//", r.text)  # nothing fetched


def test_ticket_page_shows_a_decision_diff(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(escalation, "MAX_CHECK_ATTEMPTS", 1)
    r = client.get("/traces/1")
    assert "decisions differ from what was recorded" in r.text
    assert "escalation on tier 2 (qwen3.8-27b)" in r.text


def test_attempt_page(client: TestClient) -> None:
    r = client.get("/traces/1/3")
    assert r.status_code == 200
    assert r.text.count('<details class="recording"') == 2  # attempt 3 and its review
    assert client.get("/traces/1/99").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        # Literal "/../" segments never reach the server (clients resolve them); the
        # encoded forms do, decoded to "..", and must be refused.
        "/traces/%2E%2E",
        "/traces/..%2F..%2Fetc",
        "/traces/%2E%2E/1",
        "/traces/1/%2E%2E",
        "/traces/1/%2E%2E%2F2",
        "/traces/-1",
        "/traces/+1",
        "/traces/%201",
        "/traces/1_0",
        "/traces/0x1",
        "/traces/%D9%A1",  # Arabic-Indic digit one: int() would accept it
        "/traces/1/%D9%A1",
        "/traces/1234567890123456789",
        "/traces/3",  # a plain integer with no recordings
    ],
)
def test_ids_that_are_not_plain_integers_are_refused(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert "root:" not in r.text


def test_ids_are_checked_before_any_path_is_built(cases: Path) -> None:
    for bad in ("../1", "..", "1/../1", "\u0661", " 1", "1 ", "+1", -1, True, None, 1.0):
        assert replay.parse_id(bad) is None
        with pytest.raises(ValueError):
            replay.load_ticket(cases / "recordings", bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            replay.load_span_rows(cases / "traces", bad)  # type: ignore[arg-type]
    assert replay.parse_id("12") == 12 and replay.parse_id(12) == 12


def test_the_trace_view_is_local_only(client: TestClient) -> None:
    r = client.get("/traces/1", headers={"host": "evil.example"})
    assert r.status_code == 403


def test_the_trace_view_reads_a_real_store(cases: Path, tmp_path: Path) -> None:
    store, t = _store_ticket(tmp_path)
    store.close()
    routes = trace_view.trace_routes(
        cases / "recordings", cases / "traces", store_path=tmp_path / "w.db", ladder=LADDER
    )
    c = TestClient(
        create_app(tmp_path / "w.db", extra_routes=routes, nav=trace_view.NAV), base_url=BASE_URL
    )
    r = c.get("/traces/1")  # store ticket 1 is the hand-made one above
    assert r.status_code == 200 and "Ticket 1: t" in r.text
    assert c.get("/traces/2").status_code == 200  # no store row: timeline only
    assert "cannot be checked" in c.get("/traces/2").text


if __name__ == "__main__":
    if sys.argv[1:] == ["--regenerate"]:
        make_cases(CASES)
        print(f"wrote {CASES}")
    else:
        print(__doc__)
