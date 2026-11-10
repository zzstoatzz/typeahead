CREATE TABLE account_labels (did TEXT NOT NULL, uri TEXT NOT NULL, val TEXT NOT NULL, cid TEXT, neg INTEGER NOT NULL DEFAULT 0, cts TEXT NOT NULL, exp TEXT, seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (did, uri, val));
CREATE TABLE actor_deltas (
  bucket INTEGER PRIMARY KEY,  -- 5-min bucket (Date.now() / 300_000)
  actors_delta INTEGER NOT NULL DEFAULT 0,
  handles_delta INTEGER NOT NULL DEFAULT 0,
  avatars_delta INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE actors (
  did TEXT PRIMARY KEY,
  handle TEXT NOT NULL DEFAULT '',
  display_name TEXT DEFAULT '',
  avatar_url TEXT DEFAULT '',  -- stores CID only (e.g. bafkrei...); reconstruct URL at query time
  updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
  hidden INTEGER NOT NULL DEFAULT 0
, pds TEXT DEFAULT '', identity_checked_at INTEGER DEFAULT 0, profile_checked_at INTEGER DEFAULT 0, labels TEXT NOT NULL DEFAULT '[]', created_at TEXT DEFAULT '', associated TEXT DEFAULT '{}', avatar_origin TEXT NOT NULL DEFAULT 'bsky', followers_count INTEGER NOT NULL DEFAULT 0, follows_count INTEGER NOT NULL DEFAULT 0, posts_count INTEGER NOT NULL DEFAULT 0, last_activity_at INTEGER NOT NULL DEFAULT 0, quality_score REAL NOT NULL DEFAULT 0, description TEXT DEFAULT '', banner_url TEXT DEFAULT '', profile_source TEXT NOT NULL DEFAULT '', identity_resolved_at INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idx_actors_handle ON actors(handle COLLATE NOCASE);
CREATE TRIGGER actors_ai AFTER INSERT ON actors BEGIN
  INSERT INTO actors_fts(rowid, handle, display_name)
  VALUES (new.rowid, new.handle, new.display_name);
END;
CREATE TRIGGER actors_ad AFTER DELETE ON actors BEGIN
  INSERT INTO actors_fts(actors_fts, rowid, handle, display_name)
  VALUES ('delete', old.rowid, old.handle, old.display_name);
END;
CREATE INDEX idx_actors_hidden ON actors(hidden) WHERE hidden != 0;
CREATE TRIGGER actors_au AFTER UPDATE ON actors WHEN old.handle <> new.handle OR old.display_name <> new.display_name BEGIN INSERT INTO actors_fts(actors_fts, rowid, handle, display_name) VALUES ('delete', old.rowid, old.handle, old.display_name); INSERT INTO actors_fts(rowid, handle, display_name) VALUES (new.rowid, new.handle, new.display_name); END;
CREATE INDEX idx_actors_enrich_identity ON actors(identity_checked_at) WHERE handle = '';
CREATE INDEX idx_actors_enrich_pds ON actors(identity_checked_at) WHERE handle <> '' AND pds = '';
CREATE INDEX idx_actors_enrich_profile ON actors(profile_checked_at) WHERE handle <> '' AND (avatar_url = '' OR labels = '[]' OR created_at = '');
CREATE INDEX idx_actors_updated_at ON actors(updated_at, did);
CREATE INDEX idx_actors_avatar_empty ON actors(avatar_url) WHERE avatar_url = '';
CREATE INDEX idx_actors_visible_quality_score ON actors(quality_score) WHERE handle != '' AND hidden = 0 AND quality_score > 0;
CREATE INDEX idx_actors_refresh_queue ON actors(updated_at) WHERE profile_checked_at = 0 AND handle != '';
CREATE VIRTUAL TABLE actors_fts USING fts5(
  handle, display_name,
  content='actors', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE 'actors_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE 'actors_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE 'actors_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'actors_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE blocked_domains (domain TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT (unixepoch()));
CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
CREATE TABLE identity_queue (did TEXT PRIMARY KEY, gen INTEGER NOT NULL DEFAULT 1, due_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', enqueued_at INTEGER NOT NULL, last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX idx_identity_queue_due ON identity_queue(due_at);
CREATE TABLE metrics (
  hour INTEGER PRIMARY KEY,
  searches INTEGER NOT NULL DEFAULT 0,
  total_ms REAL NOT NULL DEFAULT 0
, cache_hits INTEGER NOT NULL DEFAULT 0, cache_ms REAL NOT NULL DEFAULT 0);
CREATE TABLE mod_overrides (did TEXT PRIMARY KEY, action TEXT NOT NULL, reason TEXT DEFAULT '', created_at INTEGER NOT NULL DEFAULT (unixepoch()));
CREATE TABLE request_buckets (bucket INTEGER NOT NULL, domain TEXT NOT NULL, endpoint TEXT NOT NULL, status INTEGER NOT NULL, hits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, domain, endpoint, status)) WITHOUT ROWID;
CREATE TABLE request_daily (day INTEGER NOT NULL, domain TEXT NOT NULL, endpoint TEXT NOT NULL, status INTEGER NOT NULL, hits INTEGER NOT NULL, PRIMARY KEY (day, domain, endpoint, status)) WITHOUT ROWID;
CREATE TABLE snapshots (
  hour INTEGER PRIMARY KEY,
  total INTEGER NOT NULL DEFAULT 0,
  with_handles INTEGER NOT NULL DEFAULT 0,
  with_avatars INTEGER NOT NULL DEFAULT 0
, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE tombstones (did TEXT PRIMARY KEY, deleted_at INTEGER NOT NULL);
CREATE INDEX idx_tombstones_deleted_at ON tombstones(deleted_at);
CREATE TABLE traffic_buckets (bucket INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, domain));
CREATE TABLE traffic_daily (
       day INTEGER NOT NULL, domain TEXT NOT NULL, hits INTEGER NOT NULL,
       PRIMARY KEY (day, domain)) WITHOUT ROWID;
CREATE TABLE traffic_sources (domain TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 0);
CREATE TABLE zug_migrations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  class TEXT NOT NULL,
  dirty INTEGER NOT NULL DEFAULT 0,
  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
