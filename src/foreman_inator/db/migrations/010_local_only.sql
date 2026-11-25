-- 010_local_only (#49, GH #26): a per-repo "local only" setting.
--
--   cloud   1 (default): the repo's code may go to a cloud tier, within the spend caps
--           (invariant 7c). 0 (`fman repo set-cloud <repo> off`): it never does.
--           Escalation stops at the last local tier and hands over (`local_only`), the
--           spend gate refuses every cloud call for the repo's attempts, AI review uses a
--           local reviewer of another family or is skipped with a PR note, and shadow
--           reviews, replay sets and the failure labeler leave the repo's code local.
ALTER TABLE repo_settings ADD COLUMN cloud INTEGER NOT NULL DEFAULT 1 CHECK (cloud IN (0, 1));
