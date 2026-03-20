-- one-shot migration: add created_at and associated columns to actors table
-- run against Turso before deploying the updated worker

ALTER TABLE actors ADD COLUMN created_at TEXT DEFAULT '';
ALTER TABLE actors ADD COLUMN associated TEXT DEFAULT '{}';
