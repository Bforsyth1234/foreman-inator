-- 011_proposed (#69, GH #49): challenger prompts drafted by an LLM from labeled kickbacks.
--
--   experiments.origin     'human' (default: every row before 011, and every hand-written
--                          challenger) or 'llm' (the challenger is an approved prompt draft).
--   experiments.cutoff_at  for an 'llm' experiment: the proposer read only kickbacks older
--                          than this, and its replay uses only tickets created after it.
--   replay_sets.cutoff_at  a set built for one proposed challenger: only tickets created
--                          after the cutoff. NULL = an ordinary set (the monthly one).
--   prompt_drafts          one drafted prompt with its rationale, waiting for the user.
--                          Nothing is registered or run until it is 'approved'.
ALTER TABLE experiments ADD COLUMN origin TEXT NOT NULL DEFAULT 'human'
    CHECK (origin IN ('human', 'llm'));
ALTER TABLE experiments ADD COLUMN cutoff_at TEXT;
ALTER TABLE replay_sets ADD COLUMN cutoff_at TEXT;

-- patterns: JSON list of {"key", "count", "kickback_ids"} the draft targets.
-- base_variant: the champion the draft was written from; model: the challenger's model
-- (the champion's: the proposer writes prompts only, model choice stays human).
CREATE TABLE prompt_drafts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    role           TEXT NOT NULL CHECK (role IN ('writer', 'reviewer')),
    prompt_name    TEXT NOT NULL,
    prompt_hash    TEXT NOT NULL,
    prompt_text    TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    patterns       TEXT NOT NULL CHECK (json_valid(patterns)),
    base_variant   TEXT NOT NULL,
    model          TEXT NOT NULL,
    proposer_model TEXT NOT NULL,
    cutoff_at      TEXT NOT NULL,
    path           TEXT NOT NULL,
    est_cost_usd   REAL NOT NULL DEFAULT 0 CHECK (est_cost_usd >= 0),
    cost_usd       REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'approved', 'discarded')),
    created_at     TEXT NOT NULL,
    decided_at     TEXT,
    experiment_id  INTEGER REFERENCES experiments (id)
) STRICT;

CREATE INDEX prompt_drafts_role ON prompt_drafts (role, id);
-- One open draft per role (the one-at-a-time rule).
CREATE UNIQUE INDEX prompt_drafts_one_open ON prompt_drafts (role) WHERE status = 'draft';
