-- 007_attempt_budget (#58): why an attempt ended, beyond its outcome. 'budget': the
-- attempt reached its per-attempt spend or input-token limit (spend.Spend.budget_stop);
-- 'timeout': reserved for a writer that ran out of wall-clock time. NULL: neither.
-- end_detail is the short human text for it, e.g. "spent $1.52 of $1.50", which
-- escalation puts in the hand-over flag without needing the config.
ALTER TABLE attempts ADD COLUMN end_reason TEXT CHECK (end_reason IN ('budget', 'timeout'));
ALTER TABLE attempts ADD COLUMN end_detail TEXT;
