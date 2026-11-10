-- The nine statistics tables, as production Turso defines them
-- (docs/ops/turso-schema-20261110.sql), for the typeahead-stats D1 database.

CREATE TABLE actor_deltas (
  bucket INTEGER PRIMARY KEY,  -- 5-min bucket (Date.now() / 300_000)
  actors_delta INTEGER NOT NULL DEFAULT 0,
  handles_delta INTEGER NOT NULL DEFAULT 0,
  avatars_delta INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
CREATE TABLE metrics (
  hour INTEGER PRIMARY KEY,
  searches INTEGER NOT NULL DEFAULT 0,
  total_ms REAL NOT NULL DEFAULT 0
, cache_hits INTEGER NOT NULL DEFAULT 0, cache_ms REAL NOT NULL DEFAULT 0);
CREATE TABLE request_buckets (bucket INTEGER NOT NULL, domain TEXT NOT NULL, endpoint TEXT NOT NULL, status INTEGER NOT NULL, hits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, domain, endpoint, status)) WITHOUT ROWID;
CREATE TABLE request_daily (day INTEGER NOT NULL, domain TEXT NOT NULL, endpoint TEXT NOT NULL, status INTEGER NOT NULL, hits INTEGER NOT NULL, PRIMARY KEY (day, domain, endpoint, status)) WITHOUT ROWID;
CREATE TABLE snapshots (
  hour INTEGER PRIMARY KEY,
  total INTEGER NOT NULL DEFAULT 0,
  with_handles INTEGER NOT NULL DEFAULT 0,
  with_avatars INTEGER NOT NULL DEFAULT 0
, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE traffic_buckets (bucket INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, domain));
CREATE TABLE traffic_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain)) WITHOUT ROWID;
CREATE TABLE traffic_sources (domain TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 0);

-- recordActorDelta increments this row with an UPDATE, which is a no-op when
-- the row is missing. Turso's migration 004_corpus_counters seeds it the same
-- way; here it counts from the start of dual write.
INSERT OR IGNORE INTO counters (name, value) VALUES ('actors_total', 0);
