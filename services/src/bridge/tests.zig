const std = @import("std");
const testing = std.testing;
const json = std.json;
const Bridge = @import("Bridge.zig");
const source = @import("source.zig");
const wire = @import("wire.zig");
const doubles = @import("testing.zig");

const Rig = struct {
    store: doubles.MemoryStore,
    server: doubles.ModelServer,
    clock: doubles.FakeClock = .{},
    chaos: doubles.Chaos = undefined,
    arena: std.heap.ArenaAllocator,

    fn init(self: *Rig) void {
        self.* = .{
            .store = .init(testing.allocator),
            .server = .init(testing.allocator),
            .arena = .init(testing.allocator),
        };
        self.chaos = .{ .inner = self.server.transport() };
    }

    fn deinit(self: *Rig) void {
        self.store.deinit();
        self.server.deinit();
        self.arena.deinit();
    }

    fn config(self: *Rig) Bridge.Config {
        return .{ .source_id = "store-a", .clock = .{ .ptr = &self.clock, .now = doubles.FakeClock.now } };
    }

    fn bridge(self: *Rig, options: Bridge.Config) Bridge {
        return Bridge.init(testing.allocator, std.Options.debug_io, options, self.store.interface(), self.chaos.transport());
    }

    fn seed(self: *Rig, count: usize) !void {
        for (0..count) |n| try self.put(n, "seed");
    }

    fn id(self: *Rig, n: usize) []const u8 {
        return std.fmt.allocPrint(self.arena.allocator(), "did:plc:test{d:0>5}", .{n}) catch @panic("out of memory");
    }

    fn make(self: *Rig, n: usize, name: []const u8) !source.Actor {
        const a = self.arena.allocator();
        return .{
            .handle = try std.fmt.allocPrint(a, "user{d}.test", .{n}),
            .display_name = try std.fmt.allocPrint(a, "{s} {d}", .{ name, n }),
            .quality_score = @floatFromInt(n),
        };
    }

    fn put(self: *Rig, n: usize, name: []const u8) !void {
        try self.store.put(self.id(n), try self.make(n, name));
    }

    fn cursor(self: *Rig) wire.Cursor {
        return self.server.committedCursor(self.arena.allocator()).?;
    }

    fn string(self: *Rig, did: []const u8, name: []const u8) ![]const u8 {
        return (try self.server.field(self.arena.allocator(), did, name)).?.string;
    }

    fn flag(self: *Rig, did: []const u8, name: []const u8) !bool {
        return (try self.server.field(self.arena.allocator(), did, name)).?.bool;
    }

    /// The index holds exactly the store's current state: every live actor
    /// with its fields, and a tombstone for everything else it has held.
    fn expectConverged(self: *Rig) !void {
        const a = self.arena.allocator();
        for (self.store.actors.keys(), self.store.actors.values()) |key, actor| {
            const document = self.server.documents.get(key) orelse return error.MissingDocument;
            if (actor.status != .live or actor.handle.len == 0) {
                try testing.expect(document.deleted);
                continue;
            }
            try testing.expect(!document.deleted);
            try testing.expectEqualStrings(actor.handle, try self.string(key, "handle"));
            try testing.expectEqualStrings(actor.display_name, try self.string(key, "display_name"));
            try testing.expectEqual(actor.hidden, try self.flag(key, "hidden"));
        }
        for (self.server.documents.keys(), self.server.documents.values()) |key, document| {
            if (self.store.get(key) == null) try testing.expect(document.deleted);
        }
        const committed = self.server.committedCursor(a) orelse return error.MissingCursor;
        try testing.expectEqual(self.store.next_change, committed.position);
        try testing.expectEqual(@as(?u64, self.store.next_change), self.store.bridge);
    }
};

fn settle(bridge: *Bridge) !void {
    for (0..500) |_| {
        if ((try bridge.step()).idle) return;
    }
    return error.DidNotSettle;
}

/// Step through failures the way the run loop does.
fn settleThroughErrors(bridge: *Bridge) !void {
    for (0..500) |_| {
        const result = bridge.step() catch continue;
        if (result.idle) return;
    }
    return error.DidNotSettle;
}

test "bootstrap resyncs an empty index and withholds the watermark until the round completes" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(7);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();

    var options = rig.config();
    options.max_actors = 3;
    options.resync_pages_per_step = 1;
    var slow = rig.bridge(options);
    defer slow.deinit();
    _ = try slow.step();
    try testing.expectEqual(@as(u64, 0), rig.server.watermarks);
    try testing.expectEqual(wire.Phase.source, rig.cursor().reconciliation.phase);
    try testing.expectEqualStrings("did:plc:test00002", rig.cursor().reconciliation.after_did);
    try testing.expectEqual(@as(u64, 8), rig.cursor().position);
    try testing.expectEqual(@as(?u64, 8), rig.store.bridge);

    try settle(&bridge);
    try rig.expectConverged();
    const committed = rig.cursor();
    try testing.expectEqual(wire.Phase.idle, committed.reconciliation.phase);
    try testing.expectEqual(@as(u64, 1), committed.reconciliation.round);
    try testing.expect(committed.reconciliation.last_completed_at != null);
    try testing.expectEqualStrings("store-a", committed.source_id);
    try testing.expectEqualStrings("model", committed.build_id);
    try testing.expectEqual(rig.server.watermark, committed.through);
    try testing.expectEqual(rig.clock.seconds, rig.server.watermark);
    try testing.expectEqual(@as(u64, 7), rig.server.applied);

    const health = bridge.health();
    try testing.expectEqual(@as(u64, 0), health.lag);
    try testing.expectEqual(@as(?u64, 8), health.position);
    try testing.expectEqual(@as(u64, 8), health.index_position);
    try testing.expectEqual(rig.clock.seconds, health.last_push_at);
    try testing.expectEqual(wire.Phase.idle, health.resync_phase);
}

test "repeated changes to one actor coalesce into one document at the latest sequence" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(2);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    const before = rig.server.reconciles;
    for ([_][]const u8{ "one", "two", "three", "four" }) |name| try rig.put(0, name);
    try rig.put(0, "four");
    try rig.put(1, "other");
    try rig.put(0, "final");
    try settle(&bridge);

    try testing.expectEqual(before + 1, rig.server.reconciles);
    const body = try json.parseFromSliceLeaky(json.Value, rig.arena.allocator(), rig.server.last_reconcile, .{});
    const actors = body.object.get("actors").?.array.items;
    try testing.expectEqual(@as(usize, 2), actors.len);
    try testing.expectEqualStrings("did:plc:test00000", actors[0].object.get("did").?.string);
    try testing.expectEqual(@as(i64, 8), actors[0].object.get("version").?.integer);
    try testing.expectEqual(@as(i64, 7), actors[1].object.get("version").?.integer);
    try testing.expectEqualStrings("final 0", try rig.string("did:plc:test00000", "display_name"));
    try testing.expectEqual(@as(u64, 8), rig.server.documents.get("did:plc:test00000").?.version);
    try rig.expectConverged();
}

test "the loop rests only when the log is drained, and what arrives meanwhile is one push" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var options = rig.config();
    options.poll_interval_ms = 5000;
    options.max_batches_per_step = 1;
    var bridge = rig.bridge(options);
    defer bridge.deinit();
    try settle(&bridge);

    const before = rig.server.reconciles;
    for (0..40) |n| try rig.put(n, "while resting");
    const drained = try bridge.step();
    try testing.expectEqual(@as(usize, 1), drained.pushes);
    try testing.expectEqual(before + 1, rig.server.reconciles);
    try testing.expectEqual(@as(?u64, 5000), bridge.rest(drained));
    try rig.expectConverged();

    // Behind by more than a step can push: no rest until it has caught up.
    for (0..1201) |n| try rig.put(n, "backlog");
    const behind = try bridge.step();
    try testing.expectEqual(@as(usize, 1), behind.pushes);
    try testing.expect(!behind.idle);
    try testing.expectEqual(@as(?u64, null), bridge.rest(behind));
    try settle(&bridge);
    try rig.expectConverged();
}

test "handle, display name, hide, unhide, delete and re-create all reach the index" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);
    const did = rig.id(1);

    var subject = try rig.make(1, "seed");
    subject.handle = "renamed.test";
    try rig.store.put(did, subject);
    try settle(&bridge);
    try testing.expectEqualStrings("renamed.test", try rig.string(did, "handle"));

    subject.display_name = "New Name";
    try rig.store.put(did, subject);
    try settle(&bridge);
    try testing.expectEqualStrings("New Name", try rig.string(did, "display_name"));

    subject.hidden = true;
    try rig.store.put(did, subject);
    try settle(&bridge);
    try testing.expect(try rig.flag(did, "hidden"));

    subject.hidden = false;
    try rig.store.put(did, subject);
    try settle(&bridge);
    try testing.expect(!try rig.flag(did, "hidden"));

    try rig.store.delete(did);
    try settle(&bridge);
    try testing.expect(rig.server.documents.get(did).?.deleted);
    try testing.expectEqualStrings("", try rig.string(did, "handle"));

    subject.status = .deactivated;
    try rig.store.put(did, subject);
    try settle(&bridge);
    try testing.expect(rig.server.documents.get(did).?.deleted);

    subject.status = .live;
    try rig.store.put(did, subject);
    try settle(&bridge);
    try testing.expect(!rig.server.documents.get(did).?.deleted);
    try testing.expectEqualStrings("renamed.test", try rig.string(did, "handle"));
    try rig.expectConverged();
}

test "a long run of changes is pushed in bounded batches and the position follows each one" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);
    try rig.seed(1201);

    var options = rig.config();
    options.max_batches_per_step = 1;
    var one = rig.bridge(options);
    defer one.deinit();
    const first = try one.step();
    try testing.expectEqual(@as(usize, 1), first.pushes);
    try testing.expect(!first.idle);
    try testing.expectEqual(@as(?u64, 501), rig.store.bridge);
    try testing.expectEqual(@as(u64, 701), one.health().lag);

    try settleThroughErrors(&bridge);
    try testing.expectEqual(@as(usize, 500), rig.server.max_actors_seen);
    try testing.expectEqual(@as(u64, 1201), rig.server.applied);
    try rig.expectConverged();
}

test "a lost acknowledgement is found in the cursor and nothing is applied twice" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    try rig.put(1, "changed");
    rig.server.fault = .drop_after;
    try testing.expectError(error.IndexUnavailable, bridge.step());
    try testing.expectEqual(@as(?u64, 4), rig.store.bridge);
    try testing.expectEqual(@as(u64, 5), rig.cursor().position);
    const applied = rig.server.applied;
    const reconciles = rig.server.reconciles;

    try settle(&bridge);
    try testing.expectEqual(applied, rig.server.applied);
    try testing.expectEqual(reconciles, rig.server.reconciles);
    try testing.expectEqual(@as(u64, 1), bridge.health().recovered);
    try testing.expectEqual(@as(u64, 1), bridge.health().errors);
    try testing.expectEqualStrings("IndexUnavailable", bridge.health().lastError());
    try rig.expectConverged();
}

test "a request that never arrives leaves the position alone and is rebuilt" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    try rig.put(1, "first attempt");
    rig.server.fault = .drop_before;
    try testing.expectError(error.IndexUnavailable, bridge.step());
    try testing.expectEqual(@as(?u64, 4), rig.store.bridge);
    try testing.expectEqual(@as(u64, 4), rig.cursor().position);

    try rig.put(1, "second attempt");
    try settle(&bridge);
    try testing.expectEqualStrings("second attempt 1", try rig.string("did:plc:test00001", "display_name"));
    try testing.expectEqual(@as(u64, 0), bridge.health().recovered);
    try rig.expectConverged();
}

test "a compare-and-set conflict advances nothing and the next step re-reads the cursor" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    try rig.put(2, "changed");
    rig.server.fault = .conflict;
    try testing.expectError(error.Conflict, bridge.step());
    try testing.expectEqual(@as(?u64, 4), rig.store.bridge);
    try testing.expectEqual(@as(u64, 1), bridge.health().conflicts);
    try settle(&bridge);
    try rig.expectConverged();
}

test "a second writer on the same index is detected by the revision guard" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var first = rig.bridge(rig.config());
    defer first.deinit();
    var second = rig.bridge(rig.config());
    defer second.deinit();
    try settle(&first);
    try settle(&second);

    try rig.put(0, "from first");
    try settleThroughErrors(&first);
    try rig.put(1, "from second");
    try testing.expectError(error.Conflict, second.step());
    try testing.expectEqualStrings("seed 1", try rig.string("did:plc:test00001", "display_name"));
    try settle(&second);
    try rig.expectConverged();
}

test "a failed position write is repaired from the cursor on the next start" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    try rig.put(0, "changed");
    rig.store.fail_next_set_position = true;
    try testing.expectError(error.StoreWriteFailed, bridge.step());
    try testing.expectEqual(@as(?u64, 4), rig.store.bridge);
    try testing.expectEqual(@as(u64, 5), rig.cursor().position);
    try settle(&bridge);
    try testing.expectEqual(@as(?u64, 5), rig.store.bridge);
    try rig.expectConverged();
}

test "an idle log still sends a watermark and the policy on the heartbeat" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(2);
    rig.store.policy_value = .{
        .overrides = &.{.{ .did = "did:plc:test00001", .action = .hide }},
        .blocked_domains = &.{"spam.test"},
    };
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);
    const reconciles = rig.server.reconciles;
    const watermark = rig.server.watermark;

    rig.clock.seconds += 29;
    try settle(&bridge);
    try testing.expectEqual(reconciles, rig.server.reconciles);

    rig.clock.seconds += 1;
    try settle(&bridge);
    try testing.expectEqual(reconciles + 1, rig.server.reconciles);
    try testing.expectEqual(watermark + 30, rig.server.watermark);
    try testing.expectEqual(@as(?u64, watermark + 30), rig.server.policy_observed_at);
    try testing.expectEqual(watermark + 30, rig.cursor().through);
    const body = try json.parseFromSliceLeaky(json.Value, rig.arena.allocator(), rig.server.last_reconcile, .{});
    try testing.expectEqual(@as(usize, 0), body.object.get("actors").?.array.items.len);
    try testing.expectEqualStrings("hide", body.object.get("policy_snapshot").?.object.get("overrides").?.array.items[0].object.get("action").?.string);

    rig.clock.seconds -= 500;
    try rig.put(0, "clock went back");
    try settle(&bridge);
    try testing.expectEqual(watermark + 30, rig.server.watermark);
    try testing.expectEqual(@as(?u64, watermark + 30), rig.server.policy_observed_at);
    try rig.expectConverged();
}

test "a position below the log's floor forces a resync that also removes what the gap deleted" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(6);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);
    const watermarks = rig.server.watermarks;

    try rig.store.delete(rig.id(2));
    try rig.store.delete(rig.id(4));
    rig.store.prune(rig.id(4));
    try rig.put(3, "changed in the gap");
    try rig.put(9, "created in the gap");
    rig.store.trim(rig.store.next_change);

    var options = rig.config();
    options.resync_pages_per_step = 1;
    var slow = rig.bridge(options);
    defer slow.deinit();
    rig.clock.seconds += 60;
    _ = try slow.step();
    try testing.expectEqual(watermarks, rig.server.watermarks);
    try testing.expect(rig.cursor().reconciliation.last_completed_at == null);
    try testing.expectEqual(@as(u64, 2), rig.cursor().reconciliation.round);
    try testing.expectEqual(rig.store.next_change, rig.cursor().position);
    try testing.expect(std.mem.indexOf(u8, slow.health().lastError(), "floor") != null);

    try settle(&slow);
    try testing.expect(rig.server.documents.get(rig.id(2)).?.deleted);
    try testing.expect(rig.server.documents.get(rig.id(4)).?.deleted);
    try testing.expectEqualStrings("changed in the gap 3", try rig.string(rig.id(3), "display_name"));
    try testing.expect(rig.server.watermarks > watermarks);
    try testing.expectEqual(@as(u64, 1), slow.health().resyncs_started);
    try rig.expectConverged();
}

test "an index past the end of the store's log is resynced to the store" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(5);
    {
        var bridge = rig.bridge(rig.config());
        defer bridge.deinit();
        try settle(&bridge);
    }
    var replaced: doubles.MemoryStore = .init(testing.allocator);
    std.mem.swap(doubles.MemoryStore, &replaced, &rig.store);
    defer replaced.deinit();
    try rig.put(1, "only survivor");

    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);
    try testing.expect(rig.server.documents.get(rig.id(4)).?.deleted);
    try testing.expectEqualStrings("only survivor 1", try rig.string(rig.id(1), "display_name"));
    try rig.expectConverged();
}

test "a record pruned from the store is found by the index sweep of a requested resync" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(4);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    rig.store.prune(rig.id(2));
    try settle(&bridge);
    try testing.expect(!rig.server.documents.get(rig.id(2)).?.deleted);
    bridge.requestResync();
    try settle(&bridge);
    try testing.expect(rig.server.documents.get(rig.id(2)).?.deleted);
    try rig.expectConverged();
}

test "a cursor from another controller is refused unless adoption is configured" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(2);
    rig.server.cursor = "{\"schema_version\":1,\"source_policy\":\"turso-authoritative-observations-v2\",\"through\":100}";
    rig.server.revision = 5;

    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try testing.expectError(error.ForeignCursor, bridge.step());
    try testing.expectEqual(@as(u64, 0), rig.server.reconciles);
    try testing.expect(rig.store.bridge == null);

    var options = rig.config();
    options.adopt_foreign_cursor = true;
    var adopter = rig.bridge(options);
    defer adopter.deinit();
    try settle(&adopter);
    try rig.expectConverged();
}

test "a cursor written for another store is not continued" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    {
        var options = rig.config();
        options.source_id = "store-b";
        var other = rig.bridge(options);
        defer other.deinit();
        try settle(&other);
    }
    rig.store.prune(rig.id(0));

    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);
    try testing.expectEqualStrings("store-a", rig.cursor().source_id);
    try testing.expect(rig.server.documents.get("did:plc:test00000").?.deleted);
    try rig.expectConverged();
}

test "a record the index cannot hold becomes a counted tombstone and an inadmissible DID is passed over" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    var broken = try rig.make(1, "broken");
    broken.associated = "[1]";
    try rig.store.put(rig.id(1), broken);
    var unhandled = try rig.make(2, "no handle");
    unhandled.handle = "";
    try rig.store.put(rig.id(2), unhandled);
    try rig.store.touch("did:plc:has space");
    try rig.store.touch("not-a-did");
    try settle(&bridge);

    try testing.expect(rig.server.documents.get(rig.id(1)).?.deleted);
    try testing.expect(rig.server.documents.get(rig.id(2)).?.deleted);
    try testing.expect(rig.server.documents.get("not-a-did") == null);
    try testing.expectEqual(@as(u64, 3), bridge.health().rejected);
    try testing.expect(std.mem.indexOf(u8, bridge.health().lastError(), "inadmissible") != null);
    try testing.expectEqual(rig.store.next_change, rig.cursor().position);
    try testing.expectEqual(@as(?u64, rig.store.next_change), rig.store.bridge);

    try rig.put(1, "repaired");
    try settle(&bridge);
    try testing.expectEqualStrings("repaired 1", try rig.string("did:plc:test00001", "display_name"));
}

test "a server refusal stops progress, names the batch and halves the next attempt" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);
    try rig.seed(8);

    rig.server.fault = .invalid;
    try testing.expectError(error.Rejected, bridge.step());
    try testing.expectEqual(@as(?u64, 1), rig.store.bridge);
    try testing.expect(std.mem.indexOf(u8, bridge.health().lastError(), "injected refusal") != null);
    try testing.expect(std.mem.indexOf(u8, bridge.health().lastError(), "did:plc:test00000") != null);

    const before = rig.server.reconciles;
    try settle(&bridge);
    try testing.expectEqual(before + 2, rig.server.reconciles);
    try rig.expectConverged();
}

test "an interrupted resync resumes from the committed page and a requested round sweeps again" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(20);
    var options = rig.config();
    options.max_actors = 4;
    options.resync_pages_per_step = 2;
    {
        var bridge = rig.bridge(options);
        defer bridge.deinit();
        _ = try bridge.step();
    }
    try testing.expectEqualStrings("did:plc:test00007", rig.cursor().reconciliation.after_did);
    const applied = rig.server.applied;

    var bridge = rig.bridge(options);
    defer bridge.deinit();
    _ = try bridge.step();
    try testing.expectEqual(@as(u64, 1), rig.cursor().reconciliation.round);
    try testing.expectEqualStrings("did:plc:test00015", rig.cursor().reconciliation.after_did);
    try testing.expectEqual(applied + 8, rig.server.applied);
    try settle(&bridge);
    try testing.expectEqual(@as(u64, 20), rig.server.applied);
    const completed = rig.cursor().reconciliation.last_completed_at.?;

    const dropped = rig.server.documents.getPtr("did:plc:test00005").?;
    dropped.projection = "{}";
    dropped.hash = @splat('0');
    try testing.expectEqual(@as(usize, 0), (try bridge.step()).resync_pages);
    rig.clock.seconds += 10;
    bridge.requestResync();
    try settle(&bridge);
    try testing.expectEqual(@as(u64, 2), rig.cursor().reconciliation.round);
    try testing.expectEqual(completed + 10, rig.cursor().reconciliation.last_completed_at.?);
    try testing.expectEqual(completed + 10, rig.cursor().reconciliation.last_completed_started_at.?);
    try testing.expectEqual(@as(u64, 21), rig.server.applied);
    try rig.expectConverged();
}

test "the backend sweep commits its progress once per step when pages need no write" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(12);
    var options = rig.config();
    options.max_actors = 3;
    options.resync_pages_per_step = 100;
    var bridge = rig.bridge(options);
    defer bridge.deinit();
    const before = rig.server.reconciles;
    _ = try bridge.step();
    // 4 source pages, the phase change, one commit that ends the round
    // without a commit for any of the 4 backend pages, then the watermark.
    try testing.expectEqual(before + 7, rig.server.reconciles);
    try rig.expectConverged();

    options.resync_pages_per_step = 2;
    var paced = rig.bridge(options);
    defer paced.deinit();
    paced.requestResync();
    for (0..3) |_| _ = try paced.step();
    try testing.expectEqual(wire.Phase.backend, rig.cursor().reconciliation.phase);
    try testing.expectEqualStrings("did:plc:test00002", rig.cursor().reconciliation.after_did);
}

test "a walk that returns more than the cap is pushed a cap at a time" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(20);
    rig.store.walk_excess = 5;
    var options = rig.config();
    options.max_actors = 3;
    var bridge = rig.bridge(options);
    defer bridge.deinit();
    try settle(&bridge);
    try testing.expectEqual(@as(usize, 3), rig.server.max_actors_seen);
    try testing.expectEqual(@as(u64, 20), rig.server.applied);
    try rig.expectConverged();
}

test "a scheduled resync starts once the interval has passed" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(2);
    var options = rig.config();
    options.resync_interval_s = 3600;
    var bridge = rig.bridge(options);
    defer bridge.deinit();
    try settle(&bridge);
    rig.clock.seconds += 3599;
    try settle(&bridge);
    try testing.expectEqual(@as(u64, 1), rig.cursor().reconciliation.round);
    rig.clock.seconds += 1;
    try settle(&bridge);
    try testing.expectEqual(@as(u64, 2), rig.cursor().reconciliation.round);
}

test "a round owed since a given time runs once, whatever restarts in between" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.seed(3);
    var options = rig.config();
    options.resync_after = rig.clock.seconds + 500;
    {
        var bridge = rig.bridge(options);
        defer bridge.deinit();
        try settle(&bridge);
        try testing.expectEqual(@as(u64, 1), rig.cursor().reconciliation.round);
        rig.clock.seconds += 499;
        try settle(&bridge);
        try testing.expectEqual(@as(u64, 1), rig.cursor().reconciliation.round);

        rig.clock.seconds += 1;
        try settle(&bridge);
        try testing.expectEqual(@as(u64, 2), rig.cursor().reconciliation.round);
        try testing.expectEqual(options.resync_after, rig.cursor().reconciliation.last_completed_started_at.?);
    }
    rig.clock.seconds += 86_400;
    var restarted = rig.bridge(options);
    defer restarted.deinit();
    try settle(&restarted);
    try testing.expectEqual(@as(u64, 2), rig.cursor().reconciliation.round);
    try rig.expectConverged();
}

test "the profile-card fields reach the index, and a record without them is the document it was" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    try rig.put(1, "plain");
    var full = try rig.make(2, "full");
    full.description = "line one\nline two";
    full.banner = "bafkbanner";
    full.updated_at = 1_794_000_000;
    full.profile_checked = true;
    try rig.store.put(rig.id(2), full);
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try settle(&bridge);

    const a = rig.arena.allocator();
    try testing.expectEqualStrings("line one\nline two", try rig.string(rig.id(2), "description"));
    try testing.expectEqualStrings("bafkbanner", try rig.string(rig.id(2), "banner_url"));
    try testing.expectEqual(@as(i64, 1_794_000_000), (try rig.server.field(a, rig.id(2), "updated_at")).?.integer);
    try testing.expect(try rig.flag(rig.id(2), "profile_checked"));
    inline for (.{ "description", "banner_url", "updated_at", "profile_checked" }) |name| {
        try testing.expect(try rig.server.field(a, rig.id(1), name) == null);
    }

    const applied = rig.server.applied;
    full.description = "";
    full.profile_checked = false;
    try rig.store.put(rig.id(2), full);
    try settle(&bridge);
    try testing.expectEqual(applied + 1, rig.server.applied);
    try testing.expect(try rig.server.field(a, rig.id(2), "description") == null);
}

test "an index without a completed import is left alone" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    rig.server.import_complete = false;
    var bridge = rig.bridge(rig.config());
    defer bridge.deinit();
    try testing.expectError(error.IndexNotReady, bridge.step());
    try testing.expectEqual(@as(u64, 0), rig.server.reconciles);
}

test "random changes with kills before and after every kind of request converge" {
    var rig: Rig = undefined;
    rig.init();
    defer rig.deinit();
    var prng: std.Random.DefaultPrng = .init(0x42b1d6e);
    const random = prng.random();
    var options = rig.config();
    options.max_actors = 7;
    options.log_read_limit = 40;
    options.resync_pages_per_step = 2;

    var kills: usize = 0;
    for (0..120) |round| {
        for (0..random.uintLessThan(usize, 12)) |_| {
            const n = random.uintLessThan(usize, 60);
            switch (random.uintLessThan(u8, 10)) {
                0, 1 => try rig.store.delete(rig.id(n)),
                2 => if (rig.store.get(rig.id(n))) |record| {
                    if (record.status != .live) rig.store.prune(rig.id(n));
                },
                else => {
                    var changed = try rig.make(n, if (random.boolean()) "left" else "right");
                    changed.hidden = random.uintLessThan(u8, 4) == 0;
                    try rig.store.put(rig.id(n), changed);
                },
            }
        }
        if (round == 40) rig.store.trim(rig.store.next_change);
        rig.clock.seconds += random.uintLessThan(u64, 40);

        var bridge = rig.bridge(options);
        defer bridge.deinit();
        if (round % 17 == 0) bridge.requestResync();
        rig.chaos.countdown = random.uintLessThan(usize, 9);
        rig.chaos.after_effect = random.boolean();
        rig.chaos.fired = false;
        rig.store.fail_next_set_position = random.uintLessThan(u8, 10) == 0;
        for (0..3) |_| _ = bridge.step() catch break;
        if (rig.chaos.fired) kills += 1;
        rig.chaos.countdown = null;
        rig.store.fail_next_set_position = false;
    }
    try testing.expect(kills > 40);

    var bridge = rig.bridge(options);
    defer bridge.deinit();
    try settleThroughErrors(&bridge);
    try rig.expectConverged();
    try testing.expect(rig.cursor().reconciliation.last_completed_at != null);
}
