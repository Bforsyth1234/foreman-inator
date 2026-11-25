"""Plugin hooks (todo #68, issue #45) and the Ollama status line (todo #58, issue #35).

Fake plugins are real installed distributions: a ``*.dist-info`` with an
``entry_points.txt`` on ``sys.path``, found by ``importlib.metadata`` like any pip install.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import typer
from typer.testing import CliRunner

from foreman_inator import plugins
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.plugins import GROUP, install_cli_commands, load_plugins
from foreman_inator.worker_status import local_ollama_host, ollama_line, render_status

GOOD_PLUGIN = """
API_VERSIONS = (1,)
CALLS = []

def hello(name: str = "world") -> None:
    print(f"hello {name} from plugin")

def register(registry):
    CALLS.append(registry.api_version)
    registry.add_cli_command("hello", hello, help="Say hello.")
"""


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    *,
    dist: str = "fake-fman-plugin",
    version: str = "1.2.3",
    ep_name: str = "fake",
    group: str = GROUP,
) -> str:
    """Install a one-module distribution whose entry point in ``group`` is the module."""
    mod = f"fake_fman_plugin_{uuid.uuid4().hex[:8]}"
    site = tmp_path / f"site-{mod}"
    site.mkdir()
    (site / f"{mod}.py").write_text(body)
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    (info / "entry_points.txt").write_text(f"[{group}]\n{ep_name} = {mod}\n")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, mod, raising=False)
    return mod


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


# --- #45 plugins -------------------------------------------------------------------


def test_installed_plugin_is_loaded_listed_and_its_hook_called(tmp_path, monkeypatch, store):
    mod = _install(tmp_path, monkeypatch, GOOD_PLUGIN)
    ps = load_plugins()
    names = [p.name for p in ps.loaded]
    assert names == ["fake"], ps.failed
    assert sys.modules[mod].CALLS == [plugins.API_VERSION]  # register(registry) ran
    p = ps.loaded[0]
    assert (p.distribution, p.version, p.api_versions) == ("fake-fman-plugin", "1.2.3", (1,))
    assert [c.name for c in ps.registry.cli_commands] == ["hello"]

    out = render_status(store, plugins=ps)
    assert "Plugin fake: fake-fman-plugin 1.2.3 (hook API 1)" in out

    app = typer.Typer()

    @app.command("status")
    def status() -> None:
        print("builtin status")

    assert install_cli_commands(app, ps) == []
    res = CliRunner().invoke(app, ["hello", "--name", "fman"])
    assert res.exit_code == 0, res.output
    assert "hello fman from plugin" in res.output


def test_entry_point_in_another_group_is_ignored(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, GOOD_PLUGIN, group="something_else.plugins")
    ps = load_plugins()
    assert ps.loaded == [] and ps.failed == []


def test_plugin_that_raises_on_load_is_logged_and_skipped(tmp_path, monkeypatch, store, caplog):
    _install(tmp_path, monkeypatch, "raise RuntimeError('boom at import')", ep_name="bad")
    _install(tmp_path, monkeypatch, GOOD_PLUGIN, dist="good-plugin", ep_name="good")
    with caplog.at_level(logging.WARNING, logger="foreman_inator.plugins"):
        ps = load_plugins()  # never raises
    assert [p.name for p in ps.loaded] == ["good"]
    assert [p.name for p in ps.failed] == ["bad"]
    assert "boom at import" in ps.failed[0].reason
    assert "bad failed to load" in caplog.text
    assert "Plugin bad: SKIPPED, failed to load (RuntimeError: boom at import)" in render_status(
        store, plugins=ps
    )


def test_register_raising_discards_its_partial_hooks(tmp_path, monkeypatch):
    body = """
API_VERSIONS = (1,)
def register(registry):
    registry.add_cli_command("half", lambda: None)
    raise SystemExit("plugin tried to exit")
"""
    _install(tmp_path, monkeypatch, body)
    ps = load_plugins()
    assert ps.loaded == [] and [p.name for p in ps.failed] == ["fake"]
    assert ps.registry.cli_commands == ()


@pytest.mark.parametrize(
    "body, reason",
    [
        ("def register(registry): pass", "declares no API_VERSIONS"),
        ("API_VERSIONS = (99,)\ndef register(registry): pass", "needs hook API (99,)"),
        ("API_VERSIONS = (1,)", "no register"),
    ],
)
def test_plugin_without_a_compatible_api_version_is_skipped(tmp_path, monkeypatch, body, reason):
    _install(tmp_path, monkeypatch, body)
    ps = load_plugins()
    assert ps.loaded == []
    assert reason in ps.failed[0].reason


def test_plugin_cannot_replace_a_builtin_command(tmp_path, monkeypatch):
    body = """
API_VERSIONS = (1,)
def register(registry):
    registry.add_cli_command("approve-spend", lambda: print("hijacked"))
"""
    _install(tmp_path, monkeypatch, body)
    ps = load_plugins()
    app = typer.Typer()

    @app.command("approve-spend")
    def approve_spend() -> None:
        print("builtin")

    @app.command("other")
    def other() -> None:
        print("other")

    assert install_cli_commands(app, ps) == ["approve-spend"]
    res = CliRunner().invoke(app, ["approve-spend"])
    assert "builtin" in res.output and "hijacked" not in res.output


def test_real_cli_builtin_names_are_seen_as_taken():
    from foreman_inator import cli

    taken = plugins._taken_names(cli.app)
    assert {"run", "status", "approve-spend", "repo", "config"} <= taken


def test_default_with_no_plugin_is_todays_behaviour(store):
    ps = load_plugins(entry_points=lambda group: [])
    assert ps.loaded == [] and ps.failed == [] and ps.registry.cli_commands == ()
    app = typer.Typer()
    assert install_cli_commands(app, ps) == [] and app.registered_commands == []
    assert plugins.status_lines(ps) == ["Plugins: none"]
    out = render_status(store)  # no new arguments: no plugin or Ollama lines
    assert "Plugin" not in out and "Ollama" not in out


def test_discovery_failure_is_not_fatal():
    def broken(group):
        raise OSError("bad site-packages")

    ps = load_plugins(entry_points=broken)
    assert ps.loaded == [] and ps.failed == [] and ps.registry.cli_commands == ()


# --- #35 Ollama line ---------------------------------------------------------------


def _ps_transport(body=None, status=200, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_ollama_line_counts_loaded_models_and_size():
    seen: list[str] = []
    body = {
        "models": [
            {"name": "qwen3.8:27b", "size": 20_000_000_000, "size_vram": 1},
            {"name": "inkling-2-mini", "size": 1_300_000_000, "size_vram": 1},
        ]
    }
    line = ollama_line("http://127.0.0.1:11434/v1", transport=_ps_transport(body, seen=seen))
    assert line == "Ollama: 2 loaded (21.3 GB): qwen3.8:27b, inkling-2-mini"
    assert seen == ["http://127.0.0.1:11434/api/ps"]


def test_ollama_line_zero_loaded_is_not_unreachable():
    t = _ps_transport({"models": []})
    assert ollama_line("http://h:1", transport=t) == "Ollama: 0 loaded (0.0 GB)"


def test_ollama_line_unreachable_gives_reason(store):
    def refuse(req):
        raise httpx.ConnectError("refused", request=req)

    out = render_status(
        store, ollama_host="http://127.0.0.1:11434", ollama_transport=httpx.MockTransport(refuse)
    )
    assert (
        "Ollama: unreachable (Ollama not reachable at http://127.0.0.1:11434 (ConnectError))"
        in (out)
    )
    assert "0 loaded" not in out


def test_local_ollama_host_picks_first_local_model():
    assert local_ollama_host(Config()) == "http://127.0.0.1:11434/v1"
    # A Config refuses to have no writer tiers, so stand in with just the models.
    cloud_only = SimpleNamespace(models=tuple(m for m in Config().models if m.is_cloud))
    assert local_ollama_host(cloud_only) is None  # type: ignore[arg-type]
