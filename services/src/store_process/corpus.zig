//! The counts the statistics page takes from the corpus, by a scan of the
//! actor range through a read-only handle: live actors, those with a handle,
//! those with an avatar, the hidden ones, and the sizes of the two refresh
//! walks' pools.
//!
//! The handle is opened for a scan and closed after it, so its table
//! metadata (60 MB at 13.2 million actors in the 2026-11-10 measurement) is
//! held only while a scan runs. The scan pages 1,000 keys at a time and
//! refreshes between pages, so it never holds the writer's table collection
//! back, and reads a fixed prefix of each record without allocating. The
//! same measurement scanned every actor in about 11 seconds (1.2 million
//! actors a second) on four cores beside a writer; this scan has not been
//! timed at that size.
//!
//! A scan that follows the writer is not one snapshot: an actor that changes
//! while it runs is counted as it was when the scan passed it.
//!
//! The first scan waits `delay_seconds` after the process starts. A scan
//! reads the whole actor range and takes as much CPU and disk as it is
//! given, and on the shadow machine one that began with the process ran for
//! 175 s beside the store's open and the relay catch-up (2026-11-11).

const std = @import("std");
const store_mod = @import("../store/root.zig");
const Io = std.Io;

const log = std.log.scoped(.corpus);

pub const Counts = struct {
    /// Actor records of any status.
    records: u64 = 0,
    live: u64 = 0,
    with_handles: u64 = 0,
    with_avatars: u64 = 0,
    hidden: u64 = 0,
    /// Live, visible, with a handle and a positive score: the quality walk.
    ranked: u64 = 0,
    /// Live with a handle and not ranked: the rest walk.
    rest: u64 = 0,

    /// `snapshots.total` as `recordSnapshot` in `src/enrichment.ts` writes
    /// it: live actors less the hidden ones.
    pub fn total(self: Counts) u64 {
        return self.live - self.hidden;
    }

    pub fn entry(self: *Counts, _: []const u8, value: []const u8) !void {
        const brief = try store_mod.record.Actor.brief(value);
        self.records += 1;
        if (brief.status != .live) return;
        self.live += 1;
        self.with_handles += @intFromBool(brief.has_handle);
        self.with_avatars += @intFromBool(brief.has_avatar);
        self.hidden += @intFromBool(brief.hidden);
        if (brief.ranked()) {
            self.ranked += 1;
        } else if (brief.has_handle) self.rest += 1;
    }
};

pub const Report = struct {
    /// Null until a scan has completed.
    counts: ?Counts = null,
    /// Unix seconds.
    started_at: u64 = 0,
    finished_at: u64 = 0,
    duration_ms: u64 = 0,
    scans: u64 = 0,
    failed: u64 = 0,
};

pub const Scanner = struct {
    gpa: std.mem.Allocator,
    io: Io,
    root: Io.Dir,
    path: []const u8,
    layout: store_mod.engine.Layout,
    interval_seconds: u64,
    delay_seconds: u64 = 0,
    stopping: std.atomic.Value(bool) = .init(false),
    lock: Io.Mutex = .init,
    latest: Report = .{},

    pub fn report(self: *Scanner) Report {
        self.lock.lockUncancelable(self.io);
        defer self.lock.unlock(self.io);
        return self.latest;
    }

    fn seconds(self: *Scanner) u64 {
        return @intCast(@divFloor(Io.Timestamp.now(self.io, .real).nanoseconds, std.time.ns_per_s));
    }

    pub fn count(self: *Scanner) !Counts {
        const reader = try store_mod.Reader.open(self.gpa, self.io, self.root, self.path, self.layout);
        defer reader.close();
        var position: store_mod.scan.Position = .{};
        defer position.deinit(self.gpa);
        var counts: Counts = .{};
        _ = try reader.scanRange(&position, .actor, .{}, &counts);
        return counts;
    }

    pub fn scan(self: *Scanner) void {
        const started_at = self.seconds();
        const started = Io.Timestamp.now(self.io, .awake).nanoseconds;
        const result = self.count();
        const duration_ms: u64 = @intCast(@divFloor(Io.Timestamp.now(self.io, .awake).nanoseconds - started, std.time.ns_per_ms));
        self.lock.lockUncancelable(self.io);
        defer self.lock.unlock(self.io);
        if (result) |counts| {
            self.latest.counts = counts;
            self.latest.started_at = started_at;
            self.latest.finished_at = self.seconds();
            self.latest.duration_ms = duration_ms;
            self.latest.scans += 1;
            log.info("corpus scan: {d} live of {d} records in {d} ms", .{ counts.live, counts.records, duration_ms });
        } else |err| {
            self.latest.failed += 1;
            log.warn("corpus scan failed after {d} ms: {s}", .{ duration_ms, @errorName(err) });
        }
    }

    /// Ends `run` at its next wait, and before its next scan. A scan that
    /// cancellation interrupts only fails, so `run` cannot rely on it.
    pub fn stop(self: *Scanner) void {
        self.stopping.store(true, .release);
    }

    pub fn run(self: *Scanner) void {
        self.io.sleep(Io.Duration.fromSeconds(@intCast(self.delay_seconds)), .awake) catch return;
        while (!self.stopping.load(.acquire)) {
            self.scan();
            self.io.sleep(Io.Duration.fromSeconds(@intCast(self.interval_seconds)), .awake) catch return;
        }
    }
};

const testing = std.testing;
const harness = @import("testing.zig");

test "a scan counts what the statistics page shows and what each walk covers" {
    const f = try harness.Fixture.create(.{});
    defer f.destroy();
    _ = try f.commit(&.{
        .{ .profile = .{ .did = "did:plc:ranked", .handle = "ranked.test", .avatar = "bafkreiavatar" } },
        .{ .enrichment = .{ .did = "did:plc:ranked", .source = .bsky, .quality_score = 300 } },
        .{ .profile = .{ .did = "did:plc:unscored", .handle = "unscored.test" } },
        .{ .profile = .{ .did = "did:plc:hidden", .handle = "hidden.test", .avatar = "bafkreiavatar" } },
        .{ .enrichment = .{ .did = "did:plc:hidden", .source = .bsky, .quality_score = 200 } },
        .{ .override_set = .{ .did = "did:plc:hidden", .action = .hide } },
        .{ .profile = .{ .did = "did:plc:nohandle", .avatar = "bafkreiavatar" } },
        .{ .observe = .{ .did = "did:plc:bare" } },
        .{ .profile = .{ .did = "did:plc:gone", .handle = "gone.test", .avatar = "bafkreiavatar" } },
        .{ .gone = .{ .did = "did:plc:gone", .status = .deleted } },
    }, null);
    var scanner: Scanner = .{ .gpa = testing.allocator, .io = testing.io, .root = f.tmp.dir, .path = "store", .layout = f.owner.options.store.layout, .interval_seconds = 3600 };
    try testing.expect(scanner.report().counts == null);
    scanner.scan();
    const report = scanner.report();
    try testing.expectEqual(Counts{ .records = 6, .live = 5, .with_handles = 3, .with_avatars = 3, .hidden = 1, .ranked = 1, .rest = 2 }, report.counts.?);
    try testing.expectEqual(@as(u64, 4), report.counts.?.total());
    try testing.expectEqual(@as(u64, 1), report.scans);
    try testing.expectEqual(@as(u64, 0), report.failed);
}

test "a stopped scanner returns without scanning" {
    const f = try harness.Fixture.create(.{});
    defer f.destroy();
    _ = try f.commit(&.{.{ .observe = .{ .did = "did:plc:bare" } }}, null);
    var scanner: Scanner = .{ .gpa = testing.allocator, .io = testing.io, .root = f.tmp.dir, .path = "store", .layout = f.owner.options.store.layout, .interval_seconds = 3600 };
    scanner.stop();
    scanner.run();
    try testing.expectEqual(@as(u64, 0), scanner.report().scans);
}
