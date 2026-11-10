//! Paged range scans. A page pins one snapshot; between pages a reader
//! refreshes, so a long scan follows the writer instead of holding back its
//! table collection. A reader whose tables were collected under it gets
//! `error.Missing` from zkv: the scan refreshes and resumes after the last
//! key it delivered, so no key is delivered twice or skipped.
//!
//! A scan that refreshes is not one snapshot. Keys that do not move (actors
//! by DID, changes by sequence) are each seen once; a key that moves while
//! the scan runs (a handle, a score, a due time) can be seen at both
//! positions or at neither.

const std = @import("std");
const engine = @import("engine.zig");

pub const Options = struct {
    page: usize = 1000,
    /// Refresh a reader before every page after the first. Without it the
    /// reader keeps its snapshot until zkv reports a collected table.
    refresh: bool = true,
    /// Consecutive `error.Missing` results tolerated before giving up.
    max_retries: usize = 16,
    /// Stop after this many entries; the position can be resumed.
    limit: usize = std.math.maxInt(usize),
};

pub const Stats = struct {
    entries: u64 = 0,
    pages: u64 = 0,
    refreshes: u64 = 0,
    missing: u64 = 0,
};

pub const Error = error{ReaderRetryExhausted};

/// Where a scan stands: the next key it would deliver is the first one at or
/// after `next`. Keep it to resume a scan that stopped at its limit.
pub const Position = struct {
    next: std.ArrayList(u8) = .empty,
    done: bool = false,

    pub fn deinit(self: *Position, gpa: std.mem.Allocator) void {
        self.next.deinit(gpa);
        self.* = .{};
    }

    pub fn reset(self: *Position, gpa: std.mem.Allocator, start: []const u8) !void {
        self.next.clearRetainingCapacity();
        try self.next.appendSlice(gpa, start);
        self.done = false;
    }

    fn after(self: *Position, gpa: std.mem.Allocator, key: []const u8) !void {
        self.next.clearRetainingCapacity();
        try self.next.ensureTotalCapacity(gpa, key.len + 1);
        self.next.appendSliceAssumeCapacity(key);
        self.next.appendAssumeCapacity(0);
    }
};

/// Deliver the keys in `[position, end)` to `visitor.entry(key, value)`, both
/// borrowed for the call. Returns when the range or `options.limit` is
/// exhausted; `position.done` tells which.
pub fn run(gpa: std.mem.Allocator, db: *engine.Db, position: *Position, end: []const u8, options: Options, visitor: anytype) !Stats {
    std.debug.assert(options.page > 0);
    var stats: Stats = .{};
    var failures: usize = 0;
    var left = options.limit;
    while (!position.done and left > 0) {
        if (db.reader and ((options.refresh and stats.pages > 0) or failures > 0)) {
            try db.refresh();
            stats.refreshes += 1;
        }
        const want = @min(options.page, left);
        const got = page(gpa, db, position, end, want, &stats, visitor) catch |err| {
            if (err != error.Missing or !db.reader) return err;
            stats.missing += 1;
            failures += 1;
            if (failures > options.max_retries) return error.ReaderRetryExhausted;
            continue;
        };
        failures = 0;
        stats.pages += 1;
        left -= got;
        if (got < want) position.done = true;
    }
    return stats;
}

fn page(gpa: std.mem.Allocator, db: *engine.Db, position: *Position, end: []const u8, want: usize, stats: *Stats, visitor: anytype) !usize {
    var cursor = try db.scan(.{ .start = position.next.items, .end = end, .limit = want });
    defer cursor.deinit();
    var got: usize = 0;
    while (try cursor.next()) |entry| {
        try visitor.entry(entry.key, entry.value);
        stats.entries += 1;
        got += 1;
        try position.after(gpa, entry.key);
    }
    return got;
}

/// Scan a whole range from its start.
pub fn all(gpa: std.mem.Allocator, db: *engine.Db, start: []const u8, end: []const u8, options: Options, visitor: anytype) !Stats {
    var position: Position = .{};
    defer position.deinit(gpa);
    try position.reset(gpa, start);
    return run(gpa, db, &position, end, options, visitor);
}

pub const Count = struct {
    entries: u64 = 0,

    pub fn entry(self: *Count, _: []const u8, _: []const u8) !void {
        self.entries += 1;
    }
};
