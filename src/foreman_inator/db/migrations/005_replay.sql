-- 005_replay (#25): replay sets and per-case replay results.
--
-- A replay set is built from the latest ~100 merged tickets (``experiments.replay_sets``)
-- and rebuilt monthly; each set keeps its cases, skipped ones included with the reason,
-- so a comparison can always say which cases it ran on. A replay run is one
-- ``experiments`` row (mode 'replay') pinned to one set; its per-case, per-side scores
-- live in replay_results. Nothing here references the live tickets/attempts tables by
-- foreign key: replay rows are read-only copies of what a case needs, and the live tables
-- never point back at them (metrics and the router never see replays).

-- One built set. role: which kind of cases it holds.
CREATE TABLE replay_sets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT NOT NULL CHECK (role IN ('writer', 'reviewer')),
    built_at      TEXT NOT NULL,
    max_merges    INTEGER NOT NULL CHECK (max_merges > 0),
    case_count    INTEGER NOT NULL DEFAULT 0 CHECK (case_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0)
) STRICT;

CREATE INDEX replay_sets_role ON replay_sets (role, id);

-- One case of a set. ticket_id / attempt_id name the live rows the case was built from
-- (plain integers, no foreign key). status 'skipped' rows keep skip_reason and no data.
-- data: the case as JSON (WriterCase / ReviewerCase in experiments/replay_sets.py).
CREATE TABLE replay_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id      INTEGER NOT NULL REFERENCES replay_sets (id),
    role        TEXT NOT NULL CHECK (role IN ('writer', 'reviewer')),
    ticket_id   INTEGER NOT NULL,
    attempt_id  INTEGER,
    repo        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'skipped')),
    skip_reason TEXT,
    data        TEXT CHECK (data IS NULL OR json_valid(data)),
    created_at  TEXT NOT NULL,
    CHECK ((status = 'ok') = (data IS NOT NULL)),
    CHECK ((status = 'skipped') = (skip_reason IS NOT NULL))
) STRICT;

CREATE INDEX replay_cases_set ON replay_cases (set_id, id);

-- One replay run per experiments row: which set it is pinned to, and how it ended.
-- state 'running' also covers a run that was killed (resume picks it up).
CREATE TABLE replay_runs (
    experiment_id INTEGER PRIMARY KEY REFERENCES experiments (id),
    set_id        INTEGER NOT NULL REFERENCES replay_sets (id),
    role          TEXT NOT NULL CHECK (role IN ('writer', 'reviewer')),
    state         TEXT NOT NULL DEFAULT 'running'
                  CHECK (state IN ('running', 'finished', 'stopped', 'cancelled')),
    stop_reason   TEXT,
    started_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
) STRICT;

-- One scored side of one case: written once, when that side has finished. A side cut
-- short (spend refusal, run budget, cancel, kill) writes nothing and is rerun on resume.
--   writer status:   passed · failed · no_change · agent_failed · timed_out · error
--   reviewer status: scored · error            (score: catch · miss · false_kickback ·
--                                               correct_approve · handed)
-- 'error' (the replay could not run the case: no mirror, no verify command, family
-- conflict, ...) is final and keeps the case out of the comparison.
CREATE TABLE replay_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments (id),
    case_id       INTEGER NOT NULL REFERENCES replay_cases (id),
    side          TEXT NOT NULL CHECK (side IN ('champion', 'challenger')),
    variant_id    TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN ('passed', 'failed', 'no_change', 'agent_failed',
                                    'timed_out', 'scored', 'error')),
    verdict       TEXT CHECK (verdict IN ('approve', 'request_changes', 'handed_to_user')),
    score         TEXT CHECK (score IN ('catch', 'miss', 'false_kickback', 'correct_approve',
                                        'handed')),
    tokens_in     INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out    INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    cost_usd      REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    seconds       REAL NOT NULL DEFAULT 0 CHECK (seconds >= 0),
    detail        TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (experiment_id, case_id, side)
) STRICT;

CREATE INDEX replay_results_experiment ON replay_results (experiment_id, case_id);
