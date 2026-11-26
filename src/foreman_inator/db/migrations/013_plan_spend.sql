-- 013_plan_spend: calls to a flat-fee plan endpoint (`plan_usd_month` set on the model,
-- e.g. the GLM Coding Plan) are still written to the spend ledger at their list price,
-- so the user can see what the plan saved, but with plan = 1 they never count toward
-- the spend caps (`Store.spend_total` sums plan = 0 rows only).
ALTER TABLE spend_ledger ADD COLUMN plan INTEGER NOT NULL DEFAULT 0 CHECK (plan IN (0, 1));
