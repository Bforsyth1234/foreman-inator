"""#82 (GH #80): a restart or cancel mid-attempt no longer throws away a spend approval:
the approval's budget is carried to the ticket's next attempt.

Fake clock + a real store on a temp file; "restart" = a new ``Spend`` over the same store
(nothing in memory survives) after the attempt is marked ``superseded``, as the worker's
startup recovery does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foreman_inator.config import Config, ModelEndpoint, SpendCaps
from foreman_inator.db.store import Store
from foreman_inator.proxy.server import CallContext
from foreman_inator.spend import FLAG_THRIFTY, Spend

CLOUD = ModelEndpoint("claude", 3, "cloud", "claude-x", "https://api.anthropic.com/v1", "claude")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, seconds: float = 1.0) -> None:
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 25, 18, 37, tzinfo=UTC))


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    s = Store(tmp_path / "fman.db", clock=clock)
    yield s
    s.close()


def make_spend(store: Store, daily: float = 20.0, monthly: float = 100.0) -> Spend:
    caps = SpendCaps(daily_usd=daily, monthly_usd=monthly, experiments_monthly_usd=10.0)
    return Spend(store, Config(spend_caps=caps), tz="America/New_York", approval_usd=5.0)


def ticket(store: Store) -> int:
    return store.create_ticket(
        repo="me/toy", issue_number=11, title="t", request="r", base_branch="main"
    ).id


def ctx(attempt_id: int) -> CallContext:
    return CallContext(kind="attempt", attempt_id=attempt_id, bucket="live")


def call(spend: Spend, cost: float, attempt_id: int) -> bool:
    ok = spend.hook(CLOUD, cost, ctx(attempt_id), phase="estimate")
    if ok:
        spend.hook(CLOUD, cost, ctx(attempt_id), phase="actual")
    return bool(ok)


def start_thrifty(store: Store, spend: Spend, clock: Clock, tid: int) -> int:
    """Pass the thrifty gate (which uses the open approval), start the attempt."""
    clock.tick()
    assert spend.writer_gate(tid, approval_required=True) is None
    assert store.open_spend_approval(tid) is None  # used when the attempt starts
    clock.tick()
    return store.add_attempt(tid, model="claude", tier=3, trigger="first").id


def thrifty_attempt(store: Store, spend: Spend, clock: Clock, tid: int) -> int:
    """Asked, approved, started."""
    assert spend.writer_gate(tid, approval_required=True).cap == "thrifty"
    spend.approve(tid)
    return start_thrifty(store, spend, clock, tid)


def approvals(store: Store) -> int:
    return store.query("SELECT COUNT(*) AS n FROM spend_approvals")[0]["n"]


def restart(store: Store, attempt_id: int) -> Spend:
    """The worker dies; the new process marks the attempt superseded (worker.recover)."""
    store.finish_attempt(attempt_id, "superseded")
    return make_spend(store)


# --------------------------------------------------------------------------- thrifty


def test_restart_before_any_call_resumes_on_the_same_approval(store, clock):
    tid = ticket(store)
    aid = thrifty_attempt(store, make_spend(store), clock, tid)
    spend2 = restart(store, aid)
    out = spend2.reopen_unspent_approvals([aid])
    assert [(o.attempt_id, o.reopened, o.left_usd) for o in out] == [(aid, True, 5.0)]
    ap = store.open_spend_approval(tid)
    assert ap is not None and ap.id == 1 and ap.reopened == 1
    # the next pass: the ticket starts again on the same approval, nobody is asked
    assert spend2.writer_gate(tid, approval_required=True) is None
    assert store.open_spend_approval(tid) is None and approvals(store) == 1


def test_paused_ticket_resumes_without_the_startup_call(store, clock):
    """The gates judge a ticket's approvals themselves (a cancel mid-run, or a pass that
    ran before recovery)."""
    tid = ticket(store)
    aid = thrifty_attempt(store, make_spend(store), clock, tid)
    spend2 = restart(store, aid)
    paused = store.flag_ticket(tid, f"{FLAG_THRIFTY}: a cloud writer attempt waits")
    assert spend2.resume_reason(paused) == "approved"
    assert spend2.writer_gate(tid, approval_required=True) is None


def test_restart_after_a_041_call_carries_459_to_the_next_attempt(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    aid = thrifty_attempt(store, spend, clock, tid)
    assert call(spend, 0.41, aid)
    spend2 = restart(store, aid)
    out = spend2.reopen_unspent_approvals([aid])
    assert out[0].reopened and out[0].left_usd == pytest.approx(4.59)
    assert spend2.approval_note(tid) == "approval #1: $4.59 of $5.00 left"
    assert f"ticket {tid}: approval #1: $4.59 of $5.00 left" in spend2.summary().status_line()
    aid2 = start_thrifty(store, spend2, clock, tid)  # no new approval
    assert approvals(store) == 1
    assert call(spend2, 4.59, aid2)  # exactly what is left
    assert not spend2.hook(CLOUD, 0.01, ctx(aid2), phase="estimate")  # the cap stays hard
    refusal = spend2.refusal_for_attempt(aid2)
    assert refusal is not None and refusal.cap == "approval" and refusal.needs_approval
    assert store.get_spend_approval(1).settled_at is not None  # used up: closed
    assert store.get_spend_approval(1).spent_usd == pytest.approx(5.0)
    assert spend2.approval_note(tid) is None


def test_restart_after_spending_it_all_pauses_for_a_new_approval(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    aid = thrifty_attempt(store, spend, clock, tid)
    assert call(spend, 2.5, aid) and call(spend, 2.5, aid)
    spend2 = restart(store, aid)
    out = spend2.reopen_unspent_approvals([aid])
    assert not out[0].reopened and out[0].spent_usd == pytest.approx(5.0)
    assert store.open_spend_approval(tid) is None
    assert spend2.writer_gate(tid, approval_required=True).cap == "thrifty"


def test_attempt_that_ends_normally_closes_its_approval(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    aid = thrifty_attempt(store, spend, clock, tid)
    assert call(spend, 0.41, aid)
    store.finish_attempt(aid, "failed_checks")
    out = spend.reopen_unspent_approvals()
    assert [o.reopened for o in out] == [False]
    assert store.open_spend_approval(tid) is None and spend.approval_note(tid) is None


def test_running_attempt_and_unstarted_attempt_are_left_alone(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    assert spend.writer_gate(tid, approval_required=True) is not None
    spend.approve(tid)
    assert spend.writer_gate(tid, approval_required=True) is None
    assert spend.reopen_unspent_approvals() == []  # no attempt row yet
    aid = store.add_attempt(tid, model="claude", tier=3, trigger="first").id
    assert spend.reopen_unspent_approvals() == []  # still running
    assert store.open_spend_approval(tid) is None
    store.finish_attempt(aid, "superseded")
    assert spend.reopen_unspent_approvals()[0].reopened


def test_approval_used_for_something_else_is_never_carried(store, clock):
    """A kind-less use (plan-quota resume, or a row from before migration 015)."""
    tid = ticket(store)
    ap = store.approve_spend(tid)
    assert store.consume_spend_approval(ap.id)
    aid = store.add_attempt(tid, model="claude", tier=3, trigger="first").id
    store.finish_attempt(aid, "superseded")
    assert make_spend(store).reopen_unspent_approvals() == []
    assert store.open_spend_approval(tid) is None


# --------------------------------------------------------------------------- over-cap grant


def capped_attempt(store: Store, spend: Spend, tid: int, *, approve: bool = True) -> int:
    store.record_spend("live", 20.0)  # the daily cap is reached
    aid = store.add_attempt(tid, model="claude", tier=3, trigger="first").id
    if approve:
        spend.approve(tid)
    return aid


def test_grant_cut_short_with_nothing_settled_is_carried_whole(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    aid = capped_attempt(store, spend, tid)
    assert spend.hook(CLOUD, 1.0, ctx(aid), phase="estimate")  # uses the approval
    ap = store.get_spend_approval(1)
    assert (ap.kind, ap.attempt_id, ap.limit_usd) == ("grant", aid, 5.0)
    spend2 = restart(store, aid)  # killed before the call settled: nothing on the ledger
    assert spend2.reopen_unspent_approvals([aid])[0].left_usd == pytest.approx(5.0)
    assert spend2.blocked(tid) is None  # the open approval lets the next attempt start


def test_grant_spend_survives_a_restart_and_the_rest_carries(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    aid = capped_attempt(store, spend, tid)
    assert call(spend, 0.41, aid)
    assert store.get_spend_approval(1).spent_usd == pytest.approx(0.41)  # not only in memory
    spend2 = restart(store, aid)
    assert spend2.reopen_unspent_approvals([aid])[0].left_usd == pytest.approx(4.59)
    assert spend2.blocked(tid) is None
    aid2 = capped_attempt(store, spend2, tid, approve=False)
    assert call(spend2, 4.59, aid2)
    assert not spend2.hook(CLOUD, 0.01, ctx(aid2), phase="estimate")
    assert spend2.refusal_for_attempt(aid2).cap == "approval"
    assert spend2.blocked(tid) is not None  # used up: pause and ask


def test_grant_counts_only_spend_under_it_not_the_attempts_earlier_spend(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    store.record_spend("live", 19.5)
    aid = store.add_attempt(tid, model="claude", tier=3, trigger="first").id
    assert call(spend, 0.5, aid)  # inside the cap: no approval involved
    spend.approve(tid)
    assert spend.hook(CLOUD, 1.0, ctx(aid), phase="estimate")  # past it: the grant
    spend2 = restart(store, aid)
    assert spend2.reopen_unspent_approvals([aid])[0].left_usd == pytest.approx(5.0)


def test_carried_scraps_too_small_for_a_call_close_the_approval(store, clock):
    """No silent loop: room left but less than the next call closes it and asks."""
    tid = ticket(store)
    spend = make_spend(store)
    aid = capped_attempt(store, spend, tid)
    assert call(spend, 4.9, aid)
    spend2 = restart(store, aid)
    assert spend2.reopen_unspent_approvals([aid])[0].left_usd == pytest.approx(0.1)
    aid2 = capped_attempt(store, spend2, tid, approve=False)
    assert not spend2.hook(CLOUD, 0.5, ctx(aid2), phase="estimate")
    assert store.get_spend_approval(1).settled_at is not None
    assert store.open_spend_approval(tid) is None


def test_cancel_in_this_process_waits_for_the_call_in_flight(store, clock):
    tid = ticket(store)
    spend = make_spend(store)
    aid = capped_attempt(store, spend, tid)
    assert spend.hook(CLOUD, 1.0, ctx(aid), phase="estimate")
    store.finish_attempt(aid, "superseded")  # cancelled while the call is out
    assert spend.reopen_unspent_approvals([aid]) == []  # not judged yet
    spend.hook(CLOUD, 0.30, ctx(aid), phase="actual")
    out = spend.reopen_unspent_approvals([aid])
    assert out[0].reopened and out[0].left_usd == pytest.approx(4.70)


def test_migration_015_columns(store):
    cols = {r["name"] for r in store.query("PRAGMA table_info(spend_approvals)")}
    assert {"attempt_id", "kind", "limit_usd", "spent_usd", "settled_at", "reopened"} <= cols


def test_approving_again_replaces_a_part_spent_carried_approval(store, clock):
    """Review fix: `fman approve-spend` on a ticket whose carried approval has only
    scraps left gives a fresh full approval, not the scraps."""
    tid = ticket(store)
    spend = make_spend(store)
    first = spend.approve(tid)
    assert spend.approve(tid).id == first.id  # unused: idempotent, as before
    store.consume_spend_approval(first.id, kind="attempt")
    store.add_spend_approval_spend(first.id, 4.80)
    assert store.reopen_spend_approval(first.id)  # carried with $0.20 left
    second = spend.approve(tid)
    assert second.id != first.id
    assert second.spent_usd == 0.0
    assert store.open_spend_approval(tid).id == second.id
    assert store.get_spend_approval(first.id).settled_at is not None
