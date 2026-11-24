"""``fman export --redact`` (todo #30).

The fixture ticket below is written by the real code that writes recordings, spans and
store rows (``Recorder``, the span helpers inside ``capture_spans(traces_dir)``, a real
``Store``), and every free-text field carries its own sentinel: a random string made at
test time, some of them secret-shaped (assembled here, so no key-looking literal sits in
this file), plus a home path, a login and a repo name. The Verify check searches every
byte of the export (the decompressed archive, its tar headers, every member, gzipped
members decompressed) for every sentinel. Each class of field has a mutation that turns
its redaction off, and the same search must then find that class's sentinel: the check
is non-vacuous per class, inside the suite.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import string
import tarfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.trace import Status, StatusCode
from starlette.testclient import TestClient
from typer.testing import CliRunner

from foreman_inator import export, tracing
from foreman_inator.cli import app
from foreman_inator.db.store import Store
from foreman_inator.metrics import trace_view
from foreman_inator.metrics.web import create_app
from foreman_inator.review import checks as C
from foreman_inator.tracing import replay

CASES = Path(__file__).parent / "recordings"
KEEP = lambda r, v: v  # noqa: E731  (a rule that passes a value through: the mutations)


# =========================================================================== sentinels


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


@dataclass
class Sentinels:
    by_class: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add(self, cls: str, value: str | None = None) -> str:
        v = value if value is not None else f"Zq{cls.replace('_', '')}{secrets.token_hex(6)}Qz"
        self.by_class[cls].append(v)
        return v

    def secret(self, cls: str) -> str:
        """Secret-shaped values, assembled at test time (gitleaks scans this file)."""
        shapes = [
            "gh" + "p_" + _rand(36),
            "sk-" + "ant-" + _rand(40),
            "AK" + "IA" + _rand(16, string.ascii_uppercase + string.digits),
            "xox" + "b-" + _rand(12, string.digits) + "-" + _rand(24),
            "-----BEGIN " + "RSA PRIVATE" + " KEY-----" + _rand(40),
        ]
        return self.add(cls, secrets.choice(shapes))

    def all(self) -> list[tuple[str, str]]:
        return [(c, v) for c, vs in self.by_class.items() for v in vs]


@dataclass
class Seed:
    root: Path
    recordings: Path
    traces: Path
    db: Path
    ticket_id: int
    s: Sentinels
    outside: Path


class _Ns:
    def __init__(self) -> None:
        import time

        self._time = time
        self.last = 0

    def __call__(self) -> int:
        self.last = max(self._time.time_ns(), self.last + 1_000)
        return self.last


def seed_ticket(root: Path) -> Seed:
    """One ticket with a sentinel in every free-text field (see the module docstring)."""
    s = Sentinels()
    src = root / "src"
    rec_dir, traces, db = src / "recordings", src / "traces", src / "fman.db"
    ns = _Ns()
    src.mkdir(parents=True)
    login = s.add("login")
    owner = s.add("repo")
    repo_name = s.add("repo")
    repo = f"{owner}/{repo_name}"
    home_path = f"/Users/{login}/code/{repo_name}/src/{s.add('path')}.py"
    rel_path = f"src/{s.add('path')}.py"
    worktree = f"/Users/{login}/.foreman-inator/worktrees/{repo_name}/{s.add('path')}"
    long_tool = s.add("tool_output") + " " + s.secret("tool_output") + ("y" * 5000)
    long_tool += " " + s.add("side_file")  # only past the span's 4,000-char cut
    long_model = s.add("model_text") + ("x" * 5000) + " " + s.add("side_file")

    store = Store(db)
    try:
        with tracing.capture_spans(traces):
            t = store.create_ticket(
                repo=repo,
                issue_number=12,
                title=s.add("store_row") + " " + s.add("span_attr"),
                request=s.add("store_row") + " " + s.secret("store_row"),
                base_branch=s.add("branch"),
                owned_files=[home_path, rel_path],
            )
            with tracing.ticket_span(t.id, title=t.title, repo=repo, issue_number=12) as tt:
                t = store.update_ticket(
                    t.id, trace_id=tt.trace_id, route_reason=s.add("store_row"), routed_tier=1
                )
                with tracing.route_span(1, s.add("span_attr")):
                    tracing.record_llm_call(
                        model="inkling-2-mini",
                        tokens_in=300,
                        tokens_out=1,
                        latency_ms=100.0,
                        cost_usd=0.0,
                        prompt_version="router-v1@267fee05",
                        input=s.add("span_attr"),
                        output="small",
                    )
                with tracing.span("clarify") as cs:
                    cs.add_event(
                        "clarification",
                        {
                            "clarify.reason": s.add("span_event"),
                            "weird.key": s.add("span_event"),
                            "weird.count": 41,
                        },
                    )
                    cs.record_exception(ValueError(s.add("span_event") + " at " + home_path))
                    cs.set_status(Status(StatusCode.ERROR, s.add("span_attr")))
                _attempt(
                    store, t, s, rec_dir, traces, ns, home_path, worktree, long_tool, long_model
                )
                a1 = store.list_attempts(t.id)[0]
                _review(store, t, a1, s, rec_dir, rel_path)
                _shadow(store, t, a1, s, rec_dir)
                with tracing.github_span("review_received", attempt_id=a1.id, pr_number=55) as g:
                    tracing.add_kickback(
                        g,
                        attempt_id=a1.id,
                        by="user",
                        tag="wrong",
                        note=s.add("span_event") + f"\n{rel_path}:12 " + s.secret("span_event"),
                    )
                store.record_kickback(a1.id, by="user", tag="wrong", note=s.add("store_row"))
                store.flag_ticket(t.id, "checks_failed: " + s.add("store_row"))
                store.set_cursor(repo, "1", f"explore:{t.id}")
                _crashed(store, t, s, rec_dir, home_path)
    finally:
        store.close()
    # Files a ticket folder may hold that the export does not know: never copied.
    tdir = rec_dir / str(t.id)
    (tdir / "notes.txt").write_text(s.add("unknown_file"))
    (traces / str(t.id) / "scratch.json").write_text(s.add("unknown_file"))
    outside = root / "outside"
    outside.mkdir()
    (outside / "9.jsonl").write_text(
        json.dumps({"kind": "header", "secret": s.add("unknown_file")}) + "\n"
    )
    (tdir / "9.jsonl").symlink_to(outside / "9.jsonl")
    return Seed(root, rec_dir, traces, db, t.id, s, outside)


def _attempt(store, t, s, rec_dir, traces, ns, home_path, worktree, long_tool, long_model) -> None:
    commit = s.add("commit", secrets.token_hex(20))
    variant = "inkling-2-mini/writer-v1@0a1b2c3d"
    a = store.add_attempt(
        t.id,
        model="inkling-2-mini",
        tier=1,
        trigger="first",
        variant_id=variant,
        worktree=worktree,
        branch=f"feature/{s.add('branch')}",
        base_commit=commit,
    )
    with (
        tracing.attempt_span(
            a.id,
            variant_id=variant,
            model="inkling-2-mini",
            tier=1,
            trigger="first",
            branch=a.branch,
            base_commit=commit,
        ) as span,
        tracing.Recorder(
            rec_dir,
            t.id,
            a.id,
            trace_id=t.trace_id,
            base_commit=commit,
            model="inkling-2-mini",
            variant_id=variant,
            tier=1,
            opened_by=s.add("unknown_key"),
            secret_count=987654321,
        ) as rec,
    ):
        rec.user(
            "Issue: "
            + s.add("model_text")
            + "\nCLARIFICATION\nQ: "
            + s.add("model_text")
            + "\nA: "
            + s.secret("model_text"),
            by=s.add("login"),
        )

        def llm(text: str, calls: list[dict[str, Any]]) -> None:
            t0, t1 = ns(), ns()
            tracing.record_llm_call(
                attempt_id=a.id,
                model="inkling-2-mini",
                tokens_in=1000,
                tokens_out=50,
                latency_ms=(t1 - t0) / 1e6,
                cost_usd=0.0,
                prompt_version="writer-v1@0a1b2c3d",
                input=s.add("span_attr"),
                output=text,
                start_time_ns=t0,
                end_time_ns=t1,
            )
            rec.llm(
                text=text,
                tokens_in=1000,
                tokens_out=50,
                model="inkling-2-mini",
                stop_reason="tool_use" if calls else "end_turn",
                tool_calls=calls,
                request={
                    "model": "inkling-2-mini",
                    "stream": True,
                    "messages": [
                        {"role": "system", "content": s.add("model_text")},
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": s.add("model_text")}],
                        },
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "read",
                                "description": s.add("model_text"),
                                "parameters": {"x": s.add("model_text")},
                            },
                        }
                    ],
                    "extra_body": {"user": s.add("unknown_key"), "n": 3},
                },
                latency_ms=(t1 - t0) / 1e6,
                cost_usd=0.0,
                prompt_version="writer-v1@0a1b2c3d",
            )

        def tool(name, kind, inp, out, *, is_error=False) -> None:
            t0, t1 = ns(), ns()
            row = rec.tool(
                name=name,
                input=inp,
                content=out,
                is_error=is_error,
                tool_use_id="call_" + s.add("tool_output"),
                tool_kind=kind,
            )
            tracing.record_tool_call(
                attempt_id=a.id,
                name=name,
                input=inp,
                output=out,
                kind=kind,
                is_error=is_error,
                output_file=(row or {}).get("output_file"),
                start_time_ns=t0,
                end_time_ns=t1,
            )

        read_in = {"filePath": home_path, "offset": 10}
        llm(
            s.add("model_text") + " <think>" + s.secret("model_text") + "</think>",
            [{"id": "c1", "name": "read", "input": read_in}],
        )
        tool("read", "read", read_in, s.add("tool_output") + "\ndef handler(): ...")
        tool("grep", "search", {"pattern": s.add("tool_output")}, long_tool)
        tool(
            s.add("tool_output"),  # a tool name the model made up
            "other",
            {s.add("unknown_key"): s.add("unknown_key"), "limit": 3},
            s.add("tool_output"),
            is_error=True,
        )
        tool("bash", "shell", {"command": "pytest " + home_path}, s.secret("tool_output"))
        llm(long_model, [])
        (traces / str(t.id)).mkdir(parents=True, exist_ok=True)
        with tracing.verify_span("cd " + home_path + " && " + s.add("span_attr")) as v:
            tracing.set_verify_result(
                v, passed=False, tests_run=4, tests_failed=1, output=s.add("span_attr")
            )
        log = traces / str(t.id) / f"attempt-{a.id}-verify.log"
        log.write_text(s.add("log") + "\n" + home_path + "\n" + s.secret("log"))
        (traces / str(t.id) / f"attempt-{a.id}-ci.log").write_text(s.add("log"))
        store.finish_attempt(
            a.id,
            "failed_checks",
            tests_passed=False,
            diff_files=2,
            diff_lines=9,
            verify_output_path=str(log),
        )
        span.set_attribute("attempt.outcome", "failed_checks")
        rec.close(
            "failed_checks",
            tests_passed=False,
            diff_lines=9,
            head_commit=s.add("commit", secrets.token_hex(20)),
            error_message=s.add("model_text"),
        )


def _review(store, t, a, s, rec_dir, rel_path) -> None:
    variant = "claude-opus/reviewer-v1@9f8c1678"
    fn = s.add("check")
    results = {
        "tests": C.TestsCheck("passed", True, command="pytest " + s.add("check"), tests_run=4),
        "changed_line_coverage": C.CoverageCheck(
            "failed",
            50.0,
            measured_lines=4,
            covered_lines=2,
            uncovered_functions=(f"{rel_path}:{fn}",),
        ),
        "test_revert": C.RevertCheck("passed", True, tests=(rel_path,)),
        "run_app": C.RunAppCheck("passed", "yes", command=s.add("check") + " --help"),
    }
    note = s.add("review_note") + " " + s.secret("review_note")
    answer = json.dumps({"verdict": "request_changes", "tag": "incomplete", "note": note})
    with (
        tracing.review_span(
            -a.id, attempt_id=a.id, reviewer_model="claude-opus", variant_id=variant
        ) as rs,
        tracing.Recorder(
            rec_dir,
            t.id,
            a.id,
            role="review",
            model="claude-opus",
            variant_id=variant,
            base_commit=a.base_commit,
            head_commit=s.add("commit", secrets.token_hex(20)),
        ) as rec,
    ):
        rs.set_attribute("review.shadow", False)
        for name, res in results.items():
            with tracing.review_check_span(name) as cs:
                cs.set_attribute("check.status", res.status)
                cs.set_attribute("check.summary", res.summary())
                rec.tool(
                    name=f"review.{name}",
                    input={"command": getattr(res, "command", None) or ""},
                    content=res.summary(),
                    tool_kind="review_check",
                )
        with tracing.review_check_span("read_diff") as cs:
            rec.user(
                "Issue: " + s.add("model_text") + "\n(diff)\n" + s.add("model_text"), by="reviewer"
            )
            tracing.record_llm_call(
                attempt_id=a.id,
                review_id=-a.id,
                model="claude-opus",
                tokens_in=2000,
                tokens_out=60,
                latency_ms=5.0,
                cost_usd=0.02,
                prompt_version="reviewer-v1@9f8c1678",
                input=s.add("span_attr"),
                output=answer,
            )
            rec.llm(
                text=answer,
                tokens_in=2000,
                tokens_out=60,
                model="claude-opus",
                cost_usd=0.02,
                prompt_version="reviewer-v1@9f8c1678",
            )
            cs.set_attribute("check.status", "ok")
            cs.set_attribute("check.reason", s.add("check"))
        tracing.set_verdict(rs, "request_changes")
        rs.set_attribute("review.tag", "incomplete")
        rv = store.record_review(
            a.id,
            reviewer_model="claude-opus",
            verdict="request_changes",
            variant_id=variant,
            tests_passed=True,
            changed_line_coverage=50.0,
            tests_nonvacuous=True,
            ran_app="yes",
            tag="incomplete",
            note=s.add("store_row"),
            tokens_in=2000,
            tokens_out=60,
            cost_usd=0.02,
        )
        store.record_kickback(a.id, by="reviewer", tag="incomplete", note=s.add("store_row"))
        rs.set_attribute("review.id", rv.id)
        rec.close(
            "request_changes", output="AI review\n\n" + s.add("review_note"), tag="incomplete"
        )


def _shadow(store, t, a, s, rec_dir) -> None:
    with tracing.Recorder(
        rec_dir, t.id, a.id, role="review-shadow", model="glm-5", variant_id="glm-5/reviewer-v2@1"
    ) as rec:
        rec.user(s.add("model_text"), by="reviewer")
        rec.llm(text="Not JSON at all: " + s.add("review_note"), tokens_in=10, tokens_out=5)
        rec.close("handed_to_user", output=s.add("review_note"))
    store.record_review(
        a.id, reviewer_model="glm-5", verdict="handed_to_user", note=s.add("store_row"), shadow=True
    )


def _crashed(store, t, s, rec_dir, home_path) -> None:
    """Attempt 2's process died: its recording is a plain ``.jsonl`` with a torn line."""
    a = store.add_attempt(t.id, model="qwen3.8-27b", tier=2, trigger="escalation")
    with tracing.Recorder(rec_dir, t.id, a.id, model="qwen3.8-27b", tier=2) as rec:
        rec.user(s.add("model_text"))
        rec.tool(
            name="read",
            input={"filePath": home_path},
            content=s.add("tool_output"),
            tool_kind="read",
        )
    gz = rec_dir / str(t.id) / f"{a.id}.jsonl.gz"
    lines = gzip.decompress(gz.read_bytes()).decode().splitlines()[:-1]  # no result row
    gz.unlink()
    torn = '{"kind": "tool", "content": "' + s.add("torn_line")
    (gz.with_suffix("")).write_text("\n".join(lines) + "\n" + torn)


# =========================================================================== reading an export


def export_bytes(path: Path) -> dict[str, bytes]:
    """Every byte of an export, by member: an archive's decompressed tar stream (headers
    included) under ``<tar>``, and each member, gzipped members decompressed."""
    out: dict[str, bytes] = {}
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            out[str(p.relative_to(path))] = p.name.encode()
            if p.is_file():
                data = p.read_bytes()
                out[str(p.relative_to(path))] += data
                if p.name.endswith(".gz"):
                    out[str(p.relative_to(path)) + "!"] = gzip.decompress(data)
        return out
    raw = path.read_bytes()
    out["<archive>"] = raw
    tar_bytes = gzip.decompress(raw)
    out["<tar>"] = tar_bytes
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for m in tar.getmembers():
            if m.isfile():
                data = tar.extractfile(m).read()  # type: ignore[union-attr]
                out[m.name] = data
                if m.name.endswith(".gz"):
                    out[m.name + "!"] = gzip.decompress(data)
    return out


def leaks(path: Path, seed: Seed) -> dict[str, list[str]]:
    """Sentinel class -> where it was found in the export."""
    blobs = export_bytes(path)
    found: dict[str, list[str]] = defaultdict(list)
    for cls, value in seed.s.all():
        needle = value.encode()
        for name, data in blobs.items():
            if needle in data:
                found[cls].append(name)
    # The real home of whoever runs the tests is in tmp_path and in stack traces.
    for marker in (str(seed.root), str(Path(__file__).parent)):
        for name, data in blobs.items():
            if marker.encode() in data:
                found["local_path"].append(name)
    return dict(found)


def source_bytes(seed: Seed) -> bytes:
    parts = []
    for p in sorted(seed.root.joinpath("src").rglob("*")):
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()
            parts.append(gzip.decompress(data) if p.name.endswith(".gz") else data)
    return b"\n".join(parts)


@pytest.fixture
def seed(tmp_path: Path) -> Seed:
    return seed_ticket(tmp_path)


def _export(seed: Seed, out: Path, **kw: Any) -> export.ExportResult:
    return export.export_ticket(
        seed.ticket_id,
        recordings_dir=seed.recordings,
        traces_dir=seed.traces,
        db_path=seed.db,
        out=out,
        **kw,
    )


# =========================================================================== Verify


def test_the_fixture_really_carries_every_sentinel(seed: Seed) -> None:
    """The search below is meaningful only if the source holds every sentinel."""
    raw = source_bytes(seed) + (seed.outside / "9.jsonl").read_bytes()
    missing = [(c, v) for c, v in seed.s.all() if v.encode() not in raw]
    assert not missing
    classes = set(seed.s.by_class)
    assert {
        "tool_output",
        "model_text",
        "span_attr",
        "span_event",
        "side_file",
        "store_row",
        "path",
        "log",
        "login",
        "repo",
        "unknown_key",
        "check",
        "review_note",
    } <= classes
    # Every kind of file a ticket owns is present: finished, crashed, review, shadow
    # review, side files, span file, verify and CI logs.
    names = sorted(p.name for p in (seed.recordings / "1").iterdir())
    assert {"1.jsonl.gz", "1.review.jsonl.gz", "1.review-shadow.jsonl.gz", "2.jsonl"} <= set(names)
    assert sorted(p.name for p in (seed.recordings / "1" / "1.files").iterdir()) == [
        "0004-tool.txt",
        "0007-llm.txt",
    ]
    tnames = {p.name for p in (seed.traces / "1").iterdir()}
    assert {"attempt-1-verify.log", "attempt-1-ci.log"} <= tnames
    assert any(n.endswith(".jsonl") for n in tnames)


@pytest.mark.parametrize("form", ["archive", "folder"])
def test_verify_no_sentinel_anywhere_in_the_export(seed: Seed, tmp_path: Path, form: str) -> None:
    out = tmp_path / ("e.tar.gz" if form == "archive" else "e")
    res = _export(seed, out)
    assert res.path == out and out.exists()
    assert leaks(out, seed) == {}
    files = {f["path"] for f in res.manifest["files"]}
    assert {
        "recordings/1/1.jsonl.gz",
        "recordings/1/1.review.jsonl.gz",
        "recordings/1/1.review-shadow.jsonl.gz",
        "recordings/1/2.jsonl",
        "recordings/1/1.files/0004-tool.txt",
        "recordings/1/1.files/0007-llm.txt",
        "traces/1/attempt-1-verify.log",
        "traces/1/attempt-1-ci.log",
        "rows.json",
    } <= files
    counts = res.manifest["counts"]["files"]
    assert counts["skipped_unknown"] == 2  # notes.txt, scratch.json (the symlink is not listed)
    assert res.manifest["counts"]["lines_dropped"] == 1  # the torn line
    assert res.manifest["counts"]["values_replaced"] > 50


MUTATIONS = {
    # class -> how to turn that class's redaction off
    "tool_output": lambda mp: mp.setitem(export.ROW_SCHEMAS["tool"].rules, "content", KEEP),
    "model_text": lambda mp: mp.setitem(export.ROW_SCHEMAS["llm"].rules, "text", KEEP),
    "span_attr": lambda mp: mp.setitem(export.SPAN_ATTRIBUTES.rules, "verify.command", KEEP),
    "span_event": lambda mp: mp.setitem(export.SPAN_ATTRIBUTES.rules, "kickback.note", KEEP),
    "side_file": lambda mp: mp.setattr(export, "side_file", lambda r, d: d),
    "log": lambda mp: mp.setattr(export, "log_file", lambda r, d: d),
    "store_row": lambda mp: mp.setitem(export._TICKET.rules, "request", KEEP),
    "check": lambda mp: mp.setattr(export, "CHECK_SUMMARY", KEEP),
    "review_note": lambda mp: mp.setattr(export, "VERDICT_ANSWER", KEEP),
    "unknown_key": lambda mp: mp.setattr(export.Redactor, "unknown", KEEP),
    "path": lambda mp: mp.setattr(
        export.Redactor,
        "pseudo",
        lambda self, kind, v, ext=False, _orig=export.Redactor.pseudo: (
            v if kind == "path" else _orig(self, kind, v, ext=ext)
        ),
    ),
    "repo": lambda mp: mp.setitem(export._TICKET.rules, "repo", KEEP),
    "login": lambda mp: mp.setitem(export.ROW_SCHEMAS["user"].rules, "by", KEEP),
}


@pytest.mark.parametrize("cls", sorted(MUTATIONS))
def test_each_check_is_non_vacuous(
    seed: Seed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cls: str
) -> None:
    """Turn one class's redaction off: the same search finds that class's sentinel."""
    MUTATIONS[cls](monkeypatch)
    out = tmp_path / "leaky.tar.gz"
    _export(seed, out)
    assert cls in leaks(out, seed)


def test_archive_metadata_names_nobody(seed: Seed, tmp_path: Path) -> None:
    out = tmp_path / "e.tar.gz"
    _export(seed, out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    raw = out.read_bytes()
    assert raw[3] & 0x08 == 0  # gzip FLG.FNAME unset: no original file name stored
    assert raw[4:8] == b"\0\0\0\0"  # gzip MTIME zero
    with tarfile.open(out) as tar:
        members = tar.getmembers()
    assert members[0].name == "fman-export-t1" and members[0].isdir()
    for m in members:
        assert m.name.startswith("fman-export-t1")
        assert (m.uname, m.gname, m.uid, m.gid) == ("", "", 0, 0)
        assert m.mode == (0o700 if m.isdir() else 0o600)
        assert not (m.issym() or m.islnk())


def test_folder_output_has_safe_permissions(seed: Seed, tmp_path: Path) -> None:
    out = tmp_path / "e"
    _export(seed, out)
    for p in [out, *out.rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), p


# =========================================================================== the scheme


def test_placeholders_keep_length_and_equality_inside_one_export_only(tmp_path: Path) -> None:
    r1, r2 = export.Redactor(), export.Redactor()
    a, b = r1.text("hello world"), r1.text("hello world")
    assert a == b and a.startswith("<redacted 11 chars #") and "hello" not in a
    assert r1.text("hello worle") != a
    assert r2.text("hello world") != a  # another export, another salt
    assert r1.text("") == "" and r1.text(None) is None and r1.text(True) is True
    # A known salt gives a known hash, so the scheme is what the manifest says it is.
    import hmac

    r3 = export.Redactor(b"k" * 32)
    want = hmac.new(b"k" * 32, b"abc", hashlib.sha256).hexdigest()[:8]
    assert r3.text("abc") == f"<redacted 3 chars #{want}>"


def test_the_salt_is_not_written(seed: Seed, tmp_path: Path) -> None:
    salt = secrets.token_bytes(32)
    files = export.build_export(
        1, recordings_dir=seed.recordings, traces_dir=seed.traces, rows=None, salt=salt
    )
    blob = b"".join(gzip.decompress(v) if k.endswith(".gz") else v for k, v in files.items())
    for form in (salt, salt.hex().encode(), salt.hex().upper().encode()):
        assert form not in blob
    manifest = json.loads(files["MANIFEST.json"])
    assert "in memory only" in manifest["redaction"]["salt"]


def test_pseudonyms_are_stable_and_keep_only_a_short_extension() -> None:
    r = export.Redactor()
    assert export.PATH(r, "/Users/x/a/b.py") == "path-1.py"
    assert export.PATH(r, "/Users/x/a/b.py") == "path-1.py"
    assert export.PATH(r, "src/c.TSX") == "path-2.tsx"
    assert export.PATH(r, "weird.verylongextension") == "path-3"
    assert export.PATH(r, "Makefile") == "path-4"
    assert export.REPO(r, "acme/widgets") == "repo-1" == export.REPO(r, "acme/widgets")
    assert export.USER(r, "octocat") == "user-1" and export.USER(r, "reviewer") == "reviewer"
    assert export.BRANCH(r, "fman/issue-7/a2") == "fman/issue-7/a2"
    assert export.BRANCH(r, "main") == "main"
    assert export.BRANCH(r, "acme-widgets-fix") == "branch-1"
    assert export.COMMIT(r, "deadbeef") == "commit-1"
    assert (
        export.FILEREF(r, "/Users/x/.foreman-inator/traces/3/attempt-4-ci.log")
        == "attempt-4-ci.log"
    )
    assert export.FILEREF(r, "/Users/x/secret.log") == "path-5.log"
    assert export.TOOL_NAME(r, "read") == "read" and export.TOOL_NAME(r, "exfil") == "tool-1"


def test_unknown_keys_and_shapes_are_redacted_never_passed_through() -> None:
    r = export.Redactor()
    row = {
        "kind": "llm",
        "turn": 1,
        "text": "x",
        "mystery": "the user's code",
        "mystery_count": 12345,
        "mystery_flag": True,
        "Mystery Key!": "y",
        "nested": {"greet.py": {"inner": "z", "n": 2}},
        "usage": {"input_tokens": 5, "output_tokens": "lots"},
    }
    out = export._redact_row(r, row)
    assert out["kind"] == "llm" and out["turn"] == 1
    assert out["mystery"].startswith("<redacted 15 chars")
    assert out["mystery_count"] is None  # numbers under unknown keys are dropped
    assert out["mystery_flag"] is True  # booleans kept
    assert "Mystery Key!" not in out and any(k.startswith("key-") for k in out)
    assert "greet.py" not in json.dumps(out)  # keys inside unknown values become key-N
    assert out["usage"] == {"input_tokens": 5, "output_tokens": out["usage"]["output_tokens"]}
    assert out["usage"]["output_tokens"].startswith("<redacted")
    # An unknown row kind keeps only the common fields.
    other = export._redact_row(r, {"kind": "note", "seq": 3, "body": "secret"})
    assert other["kind"] == "note" and other["seq"] == 3 and other["body"].startswith("<redacted")
    # Enum-shaped fields accept only enum-shaped values.
    assert export.CODE(r, "approve") == "approve"
    assert export.CODE(r, "two words").startswith("<redacted")


def test_decision_answers_are_reduced_to_the_decision() -> None:
    r = export.Redactor()
    assert export.ROUTE_ANSWER(r, "<think>the user's code</think> Small") == "small"
    assert export.ROUTE_ANSWER(r, "small, because the code says ...").startswith("<redacted")
    note = "the user's secret code"
    v = json.loads(
        export.VERDICT_ANSWER(r, json.dumps({"verdict": "approve", "tag": None, "note": note}))
    )
    assert v["verdict"] == "approve" and v["tag"] is None and note not in v["note"]
    bad = json.dumps({"verdict": "sk_live_lookslikeakey", "tag": None, "note": "n"})
    assert export.VERDICT_ANSWER(r, bad).startswith("<redacted")
    bad_tag = json.dumps({"verdict": "request_changes", "tag": "zz-leak", "note": "n"})
    assert export.VERDICT_ANSWER(r, bad_tag).startswith("<redacted")
    s = export.CHECK_SUMMARY(
        r, "Changed-line coverage: 60% (6 of 10). New code no test executes: src/greet.py:banner."
    )
    assert (
        s.startswith("Changed-line coverage: <redacted") and "greet" not in s and "banner" not in s
    )
    assert s.endswith("New code no test executes: path-1.py:fn-1.")
    assert export.CHECK_SUMMARY(r, "Something else entirely").startswith("<redacted")


# =========================================================================== replay still works


@pytest.fixture
def cases(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    shutil.copytree(CASES, root, ignore=shutil.ignore_patterns("README.md"))
    for p in (root / "recordings").rglob("*.jsonl"):
        p.with_name(p.name + ".gz").write_bytes(gzip.compress(p.read_bytes()))
        p.unlink()
    return root


@pytest.mark.parametrize("tid", [1, 2])
def test_an_export_replays_like_the_original(cases: Path, tmp_path: Path, tid: int) -> None:
    """On the checked-in cases (#28), the exported recording still reproduces the exported
    trace exactly, and today's logic still agrees with the exported rows: route (label
    kept), escalation, review policy (verdicts and check phrases kept) and lifecycle."""
    rows = json.loads((cases / "rows" / f"{tid}.json").read_text())
    out = tmp_path / f"e{tid}"
    export.export_ticket(
        tid, recordings_dir=cases / "recordings", traces_dir=cases / "traces", rows=rows, out=out
    )
    rec, tr = out / "recordings", out / "traces"
    replayed = replay.replay_spans(rec, tr, tid)
    assert replay.diff_trees(replayed, replay.recorded_span_tree(tr, tid)) == []
    assert replay.tree_shape(replayed) == replay.tree_shape(
        replay.replay_spans(cases / "recordings", cases / "traces", tid)
    )
    data = json.loads((out / "rows.json").read_text())
    regression = replay.regress(replay.rows_from_json(data, recordings_dir=rec, traces_dir=tr))
    assert regression.agrees and regression.diffs == ()
    assert regression.decisions  # it really decided something
    # The timeline (the viewer's model) has the same steps, failure step included.
    a = replay.timeline(
        cases / "recordings",
        tid,
        traces_dir=cases / "traces",
        rows=replay.rows_from_json(
            rows, recordings_dir=cases / "recordings", traces_dir=cases / "traces"
        ),
    )
    b = replay.timeline(
        rec, tid, traces_dir=tr, rows=replay.rows_from_json(data, recordings_dir=rec, traces_dir=tr)
    )
    assert [[(s.kind, s.row_index) for s in r.steps] for r in a.recordings] == [
        [(s.kind, s.row_index) for s in r.steps] for r in b.recordings
    ]
    assert [len(r.failure_steps) for r in a.recordings] == [
        len(r.failure_steps) for r in b.recordings
    ]


def test_the_sentinel_export_opens_in_replay_and_the_viewer(seed: Seed, tmp_path: Path) -> None:
    out = tmp_path / "e"
    _export(seed, out)
    rec, tr = out / "recordings", out / "traces"
    report = replay.replay_ticket(1, recordings_dir=rec, traces_dir=tr)
    orig = replay.replay_ticket(1, recordings_dir=seed.recordings, traces_dir=seed.traces)
    assert orig.tree_diffs == () and report.tree_diffs == ()
    assert replay.tree_shape(replay.replay_spans(rec, tr, 1)) == replay.tree_shape(
        replay.replay_spans(seed.recordings, seed.traces, 1)
    )
    steps = [len(r.steps) for r in report.timeline.recordings]
    assert steps[:2] == [len(r.steps) for r in orig.timeline.recordings][:2]
    # The crashed recording's torn last line is dropped (and counted), so replay reads the
    # rows that survived; on the source, the torn line makes the whole file unreadable.
    assert steps[2] == 2 and [len(r.steps) for r in orig.timeline.recordings][2] == 0
    data = json.loads((out / "rows.json").read_text())
    rows = replay.rows_from_json(data, recordings_dir=rec, traces_dir=tr)
    assert rows.ticket.repo == "repo-1" and rows.explore is True
    assert [rv["shadow"] for rv in data["shadow_reviews"]] == [True]
    routes = trace_view.trace_routes(rec, tr, rows_for={1: rows}.get)
    client = TestClient(
        create_app(tmp_path / "none.db", extra_routes=routes, nav=trace_view.NAV),
        base_url="http://127.0.0.1:8642",
    )
    for url in ("/traces", "/traces/1", "/traces/1/1"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert not any(v in r.text for _, v in seed.s.all())


# =========================================================================== read-only, ids, output


def _tree_state(root: Path) -> dict[str, tuple[str, float, int]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        digest = (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ""
        )
        out[str(p.relative_to(root))] = (digest, st.st_mtime, st.st_mode)
    return out


def test_sources_are_never_modified(seed: Seed, tmp_path: Path) -> None:
    before = _tree_state(seed.root / "src")
    for p in [seed.db, *seed.recordings.rglob("*"), *seed.traces.rglob("*")]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode & ~0o222)  # read-only: any write would fail
    try:
        _export(seed, tmp_path / "e.tar.gz")
    finally:
        for p in [seed.db, *seed.recordings.rglob("*"), *seed.traces.rglob("*")]:
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | 0o200)
    after = _tree_state(seed.root / "src")
    # SQLite's read-only WAL reader (as `fman status` uses) leaves an empty -wal and the
    # -shm index beside the database; the database itself is unchanged.
    extra = set(after) - set(before)
    assert extra <= {"fman.db-wal", "fman.db-shm"}
    if "fman.db-wal" in extra:
        assert (seed.root / "src" / "fman.db-wal").stat().st_size == 0
    after = {k: v for k, v in after.items() if k not in extra}
    assert before.keys() == after.keys()
    assert {k: v[0] for k, v in before.items()} == {k: v[0] for k, v in after.items()}
    assert {k: v[1] for k, v in before.items() if v[0]} == {
        k: v[1] for k, v in after.items() if v[0]
    }


@pytest.mark.parametrize(
    "bad", ["../1", "1/../../outside", "-1", " 1", "+1", "1.0", "١", "", "1\x00"]
)
def test_a_bad_ticket_id_is_refused(seed: Seed, tmp_path: Path, bad: str) -> None:
    with pytest.raises(export.ExportError, match="not a ticket id"):
        export.export_ticket(
            bad, recordings_dir=seed.recordings, traces_dir=seed.traces, out=tmp_path / "e.tgz"
        )
    assert not (tmp_path / "e.tgz").exists()


def test_a_symlinked_ticket_folder_is_not_followed(seed: Seed, tmp_path: Path) -> None:
    (seed.recordings / "77").symlink_to(seed.outside, target_is_directory=True)
    with pytest.raises(export.ExportError, match="nothing recorded"):
        export.export_ticket(
            77, recordings_dir=seed.recordings, traces_dir=seed.traces, out=tmp_path / "e.tgz"
        )


def test_refuses_outputs_it_should_not_write(
    seed: Seed, tmp_path: Path, foreman_home: Path
) -> None:
    foreman_home.mkdir(parents=True, exist_ok=True)
    for bad, why in [
        (foreman_home / "e.tar.gz", "inside"),
        (seed.recordings / "e.tar.gz", "inside"),
        (seed.traces / "1" / "x", "inside"),
        (tmp_path / "nope" / "e.tar.gz", "no such folder"),
    ]:
        with pytest.raises(export.ExportError, match=why):
            _export(seed, bad)
    (tmp_path / "exists.tar.gz").write_text("keep me")
    with pytest.raises(export.ExportError, match="already exists"):
        _export(seed, tmp_path / "exists.tar.gz")
    assert (tmp_path / "exists.tar.gz").read_text() == "keep me"
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "f").write_text("x")
    with pytest.raises(export.ExportError, match="not an empty folder"):
        _export(seed, tmp_path / "full")
    with pytest.raises(export.ExportError, match="--redact"):
        _export(seed, tmp_path / "raw.tar.gz", redact=False)
    assert not (tmp_path / "raw.tar.gz").exists()
    assert not list(tmp_path.glob(".*.partial"))


def test_default_output_is_downloads_never_the_repo_or_home(
    seed: Seed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "userhome"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    res = export.export_ticket(1, recordings_dir=seed.recordings, traces_dir=seed.traces)
    assert res.path.parent == home / "Downloads"
    assert res.path.name.startswith("fman-export-t1-") and res.path.name.endswith(".tar.gz")
    assert export.git_work_tree(res.path) is None


# =========================================================================== the CLI


def test_cli_refuses_without_redact(seed: Seed, tmp_path: Path) -> None:
    out = tmp_path / "e.tar.gz"
    r = CliRunner().invoke(
        app,
        [
            "export",
            "1",
            "-o",
            str(out),
            "--recordings",
            str(seed.recordings),
            "--traces",
            str(seed.traces),
            "--db",
            str(seed.db),
        ],
    )
    assert r.exit_code == 2
    assert "--redact" in r.output and "no raw mode" in r.output
    assert not out.exists()


def test_cli_end_to_end_from_the_foreman_home(
    seed: Seed, tmp_path: Path, foreman_home: Path
) -> None:
    """The worker's own layout under FOREMAN_INATOR_HOME, no source flags."""
    foreman_home.mkdir(parents=True, exist_ok=True)
    shutil.copytree(seed.recordings, foreman_home / "recordings", symlinks=True)
    shutil.copytree(seed.traces, foreman_home / "traces")
    shutil.copy(seed.db, foreman_home / "foreman-inator.db")
    out = tmp_path / "bug.tar.gz"
    r = CliRunner().invoke(app, ["export", "1", "--redact", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert f"Wrote {out}" in r.output and "MANIFEST.json" in r.output
    assert leaks(out, seed) == {}
    with tarfile.open(out) as tar:
        names = tar.getnames()
        manifest = json.load(tar.extractfile("fman-export-t1/MANIFEST.json"))  # type: ignore[arg-type]
        rows = json.load(tar.extractfile("fman-export-t1/rows.json"))  # type: ignore[arg-type]
    assert "fman-export-t1/rows.json" in names
    assert manifest["ticket_id"] == 1 and manifest["redacted"] is True
    assert manifest["counts"]["files"]["rows"] == 1
    assert rows["ticket"]["repo"] == "repo-1" and rows["ladder"]  # the config's ladder
    assert rows["attempts"][0]["branch"] == "branch-1"
    assert rows["attempts"][0]["verify_output_path"] == "attempt-1-verify.log"
    assert rows["ticket"]["flag_reason"].startswith("checks_failed: <redacted")
    # The extracted export opens with `fman replay`; the recording replays the trace.
    # (The fixture's rows are not a consistent lifecycle, so decisions are not checked
    # here; test_an_export_replays_like_the_original does that on the #28 cases.)
    ex = tmp_path / "x"
    with tarfile.open(out) as tar:
        tar.extractall(ex, filter="data")
    d = ex / "fman-export-t1"
    r2 = CliRunner().invoke(
        app,
        [
            "replay",
            "1",
            "--recordings",
            str(d / "recordings"),
            "--traces",
            str(d / "traces"),
            "--rows",
            str(d / "rows.json"),
        ],
    )
    assert "span tree: replay matches the trace" in r2.output, r2.output


def test_cli_with_explicit_sources_and_a_bad_id(seed: Seed, tmp_path: Path) -> None:
    common = [
        "--recordings",
        str(seed.recordings),
        "--traces",
        str(seed.traces),
        "--db",
        str(seed.db),
    ]
    out = tmp_path / "dir-out"
    r = CliRunner().invoke(app, ["export", "1", "--redact", "-o", str(out), *common])
    assert r.exit_code == 0, r.output
    assert (out / "MANIFEST.json").is_file() and leaks(out, seed) == {}
    r = CliRunner().invoke(
        app, ["export", "../1", "--redact", "-o", str(tmp_path / "b.tgz"), *common]
    )
    assert r.exit_code == 1 and "not a ticket id" in r.output
    assert os.listdir(tmp_path).count("b.tgz") == 0
