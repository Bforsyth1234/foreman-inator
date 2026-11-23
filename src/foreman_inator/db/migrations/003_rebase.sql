-- fman: foreign_keys off
-- 003_rebase (#43): attempts.trigger gains 'rebase' (an attempt that replays the PR's change
-- onto a moved base branch and resolves the conflicts).
--
-- SQLite cannot change a CHECK constraint in place, so attempts is rebuilt (the procedure in
-- https://www.sqlite.org/lang_altertable.html#otheralter): create the new table, copy every
-- row with its id, carry the AUTOINCREMENT counter over (ids are never reused), drop the old
-- table, rename the new one, and recreate its indexes. The first line asks the store to run
-- this with foreign keys off and to run PRAGMA foreign_key_check before COMMIT: tickets,
-- kickbacks, reviews and spend_ledger reference attempts by name, so their foreign keys
-- point at the rebuilt table, unchanged.
CREATE TABLE attempts_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id          INTEGER NOT NULL REFERENCES tickets (id),
    model              TEXT NOT NULL,
    tier               INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
    variant_id         TEXT,
    "trigger"          TEXT NOT NULL
                       CHECK ("trigger" IN ('first', 'escalation', 'kickback', 'ai_kickback',
                                            'explore', 'rebase')),
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
    outcome            TEXT
                       CHECK (outcome IN ('accepted', 'kicked_back', 'failed_checks', 'superseded')),
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    base_commit        TEXT,
    verify_output_path TEXT,
    ci_passed          INTEGER CHECK (ci_passed IN (0, 1)),
    ci_status          TEXT
                       CHECK (ci_status IN ('passed', 'failed', 'base_red', 'no_ci', 'timed_out',
                                            'no_permission'))
) STRICT;

INSERT INTO attempts_new (
    id, ticket_id, model, tier, variant_id, "trigger", worktree, branch, tests_passed,
    build_passed, diff_files, diff_lines, tokens_in, tokens_out, cost_usd, seconds, outcome,
    started_at, ended_at, base_commit, verify_output_path, ci_passed, ci_status
)
SELECT
    id, ticket_id, model, tier, variant_id, "trigger", worktree, branch, tests_passed,
    build_passed, diff_files, diff_lines, tokens_in, tokens_out, cost_usd, seconds, outcome,
    started_at, ended_at, base_commit, verify_output_path, ci_passed, ci_status
FROM attempts;

-- Keep the AUTOINCREMENT high-water mark, even when the newest attempt row was deleted.
DELETE FROM sqlite_sequence WHERE name = 'attempts_new';
INSERT INTO sqlite_sequence (name, seq)
    SELECT 'attempts_new', seq FROM sqlite_sequence WHERE name = 'attempts';

DROP TABLE attempts;
ALTER TABLE attempts_new RENAME TO attempts;

CREATE INDEX attempts_ticket ON attempts (ticket_id);
CREATE INDEX attempts_model ON attempts (model);
