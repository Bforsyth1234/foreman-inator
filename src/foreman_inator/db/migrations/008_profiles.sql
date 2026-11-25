-- 008_profiles (#51): repo profiles. A repo's writer and review settings
-- (routing/profile.py), and the modes each attempt ran under, so metrics (#20) can
-- split by mode. NULL means 'normal' (repos and attempts from before 008).
ALTER TABLE repo_settings ADD COLUMN writer_mode TEXT
    CHECK (writer_mode IN ('thrifty', 'normal', 'fast'));
ALTER TABLE repo_settings ADD COLUMN review_mode TEXT
    CHECK (review_mode IN ('off', 'light', 'normal', 'strict'));
ALTER TABLE attempts ADD COLUMN writer_mode TEXT
    CHECK (writer_mode IN ('thrifty', 'normal', 'fast'));
ALTER TABLE attempts ADD COLUMN review_mode TEXT
    CHECK (review_mode IN ('off', 'light', 'normal', 'strict'));
