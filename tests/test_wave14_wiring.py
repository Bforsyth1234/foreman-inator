"""Wave 14 wiring: each slot's piece is actually called by the worker.

#52 cold-load watch on the clarity check and the router; #34 one local-slot cap shared
by the proxy and the driver's extra time; #51 repo profiles (policy limits, start tier,
the thrifty writer gate, the review-off hand-over, shadows skip light reviews, the PR
line, ``fman repo set-profile``) with todo #51's Live expectations; #38/#40 the
conventions gather with the repo's ``rules_file`` and its hash on the attempt span.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from fakes.opencode import Script, edit, say
from fakes.worker_env import REPO, Env, passing
from foreman_inator import config as config_mod
from foreman_inator import paths, routing, tracing, worker_run
from foreman_inator import worker_experiments as wx
from foreman_inator.cli import app
from foreman_inator.config import Config, ConfigError
from foreman_inator.db.store import Store
from foreman_inator.proxy.server import LocalSlots
from foreman_inator.routing import profile as prof
from foreman_inator.spend import Spend
from foreman_inator.tickets import clarify
from foreman_inator.tickets import conventions as conventions_mod
from foreman_inator.worker import ollama_watch_for
from foreman_inator.worker_actions import REVIEW_OFF_TEXT


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.close()


# --------------------------------------------------------------------------- config


def test_new_config_keys_round_trip_and_validate() -> None:
    cfg = config_mod.from_dict(
        {
            "cold_load_timeout_seconds": 90,
            "max_local_inference": 2,
            "rules_file": {REPO: "~/rules.md"},
        }
    )
    assert (cfg.cold_load_timeout_seconds, cfg.max_local_inference) == (90.0, 2)
    assert cfg.rules_file_for(REPO.upper()) == Path("~/rules.md").expanduser()
    assert cfg.rules_file_for("o/other") is None
    assert config_mod.from_dict(config_mod.to_dict(cfg)) == cfg
    defaults = Config()
    assert (defaults.cold_load_timeout_seconds, defaults.max_local_inference) == (120.0, 1)
    with pytest.raises(ConfigError, match="max_local_inference"):
        Config(max_local_inference=0)
    with pytest.raises(ConfigError, match="rules_file"):
        Config(rules_file={"not-a-repo": "x"})


# --------------------------------------------------------------------------- #52


class FakeWatch:
    def models(self) -> list[Any]:
        return []

    def is_loaded(self, model: str) -> bool:
        return False


def test_clarify_and_router_get_the_watch_and_the_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = Env(tmp_path, cold_load_timeout_seconds=77)
    try:
        seen: dict[str, dict[str, Any]] = {}
        real_assess, real_route = clarify.assess, routing.route

        def assess(*a: Any, **kw: Any) -> Any:
            seen["assess"] = kw
            return real_assess(*a, **kw)

        def route(*a: Any, **kw: Any) -> Any:
            seen["route"] = kw
            return real_route(*a, **kw)

        monkeypatch.setattr(clarify, "assess", assess)
        monkeypatch.setattr(routing, "route", route)
        e.worker.ollama_watch = watch = FakeWatch()
        e.label()
        e.once(passing(model_call=False))
        for name in ("assess", "route"):
            assert seen[name]["watch"] is watch, name
            assert seen[name]["cold_load_allowance"] == 77.0, name
    finally:
        e.close()


def test_the_watch_asks_tier_ones_local_ollama() -> None:
    assert ollama_watch_for(Config()).host == "http://127.0.0.1:11434"


# --------------------------------------------------------------------------- #34


def test_worker_proxy_and_driver_share_the_cap_and_the_queue_wait(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = Config(max_local_inference=3)
    proxy, driver = worker_run.build_proxy_and_driver(cfg, store, Spend(store, cfg))
    try:
        assert proxy.local_slots() == (0, 3)
        assert driver.extra_time == proxy.queue_wait
        # #34: the cap is the lock files under the home, shared with `fman experiment run`
        assert proxy.slots.lock_dir == paths.run_dir()
    finally:
        proxy.stop()
        store.close()


def test_worker_and_experiment_run_share_one_cross_process_cap(tmp_path: Path) -> None:
    """Non-vacuity: with `make_proxy` building `LocalSlots(cap)` (in-process) again,
    its `lock_dir` is None and this fails; so does reverting `build_proxy_and_driver`."""
    store = Store(tmp_path / "db.sqlite")
    cfg = Config(max_local_inference=1)
    spend = Spend(store, cfg)
    worker_proxy, _driver = worker_run.build_proxy_and_driver(cfg, store, spend)
    replay_proxy = wx.make_proxy(cfg, store, spend)
    try:
        assert worker_proxy.slots is not replay_proxy.slots
        assert worker_proxy.slots.lock_dir == replay_proxy.slots.lock_dir == paths.run_dir()
        assert replay_proxy.local_slots() == (0, 1)
    finally:
        worker_proxy.stop()
        replay_proxy.stop()
        store.close()


def test_launch_writer_forgets_the_attempts_queue_wait(tmp_path: Path) -> None:
    """P2: `ModelProxy._waits` must not grow per attempt. Non-vacuity: without the
    `forget_queue_wait` call in `_launch_writer`'s `finally`, `forgotten` stays empty."""
    from foreman_inator.worker_attempt import AttemptSteps

    forgotten: list[int] = []

    def boom(**kw: Any) -> Any:
        raise RuntimeError("writer crashed")

    stub = SimpleNamespace(
        config=Config(),
        driver=SimpleNamespace(run=lambda **kw: "ran"),
        proxy=SimpleNamespace(forget_queue_wait=forgotten.append),
        _sandbox_policy=lambda *a, **k: None,
        _drop_scratch=lambda policy: None,
    )
    run = AttemptSteps._launch_writer
    assert run(stub, ticket=None, worktree=tmp_path, attempt_id=9, prompt="p") == "ran"
    stub.driver = SimpleNamespace(run=boom)
    with pytest.raises(RuntimeError):
        run(stub, ticket=None, worktree=tmp_path, attempt_id=10, prompt="p")
    assert forgotten == [9, 10]  # on every path


def test_experiment_proxy_shares_the_workers_slots(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    cfg = Config(max_local_inference=2)
    spend = Spend(store, cfg)
    shared = LocalSlots(1)
    inside = wx.make_proxy(cfg, store, spend, local_slots=shared)
    alone = wx.make_proxy(cfg, store, spend)
    try:
        assert inside.slots is shared
        assert alone.local_slots() == (0, 2)
    finally:
        inside.stop()
        alone.stop()
        store.close()


def test_experiment_run_driver_gets_the_proxy_queue_wait(
    foreman_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foreman_inator import paths

    Store(paths.db_path()).close()
    proxy = SimpleNamespace(queue_wait=lambda attempt_id: 0.0, stop=lambda: None)
    made: list[dict[str, Any]] = []
    monkeypatch.setattr(wx, "make_proxy", lambda *a, **k: proxy)
    monkeypatch.setattr(
        "foreman_inator.runner.opencode.OpenCodeDriver", lambda **kw: made.append(kw) or object()
    )
    monkeypatch.setattr(wx, "run_all", lambda env, echo: None)
    assert wx.run_command(role=None, challenger=None, all_=True, echo=lambda s: None) == 0
    assert made == [{"extra_time": proxy.queue_wait}]


# --------------------------------------------------------------------------- #51


def test_fast_profile_starts_the_first_attempt_one_tier_up(env: Env) -> None:
    """``policy_for`` passes the profile's limits and ``history`` its start tier."""
    env.store.set_profile(REPO, "fast", "normal")
    n = env.label()
    env.once(passing(model_call=False))
    assert [(a.tier, a.trigger) for a in env.attempts(n)] == [(2, "first")]


def test_default_profile_policy_is_called_without_limits(env: Env) -> None:
    calls: list[dict[str, Any]] = []
    real = env.worker.policy
    env.worker.policy = lambda h, **kw: calls.append(kw) or real(h, **kw)
    n = env.label()
    env.once(passing(model_call=False))
    assert calls and all(kw == {} for kw in calls)
    assert [a.tier for a in env.attempts(n)] == [1]


def test_profile_policy_is_called_with_its_limits(env: Env) -> None:
    calls: list[dict[str, Any]] = []
    real = env.worker.policy
    env.worker.policy = lambda h, **kw: calls.append(kw) or real(h, **kw)
    env.store.set_profile(REPO, "thrifty", "strict")
    env.label()
    env.once(passing(model_call=False))
    want = prof.limits_for(prof.Profile("thrifty", "strict"))
    assert calls and all(kw == {"limits": want} for kw in calls)


def test_thrifty_cloud_writer_waits_at_the_writer_gate(env: Env, monkeypatch) -> None:
    gates: list[bool] = []
    real = env.spend.writer_gate

    def gate(ticket_id: int, *, approval_required: bool = False) -> Any:
        gates.append(approval_required)
        return real(ticket_id, approval_required=approval_required)

    monkeypatch.setattr(env.spend, "writer_gate", gate)
    env.store.set_profile(REPO, "thrifty", "normal")
    n = env.label("Big", "Please add feature().", "tier:3")
    env.once(passing(model_call=False))
    assert gates == [True]
    assert env.attempts(n) == []  # paused for `fman approve-spend`, nothing ran
    assert env.ticket(n).flag_reason


def test_live_prototype_prs_ready_with_no_bot_review(tmp_path: Path) -> None:
    """todo #51 Live: a prototype repo, two issues: both PRs ready, no bot review. (The
    two changes touch different files, so neither waits on the other's file locks.)"""
    env = Env(tmp_path, verify="true")
    try:
        p = prof.preset("prototype")
        env.store.set_profile(REPO, p.writer, p.review)
        issues = [env.label("One", "Please add one()."), env.label("Two", "Add two().")]
        for name in ("one.py", "two.py"):
            env.once(Script([edit(name, "X = 1\n"), say("Done.")]))
        assert env.review_calls == 0
        for n in issues:
            t = env.ticket(n)
            assert t.pr_number is not None, n
            pr = env.gh.get_pr(REPO, t.pr_number)
            assert not pr.draft and env.bot_reviews(t.pr_number) == []
            assert "Repo profile: prototype (writer fast, review off)" in pr.body
            assert t.flag_reason.startswith("review_skipped: review is off")
            assert any(REVIEW_OFF_TEXT in c for c in env.issue_comments(n))
        assert env.gh.ready_violations == []
    finally:
        env.close()


def test_live_production_with_review_skip_still_reviews(env: Env) -> None:
    """todo #51 Live: production + ``review:skip``: the review still runs."""
    env.store.set_profile(REPO, "fast", "strict")
    n = env.label("Typo", "Fix the typo.", "review:skip")
    env.once(passing(model_call=False))
    assert env.review_calls == 1 and len(env.reviews(n)) == 1
    assert env.bot_reviews(env.ticket(n).pr_number)
    assert not (env.ticket(n).flag_reason or "").startswith("review_skipped")


def test_review_skip_label_on_a_default_repo_keeps_its_reason(env: Env) -> None:
    n = env.label("Typo", "Fix the typo.", "review:skip")
    env.once(passing(model_call=False))
    assert env.review_calls == 0
    assert env.ticket(n).flag_reason.startswith("review_skipped: the review:skip label")
    assert "Repo profile" not in env.gh.get_pr(REPO, env.ticket(n).pr_number).body


@pytest.mark.parametrize(("review", "queued"), [("light", 0), ("normal", 1)])
def test_shadows_skip_light_reviews(env: Env, monkeypatch, review: str, queued: int) -> None:
    env.store.set_profile(REPO, "normal", review)
    env.label()
    env.once(passing(model_call=False))
    from foreman_inator import worker_background as wb

    ch = SimpleNamespace(experiment_id=1, variant_id="reviewer:challenger")
    monkeypatch.setattr(wb.shadow, "find_challenger", lambda store, cfg: ch)
    monkeypatch.setattr(wb.shadow, "recorded_skip", lambda *a: False)
    monkeypatch.setattr(
        env.store,
        "get_experiment",
        lambda i: SimpleNamespace(started_at=datetime.min.replace(tzinfo=UTC)),
    )
    submitted: list[str] = []
    monkeypatch.setattr(env.worker.shadow_lane, "submit", lambda k, fn: submitted.append(k) or True)
    assert env.worker.queue_shadows(REPO) == queued
    assert len(submitted) == queued


def test_fman_repo_set_profile(foreman_home: Path) -> None:
    from foreman_inator import paths

    Store(paths.db_path()).close()
    r = CliRunner().invoke(app, ["repo", "set-profile", REPO, "personal"])
    assert r.exit_code == 0, r.output
    assert "personal (writer thrifty, review light)" in r.output
    r = CliRunner().invoke(app, ["repo", "set-profile", REPO, "--review", "strict"])
    assert r.exit_code == 0, r.output
    with Store(paths.db_path()) as store:
        assert prof.profile_of(store.get_repo_settings(REPO)) == prof.Profile("thrifty", "strict")
    r = CliRunner().invoke(app, ["repo", "set-profile", REPO, "turbo"])
    assert r.exit_code != 0 and "valid" in r.output
    assert CliRunner().invoke(app, ["repo", "set-profile", REPO]).exit_code != 0


# --------------------------------------------------------------------------- #38/#40


def test_attempt_gathers_conventions_with_the_rules_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = tmp_path / "team-rules.md"
    rules.write_text("Always write a docstring.\n")
    e = Env(tmp_path / "e", rules_file={REPO: str(rules)})
    try:
        gathered: list[Any] = []
        real = conventions_mod.gather

        def gather(*a: Any, **kw: Any) -> Any:
            gathered.append(c := real(*a, **kw))
            assert kw["rules_file"] == rules
            return c

        monkeypatch.setattr(conventions_mod, "gather", gather)
        with tracing.capture_spans() as spans:
            e.label()
            e.once(passing(model_call=False))
        assert len(gathered) == 1
        assert "Always write a docstring." in e.driver.prompts[0]
        (att,) = [s for s in spans.get_finished_spans() if s.name == "attempt"]
        assert att.attributes["conventions.sha256"] == gathered[0].sha256
    finally:
        e.close()
