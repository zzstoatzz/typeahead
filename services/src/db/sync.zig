//! Turso → overlay projection.
//!
//! The search box holds the prefix-index snapshot (base, attached read-only as
//! `idx`) plus the live overlay. Every 5 minutes this loop pages Turso for rows
//! whose `updated_at` is newer than the watermark, projects each one into the
//! overlay, and persists the watermark per page. Recovery from any outage is
//! "replay rows newer than the attached snapshot" — bounded by the promote
//! cadence, resumable by construction. There is no local copy of the corpus:
//! docs/replica-removal-plan.md phase 3 removed the `actors` replica whose
//! 11-hour resyncs produced the 2026-07/08 retros.

const std = @import("std");
const mem = std.mem;
const io = std.Options.debug_io;
const zqlite = @import("zqlite");
const logfire = @import("logfire");
const fatal = @import("../fatal.zig");
const Allocator = std.mem.Allocator;
const TursoClient = @import("TursoClient.zig");
const LocalDb = @import("LocalDb.zig");
const overlay = @import("../index/overlay.zig");
const index_schema = @import("../index/schema.zig");
const pack = @import("../index/pack.zig");

const log = std.log.scoped(.sync);

/// column list for the Turso actor rows this loop reads
pub const actor_cols = "did, handle, display_name, avatar_url, hidden, labels, created_at, associated, pds, followers_count, follows_count, posts_count, last_activity_at, quality_score";

/// named column indexes matching actor_cols order
pub const Col = struct {
    pub const did: usize = 0;
    pub const handle: usize = 1;
    pub const display_name: usize = 2;
    pub const avatar_url: usize = 3;
    pub const hidden: usize = 4;
    pub const labels: usize = 5;
    pub const created_at: usize = 6;
    pub const associated: usize = 7;
    pub const pds: usize = 8;
    pub const followers_count: usize = 9;
    pub const follows_count: usize = 10;
    pub const posts_count: usize = 11;
    pub const last_activity_at: usize = 12;
    pub const quality_score: usize = 13;
};

/// index of the first column appended after actor_cols (updated_at)
const trailing_col: usize = Col.quality_score + 1;

/// Per-row projection context. `arena` is reset by the caller after each row;
/// `updated_at` is the source timestamp; `local` is for reading the old row
/// from overlay-then-base through a read lease (only read-pool conns have the
/// `idx` snapshot attached).
const OverlayCtx = struct {
    arena: Allocator,
    updated_at: i64,
    local: *LocalDb,
};

fn timestamp() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
}

/// Keyset page ≥2 of the incremental drain. The `updated_at >= ?1` anchor is
/// load-bearing: it gives the planner an index SEARCH on
/// idx_actors_updated_at even with bound params. The plain OR keyset form
/// `(updated_at > ? OR (updated_at = ? AND did > ?))` degrades to a full
/// index SCAN of the ~8M-row corpus when the values are parameters — that
/// scan blew the 60s fetch deadline on every page-2 query and starved the
/// drain to one page per cycle (Aug 3–14 2026 staleness incident). Guarded
/// by the planner test at the bottom of this file.
const incremental_paged_sql = "SELECT " ++ actor_cols ++ ", updated_at FROM actors WHERE handle != '' AND updated_at >= ?1 AND (updated_at > ?1 OR did > ?2) ORDER BY updated_at, did LIMIT 500";
const incremental_first_sql = "SELECT " ++ actor_cols ++ ", updated_at FROM actors WHERE handle != '' AND updated_at > ?1 ORDER BY updated_at, did LIMIT 500";

// 500-row pages keep responses ~300KB (the 2026-08-03 wedge bit on a 1.1MiB page).
const BATCH_SIZE = 500;
const SYNC_INTERVAL_S = 300;
/// Turso prunes tombstones after this long (must match cron.ts). A watermark
/// older than this can miss deletions until the next promote bakes them in.
const TOMBSTONE_RETENTION_S = 7 * 24 * 3600;

// Liveness watchdog, mirroring the ingester's.
//
// On 2026-07-28 the sync loop stopped making progress and stayed stopped for
// 48 hours. It did not crash and did not log: syncLoop catches every error and
// continues, so a hang INSIDE incrementalSync (a Turso read that never returns)
// is indistinguishable from a healthy idle loop. Exiting lets fly restart us,
// which is exactly how the same failure was handled for the ingester.
//
// Fed by every applied page, every compaction chunk, and every completed pass.
const SYNC_STALL_SECS: i64 = 20 * 60;
/// overlay compaction chunks per incremental pass (~7s each on the fly volume
/// at 20k rows: ≈ 12 min worst case, inside one watchdog window even without
/// the per-chunk progress note).
const INCREMENTAL_COMPACT_CHUNKS: u64 = 100;
pub var last_progress_at: std.atomic.Value(i64) = .init(0);

pub fn noteProgress() void {
    last_progress_at.store(timestamp(), .monotonic);
}

pub fn watchdogLoop() void {
    last_progress_at.store(timestamp(), .monotonic);
    while (true) {
        io.sleep(std.Io.Duration.fromSeconds(60), .awake) catch return;
        const age = timestamp() - last_progress_at.load(.monotonic);
        if (age >= SYNC_STALL_SECS) {
            log.err("sync watchdog: no progress in {d}s, exiting for restart", .{age});
            fatal.exitForRestart();
        }
    }
}

/// Where a pass starts reading Turso from. The attached snapshot already
/// carries every row at or below its watermark, so a persisted `last_sync`
/// behind it (a long outage, a fresh volume) resumes at the snapshot instead of
/// paging rows the base will only skip.
fn resumePoint(last_sync: i64, snapshot_watermark: i64) i64 {
    return @max(last_sync, snapshot_watermark);
}

fn readLastSync(local: *LocalDb, conn: zqlite.Conn) i64 {
    const s = logfire.span("sync.read_last_sync", .{});
    defer s.end();
    local.lock();
    defer local.unlock();
    const row = conn.row("SELECT value FROM sync_meta WHERE key = 'last_sync'", .{}) catch {
        s.setStatus(.@"error", "query failed");
        return 0;
    };
    if (row) |r| {
        defer r.deinit();
        const val = r.text(0);
        const ts = if (val.len == 0) 0 else std.fmt.parseInt(i64, val, 10) catch 0;
        s.setAttribute("last_sync", ts);
        s.setStatus(.ok, null);
        return ts;
    }
    s.setStatus(.ok, null);
    return 0;
}

fn writeLastSync(local: *LocalDb, conn: zqlite.Conn, ts: i64) void {
    local.lock();
    defer local.unlock();
    var buf: [20]u8 = undefined;
    const s = std.fmt.bufPrint(&buf, "{d}", .{ts}) catch return;
    conn.exec("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync', ?)", .{s}) catch {};
}

/// One pass: page Turso rows newer than the watermark into the overlay, then
/// the actors that became unsearchable and the tombstones. Only advances the
/// watermark to "now" when every query and write succeeded; page progress is
/// persisted as it goes.
pub fn incrementalSync(turso: *TursoClient, local: *LocalDb) !void {
    const span = logfire.span("sync.incremental", .{});
    defer span.end();

    const conn = local.getConn() orelse {
        span.setStatus(.@"error", "LocalNotOpen");
        return error.LocalNotOpen;
    };

    // Deltas are gated on the attached snapshot's watermark: only rows NEWER
    // than the base get projected (older state is already baked in). No
    // snapshot → nothing to project into; the promote watcher is fetching one.
    const snapshot_wm: i64 = @intCast(local.attachedMetaInt(index_schema.meta.source_watermark));
    if (snapshot_wm == 0) {
        log.info("incremental: no snapshot attached, waiting for promote", .{});
        span.setAttribute("skipped", "no_snapshot");
        span.setStatus(.ok, null);
        return;
    }

    const last_sync_ts = readLastSync(local, conn);
    const scope_policy = try local.attachedMetaText(std.heap.page_allocator, "pds_scope_policy");
    defer if (scope_policy) |policy| std.heap.page_allocator.free(policy);
    const scope_ready = if (scope_policy) |policy| try scopePolicyReady(local, policy) else true;
    const from = if (scope_ready) resumePoint(last_sync_ts, snapshot_wm) else snapshot_wm;
    log.info("incremental: last_sync={d} snapshot_watermark={d} from={d}", .{ last_sync_ts, snapshot_wm, from });
    span.setAttribute("from", from);

    const now = timestamp();
    if (now - from > TOMBSTONE_RETENTION_S) {
        log.warn("incremental: watermark is {d}s old, past tombstone retention — deletions before it are only recovered by the next promote", .{now - from});
        span.setAttribute("past_tombstone_retention", true);
    }

    // rows read from Turso but NOT projected because their timestamp is at or
    // below the snapshot watermark — the base already reflects them. Non-zero
    // only right after a promote whose watermark passed `last_sync`.
    var projection_skipped: usize = 0;
    var ov_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer ov_arena.deinit();

    // buffer: subtract 300s to catch stragglers (matches pub-search)
    const since_ts = from - 300;
    var since_buf: [20]u8 = undefined;
    const since_str = std.fmt.bufPrint(&since_buf, "{d}", .{since_ts}) catch return;

    var updated: usize = 0;
    var error_count: usize = 0;
    var had_turso_error = false;
    {
        var cursor_ts_buf: [20]u8 = undefined;
        var cursor_did_buf: [128]u8 = undefined;
        @memcpy(cursor_ts_buf[0..since_str.len], since_str);
        var cursor_ts: []const u8 = cursor_ts_buf[0..since_str.len];
        var cursor_did_len: usize = 0;
        // one retry per pass: a single transient turso failure shouldn't abort
        // the whole drain (each aborted pass caps catch-up at one page per cycle)
        var page_retries: u2 = 1;

        while (true) {
            const cursor_did: []const u8 = if (cursor_did_len > 0) cursor_did_buf[0..cursor_did_len] else "";
            const args: []const []const u8 = if (cursor_did_len > 0) &.{ cursor_ts, cursor_did } else &.{cursor_ts};
            const sql = if (cursor_did_len > 0) incremental_paged_sql else incremental_first_sql;

            log.info("incremental: querying updated actors (cursor_ts={s})", .{cursor_ts});
            const q_span = logfire.span("sync.query.updated", .{});

            var result = turso.query(sql, args) catch |err| {
                if (err == error.Canceled) {
                    q_span.end();
                    return err;
                }
                log.err("incremental query failed: {}", .{err});
                if (page_retries > 0) {
                    page_retries -= 1;
                    log.info("incremental: retrying page after {s}", .{@errorName(err)});
                    q_span.setStatus(.@"error", "query failed; retrying");
                    q_span.end();
                    continue;
                }
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

            log.info("incremental: projecting {d} updated actors", .{result.rows.len});
            {
                const a_span = logfire.span("sync.apply.updated", .{});
                defer a_span.end();

                local.lock();
                defer local.unlock();
                conn.exec("BEGIN", .{}) catch {};
                for (result.rows) |row| {
                    const ua = row.int(trailing_col);
                    if (ua <= snapshot_wm) {
                        projection_skipped += 1;
                        continue;
                    }
                    projectRow(conn, row, .{ .arena = ov_arena.allocator(), .updated_at = ua, .local = local }) catch {
                        error_count += 1;
                        continue;
                    };
                    _ = ov_arena.reset(.retain_capacity);
                    updated += 1;
                }
                conn.exec("COMMIT", .{}) catch {};

                a_span.setAttribute("rows", @as(i64, @intCast(result.rows.len)));
                a_span.setStatus(.ok, null);
            }

            const last_row = result.rows[result.rows.len - 1];
            const last_ua_int = last_row.int(trailing_col);
            const last_did = last_row.text(Col.did);

            const ua_str = std.fmt.bufPrint(&cursor_ts_buf, "{d}", .{last_ua_int}) catch break;
            cursor_ts = ua_str;
            if (last_did.len > 0 and last_did.len <= cursor_did_buf.len) {
                @memcpy(cursor_did_buf[0..last_did.len], last_did);
                cursor_did_len = last_did.len;
            }

            // persist progress per page: "all updates <= last_ua_int are
            // applied" holds because the keyset is ordered by updated_at.
            // Without this, a page whose fetch wedges (2026-08-03: the read
            // of a >1MiB response hung forever; watchdog restarts replayed
            // the cycle from the same watermark and re-wedged on the same
            // page) turns into an infinite restart loop. With it, each
            // watchdog cycle advances at least one page.
            writeLastSync(local, conn, last_ua_int);
            noteProgress();

            if (result.rows.len < BATCH_SIZE) break;
        }
    }

    // actors that became unsearchable (handle cleared)
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
            if (err == error.Canceled) return err;
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
                const ua = row.int(1);
                if (ua <= snapshot_wm) {
                    projection_skipped += 1;
                    continue;
                }
                projectRemoval(conn, row.text(0), "incremental.cleared", .{ .arena = ov_arena.allocator(), .updated_at = ua, .local = local }) catch {
                    error_count += 1;
                    continue;
                };
                _ = ov_arena.reset(.retain_capacity);
                cleared += 1;
            }
            conn.exec("COMMIT", .{}) catch {};
        }
    }

    // tombstones
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
            if (err == error.Canceled) return err;
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
                const del_at = row.int(1);
                if (del_at <= snapshot_wm) {
                    projection_skipped += 1;
                    continue;
                }
                projectRemoval(conn, row.text(0), "incremental.tombstone", .{ .arena = ov_arena.allocator(), .updated_at = del_at, .local = local }) catch {
                    error_count += 1;
                    continue;
                };
                _ = ov_arena.reset(.retain_capacity);
                deleted += 1;
            }
            conn.exec("COMMIT", .{}) catch {};
        }
    }

    // only advance the watermark to now when everything succeeded
    const had_error = had_turso_error or error_count > 0;
    if (had_error) {
        span.setAttribute("error_count", @as(i64, @intCast(error_count)));
        span.setStatus(.@"error", "incremental sync had errors");
        log.err("incremental: done with errors (updated={d}, cleared={d}, deleted={d}, errors={d})", .{
            updated, cleared, deleted, error_count,
        });
    } else {
        writeLastSync(local, conn, timestamp());
        if (scope_policy) |policy| {
            local.lock();
            defer local.unlock();
            try conn.exec("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('pds_scope_policy',?)", .{policy});
        }
        {
            local.lock();
            defer local.unlock();
            // TRUNCATE (not PASSIVE): the read pool nearly always has at least
            // one slot active under live load, and PASSIVE never reclaims frames
            // while a reader exists — WAL grows unbounded (we observed 748MB
            // bloat in ~16min, dragging per-query latency from <50ms to >1s).
            // TRUNCATE waits up to the writer's busy_timeout for readers to
            // release their snapshots, then truncates the WAL file to zero.
            // If it can't (sustained read pressure), it returns busy without
            // throwing; we'll retry next cycle.
            conn.exec("PRAGMA wal_checkpoint(TRUNCATE)", .{}) catch {};
        }
        span.setStatus(.ok, null);
        log.info("incremental: done (updated={d}, cleared={d}, deleted={d})", .{ updated, cleared, deleted });
    }

    // Prune overlay rows the attached base already covers, a bounded slice per
    // pass, so the overlay never ratchets up to a compaction longer than the
    // watchdog (the 2026-08-19..29 loop). Each chunk feeds the watchdog.
    const r = local.compactOverlay(snapshot_wm, INCREMENTAL_COMPACT_CHUNKS, noteProgress);
    span.setAttribute("compact_deleted", @as(i64, @intCast(r.deleted)));
    span.setAttribute("compact_complete", r.complete);

    span.setAttribute("updated", @as(i64, @intCast(updated)));
    span.setAttribute("deleted", @as(i64, @intCast(deleted)));
    span.setAttribute("cleared", @as(i64, @intCast(cleared)));
    span.setAttribute("projection_skipped", @as(i64, @intCast(projection_skipped)));
}

/// A row assembled from JSON rather than read back out of Turso.
///
/// projectRow takes `anytype` and only ever calls .text/.int/.float, so the
/// identical overlay projection the sync loop uses works unchanged on a row
/// handed to us over HTTP. No second projection implementation to drift.
pub const InlineRow = struct {
    vals: [14][]const u8,
    ints: [5]i64 = @splat(0),
    qs: f64 = 0,

    pub fn text(self: *const InlineRow, i: usize) []const u8 {
        return self.vals[i];
    }
    pub fn int(self: *const InlineRow, i: usize) i64 {
        return switch (i) {
            Col.hidden => self.ints[0],
            Col.followers_count => self.ints[1],
            Col.follows_count => self.ints[2],
            Col.posts_count => self.ints[3],
            Col.last_activity_at => self.ints[4],
            else => 0,
        };
    }
    pub fn float(self: *const InlineRow, i: usize) f64 {
        return if (i == Col.quality_score) self.qs else 0;
    }
};

/// Apply one actor the caller already has, immediately.
///
/// The point of this path is that an explicit "index this account" must not
/// wait for the next 5-minute sync tick. The first version took only a DID and
/// re-read the row FROM TURSO, which put the fast path through the slowest,
/// most contended component in the system: measured 2026-07-31, an indexed
/// point lookup took 28s while a bulk job was running, so "instant" became
/// "hung". The worker already holds the row — it wrote it moments earlier — so
/// it sends the row and nothing here touches Turso at all.
///
/// Idempotent against the sync loop, which will later project the same state.
pub fn applyInline(local: *LocalDb, row: *const InlineRow, updated_at: i64) !void {
    const conn = local.getConn() orelse return error.NoConn;
    const snapshot_wm: i64 = @intCast(local.attachedMetaInt(index_schema.meta.source_watermark));
    if (snapshot_wm == 0) return error.NoSnapshot;

    // A freshly-indexed actor is always newer than the base, so in practice
    // this is always on; the guard exists so a replayed old row cannot re-add
    // a prefix the base already accounts for.
    if (updated_at <= snapshot_wm) {
        log.info("inline: {s} at {d} is not newer than the snapshot ({d}); nothing to project", .{ row.text(Col.did), updated_at, snapshot_wm });
        return;
    }

    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();

    local.lock();
    defer local.unlock();

    conn.exec("BEGIN", .{}) catch {};
    projectRow(conn, row, .{ .arena = arena.allocator(), .updated_at = updated_at, .local = local }) catch |err| {
        conn.exec("ROLLBACK", .{}) catch {};
        return err;
    };
    conn.exec("COMMIT", .{}) catch {};
    log.info("inline: projected {s}", .{row.text(Col.did)});
}

pub fn syncLoop(allocator: Allocator, local: *LocalDb) void {
    log.info("sync loop started", .{});
    var turso = TursoClient.init(allocator) catch |err| {
        log.err("turso client init failed: {}, sync disabled", .{err});
        return;
    };
    defer turso.deinit();

    // Nothing to project into until the promote watcher has attached a
    // snapshot (a fresh volume downloads one first, ~15 min). Keep the
    // watchdog fed while waiting: this is expected idleness, not a wedge.
    while (!local.isReady()) {
        noteProgress();
        io.sleep(.{ .nanoseconds = 5 * std.time.ns_per_s }, .real) catch return;
    }

    incrementalSync(&turso, local) catch |err| {
        if (err == error.Canceled) return;
        log.err("initial sync failed: {}", .{err});
    };

    while (true) {
        io.sleep(.{ .nanoseconds = SYNC_INTERVAL_S * std.time.ns_per_s }, .real) catch return;

        incrementalSync(&turso, local) catch |err| {
            if (err == error.Canceled) return;
            log.err("periodic sync failed: {}", .{err});
        };
        // a completed pass is progress even when it applied nothing; a FAILED
        // pass counts too — the watchdog is for silence, and a loop that errors
        // every 5 minutes is loud enough to alert on separately
        noteProgress();
    }
}

/// Old (handle, display_name) of an actor as serving currently knows it —
/// the values whose prefix keys a projection must suppress.
const OldRow = struct {
    handle_buf: [256]u8 = undefined,
    name_buf: [512]u8 = undefined,
    pds_buf: [2048]u8 = undefined,
    pds: ?[]const u8 = null,
    handle: ?[]const u8 = null,
    name: ?[]const u8 = null,
};

/// Old handle/display from the overlay blob, falling back to the attached
/// base blob — the same bytes serving uses, so the keys diffed away are
/// exactly the keys that exist. Goes through a read lease because only
/// read-pool conns have the snapshot attached. A NULL overlay blob
/// (tombstone) or an actor in neither table yields an empty old row: there
/// are no live prefix keys to diff away. Returns false only when the lookup
/// itself failed (no lease, oversized blob), in which case the caller
/// projects with an empty old row and the stale keys wait for the next
/// promote to bake them out.
///
/// Phase 2 of docs/replica-removal-plan.md ran this alongside the `actors`
/// replica for two days; every disagreement class traced to the replica being
/// the wrong source (docs/search-replica-research.md, addendum).
fn snapshotOldFromOverlayBase(local: *LocalDb, did: []const u8, out: *OldRow) bool {
    const lease = local.checkoutRead() catch return false;
    defer lease.release();

    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena_state.deinit();
    return oldFromLease(arena_state.allocator(), lease, did, out);
}

fn oldFromLease(arena: std.mem.Allocator, lease: LocalDb.ReadLease, did: []const u8, out: *OldRow) bool {
    var blob: ?[]const u8 = null;
    {
        var rows = lease.query("SELECT blob FROM actor_overlay WHERE did = ?", .{did}) catch return false;
        defer rows.deinit();
        if (rows.next()) |row| {
            if (row.nullableBlob(0)) |b| {
                if (b.len > 16384) return false;
                blob = arena.dupe(u8, b) catch return false;
            } else return true;
        }
    }
    if (blob == null) blob = lease.baseActor(arena, did) catch return false;
    const b = blob orelse return true;
    if (b.len > 16384) return false;
    const ac = pack.unpackActor(b) catch return false;
    if (ac.pds.len > out.pds_buf.len) return false;
    @memcpy(out.pds_buf[0..ac.pds.len], ac.pds);
    out.pds = out.pds_buf[0..ac.pds.len];
    if (ac.handle.len > 0 and ac.handle.len <= out.handle_buf.len) {
        @memcpy(out.handle_buf[0..ac.handle.len], ac.handle);
        out.handle = out.handle_buf[0..ac.handle.len];
    }
    if (ac.display.len > 0 and ac.display.len <= out.name_buf.len) {
        @memcpy(out.name_buf[0..ac.display.len], ac.display);
        out.name = out.name_buf[0..ac.display.len];
    }
    return true;
}

test "unified base old actor suppresses renamed prefixes and preserves large sync limit" {
    const bytes = @embedFile("../index/unified_fixture.bin");
    var digest: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(bytes, &digest, .{});
    const hex = std.fmt.bytesToHex(digest, .lower);
    const map = try @import("../index/flat_prefix.zig").Map.validate(bytes, &hex);
    var conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try @import("../index/schema.zig").initOverlay(conn);
    var local = LocalDb.init(std.testing.allocator);
    const lease: LocalDb.ReadLease = .{ .db = &local, .slot = 0, .conn = conn, .prefix_map = map };
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var old: OldRow = .{};
    try std.testing.expect(oldFromLease(arena, lease, "did:arbitrary:small", &old));
    try std.testing.expectEqualStrings("old.example", old.handle.?);
    try overlay.projectUpdateWithPds(arena, conn, .{
        .did = "did:arbitrary:small",
        .handle = "new.example",
        .display_name = "New Name",
        .avatar_url = "",
        .labels = "[]",
        .associated = "{}",
        .pds = "",
        .created_at = "",
        .hidden = false,
        .quality_score = 2,
    }, old.handle.?, old.name.?, old.pds orelse "", 2);
    const suppressed = (try conn.row("SELECT op FROM prefix_overlay WHERE key='h:old' AND did='did:arbitrary:small'", .{})).?;
    defer suppressed.deinit();
    try std.testing.expectEqual(@as(i64, 0), suppressed.int(0));
    old = .{};
    try std.testing.expect(oldFromLease(arena, lease, "did:arbitrary:small", &old));
    try std.testing.expectEqualStrings("new.example", old.handle.?);
    old = .{};
    try std.testing.expect(!oldFromLease(arena, lease, "did:web:custom.example/path", &old));
    try conn.exec("UPDATE actor_overlay SET blob=NULL WHERE did='did:arbitrary:small'", .{});
    old = .{};
    try std.testing.expect(oldFromLease(arena, lease, "did:arbitrary:small", &old));
    try std.testing.expect(old.handle == null);
}

/// Project one updated actor into the overlay, inside the caller's
/// transaction on the write conn. `row` only needs .text/.int/.float.
fn projectRow(conn: zqlite.Conn, row: anytype, ctx: OverlayCtx) !void {
    const did = row.text(Col.did);
    var old: OldRow = .{};
    if (!snapshotOldFromOverlayBase(ctx.local, did, &old)) {
        log.warn("old-row lookup failed for {s}; projecting without suppression", .{did});
    }
    const scopes = try pdsScopes(ctx.local, old.pds orelse "", row.text(Col.pds));
    try overlay.projectUpdateWithPds(ctx.arena, conn, .{
        .did = did,
        .handle = row.text(Col.handle),
        .display_name = row.text(Col.display_name),
        .avatar_url = row.text(Col.avatar_url),
        .labels = row.text(Col.labels),
        .associated = row.text(Col.associated),
        .pds = row.text(Col.pds),
        .pds_indexed = scopes.new,
        .created_at = row.text(Col.created_at),
        .hidden = row.int(Col.hidden) != 0,
        .quality_score = @floatCast(row.float(Col.quality_score)),
    }, old.handle orelse "", old.name orelse "", if (scopes.old) old.pds orelse "" else "", ctx.updated_at);
}

/// Global tombstone in the overlay so serving suppresses the actor
/// immediately. Used for both "became unsearchable" and "tombstoned".
fn projectRemoval(conn: zqlite.Conn, did: []const u8, caller: []const u8, ctx: OverlayCtx) !void {
    var old: OldRow = .{};
    if (!snapshotOldFromOverlayBase(ctx.local, did, &old)) {
        log.warn("{s}: old-row lookup failed for {s}; tombstoning without suppression", .{ caller, did });
    }
    const scopes = try pdsScopes(ctx.local, old.pds orelse "", "");
    try overlay.projectDeleteWithPds(ctx.arena, conn, did, old.handle orelse "", old.name orelse "", if (scopes.old) old.pds orelse "" else "", ctx.updated_at);
}

// ─── tests ────────────────────────────────────────────────────────────────

test "resumePoint never starts behind the attached snapshot" {
    const t = std.testing;
    // fresh volume: no last_sync → replay from the snapshot watermark
    try t.expectEqual(@as(i64, 1_788_000_000), resumePoint(0, 1_788_000_000));
    // long outage: last_sync behind the snapshot → the base already has those rows
    try t.expectEqual(@as(i64, 1_788_000_000), resumePoint(1_787_000_000, 1_788_000_000));
    // steady state: last_sync ahead of the snapshot → resume where we left off
    try t.expectEqual(@as(i64, 1_788_500_000), resumePoint(1_788_500_000, 1_788_000_000));
}

// Regression guard for the Aug 3–14 2026 staleness incident. Turso's
// server-side planner degraded the plain OR keyset form to a full index
// SCAN when the values were bound params; stock local SQLite plans both
// forms fine, so the live planner guard is smoke.py's turso EXPLAIN check.
// Here we pin the seek-friendly `updated_at >= ?1` anchor shape and verify
// the query stays valid + SEARCHable against the mirrored schema.
test "incremental paged keyset query keeps its seek anchor and plans as SEARCH" {
    try std.testing.expect(std.mem.indexOf(u8, incremental_paged_sql, "updated_at >= ?1") != null);
    const testing = std.testing;
    const conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();

    // mirror the remote actors schema (columns referenced by actor_cols)
    // and the composite index the keyset walk relies on
    try conn.exec(
        \\CREATE TABLE actors (
        \\  did TEXT PRIMARY KEY,
        \\  handle TEXT NOT NULL DEFAULT '',
        \\  display_name TEXT DEFAULT '',
        \\  avatar_url TEXT DEFAULT '',
        \\  updated_at INTEGER NOT NULL DEFAULT 0,
        \\  hidden INTEGER NOT NULL DEFAULT 0,
        \\  labels TEXT NOT NULL DEFAULT '[]',
        \\  created_at TEXT DEFAULT '',
        \\  associated TEXT DEFAULT '{}',
        \\  pds TEXT DEFAULT '',
        \\  followers_count INTEGER DEFAULT 0,
        \\  follows_count INTEGER DEFAULT 0,
        \\  posts_count INTEGER DEFAULT 0,
        \\  last_activity_at INTEGER DEFAULT 0,
        \\  quality_score REAL DEFAULT 0
        \\)
    , .{});
    try conn.exec("CREATE INDEX idx_actors_updated_at ON actors(updated_at, did)", .{});

    var rows = try conn.rows(
        "EXPLAIN QUERY PLAN " ++ incremental_paged_sql,
        .{ "1786740000", "did:plc:aaaa" },
    );
    defer rows.deinit();

    var saw_search = false;
    while (rows.next()) |row| {
        const detail = row.text(3);
        try testing.expect(std.mem.indexOf(u8, detail, "SCAN") == null);
        if (std.mem.indexOf(u8, detail, "SEARCH") != null) saw_search = true;
    }
    try testing.expect(saw_search);
}

fn scopePolicyReady(local: *LocalDb, policy: []const u8) !bool {
    const lease = try local.checkoutRead();
    defer lease.release();
    var rows = try lease.query("SELECT value FROM sync_meta WHERE key='pds_scope_policy'", .{});
    defer rows.deinit();
    const row = rows.next() orelse return false;
    return std.mem.eql(u8, row.text(0), policy);
}

const PdsScopes = struct { old: bool = false, new: bool = false };

fn pdsScopes(local: *LocalDb, old_endpoint: []const u8, new_endpoint: []const u8) !PdsScopes {
    const pds = @import("../index/pds.zig");
    const old_host = pds.host(old_endpoint) orelse "";
    const new_host = pds.host(new_endpoint) orelse "";
    if (old_host.len == 0 and new_host.len == 0) return .{};
    const lease = try local.checkoutRead();
    defer lease.release();
    {
        var rows = try lease.query("SELECT value FROM idx.index_meta WHERE key='pds_scope_version'", .{});
        defer rows.deinit();
        const row = rows.next() orelse return .{};
        if (!std.mem.eql(u8, row.text(0), "1")) return .{};
    }
    var result: PdsScopes = .{};
    var rows = try lease.query("SELECT host FROM idx.pds_scopes WHERE enabled=1 AND host IN (?,?)", .{ old_host, new_host });
    defer rows.deinit();
    while (rows.next()) |row| {
        if (std.ascii.eqlIgnoreCase(row.text(0), old_host)) result.old = true;
        if (std.ascii.eqlIgnoreCase(row.text(0), new_host)) result.new = true;
    }
    return result;
}
