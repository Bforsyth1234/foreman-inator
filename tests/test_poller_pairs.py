"""#34 ab:pair in the poller (GH #16, wave 23 A): pair-aware events from GitHub state."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes.github import FakeGitHub
from foreman_inator.config import Config
from foreman_inator.db.store import Store
from foreman_inator.github.poller import PairReapplied, Poller, pair_cursor
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import Action as A
from foreman_inator.tickets.lifecycle import (
    AgentLabelRemoved,
    ClosedUnmerged,
    Merged,
    PairLost,
    PairRemoved,
    PairRequested,
    TicketRequested,
)

REPO = "brooks/toy"
ME = "brooks"
STRANGER = "mallory"


class Driver:
    """Applies poller events to the store (enough of the worker for pair transitions)."""

    def __init__(self, gh: FakeGitHub, store: Store, poller: Poller) -> None:
        self.gh, self.store, self.poller = gh, store, poller

    def handle(self, ev: object) -> None:
        if isinstance(ev, PairReapplied):
            return  # no transition: the worker only comments
        if isinstance(ev, TicketRequested):
            tr = lifecycle.apply(None, ev)
            t = self.store.create_ticket(
                repo=ev.repo,
                issue_number=ev.issue_number,
                title=ev.title,
                request=ev.body,
                base_branch="main",
            )
        else:
            t = self.store.get_ticket(ev.ticket_id)  # type: ignore[attr-defined]
            tr = lifecycle.apply(t.column, ev)  # type: ignore[arg-type]
        if A.CREATE_PAIR_SIDE in tr.actions:
            b = self.store.create_ticket(
                repo=t.repo,
                issue_number=t.issue_number,
                title=t.title,
                request=t.request,
                base_branch="main",
                pair_side="b",
            )
            self.store.create_pair(
                ticket_a=t.id, ticket_b=b.id, opponent_variant="m2", opponent_reason="next_rung"
            )
        if A.RECORD_PAIR_RESULT in tr.actions:
            res = lifecycle.pair_result(ev)
            pair = self.store.pair_for_ticket(t.id)
            assert res is not None and pair is not None
            sib = self.store.get_ticket(pair.other(t.id))
            op = lifecycle.pair_store_op(
                res, sibling_id=sib.id, pair_now=pair.status, sibling_column=sib.column
            )
            if op is not None and op.kind == "decide":
                assert op.winner_ticket_id is not None and op.loser_outcome is not None
                self.store.decide_pair(
                    pair.id, winner_ticket_id=op.winner_ticket_id, loser_outcome=op.loser_outcome
                )
            elif op is not None:
                self.store.drop_pair(pair.id)
        if tr.column != t.column:
            self.store.set_column(t.id, tr.column)

    def poll(self) -> list:
        return list(self.poller.process(self.handle))

    def sides(self) -> tuple[int, int]:
        (pair,) = self.store.list_pairs(repo=REPO, issue_number=1)
        return pair.ticket_a, pair.ticket_b

    def open_pr(self, tid: int, branch: str) -> int:
        self.gh.push_branch(REPO, branch)
        pr = self.gh.create_draft_pr(REPO, head=branch, base="main", title="t", body="Closes #1")
        self.gh.mark_ready_for_review(REPO, pr.number)
        self.store.update_ticket(tid, pr_number=pr.number)
        self.store.set_column(tid, "testing")
        return pr.number

    def paired_in_testing(self) -> tuple[int, int, int, int]:
        """agent + ab:pair → both sides in testing with a ready PR each."""
        self.gh.apply_label(REPO, 1, "agent", by=ME)
        self.gh.apply_label(REPO, 1, "ab:pair", by=ME)
        self.poll()
        a, b = self.sides()
        pa = self.open_pr(a, "fman/issue-1/a1")
        pb = self.open_pr(b, "fman/issue-1-b/a1")
        assert self.poll() == []
        return a, b, pa, pb


@pytest.fixture
def env(tmp_path: Path):
    gh = FakeGitHub()
    gh.create_repo(REPO)
    gh.create_issue(REPO, "Add a --version flag", "Print the version and exit.")
    store = Store(tmp_path / "fman.db")
    cfg = Config(allowed_users=(ME,), repos=(REPO,))
    yield Driver(gh, store, Poller(gh, store, cfg))
    store.close()


def kinds(events) -> list[str]:
    return [type(e).__name__ for e in events]


# --------------------------------------------------------------------------- starting a pair


def test_agent_with_pair_label_requests_a_pair(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.gh.apply_label(REPO, 1, "ab:pair", by=ME)
    (ev,) = env.poll()
    assert isinstance(ev, TicketRequested) and ev.pair
    assert env.store.get_cursor(REPO, pair_cursor(1)) is not None
    assert [t.pair_side for t in env.store.open_tickets_for_issue(REPO, 1)] == ["a", "b"]
    assert env.poll() == []  # the labeling is handled once


def test_pair_label_by_stranger_does_not_count(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.gh.apply_label(REPO, 1, "ab:pair", by=STRANGER)
    (ev,) = env.poll()
    assert isinstance(ev, TicketRequested) and not ev.pair
    assert env.poll() == []


def test_pair_label_added_later_requests_a_pair_once(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    (a,) = env.store.open_tickets_for_issue(REPO, 1)
    env.gh.apply_label(REPO, 1, "ab:pair", by=ME)
    (ev,) = env.poll()
    assert isinstance(ev, PairRequested) and ev.ticket_id == a.id and ev.labeled_by == ME
    assert env.sides()[0] == a.id
    assert env.poll() == []


def test_pair_label_added_later_by_stranger_is_ignored(env: Driver):
    env.gh.apply_label(REPO, 1, "agent", by=ME)
    env.poll()
    env.gh.apply_label(REPO, 1, "ab:pair", by=STRANGER)
    assert env.poll() == []


# --------------------------------------------------------------------------- picking


def test_merging_one_side_loses_the_other_before_the_issue_close(env: Driver):
    a, b, pa, pb = env.paired_in_testing()
    env.gh.merge_pr(REPO, pb, by=ME)
    assert env.gh.get_issue(REPO, 1).state == "closed"  # "Closes #1" closed it
    evs = env.poll()
    assert evs == [
        Merged(REPO, 1, ticket_id=b, pr_number=pb, merged_at=evs[0].merged_at, paired=True),
        PairLost(REPO, 1, ticket_id=a, sibling_ticket_id=b, sibling_pr_number=pb, pr_number=pa),
    ]
    assert env.store.get_ticket(b).column == "done"
    assert env.store.get_ticket(a).column == "dropped"
    (pair,) = env.store.list_pairs(repo=REPO)
    assert (pair.status, pair.winner_ticket_id, pair.loser_outcome) == (
        "decided",
        b,
        "merged_other",
    )
    assert env.poll() == []


def test_pair_lost_is_derived_again_after_a_crash(env: Driver):
    a, b, pa, pa_b = env.paired_in_testing()
    env.gh.merge_pr(REPO, pa, by=ME)
    result = env.poller.poll_once()
    merged, lost = result.events
    with env.store.transaction():  # only the Merged was handled; the poll never committed
        env.handle(merged)
        env.poller.ack(merged)
    again = env.poller.poll_once().events
    assert again == [lost]
    assert isinstance(lost, PairLost) and lost.ticket_id == b and lost.sibling_ticket_id == a


def test_closing_one_pr_drops_only_that_side(env: Driver):
    a, b, pa, pb = env.paired_in_testing()
    env.gh.close_pr_as(REPO, pa, by=ME)
    assert env.poll() == [
        ClosedUnmerged(REPO, 1, ticket_id=a, pr_number=pa, what="pr", paired=True)
    ]
    assert env.store.get_ticket(a).column == "dropped"
    assert env.store.get_ticket(b).column == "testing"
    assert env.poll() == []
    env.gh.merge_pr(REPO, pb, by=ME)
    assert kinds(env.poll()) == ["Merged"]
    (pair,) = env.store.list_pairs(repo=REPO)
    assert (pair.status, pair.winner_ticket_id, pair.loser_outcome) == ("decided", b, "rejected")


def test_issue_closed_drops_both_sides(env: Driver):
    a, b, pa, pb = env.paired_in_testing()
    env.gh.close_issue_as(REPO, 1, by=ME)
    assert env.poll() == [
        ClosedUnmerged(REPO, 1, ticket_id=a, pr_number=pa, what="issue", paired=True),
        ClosedUnmerged(REPO, 1, ticket_id=b, pr_number=pb, what="issue", paired=True),
    ]
    assert env.store.list_pairs(repo=REPO)[0].status == "dropped"


def test_agent_removed_drops_both_sides(env: Driver):
    a, b, _, _ = env.paired_in_testing()
    env.gh.remove_label_as(REPO, 1, "agent", by=ME)
    assert env.poll() == [
        AgentLabelRemoved(REPO, 1, ticket_id=a, removed_by=ME, paired=True),
        AgentLabelRemoved(REPO, 1, ticket_id=b, removed_by=ME, paired=True),
    ]


# --------------------------------------------------------------------------- removing / re-adding


def test_pair_label_removed_drops_side_b(env: Driver):
    a, b, _, pb = env.paired_in_testing()
    env.gh.remove_label_as(REPO, 1, "ab:pair", by=ME)
    assert env.poll() == [PairRemoved(REPO, 1, ticket_id=b, removed_by=ME, pr_number=pb)]
    assert env.store.get_ticket(b).column == "dropped"
    assert env.store.get_ticket(a).column == "testing"
    assert env.store.list_pairs(repo=REPO)[0].status == "dropped"
    assert env.poll() == []


def test_pair_label_removed_after_side_a_was_dropped_keeps_side_b(env: Driver):
    a, b, pa, pb = env.paired_in_testing()
    env.gh.close_pr_as(REPO, pa, by=ME)
    env.poll()
    assert env.store.get_ticket(a).column == "dropped"
    env.gh.remove_label_as(REPO, 1, "ab:pair", by=ME)
    assert env.poll() == []  # B is the only side left: it carries on, not dropped
    assert env.store.get_ticket(b).column == "testing"
    env.gh.merge_pr(REPO, pb, by=ME)
    assert kinds(env.poll()) == ["Merged"]
    assert env.store.get_ticket(b).column == "done"


def test_pair_label_removed_by_stranger_is_ignored(env: Driver):
    env.paired_in_testing()
    env.gh.remove_label_as(REPO, 1, "ab:pair", by=STRANGER)
    assert env.poll() == []


def test_pair_label_readded_after_drop_is_a_noop_event_once(env: Driver):
    a, b, _, _ = env.paired_in_testing()
    env.gh.remove_label_as(REPO, 1, "ab:pair", by=ME)
    env.poll()
    env.gh.apply_label(REPO, 1, "ab:pair", by=ME)
    (ev,) = env.poll()
    assert isinstance(ev, PairReapplied) and ev.ticket_id == a and ev.labeled_by == ME
    assert len(env.store.list_pairs(repo=REPO)) == 1  # no second pair
    assert env.poll() == []
    with pytest.raises(lifecycle.IllegalTransition):  # not a lifecycle transition
        lifecycle.apply("testing", ev)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- cost


def test_idle_pair_reads_both_prs(env: Driver):
    env.paired_in_testing()
    idle = env.poller.poll_once()
    assert idle.events == []
    assert idle.requests[REPO] == 5  # list_issues + 2 x (get_pr + list_reviews)
