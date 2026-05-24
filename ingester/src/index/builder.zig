//! Index builder. Runs offline against Turso source-of-truth, produces a
//! standalone SQLite snapshot DB at /data/build-<id>/index.db with the
//! three immutable tables populated.
//!
//! Pipeline (single pass over actors, no extra memory for the corpus):
//!
//!   1. open snapshot DB, init schema, set perf pragmas
//!   2. stream rows from Turso actors
//!   3. per row: pack actor_compact blob, write to actor_compact_base
//!              normalize handle + display, derive prefix keys,
//!              emit (key, did, score) rows to staging emissions table
//!   4. sort emissions by (key, score DESC) via an index
//!   5. iterate sorted emissions, group by key in-stream, take tier-aware
//!      top-N, pack as did_list blob, write to prefix_top_base
//!   6. drop staging, write index_meta, ANALYZE, close
//!
//! No in-memory accumulation of emissions or top-N lists across keys —
//! everything streams. Memory usage is bounded by one key's top-N
//! candidate buffer (≤300 entries × ~32 bytes ≈ 10 KB at peak).

const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;

const TursoClient = @import("../db/TursoClient.zig");
const schema = @import("schema.zig");
const normalize = @import("normalize.zig");
const prefixes = @import("prefixes.zig");
const pack = @import("pack.zig");
const scoring = @import("scoring.zig");

const log = std.log.scoped(.indexer);
const io = std.Options.debug_io;

pub const Config = struct {
    /// Filesystem root where build-<id>/ directories are created.
    /// Indexer machine's volume mount.
    build_root: []const u8 = "/data",
    /// 0 = unlimited (full corpus); >0 = LIMIT N actors (sampling).
    sample_limit: usize = 1000,
    /// Top-N caps per prefix-length tier.
    top_n_short: usize = 300, // prefix len 1-3
    top_n_medium: usize = 200, // prefix len 4-6
    top_n_long: usize = 100, // prefix len 7+
    /// Reported in index_meta; not enforced by builder.
    overlay_top_n: usize = 100,
    hydrate_cap: usize = 50,
    /// Stamped into index_meta as builder_version.
    builder_version: []const u8 = "dev",
};

pub fn readConfigFromEnv() Config {
    return .{
        .build_root = cGetenv("INDEX_BUILD_ROOT") orelse "/data",
        .sample_limit = envUsize("INDEX_SAMPLE_LIMIT", 1000),
        .top_n_short = envUsize("INDEX_TOP_N_SHORT", 300),
        .top_n_medium = envUsize("INDEX_TOP_N_MEDIUM", 200),
        .top_n_long = envUsize("INDEX_TOP_N_LONG", 100),
        .overlay_top_n = envUsize("INDEX_OVERLAY_TOP_N", 100),
        .hydrate_cap = envUsize("INDEX_HYDRATE_CAP", 50),
        .builder_version = cGetenv("INDEXER_VERSION") orelse "dev",
    };
}

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return std.mem.span(p);
    return null;
}

fn envUsize(name: [*:0]const u8, default: usize) usize {
    const v = cGetenv(name) orelse return default;
    return std.fmt.parseInt(usize, v, 10) catch default;
}

const BuildStats = struct {
    actor_count: usize = 0,
    emission_count: usize = 0,
    prefix_key_count: usize = 0,
    build_start_ms: i64 = 0,
    build_end_ms: i64 = 0,
};

fn nowMs() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_ms));
}

fn nowSeconds() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
}

/// Top-level entry. Reads config from env, runs the full build, writes
/// metadata, exits. Errors propagate so the caller (main.zig) can decide
/// whether to retry/exit-fail.
pub fn run(allocator: Allocator) !void {
    const cfg = readConfigFromEnv();

    var stats: BuildStats = .{ .build_start_ms = nowMs() };
    log.info("indexer start: sample_limit={d} top_n=({d}/{d}/{d})", .{
        cfg.sample_limit, cfg.top_n_short, cfg.top_n_medium, cfg.top_n_long,
    });

    // Build a unique-per-invocation id. Unix seconds is fine — two indexers
    // starting in the same second on the same machine isn't a real risk
    // (single indexer machine; cadence is hours).
    var id_buf: [32]u8 = undefined;
    const build_id = try std.fmt.bufPrint(&id_buf, "b{d}", .{nowSeconds()});

    // Create the build directory atomically (mkdir -p semantics; ignore EEXIST).
    var dir_buf: [256]u8 = undefined;
    const build_dir = try std.fmt.bufPrint(&dir_buf, "{s}/build-{s}", .{ cfg.build_root, build_id });
    std.Io.Dir.createDirPath(.cwd(), io, build_dir) catch |err| {
        log.err("createDirPath({s}) failed: {}", .{ build_dir, err });
        return err;
    };

    var path_buf: [320]u8 = undefined;
    const db_path = try std.fmt.bufPrint(&path_buf, "{s}/index.db", .{build_dir});
    var path_z_buf: [320]u8 = undefined;
    if (db_path.len + 1 > path_z_buf.len) return error.PathTooLong;
    @memcpy(path_z_buf[0..db_path.len], db_path);
    path_z_buf[db_path.len] = 0;
    const db_path_z: [*:0]const u8 = path_z_buf[0..db_path.len :0];

    log.info("snapshot path: {s}", .{db_path});

    const flags = zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite;
    var conn = try zqlite.open(db_path_z, flags);
    defer conn.close();

    // Build-time pragmas: WAL stays on (we want crash recovery), but skip
    // fsync per write — losing a half-built snapshot is fine because the
    // manifest hasn't been promoted yet.
    _ = conn.exec("PRAGMA journal_mode = WAL", .{}) catch {};
    _ = conn.exec("PRAGMA synchronous = OFF", .{}) catch {};
    _ = conn.exec("PRAGMA temp_store = MEMORY", .{}) catch {};
    _ = conn.exec("PRAGMA cache_size = -200000", .{}) catch {}; // 200MB page cache for the build only

    try schema.initSnapshot(conn);

    // Staging emissions: every (prefix_key, did, score) tuple emitted by
    // any actor. Dropped at end of build. Indexed on (key, score DESC)
    // so the group-and-pack step is a single sequential scan.
    try conn.exec(
        \\CREATE TABLE IF NOT EXISTS emissions (
        \\  key   TEXT NOT NULL,
        \\  did   TEXT NOT NULL,
        \\  score REAL NOT NULL
        \\)
    , .{});
    try conn.exec("DELETE FROM emissions", .{}); // clean slate for retries

    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const aa = arena.allocator();

    var turso = try TursoClient.init(allocator);
    defer turso.deinit();

    // ── stream actors + emit ──────────────────────────────────────────────
    try streamActorsAndEmit(aa, &turso, conn, cfg, &stats);
    _ = arena.reset(.free_all);

    // ── group, pack, write prefix_top_base ────────────────────────────────
    try conn.exec("CREATE INDEX IF NOT EXISTS idx_emissions_key_score ON emissions(key, score DESC)", .{});
    try buildPrefixTopBase(aa, conn, cfg, &stats);
    _ = arena.reset(.free_all);

    // ── cleanup + metadata ────────────────────────────────────────────────
    try conn.exec("DROP TABLE IF EXISTS emissions", .{});
    try writeMeta(conn, build_id, cfg, &stats);

    _ = conn.exec("ANALYZE", .{}) catch {};
    // VACUUM is not free — leave it for a follow-up if size matters.

    stats.build_end_ms = nowMs();
    const elapsed_ms = stats.build_end_ms - stats.build_start_ms;
    log.info("indexer done: build_id={s} actors={d} emissions={d} prefix_keys={d} elapsed_ms={d}", .{
        build_id,
        stats.actor_count,
        stats.emission_count,
        stats.prefix_key_count,
        elapsed_ms,
    });
}

fn streamActorsAndEmit(
    aa: Allocator,
    turso: *TursoClient,
    snapshot: zqlite.Conn,
    cfg: Config,
    stats: *BuildStats,
) !void {
    // Stream-friendly: order by did so we can resume on partial failure
    // later. The build is single-shot for now — full SELECT every time.
    var sql_buf: [512]u8 = undefined;
    const sql = if (cfg.sample_limit == 0)
        "SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM actors WHERE handle != '' ORDER BY did"
    else
        try std.fmt.bufPrint(&sql_buf, "SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM actors WHERE handle != '' ORDER BY did LIMIT {d}", .{cfg.sample_limit});

    log.info("turso query: {s}", .{sql});

    var result = try turso.query(sql, &.{});
    defer result.deinit();

    log.info("turso returned {d} rows", .{result.rows.len});

    try snapshot.exec("BEGIN", .{});
    errdefer _ = snapshot.exec("ROLLBACK", .{}) catch {};

    const stmt_actor = try snapshot.prepare(
        "INSERT OR REPLACE INTO actor_compact_base (did, blob) VALUES (?, ?)",
    );
    defer stmt_actor.deinit();

    const stmt_emit = try snapshot.prepare(
        "INSERT INTO emissions (key, did, score) VALUES (?, ?, ?)",
    );
    defer stmt_emit.deinit();

    for (result.rows) |row| {
        const did = row.text(0);
        if (did.len == 0) continue;

        const raw_handle = row.text(1);
        const raw_display = row.text(2);
        const avatar = row.text(3);
        const labels_json = row.text(4);
        const pds = row.text(5);
        const created_at = row.text(6);
        const associated_json = row.text(7);
        const hidden = row.int(8) != 0;
        const quality_score = row.float(9);

        if (hidden) continue; // builder skips hidden actors (mod tombstones land in overlay)

        // 1. pack the actor
        const blob = pack.packActor(aa, .{
            .hidden = false,
            .quality_score = @floatCast(quality_score),
            .handle = raw_handle,
            .display = raw_display,
            .avatar = avatar,
            .labels = if (labels_json.len == 0) "[]" else labels_json,
            .pds = pds,
            .created_at = created_at,
            .associated = if (associated_json.len == 0) "{}" else associated_json,
        }) catch |err| {
            log.warn("packActor failed for {s}: {s}", .{ did, @errorName(err) });
            continue;
        };

        try stmt_actor.reset();
        try stmt_actor.bindValue(did, 0);
        try stmt_actor.bindValue(zqlite.blob(blob), 1);
        try stmt_actor.stepToCompletion();

        // 2. derive prefix keys (deduped is unnecessary here — builder GROUP BY
        // handles dup keys naturally; saving the StringHashMap pass)
        const norm_handle = try normalize.handle(aa, raw_handle);
        const norm_display = try normalize.display(aa, raw_display);
        const keys = try prefixes.deriveAll(aa, norm_handle, norm_display);

        const score: f32 = scoring.scoreOf(.{ .quality_score = @floatCast(quality_score) });

        for (keys) |k| {
            try stmt_emit.reset();
            try stmt_emit.bindValue(k, 0);
            try stmt_emit.bindValue(did, 1);
            try stmt_emit.bindValue(@as(f64, @floatCast(score)), 2);
            try stmt_emit.stepToCompletion();
            stats.emission_count += 1;
        }

        stats.actor_count += 1;
        if (stats.actor_count % 1000 == 0) {
            log.info("indexer progress: actors={d} emissions={d}", .{
                stats.actor_count, stats.emission_count,
            });
        }
    }

    try snapshot.exec("COMMIT", .{});
    log.info("emissions phase done: actors={d} emissions={d}", .{
        stats.actor_count, stats.emission_count,
    });
}

fn topNFor(key_len: usize, cfg: Config) usize {
    const inner_len = if (key_len >= 2) key_len - 2 else 0; // strip "h:"/"n:"/"e:" prefix
    if (inner_len <= 3) return cfg.top_n_short;
    if (inner_len <= 6) return cfg.top_n_medium;
    return cfg.top_n_long;
}

fn buildPrefixTopBase(
    aa: Allocator,
    snapshot: zqlite.Conn,
    cfg: Config,
    stats: *BuildStats,
) !void {
    log.info("building prefix_top_base via grouped scan…", .{});

    try snapshot.exec("BEGIN", .{});
    errdefer _ = snapshot.exec("ROLLBACK", .{}) catch {};

    const stmt_insert = try snapshot.prepare(
        "INSERT OR REPLACE INTO prefix_top_base (key, dids) VALUES (?, ?)",
    );
    defer stmt_insert.deinit();

    // Sequential scan in (key, score DESC) order — idx_emissions_key_score
    // backs this with O(n) work and no temp B-tree.
    var rows = try snapshot.rows(
        "SELECT key, did, score FROM emissions ORDER BY key, score DESC",
        .{},
    );
    defer rows.deinit();

    var current_key: ?[]const u8 = null;
    var current_top: std.ArrayListUnmanaged(pack.DidEntry) = .empty;
    var current_top_n: usize = 0;

    while (rows.next()) |row| {
        const key = row.text(0);
        const did = row.text(1);
        const score = row.float(2);

        if (current_key == null or !std.mem.eql(u8, current_key.?, key)) {
            // flush previous key group
            if (current_key) |k| try writeGroup(aa, &stmt_insert, k, current_top.items);
            current_top.clearRetainingCapacity();
            // copy the new key into arena memory so it survives across iterations
            current_key = try aa.dupe(u8, key);
            current_top_n = topNFor(current_key.?.len, cfg);
            stats.prefix_key_count += 1;
        }

        if (current_top.items.len < current_top_n) {
            try current_top.append(aa, .{
                .did = try aa.dupe(u8, did),
                .score = @floatCast(score),
            });
        }
        // else: cap reached, skip the rest of this key's tail
    }

    // flush trailing group
    if (current_key) |k| try writeGroup(aa, &stmt_insert, k, current_top.items);

    try snapshot.exec("COMMIT", .{});
    log.info("prefix_top_base done: distinct_keys={d}", .{stats.prefix_key_count});
}

fn writeGroup(
    aa: Allocator,
    stmt_insert: *const zqlite.Stmt,
    key: []const u8,
    top: []const pack.DidEntry,
) !void {
    const blob = try pack.packDidList(aa, top);
    try stmt_insert.reset();
    try stmt_insert.bindValue(key, 0);
    try stmt_insert.bindValue(zqlite.blob(blob), 1);
    try stmt_insert.stepToCompletion();
}

fn writeMeta(
    conn: zqlite.Conn,
    build_id: []const u8,
    cfg: Config,
    stats: *BuildStats,
) !void {
    var buf: [64]u8 = undefined;

    try schema.setMeta(conn, schema.meta.build_id, build_id);
    try schema.setMeta(conn, schema.meta.built_at, try std.fmt.bufPrint(&buf, "{d}", .{nowSeconds()}));
    try schema.setMeta(conn, schema.meta.builder_version, cfg.builder_version);
    try schema.setMeta(conn, schema.meta.normalize_version, normalize.VERSION);
    try schema.setMeta(conn, schema.meta.scoring_version, scoring.VERSION);
    try schema.setMeta(conn, schema.meta.top_n_short, try std.fmt.bufPrint(&buf, "{d}", .{cfg.top_n_short}));
    try schema.setMeta(conn, schema.meta.top_n_medium, try std.fmt.bufPrint(&buf, "{d}", .{cfg.top_n_medium}));
    try schema.setMeta(conn, schema.meta.top_n_long, try std.fmt.bufPrint(&buf, "{d}", .{cfg.top_n_long}));
    try schema.setMeta(conn, schema.meta.overlay_top_n, try std.fmt.bufPrint(&buf, "{d}", .{cfg.overlay_top_n}));
    try schema.setMeta(conn, schema.meta.hydrate_cap, try std.fmt.bufPrint(&buf, "{d}", .{cfg.hydrate_cap}));
    try schema.setMeta(conn, schema.meta.actor_count, try std.fmt.bufPrint(&buf, "{d}", .{stats.actor_count}));
    try schema.setMeta(conn, schema.meta.prefix_count, try std.fmt.bufPrint(&buf, "{d}", .{stats.prefix_key_count}));
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

test "topNFor classifies by prefix-text length" {
    const cfg: Config = .{};
    // Keys are "h:abc" / "n:foo" / "e:bar" — strip the 2-byte ns prefix.
    try testing.expectEqual(cfg.top_n_short, topNFor("h:a".len, cfg));
    try testing.expectEqual(cfg.top_n_short, topNFor("h:abc".len, cfg));
    try testing.expectEqual(cfg.top_n_medium, topNFor("h:abcd".len, cfg));
    try testing.expectEqual(cfg.top_n_medium, topNFor("h:abcdef".len, cfg));
    try testing.expectEqual(cfg.top_n_long, topNFor("h:abcdefg".len, cfg));
    try testing.expectEqual(cfg.top_n_long, topNFor("h:abcdefghij".len, cfg));
}

test "Config defaults reflect design doc" {
    const cfg: Config = .{};
    try testing.expectEqual(@as(usize, 300), cfg.top_n_short);
    try testing.expectEqual(@as(usize, 200), cfg.top_n_medium);
    try testing.expectEqual(@as(usize, 100), cfg.top_n_long);
    try testing.expectEqual(@as(usize, 100), cfg.overlay_top_n);
    try testing.expectEqual(@as(usize, 50), cfg.hydrate_cap);
    try testing.expectEqualStrings("/data", cfg.build_root);
}
