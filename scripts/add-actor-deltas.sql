CREATE TABLE IF NOT EXISTS actor_deltas (
  bucket INTEGER PRIMARY KEY,  -- 5-min bucket (Date.now() / 300_000)
  actors_delta INTEGER NOT NULL DEFAULT 0,
  handles_delta INTEGER NOT NULL DEFAULT 0,
  avatars_delta INTEGER NOT NULL DEFAULT 0
);
