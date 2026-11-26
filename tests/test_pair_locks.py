"""File locks for an ``ab:pair`` (#34 / GH #16): the two sides are one holder.

The store's pair API (``Store.pair_for_ticket``, migration 016) is built in parallel, so
these tests use a Store subclass that answers ``pair_for_ticket`` from a dict. Locks
looks the method up with ``getattr``, so the real one drops in unchanged.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from foreman_inator.db.store import Store, Ticket
from foreman_inator.locks import Locks, overlaps

REPO = "me/toy"


class PairStore(Store):
    """A real Store plus a fake ``pair_for_ticket``."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.pairs: dict[int, SimpleNamespace] = {}

    def link(self, a: int, b: int) -> None:
        p = SimpleNamespace(ticket_a=a, ticket_b=b, status="running")
        self.pairs[a] = self.pairs[b] = p

    def pair_for_ticket(self, ticket_id: int) -> SimpleNamespace | None:
        return self.pairs.get(ticket_id)


@pytest.fixture
def store(tmp_path: Path) -> PairStore:
    s = PairStore(tmp_path / "fman.db")
    yield s
    s.close()


@pytest.fixture
def locks(store: PairStore) -> Locks:
    return Locks(store)


_issue = iter(range(1, 10_000))


def ticket(store: Store, owned: tuple[str, ...] = ()) -> Ticket:
    t = store.create_ticket(
        repo=REPO,
        issue_number=next(_issue),
        title="t",
        request="r",
        base_branch="main",
        owned_files=owned,
    )
    return store.set_column(t.id, "in_progress")


def pair(store: PairStore, owned: tuple[str, ...] = ()) -> tuple[Ticket, Ticket]:
    a, b = ticket(store, owned), ticket(store, owned)
    store.link(a.id, b.id)
    return a, b


def assert_one_holder_per_path(store: Store) -> None:
    rows = store.list_locks()
    for i, x in enumerate(rows):
        for y in rows[i + 1 :]:
            if x.repo == y.repo and x.ticket_id != y.ticket_id:
                assert not overlaps(x.path, y.path), (x, y)


def test_sides_share_paths_and_store_them_under_ticket_a(store: PairStore, locks: Locks) -> None:
    a, b = pair(store, ("src/app.py",))
    assert locks.may_start(a).ok
    rb = locks.may_start(b)  # B wants exactly what A holds: no wait
    assert rb.ok and rb.held == ("src/app.py",)
    assert locks.acquire(b, ["src/new.py"]).ok  # B widens: the pair holds it
    assert {lk.ticket_id for lk in store.list_locks()} == {a.id}
    assert locks.held(a) == locks.held(b) == ("src/app.py", "src/new.py")
    assert locks.holder_id(b) == a.id
    assert locks.conflicts(b, ["src/app.py", "src/new.py"]) == ()
    assert store.get_ticket(b.id).owned_files == ("src/app.py", "src/new.py")
    assert store.get_ticket(a.id).owned_files == ("src/app.py",)  # own wanted sets
    assert_one_holder_per_path(store)


def test_third_ticket_waits_on_the_pair_as_one_holder(store: PairStore, locks: Locks) -> None:
    a, b = pair(store)
    assert locks.acquire(b, ["src/app.py"]).ok
    c = ticket(store, ("src/app.py",))
    rc = locks.may_start(c)
    assert not rc.ok and rc.blockers == (a.id,)
    assert [c2.holder for c2 in locks.conflicts(c, ["src/app.py"])] == [a.id]
    ws = locks.waiting(REPO)
    assert [(w.ticket_id, w.blockers) for w in ws] == [(c.id, (a.id,))]
    assert_one_holder_per_path(store)


def test_release_waits_for_both_sides(store: PairStore, locks: Locks) -> None:
    a, b = pair(store, ("src/app.py",))
    assert locks.may_start(a).ok
    c = ticket(store, ("src/app.py",))
    store.set_column(a.id, "dropped")  # side A lost; B goes on alone
    assert locks.release(a) == 0
    locks.sweep()  # B is open, so the pair's rows are not stale
    assert not locks.may_start(c).ok
    assert locks.may_start(b).ok
    store.set_column(b.id, "done")
    assert locks.release(b) == 1
    assert locks.release(b) == 0  # idempotent
    assert locks.may_start(c).ok


def test_release_pair_over_frees_while_a_side_is_open(store: PairStore, locks: Locks) -> None:
    a, b = pair(store, ("src/app.py",))
    assert locks.may_start(b).ok
    assert locks.release(b) == 0
    assert locks.release(b, pair_over=True) == 1
    assert locks.held(a) == ()


def test_deadlock_with_a_pair_younger_pair_yields_both_sides(
    store: PairStore, locks: Locks
) -> None:
    c = ticket(store, ("y.py",))  # oldest
    a, b = pair(store, ("x.py",))
    assert locks.may_start(c).ok and locks.may_start(a).ok
    assert not locks.acquire(c, ["x.py"]).ok  # C waits on the pair
    r = locks.acquire(b, ["y.py"])  # B (the pair) waits on C: cycle
    assert r.yielded == (a.id,)  # the pair is the youngest holder
    assert locks.held(a) == locks.held(b) == ()
    assert locks.may_start(c).ok
    assert_one_holder_per_path(store)


def test_deadlock_with_a_pair_older_pair_keeps_its_locks(store: PairStore, locks: Locks) -> None:
    a, b = pair(store, ("x.py",))
    c = ticket(store, ("y.py",))  # youngest
    assert locks.may_start(a).ok and locks.may_start(c).ok
    assert not locks.acquire(b, ["y.py"]).ok  # side B waits on C
    r = locks.acquire(c, ["x.py"])  # C waits on the pair (via side A's lock): cycle
    assert r.yielded == (c.id,)
    assert locks.may_start(b).ok  # the pair gets y.py
    assert set(locks.held(a)) == {"x.py", "y.py"}
    assert_one_holder_per_path(store)


def test_dormant_side_keeps_pair_locks_while_other_side_works(
    store: PairStore, locks: Locks
) -> None:
    a, b = pair(store, ("src/app.py",))
    assert locks.may_start(a).ok
    c = ticket(store, ("src/app.py",))
    store.flag_ticket(a.id, "setup_failed: install")  # A dormant, B still active
    locks.sweep()
    assert locks.held(b) == ("src/app.py",)
    assert [w.ticket_id for w in locks.waiting(REPO)] == [c.id]
    store.flag_ticket(b.id, "setup_failed: install")  # now no side will run
    assert [w.ticket_id for w in locks.waiting(REPO)] == []
    assert locks.may_start(c).ok


def test_chat_holder_with_a_pair(store: PairStore, locks: Locks) -> None:
    a, b = pair(store, ("src/app.py",))
    assert locks.may_start(b).ok
    sid = store.create_chat_session(REPO, base_branch="main", tier=1, model="m").id
    holders = locks.chat_touch(sid, REPO, ["src/app.py", "src/chat.py"])
    assert [(h.ticket_id, h.paths) for h in holders] == [(a.id, ("src/app.py",))]
    r = locks.acquire(a, ["src/chat.py"])
    assert not r.ok and r.chats == (sid,) and r.blockers == ()
    locks.release_chat(sid)
    assert locks.acquire(b, ["src/chat.py"]).ok


def test_store_without_pair_api_treats_every_ticket_alone(tmp_path: Path) -> None:
    s = Store(tmp_path / "plain.db")
    try:
        lk = Locks(s)
        a, b = ticket(s, ("src/app.py",)), ticket(s, ("src/app.py",))
        assert lk.may_start(a).ok
        r = lk.may_start(b)
        assert not r.ok and r.blockers == (a.id,)
        assert lk.holder_id(b) == b.id
    finally:
        s.close()
