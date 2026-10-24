//! Local SQLite state using zqlite.
//! The search role keeps the live overlay tables and sync metadata here, with
//! the read-only prefix-index snapshot ATTACHed as `idx` on every read conn.
//! Serving = snapshot + overlay; there is no local copy of the corpus.

const std = @import("std");
const io = std.Options.debug_io;
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;
const index_schema = @import("../index/schema.zig");
const overlay = @import("../index/overlay.zig");

const log = std.log.scoped(.local_db);

const LocalDb = @This();
const FlatMap = @import("../index/flat_prefix.zig").Map;
const FlatCache = @import("../index/flat_prefix.zig").Cache;

/// One read connection per HTTP worker thread is wrong (deadlocks under
/// concurrent /search) and a single shared one is also wrong (zqlite.Conn
/// is one statement at a time). The pool gives each in-flight search its
/// own exclusive conn for the duration of the request, mutex-checkout
/// style; release with `lease.release()` (or `defer`).
const READ_POOL_DEFAULT_SIZE: usize = 4;
/// How long checkoutRead is willing to wait when all slots are busy
/// before returning error.PoolExhausted. Caller (HTTP handler) should
/// translate that to 503 so the worker falls back fast.
const READ_WAIT_DEFAULT_MS: u64 = 250;
const READ_TICK_MS: u64 = 5;

fn milliTimestamp() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_ms));
}

conn: ?zqlite.Conn = null,

/// Pool of read-only connections handed out one-per-request. Each slot
/// has its own mutex so concurrent /search calls don't serialize behind
/// one queue. WAL allows N concurrent readers safely. Sized at open()
/// from READ_POOL_SIZE env var (default 4) so we can tune without a
/// recompile when load characteristics change.
read_conns: []?zqlite.Conn = &.{},
prefix_map: ?FlatMap = null,
read_slot_mu: []std.Io.Mutex = &.{},
read_caches: []FlatCache = &.{},
read_rr: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
read_wait_budget_ms: u64 = READ_WAIT_DEFAULT_MS,

/// Dedicated read conn for /health and other operational lookups. Kept
/// out of the pool so a saturated search load can NEVER wedge health.
/// Single-statement queries only — not a hot path.
meta_conn: ?zqlite.Conn = null,
meta_mu: std.Io.Mutex = std.Io.Mutex.init,

allocator: Allocator,
is_ready: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
mutex: std.Io.Mutex = std.Io.Mutex.init, // protects write conn only
path: []const u8 = "",

/// Absolute path to the prefix-index snapshot currently ATTACHed (as `idx`)
/// on every read conn, or null if none. Owned by this struct. Set only via
/// swapSnapshot, which rebuilds the read pool so all conns attach atomically.
snapshot_path: ?[]const u8 = null,

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return std.mem.span(p);
    return null;
}

fn envUsize(name: [*:0]const u8, default: usize, min: usize, max: usize) usize {
    const v = cGetenv(name) orelse return default;
    const parsed = std.fmt.parseInt(usize, v, 10) catch return default;
    return @min(@max(parsed, min), max);
}

fn envU64(name: [*:0]const u8, default: u64, min: u64, max: u64) u64 {
    const v = cGetenv(name) orelse return default;
    const parsed = std.fmt.parseInt(u64, v, 10) catch return default;
    return @min(@max(parsed, min), max);
}

pub fn init(allocator: Allocator) LocalDb {
    return .{ .allocator = allocator };
}

/// Tables the pre-phase-3 search role kept: the 11 GB `actors` replica, its
/// FTS shadow, and the resync staging tables. A volume that predates the cut
/// still carries them; drop them and VACUUM so the file shrinks to the
/// overlay. VACUUM copies only live pages, so its cost scales with the
/// overlay, not with the 11 GB being discarded. No-op on a fresh volume.
const replica_tables = [_][]const u8{ "actors", "actors_fts", "actors_stage", "actors_fts_stage" };

fn dropReplicaTables(self: *LocalDb) void {
    const c = self.conn orelse return;
    var dropped: usize = 0;
    for (replica_tables) |name| {
        const row = c.row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", .{name}) catch continue;
        const present = row != null;
        if (row) |r| r.deinit();
        if (!present) continue;
        var sql_buf: [64]u8 = undefined;
        const sql = std.fmt.bufPrint(&sql_buf, "DROP TABLE IF EXISTS {s}", .{name}) catch continue;
        c.exec(sql, .{}) catch |err| {
            log.warn("drop {s} failed: {}", .{ name, err });
            continue;
        };
        dropped += 1;
        log.info("dropped replica table {s}", .{name});
    }
    if (dropped == 0) return;
    const t0 = milliTimestamp();
    log.info("vacuum after dropping {d} replica tables", .{dropped});
    c.exec("VACUUM", .{}) catch |err| {
        log.warn("vacuum failed: {} (space is reclaimed on the next successful one)", .{err});
        return;
    };
    log.info("vacuum done in {d}ms", .{milliTimestamp() - t0});
}

pub fn open(self: *LocalDb) !void {
    const path_env = cGetenv("LOCAL_DB_PATH") orelse "/data/local.db";
    self.path = path_env;

    // ensure SQLite temp directory exists on the persistent volume
    const tmp_dir = cGetenv("SQLITE_TMPDIR") orelse "/data/tmp";
    std.Io.Dir.createDirPath(.cwd(), io, tmp_dir) catch |err| {
        log.warn("failed to create temp dir {s}: {}", .{ tmp_dir, err });
    };

    try self.openDb(path_env);
}

fn openDb(self: *LocalDb, path_env: []const u8) !void {
    var path_buf: [256]u8 = undefined;
    if (path_env.len >= path_buf.len) return error.PathTooLong;
    @memcpy(path_buf[0..path_env.len], path_env);
    path_buf[path_env.len] = 0;
    const path: [*:0]const u8 = path_buf[0..path_env.len :0];

    log.info("opening {s}", .{path_env});

    const flags = zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite;
    self.conn = zqlite.open(path, flags) catch |err| {
        log.err("failed to open write conn: {}", .{err});
        return err;
    };

    _ = self.conn.?.exec("PRAGMA journal_mode=WAL", .{}) catch {};
    _ = self.conn.?.exec("PRAGMA busy_timeout=5000", .{}) catch {};
    _ = self.conn.?.exec("PRAGMA synchronous=NORMAL", .{}) catch {}; // safe with WAL
    _ = self.conn.?.exec("PRAGMA cache_size=-20000", .{}) catch {}; // 20MB page cache
    _ = self.conn.?.exec("PRAGMA mmap_size=268435456", .{}) catch {}; // 256MB

    self.dropReplicaTables();
    try self.openReadPool(path);
    try self.createSchema();
    log.info("initialized", .{});
}

pub fn deinit(self: *LocalDb) void {
    self.closeReadConn();
    if (self.read_conns.len > 0) {
        self.allocator.free(self.read_conns);
        self.allocator.free(self.read_slot_mu);
        self.allocator.free(self.read_caches);
        self.read_caches = &.{};
        self.read_conns = &.{};
        self.read_slot_mu = &.{};
    }
    if (self.conn) |c| c.close();
    self.conn = null;
    if (self.snapshot_path) |sp| self.allocator.free(sp);
    self.snapshot_path = null;
}

/// Open the read pool + meta conn. Each conn is read-only with `query_only`
/// set as defense in depth — even if a future bug tries to write, SQLite
/// will reject it. `busy_timeout` is small because we want to fail fast
/// to a higher-level fallback rather than block on a stuck writer.
fn openReadPool(self: *LocalDb, path: [*:0]const u8) !void {
    errdefer {
        for (self.read_conns) |*slot| {
            if (slot.*) |c| c.close();
            slot.* = null;
        }
        if (self.meta_conn) |c| c.close();
        self.meta_conn = null;
        if (self.prefix_map) |dict| dict.close();
        self.prefix_map = null;
    }
    if (self.read_conns.len == 0) {
        const size = envUsize("READ_POOL_SIZE", READ_POOL_DEFAULT_SIZE, 1, 64);
        self.read_conns = try self.allocator.alloc(?zqlite.Conn, size);
        @memset(self.read_conns, null);
        self.read_caches = try self.allocator.alloc(FlatCache, size);
        self.read_slot_mu = try self.allocator.alloc(std.Io.Mutex, size);
        for (self.read_slot_mu) |*mu| mu.* = std.Io.Mutex.init;
        self.read_wait_budget_ms = envU64("READ_POOL_WAIT_MS", READ_WAIT_DEFAULT_MS, 0, 5000);
        log.info("read pool: size={d} wait_budget_ms={d}", .{ size, self.read_wait_budget_ms });
    }

    for (self.read_caches) |*cache| cache.clear();
    for (self.read_conns, 0..) |*slot, i| {
        slot.* = zqlite.open(path, zqlite.OpenFlags.ReadOnly) catch |err| {
            log.err("failed to open read pool conn {d}: {}", .{ i, err });
            return err;
        };
        applyReadPragmas(slot.*.?);
        if (self.snapshot_path) |sp| try attachSnapshot(slot.*.?, sp);
    }

    self.meta_conn = zqlite.open(path, zqlite.OpenFlags.ReadOnly) catch |err| {
        log.err("failed to open meta conn: {}", .{err});
        return err;
    };
    applyReadPragmas(self.meta_conn.?);
    if (self.snapshot_path) |sp| {
        try attachSnapshot(self.meta_conn.?, sp);
        self.prefix_map = try loadPrefixMap(self.allocator, self.meta_conn.?, sp, false);
    }
}

pub const PreparedSnapshot = struct {
    allocator: Allocator,
    map: ?FlatMap = null,
    conns: []?zqlite.Conn,
    meta: ?zqlite.Conn = null,
    path: []const u8,

    pub fn basePrefix(self: PreparedSnapshot, arena: Allocator, key: []const u8) !?[]const u8 {
        if (self.map) |map| return map.findTagged(arena, .prefix, key);
        const row = (try self.meta.?.row("SELECT dids FROM idx.prefix_top_base WHERE key=?", .{key})) orelse return null;
        defer row.deinit();
        return try arena.dupe(u8, row.blob(0));
    }

    pub fn baseActor(self: PreparedSnapshot, arena: Allocator, did: []const u8) !?[]const u8 {
        if (self.map) |map| return map.findTagged(arena, .actor, did);
        const row = (try self.meta.?.row("SELECT blob FROM idx.actor_compact_base WHERE did=?", .{did})) orelse return null;
        defer row.deinit();
        return try arena.dupe(u8, row.blob(0));
    }

    pub fn deinit(self: *PreparedSnapshot) void {
        for (self.conns) |conn| if (conn) |c| c.close();
        self.allocator.free(self.conns);
        if (self.meta) |c| c.close();
        if (self.map) |map| map.close();
        self.allocator.free(self.path);
        self.* = undefined;
    }
};

fn loadPrefixMap(allocator: Allocator, conn: zqlite.Conn, path: []const u8, verified: bool) !?FlatMap {
    const marker = (try conn.row("SELECT value FROM idx.index_meta WHERE key='flat_prefix_version'", .{})) orelse return null;
    defer marker.deinit();
    const digest = (try conn.row("SELECT value FROM idx.index_meta WHERE key='flat_prefix_sha256'", .{})) orelse return error.MissingFlatDigest;
    defer digest.deinit();
    if (verified) {
        const side = try std.fmt.allocPrint(allocator, "{s}.prefix", .{path});
        defer allocator.free(side);
        return try FlatMap.openVerifiedFile(allocator, side);
    }
    return try FlatMap.open(allocator, path, digest.text(0));
}

pub fn prepareSnapshot(self: *LocalDb, snapshot: []const u8) !PreparedSnapshot {
    return self.prepareSnapshotMode(snapshot, false);
}

pub fn prepareVerifiedSnapshot(self: *LocalDb, snapshot: []const u8) !PreparedSnapshot {
    return self.prepareSnapshotMode(snapshot, true);
}

fn prepareSnapshotMode(self: *LocalDb, snapshot: []const u8, verified: bool) !PreparedSnapshot {
    const path = try self.allocator.dupeSentinel(u8, self.path, 0);
    defer self.allocator.free(path);
    const destination = try self.allocator.dupe(u8, snapshot);
    errdefer self.allocator.free(destination);
    const conns = try self.allocator.alloc(?zqlite.Conn, self.read_conns.len);
    @memset(conns, null);
    var prepared: PreparedSnapshot = .{ .allocator = self.allocator, .path = destination, .conns = conns };
    errdefer {
        for (prepared.conns) |conn| if (conn) |c| c.close();
        self.allocator.free(prepared.conns);
        if (prepared.meta) |c| c.close();
        if (prepared.map) |map| map.close();
    }
    prepared.meta = try zqlite.open(path, zqlite.OpenFlags.ReadOnly);
    applyReadPragmas(prepared.meta.?);
    try attachSnapshot(prepared.meta.?, snapshot);
    prepared.map = try loadPrefixMap(self.allocator, prepared.meta.?, snapshot, verified);
    for (prepared.conns) |*conn| {
        conn.* = try zqlite.open(path, zqlite.OpenFlags.ReadOnly);
        applyReadPragmas(conn.*.?);
        try attachSnapshot(conn.*.?, snapshot);
    }
    return prepared;
}

/// ATTACH the immutable snapshot as `idx` on a read conn. Bound parameter
/// (not string interpolation) so a build_id in the path can't break the SQL.
/// The conn is already query_only=ON, which extends to attached databases —
/// so the snapshot is read-only even though ATTACH defaults to read-write.
fn attachSnapshot(c: zqlite.Conn, path: []const u8) !void {
    c.exec("ATTACH DATABASE ? AS idx", .{path}) catch |err| {
        log.err("ATTACH snapshot failed ({s}): {}", .{ path, err });
        return err;
    };

    try validateSnapshotFormat(c);
}

fn validateSnapshotFormat(c: zqlite.Conn) !void {
    const flat = blk: {
        if (try c.row("SELECT value FROM idx.index_meta WHERE key='flat_prefix_version'", .{})) |row| {
            defer row.deinit();
            if (!std.mem.eql(u8, row.text(0), "5")) return error.UnsupportedFlatSnapshotVersion;
            break :blk true;
        }
        break :blk false;
    };
    const shape = (try c.row("SELECT type FROM idx.sqlite_master WHERE name='prefix_top_base'", .{})) orelse return error.InvalidFlatSnapshotSchema;
    defer shape.deinit();
    if (!std.mem.eql(u8, shape.text(0), if (flat) "view" else "table")) return error.InvalidFlatSnapshotSchema;
    if (flat) {
        const actor = (try c.row("SELECT type FROM idx.sqlite_master WHERE name='actor_compact_base'", .{})) orelse return error.InvalidFlatSnapshotSchema;
        defer actor.deinit();
        if (!std.mem.eql(u8, actor.text(0), "view")) return error.InvalidFlatSnapshotSchema;
        inline for (.{
            "SELECT key,dids FROM idx.prefix_top_base LIMIT 1",
            "SELECT did,blob FROM idx.actor_compact_base LIMIT 1",
        }) |sql| {
            if (c.row(sql, .{}) catch return error.InvalidFlatSnapshotSchema) |row| {
                row.deinit();
                return error.InvalidFlatSnapshotSchema;
            }
        }
        inline for (.{ "prefix_top_base", "actor_compact_base" }) |table| {
            const columns = (try c.row("SELECT count(*) FROM pragma_table_info(?, 'idx')", .{table})).?;
            defer columns.deinit();
            if (columns.int(0) != 2) return error.InvalidFlatSnapshotSchema;
        }
    }
}

fn applyReadPragmas(c: zqlite.Conn) void {
    _ = c.exec("PRAGMA query_only=ON", .{}) catch {};
    _ = c.exec("PRAGMA busy_timeout=1000", .{}) catch {};
    _ = c.exec("PRAGMA mmap_size=268435456", .{}) catch {}; // 256MB
    _ = c.exec("PRAGMA cache_size=-20000", .{}) catch {}; // 20MB
}

/// Close all read connections (pool + meta). Called during bootstrap to
/// avoid WAL reader interference with the writer's exclusive operations.
///
/// Lock-then-close: acquires every slot mutex (and meta_mu) BEFORE
/// closing the underlying conns. Without this, an in-flight /search
/// holding a lease while bootstrap closes would dereference a freed
/// conn — exactly the wedge we're trying to eliminate. Locks are
/// released after the conns are nulled so any racing checkoutRead
/// that wins the tryLock observes a null slot and bails out cleanly.
pub fn closeReadConn(self: *LocalDb) void {
    for (self.read_slot_mu) |*mu| mu.lockUncancelable(io);
    self.meta_mu.lockUncancelable(io);

    for (self.read_conns) |*slot| {
        if (slot.*) |c| c.close();
        slot.* = null;
    }
    if (self.meta_conn) |c| c.close();
    self.meta_conn = null;
    if (self.prefix_map) |dict| dict.close();
    self.prefix_map = null;

    self.meta_mu.unlock(io);
    for (self.read_slot_mu) |*mu| mu.unlock(io);
}

/// Reopen read connections after bootstrap completes. Re-acquires the
/// same locks so a checkoutRead racing with reopen sees either "still
/// closed" (null slot) or "fully open" — never a partially-init conn.
pub fn reopenReadConn(self: *LocalDb) !void {
    if (self.read_conns.len > 0 and self.read_conns[0] != null and self.meta_conn != null) return;

    var path_buf: [256]u8 = undefined;
    if (self.path.len >= path_buf.len) return error.PathTooLong;
    @memcpy(path_buf[0..self.path.len], self.path);
    path_buf[self.path.len] = 0;
    const path: [*:0]const u8 = path_buf[0..self.path.len :0];

    for (self.read_slot_mu) |*mu| mu.lockUncancelable(io);
    self.meta_mu.lockUncancelable(io);
    defer {
        self.meta_mu.unlock(io);
        for (self.read_slot_mu) |*mu| mu.unlock(io);
    }

    try self.openReadPool(path);
}

/// Snapshot paths and their sidecars must stay immutable; callers serialize publishers.
/// Prepare and validate the candidate while old leases remain available, then rebuild
/// the pool. A failed preparation leaves the current generation untouched.
pub fn swapSnapshot(self: *LocalDb, new_path: []const u8) !void {
    var prepared = try self.prepareSnapshot(new_path);
    defer prepared.deinit();
    self.activateSnapshot(&prepared);
}

pub fn activateSnapshot(self: *LocalDb, prepared: *PreparedSnapshot) void {
    std.debug.assert(prepared.conns.len == self.read_conns.len);
    for (self.read_slot_mu) |*mu| mu.lockUncancelable(io);
    self.meta_mu.lockUncancelable(io);
    defer {
        self.meta_mu.unlock(io);
        for (self.read_slot_mu) |*mu| mu.unlock(io);
    }
    for (self.read_conns, prepared.conns) |*live, *next| std.mem.swap(?zqlite.Conn, live, next);
    std.mem.swap(?zqlite.Conn, &self.meta_conn, &prepared.meta);
    std.mem.swap(?FlatMap, &self.prefix_map, &prepared.map);
    for (self.read_caches) |*cache| cache.clear();
    const old = self.snapshot_path;
    self.snapshot_path = prepared.path;
    prepared.path = old orelse "";
    self.setReady(true);
    log.info("snapshot attached: {s}", .{self.snapshot_path.?});
}

/// build_id of the currently-attached snapshot, or null if none attached.
/// Caller owns the returned slice. Used by the /index/status debug endpoint.
pub fn attachedBuildId(self: *LocalDb, gpa: Allocator) !?[]const u8 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
    if (self.snapshot_path == null) return null;
    const c = self.meta_conn orelse return null;
    const row = c.row("SELECT value FROM idx.index_meta WHERE key = 'build_id'", .{}) catch return null;
    if (row) |r| {
        defer r.deinit();
        return try gpa.dupe(u8, r.text(0));
    }
    return null;
}

pub const CompactResult = struct { deleted: u64, chunks: u64, complete: bool };

/// Prune sync-derived overlay rows the attached base now covers
/// (updated_at ≤ watermark). Operator rows (source=1) survive. Runs in
/// COMPACT_CHUNK steps under the write mutex, yielding between them; skips
/// entirely while no snapshot is attached (nothing covers those rows yet).
/// `max_chunks` bounds one call so a caller with a liveness budget (the sync
/// loop) can compact a little every pass instead of ratcheting; `on_chunk`
/// lets it feed its watchdog. Results are recorded in sync_meta for
/// /index/status. Best-effort: failures are logged, never fatal.
pub fn compactOverlay(self: *LocalDb, new_watermark: i64, max_chunks: ?u64, on_chunk: ?*const fn () void) CompactResult {
    const c = self.conn orelse return .{ .deleted = 0, .chunks = 0, .complete = false };
    const t0 = milliTimestamp();
    var total: u64 = 0;
    var chunks: u64 = 0;
    var complete = false;
    log.info("overlay compaction started (watermark {d}, max_chunks {?d})", .{ new_watermark, max_chunks });
    while (true) {
        if (max_chunks) |m| if (chunks >= m) break;
        if (!self.isReady()) {
            if (max_chunks != null) break;
            io.sleep(.{ .nanoseconds = 5 * std.time.ns_per_s }, .real) catch {};
            continue;
        }
        self.mutex.lockUncancelable(io);
        const deleted = overlay.compactChunk(c, new_watermark) catch |err| {
            self.mutex.unlock(io);
            log.warn("overlay compaction failed after {d} rows: {}", .{ total, err });
            break;
        };
        self.mutex.unlock(io);
        if (deleted == 0) {
            complete = true;
            break;
        }
        total += deleted;
        chunks += 1;
        if (on_chunk) |f| f();
        if (chunks % 50 == 0) {
            log.info("overlay compaction: {d} rows in {d}ms", .{ total, milliTimestamp() - t0 });
        }
        io.sleep(.{ .nanoseconds = 50 * std.time.ns_per_ms }, .real) catch {};
    }
    const ms = milliTimestamp() - t0;
    log.info("overlay compaction {s}: {d} rows in {d}ms", .{ if (complete) "done" else "paused", total, ms });
    self.recordCompaction(c, total, ms, complete);
    return .{ .deleted = total, .chunks = chunks, .complete = complete };
}

fn recordCompaction(self: *LocalDb, c: zqlite.Conn, deleted: u64, ms: i64, complete: bool) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    var b1: [24]u8 = undefined;
    var b2: [24]u8 = undefined;
    var b3: [24]u8 = undefined;
    const d = std.fmt.bufPrint(&b1, "{d}", .{deleted}) catch "0";
    const m = std.fmt.bufPrint(&b2, "{d}", .{ms}) catch "0";
    const at = std.fmt.bufPrint(&b3, "{d}", .{@divFloor(milliTimestamp(), 1000)}) catch "0";
    c.exec("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('overlay_compact_deleted', ?)", .{d}) catch {};
    c.exec("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('overlay_compact_ms', ?)", .{m}) catch {};
    c.exec("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('overlay_compact_complete', ?)", .{if (complete) "1" else "0"}) catch {};
    c.exec("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('overlay_compact_at', ?)", .{at}) catch {};
}

/// A sync_meta value as an integer, 0 when absent. Read on the meta conn so
/// /index/status never touches the read pool.
pub fn syncMetaInt(self: *LocalDb, key: []const u8) i64 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
    const c = self.meta_conn orelse return 0;
    const row = c.row("SELECT value FROM sync_meta WHERE key = ?", .{key}) catch return 0;
    if (row) |r| {
        defer r.deinit();
        return std.fmt.parseInt(i64, r.text(0), 10) catch 0;
    }
    return 0;
}

/// Rows in actor_overlay — one per actor touched since the last full
/// compaction, so it is bounded by "actors changed", not by the corpus.
/// (prefix_overlay is deliberately NOT counted: it is many rows per actor and
/// a count over it exceeded 110s on 2026-08-29.)
pub fn overlayActorRows(self: *LocalDb) i64 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
    const c = self.meta_conn orelse return -1;
    const row = c.row("SELECT count(*) FROM actor_overlay", .{}) catch return -1;
    if (row) |r| {
        defer r.deinit();
        return r.int(0);
    }
    return -1;
}

pub fn snapshotPath(self: *LocalDb, allocator: Allocator) !?[]const u8 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
    return if (self.snapshot_path) |path| try allocator.dupe(u8, path) else null;
}

/// Text-valued `index_meta` key from the attached snapshot, or null if no
/// snapshot is attached / the key is missing. Caller owns the slice.
pub fn attachedMetaText(self: *LocalDb, gpa: Allocator, key: []const u8) !?[]const u8 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
    if (self.snapshot_path == null) return null;
    const c = self.meta_conn orelse return null;
    const row = c.row("SELECT value FROM idx.index_meta WHERE key = ?", .{key}) catch return null;
    if (row) |r| {
        defer r.deinit();
        return try gpa.dupe(u8, r.text(0));
    }
    return null;
}

/// Integer-valued `index_meta` key from the attached snapshot, or 0 if no
/// snapshot is attached / the key is missing. Used to feed the previous
/// build's actor/prefix counts into the promote sanity floors.
pub fn attachedMetaInt(self: *LocalDb, key: []const u8) u64 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
    if (self.snapshot_path == null) return 0;
    const c = self.meta_conn orelse return 0;
    const row = c.row("SELECT value FROM idx.index_meta WHERE key = ?", .{key}) catch return 0;
    if (row) |r| {
        defer r.deinit();
        return std.fmt.parseInt(u64, r.text(0), 10) catch 0;
    }
    return 0;
}

pub fn isReady(self: *LocalDb) bool {
    return self.is_ready.load(.acquire);
}

pub fn setReady(self: *LocalDb, ready: bool) void {
    self.is_ready.store(ready, .release);
}

fn createSchema(self: *LocalDb) !void {
    const c = self.conn orelse return error.NotOpen;

    c.exec(
        \\CREATE TABLE IF NOT EXISTS sync_meta (
        \\  key TEXT PRIMARY KEY,
        \\  value TEXT
        \\)
    , .{}) catch |err| {
        log.err("failed to create sync_meta table: {}", .{err});
        return err;
    };

    // Prefix-index overlay tables (actor_overlay / prefix_overlay): the live
    // mutation layer the index serving path merges over the base snapshot.
    index_schema.initOverlay(c) catch |err| {
        log.err("failed to create overlay tables: {}", .{err});
        return err;
    };
}

pub const Row = struct {
    stmt: zqlite.Row,

    pub fn text(self: Row, index: usize) []const u8 {
        return self.stmt.text(index);
    }

    pub fn int(self: Row, index: usize) i64 {
        return self.stmt.int(index);
    }

    pub fn float(self: Row, index: usize) f64 {
        return self.stmt.float(index);
    }

    pub fn blob(self: Row, index: usize) []const u8 {
        return self.stmt.blob(index);
    }

    pub fn nullableBlob(self: Row, index: usize) ?[]const u8 {
        return self.stmt.nullableBlob(index);
    }
};

pub const Rows = struct {
    inner: zqlite.Rows,

    pub fn next(self: *Rows) ?Row {
        if (self.inner.next()) |r| {
            return .{ .stmt = r };
        }
        return null;
    }

    pub fn deinit(self: *Rows) void {
        self.inner.deinit();
    }

    pub fn err(self: *Rows) ?anyerror {
        return self.inner.err;
    }
};

pub const PoolError = error{ PoolExhausted, NotOpen };

/// Exclusive lease on one read connection. Callers MUST `release()` (use
/// `defer lease.release()`). Holding a lease across multiple queries is
/// intentional — one /search call makes 3-4 reads, all sharing the same
/// leased conn so it doesn't ping-pong across slots.
pub const ReadLease = struct {
    db: *LocalDb,
    slot: usize,
    conn: zqlite.Conn,
    prefix_map: ?FlatMap = null,
    prefix_cache: ?*FlatCache = null,

    pub fn release(self: ReadLease) void {
        self.db.read_slot_mu[self.slot].unlock(io);
    }

    pub fn query(self: ReadLease, comptime sql: []const u8, args: anytype) !Rows {
        const rows = self.conn.rows(sql, args) catch |e| {
            log.err("lease.query failed: {s}", .{@errorName(e)});
            return e;
        };
        return .{ .inner = rows };
    }

    pub fn baseActor(self: ReadLease, arena: Allocator, did: []const u8) !?[]const u8 {
        if (self.prefix_map) |map| return map.findTaggedCached(arena, .actor, did, self.prefix_cache);
        var rows = try self.query("SELECT blob FROM idx.actor_compact_base WHERE did = ?", .{did});
        defer rows.deinit();
        if (rows.next()) |row| return try arena.dupe(u8, row.blob(0));
        return null;
    }

    /// SELECT with runtime SQL + runtime-sized list of text bind values.
    /// Needed for `WHERE did IN (?, ?, ?, …)` batched lookups where the
    /// placeholder count is only known at runtime.
    pub fn queryTextList(self: ReadLease, sql: []const u8, args_text: []const []const u8) !Rows {
        const stmt = self.conn.prepare(sql) catch |e| {
            log.err("lease.queryTextList prepare failed: {s}", .{@errorName(e)});
            return e;
        };
        errdefer stmt.deinit();
        for (args_text, 0..) |arg, i| {
            try stmt.bindValue(arg, i);
        }
        return .{ .inner = .{ .stmt = stmt, .err = null } };
    }
};

/// Lease one read connection from the pool. Round-robins the first try
/// across slots to spread load, then falls back to a bounded poll. On
/// `error.PoolExhausted` the caller should fail the request fast — the
/// worker will fall back to bsky/turso rather than hold a doomed connection.
pub fn checkoutRead(self: *LocalDb) PoolError!ReadLease {
    if (self.read_conns.len == 0) return error.NotOpen;
    const start = self.read_rr.fetchAdd(1, .monotonic) % self.read_conns.len;

    if (self.tryAcquireSlot(start)) |lease| return lease;

    var elapsed: u64 = 0;
    while (elapsed < self.read_wait_budget_ms) : (elapsed += READ_TICK_MS) {
        io.sleep(.{ .nanoseconds = READ_TICK_MS * std.time.ns_per_ms }, .real) catch return error.PoolExhausted;
        if (self.tryAcquireSlot(start)) |lease| return lease;
    }
    return error.PoolExhausted;
}

fn tryAcquireSlot(self: *LocalDb, start: usize) ?ReadLease {
    var i: usize = 0;
    while (i < self.read_conns.len) : (i += 1) {
        const slot = (start + i) % self.read_conns.len;
        if (self.read_slot_mu[slot].tryLock()) {
            if (self.read_conns[slot]) |conn| {
                return .{ .db = self, .slot = slot, .conn = conn, .prefix_map = self.prefix_map, .prefix_cache = &self.read_caches[slot] };
            }
            self.read_slot_mu[slot].unlock(io);
        }
    }
    return null;
}

/// Execute a statement (INSERT, UPDATE, DELETE) — mutex-protected
pub fn exec(self: *LocalDb, comptime sql: []const u8, args: anytype) !void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);

    const c = self.conn orelse return error.NotOpen;
    c.exec(sql, args) catch |e| {
        log.err("exec failed: {s}", .{@errorName(e)});
        return e;
    };
}

/// Get raw write connection for batch operations (caller must hold lock)
pub fn getConn(self: *LocalDb) ?zqlite.Conn {
    return self.conn;
}

pub fn lock(self: *LocalDb) void {
    self.mutex.lockUncancelable(io);
}

pub fn unlock(self: *LocalDb) void {
    self.mutex.unlock(io);
}

/// Seconds since the last successful incremental sync watermark, or -1 if
/// unknown. Surfaced on /health as a staleness SLO: a growing value means
/// the overlay is drifting from Turso (moderation/profile get stale).
pub fn syncLagSeconds(self: *LocalDb, now_unix: i64) i64 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);

    const c = self.meta_conn orelse return -1;
    const row = c.row("SELECT value FROM sync_meta WHERE key = 'last_sync'", .{}) catch return -1;
    if (row) |r| {
        defer r.deinit();
        const last = std.fmt.parseInt(i64, r.text(0), 10) catch return -1;
        return now_unix - last;
    }
    return -1;
}

test "flat format admission rejects missing and unknown markers and permits legacy" {
    var c = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer c.close();
    try c.exec("ATTACH ':memory:' AS idx", .{});
    try c.exec("CREATE TABLE idx.index_meta(key TEXT PRIMARY KEY,value TEXT)", .{});
    try std.testing.expectError(error.InvalidFlatSnapshotSchema, validateSnapshotFormat(c));
    try c.exec("CREATE TABLE idx.prefix_top_base(key TEXT PRIMARY KEY,dids BLOB)", .{});
    try validateSnapshotFormat(c);
    try c.exec("INSERT INTO idx.index_meta VALUES('flat_prefix_version','99')", .{});
    try std.testing.expectError(error.UnsupportedFlatSnapshotVersion, validateSnapshotFormat(c));
    try c.exec("UPDATE idx.index_meta SET value='5'", .{});
    try std.testing.expectError(error.InvalidFlatSnapshotSchema, validateSnapshotFormat(c));
    try c.exec("DROP TABLE idx.prefix_top_base", .{});
    try c.exec("CREATE VIEW idx.prefix_top_base AS SELECT NULL AS key,NULL AS dids WHERE 0", .{});
    try std.testing.expectError(error.InvalidFlatSnapshotSchema, validateSnapshotFormat(c));
    try c.exec("CREATE VIEW idx.actor_compact_base AS SELECT NULL AS did,NULL AS blob WHERE 0", .{});
    try validateSnapshotFormat(c);
    try c.exec("DROP VIEW idx.actor_compact_base", .{});
    try c.exec("CREATE VIEW idx.actor_compact_base AS SELECT 1 AS wrong WHERE 0", .{});
    try std.testing.expectError(error.InvalidFlatSnapshotSchema, validateSnapshotFormat(c));
    try c.exec("DROP VIEW idx.actor_compact_base", .{});
    try c.exec("CREATE VIEW idx.actor_compact_base AS SELECT 'did:x' AS did,X'00' AS blob", .{});
    try std.testing.expectError(error.InvalidFlatSnapshotSchema, validateSnapshotFormat(c));
    try c.exec("DELETE FROM idx.index_meta", .{});
    try std.testing.expectError(error.InvalidFlatSnapshotSchema, validateSnapshotFormat(c));
}

extern "c" fn mkdtemp(template: [*:0]u8) ?[*:0]u8;

fn writeFixture(path: []const u8, bytes: []const u8) !void {
    const file = try std.Io.Dir.createFileAbsolute(io, path, .{ .truncate = true });
    defer file.close(io);
    var buffer: [4096]u8 = undefined;
    var writer = std.Io.File.Writer.init(file, io, &buffer);
    try writer.interface.writeAll(bytes);
    try writer.interface.flush();
}

test "unified generation admission and swap retain leased bytes and reject corrupt replacement" {
    var directory = "/tmp/unified-generation-XXXXXX".*;
    _ = mkdtemp(&directory) orelse return error.TestDirectoryFailed;
    defer std.Io.Dir.deleteTree(.cwd(), io, &directory) catch {};
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    const old_path = try std.fmt.allocPrint(arena, "{s}/old.db", .{directory});
    const new_path = try std.fmt.allocPrint(arena, "{s}/new.db", .{directory});
    const bad_path = try std.fmt.allocPrint(arena, "{s}/bad.db", .{directory});
    try writeFixture(old_path, @embedFile("../index/unified_fixture1.db"));
    try writeFixture(new_path, @embedFile("../index/unified_fixture2.db"));
    try writeFixture(bad_path, @embedFile("../index/unified_fixture2.db"));
    try writeFixture(try std.fmt.allocPrint(arena, "{s}.prefix", .{old_path}), @embedFile("../index/unified_fixture1.bin"));
    try writeFixture(try std.fmt.allocPrint(arena, "{s}.prefix", .{new_path}), @embedFile("../index/unified_fixture2.bin"));
    const corrupt = try arena.dupe(u8, @embedFile("../index/unified_fixture2.bin"));
    corrupt[corrupt.len - 1] ^= 1;
    try writeFixture(try std.fmt.allocPrint(arena, "{s}.prefix", .{bad_path}), corrupt);
    var local = LocalDb.init(std.testing.allocator);
    defer local.deinit();
    local.path = try std.fmt.allocPrint(arena, "{s}/overlay.db", .{directory});
    try local.openDb(local.path);
    try local.swapSnapshot(old_path);
    for (0..local.read_conns.len) |_| {
        const lease = try local.checkoutRead();
        _ = (try lease.baseActor(arena, "did:arbitrary:small")).?;
        lease.release();
    }
    const old = try local.checkoutRead();
    var released = false;
    defer if (!released) old.release();
    const before = (try old.baseActor(arena, "did:arbitrary:small")).?;
    const pack = @import("../index/pack.zig");
    try std.testing.expectEqualStrings("old.example", (try pack.unpackActor(before)).handle);
    var admitted = try local.prepareSnapshot(new_path);
    defer admitted.deinit();
    const future = (try admitted.map.?.findTagged(arena, .actor, "did:arbitrary:small")).?;
    try std.testing.expectEqualStrings("new.example", (try pack.unpackActor(future)).handle);
    try std.testing.expectError(error.FlatDigestMismatch, local.swapSnapshot(bad_path));
    try std.testing.expectEqualStrings("old.example", (try pack.unpackActor((try old.baseActor(arena, "did:arbitrary:small")).?)).handle);
    const Swap = struct {
        local: *LocalDb,
        path: []const u8,
        entered: std.atomic.Value(bool) = .init(false),
        done: std.atomic.Value(bool) = .init(false),
        failed: bool = false,
        fn run(self: *@This()) void {
            self.entered.store(true, .release);
            self.local.swapSnapshot(self.path) catch {
                self.failed = true;
            };
            self.done.store(true, .release);
        }
    };
    var swap: Swap = .{ .local = &local, .path = new_path };
    const thread = try std.Thread.spawn(.{}, Swap.run, .{&swap});
    var joined = false;
    defer if (!joined) {
        if (!released) {
            old.release();
            released = true;
        }
        thread.join();
    };
    while (!swap.entered.load(.acquire)) std.Thread.yield() catch {};
    for (0..1000) |_| std.Thread.yield() catch {};
    try std.testing.expect(!swap.done.load(.acquire));
    try std.testing.expect((try old.prefix_map.?.findTagged(arena, .prefix, "h:old")) != null);
    try std.testing.expect((try old.prefix_map.?.findTagged(arena, .prefix, "h:new")) == null);
    old.release();
    released = true;
    thread.join();
    joined = true;
    try std.testing.expect(!swap.failed);
    const current = try local.checkoutRead();
    defer current.release();
    try std.testing.expectEqualStrings("new.example", (try pack.unpackActor((try current.baseActor(arena, "did:arbitrary:small")).?)).handle);
    try std.testing.expect((try current.prefix_map.?.findTagged(arena, .prefix, "h:new")) != null);
    try std.testing.expect((try current.prefix_map.?.findTagged(arena, .prefix, "h:old")) == null);
    try std.testing.expectEqualStrings("old.example", (try pack.unpackActor(before)).handle);
}
