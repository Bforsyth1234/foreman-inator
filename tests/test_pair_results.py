"""ab:pair results (todo #34, GH #16, wave 23 E): metrics, the pair experiment, status,
promote (shown, never gating: Decision 2), the metrics page and the redacted export.

All on the real ``Store`` (migration 016).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foreman_inator import export, prompts
from foreman_inator import worker_experiments as wx
from foreman_inator.config import Config, ExperimentSettings, ModelEndpoint
from foreman_inator.db.store import Store, Ticket
from foreman_inator.experiments import pair, variants
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.metrics import experiments_ui, web
from foreman_inator.metrics import queries as q

LOCAL_URL = "http://127.0.0.1:11434/v1"
REPO = "acme/widgets"


def make_config() -> Config:
    return Config(
        app_id=1,
        allowed_users=("brooks",),
        repos=(REPO,),
        models=(
            ModelEndpoint("w-one", 1, "local", "w-one-up", LOCAL_URL, "fam-w"),
            ModelEndpoint("w-alt", 1, "local", "w-alt-up", LOCAL_URL, "fam-w"),
            ModelEndpoint("w-two", 2, "local", "w-two-up", LOCAL_URL, "fam-w"),
            ModelEndpoint("rev-x", 3, "local", "rev-x-up", LOCAL_URL, "fam-x"),
        ),
        reviewer_models=("rev-x",),
        experiments=ExperimentSettings(min_cases=2, live_min_sample=2),
    )


def vid(model: str) -> str:
    return variants.variant_id(model, prompts.load("writer-v1"))


@pytest.fixture
def env(tmp_path: Path):
    store = Store(tmp_path / "fman.db")
    config = make_config()
    variants.ensure_defaults(store, config)
    yield store, config
    store.close()


def ticket(store: Store, n: int, side: Any = None) -> Ticket:
    t = store.create_ticket(
        repo=REPO,
        issue_number=n,
        title=f"T{n}",
        request="Do it.",
        base_branch="main",
        route_reason="classifier: small",
        routed_tier=1,
        pair_side=side,
    )
    store.set_column(t.id, "in_progress")
    return t


def attempt(store: Store, t: Ticket, model: str, cost: float, review: float = 0.0) -> int:
    a = store.add_attempt(t.id, model=model, tier=1, trigger="first", variant_id=vid(model))
    store.finish_attempt(a.id, "accepted", cost_usd=cost)
    if review:
        store.record_review(a.id, reviewer_model="rev-x", verdict="approve", cost_usd=review)
    store.record_review(a.id, reviewer_model="rev-x", verdict="approve", cost_usd=50.0, shadow=True)
    return a.id


def accept(store: Store, t: Ticket, aid: int) -> None:
    store.set_accepted_attempt(t.id, aid)
    store.set_column(t.id, "done")


def played(
    store: Store,
    n: int,
    *,
    a_cost: float,
    b_cost: float,
    b_wins: bool,
    experiment_id: int | None = None,
    reason: str = "challenger",
) -> Any:
    """One decided pair on issue ``n``: side A on w-one, side B on w-alt."""
    a, b = ticket(store, n), ticket(store, n, "b")
    aa = attempt(store, a, "w-one", a_cost, review=0.25)
    ab = attempt(store, b, "w-alt", b_cost, review=0.25)
    p = store.create_pair(
        ticket_a=a.id,
        ticket_b=b.id,
        opponent_variant=vid("w-alt"),
        opponent_reason=reason,  # type: ignore[arg-type]
        experiment_id=experiment_id,
    )
    win, lose, wa = (b, a, ab) if b_wins else (a, b, aa)
    accept(store, win, wa)
    store.set_column(lose.id, "dropped")
    return pair.record(store, p, win, "merged_other")


# =========================================================================== metrics


def test_cost_per_accepted_ticket_charges_the_losers_spend(env):
    store, _ = env
    solo = ticket(store, 1)
    accept(store, solo, attempt(store, solo, "w-one", 1.0, review=0.5))
    assert q.cost_per_accepted_ticket(store).value == pytest.approx(1.5)  # unpaired: as before
    played(store, 2, a_cost=2.0, b_cost=3.0, b_wins=True)
    # winner B: 3 + .25 own, plus loser A's 2 + .25 (shadow reviews never count)
    c = q.cost_per_accepted_ticket(store)
    assert (c.count, c.total) == (2, pytest.approx(1.5 + 5.5))
    per = q.cost_per_accepted_ticket(store, q.MetricsFilter(model="w-alt"))
    assert (per.count, per.total) == (1, pytest.approx(5.5))
    [point] = q.trend(store)
    assert point.spend_usd == pytest.approx(7.0)
    # a running pair charges nothing yet
    a, b = ticket(store, 3), ticket(store, 3, "b")
    accept(store, a, attempt(store, a, "w-one", 1.0))
    attempt(store, b, "w-alt", 9.0)
    store.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant=vid("w-alt"), opponent_reason="next_rung"
    )
    assert q.cost_per_accepted_ticket(store).total == pytest.approx(7.0 + 1.0)


def test_both_prs_merged_counts_each_sides_spend_once(env):
    store, _ = env
    played(store, 1, a_cost=2.0, b_cost=3.0, b_wins=True)
    (p,) = store.list_pairs(repo=REPO, issue_number=1)
    loser = store.get_ticket(p.ticket_a)
    # a human merged the losing PR too: the loser is done, not dropped
    accept(store, loser, store.list_attempts(loser.id)[0].id)
    c = q.cost_per_accepted_ticket(store)
    # A: 2 + .25, B: 3 + .25, each once (B is not also charged A's spend)
    assert (c.count, c.total) == (2, pytest.approx(5.5))
    [point] = q.trend(store)
    assert point.spend_usd == pytest.approx(5.5)


def test_pair_record_in_the_report_and_json(env):
    store, _ = env
    rep = q.report(store)
    assert rep.pairs == [] and q.pair_record(store) == []
    played(store, 1, a_cost=1.0, b_cost=2.0, b_wins=True)
    played(store, 2, a_cost=1.0, b_cost=2.0, b_wins=False)
    played(store, 3, a_cost=1.0, b_cost=2.0, b_wins=True)
    [r] = q.report(store).pairs
    assert (r.a, r.b, r.a_wins, r.b_wins) == (vid("w-one"), vid("w-alt"), 1, 2)
    assert q.pair_record(store, q.MetricsFilter(repo="other/repo")) == []
    data = web.jsonable(q.report(store))
    assert data["pairs"][0]["b_wins"] == 2 and data["pairs"][0]["a"] == vid("w-one")
    json.dumps(data)


# =========================================================================== experiment


def test_ensure_pair_only_for_a_challenger_and_keeps_the_record(env):
    store, _ = env
    champ = vid("w-one")
    rung = pair.Opponent("w-alt", 1, "next_rung", vid("w-alt"))
    assert wx.ensure_pair(store, rung, champion=champ) is None
    assert wx.ensure_pair(store, None, champion=champ) is None
    ch = pair.Opponent("w-alt", 1, "challenger", vid("w-alt"), experiment_id=99)
    e = wx.ensure_pair(store, ch, champion=champ)
    assert (e.mode, e.role, e.status, e.challenger_variant) == (
        "pair",
        "writer",
        "running",
        vid("w-alt"),
    )
    assert e.result_note == "pairs: none decided"
    assert wx.ensure_pair(store, ch, champion=champ).id == e.id  # one per (champion, ch)
    played(store, 1, a_cost=1.0, b_cost=2.0, b_wins=True, experiment_id=e.id)
    note = wx.refresh_pair_note(store, e.id).result_note
    assert "0-1" in note and vid("w-alt") in note
    assert wx.ensure_pair(store, ch, champion=champ).result_note == note


def test_status_shows_the_pair_record(env):
    store, config = env
    ch = pair.Opponent("w-alt", 1, "challenger", vid("w-alt"))
    e = wx.ensure_pair(store, ch, champion=vid("w-one"))
    before = wx.status_lines(store, config)
    assert not any("Head-to-head" in line for line in before)  # nothing decided: unchanged
    played(store, 1, a_cost=1.0, b_cost=2.0, b_wins=True, experiment_id=e.id)
    text = "\n".join(wx.status_lines(store, config))
    assert "Head-to-head pairs (1 decided)" in text and "0-1" in text
    assert "0-1" in wx.summary(store, store.get_experiment(e.id), config)
    one = "\n".join(wx.status_lines(store, config, e.id))
    assert "writer pair, running" in one and "0-1" in one


def test_manage_page_lists_pairs_only_when_decided(env):
    store, _ = env
    assert experiments_ui._pairs_table(store) == ""
    played(store, 1, a_cost=1.0, b_cost=2.0, b_wins=False)
    html = experiments_ui._pairs_table(store)
    assert "Head-to-head pairs" in html and "<td>1-0</td>" in html and "$3.5000" in html


# =========================================================================== promote


@pytest.fixture
def wins(monkeypatch):
    def fake_compare(store: Store, experiment_id: int, *, min_cases: int = 10) -> Any:
        return SimpleNamespace(
            state="finished", verdict="wins", reason="2 of 2", wins=2, losses=0, ties=0
        )

    monkeypatch.setattr(rr, "compare", fake_compare)


def promote_scenario(tmp: Path, *, pairs: str | None, replay_only: bool) -> tuple[Any, ...]:
    tmp.mkdir(parents=True)
    store = Store(tmp / "fman.db")
    config = make_config()
    variants.ensure_defaults(store, config)
    store.create_experiment(
        role="writer",
        champion_variant=vid("w-one"),
        challenger_variant=vid("w-alt"),
        mode="replay",
    )
    if pairs is not None:  # the challenger sweeps (or loses) every head-to-head
        e = wx.ensure_pair(
            store, pair.Opponent("w-alt", 1, "challenger", vid("w-alt")), champion=vid("w-one")
        )
        for n in range(1, 6):
            played(store, n, a_cost=1.0, b_cost=1.0, b_wins=pairs == "win", experiment_id=e.id)
    res = wx.promote(store, config, vid("w-alt"), replay_only=replay_only)
    hist = [(h.variant_id, h.note) for h in variants.champion_history(store, "writer")]
    ended = [
        (x.id, x.status, x.result_note) for x in store.list_experiments() if x.mode == "replay"
    ]
    out = (
        res.outcome,
        res.message.splitlines()[0],
        res.experiments,
        variants.champion(store, "writer").id,
        hist,
        ended,
    )
    msg = res.message
    store.close()
    return out, msg


@pytest.mark.parametrize("replay_only", [False, True])
def test_promote_decision_is_identical_with_or_without_pairs(tmp_path, wins, replay_only):
    base, base_msg = promote_scenario(tmp_path / "none", pairs=None, replay_only=replay_only)
    assert "Head-to-head" not in base_msg  # unpaired output unchanged
    for kind in ("win", "lose"):
        got, msg = promote_scenario(tmp_path / kind, pairs=kind, replay_only=replay_only)
        assert got == base, kind  # Decision 2: pairs never gate promotion
        assert msg.startswith(base_msg) and "Head-to-head" in msg
        assert ("5-0" if kind == "lose" else "0-5") in msg
    assert base[0] == ("promoted" if replay_only else "refused")


def test_promote_ends_the_challengers_running_pair_experiment(env, wins):
    store, config = env
    store.create_experiment(
        role="writer",
        champion_variant=vid("w-one"),
        challenger_variant=vid("w-alt"),
        mode="replay",
    )
    e = wx.ensure_pair(
        store, pair.Opponent("w-alt", 1, "challenger", vid("w-alt")), champion=vid("w-one")
    )
    res = wx.promote(store, config, vid("w-alt"), replay_only=True)
    assert res.outcome == "promoted" and e.id not in res.experiments
    assert store.get_experiment(e.id).status == "promoted"


# =========================================================================== export


def test_export_carries_the_pair_redacted(env):
    store, _ = env
    solo = ticket(store, 9)
    rows = export.store_rows(store, solo.id)
    assert "pair" not in rows and rows["ticket"]["pair_side"] is None
    p = played(store, 1, a_cost=1.0, b_cost=2.0, b_wins=True)
    rows = export.store_rows(store, p.ticket_b)
    assert rows["ticket"]["pair_side"] == "b" and rows["pair"]["id"] == p.id
    r = export.Redactor(salt=b"0" * 32)
    out = export.ROWS.apply(r, rows)
    assert out["ticket"]["pair_side"] == "b"
    got = out["pair"]
    assert (got["ticket_a"], got["ticket_b"], got["winner_ticket_id"]) == (
        p.ticket_a,
        p.ticket_b,
        p.ticket_b,
    )
    assert (got["status"], got["loser_outcome"], got["opponent_reason"]) == (
        "decided",
        "merged_other",
        "challenger",
    )
    assert got["opponent_variant"] == vid("w-alt")
    assert got["repo"] == out["ticket"]["repo"] == "repo-1"  # pseudonymised like the ticket
    assert REPO not in json.dumps(out)
    # the worker's side-B branch is kept like side A's
    assert export.BRANCH(r, "fman/issue-7-b/a2") == "fman/issue-7-b/a2"
    assert export.BRANCH(r, "fman/issue-7/a2") == "fman/issue-7/a2"
