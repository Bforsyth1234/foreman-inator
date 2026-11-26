-- 014_plan_credits (#85): estimated GLM Coding Plan credits of a flat-fee plan call
-- (plan = 1 rows), from the call's token counts and z.ai's per-model multipliers
-- (proxy/pricing.py PLAN_CREDITS). NULL for metered rows and for plan models with no
-- multipliers in the table.
ALTER TABLE spend_ledger ADD COLUMN credits REAL;
