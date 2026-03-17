CREATE TABLE IF NOT EXISTS actors (
  did TEXT PRIMARY KEY,
  handle TEXT NOT NULL DEFAULT '',
  display_name TEXT DEFAULT '',
  avatar_url TEXT DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_actors_handle ON actors(handle COLLATE NOCASE);

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

CREATE TRIGGER IF NOT EXISTS actors_au AFTER UPDATE ON actors BEGIN
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
  with_avatars INTEGER NOT NULL DEFAULT 0
);
