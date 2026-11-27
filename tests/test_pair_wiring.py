"""ab:pair (#16) wave 24 wiring: each point the slots left for the wave's close is
called. Each test fails with its wiring reverted."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes.worker_env import REPO, USER, Env, passing
from foreman_inator import cli, worker_attempt, worker_experiments
from foreman_inator import config as config_mod
from foreman_inator.experiments.pair import PairRecord
from foreman_inator.github.poller import PAIR_LABEL, PairReapplied
from foreman_inator.metrics import web
from foreman_inator.runner.worktree import RepoMirror, WorktreeError
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import Action
from foreman_inator.worker_actions import (
    PAIR_DISABLED_TEXT,
    PAIR_REAPPLIED_TEXT,
    PAIR_SPLIT_TEXT,
)
from foreman_inator.worker_queue import Job


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()


def label_pair(e: Env) -> int:
    n = e.gh.create_issue(REPO, "Add a feature", "Please add feature().", author=USER)
    e.gh.apply_label(REPO, n, PAIR_LABEL, by=USER)
    e.gh.apply_label(REPO, n, "agent", by=USER)
    return n


def run(e: Env, times: int = 4) -> None:
    e.oc.set_script(passing())
    for _ in range(times):
        e.worker.run_once()


def sides(e: Env, n: int):
    ts = e.store.tickets_for_issue(REPO, n)
    a = next(t for t in ts if t.pair_side != "b")
    b = next(t for t in ts if t.pair_side == "b")
    return e.store.get_ticket(a.id), e.store.get_ticket(b.id)


# --------------------------------------------------------------------------- worker.py


def test_worker_actions_map_the_pair_actions(env: Env):
    w = env.worker
    assert w.actions[Action.CREATE_PAIR_SIDE] == w._a_create_pair_side
    assert w.actions[Action.CLOSE_OWN_PR] == w._a_close_own_pr
    assert w.actions[Action.DELETE_OWN_BRANCH] == w._a_delete_own_branch
    assert w.actions[Action.RECORD_PAIR_RESULT] == w._a_record_pair_result


def test_pair_reapplied_posts_one_comment_and_skips_the_lifecycle(env: Env, monkeypatch):
    e = env
    n = label_pair(e)
    run(e, 1)
    a, _b = sides(e, n)
    pair = e.store.pair_for_ticket(a.id)
    called: list[object] = []
    real = lifecycle.apply
    monkeypatch.setattr(lifecycle, "apply", lambda c, ev: called.append(ev) or real(c, ev))
    ev = PairReapplied(REPO, n, ticket_id=a.id, pair_id=pair.id, labeled_by=USER,
                       label_event_id=99)  # fmt: skip
    e.worker._local.pending, e.worker._local.ready = [], []
    e.worker._handle_event(ev)
    e.worker._handle_event(ev)  # the same labeling again: still one comment
    for _tid, _name, effect in [*e.worker._local.ready, *e.worker._local.pending]:
        effect()
    e.worker._local.pending = e.worker._local.ready = None
    assert called == []
    assert sum(PAIR_REAPPLIED_TEXT in c for c in e.issue_comments(n)) == 1


def test_pair_disabled_ignores_ab_pair_with_one_comment(tmp_path: Path):
    e = Env(tmp_path, pair_enabled=False)
    try:
        n = label_pair(e)
        run(e, 1)
        ts = e.store.tickets_for_issue(REPO, n)
        assert len(ts) == 1 and e.store.list_pairs() == []
        assert sum(PAIR_DISABLED_TEXT in c for c in e.issue_comments(n)) == 1
        # added late (PairRequested): ignored too
        m = e.gh.create_issue(REPO, "Other", "Please add other().", author=USER)
        e.gh.apply_label(REPO, m, "agent", by=USER)
        run(e, 1)
        e.gh.apply_label(REPO, m, PAIR_LABEL, by=USER)
        run(e, 1)
        assert len(e.store.tickets_for_issue(REPO, m)) == 1 and e.store.list_pairs() == []
        assert sum(PAIR_DISABLED_TEXT in c for c in e.issue_comments(m)) == 1
    finally:
        e.close()


def test_pair_enabled_is_a_config_key():
    assert config_mod.Config().pair_enabled is True
    cfg = config_mod.from_dict({"pair_enabled": False})
    assert cfg.pair_enabled is False
    assert config_mod.to_dict(cfg)["pair_enabled"] is False
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"pair_enabled": "yes"})


# --------------------------------------------------------------------------- opponent timing


def test_opponent_is_chosen_from_side_a_route_tier_2(env: Env):
    """A routes to tier 2 (after CREATE_PAIR_SIDE ran unrouted): B's opponent is the rung
    above A's tier 2 start, stored on the pair row before B's first attempt."""
    e = env
    e.route_label = "normal"  # tier 2
    n = label_pair(e)
    run(e, 3)
    a, b = sides(e, n)
    assert a.routed_tier == 2
    ladder = [m.name for m in e.config.models]
    a_model = e.store.list_attempts(a.id)[0].model
    want = ladder[ladder.index(a_model) + 1]
    pair = e.store.pair_for_ticket(a.id)
    assert (pair.opponent_variant, pair.opponent_reason) == (want, "next_rung")
    b_att = e.store.list_attempts(b.id)
    assert b_att and b_att[0].model == want != a_model


# --------------------------------------------------------------------------- experiments


def test_ensure_pair_gives_create_pair_the_pair_experiment_id(env: Env, monkeypatch):
    e = env
    seen: list[str] = []

    def fake_ensure(store, opp, *, champion):
        seen.append(champion)
        return SimpleNamespace(id=exp.id)

    exp = e.store.create_experiment(
        role="writer", champion_variant="x/writer-v1@0", challenger_variant="y/writer-v1@0",
        mode="pair",
    )  # fmt: skip
    monkeypatch.setattr(worker_experiments, "ensure_pair", fake_ensure)
    n = label_pair(e)
    run(e, 1)
    a, _b = sides(e, n)
    assert seen and seen[0]  # the writer champion's id
    assert e.store.pair_for_ticket(a.id).experiment_id == exp.id


def test_record_pair_result_refreshes_the_pair_note(env: Env, monkeypatch):
    e = env
    exp = e.store.create_experiment(
        role="writer", champion_variant="x/writer-v1@0", challenger_variant="y/writer-v1@0",
        mode="pair",
    )  # fmt: skip
    monkeypatch.setattr(
        worker_experiments, "ensure_pair", lambda s, o, *, champion: SimpleNamespace(id=exp.id)
    )
    refreshed: list[int] = []
    real = worker_experiments.refresh_pair_note
    monkeypatch.setattr(
        worker_experiments,
        "refresh_pair_note",
        lambda s, i: refreshed.append(i) or real(s, i),
    )
    n = label_pair(e)
    run(e)
    a, _b = sides(e, n)
    e.gh.merge_pr(REPO, a.pr_number)
    run(e, 2)
    assert exp.id in refreshed
    assert "1-0" in (e.store.get_experiment(exp.id).result_note or "")


# --------------------------------------------------------------------------- branches


def test_rebase_passes_side_b(env: Env, monkeypatch):
    e = env
    n = label_pair(e)
    run(e)
    _a, b = sides(e, n)
    seen: dict[str, object] = {}

    def fake_replay(self, *args, **kw):
        seen.update(kw)
        raise WorktreeError("stop here")

    monkeypatch.setattr(RepoMirror, "replay_onto_base", fake_replay)
    monkeypatch.setattr(type(e.worker), "rebase_pending", lambda self, t, a=None: True)
    e.store.set_column(b.id, "in_progress")
    e.worker.rebase_job(Job(REPO, b.id, "rebase"), threading.Event())
    assert seen.get("side") == "b"
    assert seen.get("previous") == f"refs/remotes/origin/fman/issue-{n}-b/a1"


def test_start_point_asks_the_side_aware_pr_head(monkeypatch):
    t = SimpleNamespace(issue_number=7, pr_number=12, pair_side="b")
    asked: list[object] = []
    monkeypatch.setattr(worker_attempt, "pr_head_branch", lambda x: asked.append(x) or "fman/x")
    monkeypatch.setattr(worker_attempt, "_branch_exists", lambda m, ref: True)
    assert worker_attempt._start_point(None, t, []) == "refs/remotes/origin/fman/x"
    assert asked == [t]


def test_side_a_split_proposed_drops_side_b_with_a_note(env: Env):
    e = env
    n = e.gh.create_issue(REPO, "Add a feature", "Please add feature().", author=USER)
    a = e.store.create_ticket(repo=REPO, issue_number=n, title="t", request="r",
                              base_branch="main")  # fmt: skip
    b = e.store.create_ticket(repo=REPO, issue_number=n, title="t", request="r",
                              base_branch="main", pair_side="b")  # fmt: skip
    pair = e.store.create_pair(ticket_a=a.id, ticket_b=b.id, opponent_variant="m",
                               opponent_reason="next_rung")  # fmt: skip
    e.store.update_ticket(a.id, routed_tier=3, route_reason="large")
    e.store.flag_ticket(a.id, "split_proposed: 2 proposed on the issue")
    e.worker.route_job(Job(REPO, b.id, "route"), threading.Event())
    assert e.store.get_ticket(b.id).column == "dropped"
    assert e.store.list_attempts(b.id) == []
    assert e.store.get_pair(pair.id).status == "dropped"
    assert any(PAIR_SPLIT_TEXT in c for c in e.issue_comments(n))


def test_side_a_pr_body_gets_the_line_when_b_opens(env: Env, monkeypatch):
    e = env
    added: list[int] = []
    real = e.worker.publisher.add_pair_line
    monkeypatch.setattr(
        e.worker.publisher, "add_pair_line", lambda t: added.append(t.id) or real(t)
    )
    n = label_pair(e)
    run(e)
    a, _b = sides(e, n)
    assert a.id in added


# --------------------------------------------------------------------------- cli / web


def test_retry_help_names_the_pair_side_form():
    from typer.testing import CliRunner

    out = CliRunner().invoke(cli.app, ["retry", "--help"]).output
    assert "issue:a|b" in " ".join(out.split())


def test_metrics_page_renders_pairs_escaped():
    rep = SimpleNamespace(
        pairs=[PairRecord("<a>", "b&c", 2, 1, 0.5, 0.25)],
    )
    html = web._pairs(rep)
    assert "&lt;a&gt;" in html and "b&amp;c" in html and "2-1" in html
    assert "<a>" not in html
    assert "empty" in web._pairs(SimpleNamespace(pairs=[]))
