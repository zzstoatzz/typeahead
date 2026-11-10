//! The owner, the index bridge and a model of the index server together,
//! through kills of each: the bridge's requests dropped before or after
//! they take effect, the bridge restarted with nothing in memory, the
//! owner stopped mid-stream and its store reopened, and the change log
//! trimmed under the bridge. The index must end up holding the store.

const std = @import("std");
const bridge_mod = @import("../bridge/root.zig");
const store_mod = @import("../store/root.zig");
const store_harness = @import("../store/testing/harness.zig");
const index_bridge = @import("index_bridge.zig");
const owner_mod = @import("owner.zig");
const harness = @import("testing.zig");
const Bridge = bridge_mod.Bridge;
const doubles = bridge_mod.testing;
const Fixture = harness.Fixture;
const testing = std.testing;

const universe = 60;

const Rig = struct {
    f: *Fixture,
    running: harness.Running,
    source: index_bridge.StoreSource,
    server: doubles.ModelServer,
    chaos: doubles.Chaos,
    bridge: Bridge,
    random: std.Random,
    arena: std.mem.Allocator,
    tally: Tally = .{},

    const Tally = struct { dropped: u64 = 0, bridge_restarts: u64 = 0, owner_restarts: u64 = 0, trims: u64 = 0, resyncs: u64 = 0, errors: u64 = 0 };

    fn clockNow(ptr: *anyopaque) u64 {
        const f: *Fixture = @ptrCast(@alignCast(ptr));
        return f.now();
    }

    fn startBridge(self: *Rig) void {
        self.bridge = Bridge.init(testing.allocator, testing.io, .{
            .source_id = "store-under-test",
            .log_read_limit = 16,
            .max_actors = 8,
            .clock = .{ .ptr = self.f, .now = clockNow },
        }, self.source.interface(), self.chaos.transport());
    }

    fn stopBridge(self: *Rig) void {
        const health = self.bridge.health();
        self.tally.resyncs += health.resyncs_started;
        self.tally.errors += health.errors;
        self.bridge.deinit();
    }

    fn init(self: *Rig, random: std.Random) !void {
        const f = try Fixture.create(.{
            .store = .{ .layout = store_harness.small_layout, .maintenance = store_harness.busy_policy },
            .idle_wait_ms = 1,
            .prune_seconds = 0,
            .trim_seconds = 0,
        });
        errdefer f.destroy();
        self.* = .{
            .f = f,
            .running = try harness.Running.start(f),
            .source = undefined,
            .server = .init(testing.allocator),
            .chaos = undefined,
            .bridge = undefined,
            .random = random,
            .arena = f.arena.allocator(),
        };
        errdefer self.running.stop();
        self.source = try index_bridge.StoreSource.open(testing.allocator, f.owner);
        self.chaos = .{ .inner = self.server.transport() };
        self.startBridge();
    }

    fn deinit(self: *Rig) void {
        self.stopBridge();
        self.running.stop();
        self.source.close();
        self.server.deinit();
        self.f.destroy();
    }

    fn did(self: *Rig, n: usize) []const u8 {
        return std.fmt.allocPrint(self.arena, "did:plc:{d:0>24}", .{n}) catch @panic("out of memory");
    }

    fn write(self: *Rig, list: []const store_mod.Change) !void {
        const submission = try owner_mod.Submission.create(testing.allocator);
        defer submission.destroy();
        submission.changes = list;
        try self.f.owner.call(submission);
    }

    fn change(self: *Rig) !void {
        const subject = self.did(self.random.uintLessThan(usize, universe));
        const n = self.random.uintLessThan(u16, 1000);
        try self.write(&.{switch (self.random.uintLessThan(u8, 12)) {
            0...4 => .{ .profile = .{
                .did = subject,
                .handle = try std.fmt.allocPrint(self.arena, "user{d}.test", .{n % 90}),
                .display_name = try std.fmt.allocPrint(self.arena, "name {d}", .{n}),
            } },
            5 => .{ .observe = .{ .did = subject } },
            6, 7 => .{ .gone = .{ .did = subject, .status = if (n % 2 == 0) .deleted else .deactivated } },
            8 => .{ .reactivated = subject },
            9, 10 => .{ .label = .{ .did = subject, .source = store_mod.moderation.bsky_moderation, .value = "!hide", .negated = n % 3 == 0, .created = "2026-11-01T00:00:00.000Z", .sequence = self.f.now() + n } },
            else => .{ .override_set = .{ .did = subject, .action = if (n % 2 == 0) .hide else .show } },
        }});
    }

    /// One bridge step whose requests may be dropped: before the server
    /// sees one, or after it has taken effect.
    fn step(self: *Rig, faulty: bool) !bool {
        if (faulty and self.random.uintLessThan(u8, 100) < 35) {
            self.chaos.countdown = self.random.uintLessThan(usize, 4);
            self.chaos.after_effect = self.random.boolean();
            self.chaos.fired = false;
        }
        defer {
            if (self.chaos.fired) self.tally.dropped += 1;
            self.chaos.countdown = null;
            self.chaos.fired = false;
        }
        const result = self.bridge.step() catch return false;
        return result.idle;
    }

    fn restartOwner(self: *Rig) !void {
        self.running.stop();
        self.stopBridge();
        self.source.close();
        try self.f.restart();
        self.source = try index_bridge.StoreSource.open(testing.allocator, self.f.owner);
        self.startBridge();
        self.running = try harness.Running.start(self.f);
        self.tally.owner_restarts += 1;
    }

    fn restartBridge(self: *Rig) void {
        self.stopBridge();
        self.startBridge();
        self.tally.bridge_restarts += 1;
    }

    /// Drop the whole change log, so a bridge behind it must resync.
    fn trim(self: *Rig) !void {
        const store = try self.f.owner.acquire();
        defer self.f.owner.release();
        _ = try store.trimChanges(store.next_change, 10_000);
        self.tally.trims += 1;
    }

    fn settle(self: *Rig) !void {
        var idle: usize = 0;
        for (0..5000) |_| {
            idle = if (try self.step(false)) idle + 1 else 0;
            if (idle == 3) return;
        }
        return error.DidNotSettle;
    }

    /// The index holds exactly the store: every live actor with a handle,
    /// with its fields, and a tombstone for anything else it ever held.
    fn expectConverged(self: *Rig) !void {
        try self.settle();
        const barrier = try owner_mod.Submission.create(testing.allocator);
        defer barrier.destroy();
        try self.f.owner.call(barrier);

        const store = try self.f.owner.acquire();
        defer self.f.owner.release();
        var held: usize = 0;
        var after: []const u8 = "";
        while (true) {
            const dids = try store.walk(self.arena, after, 17);
            for (dids) |subject| {
                const actor = (try store.actor(self.arena, subject)).?;
                const document = self.server.documents.get(subject);
                if (!actor.live() or actor.handle.len == 0) {
                    if (document) |found| try testing.expect(found.deleted);
                    continue;
                }
                held += 1;
                try testing.expect(document != null and !document.?.deleted);
                try testing.expectEqualStrings(actor.handle, (try self.server.field(self.arena, subject, "handle")).?.string);
                try testing.expectEqualStrings(actor.display_name, (try self.server.field(self.arena, subject, "display_name")).?.string);
                try testing.expectEqual(actor.hidden, (try self.server.field(self.arena, subject, "hidden")).?.bool);
            }
            if (dids.len < 17) break;
            after = dids[dids.len - 1];
        }
        var indexed: usize = 0;
        for (self.server.documents.keys(), self.server.documents.values()) |subject, document| {
            if (document.deleted) continue;
            indexed += 1;
            const actor = try store.actor(self.arena, subject) orelse return error.IndexHoldsAnActorTheStoreDoesNot;
            try testing.expect(actor.live() and actor.handle.len > 0);
        }
        try testing.expectEqual(held, indexed);
        const committed = self.server.committedCursor(self.arena) orelse return error.MissingCursor;
        try testing.expectEqual(store.next_change, committed.position);
        try testing.expectEqual(@as(?u64, store.next_change), (try store.cursors(self.arena)).bridge);
    }
};

test "the index converges to the store through kills of the bridge, the owner and the log" {
    var total: Rig.Tally = .{};
    for ([_]u64{ 3, 20261110 }) |seed| {
        var prng: std.Random.DefaultPrng = .init(seed);
        var rig: Rig = undefined;
        try rig.init(prng.random());
        defer rig.deinit();
        errdefer std.debug.print("bridge convergence failed: seed {d}, bridge health {any}\n", .{ seed, rig.bridge.health() });

        for (0..40) |round| {
            for (0..1 + rig.random.uintLessThan(usize, 12)) |_| try rig.change();
            for (0..rig.random.uintLessThan(usize, 4)) |_| _ = try rig.step(true);
            switch (rig.random.uintLessThan(u8, 20)) {
                0, 1 => rig.restartBridge(),
                2, 3 => try rig.restartOwner(),
                4, 6, 7 => try rig.trim(),
                5 => rig.f.advance(8 * 86_400),
                else => rig.f.advance(rig.random.uintLessThan(u64, 90)),
            }
            if (round % 10 == 9) try rig.expectConverged();
        }
        try rig.expectConverged();
        rig.tally.resyncs += rig.bridge.health().resyncs_started;
        inline for (comptime std.meta.fieldNames(Rig.Tally)) |name| @field(total, name) += @field(rig.tally, name);
    }
    errdefer std.debug.print("bridge convergence coverage: {any}\n", .{total});
    try testing.expect(total.dropped > 5 and total.bridge_restarts > 1 and total.owner_restarts > 1);
    try testing.expect(total.trims > 0 and total.resyncs > 1);
}

test "the bridge's position reaches the store through the owner's next commit" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    var source = try index_bridge.StoreSource.open(testing.allocator, f.owner);
    defer source.close();
    const interface = source.interface();
    _ = try f.commit(&.{.{ .profile = .{ .did = "did:plc:a", .handle = "a.test" } }}, null);

    try testing.expectEqual(@as(?u64, null), (try interface.cursors()).bridge);
    try interface.setPosition(2);
    try testing.expectEqual(@as(?u64, null), (try interface.cursors()).bridge);
    _ = f.owner.turn(0);
    const cursors = try interface.cursors();
    try testing.expectEqual(@as(?u64, 2), cursors.bridge);
    try testing.expectEqual(@as(u64, 2), cursors.next_change);
    try testing.expect(try interface.policy(f.arena.allocator()) == null);
    try testing.expectEqualStrings("a.test", (try interface.actor(f.arena.allocator(), "did:plc:a")).?.handle);
    try testing.expect(try interface.actor(f.arena.allocator(), "did:plc:b") == null);
    var buf: [8]owner_mod.Consumer = undefined;
    try testing.expectEqualStrings("bridge", f.owner.consumerList(&buf)[0].name);
}
