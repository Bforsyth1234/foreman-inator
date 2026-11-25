"""Wave 18 wiring: each slot's piece is actually called.

#73 ``/settings``, #74 ``/tiers`` and #76 ``/activity`` (with #79's Retry button) are mounted in
``full_app`` and every page's nav lists every page; #39 ``fman setup --public`` reaches
the manifest; #73 (GH #62) the worker re-reads config.toml at the top of each poll.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from fakes.worker_env import Env
from foreman_inator import config as config_mod
from foreman_inator import paths, worker_run
from foreman_inator import setup as setup_mod
from foreman_inator.cli import app
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.experiments import variants
from foreman_inator.metrics import activity_ui, experiments_ui, settings_ui, tiers_ui, web
from foreman_inator.worker import Worker
from foreman_inator.worker_config_reload import RESTART_FIELDS, ConfigReloadSteps

BASE = "http://127.0.0.1:8642"
NEW_PAGES = (activity_ui.NAV_ENTRY, settings_ui.NAV_ENTRY, tiers_ui.NAV_ENTRY)


class Stop(Exception):
    pass


# --------------------------------------------------------------------------- pages


def _client(cfg: Config) -> TestClient:
    db = paths.db_path()
    with Store(db) as s:
        variants.ensure_defaults(s, cfg)
    config_mod.save(cfg)  # /settings and /tiers read config.toml
    a = web.full_app(db, recordings_dir=paths.recordings_dir(), traces_dir=None, config=cfg)
    return TestClient(a, base_url=BASE)


def test_full_app_mounts_settings_tiers_and_activity() -> None:
    c = _client(Config())
    for path, label in NEW_PAGES:
        r = c.get(path)
        assert r.status_code == 200, (path, r.text)
        assert label in r.text


@pytest.mark.parametrize(
    "page", ["/", "/failures", "/resources", experiments_ui.MANAGE_PATH, *(p for p, _ in NEW_PAGES)]
)
def test_every_page_nav_lists_every_page(page: str) -> None:
    html = _client(Config()).get(page).text
    for path, _label in (experiments_ui.NAV_ENTRY, *NEW_PAGES):
        assert f'href="{path}"' in html, (page, path)


def test_activity_retry_gets_the_cli_github_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    real = activity_ui.routes

    def spy(*a: Any, **kw: Any) -> Any:
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(activity_ui, "routes", spy)
    _client(Config())
    assert seen["api_factory"] is web.retry_github
    assert web.retry_github(Config()) is None  # no App credentials: flagged tickets only


# --------------------------------------------------------------------------- --public


def test_public_reaches_the_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    servers: list[dict[str, Any]] = []

    def fake_server(**kw: Any) -> Any:
        servers.append(kw)
        raise Stop

    monkeypatch.setattr(setup_mod.manifest, "CallbackServer", fake_server)
    with pytest.raises(Stop):
        setup_mod.register_app(opener=lambda u: None, echo=lambda m: None, public=True)
    with pytest.raises(Stop):
        setup_mod.register_app(opener=lambda u: None, echo=lambda m: None)
    assert [s["public"] for s in servers] == [True, False]


def test_cli_public_flag_reaches_run_setup_and_prints_the_share_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got: list[Any] = []
    real_run = setup_mod.run_setup

    def spy_run(**kw: Any) -> Any:
        got.append(kw.get("public"))
        raise Stop

    monkeypatch.setattr(setup_mod, "run_setup", spy_run)
    r = CliRunner().invoke(app, ["setup", "--public", "--no-service"])
    assert isinstance(r.exception, Stop) and got == [True]
    CliRunner().invoke(app, ["setup", "--no-service"])
    assert got == [True, False]
    monkeypatch.setattr(setup_mod, "run_setup", real_run)

    regs: list[Any] = []
    creds = SimpleNamespace(pem="PEM", webhook_secret="", app_id=11, slug="fi-x", permissions={})

    def fake_register(**kw: Any) -> Any:
        regs.append(kw.get("public"))
        return creds

    monkeypatch.setattr(setup_mod, "register_app", fake_register)
    monkeypatch.setattr(
        setup_mod.manifest,
        "apply_app",
        lambda cfg, c, users: config_mod.replace(cfg, app_id=c.app_id),
    )
    monkeypatch.setattr(setup_mod, "AppAuth", lambda *a, **kw: (_ for _ in ()).throw(Stop()))
    out: list[str] = []
    with pytest.raises(Stop):
        setup_mod.run_setup(echo=out.append, public=True)
    assert regs == [True]
    assert setup_mod.manifest.public_app_text("fi-x") in out

    out.clear()  # an App exists now: --public points at docs/publishing.md
    with pytest.raises(Stop):
        setup_mod.run_setup(echo=out.append, public=True)
    assert regs == [True] and any("docs/publishing.md" in m for m in out)


# --------------------------------------------------------------------------- config reload


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_poll_loop_reloads_a_changed_config(env: Env, tmp_path: Path) -> None:
    assert issubclass(Worker, ConfigReloadSteps)
    w = env.worker
    path = tmp_path / "watched.toml"
    config_mod.save(w.config, path)
    w.config = config_mod.load(path)
    w.watch_config(path)
    wanted = w.config.max_open_prs_per_repo + 7
    config_mod.save(replace(w.config, max_open_prs_per_repo=wanted), path)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    w.poll_and_handle()
    assert w.config.max_open_prs_per_repo == wanted


def test_fman_run_watches_config_toml_and_restart_text_names_restart_fields() -> None:
    src = inspect.getsource(worker_run.run_worker)
    assert "worker.watch_config(paths.config_path())" in src
    assert "next poll" in setup_mod.RESTART_TEXT
    assert all(f in setup_mod.RESTART_TEXT for f in RESTART_FIELDS)
