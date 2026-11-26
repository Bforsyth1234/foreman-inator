"""ab:pair events and transitions in the pure lifecycle (todo #34, GH #16, wave 22)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foreman_inator.db.store import Pair, Store
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import (
    FLAG_PAIR_LOST,
    AgentLabelRemoved,
    ClosedUnmerged,
    IllegalTransition,
    Merged,
    PairLost,
    PairOp,
    PairRemoved,
    PairRequested,
    PairResult,
    TicketRequested,
    Transition,
    apply,
    pair_locks_releasable,
    pair_result,
    pair_status,
    pair_store_op,
)
from foreman_inator.tickets.lifecycle import Action as A

REPO = "brooks/toy"
ME = "brooks"
OPEN_COLS = ("todo", "in_progress", "testing")
STOP = (A.RELEASE_LOCKS, A.CLEANUP_WORKTREE)


def _requested(**kw):
    return TicketRequested(REPO, 1, title="t", body="b", labeled_by=ME, label_event_id=1, **kw)


def test_ticket_requested_with_pair_opens_side_b_after_side_a():
    assert apply(None, _requested(pair=True)) == Transition(
        "todo", (A.CREATE_TICKET, A.ROUTE, A.START_ATTEMPT, A.CREATE_PAIR_SIDE)
    )
    assert apply(None, _requested(pair=True, tier_override=2)).actions == (
        A.CREATE_TICKET,
        A.APPLY_TIER_OVERRIDE,
        A.ROUTE,
        A.START_ATTEMPT,
        A.CREATE_PAIR_SIDE,
    )


def test_unpaired_ticket_requested_unchanged():
    assert apply(None, _requested()) == Transition(
        "todo", (A.CREATE_TICKET, A.ROUTE, A.START_ATTEMPT)
    )


@pytest.mark.parametrize("col", OPEN_COLS)
def test_pair_requested_late_until_side_a_done(col):
    ev = PairRequested(REPO, 1, ticket_id=1, labeled_by=ME, label_event_id=9)
    assert apply(col, ev) == Transition(col, (A.CREATE_PAIR_SIDE,))


@pytest.mark.parametrize("col", [None, "done", "dropped"])
def test_pair_requested_refused_once_side_a_done(col):
    ev = PairRequested(REPO, 1, ticket_id=1, labeled_by=ME, label_event_id=9)
    with pytest.raises(IllegalTransition):
        apply(col, ev)


def test_pair_removed_drops_side_b_and_closes_its_pr():
    ev = PairRemoved(REPO, 1, ticket_id=2, removed_by=ME, pr_number=11)
    assert apply("in_progress", ev) == Transition(
        "dropped", (A.CANCEL_ATTEMPT, A.CLOSE_OWN_PR, A.RECORD_PAIR_RESULT, *STOP)
    )
    assert apply("testing", ev).actions == (A.CLOSE_OWN_PR, A.RECORD_PAIR_RESULT, *STOP)
    no_pr = PairRemoved(REPO, 1, ticket_id=2, removed_by=ME)
    assert apply("todo", no_pr) == Transition("dropped", (A.RECORD_PAIR_RESULT, *STOP))
    assert pair_result(ev) == PairResult(2, "dropped")
    for col in (None, "done", "dropped"):
        with pytest.raises(IllegalTransition):
            apply(col, ev)


def test_pair_lost_cancels_closes_deletes_branch_and_records_the_win():
    ev = PairLost(REPO, 1, ticket_id=2, sibling_ticket_id=1, sibling_pr_number=10, pr_number=11)
    assert apply("in_progress", ev) == Transition(
        "dropped",
        (
            A.CANCEL_ATTEMPT,
            A.CLOSE_OWN_PR,
            A.DELETE_OWN_BRANCH,
            A.RECORD_PAIR_RESULT,
            *STOP,
        ),
    )
    assert apply("testing", ev).actions[0] == A.CLOSE_OWN_PR
    no_pr = PairLost(REPO, 1, ticket_id=2, sibling_ticket_id=1, sibling_pr_number=10)
    assert A.CLOSE_OWN_PR not in apply("todo", no_pr).actions
    res = pair_result(ev)
    assert res is not None
    assert (res.ticket_id, res.outcome, res.winner_ticket_id, res.loser_outcome) == (
        2,
        "merged_other",
        1,
        "merged_other",
    )
    assert res.flag_reason is not None
    assert res.flag_reason.split(":", 1)[0] == FLAG_PAIR_LOST == "pair_lost"
    assert "#10" in res.flag_reason
    with pytest.raises(IllegalTransition):
        apply("done", ev)


def test_closed_pr_of_one_side_is_rejected_and_sibling_continues():
    ev = ClosedUnmerged(REPO, 1, ticket_id=2, pr_number=11, what="pr", paired=True)
    assert apply("testing", ev) == Transition("dropped", (A.RECORD_PAIR_RESULT, *STOP))
    assert pair_result(ev) == PairResult(2, "rejected", loser_outcome="rejected")
    assert pair_status("rejected", "in_progress") == "running"  # the other continues alone
    assert pair_status("rejected", "dropped") == "dropped"  # both PRs closed


def test_issue_closed_or_agent_removed_drops_the_pair():
    closed = ClosedUnmerged(REPO, 1, ticket_id=1, pr_number=None, what="issue", paired=True)
    removed = AgentLabelRemoved(REPO, 1, ticket_id=1, removed_by=ME, paired=True)
    for ev in (closed, removed):
        assert apply("in_progress", ev) == Transition(
            "dropped", (A.CANCEL_ATTEMPT, A.RECORD_PAIR_RESULT, *STOP)
        )
        assert pair_result(ev) == PairResult(1, "dropped")
    assert pair_status("dropped", "testing") == "dropped"
    assert pair_status("dropped", "done") == "decided"


def test_paired_merge_records_the_win():
    ev = Merged(REPO, 1, ticket_id=1, pr_number=10, paired=True)
    assert apply("testing", ev) == Transition(
        "done",
        (A.RECORD_ACCEPT, A.RECORD_PAIR_RESULT, A.CLOSE_ISSUE, A.POST_SUMMARY, *STOP),
    )
    assert pair_result(ev) == PairResult(1, "won", winner_ticket_id=1)
    assert pair_status("won", "testing") == "decided"
    assert pair_status("merged_other", "done") == "decided"


@pytest.mark.parametrize(
    "event",
    [
        Merged(REPO, 1, ticket_id=1, pr_number=2),
        ClosedUnmerged(REPO, 1, ticket_id=1, pr_number=2, what="pr"),
        ClosedUnmerged(REPO, 1, ticket_id=1, pr_number=None, what="issue"),
        AgentLabelRemoved(REPO, 1, ticket_id=1, removed_by=ME),
    ],
)
def test_unpaired_events_add_no_pair_actions(event):
    pair_actions = {A.CREATE_PAIR_SIDE, A.CLOSE_OWN_PR, A.DELETE_OWN_BRANCH, A.RECORD_PAIR_RESULT}
    for col in ("in_progress", "testing"):
        assert not pair_actions & set(apply(col, event).actions)
    assert pair_result(event) is None


def test_locks_release_only_once_both_sides_terminal():
    assert pair_locks_releasable(None)  # unpaired
    assert pair_locks_releasable("done")
    assert pair_locks_releasable("dropped")
    for col in OPEN_COLS:
        assert not pair_locks_releasable(col)


def test_new_names_exported():
    for name in (
        "PairRequested",
        "PairRemoved",
        "PairLost",
        "PairResult",
        "PairOutcome",
        "PairStatus",
        "FLAG_PAIR_LOST",
        "pair_result",
        "pair_status",
        "pair_store_op",
        "PairOp",
        "pair_locks_releasable",
    ):
        assert name in lifecycle.__all__
        assert hasattr(lifecycle, name)
    for action in ("CREATE_PAIR_SIDE", "CLOSE_OWN_PR", "DELETE_OWN_BRANCH", "RECORD_PAIR_RESULT"):
        assert action in A.__members__


# ------------------------------------------------- RECORD_PAIR_RESULT on the real store


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


def _pair(store: Store, n: int) -> Pair:
    kw = {"repo": REPO, "issue_number": n, "title": "t", "request": "r", "base_branch": "main"}
    a = store.create_ticket(**kw)
    b = store.create_ticket(**kw, pair_side="b")
    return store.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant="m2", opponent_reason="next_rung"
    )


def _record(store: Store, pair: Pair, event: object, sibling_column: str | None) -> Pair:
    """What the RECORD_PAIR_RESULT handler will do (wave 23), on the real store."""
    res = pair_result(event)
    assert res is not None
    now = store.get_pair(pair.id)
    op = pair_store_op(
        res, sibling_id=now.other(res.ticket_id), pair_now=now.status, sibling_column=sibling_column
    )
    if op is not None and op.kind == "decide":
        store.decide_pair(
            pair.id, winner_ticket_id=op.winner_ticket_id, loser_outcome=op.loser_outcome
        )
    elif op is not None:
        store.drop_pair(pair.id)
    return store.get_pair(pair.id)


def _closed(tid: int) -> ClosedUnmerged:
    return ClosedUnmerged(REPO, 1, ticket_id=tid, pr_number=11, what="pr", paired=True)


def test_merge_then_pair_lost_decides_once(store):
    p = _pair(store, 1)
    merged = Merged(REPO, 1, ticket_id=p.ticket_a, pr_number=10, paired=True)
    won = _record(store, p, merged, "testing")
    assert (won.status, won.winner_ticket_id, won.loser_outcome) == (
        "decided",
        p.ticket_a,
        "merged_other",
    )
    lost = PairLost(
        REPO, 1, ticket_id=p.ticket_b, sibling_ticket_id=p.ticket_a, sibling_pr_number=10
    )
    assert _record(store, p, lost, "done") == won  # re-sent decision: a store no-op


def test_rejected_side_leaves_the_pair_running_until_the_survivor_merges(store):
    p = _pair(store, 1)
    assert _record(store, p, _closed(p.ticket_b), "in_progress").status == "running"
    merged = Merged(REPO, 1, ticket_id=p.ticket_a, pr_number=10, paired=True)
    d = _record(store, p, merged, "dropped")
    assert (d.status, d.winner_ticket_id, d.loser_outcome) == ("decided", p.ticket_a, "rejected")


def test_both_prs_closed_or_label_removed_drop_the_pair(store):
    p = _pair(store, 1)
    _record(store, p, _closed(p.ticket_a), "testing")
    assert _record(store, p, _closed(p.ticket_b), "dropped").status == "dropped"
    q = _pair(store, 2)
    removed = PairRemoved(REPO, 2, ticket_id=q.ticket_b, removed_by=ME)
    assert _record(store, q, removed, "in_progress").status == "dropped"
    # a late merge of side A after the pair was dropped stores nothing
    late = Merged(REPO, 2, ticket_id=q.ticket_a, pr_number=10, paired=True)
    assert (
        pair_store_op(
            pair_result(late), sibling_id=q.ticket_b, pair_now="dropped", sibling_column="dropped"
        )
        is None
    )


def test_store_op_shapes():
    res = PairResult(1, "won", winner_ticket_id=1)
    assert pair_store_op(res, sibling_id=2, pair_now="running", sibling_column="testing") == PairOp(
        "decide", 1, "merged_other"
    )
    gone = PairResult(1, "dropped")
    assert pair_store_op(gone, sibling_id=2, pair_now="running", sibling_column="done") == PairOp(
        "decide", 2, "merged_other"
    )
    assert pair_store_op(gone, sibling_id=2, pair_now="decided", sibling_column="done") is None
