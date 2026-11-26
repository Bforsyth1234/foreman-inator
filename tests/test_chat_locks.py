"""Chat sessions as file-lock holders (#71 decision 6, wave C1e).

Migration 012 (store slot C1a) adds ``file_locks.chat_session_id`` with an exactly-one-
holder CHECK and the ``chat_sessions`` table; every Store has it, so the fixtures here use
the store's own chat helpers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from foreman_inator.db.store import Store, Ticket
from foreman_inator.locks import Locks, overlaps
from foreman_inator.worker_status import render_status

REPO = "me/toy"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "fman.db")
    yield s
    s.close()


@pytest.fixture
def locks(store: Store) -> Locks:
    return Locks(store)


_issue = iter(range(1, 10_000))


def ticket(store: Store, *, owned: tuple[str, ...] = (), repo: str = REPO) -> Ticket:
    t = store.create_ticket(
        repo=repo,
        issue_number=next(_issue),
        title="t",
        request="r",
        base_branch="main",
        owned_files=owned,
    )
    return store.set_column(t.id, "in_progress")


def chat(store: Store, *, state: str = "busy", repo: str = REPO) -> int:
    sid = store.create_chat_session(repo, base_branch="main", tier=1, model="m").id
    if state != "starting":
        store.set_chat_state(sid, state, pr_number=1 if state == "opened_pr" else None)
    return sid


def set_state(store: Store, sid: int, state: str) -> None:
    store.set_chat_state(sid, state, pr_number=1 if state == "opened_pr" else None)


def rows(store: Store) -> list[sqlite3.Row]:
    return store.query("SELECT * FROM file_locks ORDER BY repo, path")


def assert_one_holder_per_path(store: Store) -> None:
    """No two holders (tickets or chats) hold overlapping locks in one repo."""
    rs = rows(store)
    for i, a in enumerate(rs):
        assert (a["ticket_id"] is None) != (a["chat_session_id"] is None), dict(a)
        for b in rs[i + 1 :]:
            same = (a["ticket_id"], a["chat_session_id"]) == (b["ticket_id"], b["chat_session_id"])
            if a["repo"] == b["repo"] and not same:
                assert not overlaps(a["path"], b["path"]), (dict(a), dict(b))


# ------------------------------------------------------------------------------ tests


def test_schema_check_rejects_two_holders_and_none(store: Store) -> None:
    t = ticket(store)
    sid = chat(store)
    for tid, cid in ((t.id, sid), (None, None)):
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute(
                "INSERT INTO file_locks (repo, path, ticket_id, chat_session_id, acquired_at)"
                " VALUES (?, 'a.py', ?, ?, 'x')",
                (REPO, tid, cid),
            )


def test_ticket_waits_on_chat_lock(store: Store, locks: Locks) -> None:
    sid = chat(store)
    assert locks.chat_touch(sid, REPO, ["src/A.py", "./README.md"]) == []
    assert locks.chat_held(sid) == ("readme.md", "src/a.py")
    t = ticket(store, owned=("src/a.py", "src/b.py"))
    r = locks.may_start(t)
    assert not r.ok
    assert r.chats == (sid,)
    assert r.blockers == ()  # ticket ids only: callers look these up as tickets
    assert locks.held(t) == ()  # all-or-nothing: b.py is not taken either
    [w] = locks.waiting(REPO)
    assert (w.ticket_id, w.blockers, w.chats) == (t.id, (), (sid,))
    assert locks.release_chat(sid) == 2
    assert locks.release_chat(sid) == 0
    assert locks.may_start(t).ok
    assert_one_holder_per_path(store)


def test_chat_directory_lock_blocks_ticket_file(store: Store, locks: Locks) -> None:
    sid = chat(store)
    locks.chat_touch(sid, REPO, ["src/auth/"])
    blocked = ticket(store, owned=("src/auth/login.py",))
    free = ticket(store, owned=("src/authz.py",))
    assert locks.may_start(blocked).chats == (sid,)
    assert locks.may_start(free).ok


def test_chat_never_waits_on_ticket_and_reports_holders(store: Store, locks: Locks) -> None:
    t1 = ticket(store, owned=("a.py", "lib/"))
    t2 = ticket(store, owned=("b.py",))
    assert locks.may_start(t1).ok and locks.may_start(t2).ok
    sid = chat(store)
    got = locks.chat_touch(sid, REPO, ["a.py", "lib/x.py", "b.py", "c.py"])
    assert [(h.ticket_id, h.issue_number, h.paths) for h in got] == [
        (t1.id, t1.issue_number, ("a.py", "lib/x.py")),
        (t2.id, t2.issue_number, ("b.py",)),
    ]
    assert locks.chat_held(sid) == ("c.py",)  # took the free path, not the held ones
    assert locks.held(t1) == ("a.py", "lib/") and locks.held(t2) == ("b.py",)
    assert_one_holder_per_path(store)
    # once the ticket releases, the next touch takes the file
    locks.release(t2)
    assert locks.chat_touch(sid, REPO, ["b.py"]) == []
    assert locks.chat_held(sid) == ("b.py", "c.py")


def test_chat_touch_is_idempotent_and_covered_by_own_dir(store: Store, locks: Locks) -> None:
    sid = chat(store)
    locks.chat_touch(sid, REPO, ["docs/"])
    locks.chat_touch(sid, REPO, ["docs/a.md", "docs/"])
    assert locks.chat_held(sid) == ("docs/",)
    assert locks.chat_touch(sid, REPO, ["../etc/passwd", ".git/config", ""]) == []
    assert locks.chat_held(sid) == ("docs/",)


def test_second_chat_cannot_take_first_chats_path(store: Store, locks: Locks) -> None:
    a, b = chat(store), chat(store)
    locks.chat_touch(a, REPO, ["x.py"])
    assert locks.chat_touch(b, REPO, ["x.py", "y.py"]) == []
    assert locks.chat_held(a) == ("x.py",)
    assert locks.chat_held(b) == ("y.py",)
    assert_one_holder_per_path(store)


def test_chat_ids_do_not_collide_with_ticket_ids(store: Store, locks: Locks) -> None:
    """Chat session 1 and ticket 1 are different holders: the ticket does not see the
    chat's lock as its own, and the wait-for graph does not mistake one for the other."""
    t = ticket(store, owned=("own.py",))
    sid = chat(store)
    assert sid == t.id  # both 1 in a fresh store
    assert locks.may_start(t).ok
    locks.chat_touch(sid, REPO, ["shared.py"])
    r = locks.acquire(t, ["shared.py"])
    assert not r.ok and r.chats == (sid,) and r.blockers == ()
    assert locks.held(t) == ("own.py",)


def test_no_deadlock_yield_through_chat(store: Store, locks: Locks) -> None:
    """A ticket waiting on a chat while another ticket waits on it is not a cycle."""
    sid = chat(store)
    t1 = ticket(store, owned=("one.py",))
    t2 = ticket(store, owned=("two.py",))
    assert locks.may_start(t1).ok and locks.may_start(t2).ok
    locks.chat_touch(sid, REPO, ["chat.py"])
    r1 = locks.acquire(t1, ["chat.py"])
    r2 = locks.acquire(t2, ["one.py"])
    assert r1.chats == (sid,) and r1.yielded == ()
    assert r2.blockers == (t1.id,) and r2.yielded == ()
    assert locks.held(t1) == ("one.py",)


def test_sweep_drops_final_chat_sessions(store: Store, locks: Locks) -> None:
    live, gone = chat(store), chat(store)
    locks.chat_touch(live, REPO, ["a.py"])
    locks.chat_touch(gone, REPO, ["b.py"])
    set_state(store, gone, "ended")
    assert locks.sweep() == 1
    assert locks.chat_held(gone) == () and locks.chat_held(live) == ("a.py",)
    # acquire sweeps its repo first, so a leaked chat lock never blocks a ticket
    set_state(store, live, "discarded")
    assert locks.may_start(ticket(store, owned=("a.py",))).ok


def test_existing_ticket_flow_unchanged_with_chat_rows(store: Store, locks: Locks) -> None:
    """A chat row in the table does not upset the ticket-only paths (sweep, release)."""
    sid = chat(store)
    locks.chat_touch(sid, REPO, ["c.py"])
    t = ticket(store, owned=("t.py",))
    assert locks.may_start(t).ok
    store.set_column(t.id, "done")
    assert locks.sweep() == 1
    assert locks.chat_held(sid) == ("c.py",)


def test_status_says_held_by_chat_session(store: Store, locks: Locks) -> None:
    sid = chat(store)
    locks.chat_touch(sid, REPO, ["a.py"])
    holder = ticket(store, owned=("b.py",))
    assert locks.may_start(holder).ok
    t = ticket(store, owned=("a.py",))
    both = ticket(store, owned=("a.py", "b.py"))
    assert not locks.may_start(t).ok
    out = render_status(store)
    lines = out.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith(f"  #{t.id} "))
    assert f"WAITING: on files held by chat session {sid}" in lines[i + 1]
    assert "held by ticket(s)" not in lines[i + 1]
    j = next(n for n, ln in enumerate(lines) if ln.startswith(f"  #{both.id} "))
    block = "\n".join(lines[j + 1 : j + 3])
    assert f"held by ticket(s) #{holder.id}" in block
    assert f"held by chat session {sid}" in block
    assert "2 waiting." in out
