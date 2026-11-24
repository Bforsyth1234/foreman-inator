"""File locks for parallel tickets (todo #19), on a real Store under tmp_path."""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path

import pytest

from foreman_inator.db.store import Store, Ticket
from foreman_inator.locks import (
    OPEN_COLUMNS,
    Locks,
    clean_paths,
    normalise,
    overlaps,
    predict_files,
)
from foreman_inator.tickets import lifecycle
from foreman_inator.tickets.lifecycle import Action

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


def ticket(store: Store, *, repo: str = REPO, owned: tuple[str, ...] = ()) -> Ticket:
    t = store.create_ticket(
        repo=repo,
        issue_number=next(_issue),
        title="t",
        request="r",
        base_branch="main",
        owned_files=owned,
    )
    return store.set_column(t.id, "in_progress")


def assert_no_shared_paths(store: Store) -> None:
    """The core invariant: no two tickets hold overlapping locks in one repo."""
    rows = store.list_locks()
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if a.repo == b.repo and a.ticket_id != b.ticket_id:
                assert not overlaps(a.path, b.path), (a, b)


def assert_no_wait_cycle(locks: Locks, repo: str = REPO) -> None:
    edges = {w.ticket_id: set(w.blockers) for w in locks.waiting(repo)}
    state: dict[int, int] = {}

    def visit(n: int) -> None:
        state[n] = 1
        for m in edges.get(n, ()):
            assert state.get(m) != 1, f"wait-for cycle through {n} -> {m}: {edges}"
            if m not in state:
                visit(m)
        state[n] = 2

    for n in edges:
        if n not in state:
            visit(n)


# ------------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("src/a.py", "src/a.py"),
        ("./src/a.py", "src/a.py"),
        ("/src/a.py", "src/a.py"),
        ("././src//a.py", "src/a.py"),
        ("src/x/../a.py", "src/a.py"),
        ("src\\win\\a.py", "src/win/a.py"),
        ("src/a.py:42", "src/a.py"),
        ("src/a.py:42:7", "src/a.py"),
        ("src/a.py#L10-L20", "src/a.py"),
        ("src/auth/", "src/auth/"),
        ("./src/auth/./", "src/auth/"),
        ("../etc/passwd", None),
        ("src/../../x.py", None),
        (".git/config", None),
        ("~/secrets.toml", None),
        ("C:/x/a.py", None),
        ("https://example.com/a.py", None),
        ("", None),
        (".", None),
        ("/", None),
    ],
)
def test_normalise(raw: str, want: str | None) -> None:
    assert normalise(raw) == want


def test_clean_paths_dedupes_case_insensitively_and_reduces_globs() -> None:
    got = clean_paths(["Src/A.py", "src/a.py", "./src/a.py", "src/**/*.py", "*.md", "lib/*"])
    assert got == ["Src/A.py", "src/", "lib/"]


def test_overlaps_directory_rule() -> None:
    assert overlaps("src/auth/", "src/auth/login.py")
    assert overlaps("src/auth/login.py", "src/auth/")
    assert overlaps("src/", "src/auth/")
    assert not overlaps("src/auth/", "src/authz.py")
    assert not overlaps("src/a.py", "src/a.pyi")


# ---------------------------------------------------------------------- prediction


def test_predict_files_from_issue_and_router() -> None:
    title = "Crash in `src/auth/login.py` on empty password"
    body = (
        "Steps: see ./src/auth/session.py:42 and README.md (docs/guide.md).\n"
        "The handlers under src/handlers/ and `tests/**/test_*.py` too; also .gitignore\n"
        "and the Makefile. Not these: https://example.com/a/b.py, me@example.com,\n"
        "Node.js, e.g. v1.2.3, client/server, *.py, `foreman_inator.locks`, ../etc/passwd."
    )
    got = predict_files(title, body, extra=["src/auth/LOGIN.py", "src/router_guess.py"])
    assert sorted(got) == sorted(
        [
            "src/auth/login.py",
            "src/auth/session.py",
            "README.md",
            "docs/guide.md",
            "src/handlers/",
            ".gitignore",
            "Makefile",
            "tests/",
            "src/router_guess.py",
        ]
    )
    assert got[0] == "src/auth/login.py"  # title first, router guesses last
    assert got[-1] == "src/router_guess.py"


def test_issue_naming_no_files_predicts_nothing() -> None:
    assert predict_files("Make the app faster", "It feels slow when I open it.") == ()


# ------------------------------------------------------------ waiting and releasing


def test_overlapping_tickets_second_waits_until_first_releases(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store)
    assert locks.acquire(a, ["src/a.py"]).ok
    r = locks.acquire(b, ["src/a.py", "src/b.py"])
    assert not r.ok and r.blockers == (a.id,)
    assert r.held == ()  # all or nothing: b.py was not taken either
    assert [w.ticket_id for w in locks.waiting(REPO)] == [b.id]
    assert store.get_ticket(b.id).owned_files == ("src/a.py", "src/b.py")  # remembered
    assert locks.release(a) == 1
    r = locks.may_start(b)  # the queue's check; no files passed: uses owned_files
    assert r.ok and r.held == ("src/a.py", "src/b.py")
    assert_no_shared_paths(store)


@pytest.mark.parametrize("event", ["merged", "dropped", "label_removed"])
def test_released_on_merge_and_drop(store: Store, locks: Locks, event: str) -> None:
    a = ticket(store)
    b = ticket(store)
    assert locks.acquire(a, ["x.py"]).ok
    assert not locks.acquire(b, ["x.py"]).ok
    ev = {
        "merged": lifecycle.Merged(REPO, a.issue_number, ticket_id=a.id, pr_number=7),
        "dropped": lifecycle.ClosedUnmerged(
            REPO, a.issue_number, ticket_id=a.id, pr_number=None, what="issue"
        ),
        "label_removed": lifecycle.AgentLabelRemoved(
            REPO, a.issue_number, ticket_id=a.id, removed_by="me"
        ),
    }[event]
    tr = lifecycle.apply("testing", ev)
    assert Action.RELEASE_LOCKS in tr.actions
    store.set_column(a.id, tr.column)
    assert locks.release(a) == 1  # what the worker's RELEASE_LOCKS handler does
    assert locks.release(a) == 0  # safe twice
    assert locks.may_start(b).ok


def test_stale_locks_of_closed_tickets_are_swept(store: Store, locks: Locks) -> None:
    a = ticket(store)
    c = ticket(store)
    b = ticket(store)
    assert locks.acquire(a, ["x.py"]).ok
    assert locks.acquire(c, ["y.py"]).ok
    store.set_column(a.id, "done")  # crash: column moved, RELEASE_LOCKS never ran
    store.set_column(c.id, "dropped")
    assert locks.sweep() == 2
    assert store.list_locks() == []
    assert locks.sweep() == 0
    # and acquire sweeps on its own, so a leak never blocks anyone
    d = ticket(store)
    assert locks.acquire(d, ["z.py"]).ok
    store.set_column(d.id, "done")
    assert locks.acquire(b, ["z.py"]).ok


def test_closed_ticket_cannot_acquire(store: Store, locks: Locks) -> None:
    a = ticket(store)
    store.set_column(a.id, "dropped")
    r = locks.widen(a.id, ["x.py"])
    assert r.closed and not r.ok and store.list_locks() == []


def test_other_repos_do_not_block(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store, repo="me/other")
    assert locks.acquire(a, ["x.py"]).ok
    assert locks.acquire(b, ["x.py"]).ok


def test_case_insensitive(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store)
    assert locks.acquire(a, ["Src/Login.PY"]).ok
    assert locks.held(a) == ("src/login.py",)
    assert locks.acquire(b, ["src/login.py"]).blockers == (a.id,)


def test_directory_lock_blocks_files_under_it(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store)
    c = ticket(store)
    assert locks.acquire(a, ["src/auth/"]).ok
    assert locks.acquire(a, ["src/auth/login.py"]).ok  # inside its own directory lock
    assert locks.acquire(b, ["src/authz.py"]).ok
    assert locks.acquire(c, ["src/auth/x/y.py"]).blockers == (a.id,)
    assert locks.acquire(c, ["src/"]).blockers == (a.id, b.id)
    assert_no_shared_paths(store)


# ---------------------------------------------------------------------- widening


def test_diff_touching_a_locked_file_pauses_the_ticket(store: Store, locks: Locks) -> None:
    """The item's conflict test. Both tickets start; B's diff reaches into A's file."""
    a = ticket(store)
    b = ticket(store)
    assert locks.acquire(a, predict_files("Fix `src/a.py`", "")).ok
    assert locks.acquire(b, predict_files("Fix `src/b.py`", "")).ok
    diff = ["src/b.py", "src/a.py", "tests/test_b.py"]
    assert locks.conflicts(b, diff)[0].holder == a.id  # read-only check
    assert locks.held(b) == ("src/b.py",)
    r = locks.widen(b, diff)
    assert not r.ok
    assert r.blockers == (a.id,)
    assert [c.path for c in r.conflicts] == ["src/a.py"]
    assert r.held == ("src/b.py",)  # nothing new taken; keeps what it had
    assert locks.held(a) == ("src/a.py",)  # the earlier holder wins
    assert [(w.ticket_id, w.blockers) for w in locks.waiting(REPO)] == [(b.id, (a.id,))]
    assert_no_shared_paths(store)
    locks.release(a)
    r = locks.may_start(b)
    assert r.ok and set(r.held) == {"src/a.py", "src/b.py", "tests/test_b.py"}


def test_two_unpredicted_tickets_first_to_widen_wins(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store)
    assert locks.may_start(a).ok and locks.may_start(b).ok  # nothing predicted, nothing held
    assert locks.widen(b, ["app.py"]).ok
    r = locks.widen(a, ["app.py", "util.py"])
    assert r.blockers == (b.id,) and r.held == ()
    assert_no_shared_paths(store)


def test_renames_lock_both_paths(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store)
    assert locks.widen(a, [], renames=[("old/name.py", "new/name.py")]).ok
    assert locks.held(a) == ("new/name.py", "old/name.py")
    assert locks.acquire(b, ["old/name.py"]).blockers == (a.id,)


def test_conflicts_is_read_only(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store)
    locks.acquire(a, ["x.py"])
    assert locks.conflicts(b, ["x.py", "y.py"])
    assert locks.held(b) == () and store.get_ticket(b.id).owned_files == ()


# ---------------------------------------------------------------------- deadlocks


def test_paused_ticket_cannot_deadlock_with_its_blocker_younger_closes_cycle(
    store: Store, locks: Locks
) -> None:
    a = ticket(store)
    b = ticket(store)
    assert locks.acquire(a, ["a.py"]).ok and locks.acquire(b, ["b.py"]).ok
    assert locks.widen(a, ["b.py"]).blockers == (b.id,)  # A waits on B
    r = locks.widen(b, ["a.py"])  # B would wait on A: a cycle; B is younger and yields
    assert r.yielded == (b.id,) and r.held == () and r.blockers == (a.id,)
    assert_no_wait_cycle(locks)
    assert locks.may_start(a).ok  # A (older) proceeds
    assert set(locks.held(a)) == {"a.py", "b.py"}
    assert not locks.may_start(b).ok
    locks.release(a)
    assert locks.may_start(b).ok


def test_paused_ticket_cannot_deadlock_older_closes_cycle(store: Store, locks: Locks) -> None:
    a = ticket(store)
    b = ticket(store)
    assert locks.acquire(a, ["a.py"]).ok and locks.acquire(b, ["b.py"]).ok
    assert locks.widen(b, ["a.py"]).blockers == (a.id,)  # B waits on A
    r = locks.widen(a, ["b.py"])  # closes the cycle; the youngest (B) yields, A wins now
    assert r.ok and r.yielded == (b.id,)
    assert locks.held(b) == ()
    assert_no_wait_cycle(locks)
    assert_no_shared_paths(store)


def test_three_way_cycle_is_broken(store: Store, locks: Locks) -> None:
    a, b, c = ticket(store), ticket(store), ticket(store)
    for t, p in ((a, "a.py"), (b, "b.py"), (c, "c.py")):
        assert locks.acquire(t, [p]).ok
    assert not locks.widen(a, ["b.py"]).ok  # a -> b
    assert not locks.widen(b, ["c.py"]).ok  # b -> c
    r = locks.widen(c, ["a.py"])  # c -> a closes a 3-cycle; c is youngest
    assert r.yielded == (c.id,)
    assert_no_wait_cycle(locks)
    assert locks.may_start(b).ok  # c.py is free now
    assert not locks.may_start(a).ok  # still waits on b, which is not waiting: progress


def test_random_schedules_keep_both_invariants(tmp_path: Path) -> None:
    paths = ["a.py", "b.py", "c.py", "src/", "src/x.py", "src/y/z.py", "docs/", "docs/i.md"]
    for seed in range(12):
        with Store(tmp_path / f"r{seed}.db") as store:
            lk = Locks(store)
            rng = random.Random(seed)
            ts = [ticket(store) for _ in range(5)]
            for _ in range(120):
                t = rng.choice(ts)
                op = rng.random()
                if op < 0.6:
                    lk.widen(t, rng.sample(paths, rng.randint(1, 3)))
                elif op < 0.8:
                    lk.may_start(t)
                elif op < 0.9:
                    lk.release(t)
                    store.update_ticket(t.id, owned_files=[])  # a fresh ticket in its place
                else:
                    lk.sweep()
                assert_no_shared_paths(store)
                assert_no_wait_cycle(lk)


class _SlowLocks(Locks):
    """Widens the read-then-insert window, so a missing transaction would show."""

    def _rows(self, repo: str):  # type: ignore[no-untyped-def]
        rows = super()._rows(repo)
        time.sleep(0.005)
        return rows


@pytest.mark.parametrize("contested", ["hot.py", "dir"])
def test_concurrent_acquire_one_winner(tmp_path: Path, contested: str) -> None:
    """Two tickets can never both hold one path, across threads and store connections.

    ``dir``: half the tickets want ``src/``, the rest ``src/f<i>.py``: different primary
    keys, so only the transaction around the overlap check keeps them apart.
    """
    db = tmp_path / "fman.db"
    with Store(db) as setup:
        ids = [ticket(setup).id for _ in range(8)]
    stores = [Store(db) for _ in range(4)]
    barrier = threading.Barrier(len(ids))
    results: dict[int, bool] = {}

    def want(i: int) -> list[str]:
        if contested == "hot.py":
            return ["hot.py", f"own{i}.py"]
        return ["src/"] if i % 2 == 0 else [f"src/f{i}.py"]

    def go(i: int, tid: int) -> None:
        barrier.wait()
        results[tid] = _SlowLocks(stores[i % len(stores)]).acquire(tid, want(i)).ok

    threads = [threading.Thread(target=go, args=(i, t)) for i, t in enumerate(ids)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    try:
        assert len(results) == len(ids) and any(results.values())
        assert_no_shared_paths(stores[0])
        if contested == "hot.py":
            assert sum(results.values()) == 1
            winner = next(t for t, ok in results.items() if ok)
            assert {r.ticket_id for r in stores[0].list_locks()} == {winner}
    finally:
        for s in stores:
            s.close()


def test_open_columns_match_store() -> None:
    from foreman_inator.db.store import OPEN_COLUMNS as STORE_OPEN

    assert set(OPEN_COLUMNS) == set(STORE_OPEN)
