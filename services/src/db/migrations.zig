//! Migration list for the Turso schema, run via `zug.sqlite.run`.
//!
//! ## design
//!
//! Migration `001_initial_schema` is a *snapshot* of the current schema (every
//! CREATE TABLE / VIRTUAL TABLE / INDEX / TRIGGER currently in production —
//! verified against `turso db shell typeahead ".schema"` at adoption time).
//!
//! A fresh DB (no `actors` table) runs this migration from scratch. Production
//! turso, which is already in this state, gets bootstrapped:
//! `turso_schema.zig:init` calls `zug.sqlite.baseline` to seed `zug_migrations`
//! with the **baseline** migrations (the first `BOOTSTRAP_BASELINE_COUNT`
//! entries) marked already-applied, then `zug.sqlite.run` sees zero work.
//!
//! **Migrations appended after zug adoption are NOT part of the baseline.**
//! Future schema/data changes get new entries (002, 003, …); zug runs them
//! against turso normally. `BOOTSTRAP_BASELINE_COUNT` stays frozen at the count
//! that existed when zug was adopted, so a restored pre-zug backup gets the
//! baseline pre-applied but every later migration runs for real.
//!
//! Schema additions in the future should be appended as new `ALTER TABLE` /
//! `CREATE INDEX` migrations — never edit migration 001 retroactively (zug's
//! checksum check would catch the change and refuse to run).
//!
//! ## why `statements: []const []const u8` instead of `sql: []const u8`
//!
//! zug's `StatementIterator` (used when a migration sets `.sql`) splits on
//! `;` while tracking strings and comments — but NOT `BEGIN … END` blocks.
//! Our FTS5 maintenance triggers have body statements ending in `;` inside
//! the BEGIN block, which would be (incorrectly) treated as statement
//! boundaries. zug v0.1.2 added `.statements` for exactly this case: an
//! explicit pre-split list, no parsing, no semicolon ambiguity.
//!
//! ## transactions
//!
//! All migrations are `transactional: false` (the default) because the Turso
//! HTTP client closes the connection at the end of each pipeline request, so
//! BEGIN / COMMIT can't span multiple `conn.exec` calls. Partial failure is
//! recoverable via zug's dirty flag — the migration row stays `dirty=1` and
//! blocks subsequent runs until repaired.

const zug = @import("zug");

/// Number of leading migrations that bootstrap pre-marks as already-applied
/// when adopting zug on top of an existing turso DB.
///
/// **Critical: this number is FROZEN at the count of migrations that existed
/// at the moment of adoption.** Migrations appended after zug adoption (002,
/// 003, …) must NOT be folded into the baseline — they need to actually run
/// against turso. If a fresh-but-pre-zug DB ever bootstraps later (e.g. a
/// restored backup), only these baseline entries should be marked applied;
/// everything past this index runs through `zug.sqlite.run` normally.
pub const BOOTSTRAP_BASELINE_COUNT: usize = 1;

pub const migrations = [_]zug.Migration{
    .{
        .id = "001_initial_schema",
        .name = "create actors, fts5, metrics, snapshots, deltas, traffic, tombstones, mod overrides",
        .statements = &.{
            \\CREATE TABLE IF NOT EXISTS actors (
            \\  did TEXT PRIMARY KEY,
            \\  handle TEXT NOT NULL DEFAULT '',
            \\  display_name TEXT DEFAULT '',
            \\  avatar_url TEXT DEFAULT '',
            \\  updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
            \\  hidden INTEGER NOT NULL DEFAULT 0,
            \\  labels TEXT NOT NULL DEFAULT '[]',
            \\  created_at TEXT DEFAULT '',
            \\  associated TEXT DEFAULT '{}',
            \\  pds TEXT DEFAULT '',
            \\  identity_checked_at INTEGER DEFAULT 0,
            \\  profile_checked_at INTEGER DEFAULT 0
            \\)
            ,
            "CREATE INDEX IF NOT EXISTS idx_actors_handle ON actors(handle COLLATE NOCASE)",
            "CREATE INDEX IF NOT EXISTS idx_actors_hidden ON actors(hidden) WHERE hidden != 0",
            "CREATE INDEX IF NOT EXISTS idx_actors_updated_at ON actors(updated_at, did)",
            "CREATE INDEX IF NOT EXISTS idx_actors_enrich_identity ON actors(identity_checked_at) WHERE handle = ''",
            "CREATE INDEX IF NOT EXISTS idx_actors_enrich_pds ON actors(identity_checked_at) WHERE handle != '' AND pds = ''",
            "CREATE INDEX IF NOT EXISTS idx_actors_enrich_profile ON actors(profile_checked_at) WHERE handle != '' AND (avatar_url = '' OR labels = '[]' OR created_at = '')",

            \\CREATE VIRTUAL TABLE IF NOT EXISTS actors_fts USING fts5(
            \\  handle, display_name,
            \\  content='actors', content_rowid='rowid',
            \\  tokenize='unicode61 remove_diacritics 2'
            \\)
            ,
            // FTS5 maintenance triggers — kept as single statements (each
            // contains internal `;` inside its BEGIN…END body).
            \\CREATE TRIGGER IF NOT EXISTS actors_ai AFTER INSERT ON actors BEGIN
            \\  INSERT INTO actors_fts(rowid, handle, display_name)
            \\  VALUES (new.rowid, new.handle, new.display_name);
            \\END
            ,
            \\CREATE TRIGGER IF NOT EXISTS actors_ad AFTER DELETE ON actors BEGIN
            \\  INSERT INTO actors_fts(actors_fts, rowid, handle, display_name)
            \\  VALUES ('delete', old.rowid, old.handle, old.display_name);
            \\END
            ,
            \\CREATE TRIGGER IF NOT EXISTS actors_au AFTER UPDATE ON actors
            \\  WHEN old.handle <> new.handle OR old.display_name <> new.display_name
            \\BEGIN
            \\  INSERT INTO actors_fts(actors_fts, rowid, handle, display_name)
            \\  VALUES ('delete', old.rowid, old.handle, old.display_name);
            \\  INSERT INTO actors_fts(rowid, handle, display_name)
            \\  VALUES (new.rowid, new.handle, new.display_name);
            \\END
            ,

            \\CREATE TABLE IF NOT EXISTS metrics (
            \\  hour INTEGER PRIMARY KEY,
            \\  searches INTEGER NOT NULL DEFAULT 0,
            \\  total_ms REAL NOT NULL DEFAULT 0,
            \\  cache_hits INTEGER NOT NULL DEFAULT 0,
            \\  cache_ms REAL NOT NULL DEFAULT 0
            \\)
            ,
            \\CREATE TABLE IF NOT EXISTS snapshots (
            \\  hour INTEGER PRIMARY KEY,
            \\  total INTEGER NOT NULL DEFAULT 0,
            \\  with_handles INTEGER NOT NULL DEFAULT 0,
            \\  with_avatars INTEGER NOT NULL DEFAULT 0,
            \\  hidden INTEGER NOT NULL DEFAULT 0
            \\)
            ,
            \\CREATE TABLE IF NOT EXISTS actor_deltas (
            \\  bucket INTEGER PRIMARY KEY,
            \\  actors_delta INTEGER NOT NULL DEFAULT 0,
            \\  handles_delta INTEGER NOT NULL DEFAULT 0,
            \\  avatars_delta INTEGER NOT NULL DEFAULT 0
            \\)
            ,
            \\CREATE TABLE IF NOT EXISTS traffic_sources (
            \\  domain TEXT PRIMARY KEY,
            \\  hits INTEGER NOT NULL DEFAULT 0
            \\)
            ,
            \\CREATE TABLE IF NOT EXISTS tombstones (
            \\  did TEXT PRIMARY KEY,
            \\  deleted_at INTEGER NOT NULL
            \\)
            ,
            "CREATE INDEX IF NOT EXISTS idx_tombstones_deleted_at ON tombstones(deleted_at)",

            \\CREATE TABLE IF NOT EXISTS mod_overrides (
            \\  did TEXT PRIMARY KEY,
            \\  action TEXT NOT NULL,
            \\  reason TEXT DEFAULT '',
            \\  created_at INTEGER NOT NULL DEFAULT (unixepoch())
            \\)
            ,
        },
    },
    // Ranking signals. Populated by worker enrichment (counts) and by the ingest service's
    // /admin/ingest path bumping last_activity_at when a known DID's jetstream
    // event arrives. quality_score is computed offline (future), so it ships
    // as 0 today. Search.zig consumes all of these at scoring time.
    .{
        .id = "002_ranking_columns",
        .name = "add followers/follows/posts/last_activity_at/quality_score for ranking",
        .statements = &.{
            "ALTER TABLE actors ADD COLUMN followers_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE actors ADD COLUMN follows_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE actors ADD COLUMN posts_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE actors ADD COLUMN last_activity_at INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE actors ADD COLUMN quality_score REAL NOT NULL DEFAULT 0",
        },
    },
    .{
        .id = "003_blocked_domains",
        .name = "domain-level moderation blocks (apex + all subdomains)",
        .statements = &.{
            \\CREATE TABLE IF NOT EXISTS blocked_domains (
            \\  domain TEXT PRIMARY KEY,
            \\  reason TEXT NOT NULL DEFAULT '',
            \\  created_at INTEGER NOT NULL DEFAULT (unixepoch())
            \\)
            ,
        },
    },
    // Maintained corpus counters + an empty-avatar index, so the hourly
    // `recordSnapshot` never has to COUNT(*) the whole ~6M-row table (a ~300s
    // scan over libSQL HTTP that silently blew the cron budget once the table
    // grew, freezing /stats from 2026-05-18). `actors_total` is bumped by the
    // worker's recordActorDelta on every ingest/delete; with_handles /
    // with_avatars are derived from cheap selective empty-counts.
    //
    // Fresh DBs seed `actors_total` at 0 (correct — they start empty). The
    // already-populated prod DB is seeded out-of-band to its true row count;
    // the INSERT OR IGNORE below is a no-op there.
    .{
        .id = "004_corpus_counters",
        .name = "maintained actors_total counter + empty-avatar index for cheap snapshots",
        .statements = &.{
            \\CREATE TABLE IF NOT EXISTS counters (
            \\  name TEXT PRIMARY KEY,
            \\  value INTEGER NOT NULL DEFAULT 0
            \\)
            ,
            "INSERT OR IGNORE INTO counters (name, value) VALUES ('actors_total', 0)",
            "CREATE INDEX IF NOT EXISTS idx_actors_avatar_empty ON actors(avatar_url) WHERE avatar_url = ''",
        },
    },
    .{
        .id = "005_quality_score_visible_scored_index",
        .name = "index visible scored actors for ranking percentiles",
        .statements = &.{
            "CREATE INDEX IF NOT EXISTS idx_actors_visible_quality_score ON actors(quality_score) WHERE handle != '' AND hidden = 0 AND quality_score > 0",
        },
    },
    // profile-card fields served by getProfiles. Turso-only by design: neither
    // LocalDb.actor_cols nor the snapshot builder select them, so App B's
    // replica and the index don't grow.
    .{
        .id = "006_profile_card_fields",
        .name = "description + banner_url on actors for getProfiles",
        .statements = &.{
            "ALTER TABLE actors ADD COLUMN description TEXT DEFAULT ''",
            "ALTER TABLE actors ADD COLUMN banner_url TEXT DEFAULT ''",
        },
    },
    // Which appview a row's profile fields were last written from: 'bsky',
    // 'blacksky' (Bluesky answered AccountTakedown; Blacksky's appview still
    // serves the profile, counts included) or 'pds' (record only, no counts).
    // Follower counts differ between appviews, so a rank is only comparable
    // with its source known. Turso-only, like 006.
    .{
        .id = "007_profile_source",
        .name = "profile_source on actors",
        .statements = &.{
            "ALTER TABLE actors ADD COLUMN profile_source TEXT NOT NULL DEFAULT ''",
        },
    },
};
