const std = @import("std");
const store_mod = @import("../store/root.zig");
const owner_mod = @import("owner.zig");
const harness = @import("testing.zig");
const Fixture = harness.Fixture;
const Submission = owner_mod.Submission;
const Store = store_mod.Store;
const maintain = store_mod.maintain;
const testing = std.testing;

const alice = "did:plc:alice";
const bob = "did:plc:bob";

fn queue(f: *Fixture, list: []const store_mod.Change, cursor: ?store_mod.change.CursorUpdate) !*Submission {
    const submission = try Submission.create(f.arena.allocator());
    submission.changes = list;
    submission.cursor = cursor;
    submission.waited = true;
    try f.owner.post(submission, null);
    return submission;
}

test "queued submissions land in order as one commit, each with its cursor" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    const first = try queue(f, &.{.{ .observe = .{ .did = alice } }}, .{ .relay = 5 });
    const second = try queue(f, &.{.{ .gone = .{ .did = alice, .status = .deleted } }}, .{ .relay = 9 });
    const third = try queue(f, &.{.{ .observe = .{ .did = bob } }}, .{ .labeler = 3 });
    try testing.expectEqual(@as(usize, 3), f.owner.turn(0));
    try testing.expect(first.done.isSet() and second.done.isSet() and third.done.isSet());
    try testing.expectEqual(@as(u64, 1), f.owner.counters.commits.load(.monotonic));
    try testing.expect(first.summary.created == 1 and second.summary.changed == 1);
    try testing.expect(!(try f.actor(alice)).?.live());
    const cursors = try f.cursors();
    try testing.expectEqual(@as(?u64, 9), cursors.relay);
    try testing.expectEqual(@as(?u64, 3), cursors.labeler);
    try testing.expectEqual(f.now(), f.owner.counters.last_commit_at.load(.monotonic));
}

test "a commit carries at most its budget and leaves the rest queued" {
    const f = try Fixture.create(.{ .max_commit_changes = 2 });
    defer f.destroy();
    const changes = [_]store_mod.Change{ .{ .observe = .{ .did = alice } }, .{ .observe = .{ .did = bob } } };
    _ = try queue(f, &changes, .{ .relay = 1 });
    const second = try queue(f, &changes, .{ .relay = 2 });
    try testing.expectEqual(@as(usize, 1), f.owner.turn(0));
    try testing.expect(!second.done.isSet());
    try testing.expectEqual(@as(?u64, 1), (try f.cursors()).relay);
    try testing.expectEqual(@as(usize, 1), f.owner.turn(0));
    try testing.expectEqual(@as(?u64, 2), (try f.cursors()).relay);
}

const Stall = struct {
    left: usize,

    fn step(context: ?*anyopaque, store: *Store) anyerror!maintain.Pressure {
        const self: *Stall = @ptrCast(@alignCast(context.?));
        _ = try store.maintainStep();
        if (self.left == 0) return .none;
        self.left -= 1;
        return .stalled;
    }
};

test "a stalled store takes nothing from the queue until the stall clears" {
    var stall: Stall = .{ .left = 2 };
    const f = try Fixture.create(.{ .maintenance = .{ .context = &stall, .step = Stall.step } });
    defer f.destroy();
    _ = f.owner.turn(0);
    try testing.expect(f.owner.counters.stalled.load(.monotonic));
    const waiting = try queue(f, &.{.{ .observe = .{ .did = alice } }}, .{ .relay = 1 });
    _ = f.owner.turn(0);
    _ = f.owner.turn(0);
    try testing.expect(!waiting.done.isSet());
    try testing.expectEqual(@as(u64, 2), f.owner.counters.stall_turns.load(.monotonic));
    try testing.expect(try f.actor(alice) == null);
    _ = f.owner.turn(0);
    try testing.expect(waiting.done.isSet());
    _ = try f.live(alice);
}

test "a broken store is reopened and the same submissions are applied once" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    _ = try f.commit(&.{.{ .observe = .{ .did = alice } }}, .{ .relay = 1 });

    const intruder = try Store.open(testing.allocator, testing.io, f.tmp.dir, "store", .{});
    intruder.close();

    const submission = try f.commit(&.{ .{ .observe = .{ .did = bob, .enqueue_identity = true } }, .{ .identity_lease = .{ .limit = 5 } } }, .{ .relay = 2 });
    try testing.expectEqual(@as(u64, 1), f.owner.counters.reopens.load(.monotonic));
    try testing.expectEqual(@as(u32, 1), submission.summary.created);
    try testing.expectEqual(@as(usize, 1), submission.jobs.len);
    try testing.expectEqualStrings(bob, submission.jobs[0].did);
    try testing.expectEqual(@as(u64, 1), submission.jobs[0].attempts);
    try testing.expectEqual(@as(?u64, 2), (try f.cursors()).relay);
    try testing.expectEqual(@as(u64, 2), f.owner.counters.actors_created.load(.monotonic));
    _ = try f.commit(&.{.{ .observe = .{ .did = alice } }}, .{ .relay = 3 });
    try testing.expectEqual(@as(u64, 1), f.owner.counters.reopens.load(.monotonic));
}

var fatal_calls: usize = 0;

fn countFatal() void {
    fatal_calls += 1;
}

fn alwaysFails(_: ?*anyopaque, _: *Store) anyerror!maintain.Pressure {
    return error.StoreBroken;
}

test "a store that will not commit stops the owner and fails what is queued" {
    fatal_calls = 0;
    const f = try Fixture.create(.{ .max_consecutive_failures = 3, .on_fatal = countFatal });
    defer f.destroy();
    _ = try f.commit(&.{.{ .observe = .{ .did = alice } }}, .{ .relay = 1 });
    f.owner.options.maintenance = .{ .step = alwaysFails };
    const waiting = try queue(f, &.{.{ .observe = .{ .did = bob } }}, .{ .relay = 2 });
    _ = f.owner.turn(0);
    try testing.expectEqual(@as(usize, 1), fatal_calls);
    try testing.expect(f.owner.isStopped());
    try testing.expect(waiting.done.isSet());
    try testing.expectError(error.OwnerStopped, queue(f, &.{}, null));
}

test "tombstones are pruned once they are older than the retention" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    _ = try f.commit(&.{ .{ .observe = .{ .did = alice } }, .{ .observe = .{ .did = bob } } }, null);
    _ = try f.commit(&.{.{ .gone = .{ .did = alice, .status = .deleted } }}, null);
    f.advance(f.owner.options.tombstone_retention_seconds);
    _ = f.owner.turn(0);
    try testing.expect(try f.actor(alice) != null);
    f.advance(f.owner.options.prune_seconds + 1);
    _ = f.owner.turn(0);
    try testing.expect(try f.actor(alice) == null);
    _ = try f.live(bob);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.tombstones_pruned.load(.monotonic));

    _ = try f.commit(&.{.{ .observe = .{ .did = alice, .enqueue_identity = true } }}, null);
    _ = try f.live(alice);
}

test "a domain block is followed by a sweep of the actors already stored" {
    const f = try Fixture.create(.{ .sweep_scan = 1 });
    defer f.destroy();
    _ = try f.commit(&.{
        .{ .profile = .{ .did = alice, .handle = "a.spam.example" } },
        .{ .profile = .{ .did = bob, .handle = "b.fine.example" } },
    }, null);
    _ = try f.commit(&.{.{ .domain_blocked = .{ .domain = "spam.example" } }}, null);
    try testing.expectEqual(@as(usize, 1), f.owner.sweeps.items.len);
    while (f.owner.sweeps.items.len > 0) _ = f.owner.turn(0);
    try testing.expect((try f.live(alice)).hidden);
    try testing.expect(!(try f.live(bob)).hidden);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.domain_sweeps.load(.monotonic));

    _ = try f.commit(&.{.{ .domain_unblocked = "spam.example" }}, null);
    while (f.owner.sweeps.items.len > 0) _ = f.owner.turn(0);
    try testing.expect(!(try f.live(alice)).hidden);
}

fn fill(f: *Fixture, first: usize, count: usize) !void {
    for (first..first + count) |n| {
        const did = try std.fmt.allocPrint(f.arena.allocator(), "did:plc:n{d:0>4}", .{n});
        _ = try f.commit(&.{.{ .observe = .{ .did = did } }}, null);
    }
}

test "the change log is trimmed below the slowest consumer" {
    const f = try Fixture.create(.{ .trim_seconds = 0 });
    defer f.destroy();
    f.owner.noteConsumer("bridge", 1);
    f.owner.noteConsumer("compare", 1);
    try fill(f, 0, 20);
    try testing.expectEqual(@as(u64, 1), (try f.cursors()).change_floor);
    f.owner.noteConsumer("bridge", 15);
    f.owner.noteConsumer("compare", 8);
    _ = f.owner.turn(0);
    try testing.expectEqual(@as(u64, 8), (try f.cursors()).change_floor);

    var buf: [8]owner_mod.Consumer = undefined;
    const consumers = f.owner.consumerList(&buf);
    try testing.expectEqual(@as(usize, 2), consumers.len);
    try testing.expectEqualStrings("compare", consumers[1].name);
    try testing.expectEqual(@as(u64, 8), consumers[1].position);
}

test "without a consumer the log keeps a fixed tail, and a dead consumer cannot hold it all" {
    const f = try Fixture.create(.{ .trim_seconds = 0, .retain_without_consumer = 5, .retain_at_most = 12 });
    defer f.destroy();
    try fill(f, 0, 20);
    _ = f.owner.turn(0);
    try testing.expectEqual(@as(u64, 16), (try f.cursors()).change_floor);

    f.owner.noteConsumer("bridge", 2);
    try fill(f, 20, 10);
    _ = f.owner.turn(0);
    try testing.expectEqual(@as(u64, 19), (try f.cursors()).change_floor);
    const store = try f.owner.acquire();
    defer f.owner.release();
    try testing.expect(try store.changes(f.arena.allocator(), 2, 10) == .trimmed);
}

test "the bridge's stored position is a consumer again after a restart" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    _ = try f.commit(&.{}, .{ .bridge = 7 });
    try f.restart();
    var buf: [8]owner_mod.Consumer = undefined;
    const consumers = f.owner.consumerList(&buf);
    try testing.expectEqual(@as(usize, 1), consumers.len);
    try testing.expectEqualStrings("bridge", consumers[0].name);
    try testing.expectEqual(@as(u64, 7), consumers[0].position);
}
