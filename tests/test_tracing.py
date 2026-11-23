"""Tracing core and run recorder (#5)."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import socket
import threading
import time
from pathlib import Path

import fde_kit.tracing as fde_tracing
import pytest
from fde_kit.record import ReplayProvider, RunRecord
from opentelemetry import trace

from foreman_inator.tracing import (
    CONTENT_LIMIT,
    Recorder,
    add_kickback,
    attempt_span,
    attempt_span_id,
    capture_spans,
    get_recorder,
    github_span,
    load_recording,
    otel,
    prune,
    record_llm_call,
    record_tool_call,
    recording_path,
    review_check_span,
    review_span,
    root_span_id,
    route_span,
    set_verdict,
    set_verify_result,
    span_tree,
    ticket_span,
    verify_span,
)


@pytest.fixture
def spans():
    with capture_spans() as exporter:
        yield exporter


def _by_name(finished, name):
    return [s for s in finished if s.name == name]


def _hex_span(s) -> str:
    return trace.format_span_id(s.context.span_id)


# --------------------------------------------------------------------------- spans


def test_fake_attempt_has_the_spec_span_tree(spans):
    with ticket_span(7, title="Fix the thing", repo="me/toy", issue_number=12) as tk:
        with route_span(1, "small mechanical change", matched_tickets=[3, 4]):
            pass
        with attempt_span(
            41, variant_id="qwen3.6-35b/writer-v1", model="qwen3.6-35b", tier=1, trigger="first"
        ):
            # the proxy writes from another thread, knowing only the attempt id
            t = threading.Thread(
                target=record_llm_call,
                kwargs=dict(
                    attempt_id=41,
                    model="qwen3.6-35b",
                    tokens_in=100,
                    tokens_out=20,
                    latency_ms=812.5,
                    cost_usd=0.0,
                    prompt_version="writer-v1",
                    input=[{"role": "user", "content": "hi"}],
                    output="ok",
                ),
            )
            t.start()
            t.join()
            record_tool_call(
                attempt_id=41, name="read", kind="read", input={"path": "a.py"}, output="x" * 9000
            )
            with verify_span("uv run pytest -q") as v:
                set_verify_result(v, passed=False, tests_run=5, tests_failed=1, output="1 failed")
        with review_span(
            3, attempt_id=41, reviewer_model="glm-5", variant_id="glm-5/reviewer-v1"
        ) as r:
            for check in ("tests", "changed_line_coverage", "test_revert", "run_app", "read_diff"):
                with review_check_span(check, passed=True):
                    pass
            set_verdict(r, "request_changes")
        with github_span("review_received", attempt_id=41, pr_number=99) as g:
            add_kickback(g, attempt_id=41, by="reviewer", tag="incomplete", note="missed a case")

    finished = spans.get_finished_spans()
    assert span_tree(finished) == [
        (0, "ticket"),
        (1, "route"),
        (1, "attempt"),
        (2, "llm.call"),
        (2, "tool.call"),
        (2, "verify"),
        (1, "review"),
        (2, "review.check"),
        (2, "review.check"),
        (2, "review.check"),
        (2, "review.check"),
        (2, "review.check"),
        (1, "github"),
    ]
    assert all(s.attributes["ticket.id"] == 7 for s in finished)
    assert {trace.format_trace_id(s.context.trace_id) for s in finished} == {tk.trace_id}

    (root,) = _by_name(finished, "ticket")
    assert _hex_span(root) == root_span_id(tk.trace_id)
    assert root.attributes["ticket.issue_number"] == 12
    (route,) = _by_name(finished, "route")
    assert route.attributes["route.tier"] == 1
    assert tuple(route.attributes["route.matched_tickets"]) == (3, 4)
    (att,) = _by_name(finished, "attempt")
    assert att.attributes["variant_id"] == "qwen3.6-35b/writer-v1"
    assert _hex_span(att) == attempt_span_id(tk.trace_id, 41)
    (llm,) = _by_name(finished, "llm.call")
    assert llm.parent.span_id == att.context.span_id
    assert llm.attributes["llm.token_count.prompt"] == 100
    assert llm.attributes["llm.prompt_version"] == "writer-v1"
    assert llm.attributes["llm.cost_usd"] == 0.0
    (tool,) = _by_name(finished, "tool.call")
    assert len(tool.attributes["output.value"]) < CONTENT_LIMIT + 20  # truncated
    (ver,) = _by_name(finished, "verify")
    assert ver.attributes["verify.tests_failed"] == 1
    (rev,) = _by_name(finished, "review")
    assert rev.attributes["review.verdict"] == "request_changes"
    checks = [s.attributes["review.check"] for s in _by_name(finished, "review.check")]
    assert checks == ["tests", "changed_line_coverage", "test_revert", "run_app", "read_diff"]
    (gh,) = _by_name(finished, "github")
    assert [e.name for e in gh.events] == ["kickback"]
    assert gh.events[0].attributes["kickback.tag"] == "incomplete"
    assert [trace.format_span_id(lk.context.span_id) for lk in gh.links] == [_hex_span(att)]


def test_reopened_ticket_joins_the_same_trace(spans):
    with (
        ticket_span(8) as first,
        attempt_span(1, variant_id="v", model="m", tier=1, trigger="first"),
    ):
        pass
    stored = first.trace_id
    spans.clear()

    # days later, new process: only the stored trace id is known
    with capture_spans() as later, ticket_span(8, trace_id=stored) as again:
        assert again.resumed and again.trace_id == stored
        with attempt_span(2, variant_id="v", model="m", tier=1, trigger="kickback"):
            record_llm_call(
                attempt_id=2, model="m", tokens_in=1, tokens_out=1, latency_ms=1, cost_usd=0
            )
    finished = later.get_finished_spans()
    assert [s.name for s in finished] == ["llm.call", "attempt"]  # no second root
    (att,) = _by_name(finished, "attempt")
    assert trace.format_trace_id(att.context.trace_id) == stored
    assert trace.format_span_id(att.parent.span_id) == root_span_id(stored)
    assert all(s.attributes["ticket.id"] == 8 for s in finished)


def test_capture_spans_restores_the_previous_provider():
    before = otel._state.provider
    with capture_spans():
        assert otel._state.provider is not before
    assert otel._state.provider is before


def test_init_is_safe_with_no_trace_backend(tmp_path, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    monkeypatch.setattr(fde_tracing, "_initialised", False)
    monkeypatch.setattr(otel, "_state", otel._State())
    global_before = trace.get_tracer_provider()

    t0 = time.monotonic()
    otel.init(endpoint=f"http://127.0.0.1:{port}/v1/traces", traces_dir=tmp_path / "traces")
    with ticket_span(5) as tk, attempt_span(1, variant_id="v", model="m", tier=1, trigger="first"):
        pass
    otel._state.provider.force_flush()
    elapsed = time.monotonic() - t0

    assert elapsed < 3
    assert tk.trace_id != "0" * 32
    assert trace.get_tracer_provider() is global_before  # fde-kit set nothing global
    lines = (tmp_path / "traces" / "5" / f"{tk.trace_id}.jsonl").read_text().splitlines()
    assert sorted(json.loads(ln)["name"] for ln in lines) == ["attempt", "ticket"]


# --------------------------------------------------------------------------- recordings


def _record_attempt(rec_dir: Path, ticket_id: int = 7, attempt_id: int = 41) -> Path:
    with Recorder(
        rec_dir,
        ticket_id,
        attempt_id,
        trace_id="ab" * 16,
        base_commit="deadbeef",
        model="qwen3.6-35b",
        variant_id="qwen3.6-35b/writer-v1",
        tier=1,
    ) as rec:
        rec.user("Fix issue #12")
        rec.llm(
            text="I'll read a.py",
            tokens_in=100,
            tokens_out=10,
            stop_reason="tool_use",
            tool_calls=[{"id": "c1", "name": "read", "input": {"path": "a.py"}}],
            latency_ms=500,
            cost_usd=0.0,
            prompt_version="writer-v1",
        )
        rec.tool(name="read", input={"path": "a.py"}, content="y" * 10_000, tool_use_id="c1")
        rec.llm(text="Done.", tokens_in=200, tokens_out=5)
        return rec.close("failed_checks", tests_passed=False, diff_lines=3)


def test_recording_round_trips_through_fde_kit_runrecord(tmp_path):
    path = _record_attempt(tmp_path)
    assert path == recording_path(tmp_path, 7, 41) == tmp_path / "7" / "41.jsonl.gz"
    assert not (tmp_path / "7" / "41.jsonl").exists()
    with gzip.open(path, "rt") as fh:
        first = json.loads(fh.readline())
    assert next(iter(first)) == "kind"  # kind first

    record = load_recording(path)
    assert isinstance(record, RunRecord)
    assert record.run_id == "t7-a41"
    assert record.header["trace_id"] == "ab" * 16
    assert record.header["base_commit"] == "deadbeef"
    assert [r["kind"] for r in record.rows] == ["user", "llm", "tool", "llm", "result"]
    assert record.result["outcome"] == "failed_checks"
    assert record.result["turns"] == 2
    (tool,) = record.tool_rows()
    assert len(tool["content"]) == 10_000  # full in the recording
    assert Path(tool["output_file"]).read_text() == "y" * 10_000  # and next to it
    assert record.llm_rows()[0]["usage"] == {"input_tokens": 100, "output_tokens": 10}

    # fde-kit's replay machinery can serve the recorded model turns
    provider = ReplayProvider(record)
    resp = provider.complete(system="", messages=[], tools=[], max_tokens=10)
    assert resp.text == "I'll read a.py"
    assert resp.tool_calls[0].name == "read"


def test_recorder_is_shared_and_thread_and_task_safe(tmp_path):
    rec = Recorder(tmp_path, 9, 90, model="m")
    assert get_recorder(90) is rec

    def from_proxy() -> None:
        for i in range(200):
            get_recorder(90).llm(text=f"r{i}", tokens_in=1, tokens_out=1)

    async def from_driver() -> None:
        async def one(i: int) -> None:
            await asyncio.sleep(0)
            get_recorder(90).tool(name="shell", input={"i": i}, content="ok")

        await asyncio.gather(*(one(i) for i in range(200)))

    threads = [threading.Thread(target=from_proxy) for _ in range(2)]
    threads.append(threading.Thread(target=lambda: asyncio.run(from_driver())))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    path = rec.close("accepted")
    assert get_recorder(90) is None
    assert rec.llm(text="late", tokens_in=0, tokens_out=0) is None  # dropped, no crash

    record = load_recording(path)
    assert len(record.llm_rows()) == 400 and len(record.tool_rows()) == 200
    assert sorted(r["seq"] for r in record.rows[:-1]) == list(range(1, 601))
    assert sorted(r["turn"] for r in record.llm_rows()) == list(range(1, 401))


def test_recorder_closes_with_error_on_exception(tmp_path):
    with pytest.raises(RuntimeError), Recorder(tmp_path, 1, 2) as rec:
        rec.user("go")
        raise RuntimeError("boom")
    record = load_recording(recording_path(tmp_path, 1, 2))
    assert record.result["stopped_reason"] == "error"
    assert record.result["error_message"] == "boom"


def test_review_recording_sits_next_to_the_attempt(tmp_path):
    with Recorder(tmp_path, 7, 41, role="review", model="glm-5") as rec:
        assert get_recorder(41, role="review") is rec and get_recorder(41) is None
    assert load_recording(tmp_path / "7" / "41.review.jsonl.gz").run_id == "t7-a41-review"


# --------------------------------------------------------------------------- retention


def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_prune_keeps_merged_and_deletes_old_unmerged(tmp_path):
    rec_dir, traces_dir = tmp_path / "recordings", tmp_path / "traces"
    merged_old = _record_attempt(rec_dir, ticket_id=1, attempt_id=10)
    unmerged_old = _record_attempt(rec_dir, ticket_id=2, attempt_id=20)
    unmerged_new = _record_attempt(rec_dir, ticket_id=3, attempt_id=30)
    _age(merged_old, 91)
    _age(unmerged_old, 91)
    _age(unmerged_new, 89)
    (traces_dir / "2").mkdir(parents=True)
    old_trace = traces_dir / "2" / ("cd" * 16 + ".jsonl")
    old_trace.write_text("{}\n")
    _age(old_trace, 91)

    deleted = prune(rec_dir, merged={1}, traces_dir=traces_dir)

    assert merged_old.exists()
    assert not unmerged_old.exists()
    assert not (rec_dir / "2").exists()  # side files and the empty dir went too
    assert unmerged_new.exists()
    assert not old_trace.exists()
    assert unmerged_old in deleted and rec_dir / "2" / "20.files" in deleted

    # a callable works too
    _age(unmerged_new, 91)
    prune(rec_dir, merged=lambda t: t in {1, 3})
    assert unmerged_new.exists() and merged_old.exists()
