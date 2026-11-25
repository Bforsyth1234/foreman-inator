-- 009_post_merge (#24 / todo 47): what happened to a change after the user merged it.
-- A merge counts as accepted forever; these rows say "accepted, then ...".
--
--   ticket_id    the merged ticket the signal is against
--   attempt_id   its accepted attempt (tickets.accepted_attempt_id when recorded), so
--                metrics can count signals per model
--   kind         'reverted': a commit on the base branch reverts the merge commit;
--                'followup': a later accepted ticket, filed within 7 days of the merge,
--                touches some of the same owned files
--   at           when it happened (the revert commit's date / the follow-up's created_at)
--   detail       short human text (revert sha and subject / follow-up ticket and files)
--   recorded_at  when the scan (metrics/quality.py) found it
-- One row per (ticket, kind): a signal is recorded once, however often the scan runs.
CREATE TABLE post_merge_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets (id),
    attempt_id  INTEGER REFERENCES attempts (id),
    kind        TEXT NOT NULL CHECK (kind IN ('reverted', 'followup')),
    at          TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    UNIQUE (ticket_id, kind)
) STRICT;

CREATE INDEX post_merge_signals_attempt ON post_merge_signals (attempt_id);
