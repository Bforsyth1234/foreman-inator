"""The worker re-reads config.toml when it changes (todo #73, GH #62)."""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from foreman_inator import config as config_mod
from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.spend import Spend
from foreman_inator.worker_config_reload import ConfigReloadSteps, changes


class FakeWorker(ConfigReloadSteps):
    def __init__(self, cfg: Config, spend: Spend) -> None:
        self.config = cfg
        self.spend = spend
        self.poller = SimpleNamespace(config=cfg, repos=tuple(cfg.repos))
        self.repos = self.poller.repos
        self.parallel = cfg.max_parallel_per_repo


def bump(path: Path) -> None:
    """Make sure the mtime moves even on a coarse clock."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, FakeWorker]:
    path = tmp_path / "config.toml"
    cfg = Config(repos=("acme/widgets",), allowed_users=("brooks",))
    config_mod.save(cfg, path)
    cfg = config_mod.load(path)
    store = SimpleNamespace(now=lambda: None)
    w = FakeWorker(cfg, Spend(store, cfg, attempt_budget_usd=cfg.attempt_budget_usd))  # type: ignore[arg-type]
    w.watch_config(path)
    return path, w


def test_nothing_happens_while_the_file_is_unchanged(setup: tuple[Path, FakeWorker]) -> None:
    _path, w = setup
    before = w.config
    assert w.maybe_reload_config() is None
    assert w.config is before


def test_a_changed_cap_reaches_the_worker_and_spend_without_restart(
    setup: tuple[Path, FakeWorker], caplog: pytest.LogCaptureFixture
) -> None:
    path, w = setup
    new = replace(
        w.config,
        spend_caps=SpendCaps(daily_usd=1.0, monthly_usd=9.0),
        repos=("acme/widgets", "acme/gadgets"),
        max_parallel_per_repo=3,
        writer_timeout_seconds=99.0,
        attempt_budget_usd=0.5,
    )
    config_mod.save(new, path)
    bump(path)
    with caplog.at_level(logging.INFO, logger="foreman_inator.worker"):
        lines = w.maybe_reload_config()
    assert lines and "spend_caps.daily_usd: 5.0 -> 1.0" in lines
    assert w.config.spend_caps.daily_usd == 1.0
    assert w.spend.caps.daily_usd == 1.0
    assert w.spend.attempt_budget_usd == 0.5
    assert w.poller.config is w.config
    assert w.poller.repos == new.repos
    assert w.repos == new.repos
    assert w.parallel == 3
    assert w.config.writer_timeout_seconds == 99.0
    assert "config.toml reloaded" in caplog.text
    assert "spend_caps.daily_usd" in caplog.text
    assert w.maybe_reload_config() is None  # same stamp: no second read


@pytest.mark.parametrize("text", ["[spend_caps]\ndaily_usd = -1\n", "daily_usd = [", "bogus = 1"])
def test_an_invalid_file_keeps_the_old_config_and_logs_once(
    setup: tuple[Path, FakeWorker], caplog: pytest.LogCaptureFixture, text: str
) -> None:
    path, w = setup
    before, caps = w.config, w.spend.caps
    path.write_text(text)
    bump(path)
    with caplog.at_level(logging.WARNING, logger="foreman_inator.worker"):
        assert w.maybe_reload_config() is None
        assert w.maybe_reload_config() is None
    assert w.config is before
    assert w.spend.caps is caps
    assert caplog.text.count("not applied") == 1


def test_a_missing_file_keeps_the_old_config(setup: tuple[Path, FakeWorker]) -> None:
    path, w = setup
    before = w.config
    path.unlink()
    assert w.maybe_reload_config() is None
    assert w.config is before


def test_restart_only_fields_are_held_back_and_logged(
    setup: tuple[Path, FakeWorker], caplog: pytest.LogCaptureFixture
) -> None:
    path, w = setup
    extra = ModelEndpoint("x-local", 1, "local", "x", "http://127.0.0.1:11434/v1", "fam-x")
    new = replace(w.config, models=(*w.config.models, extra), poll_interval_seconds=60)
    config_mod.save(new, path)
    bump(path)
    old_models = w.config.models
    with caplog.at_level(logging.WARNING, logger="foreman_inator.worker"):
        lines = w.maybe_reload_config()
    assert lines == ["poll_interval_seconds: 45 -> 60"]
    assert w.config.models == old_models
    assert "models changed; restart the worker" in caplog.text


def test_changes_lists_every_differing_key() -> None:
    a = Config()
    b = replace(a, allowed_users=("x",), status_comment=False)
    assert changes(a, b) == ["allowed_users: [] -> [x]", "status_comment: true -> false"]
    assert changes(a, a) == []


@pytest.mark.parametrize(
    "text",
    ["", "\n", 'repos = []\nallowed_users = ["brooks"]\n', 'repos = ["acme/widgets"]\n'],
    ids=["empty", "blank", "no-repos", "no-allowed-users"],
)
def test_a_wiped_or_half_written_file_keeps_the_old_config(
    setup: tuple[Path, FakeWorker], caplog: pytest.LogCaptureFixture, text: str
) -> None:
    path, w = setup
    before, caps = w.config, w.spend.caps
    path.write_text(text)
    bump(path)
    with caplog.at_level(logging.WARNING, logger="foreman_inator.worker"):
        assert w.maybe_reload_config() is None
    assert w.config is before
    assert w.spend.caps is caps
    assert w.repos == ("acme/widgets",)
    assert "not applied" in caplog.text


def test_a_changed_daily_cap_moves_the_default_approval_amount(
    setup: tuple[Path, FakeWorker],
) -> None:
    path, w = setup
    assert w.spend.approval_usd == 5.0
    config_mod.save(replace(w.config, spend_caps=SpendCaps(daily_usd=2.0, monthly_usd=9.0)), path)
    bump(path)
    assert w.maybe_reload_config()
    assert w.spend.approval_usd == 2.0


def test_an_explicit_approval_amount_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cfg = Config(repos=("acme/widgets",), allowed_users=("brooks",))
    config_mod.save(cfg, path)
    cfg = config_mod.load(path)
    store = SimpleNamespace(now=lambda: None)
    w = FakeWorker(cfg, Spend(store, cfg, approval_usd=7.0))  # type: ignore[arg-type]
    w.watch_config(path)
    config_mod.save(replace(cfg, spend_caps=SpendCaps(daily_usd=2.0, monthly_usd=9.0)), path)
    bump(path)
    assert w.maybe_reload_config()
    assert w.spend.approval_usd == 7.0
