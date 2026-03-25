//! Local SQLite read replica using zqlite
//! Provides fast FTS5 queries while Turso remains source of truth

const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;

const log = std.log.scoped(.local_db);

const LocalDb = @This();

conn: ?zqlite.Conn = null,
read_conn: ?zqlite.Conn = null, // separate read connection — never blocked by writes in WAL mode
allocator: Allocator,
is_ready: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
mutex: std.Thread.Mutex = .{}, // protects write conn only
path: []const u8 = "",

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
    const path_env = std.posix.getenv("LOCAL_DB_PATH") orelse "/data/local.db";
    self.path = path_env;

    // ensure SQLite temp directory exists on the persistent volume
    const tmp_dir = std.posix.getenv("SQLITE_TMPDIR") orelse "/data/tmp";
    std.fs.cwd().makePath(tmp_dir) catch |err| {
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

    // separate read connection — WAL allows concurrent reads + writes
    self.read_conn = zqlite.open(path, zqlite.OpenFlags.ReadOnly) catch |err| {
        log.err("failed to open read conn: {}", .{err});
        return err;
    };
    _ = self.read_conn.?.exec("PRAGMA busy_timeout=1000", .{}) catch {};
    _ = self.read_conn.?.exec("PRAGMA mmap_size=268435456", .{}) catch {}; // 256MB
    _ = self.read_conn.?.exec("PRAGMA cache_size=-20000", .{}) catch {}; // 20MB

    self.cleanupStagingTables();
    try self.createSchema();
    self.repairFtsIfBroken();
    log.info("initialized", .{});
}

pub fn deinit(self: *LocalDb) void {
    if (self.read_conn) |c| c.close();
    self.read_conn = null;
    if (self.conn) |c| c.close();
    self.conn = null;
}

/// Close read connection (e.g. during bootstrap to avoid WAL reader interference)
pub fn closeReadConn(self: *LocalDb) void {
    if (self.read_conn) |c| c.close();
    self.read_conn = null;
}

/// Reopen read connection after bootstrap completes
pub fn reopenReadConn(self: *LocalDb) !void {
    if (self.read_conn != null) return;

    var path_buf: [256]u8 = undefined;
    if (self.path.len >= path_buf.len) return error.PathTooLong;
    @memcpy(path_buf[0..self.path.len], self.path);
    path_buf[self.path.len] = 0;
    const path: [*:0]const u8 = path_buf[0..self.path.len :0];

    self.read_conn = zqlite.open(path, zqlite.OpenFlags.ReadOnly) catch |err| {
        log.err("failed to reopen read conn: {}", .{err});
        return err;
    };
    _ = self.read_conn.?.exec("PRAGMA busy_timeout=1000", .{}) catch {};
    _ = self.read_conn.?.exec("PRAGMA mmap_size=268435456", .{}) catch {};
    _ = self.read_conn.?.exec("PRAGMA cache_size=-20000", .{}) catch {};
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
        \\  associated TEXT DEFAULT '{}'
        \\)
    , .{}) catch |err| {
        log.err("failed to create actors table: {}", .{err});
        return err;
    };

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
}

pub const Row = struct {
    stmt: zqlite.Row,

    pub fn text(self: Row, index: usize) []const u8 {
        return self.stmt.text(index);
    }

    pub fn int(self: Row, index: usize) i64 {
        return self.stmt.int(index);
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

/// SELECT using read connection (never blocked by writes)
pub fn query(self: *LocalDb, comptime sql: []const u8, args: anytype) !Rows {
    const c = self.read_conn orelse return error.NotOpen;
    const rows = c.rows(sql, args) catch |e| {
        log.err("query failed: {s}", .{@errorName(e)});
        return e;
    };
    return .{ .inner = rows };
}

/// Execute a statement (INSERT, UPDATE, DELETE) — mutex-protected
pub fn exec(self: *LocalDb, comptime sql: []const u8, args: anytype) !void {
    self.mutex.lock();
    defer self.mutex.unlock();

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
    self.mutex.lock();
}

pub fn unlock(self: *LocalDb) void {
    self.mutex.unlock();
}

/// Get cached actor count from sync_meta (written during sync, avoids full table scan)
pub fn countActors(self: *LocalDb) u64 {
    const c = self.read_conn orelse return 0;
    const row = c.row("SELECT value FROM sync_meta WHERE key = 'actor_count'", .{}) catch return 0;
    if (row) |r| {
        defer r.deinit();
        const val = r.text(0);
        return std.fmt.parseInt(u64, val, 10) catch 0;
    }
    return 0;
}
