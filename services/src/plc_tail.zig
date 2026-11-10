//! Tails the PLC directory's operation log into the durable identity queue.
//!
//! The relay's `#identity` events are the fast path, but a PDS that never
//! emits one, or an outage longer than the relay's replay window, would leave
//! a did:plc identity stale with nothing to correct it. Every PLC operation is
//! a signal to re-resolve that DID from its authority, which also sidesteps
//! nullification: we never apply an operation, only re-read the document the
//! directory now serves. did:web has no log; it relies on `#identity`.
//!
//! The worker keeps the cursor and enqueues only DIDs it already indexes,
//! recording the cursor after the enqueue commits, so a failed page replays.
//! `/export?after=` is exclusive and keyed by `createdAt`, so a full page backs
//! its cursor up to the last earlier timestamp: operations sharing the final
//! millisecond are read again rather than skipped. Repeats coalesce in the queue.

const std = @import("std");
const Io = std.Io;
const Allocator = std.mem.Allocator;
const mem = std.mem;
const json = std.json;
const zat = @import("zat");
const HttpTransport = zat.HttpTransport;
const ingest = @import("ingest.zig");

const log = std.log.scoped(.plc_tail);

pub const PAGE_SIZE = 1000;
const EXPORT_URL = "https://plc.directory/export";
pub const USER_AGENT = "typeahead.waow.tech/ingester (+https://tangled.org/zzstoatzz.io/typeahead)";
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

pub const Tail = struct {
    allocator: Allocator,
    io: Io,
    worker_url: []const u8,
    worker_secret: []const u8,

    fn auth(self: *Tail, buf: []u8) ![]const u8 {
        return std.fmt.bufPrint(buf, "Bearer {s}", .{self.worker_secret});
    }

    /// The worker's stored cursor, or its bootstrap (a few minutes ago: the
    /// historical backlog belongs to offline snapshot builds, not this tail).
    fn fetchCursor(self: *Tail, transport: *HttpTransport, out: *std.ArrayList(u8)) !void {
        var url_buf: [512]u8 = undefined;
        const url = try std.fmt.bufPrint(&url_buf, "{s}/admin/identity/plc", .{self.worker_url});
        var auth_buf: [256]u8 = undefined;
        const result = try transport.fetch(.{ .url = url, .authorization = try self.auth(&auth_buf) });
        defer self.allocator.free(result.body);
        if (result.status != .ok) return error.HttpError;
        const parsed = try json.parseFromSlice(struct { cursor: []const u8 }, self.allocator, result.body, .{ .ignore_unknown_fields = true });
        defer parsed.deinit();
        out.clearRetainingCapacity();
        try out.appendSlice(self.allocator, parsed.value.cursor);
    }

    /// Read one export page after `cursor` and report it; returns the page size.
    fn step(self: *Tail, worker: *HttpTransport, plc: *HttpTransport, cursor: *std.ArrayList(u8)) !usize {
        var arena_state = std.heap.ArenaAllocator.init(self.allocator);
        defer arena_state.deinit();
        const arena = arena_state.allocator();

        const page = try fetchPage(arena, plc, cursor.items);
        if (page.operations > 0) {
            var output: std.Io.Writer.Allocating = .init(arena);
            var jw: json.Stringify = .{ .writer = &output.writer };
            try jw.write(.{ .dids = page.dids, .cursor = page.next });

            var url_buf: [512]u8 = undefined;
            const report = try std.fmt.bufPrint(&url_buf, "{s}/admin/identity/plc", .{self.worker_url});
            var auth_buf: [256]u8 = undefined;
            const result = try worker.fetch(ingest.workerPost(report, output.written(), try self.auth(&auth_buf)));
            defer self.allocator.free(result.body);
            if (result.status != .ok) {
                log.warn("plc report HTTP {d}: {s}", .{ @backingInt(result.status), result.body });
                return error.HttpError;
            }
        }

        try advance(self.allocator, cursor, page);
        return page.operations;
    }

    pub fn run(self: *Tail) void {
        var worker = HttpTransport.init(self.io, self.allocator);
        worker.keep_alive = true;
        defer worker.deinit();
        var plc = HttpTransport.init(self.io, self.allocator);
        plc.keep_alive = true;
        plc.user_agent = USER_AGENT;
        defer plc.deinit();

        var cursor: std.ArrayList(u8) = .empty;
        defer cursor.deinit(self.allocator);
        while (true) {
            self.fetchCursor(&worker, &cursor) catch |err| {
                _ = stats.failed.fetchAdd(1, .monotonic);
                log.warn("plc cursor unavailable: {s}", .{@errorName(err)});
                self.io.sleep(Io.Duration.fromSeconds(FAILURE_BACKOFF_SECS), .awake) catch return;
                continue;
            };
            break;
        }
        log.info("tailing plc export after {s}", .{cursor.items});

        while (true) {
            const wait: i64 = if (self.step(&worker, &plc, &cursor)) |n|
                if (n >= PAGE_SIZE) CATCHUP_POLL_SECS else IDLE_POLL_SECS
            else |err| blk: {
                _ = stats.failed.fetchAdd(1, .monotonic);
                log.warn("plc tail step failed: {s}", .{@errorName(err)});
                break :blk FAILURE_BACKOFF_SECS;
            };
            self.io.sleep(Io.Duration.fromSeconds(wait), .awake) catch return;
        }
    }
};

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

// One real export page from plc.directory, reported to a worker (local
// `wrangler dev` on local sqld); skipped unless TYPEAHEAD_IDENTITY_E2E_URL is set.
test "integration: one plc export page is reported and advances the cursor" {
    const url = if (std.c.getenv("TYPEAHEAD_IDENTITY_E2E_URL")) |p| mem.span(p) else return error.SkipZigTest;
    const secret = if (std.c.getenv("TYPEAHEAD_IDENTITY_E2E_SECRET")) |p| mem.span(p) else return error.SkipZigTest;
    const io = std.Options.debug_io;
    var tail: Tail = .{ .allocator = testing.allocator, .io = io, .worker_url = url, .worker_secret = secret };
    var worker = HttpTransport.init(io, testing.allocator);
    defer worker.deinit();
    var plc = HttpTransport.init(io, testing.allocator);
    plc.user_agent = USER_AGENT;
    defer plc.deinit();

    var cursor: std.ArrayList(u8) = .empty;
    defer cursor.deinit(testing.allocator);
    try tail.fetchCursor(&worker, &cursor);
    const start = try testing.allocator.dupe(u8, cursor.items);
    defer testing.allocator.free(start);
    const n = try tail.step(&worker, &plc, &cursor);
    try testing.expect(n > 0);
    try testing.expect(mem.order(u8, cursor.items, start) == .gt);

    var stored: std.ArrayList(u8) = .empty;
    defer stored.deinit(testing.allocator);
    try tail.fetchCursor(&worker, &stored);
    try testing.expectEqualStrings(cursor.items, stored.items);
}
