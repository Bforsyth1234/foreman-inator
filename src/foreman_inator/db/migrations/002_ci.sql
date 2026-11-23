-- 002_ci (#41): GitHub CI results for the PR head, per attempt.
--   ci_passed: 1 = CI green, 0 = CI red and charged to the attempt (counts as failed
--              checks), NULL = CI did not judge it (not read yet, no CI, still pending
--              past the limit, red on the base branch too, or no permission).
--   ci_status: how the CI step ended; NULL while the attempt waits on CI (or never
--              reached it). The poller stops reading CI for an attempt once it is set.
ALTER TABLE attempts ADD COLUMN ci_passed INTEGER CHECK (ci_passed IN (0, 1));
ALTER TABLE attempts ADD COLUMN ci_status TEXT
    CHECK (ci_status IN ('passed', 'failed', 'base_red', 'no_ci', 'timed_out', 'no_permission'));
