"""ab:pair (#16) end to end: the fake GitHub and fake models through the real Worker.

The main scenario: an allowed user labels an issue ``agent`` + ``ab:pair`` → two tickets,
two attempts on different models, two PRs → the user merges one → the other PR is closed,
its ``fman/`` branch deleted, the pair decided, and ``fman status`` shows won / lost. Then
7b (a stranger's ``ab:pair`` does nothing), Decision 4 (closing one PR alone loses that
side; the other continues) and Decision 3 (removing ``ab:pair`` drops side B).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes.worker_env import REPO, USER, Env, passing
from foreman_inator.github.poller import PAIR_LABEL
from foreman_inator.pr.publisher import pair_line, pr_head_branch
from foreman_inator.worker_status import render_status


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()


def label_pair(e: Env, *, by: str = USER) -> int:
    n = e.gh.create_issue(REPO, "Add a feature", "Please add feature().", author=USER)
    e.gh.apply_label(REPO, n, PAIR_LABEL, by=by)
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


def both_prs(e: Env) -> tuple[int, object, object]:
    n = label_pair(e)
    run(e)
    a, b = sides(e, n)
    assert a.pr_number is not None and b.pr_number is not None
    return n, a, b


def test_label_two_prs_merge_one_closes_the_other(env: Env):
    e = env
    n, a, b = both_prs(e)
    att_a, att_b = e.store.list_attempts(a.id), e.store.list_attempts(b.id)
    assert att_a[0].model != att_b[0].model  # two different models
    assert att_a[0].branch == f"fman/issue-{n}/a1"
    assert att_b[0].branch == f"fman/issue-{n}-b/a1"
    assert a.pr_number != b.pr_number
    # each PR names the other (A's body was updated when B's PR opened)
    assert pair_line(b.pr_number) in e.gh._pr(REPO, a.pr_number).body
    assert pair_line(a.pr_number) in e.gh._pr(REPO, b.pr_number).body
    assert any("Two PRs" in c for c in e.issue_comments(n))

    e.gh.merge_pr(REPO, a.pr_number)
    run(e, 2)
    a, b = sides(e, n)
    assert a.column == "done" and b.column == "dropped"
    assert b.flag_reason and b.flag_reason.startswith("pair_lost")
    assert (REPO, b.pr_number) in e.gh.closed_prs
    assert (REPO, pr_head_branch(b)) in e.gh.deleted_branches
    assert (REPO, pr_head_branch(a)) not in e.gh.deleted_branches
    pair = e.store.pair_for_ticket(a.id)
    assert pair is not None and pair.status == "decided"
    assert (pair.winner_ticket_id, pair.loser_outcome) == (a.id, "merged_other")
    status = render_status(e.store)
    assert ", won" in status and ", lost" in status


def test_a_strangers_ab_pair_label_does_nothing(env: Env):
    e = env
    n = label_pair(e, by="mallory")  # 7b: not an allowed user
    run(e)
    ts = e.store.tickets_for_issue(REPO, n)
    assert len(ts) == 1 and ts[0].pair_side is None
    assert e.store.list_pairs() == []


def test_closing_one_pr_alone_loses_that_side_and_the_other_continues(env: Env):
    e = env
    n, a, b = both_prs(e)
    e.gh.close_pr_as(REPO, b.pr_number)
    run(e, 2)
    a, b = sides(e, n)
    assert b.column == "dropped" and a.column in ("testing", "in_progress")
    pair = e.store.pair_for_ticket(a.id)
    assert pair is not None and pair.status == "running"  # decided when A is merged
    e.gh.merge_pr(REPO, a.pr_number)
    run(e, 2)
    pair = e.store.get_pair(pair.id)
    assert e.store.get_ticket(a.id).column == "done"
    assert (pair.status, pair.winner_ticket_id, pair.loser_outcome) == (
        "decided",
        a.id,
        "rejected",
    )


def test_removing_ab_pair_drops_side_b(env: Env):
    e = env
    n, a, b = both_prs(e)
    e.gh.remove_label_as(REPO, n, PAIR_LABEL, by=USER)
    run(e, 2)
    a, b = sides(e, n)
    assert b.column == "dropped" and a.column not in ("dropped", "done")
    assert (REPO, b.pr_number) in e.gh.closed_prs
    pair = e.store.pair_for_ticket(a.id)
    assert pair is not None and pair.status == "dropped"
