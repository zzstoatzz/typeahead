//! The PLC export tail and the label stream, committing to the store. Both
//! take their reading, paging and replay rules from `plc_tail.zig` and
//! `label_tail.zig`; a page or a batch lands here with its cursor in one
//! commit.

const std = @import("std");
const zat = @import("zat");
const plc_tail = @import("../plc_tail.zig");
const label_tail = @import("../label_tail.zig");
const events = @import("events.zig");
const owner_mod = @import("owner.zig");
const Io = std.Io;
const Owner = owner_mod.Owner;
const Submission = owner_mod.Submission;

const log = std.log.scoped(.store_tails);

/// With no stored cursor the tail starts this far back, as the Worker did:
/// the historical backlog belongs to the bootstrap, not to this tail.
pub const plc_bootstrap_seconds = 5 * 60;

/// `2026-10-31T00:00:00.000Z` for a unix time in seconds.
pub fn isoSeconds(buf: *[24]u8, unix: u64) []const u8 {
    const day: std.time.epoch.EpochDay = .{ .day = @intCast(unix / 86_400) };
    const year_day = day.calculateYearDay();
    const month_day = year_day.calculateMonthDay();
    const seconds = unix % 86_400;
    return std.fmt.bufPrint(buf, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}.000Z", .{
        year_day.year, month_day.month.numeric(), month_day.day_index + 1, seconds / 3600, seconds % 3600 / 60, seconds % 60,
    }) catch unreachable;
}

pub const PlcTail = struct {
    gpa: std.mem.Allocator,
    io: Io,
    owner: *Owner,

    /// The stored export cursor, or the bootstrap position.
    pub fn startCursor(self: *PlcTail, out: *std.ArrayList(u8)) !void {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const stored = blk: {
            const store = try self.owner.acquire();
            defer self.owner.release();
            break :blk (try store.cursors(arena.allocator())).plc;
        };
        var buf: [24]u8 = undefined;
        out.clearRetainingCapacity();
        try out.appendSlice(self.gpa, stored orelse isoSeconds(&buf, self.owner.now() -| plc_bootstrap_seconds));
    }

    /// Commit one page: identity work for the accounts we hold, and the
    /// cursor past the page.
    pub fn apply(self: *PlcTail, page: plc_tail.Page) !void {
        if (page.operations == 0) return;
        const submission = try Submission.create(self.gpa);
        defer submission.destroy();
        var changes: events.Changes = .{ .arena = submission.arena.allocator() };
        try changes.plc(page.dids);
        submission.changes = changes.items();
        submission.cursor = .{ .plc = page.next };
        try self.owner.call(submission);
    }

    fn step(self: *PlcTail, plc: *zat.HttpTransport, cursor: *std.ArrayList(u8)) !usize {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const page = try plc_tail.fetchPage(arena.allocator(), plc, cursor.items);
        try self.apply(page);
        try plc_tail.advance(self.gpa, cursor, page);
        return page.operations;
    }

    pub fn run(self: *PlcTail) void {
        var plc = zat.HttpTransport.init(self.io, self.gpa);
        plc.keep_alive = true;
        plc.user_agent = plc_tail.USER_AGENT;
        defer plc.deinit();

        var cursor: std.ArrayList(u8) = .empty;
        defer cursor.deinit(self.gpa);
        self.startCursor(&cursor) catch |err| {
            log.warn("plc cursor unavailable: {s}", .{@errorName(err)});
            return;
        };
        log.info("tailing plc export after {s}", .{cursor.items});

        while (true) {
            const wait: i64 = if (self.step(&plc, &cursor)) |n|
                if (n >= plc_tail.PAGE_SIZE) plc_tail.CATCHUP_POLL_SECS else plc_tail.IDLE_POLL_SECS
            else |err| blk: {
                _ = plc_tail.stats.failed.fetchAdd(1, .monotonic);
                log.warn("plc tail step failed: {s}", .{@errorName(err)});
                break :blk plc_tail.FAILURE_BACKOFF_SECS;
            };
            self.io.sleep(Io.Duration.fromSeconds(wait), .awake) catch return;
        }
    }
};

/// Where the label stream's batches land in the store role.
pub const LabelSink = struct {
    gpa: std.mem.Allocator,
    owner: *Owner,
    /// Labels the Worker's schema would have refused.
    ignored: std.atomic.Value(u64) = .init(0),

    /// The stored stream seq; null before the first commit, and the stream
    /// then starts at its head.
    pub fn cursor(self: *LabelSink) !?i64 {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const store = try self.owner.acquire();
        defer self.owner.release();
        const stored = (try store.cursors(arena.allocator())).labeler orelse return null;
        return @intCast(stored);
    }

    pub fn commit(self: *LabelSink, labels: []const label_tail.Wire, reached: i64) !void {
        const submission = try Submission.create(self.gpa);
        defer submission.destroy();
        var changes: events.Changes = .{ .arena = submission.arena.allocator() };
        _ = self.ignored.fetchAdd(try changes.labels(labels), .monotonic);
        submission.changes = changes.items();
        submission.cursor = .{ .labeler = @intCast(@max(reached, 0)) };
        try self.owner.call(submission);
    }

    pub fn run(self: *LabelSink, io: Io) void {
        label_tail.follow(self.gpa, io, self);
    }
};
