"""SQLite store: numbered migrations plus typed methods per table.

The schema lives in ``migrations/NNN_<name>.sql`` (loaded with ``importlib.resources``
so it works from an installed wheel). Each file is applied once, inside one
``BEGIN IMMEDIATE`` transaction together with its ``schema_migrations`` row. Later
items add ``002_<name>.sql`` and so on; they never edit an applied file.

One ``Store`` holds one connection guarded by a re-entrant lock, so the worker's
threads can share it. A metrics page in another process opens its own
``Store(path, read_only=True)``. WAL mode lets that reader run beside the writer.

Enum values are enforced by CHECK constraints in the schema, not in Python: an
invalid value raises ``sqlite3.IntegrityError``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

# --- Enum values, copied from SPEC.md "Data model" ---------------------------------

Column = Literal["todo", "in_progress", "testing", "done", "dropped"]
Source = Literal["issue", "botinator", "ci"]
Trigger = Literal["first", "escalation", "kickback", "ai_kickback", "explore"]
Outcome = Literal["accepted", "kicked_back", "failed_checks", "superseded"]
KickbackBy = Literal["user", "reviewer"]
Tag = Literal["wrong", "incomplete", "broke", "style"]
FailureStep = Literal[
    "misread_issue", "missed_file", "wrong_edit", "ignored_failure", "weak_tests", "reviewer_miss"
]
Verdict = Literal["approve", "request_changes", "handed_to_user"]
RanApp = Literal["yes", "no", "not_runnable"]
Role = Literal["writer", "reviewer"]
ExperimentMode = Literal["replay", "seeded", "shadow", "split", "pair"]
ExperimentStatus = Literal["running", "promoted", "rejected"]
SpendBucket = Literal["live", "experiments"]

COLUMNS: tuple[str, ...] = ("todo", "in_progress", "testing", "done", "dropped")
OPEN_COLUMNS: tuple[str, ...] = ("todo", "in_progress", "testing")
SOURCES: tuple[str, ...] = ("issue", "botinator", "ci")
TRIGGERS: tuple[str, ...] = ("first", "escalation", "kickback", "ai_kickback", "explore")
OUTCOMES: tuple[str, ...] = ("accepted", "kicked_back", "failed_checks", "superseded")
KICKBACK_BY: tuple[str, ...] = ("user", "reviewer")
TAGS: tuple[str, ...] = ("wrong", "incomplete", "broke", "style")
FAILURE_STEPS: tuple[str, ...] = (
    "misread_issue",
    "missed_file",
    "wrong_edit",
    "ignored_failure",
    "weak_tests",
    "reviewer_miss",
)
VERDICTS: tuple[str, ...] = ("approve", "request_changes", "handed_to_user")
RAN_APP: tuple[str, ...] = ("yes", "no", "not_runnable")
ROLES: tuple[str, ...] = ("writer", "reviewer")
EXPERIMENT_MODES: tuple[str, ...] = ("replay", "seeded", "shadow", "split", "pair")
EXPERIMENT_STATUSES: tuple[str, ...] = ("running", "promoted", "rejected")
SPEND_BUCKETS: tuple[str, ...] = ("live", "experiments")

DEFAULT_CURSOR = "default"


class StoreError(Exception):
    """A store-level error that is not a constraint violation."""


class NotFoundError(StoreError, LookupError):
    """The requested row does not exist."""


# --- Row dataclasses ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ticket:
    id: int
    title: str
    request: str
    repo: str
    base_branch: str
    issue_number: int
    pr_number: int | None
    owned_files: tuple[str, ...]
    column: Column
    source: Source
    route_reason: str | None
    trace_id: str | None
    accepted_attempt_id: int | None
    created_at: datetime
    done_at: datetime | None
    routed_tier: int | None
    flag_reason: str | None
    flagged_at: datetime | None


@dataclass(frozen=True, slots=True)
class Attempt:
    id: int
    ticket_id: int
    model: str
    tier: int
    variant_id: str | None
    trigger: Trigger
    worktree: str | None
    branch: str | None
    tests_passed: bool | None
    build_passed: bool | None
    diff_files: int | None
    diff_lines: int | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    seconds: float | None
    outcome: Outcome | None
    started_at: datetime
    ended_at: datetime | None
    base_commit: str | None
    verify_output_path: str | None


@dataclass(frozen=True, slots=True)
class Kickback:
    id: int
    attempt_id: int
    by: KickbackBy
    tag: Tag | None
    note: str | None
    failure_step: FailureStep | None
    created_at: datetime
    failure_step_index: int | None


@dataclass(frozen=True, slots=True)
class Review:
    id: int
    attempt_id: int
    reviewer_model: str
    variant_id: str | None
    verdict: Verdict
    tests_passed: bool | None
    changed_line_coverage: float | None
    tests_nonvacuous: bool | None
    ran_app: RanApp | None
    tag: Tag | None
    note: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    created_at: datetime
    shadow: bool


@dataclass(frozen=True, slots=True)
class Experiment:
    id: int
    role: Role
    champion_variant: str
    challenger_variant: str
    mode: ExperimentMode
    traffic_share: float | None
    status: ExperimentStatus
    started_at: datetime
    ended_at: datetime | None
    result_note: str | None


@dataclass(frozen=True, slots=True)
class Variant:
    id: str
    role: Role
    model: str
    prompt_name: str
    prompt_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChampionChange:
    id: int
    role: Role
    variant_id: str
    set_at: datetime
    note: str | None


@dataclass(frozen=True, slots=True)
class RepoSettings:
    repo: str
    verify_command: str | None
    verify_prompted_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SpendEntry:
    id: int
    bucket: SpendBucket
    cost_usd: float
    model: str | None
    ticket_id: int | None
    attempt_id: int | None
    experiment_id: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SpendApproval:
    id: int
    ticket_id: int
    approved_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class FileLock:
    repo: str
    path: str
    ticket_id: int
    acquired_at: datetime


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


# --- Conversions ----------------------------------------------------------------------

_BOOL_FIELDS = frozenset({"tests_passed", "build_passed", "tests_nonvacuous", "shadow"})
_JSON_TUPLE_FIELDS = frozenset({"owned_files"})


def _ts(value: datetime) -> str:
    """Datetime → stored text. Naive values are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _to_db(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _JSON_TUPLE_FIELDS:
        if isinstance(value, str):
            raise TypeError(f"{name} must be a sequence of paths, not a string")
        return json.dumps(list(value))
    if isinstance(value, datetime):
        return _ts(value)
    if isinstance(value, bool):
        return int(value)
    return value


def _from_db(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _BOOL_FIELDS:
        return bool(value)
    if name in _JSON_TUPLE_FIELDS:
        return tuple(json.loads(value))
    if name.endswith("_at"):
        return datetime.fromisoformat(value)
    return value


def _row_to[T](cls: type[T], row: sqlite3.Row) -> T:
    return cls(**{f.name: _from_db(f.name, row[f.name]) for f in fields(cls)})  # type: ignore[arg-type]


# --- Migrations -----------------------------------------------------------------------

_MIGRATION_FILE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


def default_migrations() -> Traversable:
    """The packaged migrations folder."""
    return resources.files("foreman_inator.db").joinpath("migrations")


def load_migrations(source: Traversable | Path | None = None) -> list[Migration]:
    """Read ``NNN_<name>.sql`` files from ``source`` (default: the packaged folder)."""
    folder = default_migrations() if source is None else source
    found: dict[int, Migration] = {}
    for entry in folder.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _MIGRATION_FILE.match(entry.name)
        if match is None:
            raise StoreError(f"bad migration file name {entry.name!r}: want NNN_name.sql")
        version = int(match.group(1))
        if version in found:
            raise StoreError(f"two migrations with version {version:03d}")
        found[version] = Migration(version, entry.name, entry.read_text(encoding="utf-8"))
    if not found:
        raise StoreError(f"no migrations found in {folder}")
    return [found[v] for v in sorted(found)]


# --- Store ----------------------------------------------------------------------------


class Store:
    """Typed access to foreman-inator's SQLite database.

    ``Store(path)`` opens (creating the file if needed) and applies pending migrations.
    ``Store(path, read_only=True)`` opens an existing database for reading only and
    never migrates. ``clock`` returns "now" for every timestamp the store writes; tests
    pass a fake clock.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        auto_migrate: bool = True,
        clock: Callable[[], datetime] | None = None,
        migrations: Traversable | Path | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._clock = clock or (lambda: datetime.now(UTC))
        self._migrations_source = migrations
        self._lock = threading.RLock()
        self._tx_depth = 0
        if read_only:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        else:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        self._conn = conn
        if auto_migrate and not read_only:
            self.migrate()

    # -- lifecycle --

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def now(self) -> datetime:
        return self._clock()

    # -- transactions and raw reads --

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes atomically. Nested calls join the outer transaction."""
        with self._lock:
            if self._tx_depth:
                self._tx_depth += 1
                try:
                    yield self._conn
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._tx_depth = 0

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        """Run a read query (for metrics). Rows support ``row["name"]``."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _insert(self, table: str, values: dict[str, Any]) -> int:
        names = list(values)
        cols = ", ".join(f'"{n}"' for n in names)
        marks = ", ".join("?" for _ in names)
        params = [_to_db(n, values[n]) for n in names]
        with self.transaction() as conn:
            cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", params)
            return int(cur.lastrowid or 0)

    def _update(
        self, table: str, key: str, key_value: Any, values: dict[str, Any], allowed: frozenset[str]
    ) -> None:
        bad = set(values) - allowed
        if bad:
            raise TypeError(f"cannot update {table} field(s): {', '.join(sorted(bad))}")
        if not values:
            return
        sets = ", ".join(f'"{n}" = ?' for n in values)
        params = [_to_db(n, v) for n, v in values.items()] + [key_value]
        with self.transaction() as conn:
            cur = conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", params)
            if cur.rowcount == 0:
                raise NotFoundError(f"no {table} row with {key} = {key_value!r}")

    # -- migrations --

    def schema_version(self) -> int:
        """Highest applied migration version (0 for an empty database)."""
        if not self._has_table("schema_migrations"):
            return 0
        row = self._one("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
        return int(row["v"]) if row else 0

    def _has_table(self, name: str) -> bool:
        row = self._one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
        return row is not None

    def _applied_versions(self) -> set[int]:
        rows = self.query("SELECT version FROM schema_migrations")
        return {int(r["version"]) for r in rows}

    def migrate(self) -> list[int]:
        """Apply pending migrations in order; return the versions applied (empty = no-op)."""
        if self.read_only:
            raise StoreError("read-only store cannot migrate")
        migrations = load_migrations(self._migrations_source)
        known = {m.version for m in migrations}
        applied_now: list[int] = []
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL"
                ") STRICT"
            )
            applied = self._applied_versions()
            unknown = applied - known
            if unknown:
                raise StoreError(
                    f"database has migrations this code does not know: {sorted(unknown)}"
                )
            for m in migrations:
                if m.version in applied:
                    continue
                self._apply(m)
                applied_now.append(m.version)
        return applied_now

    def _apply(self, m: Migration) -> None:
        body = m.sql.rstrip()
        if not body.endswith(";"):
            body += ";"
        name = m.name.replace("'", "''")
        # The schema_migrations row goes first, so a second process racing on the same
        # version fails on its primary key and rolls back instead of re-running DDL.
        script = (
            "BEGIN IMMEDIATE;\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({m.version}, '{name}', '{_ts(self.now())}');\n"
            f"{body}\n"
            "COMMIT;\n"
        )
        try:
            self._conn.executescript(script)
        except sqlite3.Error:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if m.version in self._applied_versions():
                return  # another process applied it first
            raise

    # -- tickets --

    _TICKET_UPDATABLE = frozenset(
        {
            "title",
            "request",
            "base_branch",
            "pr_number",
            "owned_files",
            "route_reason",
            "trace_id",
            "routed_tier",
        }
    )

    def create_ticket(
        self,
        *,
        repo: str,
        issue_number: int,
        title: str,
        request: str,
        base_branch: str,
        source: Source = "issue",
        owned_files: Iterable[str] = (),
        route_reason: str | None = None,
        trace_id: str | None = None,
        routed_tier: int | None = None,
    ) -> Ticket:
        ticket_id = self._insert(
            "tickets",
            {
                "repo": repo,
                "issue_number": issue_number,
                "title": title,
                "request": request,
                "base_branch": base_branch,
                "source": source,
                "owned_files": owned_files,
                "route_reason": route_reason,
                "trace_id": trace_id,
                "routed_tier": routed_tier,
                "column": "todo",
                "created_at": self.now(),
            },
        )
        return self.get_ticket(ticket_id)

    def get_ticket(self, ticket_id: int) -> Ticket:
        row = self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if row is None:
            raise NotFoundError(f"no ticket {ticket_id}")
        return _row_to(Ticket, row)

    def find_open_ticket(self, repo: str, issue_number: int) -> Ticket | None:
        """The ticket in todo/in_progress/testing for this issue, if any."""
        row = self._one(
            "SELECT * FROM tickets WHERE repo = ? AND issue_number = ?"
            " AND \"column\" IN ('todo', 'in_progress', 'testing')",
            (repo, issue_number),
        )
        return _row_to(Ticket, row) if row else None

    def tickets_for_issue(self, repo: str, issue_number: int) -> list[Ticket]:
        rows = self.query(
            "SELECT * FROM tickets WHERE repo = ? AND issue_number = ? ORDER BY id",
            (repo, issue_number),
        )
        return [_row_to(Ticket, r) for r in rows]

    def list_tickets(
        self, *, column: Column | Sequence[Column] | None = None, repo: str | None = None
    ) -> list[Ticket]:
        where, params = [], []
        if column is not None:
            cols = [column] if isinstance(column, str) else list(column)
            where.append(f'"column" IN ({", ".join("?" for _ in cols)})')
            params.extend(cols)
        if repo is not None:
            where.append("repo = ?")
            params.append(repo)
        sql = "SELECT * FROM tickets"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row_to(Ticket, r) for r in self.query(sql + " ORDER BY id", params)]

    def update_ticket(self, ticket_id: int, **values: Any) -> Ticket:
        """Update plain ticket fields (see ``_TICKET_UPDATABLE``)."""
        self._update("tickets", "id", ticket_id, values, self._TICKET_UPDATABLE)
        return self.get_ticket(ticket_id)

    def set_column(self, ticket_id: int, column: Column) -> Ticket:
        """Move a ticket. ``done_at`` is stamped on done/dropped and cleared otherwise.

        Transition rules belong to the lifecycle (#11); the store only enforces the enum.
        """
        done_at = self.now() if column in ("done", "dropped") else None
        self._update(
            "tickets",
            "id",
            ticket_id,
            {"column": column, "done_at": done_at},
            frozenset({"column", "done_at"}),
        )
        return self.get_ticket(ticket_id)

    def set_accepted_attempt(self, ticket_id: int, attempt_id: int) -> Ticket:
        """Record the user's accept: ticket.accepted_attempt_id and attempt outcome."""
        with self.transaction():
            attempt = self.get_attempt(attempt_id)
            if attempt.ticket_id != ticket_id:
                raise StoreError(f"attempt {attempt_id} belongs to ticket {attempt.ticket_id}")
            self._update(
                "tickets",
                "id",
                ticket_id,
                {"accepted_attempt_id": attempt_id},
                frozenset({"accepted_attempt_id"}),
            )
            self.set_attempt_outcome(attempt_id, "accepted")
        return self.get_ticket(ticket_id)

    def flag_ticket(self, ticket_id: int, reason: str) -> Ticket:
        """Mark a ticket as handed to the user (invariant 7e)."""
        self._update(
            "tickets",
            "id",
            ticket_id,
            {"flag_reason": reason, "flagged_at": self.now()},
            frozenset({"flag_reason", "flagged_at"}),
        )
        return self.get_ticket(ticket_id)

    def clear_flag(self, ticket_id: int) -> Ticket:
        self._update(
            "tickets",
            "id",
            ticket_id,
            {"flag_reason": None, "flagged_at": None},
            frozenset({"flag_reason", "flagged_at"}),
        )
        return self.get_ticket(ticket_id)

    # -- attempts --

    _ATTEMPT_UPDATABLE = frozenset(
        {
            "variant_id",
            "worktree",
            "branch",
            "base_commit",
            "tests_passed",
            "build_passed",
            "diff_files",
            "diff_lines",
            "tokens_in",
            "tokens_out",
            "cost_usd",
            "seconds",
            "verify_output_path",
        }
    )

    def add_attempt(
        self,
        ticket_id: int,
        *,
        model: str,
        tier: int,
        trigger: Trigger,
        variant_id: str | None = None,
        worktree: str | None = None,
        branch: str | None = None,
        base_commit: str | None = None,
    ) -> Attempt:
        attempt_id = self._insert(
            "attempts",
            {
                "ticket_id": ticket_id,
                "model": model,
                "tier": tier,
                "trigger": trigger,
                "variant_id": variant_id,
                "worktree": worktree,
                "branch": branch,
                "base_commit": base_commit,
                "started_at": self.now(),
            },
        )
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: int) -> Attempt:
        row = self._one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
        if row is None:
            raise NotFoundError(f"no attempt {attempt_id}")
        return _row_to(Attempt, row)

    def list_attempts(self, ticket_id: int) -> list[Attempt]:
        rows = self.query("SELECT * FROM attempts WHERE ticket_id = ? ORDER BY id", (ticket_id,))
        return [_row_to(Attempt, r) for r in rows]

    def update_attempt(self, attempt_id: int, **values: Any) -> Attempt:
        """Set measured fields (see ``_ATTEMPT_UPDATABLE``)."""
        self._update("attempts", "id", attempt_id, values, self._ATTEMPT_UPDATABLE)
        return self.get_attempt(attempt_id)

    def add_attempt_usage(
        self, attempt_id: int, *, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0
    ) -> Attempt:
        """Add one model call's usage to the attempt's running totals (the proxy, #9)."""
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE attempts SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?,"
                " cost_usd = cost_usd + ? WHERE id = ?",
                (tokens_in, tokens_out, cost_usd, attempt_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"no attempt {attempt_id}")
        return self.get_attempt(attempt_id)

    def finish_attempt(
        self, attempt_id: int, outcome: Outcome | None = None, **values: Any
    ) -> Attempt:
        """Stamp ``ended_at`` (and ``seconds`` if not given), set fields and the outcome.

        ``outcome`` may stay None when the attempt passed checks and now waits on review.
        """
        with self.transaction():
            attempt = self.get_attempt(attempt_id)
            ended = self.now()
            values.setdefault("seconds", max(0.0, (ended - attempt.started_at).total_seconds()))
            self._update("attempts", "id", attempt_id, values, self._ATTEMPT_UPDATABLE)
            self._update(
                "attempts",
                "id",
                attempt_id,
                {"ended_at": ended, "outcome": outcome},
                frozenset({"ended_at", "outcome"}),
            )
        return self.get_attempt(attempt_id)

    def set_attempt_outcome(self, attempt_id: int, outcome: Outcome) -> Attempt:
        self._update("attempts", "id", attempt_id, {"outcome": outcome}, frozenset({"outcome"}))
        return self.get_attempt(attempt_id)

    # -- kickbacks --

    def record_kickback(
        self,
        attempt_id: int,
        *,
        by: KickbackBy,
        tag: Tag | None = None,
        note: str | None = None,
        failure_step: FailureStep | None = None,
        failure_step_index: int | None = None,
    ) -> Kickback:
        kickback_id = self._insert(
            "kickbacks",
            {
                "attempt_id": attempt_id,
                "by": by,
                "tag": tag,
                "note": note,
                "failure_step": failure_step,
                "failure_step_index": failure_step_index,
                "created_at": self.now(),
            },
        )
        return self.get_kickback(kickback_id)

    def get_kickback(self, kickback_id: int) -> Kickback:
        row = self._one("SELECT * FROM kickbacks WHERE id = ?", (kickback_id,))
        if row is None:
            raise NotFoundError(f"no kickback {kickback_id}")
        return _row_to(Kickback, row)

    def set_failure_step(
        self, kickback_id: int, failure_step: FailureStep, step_index: int | None = None
    ) -> Kickback:
        self._update(
            "kickbacks",
            "id",
            kickback_id,
            {"failure_step": failure_step, "failure_step_index": step_index},
            frozenset({"failure_step", "failure_step_index"}),
        )
        return self.get_kickback(kickback_id)

    def list_kickbacks(
        self, *, attempt_id: int | None = None, ticket_id: int | None = None
    ) -> list[Kickback]:
        if attempt_id is not None:
            rows = self.query(
                "SELECT * FROM kickbacks WHERE attempt_id = ? ORDER BY id", (attempt_id,)
            )
        elif ticket_id is not None:
            rows = self.query(
                "SELECT k.* FROM kickbacks k JOIN attempts a ON a.id = k.attempt_id"
                " WHERE a.ticket_id = ? ORDER BY k.id",
                (ticket_id,),
            )
        else:
            rows = self.query("SELECT * FROM kickbacks ORDER BY id")
        return [_row_to(Kickback, r) for r in rows]

    # -- reviews --

    def record_review(
        self,
        attempt_id: int,
        *,
        reviewer_model: str,
        verdict: Verdict,
        variant_id: str | None = None,
        tests_passed: bool | None = None,
        changed_line_coverage: float | None = None,
        tests_nonvacuous: bool | None = None,
        ran_app: RanApp | None = None,
        tag: Tag | None = None,
        note: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        shadow: bool = False,
    ) -> Review:
        """Store an AI review. Only one non-shadow review per attempt (unique index)."""
        review_id = self._insert(
            "reviews",
            {
                "attempt_id": attempt_id,
                "reviewer_model": reviewer_model,
                "verdict": verdict,
                "variant_id": variant_id,
                "tests_passed": tests_passed,
                "changed_line_coverage": changed_line_coverage,
                "tests_nonvacuous": tests_nonvacuous,
                "ran_app": ran_app,
                "tag": tag,
                "note": note,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "shadow": shadow,
                "created_at": self.now(),
            },
        )
        return self.get_review(review_id)

    def get_review(self, review_id: int) -> Review:
        row = self._one("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if row is None:
            raise NotFoundError(f"no review {review_id}")
        return _row_to(Review, row)

    def list_reviews(
        self,
        *,
        attempt_id: int | None = None,
        ticket_id: int | None = None,
        shadow: bool | None = None,
    ) -> list[Review]:
        where, params = [], []
        if attempt_id is not None:
            where.append("r.attempt_id = ?")
            params.append(attempt_id)
        if ticket_id is not None:
            where.append("a.ticket_id = ?")
            params.append(ticket_id)
        if shadow is not None:
            where.append("r.shadow = ?")
            params.append(int(shadow))
        sql = "SELECT r.* FROM reviews r JOIN attempts a ON a.id = r.attempt_id"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row_to(Review, r) for r in self.query(sql + " ORDER BY r.id", params)]

    # -- experiments --

    def create_experiment(
        self,
        *,
        role: Role,
        champion_variant: str,
        challenger_variant: str,
        mode: ExperimentMode,
        traffic_share: float | None = None,
    ) -> Experiment:
        experiment_id = self._insert(
            "experiments",
            {
                "role": role,
                "champion_variant": champion_variant,
                "challenger_variant": challenger_variant,
                "mode": mode,
                "traffic_share": traffic_share,
                "status": "running",
                "started_at": self.now(),
            },
        )
        return self.get_experiment(experiment_id)

    def get_experiment(self, experiment_id: int) -> Experiment:
        row = self._one("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
        if row is None:
            raise NotFoundError(f"no experiment {experiment_id}")
        return _row_to(Experiment, row)

    def list_experiments(
        self, *, role: Role | None = None, status: ExperimentStatus | None = None
    ) -> list[Experiment]:
        where, params = [], []
        if role is not None:
            where.append("role = ?")
            params.append(role)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM experiments"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row_to(Experiment, r) for r in self.query(sql + " ORDER BY id", params)]

    def set_experiment_note(self, experiment_id: int, result_note: str) -> Experiment:
        self._update(
            "experiments",
            "id",
            experiment_id,
            {"result_note": result_note},
            frozenset({"result_note"}),
        )
        return self.get_experiment(experiment_id)

    def end_experiment(
        self,
        experiment_id: int,
        status: Literal["promoted", "rejected"],
        result_note: str | None = None,
    ) -> Experiment:
        values: dict[str, Any] = {"status": status, "ended_at": self.now()}
        if result_note is not None:
            values["result_note"] = result_note
        self._update(
            "experiments",
            "id",
            experiment_id,
            values,
            frozenset({"status", "ended_at", "result_note"}),
        )
        return self.get_experiment(experiment_id)

    # -- variants and champions --

    def upsert_variant(
        self, variant_id: str, *, role: Role, model: str, prompt_name: str, prompt_hash: str
    ) -> Variant:
        """Insert a variant if new; an existing id must match exactly."""
        with self.transaction():
            row = self._one("SELECT * FROM variants WHERE id = ?", (variant_id,))
            if row is None:
                self._insert(
                    "variants",
                    {
                        "id": variant_id,
                        "role": role,
                        "model": model,
                        "prompt_name": prompt_name,
                        "prompt_hash": prompt_hash,
                        "created_at": self.now(),
                    },
                )
                return self.get_variant(variant_id)
            existing = _row_to(Variant, row)
            if (existing.role, existing.model, existing.prompt_name, existing.prompt_hash) != (
                role,
                model,
                prompt_name,
                prompt_hash,
            ):
                raise StoreError(f"variant {variant_id!r} already exists with other fields")
            return existing

    def get_variant(self, variant_id: str) -> Variant:
        row = self._one("SELECT * FROM variants WHERE id = ?", (variant_id,))
        if row is None:
            raise NotFoundError(f"no variant {variant_id!r}")
        return _row_to(Variant, row)

    def list_variants(self, *, role: Role | None = None) -> list[Variant]:
        if role is None:
            rows = self.query("SELECT * FROM variants ORDER BY created_at, id")
        else:
            rows = self.query(
                "SELECT * FROM variants WHERE role = ? ORDER BY created_at, id", (role,)
            )
        return [_row_to(Variant, r) for r in rows]

    def set_champion(self, role: Role, variant_id: str, note: str | None = None) -> ChampionChange:
        """Make ``variant_id`` the champion for ``role`` (appends a history row)."""
        with self.transaction():
            variant = self.get_variant(variant_id)
            if variant.role != role:
                raise StoreError(f"variant {variant_id!r} is a {variant.role}, not a {role}")
            change_id = self._insert(
                "champion_history",
                {"role": role, "variant_id": variant_id, "set_at": self.now(), "note": note},
            )
        row = self._one("SELECT * FROM champion_history WHERE id = ?", (change_id,))
        assert row is not None
        return _row_to(ChampionChange, row)

    def champion(self, role: Role) -> str | None:
        """Current champion variant id for ``role``, or None if none was set."""
        row = self._one(
            "SELECT variant_id FROM champion_history WHERE role = ? ORDER BY id DESC LIMIT 1",
            (role,),
        )
        return row["variant_id"] if row else None

    def champion_history(self, role: Role) -> list[ChampionChange]:
        rows = self.query("SELECT * FROM champion_history WHERE role = ? ORDER BY id", (role,))
        return [_row_to(ChampionChange, r) for r in rows]

    # -- repo settings --

    def get_repo_settings(self, repo: str) -> RepoSettings | None:
        row = self._one("SELECT * FROM repo_settings WHERE repo = ?", (repo,))
        return _row_to(RepoSettings, row) if row else None

    def set_verify_command(self, repo: str, verify_command: str | None) -> RepoSettings:
        now = _ts(self.now())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO repo_settings (repo, verify_command, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (repo) DO UPDATE SET verify_command = excluded.verify_command,"
                " updated_at = excluded.updated_at",
                (repo, verify_command, now),
            )
        settings = self.get_repo_settings(repo)
        assert settings is not None
        return settings

    def mark_verify_prompted(self, repo: str) -> bool:
        """Record the one-time "no verify command" flag. True only on the first call."""
        now = _ts(self.now())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO repo_settings (repo, updated_at) VALUES (?, ?)"
                " ON CONFLICT (repo) DO NOTHING",
                (repo, now),
            )
            cur = conn.execute(
                "UPDATE repo_settings SET verify_prompted_at = ?, updated_at = ?"
                " WHERE repo = ? AND verify_prompted_at IS NULL",
                (now, now, repo),
            )
            return cur.rowcount == 1

    # -- poll cursors --

    def get_cursor(self, repo: str, name: str = DEFAULT_CURSOR) -> str | None:
        row = self._one("SELECT value FROM poll_cursors WHERE repo = ? AND name = ?", (repo, name))
        return row["value"] if row else None

    def set_cursor(self, repo: str, value: str, name: str = DEFAULT_CURSOR) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO poll_cursors (repo, name, value, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (repo, name) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (repo, name, value, _ts(self.now())),
            )

    # -- spend --

    def record_spend(
        self,
        bucket: SpendBucket,
        cost_usd: float,
        *,
        model: str | None = None,
        ticket_id: int | None = None,
        attempt_id: int | None = None,
        experiment_id: int | None = None,
    ) -> SpendEntry:
        entry_id = self._insert(
            "spend_ledger",
            {
                "bucket": bucket,
                "cost_usd": float(cost_usd),
                "model": model,
                "ticket_id": ticket_id,
                "attempt_id": attempt_id,
                "experiment_id": experiment_id,
                "created_at": self.now(),
            },
        )
        row = self._one("SELECT * FROM spend_ledger WHERE id = ?", (entry_id,))
        assert row is not None
        return _row_to(SpendEntry, row)

    def spend_total(
        self, bucket: SpendBucket, *, since: datetime, until: datetime | None = None
    ) -> float:
        """Sum of ``bucket`` spend with ``since <= created_at < until``."""
        sql = "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger"
        sql += " WHERE bucket = ? AND created_at >= ?"
        params: list[Any] = [bucket, _ts(since)]
        if until is not None:
            sql += " AND created_at < ?"
            params.append(_ts(until))
        row = self._one(sql, params)
        return float(row["total"]) if row else 0.0

    def approve_spend(self, ticket_id: int) -> SpendApproval:
        approval_id = self._insert(
            "spend_approvals", {"ticket_id": ticket_id, "approved_at": self.now()}
        )
        row = self._one("SELECT * FROM spend_approvals WHERE id = ?", (approval_id,))
        assert row is not None
        return _row_to(SpendApproval, row)

    def open_spend_approval(self, ticket_id: int) -> SpendApproval | None:
        """The oldest unconsumed approval for the ticket, if any."""
        row = self._one(
            "SELECT * FROM spend_approvals WHERE ticket_id = ? AND consumed_at IS NULL"
            " ORDER BY id LIMIT 1",
            (ticket_id,),
        )
        return _row_to(SpendApproval, row) if row else None

    def consume_spend_approval(self, approval_id: int) -> bool:
        """Use an approval once. False if it was already consumed or does not exist."""
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE spend_approvals SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (_ts(self.now()), approval_id),
            )
            return cur.rowcount == 1

    # -- file locks --

    def acquire_locks(self, ticket_id: int, repo: str, paths: Iterable[str]) -> list[FileLock]:
        """All-or-nothing: lock every path for the ticket, or none.

        Returns the conflicting locks held by other tickets (empty = acquired). Paths the
        ticket already holds are kept. Path normalisation is the caller's job (#19).
        """
        wanted = sorted(set(paths))
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        with self.transaction() as conn:
            rows = conn.execute(
                f"SELECT * FROM file_locks WHERE repo = ? AND path IN ({marks})"
                " AND ticket_id != ? ORDER BY path",
                [repo, *wanted, ticket_id],
            ).fetchall()
            if rows:
                return [_row_to(FileLock, r) for r in rows]
            now = _ts(self.now())
            conn.executemany(
                "INSERT INTO file_locks (repo, path, ticket_id, acquired_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (repo, path) DO NOTHING",
                [(repo, p, ticket_id, now) for p in wanted],
            )
        return []

    def list_locks(
        self, *, repo: str | None = None, ticket_id: int | None = None
    ) -> list[FileLock]:
        where, params = [], []
        if repo is not None:
            where.append("repo = ?")
            params.append(repo)
        if ticket_id is not None:
            where.append("ticket_id = ?")
            params.append(ticket_id)
        sql = "SELECT * FROM file_locks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row_to(FileLock, r) for r in self.query(sql + " ORDER BY repo, path", params)]

    def release_locks(self, ticket_id: int) -> int:
        """Drop every lock the ticket holds; return how many."""
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM file_locks WHERE ticket_id = ?", (ticket_id,))
            return cur.rowcount
