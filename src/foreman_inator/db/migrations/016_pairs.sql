-- 016_pairs (#34, GH #16): `ab:pair` head-to-head. Each side of a pair is its own ticket
-- row; a pairs row links the two.
--
--   tickets.pair_side  'a' or 'b' on a paired ticket; NULL on every unpaired ticket (and
--                      on every row before 016). NULL counts as side 'a'.
--   pairs              one head-to-head on one issue. ticket_a is the normal ticket,
--                      ticket_b runs the opponent on its first attempt.
--     opponent_reason  'challenger' (the running writer experiment's challenger),
--                      'next_rung' (one rung up from A's start), 'lower_rung' (A started
--                      on the top rung).
--     experiment_id    the mode='pair' experiment a challenger opponent counts toward.
--     status           'running' → 'decided' (the user merged one PR, or closed one) or
--                      'dropped' (both closed, issue closed, label removed).
--     winner_ticket_id the side that won; loser_outcome says how the other side lost:
--                      'merged_other' (the user merged the winner) or 'rejected' (the user
--                      closed the loser's PR).
--
-- The one-open-ticket-per-issue index is a plain index (001), not a table constraint, so
-- it is dropped and recreated with the side in its key: one open ticket per issue per side.
ALTER TABLE tickets ADD COLUMN pair_side TEXT CHECK (pair_side IN ('a', 'b'));

DROP INDEX tickets_one_open_per_issue;
CREATE UNIQUE INDEX tickets_one_open_per_issue
    ON tickets (repo, issue_number, COALESCE(pair_side, 'a'))
    WHERE "column" IN ('todo', 'in_progress', 'testing');

CREATE TABLE pairs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo             TEXT NOT NULL,
    issue_number     INTEGER NOT NULL,
    ticket_a         INTEGER NOT NULL UNIQUE REFERENCES tickets (id),
    ticket_b         INTEGER NOT NULL UNIQUE REFERENCES tickets (id),
    opponent_variant TEXT NOT NULL,
    opponent_reason  TEXT NOT NULL
                     CHECK (opponent_reason IN ('challenger', 'next_rung', 'lower_rung')),
    experiment_id    INTEGER REFERENCES experiments (id),
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'decided', 'dropped')),
    winner_ticket_id INTEGER REFERENCES tickets (id),
    loser_outcome    TEXT CHECK (loser_outcome IN ('merged_other', 'rejected')),
    created_at       TEXT NOT NULL,
    decided_at       TEXT,
    CHECK (ticket_a != ticket_b),
    CHECK (winner_ticket_id IS NULL OR winner_ticket_id IN (ticket_a, ticket_b)),
    CHECK (status != 'decided' OR (winner_ticket_id IS NOT NULL AND loser_outcome IS NOT NULL))
) STRICT;

CREATE INDEX pairs_repo_issue ON pairs (repo, issue_number);
CREATE INDEX pairs_status ON pairs (status);
