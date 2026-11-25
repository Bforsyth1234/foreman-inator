"""The resource panel on the metrics page (todo #60, issue #37).

The worker writes ``<home>/run/resources.json`` each poll (``worker_resources``); the
metrics page reads it for ``/resources`` and a short block on ``/``; the trace view's
attempt blocks show the ``resource.*`` / ``resource.end.*`` attempt-span attributes.
"""

from __future__ import annotations

import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from foreman_inator import paths
from foreman_inator.db.store import Store
from foreman_inator.metrics import trace_view
from foreman_inator.metrics.web import create_app, full_app
from foreman_inator.resources import snapshot
from foreman_inator.resources.memory import MemoryState
from foreman_inator.resources.ollama import LoadedModel, Unreachable
from foreman_inator.worker_resources import ResourceSteps

BASE = "http://127.0.0.1:8642"
CASES = Path(__file__).parent / "recordings"
EVIL = "<script>alert(1)</script>"
NOW = 1_764_000_000.0

MEM = MemoryState(
    used_bytes=30_000_000_000, total_bytes=64_000_000_000, free_pct=41, pressure="normal"
)


def _models(host: str) -> list[LoadedModel]:
    exp = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    return [
        LoadedModel("qwen3.8-27b:latest", 21_300_000_000, 21_300_000_000, exp),
        LoadedModel(EVIL, 1_000_000_000, 0, None),
    ]


def _snap(written_at: float = NOW, **kw) -> dict:
    kw.setdefault("memory", lambda: MEM)
    kw.setdefault("ollama", _models)
    return snapshot.build(
        "http://127.0.0.1:11434/v1", kw.pop("slots", (1, 2)), now=lambda: written_at, **kw
    )


# ------------------------------------------------------------------ snapshot file


def test_build_write_load_round_trip(tmp_path: Path) -> None:
    path = snapshot.write(tmp_path / "run" / snapshot.RESOURCES_NAME, _snap())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == [snapshot.RESOURCES_NAME]  # no temp left
    r = snapshot.load(path, now=lambda: NOW + 12)
    assert r.error is None and r.age_seconds == 12 and not r.stale
    assert r.data is not None
    assert r.data["local_slots"] == {"in_use": 1, "cap": 2}
    assert r.data["memory"]["used_bytes"] == 30_000_000_000
    assert r.data["ollama"]["host"] == "http://127.0.0.1:11434"
    assert [m["name"] for m in r.data["ollama"]["models"]] == ["qwen3.8-27b:latest", EVIL]


def test_unreachable_ollama_and_no_proxy_are_not_zero(tmp_path: Path) -> None:
    data = _snap(ollama=lambda h: Unreachable("no reply in 2s", slow=True), slots=None)
    assert data["ollama"]["unreachable"] == "no reply in 2s" and data["ollama"]["slow"]
    assert data["local_slots"] is None
    boom = _snap(ollama=lambda h: (_ for _ in ()).throw(RuntimeError("x")), memory=lambda: 1 / 0)
    assert "unreachable" in boom["ollama"] and boom["memory"]["pressure"] == "unknown"
    cloud_only = snapshot.build(None, None, memory=lambda: MEM, ollama=_models)
    assert cloud_only["ollama"] is None


@pytest.mark.parametrize(
    ("content", "word"),
    [(None, "no resource snapshot yet"), ("{", "not valid JSON"), ('{"version": 9}', "unknown")],
)
def test_load_never_raises(tmp_path: Path, content: str | None, word: str) -> None:
    p = tmp_path / "resources.json"
    if content is not None:
        p.write_text(content)
    r = snapshot.load(p)
    assert r.data is None and word in (r.error or "")


def test_stale_after_five_minutes(tmp_path: Path) -> None:
    p = snapshot.write(tmp_path / "r.json", _snap())
    assert not snapshot.load(p, now=lambda: NOW + 300).stale
    assert snapshot.load(p, now=lambda: NOW + 301).stale


# ------------------------------------------------------------------ the worker mixin


class _Proxy:
    def __init__(self, slots=(1, 1), fail=False):
        self.slots, self.fail = slots, fail

    def local_slots(self):
        if self.fail:
            raise RuntimeError("down")
        return self.slots


def _worker(proxy) -> ResourceSteps:
    w = ResourceSteps()
    w.config = SimpleNamespace(
        models=[
            SimpleNamespace(kind="cloud", base_url="https://api.example"),
            SimpleNamespace(kind="local", base_url="http://127.0.0.1:11434/v1"),
        ]
    )
    w.proxy = proxy
    w.resources_memory = lambda: MEM
    w.resources_ollama = _models
    return w


def test_worker_writes_the_snapshot_to_the_run_dir() -> None:
    path = _worker(_Proxy((2, 3))).write_resources()
    assert path == paths.run_dir() / "resources.json"  # the test's FOREMAN_INATOR_HOME
    data = json.loads(path.read_text())
    assert data["local_slots"] == {"in_use": 2, "cap": 3}
    assert data["ollama"]["host"] == "http://127.0.0.1:11434"


def test_worker_snapshot_never_raises(tmp_path: Path) -> None:
    w = _worker(_Proxy(fail=True))
    path = w.write_resources(path=tmp_path / "r.json")
    assert path is not None and json.loads(path.read_text())["local_slots"] is None
    (tmp_path / "dir.json").mkdir()
    assert w.write_resources(path=tmp_path / "dir.json") is None  # cannot replace a dir


# ------------------------------------------------------------------ the pages


def _client(tmp_path: Path, data: dict | None, *, now: float = NOW + 12, monkeypatch=None):
    res = tmp_path / "resources.json"
    if data is not None:
        snapshot.write(res, data)
    if monkeypatch is not None:
        monkeypatch.setattr(snapshot, "clock", lambda: now)
    db = tmp_path / "fman.db"
    Store(db).close()
    app = full_app(
        db, recordings_dir=tmp_path / "rec", traces_dir=tmp_path / "tr", resources_path=res
    )
    return TestClient(app, base_url=BASE)


def test_resources_page_shows_models_memory_slots_and_age(tmp_path: Path, monkeypatch) -> None:
    c = _client(tmp_path, _snap(), monkeypatch=monkeypatch)
    r = c.get("/resources")
    assert r.status_code == 200
    t = r.text
    assert "Local inference slots: 1 of 2 in use" in t
    assert "Memory: 30.0 GB of 64.0 GB used, 41% free, pressure normal" in t
    assert "qwen3.8-27b:latest" in t and "21.3 GB" in t
    assert "as of 12s ago" in t and "stale" not in t
    assert "<script>" not in t and "&lt;script&gt;alert(1)&lt;/script&gt;" in t
    assert 'href="/resources">Resources</a>' in t


def test_main_page_has_the_short_block(tmp_path: Path, monkeypatch) -> None:
    c = _client(tmp_path, _snap(), monkeypatch=monkeypatch)
    t = c.get("/").text
    assert '<p class="resources">' in t
    assert "Ollama: 2 loaded (22.3 GB)" in t and "1 of 2 in use" in t
    assert "as of 12s ago" in t and 'href="/resources">details' in t


def test_stale_snapshot_says_so(tmp_path: Path, monkeypatch) -> None:
    c = _client(tmp_path, _snap(), now=NOW + 3600, monkeypatch=monkeypatch)
    for page in ("/", "/resources"):
        t = c.get(page).text
        assert "as of 60 min ago: stale" in t and "the worker may not be running" in t


def test_missing_snapshot(tmp_path: Path) -> None:
    c = _client(tmp_path, None)
    assert "no resource snapshot yet" in c.get("/resources").text
    assert "no resource snapshot yet" in c.get("/").text


def test_unreachable_text_is_escaped(tmp_path: Path, monkeypatch) -> None:
    data = _snap(ollama=lambda h: Unreachable(EVIL))
    t = _client(tmp_path, data, monkeypatch=monkeypatch).get("/resources").text
    assert "Ollama: unreachable (&lt;script&gt;" in t and "<script>" not in t


def test_resources_is_get_only_and_local_only(tmp_path: Path) -> None:
    c = _client(tmp_path, _snap())
    assert c.post("/resources").status_code == 405
    assert c.get("/resources", headers={"host": "evil.example"}).status_code == 403


def test_full_app_defaults_to_the_run_dir(tmp_path: Path) -> None:
    snapshot.write(paths.run_dir() / "resources.json", _snap(written_at=0.0))
    db = tmp_path / "fman.db"
    Store(db).close()
    c = TestClient(full_app(db, recordings_dir=tmp_path, traces_dir=None), base_url=BASE)
    assert "Local inference slots: 1 of 2 in use" in c.get("/resources").text


def test_create_app_without_a_resources_path_has_no_block(tmp_path: Path) -> None:
    db = tmp_path / "fman.db"
    Store(db).close()
    assert '<p class="resources">' not in TestClient(create_app(db), base_url=BASE).get("/").text


# ------------------------------------------------------------------ the attempt view


def _cases_with_resources(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    shutil.copytree(CASES, root, ignore=shutil.ignore_patterns("README.md"))
    (trace,) = (root / "traces" / "1").glob("*.jsonl")
    lines = []
    for ln in trace.read_text().splitlines():
        row = json.loads(ln)
        if row.get("name") == "attempt" and row["attributes"].get("attempt.id") == 3:
            row["attributes"].update(
                {
                    "resource.mem_used_bytes": 30_000_000_000,
                    "resource.mem_total_bytes": 64_000_000_000,
                    "resource.mem_free_pct": 41,
                    "resource.mem_pressure": "normal",
                    "resource.ollama_loaded": json.dumps(
                        [{"name": EVIL, "size": 21_300_000_000, "size_vram": 0}]
                    ),
                    "resource.end.mem_used_bytes": 52_000_000_000,
                    "resource.end.mem_pressure": "warn",
                    "resource.end.ollama_loaded": json.dumps({"unreachable": "no reply"}),
                }
            )
        lines.append(json.dumps(row))
    trace.write_text("\n".join(lines) + "\n")
    return root


def test_attempt_view_shows_resource_attributes(tmp_path: Path) -> None:
    root = _cases_with_resources(tmp_path)
    assert set(trace_view.attempt_resources(root / "traces", 1)) == {3}
    routes = trace_view.trace_routes(root / "recordings", root / "traces")
    c = TestClient(create_app(tmp_path / "none.db", extra_routes=routes), base_url=BASE)
    t = c.get("/traces/1/3").text
    assert t.count('<table class="resources">') == 1  # attempt 3 only, not its review
    assert "<th>Memory used</th><td>30.0 GB</td><td>52.0 GB</td>" in t
    assert "<th>Memory pressure</th><td>normal</td><td>warn</td>" in t
    assert "<th>Free memory</th><td>41%</td><td>-</td>" in t
    assert "&lt;script&gt;alert(1)&lt;/script&gt; (21.3 GB)" in t and "<script>" not in t
    assert "unreachable (no reply)" in t
    full = c.get("/traces/1").text
    assert full.count('<table class="resources">') == 1  # attempts 1, 2, 4 recorded none
    assert '<table class="resources">' not in c.get("/traces/2").text
