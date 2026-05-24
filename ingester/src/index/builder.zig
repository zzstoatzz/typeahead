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

/// Rows per Turso roundtrip during the streaming fetch. Matches the
/// pattern in `sync.zig`'s bulk pagination. 2000 is small enough that
/// each batch is ≤ ~1 MB JSON over the wire and decode is fast, but
/// big enough to amortize roundtrip latency.
const BATCH_SIZE: usize = 2000;

/// Source for the actor row stream. `.auto` picks the best available
/// path; explicit values force a specific source. v3 cursor is the
/// primary; v2 paginated is the fallback for one cycle before deletion.
pub const SourceMode = enum {
    auto,
    hrana_v3_cursor,
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
    if (std.mem.eql(u8, v, "cursor") or std.mem.eql(u8, v, "hrana_v3") or std.mem.eql(u8, v, "hrana_v3_cursor")) return .hrana_v3_cursor;
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
};

fn nowMs() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_ms));
}

fn nowSeconds() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
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

/// Resolve `.auto` to the preferred source. v3 cursor first; v2
/// paginated stays around as a fallback override via env.
fn chooseSourceMode(cfg: Config) SourceMode {
    return switch (cfg.source_mode) {
        .auto => .hrana_v3_cursor,
        .hrana_v3_cursor => .hrana_v3_cursor,
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
    // so App B fetching just index.db would silently get an incomplete
    // snapshot. With DELETE, every COMMIT flushes through to the main
    // file and the rollback journal is removed at COMMIT end.
    // synchronous=OFF skips fsync — a half-built snapshot is discarded
    // anyway (manifest hasn't been promoted), so torn writes don't matter.
    _ = conn.exec("PRAGMA journal_mode = DELETE", .{}) catch {};
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

    // ── stream actors + emit ──────────────────────────────────────────────
    //
    // Source dispatch. Hrana v2 keyset pagination is the only path that
    // exists right now; the v3 HTTP cursor path lands in a follow-up
    // commit (see docs/hrana-v3-cursor-plan.md). chooseSourceMode lets
    // us flip between them via env without changing the dispatcher
    // surface here.
    const effective_mode = chooseSourceMode(cfg);
    log.info("source mode: {s}", .{@tagName(effective_mode)});

    switch (effective_mode) {
        .hrana_v3_cursor => {
            var turso = try TursoClient.init(allocator);
            defer turso.deinit();
            try streamFromCursor(allocator, &turso, conn, cfg, &stats);
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

    // ── group, pack, write prefix_top_base (per-group scratch arena inside) ──
    {
        const t = nowMs();
        try conn.exec("CREATE INDEX IF NOT EXISTS idx_emissions_key_score ON emissions(key, score DESC)", .{});
        stats.phases.index_build_ms = nowMs() - t;
    }
    {
        const t = nowMs();
        try buildPrefixTopBase(allocator, conn, cfg, &stats);
        stats.phases.group_pack_ms = nowMs() - t;
    }

    // ── cleanup + metadata ────────────────────────────────────────────────
    try conn.exec("DROP TABLE IF EXISTS emissions", .{});
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
    // VACUUM is not free — leave it for a follow-up if size matters.

    stats.build_end_ms = nowMs();
    const elapsed_ms = stats.build_end_ms - stats.build_start_ms;
    const p = stats.phases;
    log.info("indexer done: build_id={s} actors={d} emissions={d} prefix_keys={d} elapsed_ms={d}", .{
        build_id,
        stats.actor_count,
        stats.emission_count,
        stats.prefix_key_count,
        elapsed_ms,
    });
    log.info(
        "phase ms: turso_fetch={d} derive={d} actor_insert={d} emission_insert={d} index_build={d} group_pack={d} meta={d} analyze={d}",
        .{ p.turso_fetch_ms, p.derive_ms, p.actor_insert_ms, p.emission_insert_ms, p.index_build_ms, p.group_pack_ms, p.meta_ms, p.analyze_ms },
    );
}

/// Source path via hrana v3 cursor — one POST /v3/cursor request whose
/// NDJSON response streams every actor row + the source watermark in a
/// single transactional snapshot.
///
/// Batch shape:
///   step 0: SELECT max(updated_at)  → captures the watermark
///   step 1: SELECT did, handle, ... → streams every actor
///
/// Both steps execute within the same cursor (one server-side read
/// transaction), so the watermark is the actual cutoff for what's in
/// the row stream — no race window like the v2 paginated path needed.
fn streamFromCursor(
    gpa: Allocator,
    turso: *TursoClient,
    snapshot: zqlite.Conn,
    cfg: Config,
    stats: *BuildStats,
) !void {
    var sctx = StreamCtx{
        .gpa = gpa,
        .cfg = cfg,
        .snapshot = snapshot,
        .stats = stats,
        .row_arena = std.heap.ArenaAllocator.init(gpa),
        .current_step = -1,
        .stmt_actor = undefined,
        .stmt_emit = undefined,
    };
    defer sctx.row_arena.deinit();

    sctx.stmt_actor = try snapshot.prepare(
        "INSERT OR REPLACE INTO actor_compact_base (did, blob) VALUES (?, ?)",
    );
    defer sctx.stmt_actor.deinit();

    sctx.stmt_emit = try snapshot.prepare(
        "INSERT INTO emissions (key, did, score) VALUES (?, ?, ?)",
    );
    defer sctx.stmt_emit.deinit();

    try snapshot.exec("BEGIN", .{});
    errdefer _ = snapshot.exec("ROLLBACK", .{}) catch {};

    // Build the actors SELECT — include a SQL-level LIMIT in sample
    // mode so the server stops pumping after N rows. Without this, the
    // cursor reads the full corpus and the callback just no-ops past
    // sample_limit. Production runs use sample_limit=0 (no LIMIT clause).
    var actors_sql_buf: [512]u8 = undefined;
    const actors_sql = if (cfg.sample_limit == 0)
        @as([]const u8, "SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM actors WHERE handle != '' ORDER BY did")
    else
        try std.fmt.bufPrint(&actors_sql_buf, "SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM actors WHERE handle != '' ORDER BY did LIMIT {d}", .{cfg.sample_limit});

    const stmts = [_]TursoClient.CursorStmt{
        .{ .sql = "SELECT COALESCE(MAX(updated_at), 0) FROM actors WHERE handle != ''" },
        .{ .sql = actors_sql },
    };

    const t_stream = nowMs();
    try turso.streamCursor(gpa, &stmts, &sctx, processCursorEntry);
    stats.phases.turso_fetch_ms = nowMs() - t_stream;

    try snapshot.exec("COMMIT", .{});

    log.info("emissions phase done (cursor): rows_seen={d} actors={d} emissions={d} watermark={d}", .{
        stats.rows_seen,
        stats.actor_count,
        stats.emission_count,
        stats.source_watermark,
    });
}

/// Per-cursor-stream state threaded through the entry callback.
const StreamCtx = struct {
    gpa: Allocator,
    cfg: Config,
    snapshot: zqlite.Conn,
    stats: *BuildStats,
    row_arena: std.heap.ArenaAllocator,
    /// Which step's rows we're currently processing.
    /// -1 before any step_begin; 0 = watermark; 1 = actors.
    current_step: i32,
    stmt_actor: zqlite.Stmt,
    stmt_emit: zqlite.Stmt,
    progress_at: usize = 0,
};

fn processCursorEntry(ctx: *StreamCtx, entry: TursoClient.CursorEntry) !void {
    switch (entry) {
        .step_begin => |sb| {
            ctx.current_step = @intCast(sb.step);
        },
        .step_end => |se| {
            _ = se;
        },
        .row => |r| {
            if (ctx.current_step == 0) {
                // step 0: SELECT max(updated_at) — single row, single column.
                ctx.stats.source_watermark = r.int(0);
                log.info("source_watermark (from cursor step 0): {d}", .{ctx.stats.source_watermark});
            } else if (ctx.current_step == 1) {
                // Honor sample_limit by silently skipping past N rows.
                // We can't cancel the cursor mid-stream cleanly, so the
                // server still pushes the full result; we just stop
                // processing. Production runs use sample_limit=0.
                if (ctx.cfg.sample_limit > 0 and ctx.stats.rows_seen >= ctx.cfg.sample_limit) return;

                try processActorRow(ctx, r);
                ctx.stats.rows_seen += 1;

                if (ctx.stats.rows_seen - ctx.progress_at >= 50_000) {
                    ctx.progress_at = ctx.stats.rows_seen;
                    log.info("indexer progress (cursor): rows_seen={d} actors={d} emissions={d}", .{
                        ctx.stats.rows_seen,
                        ctx.stats.actor_count,
                        ctx.stats.emission_count,
                    });
                }
            }
        },
        .step_error => |e| {
            log.err("cursor step {d} error: {s}", .{ e.step, e.message });
            return error.TursoCursorStepError;
        },
        .err => |e| {
            log.err("cursor error: {s}", .{e.message});
            return error.TursoCursorError;
        },
    }
}

/// Process one actor row (step 1 of the cursor batch). Same per-row
/// arena reset pattern as the v2 path. The TursoClient.Row reference
/// borrows from the per-line JSON parse, which lives only for the
/// duration of this call.
fn processActorRow(ctx: *StreamCtx, row: TursoClient.Row) !void {
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
    for (keys) |k| {
        try ctx.stmt_emit.reset();
        try ctx.stmt_emit.bindValue(k, 0);
        try ctx.stmt_emit.bindValue(did, 1);
        try ctx.stmt_emit.bindValue(@as(f64, @floatCast(score)), 2);
        try ctx.stmt_emit.stepToCompletion();
        ctx.stats.emission_count += 1;
    }
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
        "INSERT INTO emissions (key, did, score) VALUES (?, ?, ?)",
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

    // Sequential scan in (key, score DESC) order — idx_emissions_key_score
    // backs this with O(n) work and no temp B-tree.
    var rows = try snapshot.rows(
        "SELECT key, did, score FROM emissions ORDER BY key, score DESC",
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

    while (rows.next()) |row| {
        const key = row.text(0);
        const did = row.text(1);
        const score = row.float(2);

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
