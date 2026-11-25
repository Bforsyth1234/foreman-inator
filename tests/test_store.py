"""Tests for the SQLite store (todo #3)."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foreman_inator.db import store as store_mod
from foreman_inator.db.store import NotFoundError, Store, StoreError, load_migrations

# Copied from SPEC.md "Data model" (2026-11-23). Change only when SPEC changes.
SPEC_COLUMNS: dict[str, list[str]] = {
    "tickets": [
        "id",
        "title",
        "request",
        "repo",
        "base_branch",
        "issue_number",
        "pr_number",
        "owned_files",
        "column",
        "source",
        "route_reason",
        "trace_id",
        "accepted_attempt_id",
        "created_at",
        "done_at",
    ],
    "attempts": [
        "id",
        "ticket_id",
        "model",
        "tier",
        "variant_id",
        "trigger",
        "worktree",
        "branch",
        "tests_passed",
        "build_passed",
        "diff_files",
        "diff_lines",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "seconds",
        "outcome",
    ],
    "kickbacks": ["id", "attempt_id", "by", "tag", "note", "failure_step", "created_at"],
    "reviews": [
        "id",
        "attempt_id",
        "reviewer_model",
        "variant_id",
        "verdict",
        "tests_passed",
        "changed_line_coverage",
        "tests_nonvacuous",
        "ran_app",
        "tag",
        "note",
        "tokens_in",
        "tokens_out",
        "cost_usd",
    ],
    "experiments": [
        "id",
        "role",
        "champion_variant",
        "challenger_variant",
        "mode",
        "traffic_share",
        "status",
        "started_at",
        "ended_at",
        "result_note",
    ],
}

# Item #3 decision 1, columns as written there.
ITEM_COLUMNS: dict[str, list[str]] = {
    "variants": ["id", "role", "model", "prompt_name", "prompt_hash"],
    "repo_settings": ["repo", "verify_command"],
}

# Beyond SPEC and the item: every addition is listed here so a change is deliberate.
EXTRA_COLUMNS: dict[str, list[str]] = {
    "tickets": ["routed_tier", "flag_reason", "flagged_at"],
    "attempts": [
        "started_at",
        "ended_at",
        "base_commit",
        "verify_output_path",
        # migration 002 (#41)
        "ci_passed",
        "ci_status",
        # migration 007 (#58)
        "end_reason",
        "end_detail",
        # migration 008 (#51)
        "writer_mode",
        "review_mode",
    ],
    "kickbacks": ["failure_step_index"],
    "reviews": ["created_at", "shadow"],
    "variants": ["created_at"],
    "repo_settings": [
        "verify_prompted_at",
        "updated_at",
        # migration 006 (#45)
        "install_command",
        # migration 008 (#51)
        "writer_mode",
        "review_mode",
        # migration 010 (#49)
        "cloud",
    ],
    "champion_history": ["id", "role", "variant_id", "set_at", "note"],
    "poll_cursors": ["repo", "name", "value", "updated_at"],
    "spend_ledger": [
        "id",
        "bucket",
        "cost_usd",
        "model",
        "ticket_id",
        "attempt_id",
        "experiment_id",
        "created_at",
    ],
    "spend_approvals": ["id", "ticket_id", "approved_at", "consumed_at"],
    "file_locks": ["repo", "path", "ticket_id", "acquired_at"],
    "schema_migrations": ["version", "name", "applied_at"],
    # migration 004 (#23)
    "ticket_embeddings": ["ticket_id", "model", "dims", "vector", "text_sha256", "created_at"],
    # migration 005 (#25)
    "replay_sets": [
        "id",
        "role",
        "built_at",
        "max_merges",
        "case_count",
        "skipped_count",
        "cutoff_at",  # migration 011 (#69)
    ],  # fmt: skip
    "replay_cases": [
        "id",
        "set_id",
        "role",
        "ticket_id",
        "attempt_id",
        "repo",
        "status",
        "skip_reason",
        "data",
        "created_at",
    ],
    "replay_runs": [
        "experiment_id",
        "set_id",
        "role",
        "state",
        "stop_reason",
        "started_at",
        "updated_at",
    ],
    "replay_results": [
        "id",
        "experiment_id",
        "case_id",
        "side",
        "variant_id",
        "status",
        "verdict",
        "score",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "seconds",
        "detail",
        "created_at",
    ],
    # migration 009 (#24)
    "post_merge_signals": [
        "id",
        "ticket_id",
        "attempt_id",
        "kind",
        "at",
        "detail",
        "recorded_at",
    ],
    # migration 011 (#69)
    "experiments": ["origin", "cutoff_at"],
    "prompt_drafts": [
        "id",
        "role",
        "prompt_name",
        "prompt_hash",
        "prompt_text",
        "rationale",
        "patterns",
        "base_variant",
        "model",
        "proposer_model",
        "cutoff_at",
        "path",
        "est_cost_usd",
        "cost_usd",
        "status",
        "created_at",
        "decided_at",
        "experiment_id",
    ],  # fmt: skip
}

# Enum values copied from SPEC.md "Data model".
SPEC_ENUMS: dict[tuple[str, str], list[str]] = {
    ("tickets", "column"): ["todo", "in_progress", "testing", "done", "dropped"],
    ("tickets", "source"): ["issue", "botinator", "ci"],
    # "rebase": migration 003 (#43); SPEC's attempts.trigger gains it at wave close.
    ("attempts", "trigger"): [
        "first",
        "escalation",
        "kickback",
        "ai_kickback",
        "explore",
        "rebase",
    ],
    ("attempts", "outcome"): ["accepted", "kicked_back", "failed_checks", "superseded"],
    ("kickbacks", "by"): ["user", "reviewer"],
    ("kickbacks", "tag"): ["wrong", "incomplete", "broke", "style"],
    ("kickbacks", "failure_step"): [
        "misread_issue",
        "missed_file",
        "wrong_edit",
        "ignored_failure",
        "weak_tests",
        "reviewer_miss",
    ],
    ("reviews", "verdict"): ["approve", "request_changes", "handed_to_user"],
    ("reviews", "ran_app"): ["yes", "no", "not_runnable"],
    ("reviews", "tag"): ["wrong", "incomplete", "broke", "style"],
    ("experiments", "role"): ["writer", "reviewer"],
    ("experiments", "mode"): ["replay", "seeded", "shadow", "split", "pair"],
    ("experiments", "status"): ["running", "promoted", "rejected"],
    ("variants", "role"): ["writer", "reviewer"],
    ("champion_history", "role"): ["writer", "reviewer"],
    ("spend_ledger", "bucket"): ["live", "experiments"],
}


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "foreman-inator.db"


@pytest.fixture
def store(db_path: Path, clock: FakeClock) -> Store:
    s = Store(db_path, clock=clock)
    yield s
    s.close()


def _ticket(store: Store, issue: int = 1, **kw: object):
    args: dict[str, object] = {
        "repo": "me/toy",
        "issue_number": issue,
        "title": "Fix it",
        "request": "Please fix it",
        "base_branch": "main",
    }
    args.update(kw)
    return store.create_ticket(**args)  # type: ignore[arg-type]


def _columns(path: Path) -> dict[str, list[str]]:
    conn = sqlite3.connect(path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')] for t in tables}
    finally:
        conn.close()


def _schema_dump(path: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


# --- schema and migrations -------------------------------------------------------------


def test_fresh_db_has_every_spec_table_and_column(store: Store, db_path: Path) -> None:
    actual = _columns(db_path)
    expected: dict[str, set[str]] = {}
    for source in (SPEC_COLUMNS, ITEM_COLUMNS, EXTRA_COLUMNS):
        for table, cols in source.items():
            expected.setdefault(table, set()).update(cols)
    for table, cols in {**SPEC_COLUMNS, **ITEM_COLUMNS}.items():
        assert table in actual, f"missing table {table}"
        missing = set(cols) - set(actual[table])
        assert not missing, f"{table} missing SPEC columns {missing}"
    assert {t: set(c) for t, c in actual.items()} == expected


def test_migrating_twice_is_a_noop(store: Store, db_path: Path) -> None:
    before = _schema_dump(db_path)
    packaged = load_migrations()
    assert store.schema_version() == packaged[-1].version
    assert store.migrate() == []
    store.close()
    with Store(db_path) as again:
        assert again.migrate() == []
        rows = again.query("SELECT version, name FROM schema_migrations")
        assert [(r["version"], r["name"]) for r in rows] == [(m.version, m.name) for m in packaged]
    assert _schema_dump(db_path) == before


def test_first_open_applies_migration_one(db_path: Path) -> None:
    s = Store(db_path, auto_migrate=False)
    assert s.schema_version() == 0
    assert s.migrate() == [m.version for m in load_migrations()]  # 1, then 002_ci, ...
    assert s.migrate() == []
    s.close()


def test_packaged_migrations_are_found() -> None:
    migrations = load_migrations()
    assert migrations[0].version == 1
    assert migrations[0].name == "001_initial.sql"
    assert "CREATE TABLE tickets" in migrations[0].sql
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)


def _copy_migrations(tmp_path: Path) -> Path:
    folder = tmp_path / "migrations"
    folder.mkdir()
    src = store_mod.default_migrations().joinpath("001_initial.sql")
    (folder / "001_initial.sql").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return folder


def test_later_migration_applies_on_top(tmp_path: Path, db_path: Path) -> None:
    folder = _copy_migrations(tmp_path)
    Store(db_path, migrations=folder).close()
    (folder / "002_ticket_notes.sql").write_text(
        "CREATE TABLE ticket_notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL) STRICT;\n"
        "ALTER TABLE tickets ADD COLUMN priority INTEGER;\n",
        encoding="utf-8",
    )
    with Store(db_path, migrations=folder, auto_migrate=False) as s:
        assert s.migrate() == [2]
        assert s.schema_version() == 2
        assert s.migrate() == []
    cols = _columns(db_path)
    assert "ticket_notes" in cols
    assert "priority" in cols["tickets"]


def test_failed_migration_rolls_back(tmp_path: Path, db_path: Path) -> None:
    folder = _copy_migrations(tmp_path)
    Store(db_path, migrations=folder).close()
    (folder / "002_broken.sql").write_text(
        "CREATE TABLE half_done (id INTEGER PRIMARY KEY) STRICT;\nTHIS IS NOT SQL;\n",
        encoding="utf-8",
    )
    s = Store(db_path, migrations=folder, auto_migrate=False)
    with pytest.raises(sqlite3.Error):
        s.migrate()
    assert s.schema_version() == 1
    assert "half_done" not in _columns(db_path)
    # The store is still usable after the failed migration.
    assert _ticket(s).id == 1
    s.close()


def test_db_newer_than_code_is_refused(tmp_path: Path, db_path: Path) -> None:
    Store(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO schema_migrations VALUES (99, '099_future.sql', 'x')")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="does not know"):
        Store(db_path)


def test_bad_migration_file_name_is_refused(tmp_path: Path) -> None:
    folder = _copy_migrations(tmp_path)
    (folder / "2_oops.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(StoreError, match="bad migration file name"):
        load_migrations(folder)


def test_wal_mode_and_foreign_keys(store: Store) -> None:
    assert store.query("PRAGMA journal_mode")[0][0] == "wal"
    assert store.query("PRAGMA foreign_keys")[0][0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.add_attempt(999, model="m", tier=1, trigger="first")


# --- enums -----------------------------------------------------------------------------


def test_python_enum_constants_match_spec() -> None:
    assert list(store_mod.COLUMNS) == SPEC_ENUMS[("tickets", "column")]
    assert list(store_mod.SOURCES) == SPEC_ENUMS[("tickets", "source")]
    assert list(store_mod.TRIGGERS) == SPEC_ENUMS[("attempts", "trigger")]
    assert list(store_mod.OUTCOMES) == SPEC_ENUMS[("attempts", "outcome")]
    assert list(store_mod.KICKBACK_BY) == SPEC_ENUMS[("kickbacks", "by")]
    assert list(store_mod.TAGS) == SPEC_ENUMS[("kickbacks", "tag")]
    assert list(store_mod.FAILURE_STEPS) == SPEC_ENUMS[("kickbacks", "failure_step")]
    assert list(store_mod.VERDICTS) == SPEC_ENUMS[("reviews", "verdict")]
    assert list(store_mod.RAN_APP) == SPEC_ENUMS[("reviews", "ran_app")]
    assert list(store_mod.ROLES) == SPEC_ENUMS[("experiments", "role")]
    assert list(store_mod.EXPERIMENT_MODES) == SPEC_ENUMS[("experiments", "mode")]
    assert list(store_mod.EXPERIMENT_STATUSES) == SPEC_ENUMS[("experiments", "status")]
    assert list(store_mod.SPEND_BUCKETS) == SPEC_ENUMS[("spend_ledger", "bucket")]


def test_invalid_ticket_column_doing_raises(store: Store) -> None:
    ticket = _ticket(store)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.set_column(ticket.id, "doing")  # type: ignore[arg-type]
    assert store.get_ticket(ticket.id).column == "todo"


def _enum_writer(store: Store, table: str, column: str) -> Callable[[str], None]:
    """Return a function that writes ``value`` into ``table.column`` through the store."""
    ticket = _ticket(store)
    attempt = store.add_attempt(ticket.id, model="m", tier=1, trigger="first")
    if table == "tickets" and column == "column":
        return lambda v: store.set_column(ticket.id, v)  # type: ignore[arg-type]
    if table == "tickets" and column == "source":
        counter = iter(range(100, 1000))
        return lambda v: _ticket(store, next(counter), source=v)  # type: ignore[arg-type]
    if table == "attempts" and column == "trigger":
        return lambda v: store.add_attempt(ticket.id, model="m", tier=1, trigger=v)  # type: ignore[arg-type]
    if table == "attempts" and column == "outcome":
        return lambda v: store.set_attempt_outcome(attempt.id, v)  # type: ignore[arg-type]
    if table == "kickbacks":
        if column == "by":
            return lambda v: store.record_kickback(attempt.id, by=v)  # type: ignore[arg-type]
        return lambda v: store.record_kickback(attempt.id, by="user", **{column: v})
    if table == "reviews":
        base = {"reviewer_model": "r", "verdict": "approve", "shadow": True}
        return lambda v: store.record_review(attempt.id, **{**base, column: v})  # type: ignore[arg-type]
    if table == "experiments":
        base = {"role": "writer", "champion_variant": "a", "challenger_variant": "b"}
        base["mode"] = "replay"
        if column == "status":

            def write_status(v: str) -> None:
                exp = store.create_experiment(**base)  # type: ignore[arg-type]
                store._update("experiments", "id", exp.id, {"status": v}, frozenset({"status"}))

            return write_status
        return lambda v: store.create_experiment(**{**base, column: v})  # type: ignore[arg-type]
    if table == "variants":
        counter = iter(range(1000))
        return lambda v: store.upsert_variant(
            f"m/p@{next(counter)}",
            role=v,  # type: ignore[arg-type]
            model="m",
            prompt_name="p",
            prompt_hash="h",
        )
    if table == "champion_history":
        store.upsert_variant("m/p@w", role="writer", model="m", prompt_name="p", prompt_hash="h")

        def write_role(v: str) -> None:
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO champion_history (role, variant_id, set_at) VALUES (?, ?, ?)",
                    (v, "m/p@w", "2026-11-23T00:00:00.000000+00:00"),
                )

        return write_role
    if table == "spend_ledger":
        return lambda v: store.record_spend(v, 1.0)  # type: ignore[arg-type]
    raise AssertionError(f"no writer for {table}.{column}")


@pytest.mark.parametrize(("table", "column"), list(SPEC_ENUMS), ids=lambda x: str(x))
def test_every_enum_accepts_spec_values_and_rejects_others(
    store: Store, table: str, column: str
) -> None:
    write = _enum_writer(store, table, column)
    for value in SPEC_ENUMS[(table, column)]:
        write(value)
    for bad in ("doing", "", "TODO", "Approve"):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            write(bad)


# --- tickets ---------------------------------------------------------------------------


def test_ticket_round_trip_and_columns(store: Store, clock: FakeClock) -> None:
    t = _ticket(store, owned_files=["src/a.py", "tests/test_a.py"], routed_tier=2)
    assert t.id == 1
    assert t.column == "todo"
    assert t.owned_files == ("src/a.py", "tests/test_a.py")
    assert t.created_at == clock.now
    assert t.done_at is None

    t = store.update_ticket(t.id, pr_number=7, route_reason="rules: small", trace_id="abc")
    assert (t.pr_number, t.route_reason, t.trace_id) == (7, "rules: small", "abc")
    with pytest.raises(TypeError):
        store.update_ticket(t.id, column="done")

    clock.advance(hours=1)
    t = store.set_column(t.id, "done")
    assert t.done_at == clock.now
    assert store.list_tickets(column="done") == [t]
    assert store.list_tickets(column=["todo", "in_progress"]) == []
    with pytest.raises(NotFoundError):
        store.get_ticket(42)


def test_ticket_ids_are_never_reused(store: Store, db_path: Path) -> None:
    first = _ticket(store, 1)
    second = _ticket(store, 2)
    assert (first.id, second.id) == (1, 2)
    with store.transaction() as conn:
        conn.execute("DELETE FROM tickets WHERE id = ?", (second.id,))
    assert _ticket(store, 3).id == 3


def test_one_open_ticket_per_issue(store: Store) -> None:
    t = _ticket(store, 5)
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(store, 5)
    assert store.find_open_ticket("me/toy", 5) == t
    store.set_column(t.id, "dropped")
    assert store.find_open_ticket("me/toy", 5) is None
    again = _ticket(store, 5)
    assert [x.id for x in store.tickets_for_issue("me/toy", 5)] == [t.id, again.id]


def test_owned_files_must_be_a_list(store: Store) -> None:
    with pytest.raises(TypeError):
        _ticket(store, owned_files="src/a.py")


def test_flag_and_clear(store: Store) -> None:
    t = store.flag_ticket(_ticket(store).id, "tier 3 failed")
    assert t.flag_reason == "tier 3 failed"
    assert t.flagged_at is not None
    t = store.clear_flag(t.id)
    assert t.flag_reason is None and t.flagged_at is None


# --- attempts, kickbacks, reviews ------------------------------------------------------


def test_attempt_lifecycle(store: Store, clock: FakeClock) -> None:
    t = _ticket(store)
    a = store.add_attempt(
        t.id, model="qwen3.6-35b-a3b", tier=1, trigger="first", branch="fman/issue-1/a1"
    )
    assert a.outcome is None and a.tests_passed is None and a.cost_usd == 0
    store.add_attempt_usage(a.id, tokens_in=100, tokens_out=20, cost_usd=0.5)
    store.add_attempt_usage(a.id, tokens_in=50, tokens_out=5, cost_usd=0.25)
    clock.advance(seconds=90)
    a = store.finish_attempt(
        a.id, "failed_checks", tests_passed=False, build_passed=True, diff_files=2, diff_lines=10
    )
    assert (a.tokens_in, a.tokens_out, a.cost_usd) == (150, 25, 0.75)
    assert a.tests_passed is False and a.build_passed is True
    assert a.seconds == 90
    assert a.ended_at == clock.now
    with pytest.raises(sqlite3.IntegrityError):
        store.add_attempt(t.id, model="m", tier=4, trigger="first")
    with pytest.raises(TypeError):
        store.update_attempt(a.id, ticket_id=2)

    b = store.add_attempt(t.id, model="qwen3.8-27b", tier=2, trigger="escalation")
    store.finish_attempt(b.id, tests_passed=True)
    t = store.set_accepted_attempt(t.id, b.id)
    assert t.accepted_attempt_id == b.id
    assert store.get_attempt(b.id).outcome == "accepted"
    assert [x.id for x in store.list_attempts(t.id)] == [a.id, b.id]

    other = _ticket(store, 2)
    with pytest.raises(StoreError):
        store.set_accepted_attempt(other.id, a.id)


def test_kickbacks(store: Store) -> None:
    t = _ticket(store)
    a = store.add_attempt(t.id, model="m", tier=1, trigger="first")
    k = store.record_kickback(a.id, by="user", tag="incomplete", note="missed the edge case")
    assert (k.by, k.tag, k.failure_step) == ("user", "incomplete", None)
    k = store.set_failure_step(k.id, "missed_file", 3)
    assert (k.failure_step, k.failure_step_index) == ("missed_file", 3)
    untagged = store.record_kickback(a.id, by="reviewer")
    assert untagged.tag is None
    assert store.list_kickbacks(ticket_id=t.id) == [k, untagged]
    assert store.list_kickbacks(attempt_id=a.id) == [k, untagged]


def test_reviews_one_live_review_per_attempt(store: Store) -> None:
    a = store.add_attempt(_ticket(store).id, model="m", tier=1, trigger="first")
    r = store.record_review(
        a.id,
        reviewer_model="glm-4.6",
        verdict="request_changes",
        tests_passed=True,
        changed_line_coverage=82.5,
        tests_nonvacuous=False,
        ran_app="not_runnable",
        tag="broke",
        note="new test passes without the change",
        cost_usd=0.1,
    )
    assert r.tests_nonvacuous is False and r.shadow is False
    with pytest.raises(sqlite3.IntegrityError):
        store.record_review(a.id, reviewer_model="glm-4.6", verdict="approve")
    shadow = store.record_review(a.id, reviewer_model="claude", verdict="approve", shadow=True)
    assert store.list_reviews(attempt_id=a.id) == [r, shadow]
    assert store.list_reviews(attempt_id=a.id, shadow=False) == [r]
    with pytest.raises(sqlite3.IntegrityError):
        store.record_review(
            a.id, reviewer_model="x", verdict="approve", changed_line_coverage=120, shadow=True
        )


# --- experiments, variants, champions --------------------------------------------------


def test_experiments(store: Store) -> None:
    e = store.create_experiment(
        role="writer",
        champion_variant="a",
        challenger_variant="b",
        mode="split",
        traffic_share=0.15,
    )
    assert e.status == "running" and e.ended_at is None
    e = store.end_experiment(e.id, "promoted", "won replay and split")
    assert e.status == "promoted" and e.ended_at is not None
    assert store.list_experiments(status="running") == []
    with pytest.raises(sqlite3.IntegrityError):
        store.create_experiment(
            role="writer",
            champion_variant="a",
            challenger_variant="b",
            mode="split",
            traffic_share=1.5,
        )


def test_variants_and_champion_history(store: Store) -> None:
    v1 = store.upsert_variant(
        "qwen/writer-v1@aaaa1111",
        role="writer",
        model="qwen",
        prompt_name="writer-v1",
        prompt_hash="aaaa1111",
    )
    assert (
        store.upsert_variant(
            v1.id, role="writer", model="qwen", prompt_name="writer-v1", prompt_hash="aaaa1111"
        )
        == v1
    )
    with pytest.raises(StoreError):
        store.upsert_variant(
            v1.id, role="writer", model="qwen", prompt_name="writer-v1", prompt_hash="other"
        )
    v2 = store.upsert_variant(
        "qwen/writer-v2@bbbb2222",
        role="writer",
        model="qwen",
        prompt_name="writer-v2",
        prompt_hash="bbbb2222",
    )
    assert store.champion("writer") is None
    store.set_champion("writer", v1.id)
    store.set_champion("writer", v2.id, note="promoted")
    assert store.champion("writer") == v2.id
    assert [c.variant_id for c in store.champion_history("writer")] == [v1.id, v2.id]
    assert store.champion("reviewer") is None
    with pytest.raises(StoreError):
        store.set_champion("reviewer", v1.id)
    with pytest.raises(NotFoundError):
        store.set_champion("writer", "nope")


# --- repo settings, cursors ------------------------------------------------------------


def test_repo_settings(store: Store) -> None:
    assert store.get_repo_settings("me/toy") is None
    assert store.mark_verify_prompted("me/toy") is True
    assert store.mark_verify_prompted("me/toy") is False
    s = store.set_verify_command("me/toy", "make check")
    assert s.verify_command == "make check" and s.verify_prompted_at is not None
    assert s.install_command is None


def test_repo_install_command(store: Store) -> None:
    """#45: saved like the verify command; None clears it; the two do not touch."""
    s = store.set_install_command("me/toy", "uv sync --frozen")
    assert s.install_command == "uv sync --frozen" and s.verify_command is None
    store.set_verify_command("me/toy", "make check")
    s = store.get_repo_settings("me/toy")
    assert s.install_command == "uv sync --frozen" and s.verify_command == "make check"
    assert store.set_install_command("me/toy", None).install_command is None
    assert store.get_repo_settings("me/toy").verify_command == "make check"


def test_poll_cursors(store: Store) -> None:
    assert store.get_cursor("me/toy") is None
    store.set_cursor("me/toy", "2026-11-23T09:00:00Z")
    store.set_cursor("me/toy", "2026-11-23T09:01:00Z")
    store.set_cursor("me/toy", "evt-9", name="events")
    assert store.get_cursor("me/toy") == "2026-11-23T09:01:00Z"
    assert store.get_cursor("me/toy", name="events") == "evt-9"
    assert store.get_cursor("me/other") is None


# --- spend -----------------------------------------------------------------------------


def test_spend_ledger_windows(store: Store, clock: FakeClock) -> None:
    day = datetime(2026, 11, 23, tzinfo=UTC)
    store.record_spend("live", 1.25, model="claude")
    store.record_spend("experiments", 4.0)
    clock.advance(days=1)
    store.record_spend("live", 2.0)
    assert store.spend_total("live", since=day, until=day + timedelta(days=1)) == 1.25
    assert store.spend_total("live", since=day + timedelta(days=1)) == 2.0
    assert store.spend_total("live", since=day) == 3.25
    assert store.spend_total("experiments", since=day) == 4.0
    with pytest.raises(sqlite3.IntegrityError):
        store.record_spend("live", -1)


def test_spend_approvals(store: Store) -> None:
    t = _ticket(store)
    assert store.open_spend_approval(t.id) is None
    approval = store.approve_spend(t.id)
    assert store.open_spend_approval(t.id) == approval
    assert store.consume_spend_approval(approval.id) is True
    assert store.consume_spend_approval(approval.id) is False
    assert store.open_spend_approval(t.id) is None


# --- locks -----------------------------------------------------------------------------


def test_file_locks_all_or_nothing(store: Store) -> None:
    a, b = _ticket(store, 1), _ticket(store, 2)
    assert store.acquire_locks(a.id, "me/toy", ["src/x.py", "src/y.py"]) == []
    assert store.acquire_locks(a.id, "me/toy", ["src/x.py"]) == []  # re-acquire is fine
    conflicts = store.acquire_locks(b.id, "me/toy", ["src/y.py", "src/z.py"])
    assert [(c.path, c.ticket_id) for c in conflicts] == [("src/y.py", a.id)]
    assert store.list_locks(ticket_id=b.id) == []  # nothing taken on conflict
    assert store.acquire_locks(b.id, "me/other", ["src/y.py"]) == []  # other repo
    assert store.release_locks(a.id) == 2
    assert store.acquire_locks(b.id, "me/toy", ["src/y.py", "src/z.py"]) == []
    assert [x.path for x in store.list_locks(repo="me/toy")] == ["src/y.py", "src/z.py"]


# --- concurrency -----------------------------------------------------------------------


def test_threads_share_one_store(store: Store) -> None:
    t = _ticket(store)
    a = store.add_attempt(t.id, model="m", tier=1, trigger="first")
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(50):
                store.add_attempt_usage(a.id, tokens_in=1, cost_usd=0.01)
                store.record_spend("live", 0.01)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert store.get_attempt(a.id).tokens_in == 200
    assert len(store.query("SELECT id FROM spend_ledger")) == 200


def test_read_only_store_reads_beside_writer(store: Store, db_path: Path) -> None:
    _ticket(store)
    with Store(db_path, read_only=True) as reader:
        assert [t.id for t in reader.list_tickets()] == [1]
        _ticket(store, 2)  # writer keeps going while the reader is open
        assert [t.id for t in reader.list_tickets()] == [1, 2]
        with pytest.raises(sqlite3.OperationalError):
            reader.create_ticket(
                repo="me/toy", issue_number=3, title="t", request="r", base_branch="main"
            )
        with pytest.raises(StoreError):
            reader.migrate()


def test_transaction_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError), store.transaction():
        _ticket(store, 1)
        raise RuntimeError("boom")
    assert store.list_tickets() == []


def test_copy_of_db_opens_with_new_store(store: Store, db_path: Path, tmp_path: Path) -> None:
    _ticket(store)
    store.close()
    copy = tmp_path / "copy.db"
    shutil.copy(db_path, copy)
    with Store(copy) as s:
        assert s.migrate() == []
        assert s.get_ticket(1).title == "Fix it"
