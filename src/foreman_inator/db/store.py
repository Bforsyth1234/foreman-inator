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
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

# --- Enum values, copied from SPEC.md "Data model" ---------------------------------

Column = Literal["todo", "in_progress", "testing", "done", "dropped"]
Source = Literal["issue", "botinator", "ci"]
Trigger = Literal["first", "escalation", "kickback", "ai_kickback", "explore", "rebase"]
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
#: experiments.origin (#69, migration 011): who wrote the challenger.
ExperimentOrigin = Literal["human", "llm"]
EXPERIMENT_ORIGINS: tuple[str, ...] = ("human", "llm")
#: prompt_drafts.status (#69, migration 011).
DraftStatus = Literal["draft", "approved", "discarded"]
SpendBucket = Literal["live", "experiments"]
#: attempts.end_reason (#58, migration 007): why an attempt ended, beyond its outcome.
EndReason = Literal["budget", "timeout"]
#: Repo profile modes (#51, migration 008); NULL in a row means "normal".
WriterMode = Literal["thrifty", "normal", "fast"]
ReviewMode = Literal["off", "light", "normal", "strict"]
WRITER_MODES: tuple[str, ...] = ("thrifty", "normal", "fast")
REVIEW_MODES: tuple[str, ...] = ("off", "light", "normal", "strict")
#: post_merge_signals.kind (#24, migration 009).
SignalKind = Literal["reverted", "followup"]
SIGNAL_KINDS: tuple[str, ...] = ("reverted", "followup")
CiStatus = Literal["passed", "failed", "base_red", "no_ci", "timed_out", "no_permission"]

COLUMNS: tuple[str, ...] = ("todo", "in_progress", "testing", "done", "dropped")
OPEN_COLUMNS: tuple[str, ...] = ("todo", "in_progress", "testing")
SOURCES: tuple[str, ...] = ("issue", "botinator", "ci")
#: "rebase" added by migration 003 (#43): replay the PR's change onto a moved base branch.
TRIGGERS: tuple[str, ...] = (
    "first",
    "escalation",
    "kickback",
    "ai_kickback",
    "explore",
    "rebase",
)
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
#: attempts.ci_status (#41, migration 002). Only "failed" is charged to the attempt.
CI_STATUSES: tuple[str, ...] = (
    "passed",
    "failed",
    "base_red",
    "no_ci",
    "timed_out",
    "no_permission",
)

#: chat_sessions.state (#60, migration 012; docs/chat-design.md section 2).
ChatState = Literal[
    "starting", "idle", "busy", "closing", "opened_pr", "discarded", "ended", "failed"
]
CHAT_STATES: tuple[str, ...] = (
    "starting",
    "idle",
    "busy",
    "closing",
    "opened_pr",
    "discarded",
    "ended",
    "failed",
)
#: A session in one of these states is over for good: no state change leaves it.
CHAT_FINAL_STATES: tuple[str, ...] = ("opened_pr", "discarded", "failed")
#: States that stamp ``ended_at`` (``ended`` keeps its worktree and can be resumed,
#: opened as a PR or discarded).
CHAT_ENDED_STATES: tuple[str, ...] = ("opened_pr", "discarded", "ended", "failed")
#: chat_turns.status (#60, migration 012). NULL while the turn runs.
ChatTurnStatus = Literal["finished", "aborted", "timed_out", "errored", "refused"]
CHAT_TURN_STATUSES: tuple[str, ...] = ("finished", "aborted", "timed_out", "errored", "refused")

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
    # migration 002 (#41): GitHub CI on the PR head. Defaults keep hand-built rows valid.
    ci_passed: bool | None = None
    ci_status: CiStatus | None = None
    # migration 007 (#58): "budget" when the attempt hit its per-attempt limit.
    end_reason: EndReason | None = None
    end_detail: str | None = None
    # migration 008 (#51): the repo profile the attempt ran under (None: before 008).
    writer_mode: WriterMode | None = None
    review_mode: ReviewMode | None = None


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
    # migration 011 (#69): "llm" when the challenger is an approved prompt draft; then
    # cutoff_at is the draft's cutoff (its replay uses only tickets created after it).
    origin: ExperimentOrigin = "human"
    cutoff_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PromptDraft:
    """#69 (migration 011): a challenger prompt drafted by the proposer, waiting for the
    user. ``patterns`` is the JSON list of kickback patterns it targets."""

    id: int
    role: Role
    prompt_name: str
    prompt_hash: str
    prompt_text: str
    rationale: str
    patterns: str
    base_variant: str
    model: str
    proposer_model: str
    cutoff_at: datetime
    path: str
    est_cost_usd: float
    cost_usd: float
    status: DraftStatus
    created_at: datetime
    decided_at: datetime | None
    experiment_id: int | None


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
    install_command: str | None = None  # migration 006 (#45)
    # migration 008 (#51): the repo profile; None means "normal".
    writer_mode: WriterMode | None = None
    review_mode: ReviewMode | None = None
    # migration 010 (#49): False means "local only": the repo's code never goes to a
    # cloud model. Rows from before 010 (and repos with no row) are True.
    cloud: bool = True
    # migration 012 (#60): True lets the repo's chat sandbox reach the network (443/80).
    chat_network: bool = False


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
    chat_session_id: int | None = None  # migration 012 (#60)
    plan: bool = False  # migration 013: a flat-fee plan call, at list price; not in the caps
    credits: float | None = None  # migration 014 (#85): estimated Coding Plan credits


@dataclass(frozen=True, slots=True)
class SpendApproval:
    id: int
    ticket_id: int
    approved_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class FileLock:
    """One locked path. Exactly one of ``ticket_id`` / ``chat_session_id`` is set
    (migration 012, #60: a chat session can hold locks too)."""

    repo: str
    path: str
    ticket_id: int | None
    acquired_at: datetime
    chat_session_id: int | None = None

    @property
    def holder(self) -> tuple[Literal["ticket", "chat"], int]:
        """``("ticket", id)`` or ``("chat", id)``."""
        if self.chat_session_id is not None:
            return ("chat", self.chat_session_id)
        assert self.ticket_id is not None
        return ("ticket", self.ticket_id)


@dataclass(frozen=True, slots=True)
class ChatSession:
    """#60 (migration 012): one interactive chat session."""

    id: int
    repo: str
    base_branch: str
    base_commit: str | None
    branch: str
    worktree: str | None
    state: ChatState
    tier: int
    model: str
    opencode_session_id: str | None
    trace_id: str | None
    pr_number: int | None
    end_reason: str | None
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """#60 (migration 012): one user message in a chat session and the work on it."""

    id: int
    session_id: int
    seq: int
    tier: int
    model: str
    status: ChatTurnStatus | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    tool_calls: int
    edits: int
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class PostMergeSignal:
    """#24 (migration 009): a merged ticket was reverted, or followed up by another."""

    id: int
    ticket_id: int
    attempt_id: int | None
    kind: SignalKind
    at: datetime
    detail: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


# --- Conversions ----------------------------------------------------------------------

_BOOL_FIELDS = frozenset(
    {
        "tests_passed",
        "build_passed",
        "tests_nonvacuous",
        "shadow",
        "ci_passed",
        "cloud",
        "chat_network",
        "plan",
    }
)
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
    # A column a later migration adds is missing from rows read part-way through the
    # migrations (a test of an older migration): the field's default stands in for it.
    have = set(row.keys())
    values = {f.name: _from_db(f.name, row[f.name]) for f in fields(cls) if f.name in have}  # type: ignore[arg-type]
    return cls(**values)


# --- Migrations -----------------------------------------------------------------------

_MIGRATION_FILE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
#: First line of a migration that rebuilds a referenced table (#43): applied with foreign
#: keys off, and checked with ``PRAGMA foreign_key_check`` before it commits.
FK_OFF_PRAGMA = "-- fman: foreign_keys off"


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
        # A table rebuild (SQLite's documented procedure) needs foreign keys off, which
        # cannot change inside a transaction; the check below stands in for them.
        fk_off = m.sql.lstrip().startswith(FK_OFF_PRAGMA)
        # The schema_migrations row goes first, so a second process racing on the same
        # version fails on its primary key and rolls back instead of re-running DDL.
        script = (
            "BEGIN IMMEDIATE;\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({m.version}, '{name}', '{_ts(self.now())}');\n"
            f"{body}\n"
        )
        if fk_off:
            self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.executescript(script)
            if fk_off:
                bad = self._conn.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise StoreError(
                        f"migration {m.name} broke foreign keys: {[tuple(r) for r in bad][:5]}"
                    )
            self._conn.execute("COMMIT")
        except (sqlite3.Error, StoreError):
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if m.version in self._applied_versions():
                return  # another process applied it first
            raise
        finally:
            if fk_off:
                self._conn.execute("PRAGMA foreign_keys = ON")

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
        writer_mode: WriterMode | None = None,
        review_mode: ReviewMode | None = None,
    ) -> Attempt:
        """A new attempt row. ``writer_mode`` / ``review_mode`` (#51) default to the
        ticket's repo profile at this moment (``normal`` when the repo has none), so every
        attempt records the modes it ran under."""
        values: dict[str, Any] = {
            "ticket_id": ticket_id,
            "model": model,
            "tier": tier,
            "trigger": trigger,
            "variant_id": variant_id,
            "worktree": worktree,
            "branch": branch,
            "base_commit": base_commit,
            "started_at": self.now(),
        }
        with self.transaction():
            if self._has_column("attempts", "writer_mode"):  # a store migrated past 008
                if writer_mode is None or review_mode is None:
                    row = self._one(
                        "SELECT r.writer_mode, r.review_mode FROM tickets t"
                        " LEFT JOIN repo_settings r ON r.repo = t.repo WHERE t.id = ?",
                        (ticket_id,),
                    )
                    writer_mode = writer_mode or (row and row["writer_mode"]) or "normal"
                    review_mode = review_mode or (row and row["review_mode"]) or "normal"
                values["writer_mode"] = writer_mode
                values["review_mode"] = review_mode
            attempt_id = self._insert("attempts", values)
        return self.get_attempt(attempt_id)

    def _has_column(self, table: str, column: str) -> bool:
        with self._lock:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == column for r in rows)

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

    def set_ci_result(
        self, attempt_id: int, status: CiStatus, *, failure_output_path: str | None = None
    ) -> Attempt:
        """Record how the CI step ended for an attempt (#41), in one transaction.

        ``ci_passed`` follows from ``status``: True for ``passed``, False for ``failed``,
        None otherwise (CI did not judge the change). ``failed`` also stores the attempt
        as ``failed_checks`` and points ``verify_output_path`` at ``failure_output_path``
        (the CI log tail the worker saved), so escalation counts it as failed checks and
        the retry prompt shows that log (``prompt.read_failure_output``)."""
        passed = {"passed": True, "failed": False}.get(status)
        values: dict[str, Any] = {"ci_status": status, "ci_passed": passed}
        if status == "failed":
            values["outcome"] = "failed_checks"
            if failure_output_path is not None:
                values["verify_output_path"] = failure_output_path
        self._update(
            "attempts",
            "id",
            attempt_id,
            values,
            frozenset({"ci_status", "ci_passed", "outcome", "verify_output_path"}),
        )
        return self.get_attempt(attempt_id)

    def set_attempt_end_reason(
        self, attempt_id: int, reason: EndReason | None, detail: str | None = None
    ) -> Attempt:
        """Record why an attempt ended (#58), e.g. ``("budget", "spent $1.52 of $1.50")``."""
        self._update(
            "attempts",
            "id",
            attempt_id,
            {"end_reason": reason, "end_detail": detail},
            frozenset({"end_reason", "end_detail"}),
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
        origin: ExperimentOrigin | None = None,
        cutoff_at: datetime | None = None,
    ) -> Experiment:
        """A running experiment. ``origin`` None (#69): "llm" with the draft's cutoff when
        the challenger is an approved prompt draft (``approved_draft_for``), else "human".
        So every experiment of a proposed challenger (its replay, its shadow, a split),
        however it is started, carries the cutoff."""
        values: dict[str, Any] = {
            "role": role,
            "champion_variant": champion_variant,
            "challenger_variant": challenger_variant,
            "mode": mode,
            "traffic_share": traffic_share,
            "status": "running",
            "started_at": self.now(),
        }
        if self._has_column("experiments", "origin"):
            with self.transaction():
                if origin is None:
                    draft = self.approved_draft_for(role, challenger_variant)
                    origin = "llm" if draft is not None else "human"
                    if draft is not None and cutoff_at is None:
                        cutoff_at = draft.cutoff_at
                values["origin"] = origin
                values["cutoff_at"] = cutoff_at
                experiment_id = self._insert("experiments", values)
        else:
            experiment_id = self._insert("experiments", values)
        return self.get_experiment(experiment_id)

    def running_llm_experiments(self, role: Role) -> list[Experiment]:
        """#69: running experiments of ``role`` whose challenger an LLM drafted."""
        if not self._has_column("experiments", "origin"):
            return []
        rows = self.query(
            "SELECT * FROM experiments WHERE role = ? AND status = 'running' AND origin = 'llm'"
            " ORDER BY id",
            (role,),
        )
        return [_row_to(Experiment, r) for r in rows]

    # -- prompt drafts (#69) --

    def create_prompt_draft(
        self,
        *,
        role: Role,
        prompt_name: str,
        prompt_hash: str,
        prompt_text: str,
        rationale: str,
        patterns: str,
        base_variant: str,
        model: str,
        proposer_model: str,
        cutoff_at: datetime,
        path: str,
        est_cost_usd: float = 0.0,
        cost_usd: float = 0.0,
    ) -> PromptDraft:
        draft_id = self._insert(
            "prompt_drafts",
            {
                "role": role,
                "prompt_name": prompt_name,
                "prompt_hash": prompt_hash,
                "prompt_text": prompt_text,
                "rationale": rationale,
                "patterns": patterns,
                "base_variant": base_variant,
                "model": model,
                "proposer_model": proposer_model,
                "cutoff_at": cutoff_at,
                "path": path,
                "est_cost_usd": float(est_cost_usd),
                "cost_usd": float(cost_usd),
                "status": "draft",
                "created_at": self.now(),
            },
        )
        return self.get_prompt_draft(draft_id)

    def get_prompt_draft(self, draft_id: int) -> PromptDraft:
        row = self._one("SELECT * FROM prompt_drafts WHERE id = ?", (int(draft_id),))
        if row is None:
            raise NotFoundError(f"no prompt draft {draft_id}")
        return _row_to(PromptDraft, row)

    def list_prompt_drafts(
        self, *, role: Role | None = None, status: DraftStatus | None = None
    ) -> list[PromptDraft]:
        where, params = [], []
        if role is not None:
            where.append("role = ?")
            params.append(role)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM prompt_drafts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row_to(PromptDraft, r) for r in self.query(sql + " ORDER BY id", params)]

    def decide_prompt_draft(
        self,
        draft_id: int,
        status: Literal["approved", "discarded"],
        *,
        prompt_text: str | None = None,
        prompt_hash: str | None = None,
    ) -> PromptDraft:
        """Approve or discard an open draft (only an open one). ``prompt_text`` /
        ``prompt_hash``: the text as approved (the user may edit the draft file first)."""
        values: dict[str, Any] = {"status": status, "decided_at": _ts(self.now())}
        if prompt_text is not None:
            values["prompt_text"] = prompt_text
        if prompt_hash is not None:
            values["prompt_hash"] = prompt_hash
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE prompt_drafts SET {sets} WHERE id = ? AND status = 'draft'",
                (*values.values(), int(draft_id)),
            )
            if cur.rowcount != 1:
                draft = self.get_prompt_draft(draft_id)  # NotFoundError if missing
                raise StoreError(f"prompt draft {draft_id} is already {draft.status}")
        return self.get_prompt_draft(draft_id)

    def set_prompt_draft_experiment(self, draft_id: int, experiment_id: int) -> PromptDraft:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE prompt_drafts SET experiment_id = ? WHERE id = ?",
                (int(experiment_id), int(draft_id)),
            )
        return self.get_prompt_draft(draft_id)

    def approved_draft_for(self, role: str, variant_id: str) -> PromptDraft | None:
        """The approved draft whose prompt variant ``variant_id`` runs
        (``<model>/<prompt>@<hash8>``, a ``+rules@...`` suffix ignored), or None."""
        if not self._has_table("prompt_drafts"):
            return None
        base = variant_id.split("+", 1)[0]
        _model, _, prompt = base.rpartition("/")
        name, _, hash8 = prompt.partition("@")
        if not name or not hash8:
            return None
        row = self._one(
            "SELECT * FROM prompt_drafts WHERE role = ? AND status = 'approved'"
            " AND prompt_name = ? AND substr(prompt_hash, 1, ?) = ? ORDER BY id DESC LIMIT 1",
            (role, name, len(hash8), hash8),
        )
        return _row_to(PromptDraft, row) if row else None

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

    def set_install_command(self, repo: str, install_command: str | None) -> RepoSettings:
        """Save ``repo``'s dependency-install command (#45); None clears it (detected
        again from the lockfiles)."""
        now = _ts(self.now())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO repo_settings (repo, install_command, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (repo) DO UPDATE SET install_command = excluded.install_command,"
                " updated_at = excluded.updated_at",
                (repo, install_command, now),
            )
        settings = self.get_repo_settings(repo)
        assert settings is not None
        return settings

    def set_profile(
        self, repo: str, writer_mode: WriterMode | None, review_mode: ReviewMode | None
    ) -> RepoSettings:
        """Save ``repo``'s profile (#51). None stores NULL (``normal``). Presets and
        ``--writer`` / ``--review`` are resolved by ``routing.profile.resolve``."""
        if writer_mode is not None and writer_mode not in WRITER_MODES:
            raise ValueError(f"unknown writer mode {writer_mode!r}; valid: {WRITER_MODES}")
        if review_mode is not None and review_mode not in REVIEW_MODES:
            raise ValueError(f"unknown review mode {review_mode!r}; valid: {REVIEW_MODES}")
        now = _ts(self.now())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO repo_settings (repo, writer_mode, review_mode, updated_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (repo) DO UPDATE SET"
                " writer_mode = excluded.writer_mode, review_mode = excluded.review_mode,"
                " updated_at = excluded.updated_at",
                (repo, writer_mode, review_mode, now),
            )
        settings = self.get_repo_settings(repo)
        assert settings is not None
        return settings

    def set_cloud(self, repo: str, cloud: bool) -> RepoSettings:
        """Save ``repo``'s cloud setting (#49). False is "local only": the repo's code
        never goes to a cloud model (escalation, review, spend gate, experiments)."""
        now = _ts(self.now())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO repo_settings (repo, cloud, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (repo) DO UPDATE SET cloud = excluded.cloud,"
                " updated_at = excluded.updated_at",
                (repo, int(bool(cloud)), now),
            )
        settings = self.get_repo_settings(repo)
        assert settings is not None
        return settings

    def repo_allows_cloud(self, repo: str | None) -> bool:
        """False only when ``repo`` is set local only (#49). A repo with no settings row,
        or None (no repo known), allows cloud: the default (invariant 7c's caps apply)."""
        if not repo:
            return True
        settings = self.get_repo_settings(repo)
        return True if settings is None else bool(settings.cloud)

    def local_only_repos(self) -> frozenset[str]:
        """Every repo set local only (#49)."""
        if not self._has_column("repo_settings", "cloud"):
            return frozenset()
        rows = self.query("SELECT repo FROM repo_settings WHERE cloud = 0")
        return frozenset(r["repo"] for r in rows)

    def set_chat_network(self, repo: str, on: bool) -> RepoSettings:
        """Save ``repo``'s chat network opt-in (#60, decision 4). True gives the repo's
        chat sandbox the dependency-install outbound policy (ports 443/80); tickets are
        unaffected."""
        now = _ts(self.now())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO repo_settings (repo, chat_network, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (repo) DO UPDATE SET chat_network = excluded.chat_network,"
                " updated_at = excluded.updated_at",
                (repo, int(bool(on)), now),
            )
        settings = self.get_repo_settings(repo)
        assert settings is not None
        return settings

    def repo_allows_chat_network(self, repo: str | None) -> bool:
        """True only when ``repo`` opted in with ``set_chat_network``. Closed by default:
        a repo with no settings row, or None, gets no network (fail closed, 7g)."""
        if not repo:
            return False
        settings = self.get_repo_settings(repo)
        return False if settings is None else bool(settings.chat_network)

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
        chat_session_id: int | None = None,
        plan: bool = False,
        credits: float | None = None,
    ) -> SpendEntry:
        """One ledger row. ``chat_session_id`` (#60) tags a chat call's row; chat rows are
        bucket ``live`` (option C) but nothing here enforces that (``spend`` does).
        ``plan`` (migration 013): a flat-fee plan call at list price, kept out of the caps.
        ``credits`` (migration 014, #85): its estimated Coding Plan credits."""
        values: dict[str, Any] = {
            "bucket": bucket,
            "cost_usd": float(cost_usd),
            "model": model,
            "ticket_id": ticket_id,
            "attempt_id": attempt_id,
            "experiment_id": experiment_id,
            "created_at": self.now(),
        }
        if chat_session_id is not None:
            values["chat_session_id"] = chat_session_id
        if plan:  # only then: a store migrated part-way (before 013) has no column
            values["plan"] = True
        if credits is not None:  # only then, as for ``plan``
            values["credits"] = float(credits)
        entry_id = self._insert("spend_ledger", values)
        row = self._one("SELECT * FROM spend_ledger WHERE id = ?", (entry_id,))
        assert row is not None
        return _row_to(SpendEntry, row)

    def spend_total(
        self, bucket: SpendBucket, *, since: datetime, until: datetime | None = None
    ) -> float:
        """Sum of ``bucket`` spend with ``since <= created_at < until``. Flat-fee plan
        calls (``plan = 1``, migration 013) are left out: they cost nothing extra."""
        sql = "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger"
        sql += " WHERE bucket = ? AND plan = 0 AND created_at >= ?"
        params: list[Any] = [bucket, _ts(since)]
        if until is not None:
            sql += " AND created_at < ?"
            params.append(_ts(until))
        row = self._one(sql, params)
        return float(row["total"]) if row else 0.0

    def chat_spend(self, session_id: int) -> float:
        """Every ledger row's cost for one chat session (#60), all time."""
        row = self._one(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger"
            " WHERE chat_session_id = ?",
            (session_id,),
        )
        return float(row["total"]) if row else 0.0

    def chat_spend_total(self, since: datetime, until: datetime | None = None) -> float:
        """Sum of every chat session's spend with ``since <= created_at < until`` (#60;
        the chat sub-cap, option C). Any bucket: chat rows are ``live`` by contract."""
        sql = "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_ledger"
        sql += " WHERE chat_session_id IS NOT NULL AND plan = 0 AND created_at >= ?"
        params: list[Any] = [_ts(since)]
        if until is not None:
            sql += " AND created_at < ?"
            params.append(_ts(until))
        row = self._one(sql, params)
        return float(row["total"]) if row else 0.0

    def plan_use(
        self, *, since: datetime, until: datetime | None = None
    ) -> dict[str, tuple[int, float]]:
        """Flat-fee plan calls with ``since <= created_at < until``, both buckets, per
        model name: ``(calls, list-price USD)``. What the calls would have cost metered."""
        sql = (
            "SELECT model, COUNT(*) AS n, COALESCE(SUM(cost_usd), 0.0) AS total"
            " FROM spend_ledger WHERE plan = 1 AND created_at >= ?"
        )
        params: list[Any] = [_ts(since)]
        if until is not None:
            sql += " AND created_at < ?"
            params.append(_ts(until))
        rows = self.query(sql + " GROUP BY model", params)
        return {str(r["model"]): (int(r["n"]), float(r["total"])) for r in rows}

    def plan_credits(self, model: str, *, since: datetime, until: datetime | None = None) -> float:
        """#85: estimated Coding Plan credits of ``model``'s plan calls with
        ``since <= created_at < until``, both buckets."""
        sql = (
            "SELECT COALESCE(SUM(credits), 0.0) AS total FROM spend_ledger"
            " WHERE plan = 1 AND model = ? AND created_at >= ?"
        )
        params: list[Any] = [model, _ts(since)]
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

        Returns the conflicting locks held by other tickets or by chat sessions (empty =
        acquired). Paths the ticket already holds are kept. Path normalisation is the
        caller's job (#19).
        """
        wanted = sorted(set(paths))
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        with self.transaction() as conn:
            rows = conn.execute(
                f"SELECT * FROM file_locks WHERE repo = ? AND path IN ({marks})"
                " AND ticket_id IS NOT ? ORDER BY path",
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
        self,
        *,
        repo: str | None = None,
        ticket_id: int | None = None,
        chat_session_id: int | None = None,
    ) -> list[FileLock]:
        """Lock rows, ticket and chat holders alike unless filtered."""
        where, params = [], []
        if repo is not None:
            where.append("repo = ?")
            params.append(repo)
        if ticket_id is not None:
            where.append("ticket_id = ?")
            params.append(ticket_id)
        if chat_session_id is not None:
            where.append("chat_session_id = ?")
            params.append(chat_session_id)
        sql = "SELECT * FROM file_locks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row_to(FileLock, r) for r in self.query(sql + " ORDER BY repo, path", params)]

    def release_locks(self, ticket_id: int) -> int:
        """Drop every lock the ticket holds; return how many."""
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM file_locks WHERE ticket_id = ?", (ticket_id,))
            return cur.rowcount

    # Chat holders (#60, decision 6). Low-level rows only: policy (what to lock, warnings
    # when a ticket holds a file the chat edits, release on /pr, /discard, end) is locks.py's.

    def chat_lock_rows(self, session_id: int) -> list[FileLock]:
        """Every lock the chat session holds, by repo and path."""
        return self.list_locks(chat_session_id=session_id)

    def insert_chat_locks(self, session_id: int, repo: str, paths: Iterable[str]) -> list[FileLock]:
        """Lock each free path for the chat session; return the locks others hold.

        Not all-or-nothing, unlike ``acquire_locks``: a chat never waits on a ticket's
        locks (decision 6), so it takes what is free and the caller warns about the rest.
        Paths the session already holds are kept; one holder per (repo, path) stands.
        """
        wanted = sorted(set(paths))
        if not wanted:
            return []
        marks = ", ".join("?" for _ in wanted)
        with self.transaction() as conn:
            now = _ts(self.now())
            conn.executemany(
                "INSERT INTO file_locks (repo, path, chat_session_id, acquired_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (repo, path) DO NOTHING",
                [(repo, p, session_id, now) for p in wanted],
            )
            rows = conn.execute(
                f"SELECT * FROM file_locks WHERE repo = ? AND path IN ({marks})"
                " AND chat_session_id IS NOT ? ORDER BY path",
                [repo, *wanted, session_id],
            ).fetchall()
        return [_row_to(FileLock, r) for r in rows]

    def delete_chat_locks(self, session_id: int, paths: Iterable[str] | None = None) -> int:
        """Drop the chat session's locks (all of them, or just ``paths``); return how
        many. Never touches another holder's row."""
        with self.transaction() as conn:
            if paths is None:
                cur = conn.execute(
                    "DELETE FROM file_locks WHERE chat_session_id = ?", (session_id,)
                )
                return cur.rowcount
            wanted = sorted(set(paths))
            if not wanted:
                return 0
            marks = ", ".join("?" for _ in wanted)
            cur = conn.execute(
                f"DELETE FROM file_locks WHERE chat_session_id = ? AND path IN ({marks})",
                [session_id, *wanted],
            )
            return cur.rowcount

    # -- chat sessions and turns (#60, migration 012) --

    _CHAT_UPDATABLE = frozenset(
        {"base_commit", "worktree", "tier", "model", "opencode_session_id", "trace_id"}
    )

    def create_chat_session(
        self,
        repo: str,
        *,
        base_branch: str,
        tier: int,
        model: str,
        branch: str | None = None,
        base_commit: str | None = None,
        worktree: str | None = None,
    ) -> ChatSession:
        """A new session row in state ``starting``. ``branch`` defaults to
        ``fman/chat/<id>`` (the id is known only after the insert, so it is set in the
        same transaction)."""
        with self.transaction() as conn:
            session_id = self._insert(
                "chat_sessions",
                {
                    "repo": repo,
                    "base_branch": base_branch,
                    "base_commit": base_commit,
                    "branch": branch or "fman/chat/new",
                    "worktree": worktree,
                    "tier": tier,
                    "model": model,
                    "started_at": self.now(),
                },
            )
            if branch is None:
                conn.execute(
                    "UPDATE chat_sessions SET branch = ? WHERE id = ?",
                    (f"fman/chat/{session_id}", session_id),
                )
        return self.get_chat_session(session_id)

    def get_chat_session(self, session_id: int) -> ChatSession:
        row = self._one("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
        if row is None:
            raise NotFoundError(f"no chat session {session_id}")
        return _row_to(ChatSession, row)

    def list_chat_sessions(
        self, *, repo: str | None = None, states: Iterable[str] | None = None
    ) -> list[ChatSession]:
        """Sessions, newest first; optionally only ``repo``'s or those in ``states``."""
        where, params = [], []
        if repo is not None:
            where.append("repo = ?")
            params.append(repo)
        if states is not None:
            wanted = list(states)
            if not wanted:
                return []
            where.append(f"state IN ({', '.join('?' for _ in wanted)})")
            params.extend(wanted)
        sql = "SELECT * FROM chat_sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_row_to(ChatSession, r) for r in self.query(sql + " ORDER BY id DESC", params)]

    def update_chat_session(self, session_id: int, **values: Any) -> ChatSession:
        """Set descriptive fields (see ``_CHAT_UPDATABLE``); the state goes through
        ``set_chat_state``."""
        self._update("chat_sessions", "id", session_id, values, self._CHAT_UPDATABLE)
        return self.get_chat_session(session_id)

    def set_chat_state(
        self,
        session_id: int,
        state: ChatState,
        *,
        end_reason: str | None = None,
        pr_number: int | None = None,
    ) -> ChatSession:
        """Move a session to ``state``.

        ``opened_pr`` / ``discarded`` / ``failed`` are final: leaving one raises
        ``StoreError``. Entering any of ``CHAT_ENDED_STATES`` stamps ``ended_at`` and
        stores ``end_reason``; going back to a live state (resuming an ``ended`` session)
        clears both. ``pr_number`` is required for ``opened_pr``.
        """
        if state not in CHAT_STATES:
            raise ValueError(f"unknown chat state {state!r}; valid: {CHAT_STATES}")
        if state == "opened_pr" and pr_number is None:
            raise ValueError("opened_pr needs the pr_number")
        with self.transaction():
            current = self.get_chat_session(session_id)
            if current.state in CHAT_FINAL_STATES and state != current.state:
                raise StoreError(
                    f"chat session {session_id} is {current.state}; cannot move to {state}"
                )
            values: dict[str, Any] = {"state": state}
            if state in CHAT_ENDED_STATES:
                values["ended_at"] = current.ended_at or self.now()
                values["end_reason"] = end_reason
            else:
                values["ended_at"] = None
                values["end_reason"] = None
            if pr_number is not None:
                values["pr_number"] = pr_number
            self._update("chat_sessions", "id", session_id, values, frozenset(values))
        return self.get_chat_session(session_id)

    def add_chat_turn(self, session_id: int, *, tier: int, model: str) -> ChatTurn:
        """A new running turn (status NULL) with the next ``seq`` for the session."""
        with self.transaction() as conn:
            self.get_chat_session(session_id)  # NotFoundError before the FK error
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM chat_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn_id = self._insert(
                "chat_turns",
                {
                    "session_id": session_id,
                    "seq": int(row["n"]),
                    "tier": tier,
                    "model": model,
                    "started_at": self.now(),
                },
            )
        return self.get_chat_turn(turn_id)

    def get_chat_turn(self, turn_id: int) -> ChatTurn:
        row = self._one("SELECT * FROM chat_turns WHERE id = ?", (turn_id,))
        if row is None:
            raise NotFoundError(f"no chat turn {turn_id}")
        return _row_to(ChatTurn, row)

    def list_chat_turns(self, session_id: int) -> list[ChatTurn]:
        rows = self.query(
            "SELECT * FROM chat_turns WHERE session_id = ? ORDER BY seq", (session_id,)
        )
        return [_row_to(ChatTurn, r) for r in rows]

    def finish_chat_turn(
        self,
        turn_id: int,
        status: ChatTurnStatus,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        tool_calls: int | None = None,
        edits: int | None = None,
    ) -> ChatTurn:
        """Stamp ``ended_at`` and the status, and set the turn's totals (None keeps the
        stored value). A turn finishes once: a second call raises ``StoreError``."""
        if status not in CHAT_TURN_STATUSES:
            raise ValueError(f"unknown chat turn status {status!r}; valid: {CHAT_TURN_STATUSES}")
        given = {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": None if cost_usd is None else float(cost_usd),
            "tool_calls": tool_calls,
            "edits": edits,
        }
        values: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
        values["status"] = status
        values["ended_at"] = self.now()
        with self.transaction():
            if self.get_chat_turn(turn_id).status is not None:
                raise StoreError(f"chat turn {turn_id} has already finished")
            self._update("chat_turns", "id", turn_id, values, frozenset(values))
        return self.get_chat_turn(turn_id)

    # -- post-merge signals (#24, migration 009) --

    def merged_tickets(self, *, since: datetime, repo: str | None = None) -> list[Ticket]:
        """Tickets the user merged (done, with an accepted attempt and a PR) whose
        ``done_at`` is at or after ``since``, oldest first."""
        sql = (
            "SELECT * FROM tickets WHERE \"column\" = 'done' AND accepted_attempt_id IS NOT NULL"
            " AND pr_number IS NOT NULL AND done_at >= ?"
        )
        params: list[Any] = [_ts(since)]
        if repo is not None:
            sql += " AND repo = ?"
            params.append(repo)
        return [_row_to(Ticket, r) for r in self.query(sql + " ORDER BY done_at, id", params)]

    def record_post_merge_signal(
        self,
        ticket_id: int,
        kind: SignalKind,
        *,
        at: datetime,
        detail: str = "",
        attempt_id: int | None = None,
    ) -> bool:
        """Record a signal once per (ticket, kind). True if new, False if already there.

        ``attempt_id`` defaults to the ticket's accepted attempt."""
        if kind not in SIGNAL_KINDS:
            raise ValueError(f"unknown post-merge signal kind {kind!r}")
        if attempt_id is None:
            attempt_id = self.get_ticket(ticket_id).accepted_attempt_id
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO post_merge_signals"
                " (ticket_id, attempt_id, kind, at, detail, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ticket_id, attempt_id, kind, _ts(at), detail, _ts(self.now())),
            )
            return cur.rowcount == 1

    def list_post_merge_signals(
        self, *, ticket_id: int | None = None, kind: SignalKind | None = None
    ) -> list[PostMergeSignal]:
        where, params = [], []
        if ticket_id is not None:
            where.append("ticket_id = ?")
            params.append(ticket_id)
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        sql = "SELECT * FROM post_merge_signals"
        if where:
            sql += " WHERE " + " AND ".join(where)
        out = []
        for r in self.query(sql + " ORDER BY id", params):
            sig = _row_to(PostMergeSignal, r)
            out.append(replace(sig, at=datetime.fromisoformat(r["at"])))
        return out
