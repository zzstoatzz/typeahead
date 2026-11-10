const std = @import("std");
const args_mod = @import("args.zig");
const keys = @import("keys.zig");
const out = @import("out.zig");
const store = @import("store.zig");
const sys = @import("sys.zig");

pub const Totals = struct {
    entries: u64 = 0,
    key_bytes: u64 = 0,
    value_bytes: u64 = 0,
    pages: u64 = 0,
    refreshes: u64 = 0,
    missing_errors: u64 = 0,
    retries: u64 = 0,
};

pub const Paging = struct {
    page: usize = std.math.maxInt(usize),
    refresh: bool = false,
    max_retries: usize = 16,
};

pub fn successor(gpa: std.mem.Allocator, prefix: []const u8) ![]u8 {
    if (prefix.len == 0 or prefix[prefix.len - 1] == 0xff) return error.UnboundedPrefix;
    const end = try gpa.dupe(u8, prefix);
    end[end.len - 1] += 1;
    return end;
}

pub fn prefixScan(gpa: std.mem.Allocator, db: *store.Db, prefix: []const u8, paging: Paging, visitor: anytype) !Totals {
    const end = try successor(gpa, prefix);
    defer gpa.free(end);
    var resume_key: std.ArrayList(u8) = .empty;
    defer resume_key.deinit(gpa);
    try resume_key.appendSlice(gpa, prefix);
    var totals: Totals = .{};
    var failures: usize = 0;
    while (true) {
        const got = pageOnce(gpa, db, &resume_key, end, paging, totals.pages > 0, &totals, visitor) catch |err| {
            if (err != error.Missing) return err;
            totals.missing_errors += 1;
            failures += 1;
            if (failures > paging.max_retries) return error.ReaderRetryExhausted;
            totals.retries += 1;
            continue;
        };
        failures = 0;
        totals.pages += 1;
        if (got < paging.page) return totals;
    }
}

fn pageOnce(gpa: std.mem.Allocator, db: *store.Db, resume_key: *std.ArrayList(u8), end: []const u8, paging: Paging, later_page: bool, totals: *Totals, visitor: anytype) !usize {
    if (paging.refresh and db.reader and (later_page or totals.missing_errors > 0)) {
        try db.refresh();
        totals.refreshes += 1;
    }
    var scan = try db.scan(.{ .start = resume_key.items, .end = end, .limit = paging.page });
    defer scan.deinit();
    var got: usize = 0;
    while (try scan.next()) |entry| {
        try visitor.entry(entry.key, entry.value);
        totals.entries += 1;
        totals.key_bytes += entry.key.len;
        totals.value_bytes += entry.value.len;
        resume_key.clearRetainingCapacity();
        try resume_key.appendSlice(gpa, entry.key);
        try resume_key.append(gpa, 0);
        got += 1;
    }
    return got;
}

pub const Ignore = struct {
    pub fn entry(_: Ignore, _: []const u8, _: []const u8) !void {}
};

pub const scan_flags = store.option_flags ++ [_]args_mod.Flag{
    .{ .name = "--prefix" },
    .{ .name = "--page" },
    .{ .name = "--max-retries" },
    .{ .name = "--reader", .value = false },
};

pub fn runScan(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const prefix = try args.required("--prefix");
    const paging: Paging = .{
        .page = try args.int(usize, "--page", 1000),
        .refresh = true,
        .max_retries = try args.int(usize, "--max-retries", 16),
    };
    const mode: store.Mode = if (args.has("--reader")) .reader else .writer;
    const handle = try store.Handle.open(gpa, io, args, mode, false);
    defer handle.close();

    const started = sys.now(io);
    const totals = try prefixScan(gpa, handle.db, prefix, paging, Ignore{});
    const elapsed = sys.now(io) - started;
    try out.emit(io, .{
        .event = "scan",
        .prefix = prefix,
        .handle = mode,
        .page = paging.page,
        .open_us = handle.open_us,
        .wall_ms = elapsed / std.time.ns_per_ms,
        .entries_per_s = sys.perSecond(totals.entries, elapsed),
        .totals = totals,
        .peak_rss_bytes = sys.peakRssBytes(io),
    });
}

pub const count_flags = store.option_flags;

pub fn runCount(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const handle = try store.Handle.open(gpa, io, args, .reader, false);
    defer handle.close();
    const Row = struct { prefix: []const u8, entries: u64, key_bytes: u64, value_bytes: u64, wall_ms: u64, entries_per_s: u64 };
    var rows: [keys.prefixes.len]Row = undefined;
    var sum: Totals = .{};
    const started = sys.now(io);
    for (&rows, 0..) |*row, index| {
        const prefix = keys.prefixes[index..][0..1];
        const prefix_started = sys.now(io);
        const totals = try prefixScan(gpa, handle.db, prefix, .{}, Ignore{});
        const elapsed = sys.now(io) - prefix_started;
        row.* = .{
            .prefix = prefix,
            .entries = totals.entries,
            .key_bytes = totals.key_bytes,
            .value_bytes = totals.value_bytes,
            .wall_ms = elapsed / std.time.ns_per_ms,
            .entries_per_s = sys.perSecond(totals.entries, elapsed),
        };
        sum.entries += totals.entries;
        sum.key_bytes += totals.key_bytes;
        sum.value_bytes += totals.value_bytes;
    }
    const elapsed = sys.now(io) - started;
    try out.emit(io, .{
        .event = "count",
        .open_us = handle.open_us,
        .entries = sum.entries,
        .key_bytes = sum.key_bytes,
        .value_bytes = sum.value_bytes,
        .wall_ms = elapsed / std.time.ns_per_ms,
        .entries_per_s = sys.perSecond(sum.entries, elapsed),
        .bytes_per_s = sys.perSecond(sum.key_bytes + sum.value_bytes, elapsed),
        .prefixes = rows,
        .peak_rss_bytes = sys.peakRssBytes(io),
    });
}

pub const stat_flags = store.option_flags ++ [_]args_mod.Flag{
    .{ .name = "--reader", .value = false },
    .{ .name = "--writer", .value = false },
};

pub fn runStat(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    if (args.has("--reader") and args.has("--writer")) return error.ConflictingHandle;
    const mode: store.Mode = if (args.has("--writer")) .writer else .reader;
    const handle = try store.Handle.open(gpa, io, args, mode, false);
    defer handle.close();
    try out.emit(io, .{
        .event = "stat",
        .handle = mode,
        .fence_stride = handle.db.options.fence_stride,
        .cache_bytes = handle.db.options.cache_bytes,
        .open_us = handle.open_us,
        .shape = store.shape(handle.db),
        .cache = handle.db.cacheStats(),
        .rss_bytes = sys.currentRssBytes(io),
        .peak_rss_bytes = sys.peakRssBytes(io),
        .disk = try sys.disk(io, gpa, handle.dir),
    });
}

test "successor bounds a prefix" {
    const end = try successor(std.testing.allocator, "ha");
    defer std.testing.allocator.free(end);
    try std.testing.expectEqualStrings("hb", end);
    try std.testing.expectError(error.UnboundedPrefix, successor(std.testing.allocator, "a\xff"));
}
