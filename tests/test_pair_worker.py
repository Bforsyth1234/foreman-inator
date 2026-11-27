"""ab:pair (#16) in the worker: side B copies side A's route (``worker_route``) and its
first attempt runs the pinned opponent on its own ``fman/issue-<n>-b/a<k>`` branch
(``worker_attempt``)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from fakes.worker_env import REPO, Env, failing, passing
from foreman_inator.experiments import pair as pair_mod
from foreman_inator.routing import explore
from foreman_inator.worker_queue import Job
from foreman_inator.worker_route import PAIR_ROUTE_PREFIX, start_cursor


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()


def side_b(e: Env, a_id: int, opponent: str) -> int:
    a = e.store.get_ticket(a_id)
    b = e.store.create_ticket(
        repo=REPO,
        issue_number=a.issue_number,
        title=a.title,
        request=a.request,
        base_branch=a.base_branch,
        pair_side="b",
    )
    e.store.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant=opponent, opponent_reason="next_rung"
    )
    return b.id


def unrouted_a(e: Env) -> int:
    n = e.gh.create_issue(REPO, "Add a feature", "Please add feature().", author="brooks")
    t = e.store.create_ticket(
        repo=REPO, issue_number=n, title="Add a feature", request="Please add feature().",
        base_branch="main",
    )  # fmt: skip
    return t.id


def run_route(e: Env, ticket_id: int) -> None:
    e.worker.route_job(Job(REPO, ticket_id, "route"), threading.Event())


def test_side_b_copies_a_route_and_runs_its_opponent_on_its_own_branch(env: Env):
    e = env
    n = e.label()
    e.once(failing())  # side A: routed, first attempt on tier 1 (fails, stays in progress)
    a = e.store.tickets_for_issue(REPO, n)[0]
    assert a.routed_tier == 1
    a_att = e.store.list_attempts(a.id)
    assert a_att[0].branch == f"fman/issue-{n}/a1"
    opponent = e.config.models_for_tier(2)[0].name
    b_id = side_b(e, a.id, opponent)
    calls = e.route_calls
    e.oc.set_script(passing())
    run_route(e, b_id)
    b = e.store.get_ticket(b_id)
    assert e.route_calls == calls  # no second router call
    assert b.routed_tier == a.routed_tier
    assert b.route_reason == PAIR_ROUTE_PREFIX + (a.route_reason or "")
    assert e.store.get_cursor(REPO, start_cursor(b_id)) == e.store.get_cursor(
        REPO, start_cursor(a.id)
    )
    b_att = e.store.list_attempts(b_id)
    assert len(b_att) == 1
    assert b_att[0].model == opponent  # pinned opponent, not escalation's tier 1 pick
    assert b_att[0].branch == f"fman/issue-{n}-b/a1"
    assert f"issue-{n}-b-a1" in b_att[0].worktree
    # side A's branch and attempts are untouched
    assert [x.id for x in e.store.list_attempts(a.id)] == [x.id for x in a_att]


def test_side_b_waits_until_side_a_is_routed(env: Env):
    e = env
    a_id = unrouted_a(e)
    b_id = side_b(e, a_id, e.config.models_for_tier(2)[0].name)
    run_route(e, b_id)
    b = e.store.get_ticket(b_id)
    assert b.routed_tier is None
    assert e.store.list_attempts(b_id) == []
    assert e.route_calls == 0 and e.clarify_calls == 0
    assert not e.worker.clarify_due(b) and not e.worker.clarify_parked(b)
    # A routes (any path): B copies it on its next route job, with A's explore decision
    e.store.update_ticket(a_id, routed_tier=1, route_reason="rules: small")
    e.store.set_cursor(REPO, "1", start_cursor(a_id))
    e.store.set_cursor(REPO, explore.encode(False), explore.explore_cursor(a_id))
    b = e.worker._route_ticket(e.store.get_ticket(b_id))
    assert (b.routed_tier, b.route_reason) == (1, "pair: rules: small")
    assert e.store.get_cursor(REPO, explore.explore_cursor(b_id)) == "0"
    assert e.route_calls == 0


def test_side_b_never_proposes_a_split(env: Env):
    e = env
    a_id = unrouted_a(e)
    e.store.update_ticket(a_id, routed_tier=3, route_reason="knn: tier 3 (large)")
    b_id = side_b(e, a_id, e.config.models_for_tier(2)[0].name)
    b = e.worker._route_ticket(e.store.get_ticket(b_id))
    assert b.routed_tier == 3 and b.flag_reason is None
    assert b.route_reason == "pair: knn: tier 3 (large)"
    assert e.issue_comments(e.store.get_ticket(a_id).issue_number) == []


def test_split_pick_asks_the_pair_first_and_skips_the_split_for_paired_tickets(
    env: Env, monkeypatch
):
    e = env
    a_id = unrouted_a(e)
    e.store.update_ticket(a_id, routed_tier=1, route_reason="rules: small")
    b_id = side_b(e, a_id, e.config.models_for_tier(2)[0].name)
    seen: list[bool] = []

    def fake_pick(store, config, ticket, step, endpoint, counted, *, local_only=False):
        seen.append(local_only)
        return "pinned" if ticket.pair_side == "b" else None

    def no_split(*a, **k):
        raise AssertionError("paired tickets never reach the split")

    monkeypatch.setattr(pair_mod, "pick", fake_pick)
    monkeypatch.setattr("foreman_inator.experiments.split.pick", no_split)
    ep = e.config.models_for_tier(1)[0]
    step = e.worker.policy_for(e.store.get_ticket(a_id))
    assert e.worker._split_pick(e.store.get_ticket(b_id), step, ep) == "pinned"
    assert e.worker._split_pick(e.store.get_ticket(a_id), step, ep) is None
    e.store.set_cloud(REPO, False)  # #49: a local-only repo asks for local opponents only
    e.worker._split_pick(e.store.get_ticket(b_id), step, ep)
    assert seen == [False, False, True]
