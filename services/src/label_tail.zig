//! Follows Bluesky's moderation labeler for the store.
//!
//! The stream is `com.atproto.label.subscribeLabels` on the labeler's own
//! service. We keep labels from the adopted source whose subject is an
//! account (a DID) or that account's profile record, and hand them in batches
//! to a sink with the seq they reach. The sink (`store_process/tails.zig`)
//! commits the cursor with the batch, so the cursor only moves after the
//! labels land; any failure reconnects from the committed cursor and replays.
//!
//! Frames are decoded here rather than by the client, so a malformed frame is
//! counted and logged instead of vanishing at debug level.

const std = @import("std");
const Io = std.Io;
const Allocator = std.mem.Allocator;
const mem = std.mem;
const zat = @import("zat");

const log = std.log.scoped(.label_tail);

/// Bluesky's moderation service, the one labeler whose labels we adopt
pub const ADOPTED_SOURCE = "did:plc:ar7c4by46qjdydhdevvrndac";
const STREAM = "wss://mod.bsky.app";
const MAX_BATCH = 200;
const FLUSH_AFTER_MS = 5_000;
/// with nothing adopted, still record progress this often so a restart
/// replays minutes, not hours
const CURSOR_EVERY_MS = 60_000;
const IDLE_TIMEOUT_MS = 120_000;
const FAILURE_BACKOFF_SECS = 30;

pub const Stats = struct {
    frames: std.atomic.Value(u64) = .init(0),
    adopted: std.atomic.Value(u64) = .init(0),
    malformed: std.atomic.Value(u64) = .init(0),
    posts: std.atomic.Value(u64) = .init(0),
    post_failed: std.atomic.Value(u64) = .init(0),
    /// the cursor the sink last committed; 0 until the first commit
    committed: std.atomic.Value(i64) = .init(0),
    /// unix seconds of the newest label seen, for lag
    newest_cts: std.atomic.Value(i64) = .init(0),
};
pub var stats: Stats = .{};

/// The account a label is about when we act on it: adopted source, and a
/// subject that is a DID or that DID's profile record.
pub fn adoptedSubject(label: zat.Label) ?[]const u8 {
    if (!mem.eql(u8, label.src, ADOPTED_SOURCE)) return null;
    if (mem.startsWith(u8, label.uri, "did:")) return label.uri;
    const prefix = "at://";
    const suffix = "/app.bsky.actor.profile/self";
    if (!mem.startsWith(u8, label.uri, prefix) or !mem.endsWith(u8, label.uri, suffix)) return null;
    const did = label.uri[prefix.len .. label.uri.len - suffix.len];
    if (!mem.startsWith(u8, did, "did:") or mem.indexOfScalar(u8, did, '/') != null) return null;
    return did;
}

/// one adopted label, with the stream seq of the frame that carried it
pub const Wire = struct {
    src: []const u8,
    uri: []const u8,
    val: []const u8,
    cts: []const u8,
    seq: i64,
    cid: ?[]const u8 = null,
    neg: ?bool = null,
    exp: ?[]const u8 = null,
};

/// Follow the labeler stream into `sink` until cancelled. The sink answers
/// `cursor() !?i64`, the seq it has durably committed, and
/// `commit(labels, reached) !void`, which must land the labels and the seq
/// they reach together.
pub fn follow(allocator: Allocator, io: Io, sink: anytype) void {
    while (true) {
        const cursor = sink.cursor() catch |err| {
            _ = stats.post_failed.fetchAdd(1, .monotonic);
            log.warn("label cursor unavailable: {s}", .{@errorName(err)});
            io.sleep(Io.Duration.fromSeconds(FAILURE_BACKOFF_SECS), .awake) catch return;
            continue;
        };
        if (cursor) |c| stats.committed.store(c, .monotonic);
        log.info("following {s} labels after {?d}", .{ STREAM, cursor });

        var client = zat.LabelClient.init(io, allocator, .{
            .hosts = &.{STREAM},
            .cursor = cursor,
            .idle_timeout_ms = IDLE_TIMEOUT_MS,
        });
        defer client.deinit();
        var handler: Handler(@TypeOf(sink)) = .{ .allocator = allocator, .io = io, .sink = sink, .client = &client, .committed = cursor };
        defer handler.pending.deinit(allocator);
        defer handler.arena.deinit();
        client.subscribe(&handler) catch return;
    }
}

pub fn Handler(comptime Sink: type) type {
    return struct {
        allocator: Allocator,
        io: Io,
        sink: Sink,
        /// null in tests that drive frames by hand
        client: ?*zat.LabelClient,
        committed: ?i64,
        arena: std.heap.ArenaAllocator = .init(std.heap.page_allocator),
        pending: std.ArrayList(Wire) = .empty,
        /// the seq the buffered labels bring the stream to
        reached: ?i64 = null,
        first_pending_ms: i64 = 0,
        last_commit_ms: i64 = 0,

        const Self = @This();

        fn nowMs(self: *Self) i64 {
            return @intCast(@divFloor(Io.Timestamp.now(self.io, .awake).nanoseconds, std.time.ns_per_ms));
        }

        /// a new connection replays from the committed cursor, so labels still
        /// buffered from the old one would arrive twice
        pub fn onConnect(self: *Self, _: []const u8) void {
            self.pending.clearRetainingCapacity();
            _ = self.arena.reset(.retain_capacity);
            self.reached = null;
        }

        pub fn onRawFrame(self: *Self, data: []const u8) !void {
            _ = stats.frames.fetchAdd(1, .monotonic);
            var frame_arena = std.heap.ArenaAllocator.init(self.allocator);
            defer frame_arena.deinit();
            const event = zat.labels.decodeFrame(frame_arena.allocator(), data) catch |err| {
                _ = stats.malformed.fetchAdd(1, .monotonic);
                log.warn("skipping undecodable label frame ({d} bytes): {s}", .{ data.len, @errorName(err) });
                return;
            };
            switch (event) {
                .labels => |batch| try self.onLabels(batch.labels, batch.seq),
                .info => |info| log.info("labeler info: {s}", .{info.name orelse "?"}),
                .err => |e| {
                    // FutureCursor means our cursor is ahead of the labeler's log
                    log.err("labeler error frame: {s}: {s}", .{ e.name orelse "?", e.message orelse "" });
                    return error.LabelerError;
                },
            }
            try self.maybeFlush();
        }

        /// One decoded `#labels` frame.
        pub fn onLabels(self: *Self, labels: []const zat.Label, seq: i64) !void {
            for (labels) |label| try self.keep(label, seq);
            self.reached = seq;
        }

        fn keep(self: *Self, label: zat.Label, seq: i64) !void {
            if (zat.Datetime.parse(label.cts)) |t| stats.newest_cts.store(@divFloor(t.micros, 1_000_000), .monotonic);
            if (adoptedSubject(label) == null) return;
            const a = self.arena.allocator();
            if (self.pending.items.len == 0) self.first_pending_ms = self.nowMs();
            try self.pending.append(self.allocator, .{
                .src = try a.dupe(u8, label.src),
                .uri = try a.dupe(u8, label.uri),
                .val = try a.dupe(u8, label.val),
                .cts = try a.dupe(u8, label.cts),
                .seq = seq,
                .cid = if (label.cid) |c| try a.dupe(u8, c) else null,
                .neg = if (label.neg) true else null,
                .exp = if (label.exp) |e| try a.dupe(u8, e) else null,
            });
            _ = stats.adopted.fetchAdd(1, .monotonic);
        }

        fn maybeFlush(self: *Self) !void {
            const reached = self.reached orelse return;
            const now = self.nowMs();
            const due = self.pending.items.len >= MAX_BATCH or
                (self.pending.items.len > 0 and now - self.first_pending_ms >= FLUSH_AFTER_MS) or
                (self.committed != reached and now - self.last_commit_ms >= CURSOR_EVERY_MS);
            if (!due) return;
            self.flush(reached) catch |err| {
                _ = stats.post_failed.fetchAdd(1, .monotonic);
                log.warn("label batch not committed: {s}; replaying from {?d}", .{ @errorName(err), self.committed });
                // the client advanced past frames that never landed; reconnect
                // from what the sink actually holds
                if (self.client) |client| client.last_seq = self.committed;
                return err;
            };
        }

        pub fn flush(self: *Self, reached: i64) !void {
            try self.sink.commit(self.pending.items, reached);
            _ = stats.posts.fetchAdd(1, .monotonic);
            stats.committed.store(reached, .monotonic);
            self.committed = reached;
            self.last_commit_ms = self.nowMs();
            self.pending.clearRetainingCapacity();
            _ = self.arena.reset(.retain_capacity);
        }
    };
}

const testing = std.testing;

fn testLabel(src: []const u8, uri: []const u8) zat.Label {
    return .{ .src = src, .uri = uri, .val = "spam", .cts = "2026-11-01T00:00:00.000Z" };
}

test "adopts account and profile-record labels from Bluesky's moderation service only" {
    const did = "did:plc:k7kqivhnh5b52fyptrcp4e4a";
    try testing.expectEqualStrings(did, adoptedSubject(testLabel(ADOPTED_SOURCE, did)).?);
    try testing.expectEqualStrings(did, adoptedSubject(testLabel(ADOPTED_SOURCE, "at://" ++ did ++ "/app.bsky.actor.profile/self")).?);
    try testing.expectEqual(@as(?[]const u8, null), adoptedSubject(testLabel(ADOPTED_SOURCE, "at://" ++ did ++ "/app.bsky.feed.post/3mzaa3w7dwa24")));
    try testing.expectEqual(@as(?[]const u8, null), adoptedSubject(testLabel("did:plc:someotherlabeler", did)));
    try testing.expectEqual(@as(?[]const u8, null), adoptedSubject(testLabel(ADOPTED_SOURCE, "at://did:plc:a/b/app.bsky.actor.profile/self")));
}
