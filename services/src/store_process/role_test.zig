//! The role's start on a scratch store, offline: what has and has not
//! happened by each stage.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const config_mod = @import("config.zig");
const owner_mod = @import("owner.zig");
const role = @import("role.zig");
const Io = std.Io;
const Owner = owner_mod.Owner;
const testing = std.testing;

const Seen = struct {
    stage: role.Stage,
    /// The owner's `startup_seconds`.
    hold: u64,
    /// Whether the owner had taken a counted health snapshot.
    counted: bool,
    trimmed: u64,
    reader_open: bool,
    corpus_scans: u64,
};

const Recorder = struct {
    io: Io,
    lock: Io.Mutex = .init,
    seen: [8]Seen = undefined,
    len: usize = 0,
    finished: Io.Event = .unset,
    stopped: std.atomic.Value(u32) = .init(0),
    owner: ?*Owner = null,
    /// Whether the owner had stopped when `on_stopped` was called.
    owner_stopped: bool = false,

    fn stage(context: ?*anyopaque, at: role.Stage, state: role.State) void {
        const self: *Recorder = @ptrCast(@alignCast(context.?));
        self.lock.lockUncancelable(self.io);
        defer self.lock.unlock(self.io);
        self.seen[self.len] = .{
            .stage = at,
            .hold = state.owner.options.startup_seconds,
            .counted = state.owner.health().taken_at != 0,
            .trimmed = state.owner.counters.changes_trimmed.load(.monotonic),
            .reader_open = state.reader_open,
            .corpus_scans = state.corpus_scans,
        };
        self.len += 1;
        self.owner = state.owner;
        if (at == .corpus_started) self.finished.set(self.io);
    }

    fn stoppedHook(context: ?*anyopaque) void {
        const self: *Recorder = @ptrCast(@alignCast(context.?));
        self.owner_stopped = self.owner.?.isStopped();
        _ = self.stopped.fetchAdd(1, .monotonic);
    }
};

const Running = struct {
    listener: *Io.net.Server,
    config: config_mod.Config,
    options: role.Options,
    result: anyerror!void = {},

    fn run(self: *Running) void {
        self.result = role.run(testing.allocator, testing.io, self.listener, self.config, self.options);
    }
};

fn seed(path: []const u8) !void {
    const owner = try Owner.open(testing.allocator, testing.io, Io.Dir.cwd(), path, .{ .retain_without_consumer = 1 });
    defer owner.close();
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    const submission = try owner_mod.Submission.create(arena.allocator());
    submission.changes = &.{
        .{ .observe = .{ .did = "did:plc:alice" } },
        .{ .observe = .{ .did = "did:plc:bob" } },
        .{ .observe = .{ .did = "did:plc:carol" } },
    };
    submission.cursor = .{ .relay = 41 };
    submission.waited = true;
    try owner.post(submission, null);
    while (!submission.done.isSet()) _ = owner.turn(0);
    try testing.expect(!submission.failed);
}

test "the role feeds the index and follows upstream before it counts, trims, opens the read handle or scans, and stops once the owner has" {
    const io = testing.io;
    var tmp = testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [128]u8 = undefined;
    const path = try std.fmt.bufPrint(&path_buf, ".zig-cache/tmp/{s}/store", .{tmp.sub_path});
    try seed(path);

    var address: Io.net.IpAddress = .{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = 0 } };
    var listener = try address.listen(io, .{ .reuse_address = true });
    defer listener.deinit(io);

    var recorder: Recorder = .{ .io = io };
    var stop: std.atomic.Value(bool) = .init(false);
    var running: Running = .{
        .listener = &listener,
        .config = .{
            .store_dir = path,
            .offline = true,
            .corpus_scan_seconds = 3600,
            // Nothing listens there: the bridge runs and every step fails.
            .index = .{ .admin_url = "http://127.0.0.1:1", .admin_secret = "unused", .source_id = "role-test" },
        },
        .options = .{ .startup_seconds = 1, .corpus_delay_seconds = 3600, .context = &recorder, .on_stage = Recorder.stage, .on_stopped = Recorder.stoppedHook, .stop = &stop },
    };
    const thread = try std.Thread.spawn(.{}, Running.run, .{&running});
    recorder.finished.waitUncancelable(io);
    stop.store(true, .release);
    const wake = try listener.socket.address.connect(io, .{ .mode = .stream });
    wake.close(io);
    thread.join();
    try running.result;

    const stages = [_]role.Stage{ .store_opened, .bridge_started, .intake_started, .reader_opened, .corpus_started };
    try testing.expectEqual(stages.len, recorder.len);
    for (stages, recorder.seen[0..recorder.len]) |want, seen| try testing.expectEqual(want, seen.stage);
    for (recorder.seen[0..3]) |seen| {
        try testing.expectEqual(running.options.startup_seconds, seen.hold);
        try testing.expect(!seen.counted);
        try testing.expectEqual(@as(u64, 0), seen.trimmed);
        try testing.expect(!seen.reader_open);
    }
    try testing.expect(recorder.seen[3].reader_open);
    for (recorder.seen[0..recorder.len]) |seen| try testing.expectEqual(@as(u64, 0), seen.corpus_scans);

    try testing.expectEqual(@as(u32, 1), recorder.stopped.load(.monotonic));
    try testing.expect(recorder.owner_stopped);
    const reopened = try Owner.open(testing.allocator, io, Io.Dir.cwd(), path, .{});
    defer reopened.close();
    try testing.expectEqual(@as(?u64, 41), reopened.health().cursors.relay);
}
