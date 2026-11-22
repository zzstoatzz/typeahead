-- The accounts a tracked query put first, once per UTC day (src/first.ts).
-- A day is written by whichever comes first, the page or the hourly cron,
-- and never rewritten.
CREATE TABLE first_daily (
  day INTEGER NOT NULL,  -- Date.now() / 86_400_000
  query TEXT NOT NULL,
  rank INTEGER NOT NULL, -- 1 is first
  did TEXT NOT NULL,
  handle TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  avatar TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (day, query, rank)
) WITHOUT ROWID;
