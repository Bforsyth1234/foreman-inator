"""Wave 19 wiring (#71 chat foundations): each slot's piece is reachable and agrees.

Locks use the store's chat helpers; spend tags the real ledger column and lets the
proxy's 5 s cloud fallback through; the chat config keys reach ``Spend`` in ``fman run``;
``prune`` prunes ended sessions' recordings; lock waits name chat sessions; ``fman repo
set-chat-network`` and ``/settings`` set the per-repo opt-in; ``/experiments/manage``
reads config.toml per request. Nothing here starts a chat session (that is wave C2).
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from typer.testing import CliRunner

from foreman_inator import config as config_mod
from foreman_inator import paths, prune, worker_run
from foreman_inator.cli import app
from foreman_inator.config import Config, ConfigError, ModelEndpoint, SpendCaps
from foreman_inator.db.store import Store, Ticket
from foreman_inator.experiments import variants
from foreman_inator.locks import Conflict, LockResult, Locks, holders_text
from foreman_inator.metrics import activity_ui, experiments_ui, forms, settings_ui, web
from foreman_inator.spend import Spend
from foreman_inator.tracing import recorder
from foreman_inator.worker_queue import QueueSteps

BASE = "http://127.0.0.1:8642"
REPO = "acme/widgets"
LOCAL = ModelEndpoint("qwen", 1, "local", "qwen3", "http://127.0.0.1:11434/v1", "qwen")
CLOUD = ModelEndpoint("glm", 2, "cloud", "glm-x", "https://api.example.com/v1", "glm")


@pytest.fixture
def store() -> Store:
    s = Store(paths.db_path())
    yield s
    s.close()


def _session(store: Store, state: str = "idle") -> int:
    sid = store.create_chat_session(REPO, base_branch="main", tier=1, model="qwen").id
    if state != "starting":
        store.set_chat_state(sid, state, pr_number=7 if state == "opened_pr" else None)
    return sid


def _ticket(store: Store, owned: tuple[str, ...]) -> Ticket:
    t = store.create_ticket(
        repo=REPO, issue_number=store.query("SELECT COUNT(*) AS n FROM tickets")[0]["n"] + 1,
        title="t", request="r", base_branch="main", owned_files=owned,
    )  # fmt: skip
    return store.set_column(t.id, "in_progress")


# --------------------------------------------------------------------------- config


def test_chat_config_defaults_and_round_trip() -> None:
    cfg = Config()
    assert (cfg.chat_local_wait_seconds, cfg.chat_start_tier) == (5.0, 1)
    assert (cfg.chat_idle_minutes, cfg.chat_max_sessions) == (30, 1)
    assert cfg.chat_turn_budget_usd is None and cfg.chat_turn_budget == cfg.attempt_budget_usd
    assert replace(cfg, attempt_budget_usd=3.0).chat_turn_budget == 3.0
    assert cfg.spend_caps.chat_monthly_usd == 20.0
    raw = config_mod.to_dict(cfg)
    raw.update(chat_local_wait_seconds=2, chat_start_tier=2, chat_turn_budget_usd=0.5)
    raw["spend_caps"]["chat_monthly_usd"] = 7
    back = config_mod.from_dict(raw)
    assert (back.chat_local_wait_seconds, back.chat_start_tier) == (2.0, 2)
    assert back.chat_turn_budget == 0.5 and back.spend_caps.chat_monthly_usd == 7.0
    assert config_mod.from_dict(config_mod.to_dict(back)) == back


def test_chat_monthly_cap_may_not_exceed_monthly() -> None:
    with pytest.raises(ConfigError, match="chat_monthly_usd"):
        Config(spend_caps=SpendCaps(monthly_usd=30.0, chat_monthly_usd=31.0))
    assert SpendCaps(monthly_usd=10.0).chat_monthly_usd == 10.0  # the default is lowered
    for bad in ({"chat_start_tier": 4}, {"chat_max_sessions": 0}, {"chat_turn_budget_usd": 0}):
        with pytest.raises(ConfigError):
            Config(**bad)


def test_fman_run_gives_spend_the_chat_values() -> None:
    src = inspect.getsource(worker_run)
    assert "chat_monthly_usd=cfg.spend_caps.chat_monthly_usd" in src
    assert "chat_turn_budget_usd=cfg.chat_turn_budget" in src


# --------------------------------------------------------------------------- spend


def _spend(store: Store) -> Spend:
    cfg = Config(spend_caps=SpendCaps(daily_usd=5.0, monthly_usd=50.0))
    return Spend(store, cfg, chat_monthly_usd=cfg.spend_caps.chat_monthly_usd)


def test_spend_records_chat_cost_on_the_real_store_column(store: Store) -> None:
    sid = _session(store)
    sp = _spend(store)
    sp.register_chat_session(sid, repo=REPO, model=CLOUD.name)
    sp.start_chat_turn(sid)
    ctx = SimpleNamespace(kind="registered", bucket="live", chat_session_id=sid, token="t")
    assert sp.hook(CLOUD, 0.5, ctx, phase="estimate")
    sp.hook(CLOUD, 0.25, ctx, phase="actual")
    assert store.chat_spend(sid) == pytest.approx(0.25)


def test_spend_lets_the_proxy_fallback_through_but_not_other_models(store: Store) -> None:
    sid = _session(store)
    sp = _spend(store)
    sp.register_chat_session(sid, repo=REPO, model=LOCAL.name, small_model="tiny")
    sp.start_chat_turn(sid)
    ctx = SimpleNamespace(kind="registered", bucket="live", chat_session_id=sid, token="t")
    assert not sp.hook(CLOUD, 0.1, ctx, phase="estimate")  # not this turn's model
    fallback = replace_ns(ctx, fallback_from=LOCAL.name)
    assert sp.hook(CLOUD, 0.1, fallback, phase="estimate")
    assert not sp.hook(CLOUD, 0.1, replace_ns(ctx, fallback_from="other"), phase="estimate")
    store.set_cloud(REPO, False)  # a local-only repo never falls back (#49)
    assert not sp.hook(CLOUD, 0.1, fallback, phase="estimate")


def test_spend_unset_small_model_does_not_let_ordinary_cloud_calls_through(
    store: Store,
) -> None:
    """Review P0 (wave 19): with small_model None, `None in (model, None)` made every
    ordinary call (fallback_from None) count as a fallback, skipping the chat_model check.
    Non-vacuity: with the old tuple membership test the first assertion fails."""
    sid = _session(store)
    sp = _spend(store)
    sp.register_chat_session(sid, repo=REPO, model=LOCAL.name)
    sp.start_chat_turn(sid)
    ctx = SimpleNamespace(kind="registered", bucket="live", chat_session_id=sid, token="t")
    assert not sp.hook(CLOUD, 0.1, ctx, phase="estimate")
    assert sp.refusal_for_chat(sid).cap == "chat_model"
    assert sp.hook(CLOUD, 0.1, replace_ns(ctx, fallback_from=LOCAL.name), phase="estimate")


def replace_ns(ns: SimpleNamespace, **kw: object) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(ns), **kw})


# --------------------------------------------------------------------------- locks


def test_chat_locks_go_through_the_store_helpers(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    for name in ("insert_chat_locks", "chat_lock_rows", "delete_chat_locks"):
        real = getattr(store, name)
        monkeypatch.setattr(
            store, name, lambda *a, _r=real, _n=name, **k: (calls.append(_n), _r(*a, **k))[1]
        )
    sid = _session(store, "busy")
    lk = Locks(store)
    assert lk.chat_touch(sid, REPO, ["src/a.py"]) == []
    assert lk.chat_held(sid) == ("src/a.py",)
    assert lk.release_chat(sid) == 1
    assert calls == ["insert_chat_locks", "chat_lock_rows", "delete_chat_locks"]


def test_lock_gate_log_names_the_chat_session(caplog: pytest.LogCaptureFixture) -> None:
    res = LockResult(5, (), (Conflict("a.py", "a.py", 3, "chat"), Conflict("b.py", "b.py", 9)))
    fake = SimpleNamespace(locks=SimpleNamespace(may_start=lambda t: res), _waiting={})
    with caplog.at_level(logging.INFO, logger="foreman_inator.worker"):
        assert not QueueSteps._lock_gate(fake, SimpleNamespace(id=5))
    assert "held by ticket(s) #9 and chat session 3" in caplog.text
    assert holders_text((), (3, 4)) == "chat sessions 3, 4"


def test_activity_page_shows_a_ticket_waiting_on_a_chat(store: Store) -> None:
    sid = _session(store, "busy")
    Locks(store).chat_touch(sid, REPO, ["a.py"])
    t = _ticket(store, ("a.py",))
    assert not Locks(store).may_start(t).ok
    html, summary = activity_ui._columns(store, None, 5)
    assert f"WAITING: on files held by chat session {sid}" in html
    assert "1 waiting." in summary


# --------------------------------------------------------------------------- prune


def test_prune_removes_old_recordings_of_ended_chats_only(store: Store) -> None:
    ended, pr, live = _session(store, "ended"), _session(store, "opened_pr"), _session(store)
    rec = paths.recordings_dir()
    old = time.time() - 200 * 86400
    files = {}
    for sid in (ended, pr, live, 999):
        f = recorder.chat_recording_path(rec, sid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")
        os.utime(f, (old, old))
        files[sid] = f
    report = prune.run(store, paths.home(), days=90)
    assert not files[ended].exists() and not files[999].exists()
    assert files[pr].exists() and files[live].exists()
    assert any("chat" in r.reason for r in report.removed)


# --------------------------------------------------------------------------- repo opt-in


def test_cli_set_chat_network(store: Store) -> None:
    r = CliRunner().invoke(app, ["repo", "set-chat-network", REPO, "on"])
    assert r.exit_code == 0, r.output
    assert store.repo_allows_chat_network(REPO)
    r = CliRunner().invoke(app, ["repo", "set-chat-network", REPO, "off"])
    assert r.exit_code == 0 and not store.repo_allows_chat_network(REPO)
    assert CliRunner().invoke(app, ["repo", "set-chat-network", REPO, "maybe"]).exit_code != 0


def test_settings_page_sets_chat_network(store: Store) -> None:
    config_mod.save(Config(repos=(REPO,)))
    routes = settings_ui.routes(paths.db_path(), guard=forms.FormGuard("tok"))
    c = TestClient(web.LoopbackHostOnly(Starlette(routes=routes)), base_url=BASE)
    assert 'name="chat_network"' in c.get("/settings").text
    r = c.post(
        "/settings/repo",
        data={"csrf_token": "tok", "repo": REPO, "chat_network": "on"},
        headers={"origin": BASE},
    )
    assert r.status_code == 200, r.text
    assert "chat network: off -&gt; on" in r.text
    assert store.repo_allows_chat_network(REPO)


# --------------------------------------------------------------------------- experiments


def test_experiments_page_reads_config_per_request(store: Store) -> None:
    extra = ModelEndpoint("extra-x", 1, "local", "extra", "http://127.0.0.1:11434/v1", "extra")
    started = replace(Config(), models=(*Config().models, extra))
    variants.ensure_defaults(store, started)
    config_mod.save(started)
    a = web.full_app(
        paths.db_path(), recordings_dir=paths.recordings_dir(), traces_dir=None, config=started
    )
    c = TestClient(a, base_url=BASE)
    assert re.search(r'<option value="extra-x"', c.get(experiments_ui.MANAGE_PATH).text)
    config_mod.save(Config())  # the model is removed after start
    assert "extra-x" not in c.get(experiments_ui.MANAGE_PATH).text
    paths.config_path().write_text("not = [valid")  # unreadable: the startup config
    assert re.search(r'<option value="extra-x"', c.get(experiments_ui.MANAGE_PATH).text)
