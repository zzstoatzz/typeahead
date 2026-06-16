//! Local schema setup for the prefix index. NOT a Zug migration — these
//! tables live in two LOCAL SQLite databases (the indexer's per-build
//! snapshot file and the search service's overlay DB) and never touch Turso. Init
//! runs idempotently every time we open either DB.
//!
//! Snapshot DB layout (indexer writes once, search service opens via ATTACH AS idx):
//!   prefix_top_base       — top-N DIDs per prefix key (immutable)
//!   actor_compact_base    — packed presentation blob per DID (immutable)
//!   index_meta            — build metadata (build_id, versions, top_n_*)
//!
//! Overlay DB layout (search service local.db, mutable from the sync loop):
//!   actor_overlay         — newer blob / tombstone wins over base
//!   prefix_overlay        — op=1 adds, op=0 suppresses a base entry
//!
//! See docs/typeahead-index-design.md for the why-it-looks-like-this.

const std = @import("std");
const zqlite = @import("zqlite");

const log = std.log.scoped(.index_schema);

/// Stable table names, exported so call sites elsewhere can format
/// queries against them without retyping the strings.
pub const table = struct {
    pub const prefix_top_base = "prefix_top_base";
    pub const actor_compact_base = "actor_compact_base";
    pub const index_meta = "index_meta";
    pub const actor_overlay = "actor_overlay";
    pub const prefix_overlay = "prefix_overlay";
};

/// index_meta key names. Indexer writes; serving validates a subset on
/// snapshot load. Adding a key here is additive — old snapshots just
/// don't have it. Removing or renaming a key is a breaking change.
pub const meta = struct {
    pub const build_id = "build_id"; // ulid-ish, unique per build
    pub const built_at = "built_at"; // unix seconds
    pub const source_watermark = "source_watermark"; // Turso updated_at at build start
    pub const builder_version = "builder_version"; // git sha of indexer
    pub const normalize_version = "normalize_version"; // mirrors normalize.zig VERSION
    pub const scoring_version = "scoring_version"; // mirrors scoring.zig VERSION
    pub const top_n_short = "top_n_short"; // prefix len 1-3
    pub const top_n_medium = "top_n_medium"; // prefix len 4-6
    pub const top_n_long = "top_n_long"; // prefix len 7+
    pub const overlay_top_n = "overlay_top_n"; // overlay cap per key
    pub const hydrate_cap = "hydrate_cap"; // serving merge cap
    pub const actor_count = "actor_count"; // count emitted by builder
    pub const prefix_count = "prefix_count"; // distinct keys in prefix_top_base
    pub const cjk_key_count = "cjk_key_count"; // v2+ : CJK n: keys emitted
    pub const cjk_actor_count = "cjk_actor_count"; // v2+ : actors with ≥1 CJK key
};

/// Create snapshot tables on the indexer's per-build DB. Idempotent.
/// Called once when the indexer opens a fresh build-<id>/index.db AND
/// every time the search service opens an existing snapshot file (CREATE IF NOT
/// EXISTS makes this safe).
pub fn initSnapshot(conn: zqlite.Conn) !void {
    try conn.exec(
        \\CREATE TABLE IF NOT EXISTS prefix_top_base (
        \\  key   TEXT PRIMARY KEY,
        \\  dids  BLOB NOT NULL
        \\) WITHOUT ROWID
    , .{});

    try conn.exec(
        \\CREATE TABLE IF NOT EXISTS actor_compact_base (
        \\  did   TEXT PRIMARY KEY,
        \\  blob  BLOB NOT NULL
        \\) WITHOUT ROWID
    , .{});

    try conn.exec(
        \\CREATE TABLE IF NOT EXISTS index_meta (
        \\  key   TEXT PRIMARY KEY,
        \\  value TEXT
        \\)
    , .{});
}

/// Create overlay tables on the search service DB (its existing local.db).
/// Idempotent. Called from LocalDb.openDb after the existing schema runs.
/// `source` distinguishes sync-derived overlay (0) from operator overrides (1).
/// Compaction-on-promote prunes only source=0 rows ≤ the new snapshot
/// watermark; operator rows survive forever (a manual hide/show must not vanish
/// when a snapshot promotes). blob=NULL in actor_overlay is a global DID
/// tombstone — suppresses the candidate regardless of any prefix_overlay row.
pub const overlay_source = struct {
    pub const sync: i64 = 0;
    pub const operator: i64 = 1;
};

pub fn initOverlay(conn: zqlite.Conn) !void {
    try conn.exec(
        \\CREATE TABLE IF NOT EXISTS actor_overlay (
        \\  did         TEXT PRIMARY KEY,
        \\  blob        BLOB,
        \\  hidden      INTEGER NOT NULL,
        \\  updated_at  INTEGER NOT NULL,
        \\  source      INTEGER NOT NULL DEFAULT 0
        \\) WITHOUT ROWID
    , .{});

    try conn.exec(
        \\CREATE TABLE IF NOT EXISTS prefix_overlay (
        \\  key         TEXT NOT NULL,
        \\  did         TEXT NOT NULL,
        \\  score       REAL NOT NULL,
        \\  op          INTEGER NOT NULL,
        \\  updated_at  INTEGER NOT NULL,
        \\  source      INTEGER NOT NULL DEFAULT 0,
        \\  PRIMARY KEY (key, did)
        \\) WITHOUT ROWID
    , .{});

    // additive column-add for any DB created before `source` existed.
    conn.exec("ALTER TABLE actor_overlay ADD COLUMN source INTEGER NOT NULL DEFAULT 0", .{}) catch {};
    conn.exec("ALTER TABLE prefix_overlay ADD COLUMN source INTEGER NOT NULL DEFAULT 0", .{}) catch {};

    // updated_at indexes back the compaction prune (delete rows ≤ watermark).
    try conn.exec(
        \\CREATE INDEX IF NOT EXISTS idx_prefix_overlay_updated_at
        \\  ON prefix_overlay(updated_at)
    , .{});
    try conn.exec(
        \\CREATE INDEX IF NOT EXISTS idx_actor_overlay_updated_at
        \\  ON actor_overlay(updated_at)
    , .{});
}

// ─── meta helpers ────────────────────────────────────────────────────────

/// Upsert an index_meta row. Call from the indexer once per metadata key
/// when finishing a build. Values are stored as TEXT (caller stringifies).
pub fn setMeta(conn: zqlite.Conn, key: []const u8, value: []const u8) !void {
    try conn.exec(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        .{ key, value },
    );
}

/// Fetch an index_meta row. Returns null if the key is absent. Caller
/// owns the slice memory after the call returns (we dupe out of zqlite's
/// row scope before the row goes away).
pub fn getMeta(conn: zqlite.Conn, allocator: std.mem.Allocator, key: []const u8) !?[]const u8 {
    const maybe_row = conn.row("SELECT value FROM index_meta WHERE key = ?", .{key}) catch |err| {
        log.err("getMeta({s}) failed: {s}", .{ key, @errorName(err) });
        return err;
    };
    if (maybe_row) |row| {
        defer row.deinit();
        const v = row.text(0);
        return try allocator.dupe(u8, v);
    }
    return null;
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

fn openMem() !zqlite.Conn {
    return try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
}

test "initSnapshot creates all three tables idempotently" {
    var conn = try openMem();
    defer conn.close();

    try initSnapshot(conn);
    try initSnapshot(conn); // idempotent: second call must not throw

    // sqlite_master query confirms tables exist
    const names = [_][]const u8{ "prefix_top_base", "actor_compact_base", "index_meta" };
    for (names) |name| {
        const row = try conn.row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            .{name},
        );
        try testing.expect(row != null);
        if (row) |r| r.deinit();
    }
}

test "initSnapshot tables are WITHOUT ROWID" {
    var conn = try openMem();
    defer conn.close();

    try initSnapshot(conn);

    // WITHOUT ROWID tables show "CREATE TABLE ... WITHOUT ROWID" in sqlite_master.sql
    for (&[_][]const u8{ "prefix_top_base", "actor_compact_base" }) |name| {
        const row = try conn.row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            .{name},
        );
        try testing.expect(row != null);
        if (row) |r| {
            defer r.deinit();
            const sql = r.text(0);
            try testing.expect(std.mem.indexOf(u8, sql, "WITHOUT ROWID") != null);
        }
    }
    // index_meta is intentionally rowid-backed: it's tiny and we never
    // care about its physical layout.
    const meta_row = try conn.row(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'index_meta'",
        .{},
    );
    try testing.expect(meta_row != null);
    if (meta_row) |r| {
        defer r.deinit();
        const sql = r.text(0);
        try testing.expect(std.mem.indexOf(u8, sql, "WITHOUT ROWID") == null);
    }
}

test "initOverlay creates tables + index idempotently" {
    var conn = try openMem();
    defer conn.close();

    try initOverlay(conn);
    try initOverlay(conn);

    const tables = [_][]const u8{ "actor_overlay", "prefix_overlay" };
    for (tables) |name| {
        const row = try conn.row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            .{name},
        );
        try testing.expect(row != null);
        if (row) |r| r.deinit();
    }

    const idx = try conn.row(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_prefix_overlay_updated_at'",
        .{},
    );
    try testing.expect(idx != null);
    if (idx) |r| r.deinit();
}

test "setMeta/getMeta roundtrip" {
    var conn = try openMem();
    defer conn.close();
    try initSnapshot(conn);

    try setMeta(conn, meta.build_id, "01J2X3FAKE");
    try setMeta(conn, meta.built_at, "1779600000");
    try setMeta(conn, meta.normalize_version, "v1-ascii-latin1");

    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const got = try getMeta(conn, arena.allocator(), meta.build_id);
    try testing.expect(got != null);
    try testing.expectEqualStrings("01J2X3FAKE", got.?);

    const missing = try getMeta(conn, arena.allocator(), "no_such_key");
    try testing.expect(missing == null);
}

test "setMeta overwrites existing value" {
    var conn = try openMem();
    defer conn.close();
    try initSnapshot(conn);

    try setMeta(conn, meta.build_id, "first");
    try setMeta(conn, meta.build_id, "second");

    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const got = try getMeta(conn, arena.allocator(), meta.build_id);
    try testing.expectEqualStrings("second", got.?);
}

test "table name constants reflect actual created tables" {
    var conn = try openMem();
    defer conn.close();
    try initSnapshot(conn);
    try initOverlay(conn);

    inline for (.{
        table.prefix_top_base,
        table.actor_compact_base,
        table.index_meta,
        table.actor_overlay,
        table.prefix_overlay,
    }) |name| {
        const row = try conn.row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            .{name},
        );
        try testing.expect(row != null);
        if (row) |r| r.deinit();
    }
}

test "meta key constants are non-empty and stable" {
    // Catch typos at compile time; serve as docs for the contract.
    inline for (.{
        meta.build_id,        meta.built_at,          meta.source_watermark,
        meta.builder_version, meta.normalize_version, meta.scoring_version,
        meta.top_n_short,     meta.top_n_medium,      meta.top_n_long,
        meta.overlay_top_n,   meta.hydrate_cap,       meta.actor_count,
        meta.prefix_count,    meta.cjk_key_count,     meta.cjk_actor_count,
    }) |k| {
        try testing.expect(k.len > 0);
    }
}
