-- 015_approval_attempt (#82, GH #80): a spend approval's budget survives a restart or a
-- cancel. An approval covers up to limit_usd of cloud spend; an attempt cut short
-- (superseded) hands what is left to the ticket's next attempt instead of losing it.
--   attempt_id: the attempt using it now (a thrifty approval, kind 'attempt', is used
--               before its attempt row exists: it is bound at that attempt's first
--               cloud call, or to the ticket's first attempt started at or after
--               consumed_at when its end is judged).
--   kind:       'attempt' (the thrifty writer gate) or 'grant' (an over-cap grant).
--               NULL on a used approval: used by something else (a plan-quota resume,
--               or before 015); such an approval is never carried.
--   limit_usd:  the room it was given at its first use (the approval cap, kept hard).
--   spent_usd:  cloud spend under it so far, over every attempt that carried it.
--   settled_at: closed: used up, or its attempt finished normally.
--   reopened:   how many times a cut-short attempt handed it on.
ALTER TABLE spend_approvals ADD COLUMN attempt_id INTEGER REFERENCES attempts (id);
ALTER TABLE spend_approvals ADD COLUMN kind TEXT CHECK (kind IN ('attempt', 'grant'));
ALTER TABLE spend_approvals ADD COLUMN limit_usd REAL;
ALTER TABLE spend_approvals ADD COLUMN spent_usd REAL NOT NULL DEFAULT 0.0;
ALTER TABLE spend_approvals ADD COLUMN settled_at TEXT;
ALTER TABLE spend_approvals ADD COLUMN reopened INTEGER NOT NULL DEFAULT 0;
