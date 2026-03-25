//! Sync from Turso to local SQLite
//! Full sync on startup, incremental sync every 5 minutes

const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;
const TursoClient = @import("TursoClient.zig");
const LocalDb = @import("LocalDb.zig");

const log = std.log.scoped(.sync);

const BATCH_SIZE = 2000;
const SYNC_INTERVAL_S = 300; // 5 minutes
const TOMBSTONE_RETENTION_S = 7 * 24 * 3600; // 7 days — must match cron.ts pruning

/// Full sync: fetch all searchable actors from Turso using keyset pagination.
/// Does NOT run stale cleanup — that's handled by incremental sync (tombstones +
/// "became unsearchable" queries). This avoids races with the dual-write path.
/// When `wipe` is true, deletes all local data first (used by stale rebuild path).
pub fn fullSync(turso: *TursoClient, local: *LocalDb, wipe: bool) !void {
    log.info("starting full sync (wipe={})...", .{wipe});

    const conn = local.getConn() orelse return error.LocalNotOpen;

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
        // bootstrap path: use staging tables for fast bulk load
        return fullSyncBootstrap(turso, local, conn, wipe);
    }

    // re-sync path: previous sync completed, keep serving while re-syncing
    local.setReady(true);
    log.info("previous sync complete, keeping ready during re-sync", .{});
    return fullSyncResync(turso, local, conn);
}

/// Bootstrap path: load into staging tables (no indexes), build indexes in bulk,
/// then atomically swap into place. Live schema is never in a degraded state.
fn fullSyncBootstrap(turso: *TursoClient, local: *LocalDb, conn: zqlite.Conn, wipe: bool) !void {
    const total_t0 = std.time.milliTimestamp();
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
            \\  associated TEXT DEFAULT '{}'
            \\)
        , .{}) catch |err| {
            log.err("failed to create actors_stage: {}", .{err});
            return err;
        };
    }

    // keyset pagination: fetch rows WHERE rowid > last_rowid ORDER BY rowid
    var actor_count: usize = 0;
    var error_count: usize = 0;
    var last_rowid: i64 = 0;
    var had_turso_error = false;
    const load_t0 = std.time.milliTimestamp();

    while (true) {
        var rowid_buf: [20]u8 = undefined;
        const rowid_str = std.fmt.bufPrint(&rowid_buf, "{d}", .{last_rowid}) catch break;

        const t0 = std.time.milliTimestamp();

        // fetch from turso (no lock held)
        var result = turso.query(
            \\SELECT did, handle, display_name, avatar_url, hidden, labels, created_at, associated, rowid
            \\FROM actors WHERE handle != '' AND rowid > ?
            \\ORDER BY rowid LIMIT 2000
        , &.{rowid_str}) catch |err| {
            log.err("turso query failed at rowid {d}: {}", .{ last_rowid, err });
            had_turso_error = true;
            break;
        };
        defer result.deinit();

        const t1 = std.time.milliTimestamp();

        if (result.rows.len == 0) break;

        // write batch to staging table — no indexes, fast sequential appends
        {
            local.lock();
            defer local.unlock();
            conn.exec("BEGIN", .{}) catch {};
            for (result.rows) |row| {
                insertActorStage(conn, row) catch |err| {
                    log.err("insert actor failed: {}", .{err});
                    error_count += 1;
                    continue;
                };
                actor_count += 1;
            }
            conn.exec("COMMIT", .{}) catch {};
        }

        const t2 = std.time.milliTimestamp();

        // advance cursor to last row's rowid (column 8)
        last_rowid = result.rows[result.rows.len - 1].int(8);

        log.info("batch: {d} rows, rowid={d}, total={d} | fetch={d}ms apply={d}ms", .{
            result.rows.len, last_rowid, actor_count,
            t1 - t0, t2 - t1,
        });
    }

    if (had_turso_error or error_count > 0) {
        log.warn("bootstrap incomplete — {d} synced, {d} local write errors, turso_error={}", .{ actor_count, error_count, had_turso_error });
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

    const load_ms = std.time.milliTimestamp() - load_t0;

    // checkpoint WAL before heavy DDL — clean slate for index builds
    {
        local.lock();
        defer local.unlock();
        conn.exec("PRAGMA wal_checkpoint(TRUNCATE)", .{}) catch {};
    }

    // build indexes on staging table
    log.info("building indexes on {d} rows...", .{actor_count});
    const idx_did_t0 = std.time.milliTimestamp();
    {
        local.lock();
        defer local.unlock();
        conn.exec("CREATE UNIQUE INDEX idx_stage_did ON actors_stage(did)", .{}) catch |err| {
            log.err("failed to create unique index on staging: {}", .{err});
            conn.exec("DROP TABLE IF EXISTS actors_stage", .{}) catch {};
            return err;
        };
    }
    const idx_did_ms = std.time.milliTimestamp() - idx_did_t0;

    const idx_handle_t0 = std.time.milliTimestamp();
    {
        local.lock();
        defer local.unlock();
        conn.exec("CREATE INDEX idx_stage_handle ON actors_stage(handle COLLATE NOCASE)", .{}) catch |err| {
            log.err("failed to create handle index on staging: {}", .{err});
            conn.exec("DROP TABLE IF EXISTS actors_stage", .{}) catch {};
            return err;
        };
    }
    const idx_handle_ms = std.time.milliTimestamp() - idx_handle_t0;

    // swap actors table first, then build FTS with final name.
    // FTS5 virtual tables use shadow tables (_content, _data, _idx, etc.)
    // that don't reliably rename with ALTER TABLE RENAME — so we never rename FTS5.
    {
        local.lock();
        defer local.unlock();
        conn.exec("BEGIN", .{}) catch {};
        conn.exec("DROP TABLE IF EXISTS actors_fts", .{}) catch {};
        conn.exec("DROP TABLE IF EXISTS actors", .{}) catch {};
        conn.exec("ALTER TABLE actors_stage RENAME TO actors", .{}) catch |err| {
            log.err("swap failed (rename actors_stage): {}", .{err});
            conn.exec("ROLLBACK", .{}) catch {};
            return err;
        };
        conn.exec("COMMIT", .{}) catch |err| {
            log.err("swap commit failed: {}", .{err});
            return err;
        };
    }

    // build FTS with its final name — no rename needed
    const fts_create_t0 = std.time.milliTimestamp();
    {
        local.lock();
        defer local.unlock();
        conn.exec(
            \\CREATE VIRTUAL TABLE actors_fts USING fts5(
            \\  did UNINDEXED, handle, display_name,
            \\  tokenize='unicode61 remove_diacritics 2'
            \\)
        , .{}) catch |err| {
            log.err("failed to create FTS table: {}", .{err});
            return err;
        };
    }
    const fts_create_ms = std.time.milliTimestamp() - fts_create_t0;

    const fts_pop_t0 = std.time.milliTimestamp();
    {
        local.lock();
        defer local.unlock();
        conn.exec(
            \\INSERT INTO actors_fts (did, handle, display_name)
            \\SELECT did, handle, display_name FROM actors WHERE handle != ''
        , .{}) catch |err| {
            log.err("FTS populate failed: {}", .{err});
            return err;
        };
    }
    const fts_pop_ms = std.time.milliTimestamp() - fts_pop_t0;

    // post-swap: optimize, record completion + actor count, checkpoint
    {
        local.lock();
        defer local.unlock();
        conn.exec("PRAGMA optimize", .{}) catch {};

        var ts_buf: [20]u8 = undefined;
        const ts_str = std.fmt.bufPrint(&ts_buf, "{d}", .{std.time.timestamp()}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync', ?)",
            .{ts_str},
        ) catch {};
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('sync_complete', '1')",
            .{},
        ) catch {};

        // cache actor count so health endpoint doesn't need COUNT(*)
        var count_buf: [20]u8 = undefined;
        const count_str = std.fmt.bufPrint(&count_buf, "{d}", .{actor_count}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('actor_count', ?)",
            .{count_str},
        ) catch {};

        conn.exec("PRAGMA wal_checkpoint(PASSIVE)", .{}) catch {};
    }

    const total_ms = std.time.milliTimestamp() - total_t0;

    // reopen read connection before serving traffic
    local.reopenReadConn() catch |err| {
        log.err("failed to reopen read conn after bootstrap: {}", .{err});
    };

    local.setReady(true);
    log.info("bootstrap complete — total_rows={d} load_ms={d} idx_did_ms={d} idx_handle_ms={d} fts_create_ms={d} fts_populate_ms={d} total_ms={d}", .{
        actor_count, load_ms, idx_did_ms, idx_handle_ms, fts_create_ms, fts_pop_ms, total_ms,
    });
}

/// Re-sync path: previous sync completed, update live table directly.
/// Only processes actors that may have changed — index maintenance cost is negligible.
fn fullSyncResync(turso: *TursoClient, local: *LocalDb, conn: zqlite.Conn) !void {
    var actor_count: usize = 0;
    var error_count: usize = 0;
    var last_rowid: i64 = 0;
    var had_turso_error = false;

    while (true) {
        var rowid_buf: [20]u8 = undefined;
        const rowid_str = std.fmt.bufPrint(&rowid_buf, "{d}", .{last_rowid}) catch break;

        const t0 = std.time.milliTimestamp();

        var result = turso.query(
            \\SELECT did, handle, display_name, avatar_url, hidden, labels, created_at, associated, rowid
            \\FROM actors WHERE handle != '' AND rowid > ?
            \\ORDER BY rowid LIMIT 2000
        , &.{rowid_str}) catch |err| {
            log.err("turso query failed at rowid {d}: {}", .{ last_rowid, err });
            had_turso_error = true;
            break;
        };
        defer result.deinit();

        const t1 = std.time.milliTimestamp();

        if (result.rows.len == 0) break;

        {
            local.lock();
            defer local.unlock();
            conn.exec("BEGIN", .{}) catch {};
            for (result.rows) |row| {
                insertActorOnly(conn, row) catch |err| {
                    log.err("insert actor failed: {}", .{err});
                    error_count += 1;
                    continue;
                };
                actor_count += 1;
            }
            conn.exec("COMMIT", .{}) catch {};
        }

        const t2 = std.time.milliTimestamp();

        last_rowid = result.rows[result.rows.len - 1].int(8);

        log.info("batch: {d} rows, rowid={d}, total={d} | fetch={d}ms apply={d}ms", .{
            result.rows.len, last_rowid, actor_count,
            t1 - t0, t2 - t1,
        });
    }

    if (had_turso_error or error_count > 0) {
        log.warn("full sync incomplete — {d} synced, {d} local write errors, turso_error={}", .{ actor_count, error_count, had_turso_error });
        return;
    }

    // rebuild FTS from scratch (DROP + CREATE — never reuse a potentially
    // broken FTS table from a previous failed rename)
    {
        log.info("building FTS index for {d} actors...", .{actor_count});
        const fts_t0 = std.time.milliTimestamp();
        local.lock();
        defer local.unlock();
        conn.exec("DROP TABLE IF EXISTS actors_fts", .{}) catch {};
        conn.exec(
            \\CREATE VIRTUAL TABLE actors_fts USING fts5(
            \\  did UNINDEXED, handle, display_name,
            \\  tokenize='unicode61 remove_diacritics 2'
            \\)
        , .{}) catch |err| {
            log.err("failed to create FTS table: {}", .{err});
        };
        conn.exec(
            \\INSERT INTO actors_fts (did, handle, display_name)
            \\SELECT did, handle, display_name FROM actors WHERE handle != ''
        , .{}) catch |err| {
            log.err("FTS bulk insert failed: {}", .{err});
        };
        const fts_t1 = std.time.milliTimestamp();
        log.info("FTS index built in {d}ms", .{fts_t1 - fts_t0});
    }

    // record sync time + mark complete + actor count
    {
        local.lock();
        defer local.unlock();
        var ts_buf: [20]u8 = undefined;
        const ts_str = std.fmt.bufPrint(&ts_buf, "{d}", .{std.time.timestamp()}) catch "0";
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
    log.info("full sync complete — {d} actors", .{actor_count});
}

/// Incremental sync: fetch actors updated since last sync + tombstones.
/// Uses keyset pagination on (updated_at, did) to drain all updates.
/// Only advances watermark when all turso queries succeed and all local writes succeed.
pub fn incrementalSync(turso: *TursoClient, local: *LocalDb) !void {
    const conn = local.getConn() orelse return error.LocalNotOpen;

    // get last sync time
    local.lock();
    const last_sync_ts = blk: {
        const row = conn.row(
            "SELECT value FROM sync_meta WHERE key = 'last_sync'",
            .{},
        ) catch {
            local.unlock();
            break :blk @as(i64, 0);
        };
        if (row) |r| {
            defer r.deinit();
            const val = r.text(0);
            local.unlock();
            break :blk if (val.len == 0) 0 else std.fmt.parseInt(i64, val, 10) catch 0;
        }
        local.unlock();
        break :blk @as(i64, 0);
    };

    if (last_sync_ts == 0) {
        log.info("no last_sync found, doing full sync", .{});
        return fullSync(turso, local, false);
    }

    // if last sync is older than tombstone retention, local data is too stale —
    // tombstones may have been pruned, so incremental sync can't catch deletions.
    // wipe and rebuild from scratch.
    const now = std.time.timestamp();
    if (now - last_sync_ts > TOMBSTONE_RETENTION_S) {
        log.warn("last sync {d}s ago (>{d}s tombstone retention), wiping stale replica", .{
            now - last_sync_ts, TOMBSTONE_RETENTION_S,
        });
        return fullSync(turso, local, true);
    }

    local.setReady(true);

    // buffer: subtract 300s to catch stragglers (matches leaflet-search)
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
                \\SELECT did, handle, display_name, avatar_url, hidden, labels, created_at, associated, updated_at
                \\FROM actors WHERE handle != '' AND (updated_at > ?1 OR (updated_at = ?2 AND did > ?3))
                \\ORDER BY updated_at, did LIMIT 2000
            else
                \\SELECT did, handle, display_name, avatar_url, hidden, labels, created_at, associated, updated_at
                \\FROM actors WHERE handle != '' AND updated_at > ?1
                \\ORDER BY updated_at, did LIMIT 2000
            ;

            var result = turso.query(sql, args) catch |err| {
                log.err("incremental query failed: {}", .{err});
                had_turso_error = true;
                break;
            };
            defer result.deinit();

            if (result.rows.len == 0) break;

            {
                local.lock();
                defer local.unlock();
                conn.exec("BEGIN", .{}) catch {};
                for (result.rows) |row| {
                    upsertActorLocal(conn, row) catch {
                        error_count += 1;
                        continue;
                    };
                    updated += 1;
                }
                conn.exec("COMMIT", .{}) catch {};
            }

            // advance cursor to last row's (updated_at, did)
            const last_row = result.rows[result.rows.len - 1];
            const last_ua_int = last_row.int(8); // updated_at is integer
            const last_did = last_row.text(0);

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
        var result = turso.query(
            "SELECT did FROM actors WHERE updated_at > ? AND handle = ''",
            &.{since_str},
        ) catch |err| {
            log.err("unsearchable query failed: {}", .{err});
            had_turso_error = true;
            break :unsearchable;
        };
        defer result.deinit();

        if (result.rows.len > 0) {
            local.lock();
            defer local.unlock();
            for (result.rows) |row| {
                const did = row.text(0);
                conn.exec("DELETE FROM actors WHERE did = ?", .{did}) catch {
                    error_count += 1;
                    continue;
                };
                conn.exec("DELETE FROM actors_fts WHERE did = ?", .{did}) catch {};
                cleared += 1;
            }
        }
    }

    // fetch tombstones
    var deleted: usize = 0;
    tombstone: {
        var tomb_result = turso.query(
            "SELECT did FROM tombstones WHERE deleted_at > ?",
            &.{since_str},
        ) catch |err| {
            log.err("tombstone query failed: {}", .{err});
            had_turso_error = true;
            break :tombstone;
        };
        defer tomb_result.deinit();

        if (tomb_result.rows.len > 0) {
            local.lock();
            defer local.unlock();
            for (tomb_result.rows) |row| {
                const did = row.text(0);
                conn.exec("DELETE FROM actors WHERE did = ?", .{did}) catch {
                    error_count += 1;
                    continue;
                };
                conn.exec("DELETE FROM actors_fts WHERE did = ?", .{did}) catch {};
                deleted += 1;
            }
        }
    }

    // only advance watermark when everything succeeded
    const had_error = had_turso_error or error_count > 0;
    if (had_error) {
        log.warn("incremental sync had errors — {d} updated, {d} deleted, {d} cleared, {d} write errors, turso_error={}", .{
            updated, deleted, cleared, error_count, had_turso_error,
        });
    } else {
        local.lock();
        defer local.unlock();
        var ts_buf: [20]u8 = undefined;
        const ts_str = std.fmt.bufPrint(&ts_buf, "{d}", .{std.time.timestamp()}) catch "0";
        conn.exec(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync', ?)",
            .{ts_str},
        ) catch {};

        // refresh cached actor count when data changed
        if (updated > 0 or deleted > 0 or cleared > 0) {
            updateActorCount(conn);
            log.info("incremental sync — {d} updated, {d} deleted, {d} cleared", .{ updated, deleted, cleared });
        }

        conn.exec("PRAGMA wal_checkpoint(PASSIVE)", .{}) catch {};
    }
}

/// Background sync loop: full sync on boot, then incremental every 5 min
pub fn syncLoop(allocator: Allocator, local: *LocalDb) void {
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
        std.Thread.sleep(SYNC_INTERVAL_S * std.time.ns_per_s);

        incrementalSync(&turso, local) catch |err| {
            log.err("periodic sync failed: {}", .{err});
        };
    }
}

/// Insert actor row into staging table (no indexes, no FTS — used during bootstrap)
fn insertActorStage(conn: zqlite.Conn, row: anytype) !void {
    conn.exec(
        \\INSERT INTO actors_stage
        \\(did, handle, display_name, avatar_url, hidden, labels, created_at, associated)
        \\VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    , .{
        row.text(0), // did
        row.text(1), // handle
        row.text(2), // display_name
        row.text(3), // avatar_url
        row.int(4), // hidden
        row.text(5), // labels
        row.text(6), // created_at
        row.text(7), // associated
    }) catch |err| {
        return err;
    };
}

/// Insert actor row without FTS update (used during re-sync on live table)
fn insertActorOnly(conn: zqlite.Conn, row: anytype) !void {
    conn.exec(
        \\INSERT OR REPLACE INTO actors
        \\(did, handle, display_name, avatar_url, hidden, labels, created_at, associated)
        \\VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    , .{
        row.text(0), // did
        row.text(1), // handle
        row.text(2), // display_name
        row.text(3), // avatar_url
        row.int(4), // hidden
        row.text(5), // labels
        row.text(6), // created_at
        row.text(7), // associated
    }) catch |err| {
        return err;
    };
}

/// Upsert a turso row into local SQLite + FTS
fn upsertActorLocal(conn: zqlite.Conn, row: anytype) !void {
    const did = row.text(0);

    conn.exec(
        \\INSERT OR REPLACE INTO actors
        \\(did, handle, display_name, avatar_url, hidden, labels, created_at, associated)
        \\VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    , .{
        did,
        row.text(1), // handle
        row.text(2), // display_name
        row.text(3), // avatar_url
        row.int(4), // hidden
        row.text(5), // labels
        row.text(6), // created_at
        row.text(7), // associated
    }) catch |err| {
        return err;
    };

    // manual FTS update (standalone FTS, not content-synced)
    conn.exec("DELETE FROM actors_fts WHERE did = ?", .{did}) catch {};
    conn.exec(
        "INSERT INTO actors_fts (did, handle, display_name) VALUES (?, ?, ?)",
        .{ did, row.text(1), row.text(2) },
    ) catch {};
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
