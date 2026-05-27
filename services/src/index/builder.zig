//! Index builder. Runs offline against Turso source-of-truth, produces a
//! standalone SQLite snapshot DB at /data/build-<id>/index.db with the
//! three immutable tables populated.
//!
//! Pipeline (single pass over actors, no extra memory for the corpus):
//!
//!   1. open index.db, init schema, set pragmas, ATTACH a scratch staging.db
//!   2. stream rows from Turso actors (or a local export via INDEX_REUSE_SOURCE)
//!   3. per row: pack actor_compact blob, write to actor_compact_base (index.db)
//!              normalize handle + display, derive prefix keys,
//!              emit (key, did, score) rows to stage.emissions (staging.db)
//!   4. sort stage.emissions by (key, score DESC) via an index in staging.db
//!   5. iterate sorted emissions, group by key in-stream, take tier-aware
//!      top-N, pack as did_list blob, write to prefix_top_base (index.db)
//!   6. DETACH + delete staging.db, write index_meta, ANALYZE, close
//!
//! Staging lives in a SEPARATE attached DB, never in index.db. emissions
//! (~144M rows, ~8GB) + its index (~3GB) are pure scratch; in index.db they
//! ballooned the shipped artifact to ~12GB of unreclaimed free pages. Deleting
//! the scratch file frees them with no VACUUM, so index.db ships at its real
//! ~2GB size. The CREATE INDEX sort is file-backed (temp_store=FILE) and
//! steered onto the build volume via SQLITE_TMPDIR, not held in RAM.
//!
//! No in-memory accumulation of emissions or top-N lists across keys —
//! everything streams. Memory usage is bounded by one key's top-N
//! candidate buffer (≤300 entries × ~32 bytes ≈ 10 KB at peak) plus the
//! 200MB page cache — flat regardless of corpus size.

const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;

const TursoClient = @import("../db/TursoClient.zig");
const schema = @import("schema.zig");
const normalize = @import("normalize.zig");
const prefixes = @import("prefixes.zig");
const pack = @import("pack.zig");
const scoring = @import("scoring.zig");
const manifest = @import("manifest.zig");
const r2 = @import("r2.zig");

const log = std.log.scoped(.indexer);
const io = std.Options.debug_io;

// std.c omits setenv in 0.16's curated libc list (getenv is present).
// Declared here so we can steer SQLite's temp_store=FILE scratch onto the
// build volume via SQLITE_TMPDIR.
extern "c" fn setenv(name: [*:0]const u8, value: [*:0]const u8, overwrite: c_int) c_int;

/// Rows per Turso roundtrip during the streaming fetch. Matches the
/// pattern in `sync.zig`'s bulk pagination. 2000 is small enough that
/// each batch is ≤ ~1 MB JSON over the wire and decode is fast, but
/// big enough to amortize roundtrip latency.
const BATCH_SIZE: usize = 2000;

/// Source for the actor row stream. `.auto` picks the best available
/// path. `export_http` (GET /info + /export/{gen} → local SQLite, then
/// ATTACH) is the primary — ~60× faster than the v3 cursor for bulk
/// reads. `hrana_v2_paginated` stays as a fallback for one cycle.
pub const SourceMode = enum {
    auto,
    export_http,
    hrana_v2_paginated,
};

pub const Config = struct {
    /// Filesystem root where build-<id>/ directories are created.
    /// Indexer machine's volume mount.
    build_root: []const u8 = "/data",
    /// 0 = unlimited (full corpus); >0 = LIMIT N actors (sampling).
    sample_limit: usize = 1000,
    /// Where the actor stream comes from. See SourceMode.
    source_mode: SourceMode = .auto,
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
        .source_mode = parseSourceMode(cGetenv("INDEX_SOURCE")),
        .top_n_short = envUsize("INDEX_TOP_N_SHORT", 300),
        .top_n_medium = envUsize("INDEX_TOP_N_MEDIUM", 200),
        .top_n_long = envUsize("INDEX_TOP_N_LONG", 100),
        .overlay_top_n = envUsize("INDEX_OVERLAY_TOP_N", 100),
        .hydrate_cap = envUsize("INDEX_HYDRATE_CAP", 50),
        .builder_version = cGetenv("INDEXER_VERSION") orelse "dev",
    };
}

fn parseSourceMode(s: ?[]const u8) SourceMode {
    const v = s orelse return .auto;
    if (std.mem.eql(u8, v, "auto")) return .auto;
    if (std.mem.eql(u8, v, "export") or std.mem.eql(u8, v, "export_http")) return .export_http;
    if (std.mem.eql(u8, v, "hrana_v2") or std.mem.eql(u8, v, "hrana_v2_paginated")) return .hrana_v2_paginated;
    log.warn("INDEX_SOURCE='{s}' unrecognized; defaulting to auto", .{v});
    return .auto;
}

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return std.mem.span(p);
    return null;
}

fn envUsize(name: [*:0]const u8, default: usize) usize {
    const v = cGetenv(name) orelse return default;
    return std.fmt.parseInt(usize, v, 10) catch default;
}

/// Truthy env flag: unset / "" / "0" / "false" / "no" → false, anything else
/// → true. So `INDEX_UPLOAD_R2=0` actually disables upload — a bare presence
/// check would treat "0" as on and surprise a dry-run.
fn envFlag(name: [*:0]const u8) bool {
    const v = cGetenv(name) orelse return false;
    if (v.len == 0 or std.mem.eql(u8, v, "0")) return false;
    if (std.ascii.eqlIgnoreCase(v, "false") or std.ascii.eqlIgnoreCase(v, "no")) return false;
    return true;
}

const BuildStats = struct {
    /// Number of rows the builder advanced past in Turso. Used for the
    /// cursor (sample_limit, progress logs) and to know we've reached the
    /// watermark's tail. NOT the same as rows in actor_compact_base —
    /// hidden actors are skipped by the builder but still counted here so
    /// the cursor can advance past them.
    rows_seen: usize = 0,
    /// Number of rows ACTUALLY written to actor_compact_base. This is what
    /// the manifest contract calls `actor_count` and what /health should
    /// report. Excludes hidden actors and any rows where packActor failed.
    actor_count: usize = 0,
    emission_count: usize = 0,
    prefix_key_count: usize = 0,
    /// CJK n: keys emitted (any key with a non-ASCII byte) and actors that
    /// emitted ≥1. Manifest-reported; the promote gate floors them for
    /// v2-cjk snapshots so a CJK-blind build can't silently attach.
    cjk_key_count: usize = 0,
    cjk_actor_count: usize = 0,
    build_start_ms: i64 = 0,
    build_end_ms: i64 = 0,
    /// max(updated_at) seen in Turso actors at build start. Persisted to
    /// index_meta so the serving layer can answer "how stale is this
    /// snapshot" without re-querying Turso.
    source_watermark: i64 = 0,
    phases: Phases = .{},
};

/// Per-phase cumulative time. Bracketed via `nowMs()` at coarse boundaries
/// (one bracket pair around each logical phase, not per-row) so the
/// overhead of the timestamp syscall stays well under 1% of total build
/// cost. The breakdown is what tells us whether build-time growth is
/// driven by Turso fetch, emission inserts, or the group/pack pass.
const Phases = struct {
    turso_fetch_ms: i64 = 0,
    derive_ms: i64 = 0, // normalize + deriveAll per actor (cumulative)
    actor_insert_ms: i64 = 0, // pack + INSERT actor_compact_base (cumulative)
    emission_insert_ms: i64 = 0, // INSERT emissions inner loop (cumulative)
    index_build_ms: i64 = 0, // CREATE INDEX idx_emissions_key_score
    group_pack_ms: i64 = 0, // buildPrefixTopBase whole pass
    meta_ms: i64 = 0, // writeMeta
    analyze_ms: i64 = 0, // ANALYZE
    manifest_ms: i64 = 0, // sha256 + write manifest.json
};

fn nowMs() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_ms));
}

fn nowSeconds() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
}

/// SQLite single-quoted string literal: wrap in '' and double any
/// embedded single quote. Writes into `buf`, returns the quoted slice
/// (including the surrounding quotes). Defensive for paths interpolated
/// into ATTACH — our build dirs don't contain quotes, but this is
/// becoming general infrastructure.
fn sqlQuote(buf: []u8, s: []const u8) ![]const u8 {
    var w: usize = 0;
    if (w >= buf.len) return error.PathTooLong;
    buf[w] = '\'';
    w += 1;
    for (s) |c| {
        if (c == '\'') {
            if (w + 2 > buf.len) return error.PathTooLong;
            buf[w] = '\'';
            buf[w + 1] = '\'';
            w += 2;
        } else {
            if (w + 1 > buf.len) return error.PathTooLong;
            buf[w] = c;
            w += 1;
        }
    }
    if (w >= buf.len) return error.PathTooLong;
    buf[w] = '\'';
    w += 1;
    return buf[0..w];
}

/// Delete a build's source.db + sidecars. Called on a SUCCESSFUL export
/// build to reclaim ~3+ GB. On failure we leave them so an operator can
/// inspect / re-run with INDEX_REUSE_SOURCE.
fn deleteSourceFiles(build_dir: []const u8) void {
    const names = [_][]const u8{ "source.db", "source.db-wal", "source.db-shm", "source.db.part" };
    for (names) |name| {
        var path_buf: [320]u8 = undefined;
        const p = std.fmt.bufPrint(&path_buf, "{s}/{s}", .{ build_dir, name }) catch continue;
        std.Io.Dir.deleteFile(.cwd(), io, p) catch |err| switch (err) {
            error.FileNotFound => {},
            else => log.warn("deleteSourceFiles({s}) failed: {s}", .{ p, @errorName(err) }),
        };
    }
}

/// Delete a build's staging.db + sidecars after DETACH. The scratch DB
/// holds the ~11GB emissions table + its index; dropping the file frees it
/// without a VACUUM rewrite of index.db.
fn deleteStagingFiles(build_dir: []const u8) void {
    const names = [_][]const u8{ "staging.db", "staging.db-journal", "staging.db-wal", "staging.db-shm" };
    for (names) |name| {
        var path_buf: [320]u8 = undefined;
        const p = std.fmt.bufPrint(&path_buf, "{s}/{s}", .{ build_dir, name }) catch continue;
        std.Io.Dir.deleteFile(.cwd(), io, p) catch |err| switch (err) {
            error.FileNotFound => {},
            else => log.warn("deleteStagingFiles({s}) failed: {s}", .{ p, @errorName(err) }),
        };
    }
}

/// File size in bytes, 0 if it can't be opened. Used for build observability
/// (final index.db size, peak staging footprint).
fn fileSize(path: []const u8) u64 {
    const f = std.Io.Dir.openFileAbsolute(io, path, .{}) catch return 0;
    defer f.close(io);
    const st = f.stat(io) catch return 0;
    return st.size;
}

/// Peak resident set size (VmHWM) from /proc/self/status, in kB. 0 when
/// unavailable (macOS has no such file). The CREATE INDEX sort is the
/// memory high-water mark, so this is the number that sizes the machine.
fn peakRssKb() u64 {
    const f = std.Io.Dir.openFileAbsolute(io, "/proc/self/status", .{}) catch return 0;
    defer f.close(io);
    var buf: [4096]u8 = undefined;
    const n = f.readStreaming(io, &.{&buf}) catch return 0;
    const content = buf[0..n];
    const idx = std.mem.indexOf(u8, content, "VmHWM:") orelse return 0;
    var rest = content[idx + "VmHWM:".len ..];
    var i: usize = 0;
    while (i < rest.len and (rest[i] == ' ' or rest[i] == '\t')) : (i += 1) {}
    rest = rest[i..];
    var j: usize = 0;
    while (j < rest.len and rest[j] >= '0' and rest[j] <= '9') : (j += 1) {}
    return std.fmt.parseInt(u64, rest[0..j], 10) catch 0;
}

/// Fetch max(updated_at) from Turso actors. Used as source_watermark
/// in index_meta. Cheap (single aggregate) compared to the streaming
/// pass. Used by the hrana fallback path; the export path reads the
/// watermark from the local snapshot DB instead.
fn readSourceWatermark(turso: *TursoClient) !i64 {
    var result = try turso.query("SELECT COALESCE(MAX(updated_at), 0) FROM actors", &.{});
    defer result.deinit();
    if (result.rows.len == 0) return 0;
    return result.rows[0].int(0);
}

/// Resolve `.auto` to the preferred source: export_http. v2 paginated
/// stays available as an explicit fallback via INDEX_SOURCE=hrana_v2.
fn chooseSourceMode(cfg: Config) SourceMode {
    return switch (cfg.source_mode) {
        .auto => .export_http,
        .export_http => .export_http,
        .hrana_v2_paginated => .hrana_v2_paginated,
    };
}

/// Random 16-bit hex suffix appended to the unix-second timestamp in the
/// build_id. Two builds starting in the same wall-clock second would
/// otherwise collide and contaminate each other's per-build directory.
/// Crash recovery in the same second after a failed build is the more
/// realistic case; the suffix makes it impossible to step on a stale
/// dir even then.
fn randomHex4(io_: std.Io, buf: *[4]u8) void {
    var raw: [2]u8 = undefined;
    io_.random(&raw);
    const hex = "0123456789abcdef";
    buf[0] = hex[(raw[0] >> 4) & 0xF];
    buf[1] = hex[raw[0] & 0xF];
    buf[2] = hex[(raw[1] >> 4) & 0xF];
    buf[3] = hex[raw[1] & 0xF];
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

    // build_id = "b<unix_seconds>-<16-bit hex random>". The random suffix
    // makes accidental same-second collisions (mostly a retry-after-crash
    // scenario) impossible; without it, the retry would land in an
    // existing dir with stale partial state and INSERT into emissions
    // without conflict resolution (duplicating rows).
    var id_buf: [32]u8 = undefined;
    var rand_buf: [4]u8 = undefined;
    randomHex4(io, &rand_buf);
    const build_id = try std.fmt.bufPrint(&id_buf, "b{d}-{s}", .{ nowSeconds(), rand_buf });

    // Create the build directory atomically (mkdir -p semantics; ignore EEXIST).
    var dir_buf: [256]u8 = undefined;
    const build_dir = try std.fmt.bufPrint(&dir_buf, "{s}/build-{s}", .{ cfg.build_root, build_id });
    std.Io.Dir.createDirPath(.cwd(), io, build_dir) catch |err| {
        log.err("createDirPath({s}) failed: {}", .{ build_dir, err });
        return err;
    };

    // Point SQLite's file-backed temp scratch (temp_store=FILE, set below)
    // at the build dir. build_dir lives on the Fly volume (build_root
    // defaults to /data), so the CREATE INDEX sort over ~144M emission rows
    // spills to the volume instead of the root fs / RAM. setenv copies the
    // value, so the stack buffer can go out of scope safely.
    {
        var tmp_z_buf: [260]u8 = undefined;
        if (build_dir.len + 1 <= tmp_z_buf.len) {
            @memcpy(tmp_z_buf[0..build_dir.len], build_dir);
            tmp_z_buf[build_dir.len] = 0;
            const tmp_z: [*:0]const u8 = tmp_z_buf[0..build_dir.len :0];
            _ = setenv("SQLITE_TMPDIR", tmp_z, 1);
            _ = setenv("TMPDIR", tmp_z, 1);
        }
    }

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

    // Build-time pragmas. journal_mode=DELETE matters: the snapshot
    // artifact transport ships ONLY index.db (no .db-wal sidecar). WAL
    // mode would leave committed pages in index.db-wal until a checkpoint,
    // so the search service fetching just index.db would silently get an incomplete
    // snapshot. With DELETE, every COMMIT flushes through to the main
    // file and the rollback journal is removed at COMMIT end.
    // synchronous=OFF skips fsync — a half-built snapshot is discarded
    // anyway (manifest hasn't been promoted), so torn writes don't matter.
    _ = conn.exec("PRAGMA journal_mode = DELETE", .{}) catch {};
    _ = conn.exec("PRAGMA synchronous = OFF", .{}) catch {};
    _ = conn.exec("PRAGMA temp_store = FILE", .{}) catch {}; // sort scratch → SQLITE_TMPDIR (volume), not RAM
    _ = conn.exec("PRAGMA cache_size = -200000", .{}) catch {}; // 200MB page cache for the build only

    try schema.initSnapshot(conn);

    // Staging lives in a SEPARATE attached DB, never in index.db. The
    // emissions table (~144M rows, ~8GB) plus its (key, score DESC) index
    // (~3GB) are pure build scratch. Kept in index.db they ballooned the
    // shipped artifact to ~12GB of mostly-free pages — DROP TABLE without
    // VACUUM never reclaims them. With a scratch DB we DETACH + delete it
    // at the end: index.db only ever holds the real artifact (~2GB), and we
    // never pay a VACUUM rewrite.
    var staging_buf: [320]u8 = undefined;
    const staging_path = try std.fmt.bufPrint(&staging_buf, "{s}/staging.db", .{build_dir});
    {
        var q_buf: [400]u8 = undefined;
        const q = try sqlQuote(&q_buf, staging_path);
        var sql_buf: [480]u8 = undefined;
        const attach_sql = try std.fmt.bufPrint(&sql_buf, "ATTACH DATABASE {s} AS stage", .{q});
        try conn.exec(attach_sql, .{});
    }
    // Scratch DB is throwaway: no rollback journal, no fsync. A crash just
    // restarts the whole build from a fresh dir, so durability is moot.
    _ = conn.exec("PRAGMA stage.journal_mode = OFF", .{}) catch {};
    _ = conn.exec("PRAGMA stage.synchronous = OFF", .{}) catch {};
    try conn.exec(
        \\CREATE TABLE stage.emissions (
        \\  key   TEXT NOT NULL,
        \\  did   TEXT NOT NULL,
        \\  score REAL NOT NULL
        \\)
    , .{});

    // ── stream actors + emit ──────────────────────────────────────────────
    //
    // Source dispatch. export_http is primary (GET /info + /export →
    // local SQLite checkpoint, ATTACH, iterate — ~60× faster than the
    // hrana cursor for bulk reads). hrana_v2_paginated is the fallback.
    const effective_mode = chooseSourceMode(cfg);
    log.info("source mode: {s}", .{@tagName(effective_mode)});

    switch (effective_mode) {
        .export_http => {
            // Dev affordance: build from an existing local SQLite file,
            // no Turso creds or network needed. Useful for iterating on
            // the build phase without re-downloading 3+ GB.
            if (cGetenv("INDEX_REUSE_SOURCE")) |reuse| {
                log.info("INDEX_REUSE_SOURCE set — skipping export, using {s}", .{reuse});
                try buildFromAttached(allocator, conn, reuse, cfg, &stats);
            } else {
                var turso = try TursoClient.init(allocator);
                defer turso.deinit();
                try streamFromExport(allocator, &turso, conn, build_dir, cfg, &stats);
            }
        },
        .hrana_v2_paginated => {
            var turso = try TursoClient.init(allocator);
            defer turso.deinit();
            stats.source_watermark = readSourceWatermark(&turso) catch |err| blk: {
                log.warn("source_watermark fetch failed: {s}; defaulting to 0", .{@errorName(err)});
                break :blk 0;
            };
            log.info("source_watermark: {d}", .{stats.source_watermark});
            try streamActorsAndEmit(allocator, &turso, conn, cfg, &stats);
        },
        .auto => unreachable, // chooseSourceMode resolves auto
    }

    // Source is fully consumed once emissions are staged (buildFromAttached
    // detaches src on return). Reclaim its ~3GB now, before the disk-heavy
    // CREATE INDEX, so peak volume usage stays lower. Skipped in reuse mode
    // — that file isn't ours to delete.
    if (effective_mode == .export_http and cGetenv("INDEX_REUSE_SOURCE") == null) {
        deleteSourceFiles(build_dir);
    }

    // ── group, pack, write prefix_top_base (per-group scratch arena inside) ──
    {
        const t = nowMs();
        // did is in the index so the group/pack scan is index-ONLY (covering).
        // Without it, the (key,score) index orders the scan but SQLite must do
        // a rowid lookup into the ~8GB emissions table for did on every one of
        // ~144M rows — random I/O that thrashed the volume for hours at full
        // scale. Covering it turns the scan into a sequential index read.
        try conn.exec("CREATE INDEX IF NOT EXISTS stage.idx_emissions_key_score ON emissions(key, score DESC, did)", .{});
        stats.phases.index_build_ms = nowMs() - t;
    }
    {
        const t = nowMs();
        try buildPrefixTopBase(allocator, conn, cfg, &stats);
        stats.phases.group_pack_ms = nowMs() - t;
    }

    // ── cleanup + metadata ────────────────────────────────────────────────
    // Peak disk footprint: staging (emissions+index) + index.db coexist here,
    // just before we drop staging. Logged for machine/volume sizing.
    log.info("peak artifact footprint: staging.db={d}B index.db={d}B", .{ fileSize(staging_path), fileSize(db_path) });
    // Discard staging entirely: DETACH then delete the file. Frees the
    // ~11GB of emissions+index instantly with no VACUUM rewrite, and
    // guarantees index.db ships at real-artifact size.
    conn.exec("DETACH DATABASE stage", .{}) catch |err| {
        log.warn("DETACH stage failed: {s}", .{@errorName(err)});
    };
    deleteStagingFiles(build_dir);
    {
        const t = nowMs();
        try writeMeta(conn, build_id, cfg, &stats);
        stats.phases.meta_ms = nowMs() - t;
    }

    {
        const t = nowMs();
        _ = conn.exec("ANALYZE", .{}) catch {};
        stats.phases.analyze_ms = nowMs() - t;
    }
    // No VACUUM needed: staging never lived in index.db, so there are no
    // freed pages to reclaim — index.db is already real-artifact size.

    // Manifest LAST: index.db is final + ANALYZE'd. Streams its sha256 (the
    // ~30-60s pass over the artifact is counted in elapsed). The search service reads this
    // to gate promotion before downloading. R2 upload (Phase 2) consumes it.
    {
        const t = nowMs();
        const m = try manifest.write(allocator, .{
            .build_id = build_id,
            .db_path = db_path,
            .build_dir = build_dir,
            .promoted_at = nowSeconds(),
            .source_watermark = stats.source_watermark,
            .actor_count = stats.actor_count,
            .prefix_count = stats.prefix_key_count,
            .top_n_short = cfg.top_n_short,
            .top_n_medium = cfg.top_n_medium,
            .top_n_long = cfg.top_n_long,
            .overlay_top_n = cfg.overlay_top_n,
            .hydrate_cap = cfg.hydrate_cap,
            .builder_version = cfg.builder_version,
            .cjk_key_count = stats.cjk_key_count,
            .cjk_actor_count = stats.cjk_actor_count,
        });
        stats.phases.manifest_ms = nowMs() - t;
        log.info("manifest written: byte_size={d} sha256={s}", .{ m.byte_size, m.sha256_hex[0..16] });
    }

    stats.build_end_ms = nowMs();
    const elapsed_ms = stats.build_end_ms - stats.build_start_ms;
    const p = stats.phases;
    log.info("indexer done: build_id={s} actors={d} emissions={d} prefix_keys={d} cjk_keys={d} cjk_actors={d} elapsed_ms={d}", .{
        build_id,
        stats.actor_count,
        stats.emission_count,
        stats.prefix_key_count,
        stats.cjk_key_count,
        stats.cjk_actor_count,
        elapsed_ms,
    });
    log.info(
        "phase ms: turso_fetch={d} derive={d} actor_insert={d} emission_insert={d} index_build={d} group_pack={d} meta={d} analyze={d} manifest={d}",
        .{ p.turso_fetch_ms, p.derive_ms, p.actor_insert_ms, p.emission_insert_ms, p.index_build_ms, p.group_pack_ms, p.meta_ms, p.analyze_ms, p.manifest_ms },
    );
    log.info("indexer resources: index_db_bytes={d} peak_rss_kb={d}", .{ fileSize(db_path), peakRssKb() });

    // Publish to R2 (opt-in via truthy INDEX_UPLOAD_R2 so local/sample/dry
    // builds don't upload). On failure we log loudly but don't discard the
    // on-disk build — a retry/manual upload can still promote it.
    if (envFlag("INDEX_UPLOAD_R2")) {
        uploadArtifacts(allocator, build_id, db_path, build_dir) catch |err|
            log.err("R2 upload failed: {s}", .{@errorName(err)});
    } else {
        log.info("R2 upload skipped (INDEX_UPLOAD_R2 not truthy); build on disk at {s}", .{build_dir});
    }
}

/// Upload index.db + manifest.json under the build prefix, then copy the
/// manifest to the latest key LAST — so the search service never sees a
/// latest pointing at an index.db that isn't fully present.
///
/// CHANNEL ISOLATION: prod search polls `latest.json` under `builds/`. A
/// scratch/staging build MUST set `INDEX_BUILD_PREFIX` (e.g.
/// `staging/cjk-search/`) AND `INDEX_LATEST_KEY` (e.g.
/// `latest.cjk-search.json`) so it never overwrites prod's `latest.json`.
fn uploadArtifacts(gpa: Allocator, build_id: []const u8, db_path: []const u8, build_dir: []const u8) !void {
    const cfg = try r2.configure(gpa);
    const prefix = cGetenv("INDEX_BUILD_PREFIX") orelse "builds/";
    const latest_key = cGetenv("INDEX_LATEST_KEY") orelse "latest.json";

    var kbuf: [320]u8 = undefined;
    const db_key = try std.fmt.bufPrint(&kbuf, "{s}{s}/index.db", .{ prefix, build_id });
    try r2.upload(gpa, cfg, db_path, db_key);

    var mpath_buf: [320]u8 = undefined;
    const manifest_local = try std.fmt.bufPrint(&mpath_buf, "{s}/manifest.json", .{build_dir});
    var mkey_buf: [320]u8 = undefined;
    const m_key = try std.fmt.bufPrint(&mkey_buf, "{s}{s}/manifest.json", .{ prefix, build_id });
    try r2.upload(gpa, cfg, manifest_local, m_key);

    try r2.upload(gpa, cfg, manifest_local, latest_key);
    log.info("R2 publish complete: {s} -> {s}{s}/index.db", .{ latest_key, prefix, build_id });
}

/// Primary source path. Export Turso → local SQLite checkpoint file via
/// the export HTTP endpoints (TursoClient.exportToFile), ATTACH it
/// read-only, validate, then iterate `src.actors` from local disk.
/// source_watermark comes from the exported file's own MAX(updated_at)
/// — the exact watermark of this checkpoint snapshot. The exported file
/// is deleted after a successful build.
fn streamFromExport(
    gpa: Allocator,
    turso: *TursoClient,
    snapshot: zqlite.Conn,
    build_dir: []const u8,
    cfg: Config,
    stats: *BuildStats,
) !void {
    var path_buf: [320]u8 = undefined;
    const source_path = try std.fmt.bufPrint(&path_buf, "{s}/source.db", .{build_dir});

    const t_export = nowMs();
    try turso.exportToFile(source_path);
    stats.phases.turso_fetch_ms = nowMs() - t_export;
    log.info("export complete in {d}ms → {s}", .{ stats.phases.turso_fetch_ms, source_path });

    try buildFromAttached(gpa, snapshot, source_path, cfg, stats);
}

/// ATTACH a local SQLite file as `src`, validate it, then iterate
/// `src.actors` into the snapshot. Split out so the export path and the
/// INDEX_REUSE_SOURCE dev path share the build logic.
fn buildFromAttached(
    gpa: Allocator,
    snapshot: zqlite.Conn,
    source_path: []const u8,
    cfg: Config,
    stats: *BuildStats,
) !void {
    // Plain-path ATTACH (NOT a file: URI). The snapshot connection isn't
    // opened with SQLITE_OPEN_URI, so a 'file:...?mode=ro' string would
    // be treated as a literal filename and fail. We never write to src
    // (all writes target the main snapshot tables), so read-write attach
    // is harmless. Path is SQL-quoted (single quotes doubled) so it's
    // safe even if a build dir ever contains one.
    var quoted_buf: [512]u8 = undefined;
    const quoted_path = try sqlQuote(&quoted_buf, source_path);
    var attach_buf: [600]u8 = undefined;
    const attach_sql = try std.fmt.bufPrint(&attach_buf, "ATTACH DATABASE {s} AS src", .{quoted_path});
    try snapshot.exec(attach_sql, .{});
    defer snapshot.exec("DETACH DATABASE src", .{}) catch {};

    try validateExportedDb(snapshot);

    // source_watermark from the exported file — the checkpoint's own cutoff.
    {
        const row = try snapshot.row("SELECT COALESCE(MAX(updated_at), 0) FROM src.actors", .{});
        if (row) |r| {
            defer r.deinit();
            stats.source_watermark = r.int(0);
        }
        log.info("source_watermark from exported DB: {d}", .{stats.source_watermark});
    }

    var sctx = StreamCtx{
        .gpa = gpa,
        .stats = stats,
        .row_arena = std.heap.ArenaAllocator.init(gpa),
        .stmt_actor = undefined,
        .stmt_emit = undefined,
    };
    defer sctx.row_arena.deinit();

    sctx.stmt_actor = try snapshot.prepare(
        "INSERT OR REPLACE INTO actor_compact_base (did, blob) VALUES (?, ?)",
    );
    defer sctx.stmt_actor.deinit();
    sctx.stmt_emit = try snapshot.prepare(
        "INSERT INTO stage.emissions (key, did, score) VALUES (?, ?, ?)",
    );
    defer sctx.stmt_emit.deinit();

    try snapshot.exec("BEGIN", .{});
    errdefer _ = snapshot.exec("ROLLBACK", .{}) catch {};

    var rows = if (cfg.sample_limit == 0)
        try snapshot.rows("SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM src.actors WHERE handle != '' ORDER BY did", .{})
    else
        try snapshot.rows("SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM src.actors WHERE handle != '' ORDER BY did LIMIT ?", .{cfg.sample_limit});
    defer rows.deinit();

    while (rows.next()) |row| {
        try processActorRow(&sctx, row);
        stats.rows_seen += 1;
        if (stats.rows_seen - sctx.progress_at >= 50_000) {
            sctx.progress_at = stats.rows_seen;
            log.info("indexer progress (export): rows_seen={d} actors={d} emissions={d}", .{
                stats.rows_seen, stats.actor_count, stats.emission_count,
            });
        }
    }

    try snapshot.exec("COMMIT", .{});
    log.info("emissions phase done (export): rows_seen={d} actors={d} emissions={d} watermark={d}", .{
        stats.rows_seen, stats.actor_count, stats.emission_count, stats.source_watermark,
    });
}

/// Validate the exported DB before building from it. Cheap structural
/// checks: quick_check (catches a torn/garbage file — belt-and-suspenders
/// on top of the .part→rename guard) and a plausible actor count.
fn validateExportedDb(snapshot: zqlite.Conn) !void {
    {
        const row = snapshot.row("PRAGMA src.quick_check", .{}) catch |err| {
            log.err("exported DB quick_check errored: {s}", .{@errorName(err)});
            return error.ExportValidationFailed;
        };
        if (row) |r| {
            defer r.deinit();
            const result = r.text(0);
            if (!std.mem.eql(u8, result, "ok")) {
                log.err("exported DB quick_check: {s}", .{result});
                return error.ExportCorrupt;
            }
        } else return error.ExportValidationFailed;
    }
    {
        const row = try snapshot.row("SELECT COUNT(*) FROM src.actors WHERE handle != ''", .{});
        if (row) |r| {
            defer r.deinit();
            const count = r.int(0);
            if (count < 1) {
                log.err("exported DB has no searchable actors", .{});
                return error.ExportEmptyActors;
            }
            log.info("exported DB validated: {d} searchable actors", .{count});
        } else return error.ExportEmptyActors;
    }
}

/// Per-stream state threaded through processActorRow.
const StreamCtx = struct {
    gpa: Allocator,
    stats: *BuildStats,
    row_arena: std.heap.ArenaAllocator,
    stmt_actor: zqlite.Stmt,
    stmt_emit: zqlite.Stmt,
    progress_at: usize = 0,
};

/// Process one actor row. Per-row arena reset keeps memory flat. `row`
/// is `anytype` so this works for both zqlite rows (export path, reading
/// the ATTACHed file) and any other row type exposing text/int/float.
fn processActorRow(ctx: *StreamCtx, row: anytype) !void {
    defer _ = ctx.row_arena.reset(.retain_capacity);
    const aa = ctx.row_arena.allocator();

    const did = row.text(0);
    if (did.len == 0) return;

    const raw_handle = row.text(1);
    const raw_display = row.text(2);
    const avatar = row.text(3);
    const labels_json = row.text(4);
    const pds = row.text(5);
    const created_at = row.text(6);
    const associated_json = row.text(7);
    const hidden = row.int(8) != 0;
    const quality_score = row.float(9);

    if (hidden) return;

    const t_actor = nowMs();
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
        return;
    };
    try ctx.stmt_actor.reset();
    try ctx.stmt_actor.bindValue(did, 0);
    try ctx.stmt_actor.bindValue(zqlite.blob(blob), 1);
    try ctx.stmt_actor.stepToCompletion();
    ctx.stats.phases.actor_insert_ms += nowMs() - t_actor;
    ctx.stats.actor_count += 1;

    const t_derive = nowMs();
    const norm_handle = try normalize.handle(aa, raw_handle);
    const norm_display = try normalize.display(aa, raw_display);
    const keys = try prefixes.deriveAllUnique(aa, norm_handle, norm_display);
    ctx.stats.phases.derive_ms += nowMs() - t_derive;

    const score: f32 = scoring.scoreOf(.{ .quality_score = @floatCast(quality_score) });

    const t_emit = nowMs();
    var actor_has_cjk = false;
    for (keys) |k| {
        try ctx.stmt_emit.reset();
        try ctx.stmt_emit.bindValue(k, 0);
        try ctx.stmt_emit.bindValue(did, 1);
        try ctx.stmt_emit.bindValue(@as(f64, @floatCast(score)), 2);
        try ctx.stmt_emit.stepToCompletion();
        ctx.stats.emission_count += 1;
        // CJK keys (display-name bigrams) carry non-ASCII bytes; ASCII h:/n:/
        // e:/d: keys never do. Count them for the manifest + promote floor.
        for (k) |b| {
            if (b >= 0x80) {
                ctx.stats.cjk_key_count += 1;
                actor_has_cjk = true;
                break;
            }
        }
    }
    if (actor_has_cjk) ctx.stats.cjk_actor_count += 1;
    ctx.stats.phases.emission_insert_ms += nowMs() - t_emit;
}

/// gpa is used for long-lived allocations (the per-actor scratch arena's
/// backing, the last-DID cursor buffer). The per-actor scratch arena
/// resets after each row so peak memory stays flat at full corpus scale
/// instead of growing with N actors.
///
/// Pagination: keyset cursor on `did` with batch size BATCH_SIZE. Each
/// batch is a fresh Turso roundtrip + a fresh local transaction, so a
/// crash mid-build loses at most one batch of work. The
/// `updated_at <= source_watermark` filter pins the snapshot to a
/// consistent point-in-time — actors written after the watermark are
/// excluded from this build and pick up in the next one. Without this
/// filter, an actor renamed during pagination could appear in the
/// snapshot under both old and new handles.
fn streamActorsAndEmit(
    gpa: Allocator,
    turso: *TursoClient,
    snapshot: zqlite.Conn,
    cfg: Config,
    stats: *BuildStats,
) !void {
    const stmt_actor = try snapshot.prepare(
        "INSERT OR REPLACE INTO actor_compact_base (did, blob) VALUES (?, ?)",
    );
    defer stmt_actor.deinit();

    const stmt_emit = try snapshot.prepare(
        "INSERT INTO stage.emissions (key, did, score) VALUES (?, ?, ?)",
    );
    defer stmt_emit.deinit();

    // Per-row scratch arena lives across batches; reset per actor inside
    // the inner loop.
    var row_arena = std.heap.ArenaAllocator.init(gpa);
    defer row_arena.deinit();
    const aa = row_arena.allocator();

    // GPA-backed buffer for the cursor: row.text() slices borrow from
    // turso's parsed JSON, which is freed at result.deinit() each batch.
    // We dupe the last DID after each batch so the next iteration's
    // `did > ?` bind has a valid pointer.
    var last_did_buf: std.ArrayListUnmanaged(u8) = .empty;
    defer last_did_buf.deinit(gpa);

    var watermark_buf: [24]u8 = undefined;
    const watermark_str = try std.fmt.bufPrint(&watermark_buf, "{d}", .{stats.source_watermark});

    var batch_idx: usize = 0;
    paginate: while (true) {
        // Honor cfg.sample_limit by capping this batch's LIMIT.
        const remaining: usize = if (cfg.sample_limit == 0)
            BATCH_SIZE
        else if (stats.rows_seen >= cfg.sample_limit)
            break :paginate
        else
            @min(BATCH_SIZE, cfg.sample_limit - stats.rows_seen);

        var limit_buf: [16]u8 = undefined;
        const limit_str = try std.fmt.bufPrint(&limit_buf, "{d}", .{remaining});

        const t_fetch = nowMs();
        var result = try turso.query(
            "SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM actors WHERE handle != '' AND updated_at <= ? AND did > ? ORDER BY did LIMIT ?",
            &.{ watermark_str, last_did_buf.items, limit_str },
        );
        stats.phases.turso_fetch_ms += nowMs() - t_fetch;

        if (result.rows.len == 0) {
            result.deinit();
            break :paginate;
        }

        try snapshot.exec("BEGIN", .{});
        errdefer _ = snapshot.exec("ROLLBACK", .{}) catch {};

        var batch_last_did: []const u8 = "";

        for (result.rows) |row| {
            defer _ = row_arena.reset(.retain_capacity);

            const did = row.text(0);
            if (did.len == 0) continue;
            batch_last_did = did;

            const raw_handle = row.text(1);
            const raw_display = row.text(2);
            const avatar = row.text(3);
            const labels_json = row.text(4);
            const pds = row.text(5);
            const created_at = row.text(6);
            const associated_json = row.text(7);
            const hidden = row.int(8) != 0;
            const quality_score = row.float(9);

            // rows_seen advances for cursor / sample_limit / progress;
            // actor_count advances only after we actually write the row.
            stats.rows_seen += 1;

            if (hidden) continue;

            // 1. pack + insert actor blob
            const t_actor = nowMs();
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
            stats.phases.actor_insert_ms += nowMs() - t_actor;
            stats.actor_count += 1; // bumps ONLY after the actor row is in the snapshot DB

            // 2. derive prefix keys, deduped (see commit 36d969f rationale)
            const t_derive = nowMs();
            const norm_handle = try normalize.handle(aa, raw_handle);
            const norm_display = try normalize.display(aa, raw_display);
            const keys = try prefixes.deriveAllUnique(aa, norm_handle, norm_display);
            stats.phases.derive_ms += nowMs() - t_derive;

            const score: f32 = scoring.scoreOf(.{ .quality_score = @floatCast(quality_score) });

            const t_emit = nowMs();
            for (keys) |k| {
                try stmt_emit.reset();
                try stmt_emit.bindValue(k, 0);
                try stmt_emit.bindValue(did, 1);
                try stmt_emit.bindValue(@as(f64, @floatCast(score)), 2);
                try stmt_emit.stepToCompletion();
                stats.emission_count += 1;
            }
            stats.phases.emission_insert_ms += nowMs() - t_emit;
        }

        // Capture rows-len for the short-batch check before deinit
        // invalidates the slice header.
        const this_batch_rows = result.rows.len;

        // Save the last DID into gpa-owned memory BEFORE result.deinit
        // invalidates the slice. Cursor for the next iteration.
        last_did_buf.clearRetainingCapacity();
        try last_did_buf.appendSlice(gpa, batch_last_did);

        result.deinit();
        try snapshot.exec("COMMIT", .{});

        batch_idx += 1;
        log.info("indexer batch {d}: rows_seen={d} actors={d} emissions={d} cursor_did={s}", .{
            batch_idx,
            stats.rows_seen,
            stats.actor_count,
            stats.emission_count,
            last_did_buf.items[0..@min(40, last_did_buf.items.len)],
        });

        // Short-batch sentinel: if Turso returned fewer rows than asked
        // for, we've reached the watermark's tail. No need for another
        // roundtrip just to learn that.
        if (this_batch_rows < remaining) break :paginate;
    }

    log.info("emissions phase done: rows_seen={d} actors={d} emissions={d}", .{
        stats.rows_seen, stats.actor_count, stats.emission_count,
    });
}

fn topNFor(key_len: usize, cfg: Config) usize {
    const inner_len = if (key_len >= 2) key_len - 2 else 0; // strip "h:"/"n:"/"e:" prefix
    if (inner_len <= 3) return cfg.top_n_short;
    if (inner_len <= 6) return cfg.top_n_medium;
    return cfg.top_n_long;
}

fn buildPrefixTopBase(
    gpa: Allocator,
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

    // Sequential scan in (key, score DESC) order. idx_emissions_key_score is
    // a COVERING index (key, score DESC, did) so this is index-only: one
    // ordered pass over the index, no per-row table lookups, no temp B-tree.
    var rows = try snapshot.rows(
        "SELECT key, did, score FROM stage.emissions ORDER BY key, score DESC",
        .{},
    );
    defer rows.deinit();

    // Per-group scratch arena. Holds the current key's duped string, the
    // top-N DidEntry items, and the packed blob. Reset on each key change
    // after the previous group has been flushed to the DB — without this,
    // per-key allocations accumulate across the full key space (1.6M at
    // 100k actors → 10M+ at 5.9M) and the build OOMs late in the pass.
    var group_arena = std.heap.ArenaAllocator.init(gpa);
    defer group_arena.deinit();
    const aa = group_arena.allocator();

    var current_key: ?[]const u8 = null;
    var current_top: std.ArrayListUnmanaged(pack.DidEntry) = .empty;
    var current_top_n: usize = 0;

    // Progress logging: this scan is the longest phase (tens of minutes at
    // full scale on shared CPU) and would otherwise sit silent. Log every
    // 10M rows against the known emission total.
    const total_emissions = stats.emission_count;
    var rows_scanned: u64 = 0;
    var progress_at: u64 = 0;

    while (rows.next()) |row| {
        const key = row.text(0);
        const did = row.text(1);
        const score = row.float(2);

        rows_scanned += 1;
        if (rows_scanned - progress_at >= 10_000_000) {
            progress_at = rows_scanned;
            log.info("group/pack progress: scanned={d}/{d} keys={d}", .{ rows_scanned, total_emissions, stats.prefix_key_count });
        }

        if (current_key == null or !std.mem.eql(u8, current_key.?, key)) {
            // flush previous key group, THEN reset the arena — writeGroup
            // binds the packed blob with SQLITE_STATIC, so the blob must
            // stay valid through stepToCompletion which lives inside it.
            if (current_key) |k| {
                try writeGroup(aa, &stmt_insert, k, current_top.items);
                _ = group_arena.reset(.retain_capacity);
                current_top = .empty;
            }
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
    try schema.setMeta(conn, schema.meta.source_watermark, try std.fmt.bufPrint(&buf, "{d}", .{stats.source_watermark}));
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
    try schema.setMeta(conn, schema.meta.cjk_key_count, try std.fmt.bufPrint(&buf, "{d}", .{stats.cjk_key_count}));
    try schema.setMeta(conn, schema.meta.cjk_actor_count, try std.fmt.bufPrint(&buf, "{d}", .{stats.cjk_actor_count}));
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

// Regression guard: the group/pack scan MUST be index-only. The index has to
// include `did` (the third selected column) or SQLite does a rowid lookup
// into the multi-GB emissions table per row — ~144M random reads that
// thrashed the volume for hours at full scale. If someone drops `did` from
// the index, this catches it: the plan degrades from COVERING INDEX to INDEX.
test "group/pack index is covering (index-only scan)" {
    var conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try conn.exec("CREATE TABLE emissions (key TEXT NOT NULL, did TEXT NOT NULL, score REAL NOT NULL)", .{});
    try conn.exec("CREATE INDEX idx_emissions_key_score ON emissions(key, score DESC, did)", .{});

    var rows = try conn.rows("EXPLAIN QUERY PLAN SELECT key, did, score FROM emissions ORDER BY key, score DESC", .{});
    defer rows.deinit();
    var covering = false;
    while (rows.next()) |row| {
        if (std.mem.indexOf(u8, row.text(3), "COVERING INDEX") != null) covering = true;
    }
    try testing.expect(covering);
}
