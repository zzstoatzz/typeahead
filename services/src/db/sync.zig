//! Sync from Turso to local SQLite
//! Full sync on startup, incremental sync every 5 minutes

const std = @import("std");
const mem = std.mem;
const io = std.Options.debug_io;
const zqlite = @import("zqlite");
const logfire = @import("logfire");
const Allocator = std.mem.Allocator;
const TursoClient = @import("TursoClient.zig");
const LocalDb = @import("LocalDb.zig");
const Col = LocalDb.Col;
const overlay = @import("../index/overlay.zig");
const index_schema = @import("../index/schema.zig");

const log = std.log.scoped(.sync);

/// Per-row overlay-projection context, passed into applyActorUpdate/Delete
/// when a delta is newer than the attached snapshot watermark. `arena` is
/// reset by the caller after each row; `updated_at` is the source timestamp.
const OverlayCtx = struct {
    arena: Allocator,
    updated_at: i64,
};

fn timestamp() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
}

fn milliTimestamp() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_ms));
}

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return mem.span(p);
    return null;
}

/// Read SYNC_BOOTSTRAP_PAUSE_MS — sleep this many ms between Turso fetch
/// batches during full bootstrap/resync. Defaults to 0 (no pause, fastest).
/// Set to 100-500ms when bootstrap risks competing with live Turso reads
/// from the worker's fallback path. The trade-off is bootstrap takes longer
/// to complete but live traffic stays responsive.
fn bootstrapPauseMs() u32 {
    const v = cGetenv("SYNC_BOOTSTRAP_PAUSE_MS") orelse return 0;
    return std.fmt.parseInt(u32, v, 10) catch 0;
}

/// Max acceptable replica lag before incremental self-heals via a bulk resync.
/// Default 6h: well under tombstone retention, comfortably above a healthy
/// incremental cadence. Tunable via SYNC_MAX_LAG_S.
fn maxLagS() i64 {
    const v = cGetenv("SYNC_MAX_LAG_S") orelse return 6 * 3600;
    return std.fmt.parseInt(i64, v, 10) catch 6 * 3600;
}

/// Whether to maintain the local actors_fts table. The prefix-index serving
/// path does NOT use FTS (only the snapshot + actors for the moderation belt),
/// so the search service sets ENABLE_LOCAL_FTS=0 — the per-row FTS delete-old
/// was the incremental bottleneck (orphan rows → failing deletes → can't keep
/// up). Default on so non-search roles keep the old local FTS tables if needed.
fn localFtsEnabled() bool {
    const v = cGetenv("ENABLE_LOCAL_FTS") orelse return true;
    return !std.mem.eql(u8, v, "0");
}

/// index of the first column appended after actor_cols (rowid or updated_at)
const trailing_col: usize = Col.quality_score + 1;

const BATCH_SIZE = 2000;
const SYNC_INTERVAL_S = 300; // 5 minutes
const TOMBSTONE_RETENTION_S = 7 * 24 * 3600; // 7 days — must match cron.ts pruning

/// Full sync: fetch all searchable actors from Turso using keyset pagination.
/// Does NOT run stale cleanup — that's handled by incremental sync (tombstones +
/// "became unsearchable" queries). This avoids races with the dual-write path.
/// When `wipe` is true, deletes all local data first (used by stale rebuild path).
pub fn fullSync(turso: *TursoClient, local: *LocalDb, wipe: bool) !void {
    const span = logfire.span("sync.full", .{});
    defer span.end();

    const conn = local.getConn() orelse {
        span.setStatus(.@"error", "LocalNotOpen");
        return error.LocalNotOpen;
    };

    // check if a previous full sync completed successfully
    const was_completed = blk: {
        local.lock();
        defer local.unlock();
        const row = conn.row(
            "SELECT value FROM sync_meta WHERE key = 'sync_complete'",
            .{},
        ) catch break :blk false;
        if (row) |r| {
            defer r.deinit();
            break :blk std.mem.eql(u8, r.text(0), "1");
        }
        break :blk false;
    };

    if (wipe or !was_completed) {
        log.info("full_sync: taking bootstrap path (wipe={}, was_completed={})", .{ wipe, was_completed });
        span.setAttribute("mode", "bootstrap");
        return fullSyncBootstrap(turso, local, conn, wipe);
    }

    // re-sync path: previous sync completed, keep serving while re-syncing
    log.info("full_sync: taking resync path", .{});
    local.setReady(true);
    span.setAttribute("mode", "resync");
    return fullSyncResync(turso, local, conn);
}

/// Bootstrap path: load into staging tables (no indexes), build indexes in bulk,
/// then atomically swap into place. Live schema is never in a degraded state.
fn fullSyncBootstrap(turso: *TursoClient, local: *LocalDb, conn: zqlite.Conn, wipe: bool) !void {
    const span = logfire.span("sync.bootstrap", .{});
    defer span.end();

    const total_t0 = milliTimestamp();
    local.setReady(false);

    // close read connection during bootstrap — not serving traffic,
    // avoids WAL reader lock interference with DDL/index builds
    local.closeReadConn();
    errdefer local.reopenReadConn() catch {};

    if (wipe) {
        local.lock();
        defer local.unlock();
        conn.exec("DELETE FROM sync_meta", .{}) catch {};
    }

    // clean up any leftover staging tables from a previous failed attempt
    {
        local.lock();
        defer local.unlock();
        conn.exec("DROP TABLE IF EXISTS actors_fts_stage", .{}) catch {};
        conn.exec("DROP TABLE IF EXISTS actors_stage", .{}) catch {};
    }

    // create staging table — no PK, no indexes for fast sequential appends
    {
        local.lock();
        defer local.unlock();
        conn.exec(
            \\CREATE TABLE actors_stage (
            \\  did TEXT NOT NULL,
            \\  handle TEXT NOT NULL DEFAULT '',
            \\  display_name TEXT DEFAULT '',
            \\  avatar_url TEXT DEFAULT '',
            \\  hidden INTEGER NOT NULL DEFAULT 0,
            \\  labels TEXT NOT NULL DEFAULT '[]',
            \\  created_at TEXT DEFAULT '',
            \\  associated TEXT DEFAULT '{}',
            \\  pds TEXT DEFAULT '',
            \\  followers_count INTEGER NOT NULL DEFAULT 0,
            \\  follows_count INTEGER NOT NULL DEFAULT 0,
            \\  posts_count INTEGER NOT NULL DEFAULT 0,
            \\  last_activity_at INTEGER NOT NULL DEFAULT 0,
            \\  quality_score REAL NOT NULL DEFAULT 0
            \\)
        , .{}) catch |err| {
            span.recordError(err);
            return err;
        };
    }

    // keyset pagination: fetch rows WHERE rowid > last_rowid ORDER BY rowid
    var actor_count: usize = 0;
    var error_count: usize = 0;
    var last_rowid: i64 = 0;
    var had_turso_error = false;
    const load_t0 = milliTimestamp();
    const pause_ms = bootstrapPauseMs();
    if (pause_ms > 0) {
        log.info("bootstrap: pacing with {d}ms pause between batches", .{pause_ms});
    }

    while (true) {
        var rowid_buf: [20]u8 = undefined;
        const rowid_str = std.fmt.bufPrint(&rowid_buf, "{d}", .{last_rowid}) catch break;

        const t0 = milliTimestamp();

        // fetch from turso (no lock held)
        var result = turso.query(
            "SELECT " ++ LocalDb.actor_cols ++ ", rowid FROM actors WHERE handle != '' AND rowid > ? ORDER BY rowid LIMIT 2000",
            &.{rowid_str},
        ) catch |err| {
            log.err("turso query failed at rowid {d}: {}", .{ last_rowid, err });
            had_turso_error = true;
            break;
        };
        defer result.deinit();

        const t1 = milliTimestamp();

        if (result.rows.len == 0) break;

        // write batch to staging table — no indexes, fast sequential appends
        {
            local.lock();
            defer local.unlock();
            conn.exec("BEGIN", .{}) catch {};
            for (result.rows) |row| {
                insertActorRow(conn, "INSERT INTO actors_stage", row) catch |err| {
                    log.err("insert actor failed: {}", .{err});
                    error_count += 1;
                    continue;
                };
                actor_count += 1;
            }
            conn.exec("COMMIT", .{}) catch {};
        }

        const t2 = milliTimestamp();

        last_rowid = result.rows[result.rows.len - 1].int(trailing_col);

        // high-frequency batch progress — keep on stderr only
        log.info("batch: {d} rows, rowid={d}, total={d} | fetch={d}ms apply={d}ms", .{
            result.rows.len, last_rowid, actor_count,
            t1 - t0,         t2 - t1,
        });

        if (pause_ms > 0) {
            io.sleep(.{ .nanoseconds = @as(u64, pause_ms) * std.time.ns_per_ms }, .real) catch {};
        }
    }

    if (had_turso_error or error_count > 0) {
        span.setAttribute("error_count", @as(i64, @intCast(error_count)));
        span.setStatus(.@"error", "bootstrap incomplete");
        // clean up staging table
        {
            local.lock();
            defer local.unlock();
            conn.exec("DROP TABLE IF EXISTS actors_stage", .{}) catch {};
        }
        // restore read connection so health endpoint works
        local.reopenReadConn() catch {};
        return;
    }

    const load_ms = milliTimestamp() - load_t0;

    // checkpoint WAL before heavy DDL — clean slate for index builds
    {
        local.lock();
        defer local.unlock();
        conn.exec("PRAGMA wal_checkpoint(TRUNCATE)", .{}) catch {};
    }

    // build indexes on staging table
    const idx_did_t0 = milliTimestamp();
    {
        local.lock();
        defer local.unlock();
        conn.exec("CREATE UNIQUE INDEX idx_stage_did ON actors_stage(did)", .{}) catch |err| {
            span.recordError(err);
            conn.exec("DROP TABLE IF EXISTS actors_stage", .{}) catch {};
            return err;
        };
    }
    const idx_did_ms = milliTimestamp() - idx_did_t0;

    const idx_handle_t0 = milliTimestamp();
    {
        local.lock();
        defer local.unlock();
        conn.exec("CREATE INDEX idx_stage_handle ON actors_stage(handle COLLATE NOCASE)", .{}) catch |err| {
            span.recordError(err);
            conn.exec("DROP TABLE IF EXISTS actors_stage", .{}) catch {};
            return err;
        };
    }
    const idx_handle_ms = milliTimestamp() - idx_handle_t0;

    // swap actors table first, then build FTS with final name.
    {
        local.lock();
        defer local.unlock();
        conn.exec("BEGIN", .{}) catch {};
        conn.exec("DROP TABLE IF EXISTS actors_fts", .{}) catch {};
        conn.exec("DROP TABLE IF EXISTS actors", .{}) catch {};
        conn.exec("ALTER TABLE actors_stage RENAME TO actors", .{}) catch |err| {
            span.recordError(err);
            conn.exec("ROLLBACK", .{}) catch {};
            return err;
        };
        conn.exec("COMMIT", .{}) catch |err| {
            span.recordError(err);
            return err;
        };
    }

    // build FTS with its final name — no rename needed. Skipped when
    // ENABLE_LOCAL_FTS=0 (prefix-index serving doesn't use FTS).
    var fts_create_ms: i64 = 0;
    var fts_pop_ms: i64 = 0;
    if (localFtsEnabled()) {
        const fts_create_t0 = milliTimestamp();
        {
            local.lock();
            defer local.unlock();
            conn.exec(
                \\CREATE VIRTUAL TABLE actors_fts USING fts5(
                \\  did UNINDEXED, handle, display_name,
                \\  tokenize='unicode61 remove_diacritics 2'
                \\)
            , .{}) catch |err| {
                span.recordError(err);
                return err;
            };
        }
        fts_create_ms = milliTimestamp() - fts_create_t0;

        const fts_pop_t0 = milliTimestamp();
        {
            local.lock();
            defer local.unlock();
            conn.exec(
                \\INSERT INTO actors_fts (did, handle, display_name)
                \\SELECT did, handle, display_name FROM actors WHERE handle != ''
            , .{}) catch |err| {
                span.recordError(err);
                return err;
            };
        }
        fts_pop_ms = milliTimestamp() - fts_pop_t0;
    } else {
        log.info("bootstrap: FTS disabled (ENABLE_LOCAL_FTS=0), skipping FTS build", .{});
    }

    // post-swap: optimize, record completion + actor count, checkpoint
    {
        local.lock();
        defer local.unlock();
        conn.exec("PRAGMA optimize", .{}) catch {};

        var ts_buf: [20]u8 = undefined;
        const ts_str = std.fmt.bufPrint(&ts_buf, "{d}", .{timestamp()}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync', ?)",
            .{ts_str},
        ) catch {};
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('sync_complete', '1')",
            .{},
        ) catch {};

        var count_buf: [20]u8 = undefined;
        const count_str = std.fmt.bufPrint(&count_buf, "{d}", .{actor_count}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('actor_count', ?)",
            .{count_str},
        ) catch {};

        conn.exec("PRAGMA wal_checkpoint(PASSIVE)", .{}) catch {};
    }

    const total_ms = milliTimestamp() - total_t0;

    // reopen read connection before serving traffic
    local.reopenReadConn() catch {};

    local.setReady(true);
    span.setAttribute("actors", @as(i64, @intCast(actor_count)));
    span.setAttribute("load_ms", load_ms);
    span.setAttribute("idx_did_ms", idx_did_ms);
    span.setAttribute("idx_handle_ms", idx_handle_ms);
    span.setAttribute("fts_create_ms", fts_create_ms);
    span.setAttribute("fts_populate_ms", fts_pop_ms);
    span.setAttribute("total_ms", total_ms);
    span.setStatus(.ok, null);
}

/// Re-sync path: previous sync completed, update live table directly.
/// Only processes actors that may have changed — index maintenance cost is negligible.
fn fullSyncResync(turso: *TursoClient, local: *LocalDb, conn: zqlite.Conn) !void {
    const span = logfire.span("sync.resync", .{});
    defer span.end();

    log.info("resync: starting full table resync", .{});

    var actor_count: usize = 0;
    var error_count: usize = 0;
    var last_rowid: i64 = 0;
    var had_turso_error = false;
    var batch_num: usize = 0;
    const pause_ms = bootstrapPauseMs();
    if (pause_ms > 0) {
        log.info("resync: pacing with {d}ms pause between batches", .{pause_ms});
    }

    while (true) {
        var rowid_buf: [20]u8 = undefined;
        const rowid_str = std.fmt.bufPrint(&rowid_buf, "{d}", .{last_rowid}) catch break;

        const q_span = logfire.span("sync.query.resync_batch", .{});
        const t0 = milliTimestamp();

        var result = turso.query(
            "SELECT " ++ LocalDb.actor_cols ++ ", rowid FROM actors WHERE handle != '' AND rowid > ? ORDER BY rowid LIMIT 2000",
            &.{rowid_str},
        ) catch |err| {
            log.err("resync: turso query failed at rowid {d}: {}", .{ last_rowid, err });
            q_span.setStatus(.@"error", "query failed");
            q_span.end();
            had_turso_error = true;
            break;
        };
        defer result.deinit();

        const t1 = milliTimestamp();
        q_span.setAttribute("rows", @as(i64, @intCast(result.rows.len)));
        q_span.setAttribute("fetch_ms", t1 - t0);
        q_span.setStatus(.ok, null);
        q_span.end();

        if (result.rows.len == 0) break;

        {
            const a_span = logfire.span("sync.apply.resync_batch", .{});
            defer a_span.end();

            local.lock();
            defer local.unlock();
            conn.exec("BEGIN", .{}) catch {};
            for (result.rows) |row| {
                insertActorRow(conn, "INSERT OR REPLACE INTO actors", row) catch |err| {
                    log.err("resync: insert actor failed: {}", .{err});
                    error_count += 1;
                    continue;
                };
                actor_count += 1;
            }
            conn.exec("COMMIT", .{}) catch {};

            a_span.setAttribute("rows", @as(i64, @intCast(result.rows.len)));
            a_span.setStatus(.ok, null);
        }

        const t2 = milliTimestamp();
        last_rowid = result.rows[result.rows.len - 1].int(trailing_col);
        batch_num += 1;

        log.info("resync: batch {d}: {d} rows, rowid={d}, total={d} | fetch={d}ms apply={d}ms", .{
            batch_num, result.rows.len, last_rowid, actor_count,
            t1 - t0,   t2 - t1,
        });

        if (pause_ms > 0) {
            io.sleep(.{ .nanoseconds = @as(u64, pause_ms) * std.time.ns_per_ms }, .real) catch {};
        }
    }

    if (had_turso_error or error_count > 0) {
        span.setAttribute("error_count", @as(i64, @intCast(error_count)));
        span.setStatus(.@"error", "resync incomplete");
        log.err("resync: incomplete (actors={d}, errors={d})", .{ actor_count, error_count });
        return;
    }

    // rebuild FTS from scratch (skipped when ENABLE_LOCAL_FTS=0 — the prefix
    // index path doesn't use FTS, and per-row FTS maintenance was the sync
    // bottleneck on the search service).
    if (localFtsEnabled()) {
        log.info("resync: download complete ({d} actors), rebuilding FTS", .{actor_count});
        const fts_span = logfire.span("sync.fts_rebuild", .{});
        defer fts_span.end();

        local.lock();
        defer local.unlock();
        conn.exec("DROP TABLE IF EXISTS actors_fts", .{}) catch {};
        conn.exec(
            \\CREATE VIRTUAL TABLE actors_fts USING fts5(
            \\  did UNINDEXED, handle, display_name,
            \\  tokenize='unicode61 remove_diacritics 2'
            \\)
        , .{}) catch |err| {
            fts_span.recordError(err);
        };
        conn.exec(
            \\INSERT INTO actors_fts (did, handle, display_name)
            \\SELECT did, handle, display_name FROM actors WHERE handle != ''
        , .{}) catch |err| {
            fts_span.recordError(err);
        };
        fts_span.setAttribute("actors", @as(i64, @intCast(actor_count)));
        log.info("resync: FTS rebuild complete", .{});
    } else {
        log.info("resync: download complete ({d} actors), FTS disabled (skipped)", .{actor_count});
    }

    // record sync time + mark complete + actor count
    {
        local.lock();
        defer local.unlock();
        var ts_buf: [20]u8 = undefined;
        const ts_str = std.fmt.bufPrint(&ts_buf, "{d}", .{timestamp()}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync', ?)",
            .{ts_str},
        ) catch {};
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('sync_complete', '1')",
            .{},
        ) catch {};

        var count_buf: [20]u8 = undefined;
        const count_str = std.fmt.bufPrint(&count_buf, "{d}", .{actor_count}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('actor_count', ?)",
            .{count_str},
        ) catch {};

        conn.exec("PRAGMA wal_checkpoint(PASSIVE)", .{}) catch {};
    }

    local.setReady(true);
    span.setAttribute("actors", @as(i64, @intCast(actor_count)));
    span.setStatus(.ok, null);
    log.info("resync: complete ({d} actors)", .{actor_count});
}

/// Incremental sync: fetch actors updated since last sync + tombstones.
/// Uses keyset pagination on (updated_at, did) to drain all updates.
/// Only advances watermark when all turso queries succeed and all local writes succeed.
pub fn incrementalSync(turso: *TursoClient, local: *LocalDb) !void {
    const span = logfire.span("sync.incremental", .{});
    defer span.end();

    const conn = local.getConn() orelse {
        span.setStatus(.@"error", "LocalNotOpen");
        return error.LocalNotOpen;
    };

    // get last sync time
    const last_sync_ts = blk: {
        const s = logfire.span("sync.read_last_sync", .{});
        defer s.end();
        local.lock();
        defer local.unlock();
        const row = conn.row(
            "SELECT value FROM sync_meta WHERE key = 'last_sync'",
            .{},
        ) catch {
            s.setStatus(.@"error", "query failed");
            break :blk @as(i64, 0);
        };
        if (row) |r| {
            defer r.deinit();
            const val = r.text(0);
            const ts = if (val.len == 0) 0 else std.fmt.parseInt(i64, val, 10) catch 0;
            s.setAttribute("last_sync", ts);
            s.setStatus(.ok, null);
            break :blk ts;
        }
        s.setStatus(.ok, null);
        break :blk @as(i64, 0);
    };

    log.info("incremental: last_sync={d}", .{last_sync_ts});

    if (last_sync_ts == 0) {
        log.info("incremental: no last_sync, falling back to full sync", .{});
        span.setAttribute("fallback", "full_sync");
        return fullSync(turso, local, false);
    }

    // if last sync is older than tombstone retention, local data is too stale
    // to trust incremental tombstones — wipe and rebootstrap.
    const now = timestamp();
    if (now - last_sync_ts > TOMBSTONE_RETENTION_S) {
        log.info("incremental: stale ({d}s old), falling back to full sync with wipe", .{now - last_sync_ts});
        span.setAttribute("fallback", "stale_wipe");
        return fullSync(turso, local, true);
    }

    // self-healing convergence: if lag exceeds SYNC_MAX_LAG_S, the per-row
    // incremental drain is too slow to catch up — do a bulk resync (keeps
    // serving, rebuilds FTS clean, resets the watermark to now) instead of
    // grinding. Keeps the replica from drifting permanently.
    if (now - last_sync_ts > maxLagS()) {
        log.info("incremental: lag {d}s exceeds max {d}s, self-healing via bulk resync", .{ now - last_sync_ts, maxLagS() });
        span.setAttribute("fallback", "lag_resync");
        return fullSync(turso, local, false);
    }

    local.setReady(true);

    // Overlay projection: gate deltas on the attached snapshot watermark — only
    // changes NEWER than the base get projected (older state is already baked
    // in). 0 = no snapshot attached → overlay projection is skipped entirely.
    const overlay_watermark: i64 = @intCast(local.attachedMetaInt(index_schema.meta.source_watermark));
    var ov_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer ov_arena.deinit();

    // buffer: subtract 300s to catch stragglers (matches pub-search)
    const since_ts = last_sync_ts - 300;
    var since_buf: [20]u8 = undefined;
    const since_str = std.fmt.bufPrint(&since_buf, "{d}", .{since_ts}) catch return;

    // fetch updated searchable actors — keyset pagination to drain all
    var updated: usize = 0;
    var error_count: usize = 0;
    var had_turso_error = false;
    {
        var cursor_ts_buf: [20]u8 = undefined;
        var cursor_did_buf: [128]u8 = undefined;
        @memcpy(cursor_ts_buf[0..since_str.len], since_str);
        var cursor_ts: []const u8 = cursor_ts_buf[0..since_str.len];
        var cursor_did_len: usize = 0;

        while (true) {
            const cursor_did: []const u8 = if (cursor_did_len > 0) cursor_did_buf[0..cursor_did_len] else "";

            const args: []const []const u8 = if (cursor_did_len > 0)
                &.{ cursor_ts, cursor_ts, cursor_did }
            else
                &.{cursor_ts};

            const sql = if (cursor_did_len > 0)
                "SELECT " ++ LocalDb.actor_cols ++ ", updated_at FROM actors WHERE handle != '' AND (updated_at > ?1 OR (updated_at = ?2 AND did > ?3)) ORDER BY updated_at, did LIMIT 2000"
            else
                "SELECT " ++ LocalDb.actor_cols ++ ", updated_at FROM actors WHERE handle != '' AND updated_at > ?1 ORDER BY updated_at, did LIMIT 2000";

            log.info("incremental: querying updated actors (cursor_ts={s})", .{cursor_ts});
            const q_span = logfire.span("sync.query.updated", .{});

            var result = turso.query(sql, args) catch |err| {
                log.err("incremental query failed: {}", .{err});
                q_span.setStatus(.@"error", "query failed");
                q_span.end();
                had_turso_error = true;
                break;
            };
            defer result.deinit();

            q_span.setAttribute("rows", @as(i64, @intCast(result.rows.len)));
            q_span.setStatus(.ok, null);
            q_span.end();

            if (result.rows.len == 0) break;

            log.info("incremental: applying {d} updated actors", .{result.rows.len});
            {
                const a_span = logfire.span("sync.apply.updated", .{});
                defer a_span.end();

                local.lock();
                defer local.unlock();
                conn.exec("BEGIN", .{}) catch {};
                for (result.rows) |row| {
                    const ua = row.int(trailing_col);
                    const octx: ?OverlayCtx = if (overlay_watermark > 0 and ua > overlay_watermark)
                        .{ .arena = ov_arena.allocator(), .updated_at = ua }
                    else
                        null;
                    applyActorUpdate(conn, row, "incremental", octx) catch {
                        error_count += 1;
                        continue;
                    };
                    if (octx != null) _ = ov_arena.reset(.retain_capacity);
                    updated += 1;
                }
                conn.exec("COMMIT", .{}) catch {};

                a_span.setAttribute("rows", @as(i64, @intCast(result.rows.len)));
                a_span.setStatus(.ok, null);
            }

            const last_row = result.rows[result.rows.len - 1];
            const last_ua_int = last_row.int(trailing_col);
            const last_did = last_row.text(Col.did);

            // serialize updated_at integer to string for next query
            const ua_str = std.fmt.bufPrint(&cursor_ts_buf, "{d}", .{last_ua_int}) catch break;
            cursor_ts = ua_str;

            if (last_did.len > 0 and last_did.len <= cursor_did_buf.len) {
                @memcpy(cursor_did_buf[0..last_did.len], last_did);
                cursor_did_len = last_did.len;
            }

            // safety: stop if batch was less than full (we've drained)
            if (result.rows.len < BATCH_SIZE) break;
        }
    }

    // fetch actors that became unsearchable (handle cleared)
    var cleared: usize = 0;
    unsearchable: {
        log.info("incremental: querying unsearchable actors", .{});
        const q_span = logfire.span("sync.query.unsearchable", .{});

        var result = turso.query(
            "SELECT did, updated_at FROM actors WHERE updated_at > ? AND handle = ''",
            &.{since_str},
        ) catch |err| {
            log.err("unsearchable query failed: {}", .{err});
            q_span.setStatus(.@"error", "query failed");
            q_span.end();
            had_turso_error = true;
            break :unsearchable;
        };
        defer result.deinit();

        q_span.setAttribute("rows", @as(i64, @intCast(result.rows.len)));
        q_span.setStatus(.ok, null);
        q_span.end();

        if (result.rows.len > 0) {
            log.info("incremental: clearing {d} unsearchable actors", .{result.rows.len});
            local.lock();
            defer local.unlock();
            conn.exec("BEGIN", .{}) catch {};
            for (result.rows) |row| {
                const did = row.text(0);
                const ua = row.int(1);
                const octx: ?OverlayCtx = if (overlay_watermark > 0 and ua > overlay_watermark)
                    .{ .arena = ov_arena.allocator(), .updated_at = ua }
                else
                    null;
                applyActorDelete(conn, did, "incremental.cleared", octx) catch {
                    error_count += 1;
                    continue;
                };
                if (octx != null) _ = ov_arena.reset(.retain_capacity);
                cleared += 1;
            }
            conn.exec("COMMIT", .{}) catch {};
        }
    }

    // fetch tombstones
    var deleted: usize = 0;
    tombstone: {
        log.info("incremental: querying tombstones", .{});
        const q_span = logfire.span("sync.query.tombstones", .{});

        var tomb_result = turso.query(
            "SELECT did, deleted_at FROM tombstones WHERE deleted_at > ?",
            &.{since_str},
        ) catch |err| {
            log.err("tombstone query failed: {}", .{err});
            q_span.setStatus(.@"error", "query failed");
            q_span.end();
            had_turso_error = true;
            break :tombstone;
        };
        defer tomb_result.deinit();

        q_span.setAttribute("rows", @as(i64, @intCast(tomb_result.rows.len)));
        q_span.setStatus(.ok, null);
        q_span.end();

        if (tomb_result.rows.len > 0) {
            log.info("incremental: deleting {d} tombstoned actors", .{tomb_result.rows.len});
            local.lock();
            defer local.unlock();
            conn.exec("BEGIN", .{}) catch {};
            for (tomb_result.rows) |row| {
                const did = row.text(0);
                const del_at = row.int(1);
                const octx: ?OverlayCtx = if (overlay_watermark > 0 and del_at > overlay_watermark)
                    .{ .arena = ov_arena.allocator(), .updated_at = del_at }
                else
                    null;
                applyActorDelete(conn, did, "incremental.tombstone", octx) catch {
                    error_count += 1;
                    continue;
                };
                if (octx != null) _ = ov_arena.reset(.retain_capacity);
                deleted += 1;
            }
            conn.exec("COMMIT", .{}) catch {};
        }
    }

    // FTS is maintained per-row above via applyActorUpdate / applyActorDelete.
    // No bulk rebuild — that pattern (drop + recreate + insert ~6M rows) took
    // 15-20 min on a 256MB VM and left the live FTS empty for that entire
    // window, breaking display-name search ~80% of the time in steady state.
    // Bootstrap / resync still do bulk builds (against staging, while ready=false)
    // and LocalDb.checkAndRepair detects a broken FTS on startup and triggers
    // a fresh bootstrap as recovery.

    // only advance watermark when everything succeeded
    const had_error = had_turso_error or error_count > 0;
    if (had_error) {
        span.setAttribute("error_count", @as(i64, @intCast(error_count)));
        span.setStatus(.@"error", "incremental sync had errors");
        log.err("incremental: done with errors (updated={d}, cleared={d}, deleted={d}, errors={d})", .{
            updated, cleared, deleted, error_count,
        });
    } else {
        local.lock();
        defer local.unlock();
        var ts_buf: [20]u8 = undefined;
        const ts_str = std.fmt.bufPrint(&ts_buf, "{d}", .{timestamp()}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync', ?)",
            .{ts_str},
        ) catch {};

        // refresh cached actor count when data changed
        if (updated > 0 or deleted > 0 or cleared > 0) {
            updateActorCount(conn);
        }

        // TRUNCATE (not PASSIVE): the read pool nearly always has at least
        // one slot active under live load, and PASSIVE never reclaims frames
        // while a reader exists — WAL grows unbounded (we observed 748MB
        // bloat in ~16min, dragging per-query latency from <50ms to >1s).
        // TRUNCATE waits up to the writer's busy_timeout for readers to
        // release their snapshots, then truncates the WAL file to zero.
        // If it can't (sustained read pressure), it returns busy without
        // throwing; we'll retry next cycle.
        conn.exec("PRAGMA wal_checkpoint(TRUNCATE)", .{}) catch {};
        span.setStatus(.ok, null);
        log.info("incremental: done (updated={d}, cleared={d}, deleted={d})", .{
            updated, cleared, deleted,
        });
    }

    span.setAttribute("updated", @as(i64, @intCast(updated)));
    span.setAttribute("deleted", @as(i64, @intCast(deleted)));
    span.setAttribute("cleared", @as(i64, @intCast(cleared)));
}

/// Background sync loop: full sync on boot, then incremental every 5 min
pub fn syncLoop(allocator: Allocator, local: *LocalDb) void {
    log.info("sync loop started", .{});
    var turso = TursoClient.init(allocator) catch |err| {
        log.err("turso client init failed: {}, sync disabled", .{err});
        return;
    };
    defer turso.deinit();

    // initial sync (full or incremental depending on state)
    incrementalSync(&turso, local) catch |err| {
        log.err("initial sync failed: {}", .{err});
    };

    // periodic incremental sync
    while (true) {
        io.sleep(.{ .nanoseconds = SYNC_INTERVAL_S * std.time.ns_per_s }, .real) catch {};

        incrementalSync(&turso, local) catch |err| {
            log.err("periodic sync failed: {}", .{err});
        };
    }
}

/// Insert actor row into a table. `prefix` is the INSERT clause, e.g.
/// "INSERT INTO actors_stage" or "INSERT OR REPLACE INTO actors".
fn insertActorRow(conn: zqlite.Conn, comptime prefix: []const u8, row: anytype) !void {
    conn.exec(prefix ++ " (" ++ LocalDb.actor_cols ++ ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", .{
        row.text(Col.did),
        row.text(Col.handle),
        row.text(Col.display_name),
        row.text(Col.avatar_url),
        row.int(Col.hidden),
        row.text(Col.labels),
        row.text(Col.created_at),
        row.text(Col.associated),
        row.text(Col.pds),
        row.int(Col.followers_count),
        row.int(Col.follows_count),
        row.int(Col.posts_count),
        row.int(Col.last_activity_at),
        row.float(Col.quality_score),
    }) catch |err| return err;
}

/// Snapshot of (handle, display_name) from `actors` for a DID, used to
/// drive FTS5 special-delete before the row is upserted/removed.
const OldFtsRow = struct {
    handle_buf: [256]u8 = undefined,
    name_buf: [512]u8 = undefined,
    handle: ?[]const u8 = null,
    name: ?[]const u8 = null,
};

/// Snapshot must come from `actors_fts` (not `actors`) because the FTS5
/// special-delete `INSERT INTO ft(ft, did, handle, display_name) VALUES('delete', ...)`
/// requires the EXACT tuple currently in the FTS table. Reading from
/// `actors` can produce a stale snapshot (prior sync already updated the
/// row but FTS rebuild failed silently), so the delete-old never matches
/// and the FTS table accumulates orphan rows — causing the hundreds-per-
/// cycle "FTS delete-old failed" warnings we saw on the search service.
/// Old handle/display from the `actors` row being replaced — the correct
/// source for overlay old-prefix derivation, and FTS-independent (works when
/// ENABLE_LOCAL_FTS=0). Shares OldFtsRow's buffers.
fn snapshotOldActorRow(conn: zqlite.Conn, did: []const u8, out: *OldFtsRow) void {
    if (conn.row("SELECT handle, display_name FROM actors WHERE did = ?", .{did})) |maybe| {
        if (maybe) |r| {
            defer r.deinit();
            const h = r.text(0);
            const n = r.text(1);
            if (h.len > 0 and h.len <= out.handle_buf.len) {
                @memcpy(out.handle_buf[0..h.len], h);
                out.handle = out.handle_buf[0..h.len];
            }
            if (n.len > 0 and n.len <= out.name_buf.len) {
                @memcpy(out.name_buf[0..n.len], n);
                out.name = out.name_buf[0..n.len];
            }
        }
    } else |_| {}
}

fn snapshotOldFtsRow(conn: zqlite.Conn, did: []const u8, out: *OldFtsRow) void {
    if (conn.row("SELECT handle, display_name FROM actors_fts WHERE did = ?", .{did})) |maybe| {
        if (maybe) |r| {
            defer r.deinit();
            const h = r.text(0);
            const n = r.text(1);
            if (h.len > 0 and h.len <= out.handle_buf.len) {
                @memcpy(out.handle_buf[0..h.len], h);
                out.handle = out.handle_buf[0..h.len];
            }
            if (n.len > 0 and n.len <= out.name_buf.len) {
                @memcpy(out.name_buf[0..n.len], n);
                out.name = out.name_buf[0..n.len];
            }
        }
    } else |_| {}
}

/// Per-row incremental apply: snapshot old FTS values, INSERT OR REPLACE the
/// actor, then FTS5 special-delete the old entry and insert the new one. Keeps
/// the local FTS table consistent without periodic full rebuilds.
/// `caller` is just for log context ("incremental" vs other future callers).
fn applyActorUpdate(conn: zqlite.Conn, row: anytype, caller: []const u8, octx: ?OverlayCtx) !void {
    const did = row.text(Col.did);
    const new_handle = row.text(Col.handle);
    const new_name = row.text(Col.display_name);
    const fts = localFtsEnabled();

    // Old handle/display: from actors_fts when maintaining FTS (its tuple is
    // needed for the special-delete), else from `actors` for the overlay diff.
    var old: OldFtsRow = .{};
    if (fts) {
        snapshotOldFtsRow(conn, did, &old);
    } else if (octx != null) {
        snapshotOldActorRow(conn, did, &old);
    }

    try insertActorRow(conn, "INSERT OR REPLACE INTO actors", row);

    if (fts) {
        if (old.handle) |oh| {
            conn.exec(
                \\INSERT INTO actors_fts(actors_fts, did, handle, display_name)
                \\VALUES('delete', ?, ?, ?)
            , .{ did, oh, old.name orelse "" }) catch |err| {
                log.warn("{s}: FTS delete-old failed for {s}: {}", .{ caller, did, err });
            };
        }
        if (new_handle.len > 0) {
            conn.exec(
                \\INSERT INTO actors_fts (did, handle, display_name)
                \\VALUES (?, ?, ?)
            , .{ did, new_handle, new_name }) catch |err| {
                log.warn("{s}: FTS insert-new failed for {s}: {}", .{ caller, did, err });
            };
        }
    }

    // Overlay projection (same transaction as the actor write). Best-effort:
    // the live-actors safety belt still gives serving correct moderation if a
    // projection fails; base still serves the actor either way.
    if (octx) |ctx| {
        overlay.projectUpdate(ctx.arena, conn, .{
            .did = did,
            .handle = new_handle,
            .display_name = new_name,
            .avatar_url = row.text(Col.avatar_url),
            .labels = row.text(Col.labels),
            .associated = row.text(Col.associated),
            .pds = row.text(Col.pds),
            .created_at = row.text(Col.created_at),
            .hidden = row.int(Col.hidden) != 0,
            .quality_score = @floatCast(row.float(Col.quality_score)),
        }, old.handle orelse "", old.name orelse "", ctx.updated_at) catch |err| {
            log.warn("{s}: overlay projectUpdate failed for {s}: {}", .{ caller, did, err });
        };
    }
}

/// Per-row incremental remove: snapshot old FTS values, DELETE the actor
/// row, then FTS5 special-delete the old entry. Used for both the "became
/// unsearchable" and "tombstoned" paths.
fn applyActorDelete(conn: zqlite.Conn, did: []const u8, caller: []const u8, octx: ?OverlayCtx) !void {
    const fts = localFtsEnabled();
    var old: OldFtsRow = .{};
    if (fts) {
        snapshotOldFtsRow(conn, did, &old);
    } else if (octx != null) {
        snapshotOldActorRow(conn, did, &old);
    }

    try conn.exec("DELETE FROM actors WHERE did = ?", .{did});

    if (fts) {
        if (old.handle) |oh| {
            conn.exec(
                \\INSERT INTO actors_fts(actors_fts, did, handle, display_name)
                \\VALUES('delete', ?, ?, ?)
            , .{ did, oh, old.name orelse "" }) catch |err| {
                log.warn("{s}: FTS delete failed for {s}: {}", .{ caller, did, err });
            };
        }
    }

    // Global tombstone in the overlay so index serving suppresses the actor
    // immediately (same transaction as the row delete).
    if (octx) |ctx| {
        overlay.projectDelete(ctx.arena, conn, did, old.handle orelse "", old.name orelse "", ctx.updated_at) catch |err| {
            log.warn("{s}: overlay projectDelete failed for {s}: {}", .{ caller, did, err });
        };
    }
}

/// Update cached actor count in sync_meta (avoids COUNT(*) on health checks)
fn updateActorCount(conn: zqlite.Conn) void {
    const row = conn.row("SELECT COUNT(*) FROM actors WHERE handle != ''", .{}) catch return;
    if (row) |r| {
        defer r.deinit();
        var buf: [20]u8 = undefined;
        const count_str = std.fmt.bufPrint(&buf, "{d}", .{r.int(0)}) catch return;
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('actor_count', ?)",
            .{count_str},
        ) catch {};
    }
}
