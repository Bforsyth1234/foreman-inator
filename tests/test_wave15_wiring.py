"""Wave 15 wiring: each slot's piece is actually called.

#29 the recorder keeps the proxy's prompt-cache counts; #39 the live reviewer gets the
repo's ``rules_file`` and a replayed rules file reaches a ``reviewer-v2`` review (#43
``conventions`` override); #43 ``fman experiment run --rules``; #24 the daily post-merge
scan runs from the poll loop after recover; #45/#35 plugin CLI commands, the worker's
startup lines and ``fman status``'s Ollama and plugin lines.
"""

from __future__ import annotations

import gzip
import importlib
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import test_reviewer_conventions as rc
from fakes.worker_env import REPO, Env, passing
from foreman_inator import config as config_mod
from foreman_inator import paths, plugins, tracing, worker_run
from foreman_inator import worker_experiments as wx
from foreman_inator import worker_status as ws
from foreman_inator.cli import app
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.metrics import quality
from foreman_inator.plugins import GROUP, LoadedPlugin, PluginSet
from foreman_inator.resources.ollama import LoadedModel
from foreman_inator.review import reviewer as rv
from test_reviewer_conventions import make_pr  # noqa: F401  (fixture)

PLUGIN = """
API_VERSIONS = (1,)

def wave15(name: str = "world") -> None:
    print(f"wave15 hello {name}")

def register(registry):
    registry.add_cli_command("wave15-hello", wave15, help="Say hello.")
"""


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def _install_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = f"fake_wave15_plugin_{uuid.uuid4().hex[:8]}"
    site = tmp_path / f"site-{mod}"
    site.mkdir()
    (site / f"{mod}.py").write_text(PLUGIN)
    info = site / "fake_wave15_plugin-0.4.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: fake-wave15-plugin\n"
                                   "Version: 0.4.0\n")  # fmt: skip
    (info / "entry_points.txt").write_text(f"[{GROUP}]\nwave15 = {mod}\n")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, mod, raising=False)


ONE_PLUGIN = PluginSet(loaded=[LoadedPlugin("wave15", "fake-wave15-plugin", "0.4.0", (1,))])


# --------------------------------------------------------------------------- #29


def test_recorder_keeps_the_prompt_cache_counts(tmp_path: Path) -> None:
    rec = tracing.Recorder(tmp_path, 1, 2, model="claude", base_commit="b0")
    rec.llm(text="hi", tokens_in=10, tokens_out=2, cache_read_tokens=700, cache_write_tokens=30)
    rec.llm(text="again", tokens_in=4, tokens_out=1)
    rec.close("finished")
    (f,) = (tmp_path / "1").glob("2*.jsonl.gz")
    rows = [json.loads(x) for x in gzip.decompress(f.read_bytes()).decode().splitlines()]
    usage = [r["usage"] for r in rows if r["kind"] == "llm"]
    assert usage[0] == {"input_tokens": 10, "output_tokens": 2,
                        "cache_read_tokens": 700, "cache_write_tokens": 30}  # fmt: skip
    assert usage[1] == {"input_tokens": 4, "output_tokens": 1}  # no caching: old shape


# --------------------------------------------------------------------------- #39 / #43


def test_live_review_gets_the_repos_rules_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = tmp_path / "team-rules.md"
    rules.write_text("Always write a docstring.\n")
    e = Env(tmp_path / "e", rules_file={REPO: str(rules)})
    try:
        seen: list[Any] = []
        real = e.worker._launch_review

        def launch(**kw: Any) -> Any:
            seen.append(kw.get("rules_file"))
            return real(**kw)

        monkeypatch.setattr(e.worker, "_launch_review", launch)
        e.label()
        e.once(passing(model_call=False))
        assert seen == [rules]
    finally:
        e.close()


def test_review_change_uses_a_given_conventions_override(make_pr, monkeypatch) -> None:  # noqa: F811
    """``conventions=`` (#43) replaces the base-commit gather on a ``reviewer-v2`` review,
    and the replay runner sees that ``review_change`` takes it."""
    assert rr.REVIEW_TAKES_CONVENTIONS

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("an override must not gather the base's rules")

    monkeypatch.setattr(rv, "base_conventions", boom)
    override = rr.RulesOverride("REPLAYED-RULE-5x: tabs, never spaces.", "AGENTS.md")
    client = rc.CapturingModel()
    out = rc._review(make_pr(rc.GOOD), client, prompt_name="reviewer-v2",
                     conventions=override.conventions())  # fmt: skip
    assert out.verdict == "approve", out.note
    user = rc._user(client)
    assert "REPLAYED-RULE-5x" in user and rc.BASE_RULE not in user


# --------------------------------------------------------------------------- #43 CLI


def test_fman_experiment_run_rules_reaches_run_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(wx, "run_command", lambda **kw: calls.append(kw) or 0)
    rules = tmp_path / "AGENTS.md"
    rules.write_text("Prefer short names.\n")
    r = CliRunner().invoke(app, ["experiment", "run", "--role", "writer", "--rules", str(rules)])
    assert r.exit_code == 0, r.output
    assert calls[0]["rules"] == rules and calls[0]["challenger"] is None


# --------------------------------------------------------------------------- #24


def test_poll_scans_post_merge_quality_after_recover(env: Env, monkeypatch) -> None:
    scans: list[int] = []
    monkeypatch.setattr(
        quality, "scan", lambda store, api, **kw: scans.append(1) or quality.ScanReport()
    )
    env.worker.poll_and_handle()
    assert scans == []  # before recover(), like prune
    env.worker.recover()
    env.worker.poll_and_handle()
    env.worker.poll_and_handle()  # same worker-clock day: no second scan
    assert scans == [1]


# --------------------------------------------------------------------------- #45 / #35


def test_plugin_cli_command_is_installed_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foreman_inator.cli as cli

    _install_plugin(tmp_path, monkeypatch)
    try:
        fresh = importlib.reload(cli)
        r = CliRunner().invoke(fresh.app, ["wave15-hello", "--name", "fman"])
        assert r.exit_code == 0, r.output
        assert "wave15 hello fman" in r.output
    finally:
        monkeypatch.undo()
        importlib.reload(cli)


def test_worker_startup_logs_the_plugins(foreman_home: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(worker_run, "_setup_logging", lambda: None)
    monkeypatch.setattr(plugins, "load_plugins", lambda: ONE_PLUGIN)
    with caplog.at_level(logging.INFO, logger="foreman_inator"):
        assert worker_run.run_worker(once=True) == 1  # no App configured: stops after
    assert "Plugin wave15: fake-wave15-plugin 0.4.0 (hook API 1)" in caplog.text


@pytest.mark.parametrize("with_db", [False, True])
def test_status_shows_ollama_and_plugins(foreman_home: Path, monkeypatch, with_db: bool) -> None:
    cfg = Config(app_id=1, repos=(REPO,))
    host = ws.local_ollama_host(cfg)
    assert host is not None  # the default config has local models
    config_mod.save(cfg)
    if with_db:
        Store(paths.db_path()).close()
    hosts: list[str] = []

    def loaded(host: str, **kw: Any) -> list[LoadedModel]:
        hosts.append(host)
        return [LoadedModel("qwen3:8b", 5_000_000_000, 5_000_000_000, None)]

    monkeypatch.setattr(ws, "loaded_models", loaded)
    monkeypatch.setattr(plugins, "load_plugins", lambda: ONE_PLUGIN)
    r = CliRunner().invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert hosts == [host]
    assert "Ollama: 1 loaded (5.0 GB)" in r.output
    assert "Plugin wave15: fake-wave15-plugin 0.4.0 (hook API 1)" in r.output
