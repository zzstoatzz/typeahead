//! Local SQLite state using zqlite.
//! The search role keeps a Turso actor replica, live overlay tables, and an
//! attached read-only prefix-index snapshot. Older FTS helpers remain for roles
//! that still need local FTS maintenance.

const std = @import("std");
const io = std.Options.debug_io;
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;
const index_schema = @import("../index/schema.zig");
const overlay = @import("../index/overlay.zig");

const log = std.log.scoped(.local_db);

const LocalDb = @This();

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
read_slot_mu: []std.Io.Mutex = &.{},
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

/// column list for full actor row queries (sync + search)
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

/// Clean up leftover staging tables from interrupted bootstrap.
/// Called during open so sync starts clean.
fn cleanupStagingTables(self: *LocalDb) void {
    const c = self.conn orelse return;
    c.exec("DROP TABLE IF EXISTS actors_fts_stage", .{}) catch {};
    c.exec("DROP TABLE IF EXISTS actors_stage", .{}) catch {};
}

/// Verify FTS5 table is functional. If broken (e.g. from a failed RENAME),
/// drop it and recreate empty — sync will repopulate it.
fn repairFtsIfBroken(self: *LocalDb) void {
    const c = self.conn orelse return;
    // probe: try a simple MATCH query — if shadow tables are broken this errors
    const row = c.row("SELECT did FROM actors_fts WHERE actors_fts MATCH '\"test\"*' LIMIT 1", .{}) catch {
        log.warn("FTS5 table broken, dropping and recreating", .{});
        c.exec("DROP TABLE IF EXISTS actors_fts", .{}) catch {};
        c.exec(
            \\CREATE VIRTUAL TABLE actors_fts USING fts5(
            \\  did UNINDEXED, handle, display_name,
            \\  tokenize='unicode61 remove_diacritics 2'
            \\)
        , .{}) catch {};
        // clear sync state so fullSync rebuilds FTS
        c.exec("DELETE FROM sync_meta WHERE key = 'sync_complete'", .{}) catch {};
        c.exec("DELETE FROM sync_meta WHERE key = 'last_sync'", .{}) catch {};
        log.info("FTS5 repaired, sync will rebuild on next run", .{});
        return;
    };
    if (row) |r| r.deinit();
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

    try self.openReadPool(path);

    self.cleanupStagingTables();
    try self.createSchema();
    self.repairFtsIfBroken();
    log.info("initialized", .{});
}

pub fn deinit(self: *LocalDb) void {
    self.closeReadConn();
    if (self.read_conns.len > 0) {
        self.allocator.free(self.read_conns);
        self.allocator.free(self.read_slot_mu);
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
    if (self.read_conns.len == 0) {
        const size = envUsize("READ_POOL_SIZE", READ_POOL_DEFAULT_SIZE, 1, 64);
        self.read_conns = try self.allocator.alloc(?zqlite.Conn, size);
        @memset(self.read_conns, null);
        self.read_slot_mu = try self.allocator.alloc(std.Io.Mutex, size);
        for (self.read_slot_mu) |*mu| mu.* = std.Io.Mutex.init;
        self.read_wait_budget_ms = envU64("READ_POOL_WAIT_MS", READ_WAIT_DEFAULT_MS, 0, 5000);
        log.info("read pool: size={d} wait_budget_ms={d}", .{ size, self.read_wait_budget_ms });
    }

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
    if (self.snapshot_path) |sp| try attachSnapshot(self.meta_conn.?, sp);
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

/// Atomically swap the ATTACHed prefix-index snapshot to `new_path`. Rebuilds
/// the read pool (close → set path → reopen) so every conn attaches the new
/// file in lockstep. The caller MUST have already verified + validated the
/// file (promote.zig) — this only does the attach. On reopen failure the swap
/// reverts to the prior snapshot so the pool never lands fully wedged.
///
/// Brief unavailability window during the rebuild (same as bootstrap's
/// close/reopen): a concurrent checkoutRead observes null slots and fails
/// fast to 503. Swaps are rare (per build, every 6-12h), so this is fine.
pub fn swapSnapshot(self: *LocalDb, new_path: []const u8) !void {
    const dup = try self.allocator.dupe(u8, new_path);
    const old = self.snapshot_path;

    self.closeReadConn();
    self.snapshot_path = dup;
    self.reopenReadConn() catch |err| {
        log.err("snapshot swap failed: {}; reverting to prior snapshot", .{err});
        self.allocator.free(dup);
        self.snapshot_path = old;
        self.reopenReadConn() catch |rerr| {
            log.err("revert reopen also failed: {}", .{rerr});
        };
        return err;
    };

    if (old) |o| self.allocator.free(o);
    log.info("snapshot attached: {s}", .{new_path});
}

/// build_id of the currently-attached snapshot, or null if none attached.
/// Caller owns the returned slice. Used by the /index/status debug endpoint.
pub fn attachedBuildId(self: *LocalDb, gpa: Allocator) !?[]const u8 {
    if (self.snapshot_path == null) return null;
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
    const c = self.meta_conn orelse return null;
    const row = c.row("SELECT value FROM idx.index_meta WHERE key = 'build_id'", .{}) catch return null;
    if (row) |r| {
        defer r.deinit();
        return try gpa.dupe(u8, r.text(0));
    }
    return null;
}

/// Prune sync-derived overlay rows the freshly-promoted base now covers
/// (updated_at ≤ new_watermark). Operator rows (source=1) survive. Writes via
/// the write conn under the write mutex; best-effort (logged, not fatal).
pub const CompactResult = struct { deleted: u64, chunks: u64, complete: bool };

/// Prune sync-derived overlay rows the attached base now covers
/// (updated_at ≤ watermark). Operator rows (source=1) survive. Runs in
/// COMPACT_CHUNK steps under the write mutex, yielding between them; skips
/// entirely while the replica is not ready (a resync owns the mutex).
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

/// Path of the currently-attached snapshot file, or null. Only the promote
/// watcher mutates snapshot_path (via swapSnapshot) and only it reads this,
/// so no lock is needed. Used to pick the inactive A/B slot to download into.
pub fn snapshotPath(self: *LocalDb) ?[]const u8 {
    return self.snapshot_path;
}

/// Integer-valued `index_meta` key from the attached snapshot, or 0 if no
/// snapshot is attached / the key is missing. Used to feed the previous
/// build's actor/prefix counts into the promote sanity floors.
pub fn attachedMetaInt(self: *LocalDb, key: []const u8) u64 {
    if (self.snapshot_path == null) return 0;
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);
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
        \\CREATE TABLE IF NOT EXISTS actors (
        \\  did TEXT PRIMARY KEY,
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
        log.err("failed to create actors table: {}", .{err});
        return err;
    };

    // in-place column adds for existing deployments. All allowed to fail silently
    // (column already present is the common case).
    c.exec("ALTER TABLE actors ADD COLUMN pds TEXT DEFAULT ''", .{}) catch {};
    c.exec("ALTER TABLE actors ADD COLUMN followers_count INTEGER NOT NULL DEFAULT 0", .{}) catch {};
    c.exec("ALTER TABLE actors ADD COLUMN follows_count INTEGER NOT NULL DEFAULT 0", .{}) catch {};
    c.exec("ALTER TABLE actors ADD COLUMN posts_count INTEGER NOT NULL DEFAULT 0", .{}) catch {};
    c.exec("ALTER TABLE actors ADD COLUMN last_activity_at INTEGER NOT NULL DEFAULT 0", .{}) catch {};
    c.exec("ALTER TABLE actors ADD COLUMN quality_score REAL NOT NULL DEFAULT 0", .{}) catch {};

    c.exec("CREATE INDEX IF NOT EXISTS idx_actors_handle ON actors(handle COLLATE NOCASE)", .{}) catch {};

    // standalone FTS5 (not content-synced — avoids rowid tracking complexity with INSERT OR REPLACE)
    c.exec(
        \\CREATE VIRTUAL TABLE IF NOT EXISTS actors_fts USING fts5(
        \\  did UNINDEXED, handle, display_name,
        \\  tokenize='unicode61 remove_diacritics 2'
        \\)
    , .{}) catch |err| {
        log.err("failed to create actors_fts: {}", .{err});
        return err;
    };

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
                return .{ .db = self, .slot = slot, .conn = conn };
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

/// Get cached actor count from sync_meta. Uses the dedicated meta conn
/// (NOT the read pool) so /health stays responsive even when /search is
/// saturating the pool.
pub fn countActors(self: *LocalDb) u64 {
    self.meta_mu.lockUncancelable(io);
    defer self.meta_mu.unlock(io);

    const c = self.meta_conn orelse return 0;
    const row = c.row("SELECT value FROM sync_meta WHERE key = 'actor_count'", .{}) catch return 0;
    if (row) |r| {
        defer r.deinit();
        const val = r.text(0);
        return std.fmt.parseInt(u64, val, 10) catch 0;
    }
    return 0;
}

/// Seconds since the last successful incremental sync watermark, or -1 if
/// unknown. Surfaced on /health as a staleness SLO: a growing value means
/// the local replica is drifting from Turso (moderation/profile get stale).
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
