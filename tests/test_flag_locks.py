"""#50 (a closed ticket shows no FLAGGED line) and #55 (a dormant flagged ticket with no
PR stops holding its file locks), on a real Store under tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from foreman_inator.db.store import Store, Ticket
from foreman_inator.locks import DORMANT_FLAGS, Locks, dormant, overlaps
from foreman_inator.worker_status import render_status

REPO = "me/toy"
_issue = iter(range(1, 10_000))


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


@pytest.fixture
def locks(store: Store) -> Locks:
    return Locks(store)


def ticket(store: Store, owned: tuple[str, ...] = ("README.md",)) -> Ticket:
    t = store.create_ticket(
        repo=REPO,
        issue_number=next(_issue),
        title="t",
        request="r",
        base_branch="main",
        owned_files=owned,
    )
    return store.set_column(t.id, "in_progress")


def assert_no_shared_paths(store: Store) -> None:
    rows = store.list_locks()
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if a.repo == b.repo and a.ticket_id != b.ticket_id:
                assert not overlaps(a.path, b.path), (a, b)


# --------------------------------------------------------------------------- #50


@pytest.mark.parametrize("column", ["done", "dropped"])
def test_closed_handed_over_ticket_shows_no_flag(store: Store, column: str) -> None:
    t = ticket(store)
    store.update_ticket(t.id, pr_number=7)
    store.flag_ticket(t.id, "reviewer_handover: the AI reviewer handed the change to you")
    store.set_column(t.id, "testing")
    store.set_column(t.id, column)  # merged (done) or closed unmerged (dropped)
    out = render_status(store)
    assert "FLAGGED" not in out
    assert "0 flagged for you" in out
    assert store.get_ticket(t.id).flag_reason  # kept as history


def test_open_flagged_ticket_still_shows_flag(store: Store) -> None:
    t = ticket(store)
    store.flag_ticket(t.id, "reviewer_handover: x")
    out = render_status(store)
    assert "FLAGGED: reviewer_handover: x" in out
    assert "1 flagged for you" in out


# --------------------------------------------------------------------------- #55


@pytest.mark.parametrize("kind", sorted(DORMANT_FLAGS))
def test_dormant_flag_without_pr_releases_locks_for_second_ticket(
    store: Store, locks: Locks, kind: str
) -> None:
    first = ticket(store)
    assert locks.may_start(first).ok
    second = ticket(store)
    assert not locks.may_start(second).ok
    assert [w.ticket_id for w in locks.waiting(REPO)] == [second.id]

    store.flag_ticket(first.id, f"{kind}: something broke")
    assert dormant(store.get_ticket(first.id))
    assert locks.waiting(REPO) == []  # read-only view already treats it as released
    assert locks.may_start(second).ok
    assert locks.held(first.id) == ()
    assert locks.held(second.id) == ("readme.md",)
    assert store.get_ticket(first.id).owned_files == ("README.md",)  # kept for resume
    assert_no_shared_paths(store)


def test_flagged_ticket_with_open_pr_keeps_blocking(store: Store, locks: Locks) -> None:
    first = ticket(store)
    assert locks.may_start(first).ok
    store.update_ticket(first.id, pr_number=12)
    store.flag_ticket(first.id, "checks_failed: verify failed on tier 3")
    assert not dormant(store.get_ticket(first.id))
    second = ticket(store)
    res = locks.may_start(second)
    assert not res.ok and res.blockers == (first.id,)
    assert locks.held(first.id) == ("readme.md",)


def test_non_dormant_flag_keeps_blocking(store: Store, locks: Locks) -> None:
    first = ticket(store)
    assert locks.may_start(first).ok
    store.flag_ticket(first.id, "reviewer_handover: x")
    second = ticket(store)
    assert not locks.may_start(second).ok


def test_resumed_ticket_reacquires_or_waits(store: Store, locks: Locks) -> None:
    first = ticket(store)
    assert locks.may_start(first).ok
    store.flag_ticket(first.id, "setup_failed: install: boom")
    second = ticket(store)
    assert locks.may_start(second).ok

    store.clear_flag(first.id)  # the user re-labels the issue
    res = locks.may_start(first)
    assert not res.ok and res.blockers == (second.id,)  # second now holds it
    assert_no_shared_paths(store)

    locks.release(second.id)
    assert locks.may_start(first).ok
    assert locks.held(first.id) == ("readme.md",)


def test_dormant_ticket_acquiring_keeps_its_own_locks(store: Store, locks: Locks) -> None:
    t = ticket(store)
    assert locks.may_start(t).ok
    store.flag_ticket(t.id, "setup_failed: x")
    assert locks.may_start(t).ok  # the caller is never swept by its own acquire
    assert locks.held(t.id) == ("readme.md",)


def test_waiting_line_says_what_releases_a_flagged_holder(store: Store, locks: Locks) -> None:
    first = ticket(store)
    assert locks.may_start(first).ok
    store.update_ticket(first.id, pr_number=3)
    store.flag_ticket(first.id, "reviewer_handover: x")
    second = ticket(store)
    assert not locks.may_start(second).ok
    out = render_status(store)
    assert f"WAITING: on files held by ticket(s) #{first.id}" in out
    assert f"close or re-label issue {REPO}#{first.issue_number}" in out
