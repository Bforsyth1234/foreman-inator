-- 006_install (#45): the repo's saved dependency-install command, set by
-- `fman repo set-install OWNER/REPO "<cmd>"` (NULL: detected from the lockfiles, like
-- verify_command).
ALTER TABLE repo_settings ADD COLUMN install_command TEXT;
