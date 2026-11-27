"""#83 (GH #82): a blocker paused at a spend cap gets the approve-spend hint, not the
"is flagged: close or re-label" text, in both ``fman status`` and the activity page."""

from __future__ import annotations

from pathlib import Path

import pytest

from foreman_inator.db.store import Store, Ticket
from foreman_inator.locks import Locks
from foreman_inator.spend import FLAG_SPEND_CAP
from foreman_inator.worker_status import _release_hint, render_status

REPO = "me/toy"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


def _ticket(store: Store, issue: int) -> Ticket:
    t = store.create_ticket(
        repo=REPO,
        issue_number=issue,
        title="t",
        request="r",
        base_branch="main",
        owned_files=("README.md",),
    )
    return store.set_column(t.id, "in_progress")


def _blocked_pair(store: Store, reason: str) -> tuple[Ticket, Ticket]:
    locks = Locks(store)
    first = _ticket(store, 1)
    assert locks.may_start(first).ok
    store.update_ticket(first.id, pr_number=3)  # keeps its locks whatever the flag
    store.flag_ticket(first.id, reason)
    second = _ticket(store, 2)
    assert not locks.may_start(second).ok
    return first, second


def test_spend_paused_blocker_names_approve_spend(store: Store) -> None:
    first, _ = _blocked_pair(store, f"{FLAG_SPEND_CAP}: thrifty profile: over the cap")
    out = render_status(store)
    assert f"WAITING: on files held by ticket(s) #{first.id}" in out
    assert f"(#{first.id} waits for spend approval: run `fman approve-spend {first.id}`)" in out
    assert "is flagged" not in out
    assert "close or re-label" not in out
    assert "0 flagged for you" in out  # footer count unchanged: a spend pause is a wait


def test_real_flag_blocker_still_says_close_or_relabel(store: Store) -> None:
    first, _ = _blocked_pair(store, "reviewer_handover: x")
    out = render_status(store)
    assert f"#{first.id} is flagged: close or re-label issue {REPO}#{first.issue_number}" in out
    assert "approve-spend" not in out
    assert "1 flagged for you" in out


def test_mixed_blockers_each_get_their_own_hint(store: Store) -> None:
    a = _ticket(store, 1)
    b = _ticket(store, 2)
    store.flag_ticket(a.id, f"{FLAG_SPEND_CAP}: cap")
    store.flag_ticket(b.id, "setup_failed: x")
    hint = _release_hint((a.id, b.id), store)
    assert hint == (
        f" (#{a.id} waits for spend approval: run `fman approve-spend {a.id}`; "
        f"#{b.id} is flagged: close or re-label issue {REPO}#{b.issue_number})"
    )
