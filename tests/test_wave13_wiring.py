"""Wave 13 wiring: the daily prune in the poll loop (#56), `fman prune`, and the
per-attempt budget in `fman status` (#58)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes.worker_env import Env
from foreman_inator import paths
from foreman_inator.cli import app
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.worker_status import render_status


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_poll_prunes_only_after_recover(env: Env, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(type(env.worker), "maybe_prune", lambda self, **k: calls.append(1))
    env.worker.poll_and_handle()
    assert calls == []  # recover() has not run: a dead process's attempts look running
    env.worker.recover()
    env.worker.poll_and_handle()
    assert calls == [1]


def test_fman_prune_dry_run_and_bad_days(foreman_home: Path) -> None:
    Store(paths.db_path()).close()
    res = CliRunner().invoke(app, ["prune", "--dry-run", "--days", "30"])
    assert res.exit_code == 0, res.output
    assert "nothing to prune (retention 30 days)" in res.output
    res = CliRunner().invoke(app, ["prune", "--days", "0.5"])
    assert res.exit_code != 0


def test_status_shows_attempt_spend_against_budget(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    t = store.create_ticket(repo="o/r", issue_number=1, title="t", request="r", base_branch="main")
    store.set_column(t.id, "in_progress")
    a = store.add_attempt(t.id, model="glm", tier=3, trigger="first")
    store.update_attempt(a.id, cost_usd=0.42)
    out = render_status(store, config=Config(attempt_budget_usd=2.0))
    assert "$0.42 of $2.00" in out
    assert "of $" not in render_status(store)
