-- fman: foreign_keys off
-- 012_chat (#60, docs/chat-design.md section 12 and the 2026-11-25 decisions): chat
-- sessions, their turns, chat spend, the per-repo chat network opt-in, and chat file locks.
--
--   chat_sessions        one interactive session: a worktree on branch fman/chat/<id> cut
--                        from the repo's base branch. Message text is never stored here (it
--                        is in the recording, which has a retention rule).
--   chat_turns           one user message and the model's work on it; seq counts from 1 per
--                        session. status is NULL while the turn runs.
--   spend_ledger         gains chat_session_id: a chat call's row (bucket 'live', option C).
--   repo_settings        gains chat_network: 1 lets the repo's chat sandbox reach the network
--                        (ports 443/80, as a first-attempt dependency install). Default 0:
--                        closed. Tickets are unaffected.
--   file_locks           gains chat_session_id: a lock is held by exactly one ticket or one
--                        chat session (decision 6). ticket_id becomes nullable, so SQLite
--                        needs a table rebuild (https://www.sqlite.org/lang_altertable.html
--                        #otheralter, as 003_rebase does). Nothing references file_locks; the
--                        first line still runs this with foreign keys off and checks them
--                        before COMMIT. One holder per (repo, path) stays the primary key.
CREATE TABLE chat_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    repo                TEXT NOT NULL,
    base_branch         TEXT NOT NULL,
    base_commit         TEXT,
    branch              TEXT NOT NULL CHECK (branch LIKE 'fman/chat/%'),
    worktree            TEXT,
    state               TEXT NOT NULL DEFAULT 'starting'
                        CHECK (state IN ('starting', 'idle', 'busy', 'closing', 'opened_pr',
                                         'discarded', 'ended', 'failed')),
    tier                INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
    model               TEXT NOT NULL,
    opencode_session_id TEXT,
    trace_id            TEXT,
    pr_number           INTEGER,
    end_reason          TEXT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT
) STRICT;

CREATE INDEX chat_sessions_state ON chat_sessions (state);
CREATE INDEX chat_sessions_repo ON chat_sessions (repo, id);

CREATE TABLE chat_turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES chat_sessions (id),
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    tier        INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
    model       TEXT NOT NULL,
    status      TEXT CHECK (status IN ('finished', 'aborted', 'timed_out', 'errored', 'refused')),
    tokens_in   INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out  INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd    REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    tool_calls  INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
    edits       INTEGER NOT NULL DEFAULT 0 CHECK (edits >= 0),
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    UNIQUE (session_id, seq)
) STRICT;

ALTER TABLE spend_ledger ADD COLUMN chat_session_id INTEGER REFERENCES chat_sessions (id);
CREATE INDEX spend_ledger_chat ON spend_ledger (chat_session_id) WHERE chat_session_id IS NOT NULL;

ALTER TABLE repo_settings ADD COLUMN chat_network INTEGER NOT NULL DEFAULT 0
    CHECK (chat_network IN (0, 1));

CREATE TABLE file_locks_new (
    repo            TEXT NOT NULL,
    path            TEXT NOT NULL,
    ticket_id       INTEGER REFERENCES tickets (id),
    acquired_at     TEXT NOT NULL,
    chat_session_id INTEGER REFERENCES chat_sessions (id),
    PRIMARY KEY (repo, path),
    -- exactly one holder kind
    CHECK ((ticket_id IS NULL) <> (chat_session_id IS NULL))
) STRICT;

INSERT INTO file_locks_new (repo, path, ticket_id, acquired_at)
    SELECT repo, path, ticket_id, acquired_at FROM file_locks;

DROP TABLE file_locks;
ALTER TABLE file_locks_new RENAME TO file_locks;

CREATE INDEX file_locks_ticket ON file_locks (ticket_id);
CREATE INDEX file_locks_chat ON file_locks (chat_session_id);
