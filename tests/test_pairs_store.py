"""Store support for ``ab:pair`` (#34, GH #16, migration 016)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foreman_inator.db import store as store_mod
from foreman_inator.db.store import NotFoundError, Store, StoreError

NOW = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "f.db", clock=lambda: NOW)
    yield s
    s.close()


def _ticket(s: Store, issue: int = 5, **kw: object) -> store_mod.Ticket:
    args: dict[str, object] = {
        "repo": "me/toy",
        "issue_number": issue,
        "title": "Fix it",
        "request": "Please fix it",
        "base_branch": "main",
    }
    args.update(kw)
    return s.create_ticket(**args)  # type: ignore[arg-type]


def _pair(s: Store, issue: int = 5) -> tuple[store_mod.Ticket, store_mod.Ticket, store_mod.Pair]:
    a = _ticket(s, issue)
    b = _ticket(s, issue, pair_side="b")
    p = s.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant="w@glm", opponent_reason="next_rung"
    )
    return s.get_ticket(a.id), b, p


def test_unpaired_ticket_has_no_side_and_one_open_per_issue_still_holds(store: Store) -> None:
    t = _ticket(store)
    assert t.pair_side is None
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(store)
    # an explicit side 'a' counts the same as NULL
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(store, pair_side="a")
    assert store.find_open_ticket("me/toy", 5) == t
    assert store.open_tickets_for_issue("me/toy", 5) == [t]
    assert store.pair_for_ticket(t.id) is None
    assert store.sibling(t.id) is None


def test_side_b_may_be_open_beside_side_a_but_only_one(store: Store) -> None:
    a = _ticket(store)
    b = _ticket(store, pair_side="b")
    assert b.pair_side == "b"
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(store, pair_side="b")
    assert store.find_open_ticket("me/toy", 5) == a
    assert [t.id for t in store.open_tickets_for_issue("me/toy", 5)] == [a.id, b.id]


def test_find_open_ticket_returns_b_when_it_is_the_only_open_one(store: Store) -> None:
    a, b, _ = _pair(store)
    store.set_column(a.id, "dropped")
    assert store.find_open_ticket("me/toy", 5) == b
    assert store.open_tickets_for_issue("me/toy", 5) == [b]


def test_bad_pair_side_is_rejected(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _ticket(store, pair_side="c")


def test_create_pair_links_siblings_and_marks_side_a(store: Store) -> None:
    a, b, p = _pair(store)
    assert a.pair_side == "a"
    assert (p.repo, p.issue_number, p.ticket_a, p.ticket_b) == ("me/toy", 5, a.id, b.id)
    assert (p.status, p.winner_ticket_id, p.loser_outcome, p.decided_at) == (
        "running",
        None,
        None,
        None,
    )
    assert (p.opponent_variant, p.opponent_reason, p.experiment_id) == ("w@glm", "next_rung", None)
    assert p.created_at == NOW
    assert store.pair_for_ticket(a.id) == p == store.pair_for_ticket(b.id)
    assert store.sibling(a.id) == b
    assert store.sibling(b.id) == a
    assert store.get_pair(p.id) == p
    with pytest.raises(NotFoundError):
        store.get_pair(99)


def test_create_pair_refuses_bad_links(store: Store) -> None:
    a = _ticket(store)
    other = _ticket(store, 6, pair_side="b")
    not_b = _ticket(store, 7)
    with pytest.raises(StoreError, match="different issues"):
        store.create_pair(
            ticket_a=a.id, ticket_b=other.id, opponent_variant="v", opponent_reason="challenger"
        )
    with pytest.raises(StoreError, match="not a side-b"):
        store.create_pair(
            ticket_a=not_b.id, ticket_b=not_b.id, opponent_variant="v", opponent_reason="next_rung"
        )
    b = _ticket(store, pair_side="b")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.create_pair(ticket_a=a.id, ticket_b=b.id, opponent_variant="v", opponent_reason="x")  # type: ignore[arg-type]
    # the failed insert rolled back the side-a mark too
    assert store.get_ticket(a.id).pair_side is None
    store.create_pair(
        ticket_a=a.id, ticket_b=b.id, opponent_variant="v", opponent_reason="next_rung"
    )
    store.set_column(b.id, "dropped")
    b2 = _ticket(store, pair_side="b")
    with pytest.raises(StoreError, match="already in a pair"):
        store.create_pair(
            ticket_a=a.id, ticket_b=b2.id, opponent_variant="v", opponent_reason="next_rung"
        )
    assert store.pair_for_ticket(b2.id) is None


def test_decide_pair_records_the_winner_once(store: Store) -> None:
    a, b, p = _pair(store)
    with pytest.raises(StoreError, match="not in pair"):
        store.decide_pair(p.id, winner_ticket_id=999, loser_outcome="rejected")
    d = store.decide_pair(p.id, winner_ticket_id=b.id, loser_outcome="merged_other")
    assert (d.status, d.winner_ticket_id, d.loser_outcome, d.decided_at) == (
        "decided",
        b.id,
        "merged_other",
        NOW,
    )
    assert store.decide_pair(p.id, winner_ticket_id=b.id, loser_outcome="merged_other") == d
    with pytest.raises(StoreError, match="already decided"):
        store.decide_pair(p.id, winner_ticket_id=a.id, loser_outcome="rejected")
    with pytest.raises(StoreError, match="decided"):
        store.drop_pair(p.id)


def test_drop_pair(store: Store) -> None:
    a, _, p = _pair(store)
    d = store.drop_pair(p.id)
    assert (d.status, d.winner_ticket_id, d.decided_at) == ("dropped", None, NOW)
    assert store.drop_pair(p.id) == d
    with pytest.raises(StoreError, match="dropped"):
        store.decide_pair(p.id, winner_ticket_id=a.id, loser_outcome="rejected")


def test_decided_pair_needs_winner_and_outcome_in_schema(store: Store) -> None:
    _, _, p = _pair(store)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), store.transaction() as conn:
        conn.execute("UPDATE pairs SET status = 'decided' WHERE id = ?", (p.id,))


def test_list_pairs_filters(store: Store) -> None:
    exp = store.create_experiment(
        role="writer", champion_variant="w@a", challenger_variant="w@b", mode="pair"
    )
    _, _, p1 = _pair(store, 5)
    a2 = _ticket(store, 6)
    b2 = _ticket(store, 6, pair_side="b")
    p2 = store.create_pair(
        ticket_a=a2.id,
        ticket_b=b2.id,
        opponent_variant="w@b",
        opponent_reason="challenger",
        experiment_id=exp.id,
    )
    store.drop_pair(p1.id)
    ids = lambda ps: [p.id for p in ps]  # noqa: E731
    assert ids(store.list_pairs()) == [p1.id, p2.id]
    assert ids(store.list_pairs(status="running")) == [p2.id]
    assert ids(store.list_pairs(status=("running", "dropped"))) == [p1.id, p2.id]
    assert ids(store.list_pairs(issue_number=5)) == [p1.id]
    assert ids(store.list_pairs(experiment_id=exp.id)) == [p2.id]
    assert ids(store.list_pairs(repo="other/repo")) == []


def test_migration_keeps_existing_open_tickets(tmp_path: Path) -> None:
    """A database at 015 with open tickets migrates; they read back unpaired."""
    folder = tmp_path / "migrations"
    folder.mkdir()
    for m in store_mod.load_migrations():
        if m.version <= 15:
            (folder / m.name).write_text(m.sql, encoding="utf-8")
    db = tmp_path / "old.db"
    with Store(db, migrations=folder, clock=lambda: NOW) as old:
        t = _ticket(old)
    with Store(db, clock=lambda: NOW) as new:
        assert new.schema_version() == 16
        assert new.get_ticket(t.id).pair_side is None
        with pytest.raises(sqlite3.IntegrityError):
            _ticket(new)
        b = _ticket(new, pair_side="b")
        assert new.open_tickets_for_issue("me/toy", 5)[1] == b
