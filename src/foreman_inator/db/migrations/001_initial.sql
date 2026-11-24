-- 001_initial: every table in SPEC.md "Data model", plus the support tables
-- later items need (variants, champion history, repo settings, poll cursors,
-- spend ledger and approvals, file locks).
--
-- Conventions (later migrations should follow them):
--   * STRICT tables: types are enforced (INTEGER, REAL, TEXT only).
--   * Enums are CHECK constraints with the exact values from SPEC.md.
--   * Booleans are INTEGER 0/1 (NULL = not measured yet).
--   * Times are TEXT, ISO-8601 UTC with microseconds, written by the store
--     (e.g. 2026-11-23T10:00:00.000000+00:00), so they sort as text.
--   * JSON columns are TEXT with a json_valid CHECK.
--   * "column" and "by" are SQL keywords and must be double-quoted.
-- The runner wraps this file in BEGIN IMMEDIATE ... COMMIT; do not add your own.

-- tickets: one per GitHub issue. AUTOINCREMENT so an ID is never reused,
-- even after the highest row is deleted.
CREATE TABLE tickets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT NOT NULL,
    request             TEXT NOT NULL,
    repo                TEXT NOT NULL,
    base_branch         TEXT NOT NULL,
    issue_number        INTEGER NOT NULL,
    pr_number           INTEGER,
    owned_files         TEXT NOT NULL DEFAULT '[]'
                        CHECK (json_valid(owned_files) AND json_type(owned_files) = 'array'),
    "column"            TEXT NOT NULL DEFAULT 'todo'
                        CHECK ("column" IN ('todo', 'in_progress', 'testing', 'done', 'dropped')),
    source              TEXT NOT NULL DEFAULT 'issue'
                        CHECK (source IN ('issue', 'botinator', 'ci')),
    route_reason        TEXT,
    trace_id            TEXT,
    accepted_attempt_id INTEGER REFERENCES attempts (id),
    created_at          TEXT NOT NULL,
    done_at             TEXT,
    -- beyond SPEC: the tier the router picked (router hit rate, #20/#23),
    -- which can differ from the first attempt's tier under exploration.
    routed_tier         INTEGER CHECK (routed_tier BETWEEN 1 AND 3),
    -- beyond SPEC: set when the ticket is handed to the user (invariant 7e).
    flag_reason         TEXT,
    flagged_at          TEXT
) STRICT;

CREATE INDEX tickets_repo_issue ON tickets (repo, issue_number);
CREATE INDEX tickets_column ON tickets ("column");
-- At most one open ticket per issue (poller idempotency, #11).
CREATE UNIQUE INDEX tickets_one_open_per_issue ON tickets (repo, issue_number)
    WHERE "column" IN ('todo', 'in_progress', 'testing');

-- attempts: one model run in one worktree.
CREATE TABLE attempts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id          INTEGER NOT NULL REFERENCES tickets (id),
    model              TEXT NOT NULL,
    tier               INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
    variant_id         TEXT,
    "trigger"          TEXT NOT NULL
                       CHECK ("trigger" IN ('first', 'escalation', 'kickback', 'ai_kickback', 'explore')),
    worktree           TEXT,
    branch             TEXT,
    tests_passed       INTEGER CHECK (tests_passed IN (0, 1)),
    build_passed       INTEGER CHECK (build_passed IN (0, 1)),
    diff_files         INTEGER CHECK (diff_files >= 0),
    diff_lines         INTEGER CHECK (diff_lines >= 0),
    tokens_in          INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out         INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd           REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    seconds            REAL CHECK (seconds >= 0),
    -- NULL while the attempt is still running.
    outcome            TEXT
                       CHECK (outcome IN ('accepted', 'kicked_back', 'failed_checks', 'superseded')),
    -- beyond SPEC: lifecycle times, the base commit (recording/replay, #5/#25)
    -- and the saved verify output (retry with the error output, #14/#16).
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    base_commit        TEXT,
    verify_output_path TEXT
) STRICT;

CREATE INDEX attempts_ticket ON attempts (ticket_id);
CREATE INDEX attempts_model ON attempts (model);

-- kickbacks: a rejection, charged to the attempt that produced the change.
CREATE TABLE kickbacks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id         INTEGER NOT NULL REFERENCES attempts (id),
    "by"               TEXT NOT NULL CHECK ("by" IN ('user', 'reviewer')),
    -- NULL when a user kickback carries no kickback:<tag> label.
    tag                TEXT CHECK (tag IN ('wrong', 'incomplete', 'broke', 'style')),
    note               TEXT,
    failure_step       TEXT
                       CHECK (failure_step IN ('misread_issue', 'missed_file', 'wrong_edit',
                                               'ignored_failure', 'weak_tests', 'reviewer_miss')),
    created_at         TEXT NOT NULL,
    -- beyond SPEC: the trace step index the failure label points at (#27).
    failure_step_index INTEGER CHECK (failure_step_index >= 0)
) STRICT;

CREATE INDEX kickbacks_attempt ON kickbacks (attempt_id);

-- reviews: AI review verdicts.
CREATE TABLE reviews (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id            INTEGER NOT NULL REFERENCES attempts (id),
    reviewer_model        TEXT NOT NULL,
    variant_id            TEXT,
    verdict               TEXT NOT NULL
                          CHECK (verdict IN ('approve', 'request_changes', 'handed_to_user')),
    tests_passed          INTEGER CHECK (tests_passed IN (0, 1)),
    -- percent 0..100; NULL = unknown (no coverage data).
    changed_line_coverage REAL CHECK (changed_line_coverage BETWEEN 0 AND 100),
    tests_nonvacuous      INTEGER CHECK (tests_nonvacuous IN (0, 1)),
    ran_app               TEXT CHECK (ran_app IN ('yes', 'no', 'not_runnable')),
    tag                   TEXT CHECK (tag IN ('wrong', 'incomplete', 'broke', 'style')),
    note                  TEXT,
    tokens_in             INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out            INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd              REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    -- beyond SPEC: when it was recorded, and whether it is a shadow
    -- (challenger) review that is stored but never posted (#26).
    created_at            TEXT NOT NULL,
    shadow                INTEGER NOT NULL DEFAULT 0 CHECK (shadow IN (0, 1))
) STRICT;

CREATE INDEX reviews_attempt ON reviews (attempt_id);
-- "One review per writer attempt" for the review that counts; shadow reviews are extra.
CREATE UNIQUE INDEX reviews_one_live_per_attempt ON reviews (attempt_id) WHERE shadow = 0;

-- experiments: model and prompt comparisons.
CREATE TABLE experiments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    role               TEXT NOT NULL CHECK (role IN ('writer', 'reviewer')),
    champion_variant   TEXT NOT NULL,
    challenger_variant TEXT NOT NULL,
    mode               TEXT NOT NULL
                       CHECK (mode IN ('replay', 'seeded', 'shadow', 'split', 'pair')),
    traffic_share      REAL CHECK (traffic_share BETWEEN 0 AND 1),
    status             TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'promoted', 'rejected')),
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    result_note        TEXT
) STRICT;

-- beyond SPEC: variants (item #3 decision 1). id = <model>/<prompt-name>@<hash8> (#24).
CREATE TABLE variants (
    id          TEXT PRIMARY KEY,
    role        TEXT NOT NULL CHECK (role IN ('writer', 'reviewer')),
    model       TEXT NOT NULL,
    prompt_name TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL
) STRICT;

-- beyond SPEC: champion-per-role history (#24). The current champion is the
-- latest row for the role.
CREATE TABLE champion_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL CHECK (role IN ('writer', 'reviewer')),
    variant_id TEXT NOT NULL REFERENCES variants (id),
    set_at     TEXT NOT NULL,
    note       TEXT
) STRICT;

CREATE INDEX champion_history_role ON champion_history (role, id);

-- beyond SPEC: per-repo settings (item #3 decision 1, #6). verify_prompted_at
-- records the one-time "no verify command found" flag.
CREATE TABLE repo_settings (
    repo               TEXT PRIMARY KEY,
    verify_command     TEXT,
    verify_prompted_at TEXT,
    updated_at         TEXT NOT NULL
) STRICT;

-- beyond SPEC: poll cursors per repo (#11). name lets the poller keep
-- separate cursors (e.g. 'issues', 'events'); value is opaque to the store.
CREATE TABLE poll_cursors (
    repo       TEXT NOT NULL,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, name)
) STRICT;

-- beyond SPEC: cloud spend ledger (#18). One row per charged call or batch.
CREATE TABLE spend_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket        TEXT NOT NULL CHECK (bucket IN ('live', 'experiments')),
    cost_usd      REAL NOT NULL CHECK (cost_usd >= 0),
    model         TEXT,
    ticket_id     INTEGER REFERENCES tickets (id),
    attempt_id    INTEGER REFERENCES attempts (id),
    experiment_id INTEGER REFERENCES experiments (id),
    created_at    TEXT NOT NULL
) STRICT;

CREATE INDEX spend_ledger_bucket_time ON spend_ledger (bucket, created_at);

-- beyond SPEC: `fman approve-spend <ticket>` approvals past the cap (#18).
-- consumed_at is set when the waiting tier 3 attempt uses the approval.
CREATE TABLE spend_approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets (id),
    approved_at TEXT NOT NULL,
    consumed_at TEXT
) STRICT;

CREATE INDEX spend_approvals_ticket ON spend_approvals (ticket_id);

-- beyond SPEC: file locks per repo (#19). One holder per (repo, path).
CREATE TABLE file_locks (
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    ticket_id   INTEGER NOT NULL REFERENCES tickets (id),
    acquired_at TEXT NOT NULL,
    PRIMARY KEY (repo, path)
) STRICT;

CREATE INDEX file_locks_ticket ON file_locks (ticket_id);
