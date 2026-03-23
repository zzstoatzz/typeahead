CREATE TABLE IF NOT EXISTS actors (
  did TEXT PRIMARY KEY,
  handle TEXT NOT NULL DEFAULT '',
  display_name TEXT DEFAULT '',
  avatar_url TEXT DEFAULT '',  -- stores CID only (e.g. bafkrei...); reconstruct URL at query time
  updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
  hidden INTEGER NOT NULL DEFAULT 0,
  labels TEXT NOT NULL DEFAULT '[]',
  created_at TEXT DEFAULT '',
  associated TEXT DEFAULT '{}',
  pds TEXT DEFAULT '',
  identity_checked_at INTEGER DEFAULT 0,
  profile_checked_at INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_actors_handle ON actors(handle COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_actors_hidden ON actors(hidden) WHERE hidden != 0;

CREATE VIRTUAL TABLE IF NOT EXISTS actors_fts USING fts5(
  handle, display_name,
  content='actors', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);

-- keep FTS5 in sync via triggers
CREATE TRIGGER IF NOT EXISTS actors_ai AFTER INSERT ON actors BEGIN
  INSERT INTO actors_fts(rowid, handle, display_name)
  VALUES (new.rowid, new.handle, new.display_name);
END;

CREATE TRIGGER IF NOT EXISTS actors_ad AFTER DELETE ON actors BEGIN
  INSERT INTO actors_fts(actors_fts, rowid, handle, display_name)
  VALUES ('delete', old.rowid, old.handle, old.display_name);
END;

CREATE TRIGGER IF NOT EXISTS actors_au AFTER UPDATE ON actors
  WHEN old.handle <> new.handle OR old.display_name <> new.display_name
BEGIN
  INSERT INTO actors_fts(actors_fts, rowid, handle, display_name)
  VALUES ('delete', old.rowid, old.handle, old.display_name);
  INSERT INTO actors_fts(rowid, handle, display_name)
  VALUES (new.rowid, new.handle, new.display_name);
END;

CREATE TABLE IF NOT EXISTS metrics (
  hour INTEGER PRIMARY KEY,
  searches INTEGER NOT NULL DEFAULT 0,
  total_ms REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshots (
  hour INTEGER PRIMARY KEY,
  total INTEGER NOT NULL DEFAULT 0,
  with_handles INTEGER NOT NULL DEFAULT 0,
  with_avatars INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS actor_deltas (
  bucket INTEGER PRIMARY KEY,  -- 5-min bucket (Date.now() / 300_000)
  actors_delta INTEGER NOT NULL DEFAULT 0,
  handles_delta INTEGER NOT NULL DEFAULT 0,
  avatars_delta INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS traffic_sources (
  domain TEXT PRIMARY KEY,
  hits INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tombstones (
  did TEXT PRIMARY KEY,
  deleted_at INTEGER NOT NULL
);
