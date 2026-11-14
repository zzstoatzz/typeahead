//! Reads the PLC directory's operation log for the store's identity queue.
//!
//! The relay's `#identity` events are the fast path, but a PDS that never
//! emits one, or an outage longer than the relay's replay window, would leave
//! a did:plc identity stale with nothing to correct it. Every PLC operation is
//! a signal to re-resolve that DID from its authority, which also sidesteps
//! nullification: we never apply an operation, only re-read the document the
//! directory now serves. did:web has no log; it relies on `#identity`.
//!
//! `store_process/tails.zig` keeps the cursor in the store and commits it
//! with the page it covers, so a failed page replays.
//! `/export?after=` is exclusive and keyed by `createdAt`, so a full page backs
//! its cursor up to the last earlier timestamp: operations sharing the final
//! millisecond are read again rather than skipped. Repeats coalesce in the queue.

const std = @import("std");
const Allocator = std.mem.Allocator;
const mem = std.mem;
const json = std.json;
const zat = @import("zat");
const HttpTransport = zat.HttpTransport;

const log = std.log.scoped(.plc_tail);

pub const PAGE_SIZE = 1000;
const EXPORT_URL = "https://plc.directory/export";
pub const USER_AGENT = "typeahead.waow.tech/store (+https://tangled.org/zzstoatzz.io/typeahead)";
/// plc.directory allows 500 requests per 5 minutes; catch-up stays well under.
pub const CATCHUP_POLL_SECS = 2;
pub const IDLE_POLL_SECS = 30;
pub const FAILURE_BACKOFF_SECS = 30;

pub const Stats = struct {
    pages: std.atomic.Value(u64) = .init(0),
    operations: std.atomic.Value(u64) = .init(0),
    failed: std.atomic.Value(u64) = .init(0),
};
pub var stats: Stats = .{};

const Operation = struct { did: []const u8, createdAt: []const u8 };

/// The cursor after a page: its final timestamp, except that a full page
/// backs up to the last timestamp before the final one, so operations that
/// share the final millisecond are read again rather than skipped.
pub fn nextCursor(timestamps: []const []const u8, current: []const u8) []const u8 {
    if (timestamps.len == 0) return current;
    const last = timestamps[timestamps.len - 1];
    if (timestamps.len < PAGE_SIZE) return last;
    var i = timestamps.len - 1;
    while (i > 0) {
        i -= 1;
        if (!mem.eql(u8, timestamps[i], last)) return timestamps[i];
    }
    return last;
}

pub const Page = struct {
    /// did:plc subjects of the page's operations, repeats included
    dids: []const []const u8,
    operations: usize,
    /// the cursor after this page; see `nextCursor`
    next: []const u8,
};

/// Read one export page after `cursor`. Everything in the result is owned by
/// `arena`, except `next`, which may be `cursor` itself.
pub fn fetchPage(arena: Allocator, plc: *HttpTransport, cursor: []const u8) !Page {
    const url = try std.fmt.allocPrint(arena, "{s}?count={d}&after={s}", .{ EXPORT_URL, PAGE_SIZE, cursor });
    const response = try plc.fetch(.{ .url = url });
    defer plc.allocator.free(response.body);
    if (response.status != .ok) {
        log.warn("plc export HTTP {d}", .{@backingInt(response.status)});
        return error.HttpError;
    }
    return parsePage(arena, response.body, cursor);
}

pub fn parsePage(arena: Allocator, body: []const u8, cursor: []const u8) !Page {
    var dids: std.ArrayList([]const u8) = .empty;
    var timestamps: std.ArrayList([]const u8) = .empty;
    var lines = mem.splitScalar(u8, body, '\n');
    while (lines.next()) |line| {
        if (line.len == 0) continue;
        const op = json.parseFromSliceLeaky(Operation, arena, line, .{ .ignore_unknown_fields = true, .allocate = .alloc_always }) catch {
            log.warn("unparseable plc export line ({d} bytes)", .{line.len});
            return error.InvalidExport;
        };
        try timestamps.append(arena, op.createdAt);
        if (mem.startsWith(u8, op.did, "did:plc:")) try dids.append(arena, op.did);
    }
    return .{ .dids = dids.items, .operations = timestamps.items.len, .next = nextCursor(timestamps.items, cursor) };
}

/// Move `cursor` past a page that has been reported, and count it.
pub fn advance(allocator: Allocator, cursor: *std.ArrayList(u8), page: Page) !void {
    const owned = try allocator.dupe(u8, page.next);
    defer allocator.free(owned);
    cursor.clearRetainingCapacity();
    try cursor.appendSlice(allocator, owned);
    _ = stats.pages.fetchAdd(1, .monotonic);
    _ = stats.operations.fetchAdd(page.operations, .monotonic);
}

const testing = std.testing;

test "nextCursor: a partial page advances to its final timestamp" {
    const page = [_][]const u8{ "2026-10-31T00:00:00.001Z", "2026-10-31T00:00:00.002Z" };
    try testing.expectEqualStrings("2026-10-31T00:00:00.002Z", nextCursor(&page, "2026-10-30T00:00:00.000Z"));
}

test "nextCursor: an empty page keeps the cursor" {
    try testing.expectEqualStrings("2026-10-30T00:00:00.000Z", nextCursor(&.{}, "2026-10-30T00:00:00.000Z"));
}

test "nextCursor: a full page re-reads operations sharing its final millisecond" {
    var page: [PAGE_SIZE][]const u8 = undefined;
    for (&page, 0..) |*t, i| t.* = if (i < PAGE_SIZE - 3) "2026-10-31T00:00:00.001Z" else "2026-10-31T00:00:00.002Z";
    try testing.expectEqualStrings("2026-10-31T00:00:00.001Z", nextCursor(&page, "2026-10-30T00:00:00.000Z"));
}

test "nextCursor: a full page of one millisecond still advances" {
    var page: [PAGE_SIZE][]const u8 = undefined;
    for (&page) |*t| t.* = "2026-10-31T00:00:00.001Z";
    try testing.expectEqualStrings("2026-10-31T00:00:00.001Z", nextCursor(&page, "2026-10-30T00:00:00.000Z"));
}
