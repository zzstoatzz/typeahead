const std = @import("std");
const builtin = @import("builtin");
const Io = std.Io;

pub fn now(io: Io) u64 {
    return @intCast(Io.Clock.awake.now(io).nanoseconds);
}

pub fn unixSeconds(io: Io) u64 {
    return @intCast(@divFloor(Io.Clock.real.now(io).nanoseconds, std.time.ns_per_s));
}

pub fn msSince(io: Io, started: u64) u64 {
    return (now(io) - started) / std.time.ns_per_ms;
}

pub fn perSecond(count: u64, elapsed_ns: u64) u64 {
    if (elapsed_ns == 0) return 0;
    return @intCast(@as(u128, count) * std.time.ns_per_s / elapsed_ns);
}

fn procStatusKb(io: Io, field: []const u8) ?u64 {
    const file = Io.Dir.cwd().openFile(io, "/proc/self/status", .{}) catch return null;
    defer file.close(io);
    var buf: [8192]u8 = undefined;
    const len = file.readPositionalAll(io, &buf, 0) catch return null;
    var lines = std.mem.tokenizeScalar(u8, buf[0..len], '\n');
    while (lines.next()) |line| {
        if (!std.mem.startsWith(u8, line, field)) continue;
        var parts = std.mem.tokenizeAny(u8, line[field.len..], " \t");
        return std.fmt.parseInt(u64, parts.next() orelse return null, 10) catch null;
    }
    return null;
}

pub fn peakRssBytes(io: Io) u64 {
    if (builtin.os.tag == .linux) {
        if (procStatusKb(io, "VmHWM:")) |kb| return kb * 1024;
    }
    const raw: u64 = @intCast(std.posix.getrusage(std.posix.rusage.SELF).maxrss);
    return if (builtin.os.tag.isDarwin()) raw else raw * 1024;
}

pub fn currentRssBytes(io: Io) ?u64 {
    if (builtin.os.tag != .linux) return null;
    return (procStatusKb(io, "VmRSS:") orelse return null) * 1024;
}

pub const Disk = struct {
    bytes: u64 = 0,
    files: u64 = 0,
    sst_files: u64 = 0,
    log_files: u64 = 0,
};

pub fn disk(io: Io, gpa: std.mem.Allocator, dir: Io.Dir) !Disk {
    var walker = try dir.walk(gpa);
    defer walker.deinit();
    var total: Disk = .{};
    while (try walker.next(io)) |entry| {
        if (entry.kind != .file) continue;
        const stat = entry.dir.statFile(io, entry.basename, .{}) catch |err| switch (err) {
            error.FileNotFound => continue,
            else => return err,
        };
        total.bytes += stat.size;
        total.files += 1;
        if (std.mem.startsWith(u8, entry.path, "sst")) total.sst_files += 1;
        if (std.mem.startsWith(u8, entry.path, "log")) total.log_files += 1;
    }
    return total;
}

pub const DiskWatch = struct {
    io: Io,
    gpa: std.mem.Allocator,
    dir: Io.Dir,
    interval_ns: u64,
    start: Disk,
    peak_bytes: u64,
    last: u64,
    samples: u64 = 1,

    pub fn init(io: Io, gpa: std.mem.Allocator, dir: Io.Dir, interval_ms: u64) !DiskWatch {
        const first = try disk(io, gpa, dir);
        return .{ .io = io, .gpa = gpa, .dir = dir, .interval_ns = interval_ms * std.time.ns_per_ms, .start = first, .peak_bytes = first.bytes, .last = now(io) };
    }

    pub fn poll(self: *DiskWatch) !void {
        if (now(self.io) - self.last >= self.interval_ns) _ = try self.sample();
    }

    pub fn sample(self: *DiskWatch) !Disk {
        const current = try disk(self.io, self.gpa, self.dir);
        self.peak_bytes = @max(self.peak_bytes, current.bytes);
        self.samples += 1;
        self.last = now(self.io);
        return current;
    }
};

pub const Summary = struct {
    count: u64 = 0,
    p50_us: u64 = 0,
    p95_us: u64 = 0,
    p99_us: u64 = 0,
    max_us: u64 = 0,
    total_ms: u64 = 0,
};

fn rank(sorted: []const u64, percent: u64) u64 {
    const index = (sorted.len * percent + 99) / 100;
    return sorted[@max(index, 1) - 1];
}

pub fn summarize(nanoseconds: []u64) Summary {
    if (nanoseconds.len == 0) return .{};
    std.mem.sort(u64, nanoseconds, {}, std.sort.asc(u64));
    var total: u64 = 0;
    for (nanoseconds) |ns| total += ns;
    return .{
        .count = nanoseconds.len,
        .p50_us = rank(nanoseconds, 50) / std.time.ns_per_us,
        .p95_us = rank(nanoseconds, 95) / std.time.ns_per_us,
        .p99_us = rank(nanoseconds, 99) / std.time.ns_per_us,
        .max_us = nanoseconds[nanoseconds.len - 1] / std.time.ns_per_us,
        .total_ms = total / std.time.ns_per_ms,
    };
}

test "summarize uses nearest-rank percentiles" {
    var samples: [100]u64 = undefined;
    for (&samples, 0..) |*sample, i| sample.* = (100 - i) * std.time.ns_per_us;
    const summary = summarize(&samples);
    try std.testing.expectEqual(@as(u64, 100), summary.count);
    try std.testing.expectEqual(@as(u64, 50), summary.p50_us);
    try std.testing.expectEqual(@as(u64, 95), summary.p95_us);
    try std.testing.expectEqual(@as(u64, 99), summary.p99_us);
    try std.testing.expectEqual(@as(u64, 100), summary.max_us);
    var one = [_]u64{7 * std.time.ns_per_us};
    try std.testing.expectEqual(@as(u64, 7), summarize(&one).p99_us);
}
