-- one-shot migration: add enrichment pipeline columns to actors table
-- run once against Turso before deploying the updated worker

ALTER TABLE actors ADD COLUMN pds TEXT DEFAULT '';
ALTER TABLE actors ADD COLUMN identity_checked_at INTEGER DEFAULT 0;
ALTER TABLE actors ADD COLUMN profile_checked_at INTEGER DEFAULT 0;
