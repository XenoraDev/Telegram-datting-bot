-- 001_init: migration bookkeeping. Created by the migration runner's first migration;
-- all application tables arrive in later migrations, in the phase that needs them.
CREATE TABLE schema_migrations (
    version    TEXT        PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
