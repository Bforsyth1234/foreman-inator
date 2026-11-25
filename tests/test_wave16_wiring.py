"""Wave 16 wiring: each slot's piece is actually called.

#42 ``fman rules show``; #49 (GH #26) ``fman repo set-cloud``, local-only limits, no cloud
embedder, replay cases, the status line and the dormant flags; #34 ``fman experiment
split``; #35 ``--mode seeded`` and its status; #50 the config keys, the cloud check and
the ``tier:N`` opt-out; #77 ``fman sweep`` and the open-PR WAITING line; #72
``reviewer_order``; the Ollama status line's "slow".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from fakes.worker_env import REPO, Env, passing
from foreman_inator import config as config_mod
from foreman_inator import locks, paths, rules_show, sweep
from foreman_inator import worker_experiments as wx
from foreman_inator import worker_promote as wp
from foreman_inator import worker_status as ws
from foreman_inator.cli import app
from foreman_inator.config import Config, ConfigError, local_tier2_models
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import replay_sets as rs
from foreman_inator.resources import ollama
from foreman_inator.review import reviewer as rv
from foreman_inator.routing import knn
from foreman_inator.tickets import lifecycle
from foreman_inator.worker_actions import Ctx


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


def _invoke(*args: str) -> Any:
    r = CliRunner().invoke(app, list(args))
    return r


# --------------------------------------------------------------------------- #42


@pytest.mark.parametrize("with_db", [False, True])
def test_fman_rules_show_calls_rules_show_run(
    foreman_home: Path, monkeypatch: pytest.MonkeyPatch, with_db: bool
) -> None:
    if with_db:
        Store(paths.db_path()).close()
    calls: list[dict[str, Any]] = []

    def run(repo: str, **kw: Any) -> int:
        calls.append({"repo": repo, **kw})
        kw["echo"]("shown")
        return 0

    monkeypatch.setattr(rules_show, "run", run)
    r = _invoke("rules", "show", REPO, "--ref", "abc123", "--file", "a.py", "--file", "b/c.py")
    assert r.exit_code == 0, r.output
    assert "shown" in r.output
    (c,) = calls
    assert c["repo"] == REPO and c["ref"] == "abc123"
    assert list(c["owned_files"]) == ["a.py", "b/c.py"]
    assert isinstance(c["config"], Config)  # no config.toml: the defaults
    assert (c["store"] is not None) == with_db


# --------------------------------------------------------------------------- #49


def test_fman_repo_set_cloud_saves_the_setting(foreman_home: Path) -> None:
    Store(paths.db_path()).close()
    r = _invoke("repo", "set-cloud", REPO, "off")
    assert r.exit_code == 0, r.output
    with Store(paths.db_path()) as s:
        assert not s.repo_allows_cloud(REPO)
    assert _invoke("repo", "set-cloud", REPO, "on").exit_code == 0
    with Store(paths.db_path()) as s:
        assert s.repo_allows_cloud(REPO)
    assert _invoke("repo", "set-cloud", REPO, "maybe").exit_code != 0


def test_route_limits_are_local_only_for_a_cloud_false_repo(env: Env) -> None:
    env.label()
    env.worker.poll_and_handle()
    t = env.ticket()
    assert env.worker.limits(t) is None  # today's rules
    env.store.set_cloud(REPO, False)
    lim = env.worker.limits(env.store.get_ticket(t.id))
    assert lim is not None and lim.cloud is False


def test_knn_and_backfill_never_use_a_cloud_embedder_for_local_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path, allow_cloud_embedder=True)
    try:
        seen: list[bool] = []
        monkeypatch.setattr(
            knn, "route_knn", lambda t, **kw: seen.append(kw["allow_cloud_embedder"])
        )
        monkeypatch.setattr(
            knn,
            "backfill",
            lambda store, **kw: (
                seen.append(kw["allow_cloud_embedder"]) or knn.BackfillResult((), 0)
            ),
        )
        e.label()
        e.worker.poll_and_handle()
        # The poll queues one backfill on the local lane; let it finish so the calls
        # below are the only ones counted (wave 17: the poll does more after queueing).
        e.worker.drain_background(timeout=10.0)
        seen.clear()
        t = e.ticket()
        e.worker._knn_route(t)
        e.worker._backfill_once()
        e.store.set_cloud(REPO, False)
        e.worker._knn_route(t)
        e.worker._backfill_once()
        assert seen == [True, True, False, False]
    finally:
        e.close()


def _ep(name: str, kind: str) -> Any:
    return SimpleNamespace(name=name, is_cloud=kind == "cloud")


@pytest.mark.parametrize(("kinds", "cloud"), [(("local", "local"), False),
                                              (("local", "cloud"), True)])  # fmt: skip
def test_replay_cases_leave_out_local_only_repos_for_cloud_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kinds: tuple[str, str], cloud: bool
) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(rs, "list_cases", lambda store, set_id, **kw: seen.append(
        kw["cloud_models"]) or [])  # fmt: skip
    monkeypatch.setattr(rr, "_tap_for", lambda proxy: None)
    with Store(tmp_path / "x.db") as store:
        deps = SimpleNamespace(store=store, proxy=None)
        champ = SimpleNamespace(endpoint=_ep("a", kinds[0]))
        chall = SimpleNamespace(endpoint=_ep("b", kinds[1]))
        rr._drive(deps, rr.ReplaySettings(), 1, 1, "writer", champ, chall, __import__(
            "threading").Event())  # fmt: skip
    assert seen == [cloud]


def test_status_lists_local_only_repos(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        assert ws.local_only_lines(store) == []
        store.set_cloud("me/secret", False)
        assert "me/secret" in ws.render_status(store)
        assert ws.local_only_lines(store) == [
            "Local only (cloud = false, `fman repo set-cloud`): me/secret"
        ]


def test_local_only_and_split_proposed_are_dormant_flags() -> None:
    assert {"local_only", "split_proposed"} <= locks.DORMANT_FLAGS


# --------------------------------------------------------------------------- #34


def test_fman_experiment_split_commands(foreman_home: Path, monkeypatch) -> None:
    Store(paths.db_path()).close()
    config_mod.save(Config())
    calls: list[tuple[Any, ...]] = []
    exp = SimpleNamespace(id=7, challenger_variant="glm/writer-v1", champion_variant="q/w")
    monkeypatch.setattr(wx, "start_split", lambda s, c, v, share: calls.append(("start", v,
                        share)) or exp)  # fmt: skip
    monkeypatch.setattr(wx, "stop_split", lambda s, i: calls.append(("stop", i)) or exp)
    monkeypatch.setattr(wx, "split_lines", lambda s, c, i: calls.append(("status", i))
                        or ["split line"])  # fmt: skip
    assert _invoke("experiment", "split", "start", "glm", "--share", "0.2").exit_code == 0
    assert _invoke("experiment", "split", "stop", "7").exit_code == 0
    r = _invoke("experiment", "split", "status")
    assert r.exit_code == 0 and "split line" in r.output
    assert calls == [("start", "glm", 0.2), ("stop", 7), ("status", None)]


# --------------------------------------------------------------------------- #35


def test_fman_experiment_run_mode_seeded_reaches_run_command(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(wx, "run_command", lambda **kw: calls.append(kw) or 0)
    r = _invoke("experiment", "run", "--role", "reviewer", "--challenger", "glm",
                "--mode", "seeded", "--seed", "3", "--kinds", "drop_if_guard")  # fmt: skip
    assert r.exit_code == 0, r.output
    (c,) = calls
    assert (c["mode"], c["seed"], c["kinds"]) == ("seeded", 3, ["drop_if_guard"])


def test_seeded_run_calls_run_seeded_with_reviewer_variants(tmp_path: Path, monkeypatch) -> None:
    got: dict[str, Any] = {}

    def run_seeded(deps: Any, **kw: Any) -> Any:
        got.update(kw)
        return SimpleNamespace(experiment_id=4)

    monkeypatch.setattr(rr, "run_seeded", run_seeded)
    monkeypatch.setattr(rr, "seeded_note", lambda c: "seeded note")
    out: list[str] = []
    with Store(tmp_path / "x.db") as store:
        cfg = Config(models=local_tier2_models())
        env = wx.RunEnv(store, cfg, deps=None)  # type: ignore[arg-type]
        wx.run_seeded_experiment(env, "glm", seed=5, kinds=["neutralise_check"], echo=out.append)
    assert isinstance(got["champion"], rr.ReviewerVariant)
    assert isinstance(got["challenger"], rr.ReviewerVariant)
    assert got["challenger"].endpoint.name == "glm"
    assert (got["seed"], got["kinds"]) == (5, ["neutralise_check"])
    assert out[0] == "seeded note"
    with pytest.raises(wx.ExperimentError, match="seeded"):
        wx.run_command(role="writer", challenger="glm", all_=False, mode="seeded")


def test_seeded_status_shows_the_result_note() -> None:
    exp = SimpleNamespace(mode="seeded", role="reviewer", result_note="catch 3/4\ndetails")
    assert wp.summary(None, exp, Config()) == "catch 3/4"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #50 / #72 config


def test_new_config_keys_round_trip_and_validate() -> None:
    cfg = Config(split_large=False, split_timeout_seconds=60.0, reviewer_order="cheapest")
    back = config_mod.from_dict(config_mod.to_dict(cfg))
    assert (back.split_large, back.split_timeout_seconds, back.reviewer_order) == (
        False, 60.0, "cheapest")  # fmt: skip
    assert Config().reviewer_order == "configured" and Config().split_large is True
    with pytest.raises(ConfigError, match="reviewer_order"):
        Config(reviewer_order="random")


def test_split_cloud_check_is_the_repos_cloud_setting(env: Env) -> None:
    assert env.worker._cloud_allowed(REPO)
    env.store.set_cloud(REPO, False)
    assert not env.worker._cloud_allowed(REPO)


def test_tier_label_after_a_split_proposal_clears_the_flag(env: Env) -> None:
    env.label()
    env.worker.poll_and_handle()
    t = env.store.flag_ticket(env.ticket().id, "split_proposed: 3 sub-issues proposed")
    ev = lifecycle.TierOverride(REPO, t.issue_number, ticket_id=t.id, tier=2, labeled_by="brooks")
    env.worker._a_tier_override(Ctx(t, ev))
    t = env.store.get_ticket(t.id)
    assert t.flag_reason is None and t.routed_tier == 2


# --------------------------------------------------------------------------- #77


def test_fman_sweep_uses_the_classifier_router(foreman_home: Path, monkeypatch) -> None:
    config_mod.save(Config(repos=(REPO,)))
    sentinel = object()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sweep, "classifier_router", lambda cfg, repo: sentinel)
    monkeypatch.setattr(sweep, "run", lambda repo, **kw: calls.append({"repo": repo, **kw}) or 0)
    r = _invoke("sweep", REPO, "--label", "bug", "--exclude-label", "wontfix",
                "--limit", "3", "--dry-run")  # fmt: skip
    assert r.exit_code == 0, r.output
    (c,) = calls
    assert c["router"] is sentinel and c["dry_run"] is True and c["yes"] is False
    assert c["filters"] == sweep.Filters(label="bug", exclude_label=("wontfix",), limit=3)


def test_status_waits_on_the_open_pr_cap(tmp_path: Path, monkeypatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(ws, "open_pr_wait_text", lambda s, t, cap: seen.append(cap)
                        or "open-PR cap (5/5)")  # fmt: skip
    with Store(tmp_path / "s.db") as store:
        store.create_ticket(repo=REPO, issue_number=1, title="t", request="r",
                            base_branch="main")  # fmt: skip
        text = ws.render_status(store, config=Config(max_open_prs_per_repo=5))
    assert "WAITING: open-PR cap (5/5)" in text and set(seen) == {5}


# --------------------------------------------------------------------------- #72


def test_cheapest_reviewer_order_reaches_the_review(tmp_path: Path, monkeypatch) -> None:
    e = Env(tmp_path, reviewer_order="cheapest")
    try:
        orders: list[str] = []
        picks: list[str] = []
        real_esc, real_pick = rv.review_with_escalation, rv.pick_reviewer

        def esc(review: Any, **kw: Any) -> Any:
            orders.append(kw["order"])
            return real_esc(review, **kw)

        def pick(config: Any, writer: Any, **kw: Any) -> Any:
            picks.append(kw.get("order", "configured"))
            return real_pick(config, writer, **kw)

        monkeypatch.setattr(rv, "review_with_escalation", esc)
        monkeypatch.setattr(rv, "pick_reviewer", pick)
        e.label()
        e.once(passing(model_call=False))
        assert orders == ["cheapest"] and picks == ["cheapest"]
    finally:
        e.close()


# --------------------------------------------------------------------------- Ollama


def test_ollama_status_says_slow_on_a_read_timeout(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    got = ollama.loaded_models("http://127.0.0.1:11434", transport=httpx.MockTransport(handler))
    assert isinstance(got, ollama.Unreachable) and got.slow

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    down = ollama.loaded_models("http://127.0.0.1:11434", transport=httpx.MockTransport(refused))
    assert isinstance(down, ollama.Unreachable) and not down.slow
    timeouts: list[float] = []
    monkeypatch.setattr(ws, "loaded_models", lambda host, **kw: timeouts.append(kw["timeout"])
                        or got)  # fmt: skip
    line = ws.ollama_line("http://127.0.0.1:11434")
    assert line.startswith("Ollama: slow (no reply") and timeouts == [5.0]
