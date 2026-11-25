"""Writer traffic split (todo #34, GH #16): assignment, judging, promotion, the worker.

``ab:pair`` is not built (it needs a data-model rework), so nothing here covers it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fakes.opencode import Script, edit, say
from fakes.worker_env import Env, failing, passing
from foreman_inator import prompts
from foreman_inator import worker_experiments as wx
from foreman_inator.config import Config, ExperimentSettings, ModelEndpoint, key_ref
from foreman_inator.db.store import Store
from foreman_inator.experiments import replay_runner as rr
from foreman_inator.experiments import split, variants
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
            "rev-x", 3, "cloud", "rev-x-up", "https://x.example.invalid/v1", "fam-x", key_ref("x")
        ),
    )


def make_config(**kw: Any) -> Config:
    kw.setdefault("experiments", ExperimentSettings(min_cases=2, live_min_sample=2))
    return Config(
        app_id=1,
        allowed_users=("brooks",),
        repos=(REPO,),
        models=models(),
        reviewer_models=("rev-x",),
        **kw,
    )


def vid(model: str) -> str:
    return variants.variant_id(model, prompts.load("writer-v1"))


@pytest.fixture
def wins(monkeypatch):
    """Every replay compares as finished and won (what ``fman experiment run`` stores)."""
    state = {"verdict": "wins"}

    def fake_compare(store: Store, experiment_id: int, *, min_cases: int = 10) -> Any:
        return SimpleNamespace(
            state="finished", verdict=state["verdict"], reason="2 of 2", wins=2, losses=0, ties=0
        )

    monkeypatch.setattr(rr, "compare", fake_compare)
    return state


@pytest.fixture
def env(tmp_path: Path):
    store = Store(tmp_path / "fman.db")
    config = make_config()
    variants.ensure_defaults(store, config)
    yield store, config
    store.close()


def replay_of(store: Store, challenger: str) -> None:
    store.create_experiment(
        role="writer",
        champion_variant=variants.champion(store, "writer").id,
        challenger_variant=challenger,
        mode="replay",
    )


def start(store: Store, config: Config, model: str, share: float = 0.5) -> Any:
    replay_of(store, vid(model))
    return wx.start_split(store, config, vid(model), share=share)


def ticket(store: Store, n: int, route_reason: str = "classifier: small") -> Any:
    return store.create_ticket(
        repo=REPO,
        issue_number=n,
        title=f"T{n}",
        request="Do it.",
        base_branch="main",
        route_reason=route_reason,
        routed_tier=1,
    )


FIRST = esc.Retry("first", 1, "first attempt", "w-one")


def ep(config: Config, name: str = "w-one") -> ModelEndpoint:
    return config.model(name)


def ticket_on(store: Store, exp: Any, side: str, start: int = 1, **kw: Any) -> Any:
    """A new ticket whose stable draw puts it on ``side``."""
    n = start
    while True:
        t = ticket(store, n, **kw)
        if split.side_for(exp, t.id) == side:
            return t
        n += 1


# =========================================================================== the draw


def test_the_draw_is_stable_and_about_the_share():
    exp = SimpleNamespace(id=7, traffic_share=0.15)
    draws = [split.side_for(exp, t) for t in range(1, 4001)]
    assert draws == [split.side_for(exp, t) for t in range(1, 4001)]
    share = draws.count("challenger") / len(draws)
    assert 0.13 < share < 0.17
    other = [split.side_for(SimpleNamespace(id=8, traffic_share=0.15), t) for t in range(1, 4001)]
    assert other != draws  # keyed by experiment too


# =========================================================================== pick


def test_a_first_attempt_on_the_challenger_side_runs_the_challenger(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    t = ticket_on(store, exp, "challenger")
    p = split.pick(store, config, t, FIRST, ep(config), (), min_cases=2)
    assert p is not None and p.side == "challenger" and p.endpoint.name == "w-alt"
    assert variants.variant_id(p.endpoint.name, p.template) == vid("w-alt")
    assert split.stored_side(store, t, exp.id) == "challenger"
    c = ticket_on(store, exp, "champion", start=100)
    p = split.pick(store, config, c, FIRST, ep(config), (), min_cases=2)
    assert p is not None and p.side == "champion" and p.template is None
    assert p.endpoint.name == "w-one"
    assert split.assignments(store, exp.id) == {t.id: "challenger", c.id: "champion"}


def test_never_retries_escalations_explore_or_tier_labels(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    t = ticket_on(store, exp, "challenger")
    a = store.add_attempt(t.id, model="w-one", tier=1, trigger="first")
    store.finish_attempt(a.id, "failed_checks")
    retry = esc.Retry("first", 1, "same-tier retry", "w-one")
    assert split.pick(store, config, t, retry, ep(config), [a], min_cases=2) is None
    up = esc.Escalate("escalation", 1, "up", "w-one")
    o = ticket_on(store, exp, "challenger", start=50)
    assert split.pick(store, config, o, up, ep(config), (), min_cases=2) is None
    explore = esc.Retry("explore", 1, "explore", "w-one")
    assert split.pick(store, config, o, explore, ep(config), (), min_cases=2) is None
    why = "override: tier:1 label by brooks; router not asked"
    label = ticket_on(store, exp, "challenger", start=200, route_reason=why)
    assert split.pick(store, config, label, FIRST, ep(config), (), min_cases=2) is None
    assert split.stored_side(store, label, exp.id) is None  # not in the experiment at all
    # a ticket the champion would not run (another tier's model) is not split either
    two = esc.Retry("first", 2, "first", "w-two")
    assert split.pick(store, config, o, two, ep(config, "w-two"), (), min_cases=2) is None


def test_a_retry_after_a_challenger_attempt_goes_back_to_the_champion(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    t = ticket_on(store, exp, "challenger")
    assert split.pick(store, config, t, FIRST, ep(config), (), min_cases=2).side == "challenger"
    a = store.add_attempt(t.id, model="w-alt", tier=1, trigger="first", variant_id=vid("w-alt"))
    store.finish_attempt(a.id, "failed_checks")
    retry = esc.Retry("first", 1, "same-tier retry", "w-alt")  # the ladder's w-alt rung
    p = split.pick(store, config, t, retry, ep(config, "w-alt"), [a], min_cases=2)
    assert p is not None and (p.side, p.endpoint.name, p.template) == ("champion", "w-one", None)
    kick = esc.Retry("kickback", 1, "user kickback", "w-alt")
    assert split.pick(store, config, t, kick, ep(config, "w-alt"), [a], min_cases=2).side == (
        "champion"
    )
    up = esc.Escalate("escalation", 2, "up", "w-two")
    assert split.pick(store, config, t, up, ep(config, "w-two"), [a], min_cases=2) is None


def run_ladder(store: Store, config: Config, t: Any, outcome_of: Any, cap: int) -> list[Any]:
    """Drive ``next_step`` + ``pick`` (as the worker does) over failing attempts until the
    ticket is handed to the user or ``cap`` attempts ran. Returns the attempts."""
    ladder = esc.build_ladder(config.models)
    exp = split.active_split(store, min_cases=2)
    ch = split.load_challenger(config, exp.challenger_variant)
    for i in range(cap + 1):
        done = store.list_attempts(t.id)
        h = esc.History(tuple(done), routed_tier=t.routed_tier)
        step = esc.next_step(h, ladder)
        if isinstance(step, esc.HandToUser) or len(done) >= cap:
            break
        endpoint = config.model(step.model)
        p = split.pick(store, config, t, step, endpoint, done, min_cases=2)
        if p is not None:
            endpoint = p.endpoint
        challenger = p is not None and p.side == "challenger"
        variant = ch.variant_id if challenger else vid(endpoint.name)
        a = store.add_attempt(
            t.id, model=endpoint.name, tier=endpoint.tier, trigger=step.trigger, variant_id=variant
        )
        store.finish_attempt(a.id, outcome_of(i))
    return store.list_attempts(t.id)


def opus_fable_env(tmp_path: Path) -> tuple[Store, Config]:
    cloud = "https://c.example.invalid/v1"
    config = Config(
        app_id=1,
        allowed_users=("brooks",),
        repos=(REPO,),
        models=(
            ModelEndpoint("w-one", 1, "local", "w-one-up", LOCAL_URL, "fam-w"),
            ModelEndpoint("w-two", 2, "local", "w-two-up", LOCAL_URL, "fam-w2"),
            ModelEndpoint("opus", 3, "cloud", "opus-up", cloud, "fam-c", key_ref("c")),
            ModelEndpoint("fable", 3, "cloud", "fable-up", cloud, "fam-c", key_ref("c")),
            ModelEndpoint("rev-x", 3, "cloud", "rev-x-up", cloud, "fam-x", key_ref("x")),
        ),
        reviewer_models=("rev-x",),
        experiments=ExperimentSettings(min_cases=2, live_min_sample=2),
    )
    store = Store(tmp_path / "fman.db")
    variants.ensure_defaults(store, config)
    variants.set_champion(store, "writer", vid("opus"))
    return store, config


FAILS = {
    "failed checks": lambda i: "failed_checks",
    "with kickbacks": lambda i: "kicked_back" if i % 3 == 1 else "failed_checks",
    "kickbacks only": lambda i: "kicked_back",
}


@pytest.mark.parametrize("fails", FAILS.values(), ids=FAILS.keys())
def test_split_retries_stay_bounded_when_the_challenger_rung_is_above_the_champion(
    tmp_path: Path, wins, fails
):
    """7e: tier 3 Opus then Fable, champion Opus, challenger Fable. Only the retry right
    after Fable's first attempt goes back to Opus; the ladder then reaches Fable's rung by
    escalation and its retries stay there, so the ticket reaches the user."""
    store, config = opus_fable_env(tmp_path)
    try:
        exp = start(store, config, "fable")
        t = ticket_on(store, exp, "challenger")
        store.update_ticket(t.id, routed_tier=3)
        t = store.get_ticket(t.id)
        bound = esc.attempt_bound(len(esc.build_ladder(config.models)))
        done = run_ladder(store, config, t, fails, bound + 5)
        assert len(done) <= bound
        assert (done[0].model, done[0].variant_id) == ("fable", vid("fable"))
        assert done[1].model == "opus"  # the retry right after it: the champion
        last = esc.next_step(
            esc.History(tuple(done), routed_tier=3), esc.build_ladder(config.models)
        )
        assert isinstance(last, esc.HandToUser)
        assert any(a.model == "fable" and a.trigger == "escalation" for a in done)
    finally:
        store.close()


@pytest.mark.parametrize("fails", FAILS.values(), ids=FAILS.keys())
def test_split_retries_stay_bounded_on_two_local_rungs_of_one_tier(env, wins, fails):
    store, config = env
    exp = start(store, config, "w-alt")
    t = ticket_on(store, exp, "challenger")
    ladder = esc.build_ladder(config.models)
    bound = esc.attempt_bound(len(ladder))
    done = run_ladder(store, config, t, fails, bound + 5)
    assert len(done) <= bound
    assert [a.model for a in done[:2]] == ["w-alt", "w-one"]
    assert isinstance(
        esc.next_step(esc.History(tuple(done), routed_tier=1), ladder), esc.HandToUser
    )


def test_no_traffic_unless_the_challenger_won_its_replay_against_this_champion(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    t = ticket_on(store, exp, "challenger")
    wins["verdict"] = "inconclusive"
    assert split.pick(store, config, t, FIRST, ep(config), (), min_cases=2) is None
    wins["verdict"] = "wins"
    assert split.pick(store, config, t, FIRST, ep(config), (), min_cases=2) is not None
    variants.set_champion(store, "writer", vid("w-two"), note="test")
    assert split.active_split(store, min_cases=2) is None  # compares against the old one


def test_start_refuses_without_a_replay_win_another_tier_or_cloud(env, wins):
    store, config = env
    with pytest.raises(wx.ExperimentError, match="has not won"):
        wx.start_split(store, config, vid("w-alt"))  # no replay at all
    replay_of(store, vid("w-alt"))
    wins["verdict"] = "does_not_win"
    with pytest.raises(wx.ExperimentError, match="has not won"):
        wx.start_split(store, config, vid("w-alt"))
    wins["verdict"] = "wins"
    for model, why in (("w-two", "same tier"), ("c-one", "never sends local")):
        replay_of(store, vid(model))
        with pytest.raises(wx.ExperimentError, match=why):
            wx.start_split(store, config, vid(model))
    with pytest.raises(wx.ExperimentError, match="traffic share"):
        wx.start_split(store, config, vid("w-alt"), share=0.9)
    exp = wx.start_split(store, config, vid("w-alt"))
    assert (exp.mode, exp.traffic_share) == ("split", 0.15)
    assert wx.start_split(store, config, vid("w-alt")).id == exp.id
    assert [e.id for e in split.running_splits(store)] == [exp.id]
    stopped = wx.stop_split(store, exp.id)
    assert stopped.status == "rejected" and "stopped by" in stopped.result_note


# =========================================================================== judging


def decide(store: Store, exp: Any, side: str, outcomes: list[str], cost: float,
           start: int) -> int:  # fmt: skip
    model = "w-alt" if side == "challenger" else "w-one"
    n = start
    for outcome in outcomes:
        t = ticket_on(store, exp, side, start=n)
        n = t.issue_number + 1
        store.set_cursor(REPO, side, split.cursor_name(exp.id, t.id))
        a = store.add_attempt(t.id, model=model, tier=1, trigger="first", variant_id=vid(model))
        store.update_attempt(a.id, cost_usd=cost)
        store.finish_attempt(a.id, outcome)
        if outcome == "failed_checks":  # the champion's retry is on neither side
            b = store.add_attempt(t.id, model="w-one", tier=1, trigger="first",
                                  variant_id=vid("w-one"))  # fmt: skip
            store.update_attempt(b.id, cost_usd=50.0)
            store.finish_attempt(b.id, "accepted")
    return n


def test_judge_holds_fails_and_waits_for_a_sample(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    n = decide(store, exp, "champion", ["accepted", "failed_checks"], 1.0, 1)
    c = split.score(store, exp.id)
    assert (c.champion.decided, c.champion.accepted, c.champion.cost_usd) == (2, 1, 2.0)
    assert split.judge(c, min_sample=2).status == "insufficient"  # no challenger yet
    n = decide(store, exp, "challenger", ["accepted", "kicked_back"], 0.5, n)
    c = split.score(store, exp.id)
    assert c.challenger.accept_rate == 0.5 and c.challenger.cost_per_accepted == 1.0
    assert split.judge(c, min_sample=2).status == "holds"
    assert split.judge(c, min_sample=3).status == "insufficient"
    decide(store, exp, "challenger", ["failed_checks"], 0.5, n)
    j = split.judge(split.score(store, exp.id), min_sample=2)
    assert j.status == "fails" and "accept rate 33% is below the champion's 50%" in j.reasons[0]


def test_judge_fails_a_dearer_challenger(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    n = decide(store, exp, "champion", ["accepted", "accepted"], 1.0, 1)
    decide(store, exp, "challenger", ["accepted", "accepted"], 3.0, n)
    j = split.judge(split.score(store, exp.id), min_sample=2)
    assert j.status == "fails" and "cost per accepted ticket $3.0000" in j.reasons[0]


# =========================================================================== promote


def test_promote_a_writer_that_holds_live_in_its_split(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    n = decide(store, exp, "champion", ["accepted", "failed_checks"], 1.0, 1)
    res = wx.promote(store, config, vid("w-alt"))
    assert res.outcome == "refused" and "--replay-only" in res.message and "split #" in res.message
    decide(store, exp, "challenger", ["accepted", "accepted"], 0.5, n)
    res = wx.promote(store, config, str(exp.id))
    assert res.outcome == "promoted", res.message
    assert variants.champion(store, "writer").id == vid("w-alt")
    assert f"holds live in split #{exp.id}" in variants.champion_history(store, "writer")[-1].note
    assert store.get_experiment(exp.id).status == "promoted"
    assert "live: holds" in store.get_experiment(exp.id).result_note


def test_promote_rejects_a_writer_that_fails_live(env, wins):
    store, config = env
    champion = variants.champion(store, "writer").id
    exp = start(store, config, "w-alt")
    n = decide(store, exp, "champion", ["accepted", "accepted"], 1.0, 1)
    decide(store, exp, "challenger", ["failed_checks", "accepted"], 1.0, n)
    res = wx.promote(store, config, vid("w-alt"), replay_only=True)
    assert res.outcome == "rejected"
    assert variants.champion(store, "writer").id == champion
    assert {store.get_experiment(i).status for i in res.experiments} == {"rejected"}


def test_status_shows_the_split(env, wins):
    store, config = env
    exp = start(store, config, "w-alt")
    text = "\n".join(wx.status_lines(store, config))
    assert f"#{exp.id:<4} writer   split" in text and "live: insufficient" in text
    lines = wx.split_lines(store, config)
    assert lines[0] == f"Split #{exp.id}: running, 50% of first attempts, gets traffic"


# =========================================================================== the worker


def worker_env(tmp: Path) -> Env:
    e = Env(tmp, models=models(), reviewer_models=("rev-x",),
            experiments=ExperimentSettings(min_cases=2, live_min_sample=2))  # fmt: skip
    variants.ensure_defaults(e.store, e.config)
    return e


def test_the_worker_runs_the_challenger_on_its_share_and_retries_on_the_champion(
    tmp_path: Path, wins
):
    e = worker_env(tmp_path)
    try:
        exp = start(e.store, e.config, "w-alt", share=0.5)
        # the next ticket ids, in order, are on these sides
        sides = {i: split.side_for(exp, i) for i in range(1, 30)}
        chall = next(i for i, s in sides.items() if s == "challenger")
        for i in range(1, chall):  # tickets before it: champion side
            n = e.label(f"F{i}", "Add it.")
            e.once(passing())
            assert e.attempts(n)[0].model == "w-one"
        n = e.label("Challenged", "Add it.")
        e.once(failing())
        assert e.ticket(n).id == chall
        first = e.attempts(n)[0]
        assert (first.model, first.variant_id, first.trigger) == ("w-alt", vid("w-alt"), "first")
        assert e.driver.endpoints[-1] == "w-alt"
        e.once(passing())  # the same-tier retry goes back to the champion
        second = e.attempts(n)[1]
        assert (second.model, second.variant_id) == ("w-one", vid("w-one"))
        assert split.assignments(e.store, exp.id)[chall] == "challenger"
    finally:
        e.close()


def test_a_tier_label_ticket_is_never_split(tmp_path: Path, wins):
    e = worker_env(tmp_path)
    try:
        exp = start(e.store, e.config, "w-alt", share=0.5)
        chall = next(i for i in range(1, 30) if split.side_for(exp, i) == "challenger")
        for i in range(1, chall + 1):
            n = e.label(f"F{i}", "Add it.", "tier:1")
            e.once(Script([edit("status.txt", "pass\n"), edit("f.py", f"x = {i}\n"), say("Ok")]))
            assert e.attempts(n)[0].model == "w-one"
        assert chall not in split.assignments(e.store, exp.id)
    finally:
        e.close()
