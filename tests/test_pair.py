"""ab:pair (todo #34, GH #16): side B's opponent, its first-attempt pin, the pair record,
and split.pick skipping paired tickets.

Runs against the real ``Store`` Pair API (migration 016), so the pin, the decision and
the cost queries read real pair, attempt and review rows.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foreman_inator import prompts
from foreman_inator.config import Config, ModelEndpoint, key_ref
from foreman_inator.db.store import Pair, PairSide, Store, Ticket
from foreman_inator.experiments import pair, split, variants
from foreman_inator.routing import escalation as esc

LOCAL_URL = "http://127.0.0.1:11434/v1"
REPO = "acme/widgets"


def models() -> tuple[ModelEndpoint, ...]:
    return (
        ModelEndpoint("w-one", 1, "local", "w-one-up", LOCAL_URL, "fam-w"),
        ModelEndpoint("w-alt", 1, "local", "w-alt-up", LOCAL_URL, "fam-w"),
        ModelEndpoint("w-two", 2, "local", "w-two-up", LOCAL_URL, "fam-w2"),
        ModelEndpoint(
            "c-one", 1, "cloud", "c-one-up", "https://c.example.invalid/v1", "fam-c", key_ref("c")
        ),
        ModelEndpoint(
            "c-three", 3, "cloud", "c3-up", "https://x.example.invalid/v1", "fam-x", key_ref("x")
        ),
    )


def make_config() -> Config:
    return Config(
        app_id=1,
        allowed_users=("brooks",),
        repos=(REPO,),
        models=models(),
        reviewer_models=("c-three",),
    )


LADDER = esc.build_ladder(models())  # w-one, w-alt, c-one (t1), w-two (t2), c-three (t3)


def rung(model: str) -> esc.Rung:
    return next(r for r in LADDER if r.model == model)


def vid(model: str) -> str:
    return variants.variant_id(model, prompts.load("writer-v1"))


def exp(model: str, eid: int = 4) -> Any:
    return SimpleNamespace(id=eid, challenger_variant=vid(model), champion_variant=vid("w-one"))


# =========================================================================== opponent


def test_challenger_when_it_fits_a_tier_else_next_rung():
    o = pair.choose_opponent(LADDER, rung("w-one"), experiment=exp("w-alt"))
    assert (o.model, o.reason, o.variant, o.experiment_id) == (
        "w-alt",
        "challenger",
        vid("w-alt"),
        4,
    )
    # a challenger on another tier does not fit: the next rung up instead
    o = pair.choose_opponent(LADDER, rung("w-one"), experiment=exp("w-two"))
    assert (o.model, o.reason, o.experiment_id) == ("w-alt", "next_rung", None)
    # 7c: a cloud challenger never takes a local start's code to the cloud
    o = pair.choose_opponent(LADDER, rung("w-one"), experiment=exp("c-one"))
    assert o.reason == "next_rung"
    # an unconfigured challenger model does not fit either
    o = pair.choose_opponent(LADDER, rung("w-two"), experiment=exp("nope"))
    assert (o.model, o.reason) == ("c-three", "next_rung")


def test_top_rung_takes_the_rung_below_and_one_rung_has_none():
    o = pair.choose_opponent(LADDER, rung("c-three"), prompt=prompts.load("writer-v1"))
    assert (o.model, o.reason, o.variant) == ("w-two", "lower_rung", vid("w-two"))
    assert pair.choose_opponent(LADDER[:1], LADDER[0]) is None
    assert pair.choose_opponent(LADDER, "w-two").model == "c-three"  # by model name
    assert pair.choose_opponent(LADDER, 0).model == "w-alt"  # by index
    assert pair.choose_opponent(LADDER, "missing") is None


def test_local_only_uses_local_rungs_only():
    # next local rung after w-alt skips the cloud c-one
    o = pair.choose_opponent(LADDER, rung("w-alt"), local_only=True)
    assert (o.model, o.reason) == ("w-two", "next_rung")
    # top local rung: the local rung below, never cloud c-three
    o = pair.choose_opponent(LADDER, rung("w-two"), local_only=True)
    assert (o.model, o.reason) == ("w-alt", "lower_rung")
    # a cloud challenger is refused for a local-only repo even on a cloud-kind start
    cloud_ladder = (esc.Rung(1, "w-one", False), esc.Rung(1, "c-one", True))
    o = pair.choose_opponent(
        cloud_ladder, cloud_ladder[0], experiment=exp("c-one"), local_only=True
    )
    assert o is None  # no second local model
    assert pair.choose_opponent(cloud_ladder, cloud_ladder[0], local_only=False).model == "c-one"


def test_create_args_match_the_pair_columns():
    o = pair.choose_opponent(LADDER, rung("w-two"))
    assert pair.create_args(o) == {
        "opponent_variant": "c-three",
        "opponent_reason": "next_rung",
        "experiment_id": None,
    }


# =========================================================================== store


@pytest.fixture
def env(tmp_path: Path):
    store = Store(tmp_path / "fman.db")
    config = make_config()
    variants.ensure_defaults(store, config)
    yield store, config
    store.close()


def ticket(store: Store, n: int, side: PairSide | None = None) -> Ticket:
    return store.create_ticket(
        repo=REPO,
        issue_number=n,
        title=f"T{n}",
        request="Do it.",
        base_branch="main",
        route_reason="classifier: small",
        routed_tier=1,
        pair_side=side,
    )


def stored_exp(store: Store, model: str) -> Any:
    """A real running writer experiment (so a pair row's experiment_id is a real one)."""
    return store.create_experiment(
        role="writer",
        champion_variant=vid("w-one"),
        challenger_variant=vid(model),
        mode="pair",
    )


def set_opponent(store: Store, pair_id: int, variant: str) -> None:
    """Rewrite a stored pair's opponent (a prompt or config change after it was opened)."""
    with store.transaction() as conn:
        conn.execute("UPDATE pairs SET opponent_variant = ? WHERE id = ?", (variant, pair_id))


FIRST = esc.Retry("first", 1, "first attempt", "w-one")


def opened(store: Store, opponent: pair.Opponent, n: int = 1) -> tuple[Ticket, Ticket, Pair]:
    a, b = ticket(store, n), ticket(store, n, "b")
    p = store.create_pair(ticket_a=a.id, ticket_b=b.id, **pair.create_args(opponent))
    return store.get_ticket(a.id), b, p


# =========================================================================== the pin


def test_pin_on_side_b_first_attempt_only(env):
    store, config = env
    a, b, p = opened(store, pair.choose_opponent(LADDER, rung("w-one")))
    pk = pair.pick(store, config, b, FIRST, config.model("w-one"), ())
    assert pk is not None and pk.endpoint.name == "w-alt" and pk.template is None
    assert (pk.pair_id, pk.opponent_reason) == (p.id, "next_rung")
    assert pair.pick(store, config, a, FIRST, config.model("w-one"), ()) is None  # side A
    # an interrupted first attempt re-runs with the pin
    cut = store.add_attempt(b.id, model="w-alt", tier=1, trigger="first")
    store.finish_attempt(cut.id, "superseded")
    cut = store.get_attempt(cut.id)
    assert pair.pick(store, config, b, FIRST, config.model("w-alt"), [cut]) is not None
    # any counted attempt: escalation decides
    done = store.add_attempt(b.id, model="w-alt", tier=1, trigger="first")
    done = store.finish_attempt(done.id, "failed_checks")
    assert pair.pick(store, config, b, FIRST, config.model("w-alt"), [cut, done]) is None
    up = esc.Escalate("escalation", 2, "up", "w-two")
    assert pair.pick(store, config, b, up, config.model("w-two"), ()) is None
    store.decide_pair(p.id, winner_ticket_id=a.id, loser_outcome="merged_other")
    assert pair.pick(store, config, b, FIRST, config.model("w-one"), ()) is None  # not running


def test_challenger_pin_runs_its_prompt_and_rechecks_7c(env):
    store, config = env
    x = stored_exp(store, "w-alt")
    _, b, p = opened(store, pair.choose_opponent(LADDER, rung("w-one"), experiment=x))
    pk = pair.pick(store, config, b, FIRST, config.model("w-one"), ())
    assert pk.endpoint.name == "w-alt" and pk.experiment_id == x.id
    assert variants.variant_id(pk.endpoint.name, pk.template) == vid("w-alt")
    # stored challenger on the cloud against a local step: refused at run time
    set_opponent(store, p.id, vid("c-one"))
    assert pair.pick(store, config, b, FIRST, config.model("w-one"), ()) is None


def test_local_only_never_pins_a_cloud_opponent(env):
    store, config = env
    _, b, _ = opened(store, pair.choose_opponent(LADDER, rung("w-two")))  # c-three
    step = esc.Retry("first", 2, "first", "w-two")
    assert pair.pick(store, config, b, step, config.model("w-two"), ()).endpoint.name == "c-three"
    assert pair.pick(store, config, b, step, config.model("w-two"), (), local_only=True) is None


def test_split_skips_paired_tickets(env, monkeypatch):
    store, config = env
    called = []
    monkeypatch.setattr(split, "active_split", lambda *a, **k: called.append(1))
    t = ticket(store, 9, "b")
    assert split.pick(store, config, t, FIRST, config.model("w-one"), ()) is None
    assert called == []  # never even looked for a split
    split.pick(store, config, ticket(store, 10), FIRST, config.model("w-one"), ())
    assert called == [1]


# =========================================================================== results


def spend(store: Store, t: Ticket, model: str, cost: float, review: float = 0.0) -> None:
    att = store.add_attempt(t.id, model=model, tier=1, trigger="first", variant_id=vid(model))
    store.add_attempt_usage(att.id, cost_usd=cost)
    store.finish_attempt(att.id, "accepted")
    store.record_review(att.id, reviewer_model="c-three", verdict="approve", cost_usd=review)
    store.record_review(
        att.id, reviewer_model="c-three", verdict="approve", cost_usd=100.0, shadow=True
    )


def test_record_validates_and_decides(env):
    store, _ = env
    a, b, p = opened(store, pair.choose_opponent(LADDER, rung("w-one")))
    with pytest.raises(ValueError, match="loser outcome"):
        pair.record(store, p, a, "lost")
    with pytest.raises(ValueError, match="not a side"):
        pair.record(store, p, 999, "merged_other")
    d = pair.record(store, p, b, "rejected")
    assert (d.status, d.winner_ticket_id, d.loser_outcome) == ("decided", b.id, "rejected")


def test_score_counts_wins_and_charges_the_losers_spend(env):
    store, _ = env
    x = stored_exp(store, "w-alt")
    opp = pair.choose_opponent(LADDER, rung("w-one"), experiment=x)
    results = []
    for n, (ca, cb, a_wins) in enumerate([(1.0, 2.0, True), (0.5, 0.25, False), (3, 1, True)]):
        a, b, p = opened(store, opp, 10 + n)
        spend(store, a, "w-one", ca, review=0.5)
        spend(store, b, "w-alt", cb)
        results.append((p, a if a_wins else b))
    for p, w in results:
        pair.record(store, p, w, "merged_other")
    opened(store, opp, 50)  # running: not scored
    dropped = opened(store, opp, 51)[2]
    store.drop_pair(dropped.id)  # dropped: not scored
    [r] = pair.score(store)
    assert (r.a, r.b, r.a_wins, r.b_wins, r.decided) == (vid("w-one"), vid("w-alt"), 2, 1, 3)
    # A's wins: (1 + .5 + 2) + (3 + .5 + 1) = 8.0 over 2 wins; B's: .5 + .5 + .25 = 1.25
    assert r.a_cost_per_win == pytest.approx(4.0)
    assert r.b_cost_per_win == pytest.approx(1.25)
    assert pair.score(store, experiment_id=x.id) == [r]
    assert pair.score(store, experiment_id=x.id + 1) == []
    assert pair.score(store, repo=REPO) == [r]
    assert pair.score(store, repo="other/repo") == []
    assert "2-1" in pair.note([r]) and "$4.0000" in pair.note([r])
    assert pair.note([]) == "pairs: none decided"
