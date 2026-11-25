"""Memory state on each attempt (todo #59, issue #36): ``resources.memory`` parsing behind
an injected runner, and the attempt span carrying ``resource.*`` at start and
``resource.end.*`` at end."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from fakes.worker_env import Env, failing, passing
from foreman_inator.config import ModelEndpoint
from foreman_inator.resources import memory
from foreman_inator.tracing import capture_spans

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                  1095516.
Pages active:                                3254524.
Pages inactive:                              2969903.
Pages wired down:                             500492.
Pages purgeable:                              245223.
"Translation faults":                     6209448067.
Anonymous pages:                             5412714.
Pages occupied by compressor:                 214099.
"""
PAGE = 16384
USED = ((5412714 - 245223) + 500492 + 214099) * PAGE


def runner(outputs: dict[str, str | Exception]):
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(list(cmd))
        out = outputs[cmd[0]]
        if isinstance(out, Exception):
            raise out
        return out

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- parsing


def test_parse_vm_stat_used_is_app_plus_wired_plus_compressed() -> None:
    assert memory.parse_vm_stat(VM_STAT) == USED


def test_parse_vm_stat_unreadable_is_none() -> None:
    assert memory.parse_vm_stat("") is None
    assert memory.parse_vm_stat("page size of 4096 bytes\nPages free: 10.\n") is None


def test_parse_vm_stat_without_anonymous_falls_back_to_active() -> None:
    text = "(page size of 4096 bytes)\nPages active: 10.\nPages wired down: 5.\n"
    assert memory.parse_vm_stat(text) == 15 * 4096


@pytest.mark.parametrize(
    ("level", "pressure"), [("1", "normal"), ("2", "warn"), ("4", "critical"), ("9", "unknown")]
)
def test_parse_sysctl_pressure_levels(level: str, pressure: str) -> None:
    assert memory.parse_sysctl(f"137438953472\n{level}\n91\n") == (137438953472, pressure, 91)


def test_read_memory_on_a_mac() -> None:
    run = runner({"vm_stat": VM_STAT, "sysctl": "137438953472\n2\n12\n"})
    state = memory.read_memory(run)
    assert state == memory.MemoryState(USED, 137438953472, 12, "warn")
    assert run.calls[1] == ["sysctl", "-n", *memory._SYSCTLS]


def test_read_memory_without_the_commands_never_raises() -> None:
    run = runner(
        {
            "vm_stat": FileNotFoundError("vm_stat"),
            "sysctl": subprocess.TimeoutExpired("sysctl", 2),
        }
    )
    assert memory.read_memory(run) == memory.MemoryState(None, None, None, "unknown")


# --------------------------------------------------------------------------- snapshot


def _ollama(handler):
    return httpx.MockTransport(handler)


def test_snapshot_includes_loaded_models_as_json() -> None:
    def ps(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/ps"
        model = {"name": "qwen3.8:27b", "size": 9, "size_vram": 7}
        return httpx.Response(200, json={"models": [model]})

    run = runner({"vm_stat": VM_STAT, "sysctl": "100\n1\n50\n"})
    attrs = memory.snapshot("http://127.0.0.1:11434/v1", run=run, transport=_ollama(ps))
    assert attrs == {
        "mem_used_bytes": USED,
        "mem_total_bytes": 100,
        "mem_free_pct": 50,
        "mem_pressure": "normal",
        "ollama_loaded": json.dumps([{"name": "qwen3.8:27b", "size": 9, "size_vram": 7}]),
    }


def test_snapshot_unreachable_ollama_and_no_commands() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    run = runner({"vm_stat": FileNotFoundError(), "sysctl": FileNotFoundError()})
    attrs = memory.snapshot("http://127.0.0.1:11434", run=run, transport=_ollama(down))
    assert attrs["mem_pressure"] == "unknown"
    assert "mem_used_bytes" not in attrs and "mem_total_bytes" not in attrs
    assert "unreachable" in json.loads(attrs["ollama_loaded"])
    no_host = json.loads(memory.snapshot(None, run=run)["ollama_loaded"])
    assert no_host == {"unreachable": "no local model configured"}


def test_local_ollama_host_prefers_the_writer() -> None:
    local = ModelEndpoint("a", 1, "local", "a", "http://127.0.0.1:1/v1", "x")
    other = ModelEndpoint("b", 2, "local", "b", "http://127.0.0.1:2/v1", "y")
    cloud = ModelEndpoint("c", 3, "cloud", "c", "https://api.example.com/v1", "z")
    assert memory.local_ollama_host(local, (other,)) == local.base_url
    assert memory.local_ollama_host(cloud, (cloud, other)) == other.base_url
    assert memory.local_ollama_host(cloud, (cloud,)) is None


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, k: str, v: object) -> None:
        self.attributes[k] = v


def test_attempt_snapshots_writes_end_even_when_the_attempt_raises() -> None:
    span = _Span()
    n = iter(range(10))
    with pytest.raises(RuntimeError), memory.attempt_snapshots(
        span, "h", snap=lambda host: {"mem_used_bytes": next(n), "host": host}
    ):  # fmt: skip
        raise RuntimeError("attempt failed")
    assert span.attributes == {
        "resource.mem_used_bytes": 0,
        "resource.host": "h",
        "resource.end.mem_used_bytes": 1,
        "resource.end.host": "h",
    }


def test_a_broken_probe_never_breaks_the_attempt() -> None:
    def boom(host):
        raise OSError("no")

    span = _Span()
    with memory.attempt_snapshots(span, None, snap=boom):
        pass
    assert span.attributes == {}


# --------------------------------------------------------------------------- the worker


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def _fake_snapshot(log: list[str | None]):
    def snap(host: str | None) -> dict[str, object]:
        log.append(host)
        return {"mem_used_bytes": 1000 * len(log), "mem_pressure": "warn", "ollama_loaded": "[]"}

    return snap


@pytest.mark.parametrize("script", [passing, failing])
def test_attempt_span_records_memory_at_start_and_end(env: Env, script) -> None:
    hosts: list[str | None] = []
    env.worker.resource_snapshot = _fake_snapshot(hosts)
    with capture_spans() as spans:
        env.label()
        env.once(script())
    att = [s for s in spans.get_finished_spans() if s.name == "attempt"]
    assert len(att) == 1
    a = att[0].attributes
    assert a["resource.mem_used_bytes"] == 1000
    assert a["resource.end.mem_used_bytes"] == 2000
    assert a["resource.mem_pressure"] == a["resource.end.mem_pressure"] == "warn"
    assert a["resource.ollama_loaded"] == "[]"
    # Tier 1 is local: its own Ollama is the one asked.
    assert hosts == ["http://127.0.0.1:11434/v1"] * 2
