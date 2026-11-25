"""Migration 012 and the chat store methods (#60, docs/chat-design.md section 12)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foreman_inator.db import store as store_mod
from foreman_inator.db.store import NotFoundError, Store, StoreError

REPO = "me/toy"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    s = Store(tmp_path / "fman.db", clock=clock)
    yield s
    s.close()


def _ticket(s: Store, issue: int = 1):
    return s.create_ticket(
        repo=REPO, issue_number=issue, title="t", request="r", base_branch="main"
    )


def _session(s: Store, **kw):
    args = {"base_branch": "main", "tier": 1, "model": "qwen"}
    args.update(kw)
    return s.create_chat_session(REPO, **args)  # type: ignore[arg-type]


# --- sessions ---------------------------------------------------------------------------


def test_create_session_defaults_branch_to_its_id(store: Store, clock: Clock) -> None:
    a = _session(store)
    b = _session(store, base_commit="c" * 40, worktree="/w/chat-2")
    assert (a.id, a.branch, a.state, a.started_at) == (1, "fman/chat/1", "starting", clock.now)
    assert b.branch == "fman/chat/2" and b.base_commit == "c" * 40
    assert _session(store, branch="fman/chat/x").branch == "fman/chat/x"
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _session(store, branch="fman/42")  # never a ticket branch
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _session(store, tier=4)
    with pytest.raises(NotFoundError):
        store.get_chat_session(999)


def test_update_and_list_sessions(store: Store) -> None:
    a = _session(store)
    _session(store)
    s = store.update_chat_session(a.id, opencode_session_id="ses_1", trace_id="t" * 32, tier=2)
    assert (s.opencode_session_id, s.tier) == ("ses_1", 2)
    with pytest.raises(TypeError):
        store.update_chat_session(a.id, state="idle")  # states go through set_chat_state
    store.set_chat_state(a.id, "idle")
    assert [x.id for x in store.list_chat_sessions()] == [2, 1]
    assert [x.id for x in store.list_chat_sessions(states=("idle", "busy"))] == [1]
    assert store.list_chat_sessions(states=()) == []
    assert store.list_chat_sessions(repo="other/repo") == []


def test_chat_state_lifecycle(store: Store, clock: Clock) -> None:
    s = _session(store)
    assert store.set_chat_state(s.id, "idle").ended_at is None
    store.set_chat_state(s.id, "busy")
    clock.now += timedelta(minutes=30)
    ended = store.set_chat_state(s.id, "ended", end_reason="host_restart")
    assert (ended.ended_at, ended.end_reason) == (clock.now, "host_restart")
    resumed = store.set_chat_state(s.id, "idle")  # resume of an ended session
    assert (resumed.ended_at, resumed.end_reason) == (None, None)
    store.set_chat_state(s.id, "closing")
    with pytest.raises(ValueError, match="pr_number"):
        store.set_chat_state(s.id, "opened_pr")
    done = store.set_chat_state(s.id, "opened_pr", pr_number=17)
    assert (done.state, done.pr_number, done.ended_at) == ("opened_pr", 17, clock.now)
    with pytest.raises(StoreError, match="opened_pr"):
        store.set_chat_state(s.id, "idle")  # final
    with pytest.raises(ValueError, match="unknown chat state"):
        store.set_chat_state(s.id, "paused")  # type: ignore[arg-type]
    f = _session(store)
    store.set_chat_state(f.id, "failed", end_reason="sandbox")
    with pytest.raises(StoreError):
        store.set_chat_state(f.id, "starting")


# --- turns ------------------------------------------------------------------------------


def test_turns_number_per_session_and_finish_once(store: Store, clock: Clock) -> None:
    a, b = _session(store), _session(store)
    t1 = store.add_chat_turn(a.id, tier=1, model="qwen")
    t2 = store.add_chat_turn(a.id, tier=3, model="opus")
    u1 = store.add_chat_turn(b.id, tier=1, model="qwen")
    assert (t1.seq, t2.seq, u1.seq) == (1, 2, 1)
    assert t1.status is None and t1.ended_at is None and t1.cost_usd == 0
    clock.now += timedelta(seconds=40)
    done = store.finish_chat_turn(
        t2.id, "finished", tokens_in=100, tokens_out=20, cost_usd=0.5, tool_calls=3, edits=2
    )
    assert (done.status, done.tokens_in, done.cost_usd, done.edits) == ("finished", 100, 0.5, 2)
    assert done.ended_at == clock.now
    with pytest.raises(StoreError, match="already finished"):
        store.finish_chat_turn(t2.id, "aborted")
    refused = store.finish_chat_turn(t1.id, "refused")
    assert (refused.status, refused.tokens_in) == ("refused", 0)
    with pytest.raises(ValueError):
        store.finish_chat_turn(u1.id, "done")  # type: ignore[arg-type]
    assert [t.seq for t in store.list_chat_turns(a.id)] == [1, 2]
    with pytest.raises(NotFoundError):
        store.add_chat_turn(999, tier=1, model="qwen")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), store.transaction() as conn:
        conn.execute(
            "INSERT INTO chat_turns (session_id, seq, tier, model, started_at)"
            " VALUES (?, 1, 1, 'q', 'x')",
            (a.id,),
        )


# --- spend ------------------------------------------------------------------------------


def test_chat_spend_per_session_and_window(store: Store, clock: Clock) -> None:
    a, b = _session(store), _session(store)
    t = _ticket(store)
    start = clock.now
    e = store.record_spend("live", 0.25, model="glm", chat_session_id=a.id)
    assert e.chat_session_id == a.id and e.ticket_id is None
    store.record_spend("live", 0.5, model="glm", chat_session_id=b.id)
    store.record_spend("live", 2.0, model="glm", ticket_id=t.id)  # a ticket's: not chat
    clock.now += timedelta(days=1)
    store.record_spend("live", 1.0, model="opus", chat_session_id=a.id)
    assert store.chat_spend(a.id) == pytest.approx(1.25)
    assert store.chat_spend(b.id) == pytest.approx(0.5)
    assert store.chat_spend(999) == 0.0
    assert store.chat_spend_total(start) == pytest.approx(1.75)
    assert store.chat_spend_total(start, start + timedelta(hours=1)) == pytest.approx(0.75)
    assert store.spend_total("live", since=start) == pytest.approx(3.75)  # one live ceiling
    assert store.record_spend("live", 0.1).chat_session_id is None
    with pytest.raises(sqlite3.IntegrityError):
        store.record_spend("live", 0.1, chat_session_id=999)  # FK enforced


# --- repo settings ----------------------------------------------------------------------


def test_chat_network_is_closed_by_default(store: Store) -> None:
    assert store.repo_allows_chat_network(REPO) is False  # no row
    assert store.repo_allows_chat_network(None) is False
    store.set_cloud(REPO, False)  # a row from another setting
    assert store.repo_allows_chat_network(REPO) is False
    assert store.set_chat_network(REPO, True).chat_network is True
    assert store.repo_allows_chat_network(REPO) is True
    assert store.get_repo_settings(REPO).cloud is False  # other settings untouched
    store.set_chat_network(REPO, False)
    assert store.repo_allows_chat_network(REPO) is False
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), store.transaction() as conn:
        conn.execute("UPDATE repo_settings SET chat_network = 2")


# --- file locks -------------------------------------------------------------------------


def test_chat_locks_take_free_paths_and_report_held_ones(store: Store) -> None:
    t = _ticket(store)
    c, d = _session(store), _session(store)
    assert store.acquire_locks(t.id, REPO, ["src/a.py"]) == []
    held = store.insert_chat_locks(c.id, REPO, ["src/a.py", "src/b.py", "src/b.py"])
    assert [(x.path, x.holder) for x in held] == [("src/a.py", ("ticket", t.id))]
    assert [x.path for x in store.chat_lock_rows(c.id)] == ["src/b.py"]
    assert store.insert_chat_locks(c.id, REPO, ["src/b.py"]) == []  # re-insert is fine
    other = store.insert_chat_locks(d.id, REPO, ["src/b.py"])
    assert [x.holder for x in other] == [("chat", c.id)]
    assert store.insert_chat_locks(c.id, REPO, []) == []
    # one holder per path: the ticket row is untouched
    assert [x.ticket_id for x in store.list_locks(ticket_id=t.id)] == [t.id]
    assert {x.holder for x in store.list_locks(repo=REPO)} == {("ticket", t.id), ("chat", c.id)}


def test_ticket_acquire_conflicts_with_a_chat_lock(store: Store) -> None:
    """A chat-held row has ticket_id NULL; the ticket's conflict check must still see it."""
    t = _ticket(store)
    c = _session(store)
    store.insert_chat_locks(c.id, REPO, ["src/a.py"])
    conflicts = store.acquire_locks(t.id, REPO, ["src/a.py", "src/z.py"])
    assert [(x.path, x.chat_session_id, x.ticket_id) for x in conflicts] == [
        ("src/a.py", c.id, None)
    ]
    assert store.list_locks(ticket_id=t.id) == []  # all-or-nothing for tickets
    assert store.release_locks(t.id) == 0  # never drops a chat's row
    assert len(store.chat_lock_rows(c.id)) == 1


def test_delete_chat_locks(store: Store) -> None:
    t = _ticket(store)
    c = _session(store)
    store.acquire_locks(t.id, REPO, ["src/t.py"])
    store.insert_chat_locks(c.id, REPO, ["a", "b", "c"])
    assert store.delete_chat_locks(c.id, ["a", "src/t.py"]) == 1  # never another holder's
    assert store.delete_chat_locks(c.id, []) == 0
    assert store.delete_chat_locks(c.id) == 2
    assert store.chat_lock_rows(c.id) == []
    assert [x.path for x in store.list_locks()] == ["src/t.py"]


def test_lock_row_needs_exactly_one_holder(store: Store) -> None:
    t = _ticket(store)
    c = _session(store)
    sql = (
        "INSERT INTO file_locks (repo, path, ticket_id, chat_session_id, acquired_at)"
        " VALUES (?, ?, ?, ?, 'x')"
    )
    for tid, cid in ((t.id, c.id), (None, None)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"), store.transaction() as conn:
            conn.execute(sql, (REPO, "p", tid, cid))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), store.transaction() as conn:
        conn.execute(sql, (REPO, "p", None, 999))


# --- the migration on an existing database ---------------------------------------------


def test_migration_012_keeps_lock_rows_and_spend(tmp_path: Path) -> None:
    folder = tmp_path / "migrations"
    folder.mkdir()
    packaged = store_mod.default_migrations()
    for entry in packaged.iterdir():
        if entry.name.endswith(".sql") and entry.name < "012":
            (folder / entry.name).write_text(entry.read_text(encoding="utf-8"))
    db = tmp_path / "fman.db"
    with Store(db, migrations=folder) as s:
        assert s.schema_version() == 11
        t1, t2 = _ticket(s, 1), _ticket(s, 2)
        s.acquire_locks(t1.id, REPO, ["src/a.py", "src/b.py"])
        s.acquire_locks(t2.id, REPO, ["src/c.py"])
        s.record_spend("live", 0.5, ticket_id=t1.id)
        s.set_cloud(REPO, False)
        before = [tuple(r) for r in s.query("SELECT * FROM file_locks ORDER BY path")]
    (folder / "012_chat.sql").write_text(
        packaged.joinpath("012_chat.sql").read_text(encoding="utf-8")
    )
    with Store(db, migrations=folder, auto_migrate=False) as s:
        assert s.migrate() == [12]
        after = [tuple(r)[:4] for r in s.query("SELECT * FROM file_locks ORDER BY path")]
        assert after == before
        assert all(lk.chat_session_id is None for lk in s.list_locks())
        assert s.query("PRAGMA foreign_keys")[0][0] == 1
        assert s.query("PRAGMA foreign_key_check") == []
        assert s.get_repo_settings(REPO).chat_network is False
        assert s.query("SELECT chat_session_id FROM spend_ledger")[0][0] is None
        # existing ticket behaviour, unchanged
        assert [x.ticket_id for x in s.acquire_locks(t2.id, REPO, ["src/a.py"])] == [t1.id]
        assert s.release_locks(t1.id) == 2
        with pytest.raises(sqlite3.IntegrityError):
            s.acquire_locks(999, REPO, ["src/q.py"])  # ticket FK still enforced
    indexes = {
        r[0]
        for r in sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE tbl_name = 'file_locks' AND type = 'index'"
        )
    }
    assert {"file_locks_ticket", "file_locks_chat"} <= indexes
