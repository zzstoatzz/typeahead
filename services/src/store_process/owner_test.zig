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

fn owed(f: *Fixture) ![]const []const u8 {
    const store = try f.owner.acquire();
    defer f.owner.release();
    return store.owedSweeps(f.arena.allocator());
}

test "a sweep a restart interrupts is owed, and the next process runs it from the top" {
    const f = try Fixture.create(.{ .sweep_scan = 1 });
    defer f.destroy();
    const carol = "did:plc:carol";
    _ = try f.commit(&.{
        .{ .profile = .{ .did = alice, .handle = "a.spam.example" } },
        .{ .profile = .{ .did = bob, .handle = "b.fine.example" } },
        .{ .profile = .{ .did = carol, .handle = "z.Spam.Example" } },
    }, null);
    try testing.expectEqual(@as(usize, 0), (try owed(f)).len);

    // The block and its mark land in one commit; the process dies with the
    // sweep part of the way down the handle range.
    _ = try f.commit(&.{.{ .domain_blocked = .{ .domain = "Spam.Example" } }}, null);
    try testing.expectEqualStrings("spam.example", (try owed(f))[0]);
    try testing.expect((try f.live(alice)).hidden);
    try testing.expect(!(try f.live(carol)).hidden);
    try testing.expectEqual(@as(usize, 1), f.owner.sweeps.items.len);
    try f.restart();

    try testing.expectEqual(@as(usize, 1), f.owner.sweeps.items.len);
    try testing.expectEqualStrings("spam.example", f.owner.sweeps.items[0].domain);
    while (f.owner.sweeps.items.len > 0) _ = f.owner.turn(0);
    try testing.expect((try f.live(alice)).hidden);
    try testing.expect(!(try f.live(bob)).hidden);
    try testing.expect((try f.live(carol)).hidden);
    try testing.expectEqual(@as(usize, 0), (try owed(f)).len);

    // Nothing is owed, so another start sweeps nothing.
    try f.restart();
    try testing.expectEqual(@as(usize, 0), f.owner.sweeps.items.len);

    // An unblock that is interrupted is owed the same way.
    _ = try f.commit(&.{.{ .domain_unblocked = "spam.example" }}, null);
    try testing.expect((try f.live(carol)).hidden);
    try f.restart();
    while (f.owner.sweeps.items.len > 0) _ = f.owner.turn(0);
    try testing.expect(!(try f.live(alice)).hidden);
    try testing.expect(!(try f.live(carol)).hidden);
    try testing.expectEqual(@as(usize, 0), (try owed(f)).len);
}

test "a block that changes while its sweep is under way starts the sweep over" {
    const f = try Fixture.create(.{ .sweep_scan = 1 });
    defer f.destroy();
    const carol = "did:plc:carol";
    _ = try f.commit(&.{
        .{ .profile = .{ .did = alice, .handle = "a.spam.example" } },
        .{ .profile = .{ .did = bob, .handle = "m.spam.example" } },
        .{ .profile = .{ .did = carol, .handle = "z.spam.example" } },
    }, null);
    _ = try f.commit(&.{.{ .domain_blocked = .{ .domain = "spam.example" } }}, null);
    try testing.expect((try f.live(alice)).hidden);
    try testing.expect(!(try f.live(carol)).hidden);

    // The unblock arrives with the first sweep past `a` and short of `z`.
    // One sweep finishing from where it stood would leave `a` hidden.
    _ = try f.commit(&.{.{ .domain_unblocked = "spam.example" }}, null);
    try testing.expectEqual(@as(usize, 1), f.owner.sweeps.items.len);
    while (f.owner.sweeps.items.len > 0) _ = f.owner.turn(0);
    for ([_][]const u8{ alice, bob, carol }) |did| try testing.expect(!(try f.live(did)).hidden);
    try testing.expectEqual(@as(usize, 0), (try owed(f)).len);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.domain_sweeps.load(.monotonic));
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

test "a restarted owner commits at once and scans nothing until startup_seconds have passed" {
    const f = try Fixture.create(.{ .startup_seconds = 30, .retain_without_consumer = 1 });
    defer f.destroy();
    _ = try f.commit(&.{.{ .observe = .{ .did = alice } }}, .{ .relay = 7 });
    _ = try f.commit(&.{.{ .gone = .{ .did = alice, .status = .deleted } }}, null);
    f.advance(f.owner.options.tombstone_retention_seconds + 60);
    try f.restart();

    const opened = f.owner.health();
    try testing.expectEqual(@as(u64, 0), opened.taken_at);
    try testing.expectEqual(@as(?u64, 7), opened.cursors.relay);
    try testing.expect(opened.tombstones == null);
    try testing.expect(opened.disk == null);

    _ = try f.commit(&.{.{ .observe = .{ .did = "did:plc:carol" } }}, .{ .relay = 8 });
    f.advance(29);
    _ = f.owner.turn(0);
    try testing.expectEqual(@as(?u64, 8), (try f.cursors()).relay);
    try testing.expect(try f.actor(alice) != null);
    try testing.expectEqual(@as(u64, 0), f.owner.counters.tombstones_pruned.load(.monotonic));
    try testing.expectEqual(@as(u64, 0), f.owner.counters.changes_trimmed.load(.monotonic));
    try testing.expectEqual(@as(u64, 0), f.owner.health().taken_at);

    f.advance(1);
    _ = f.owner.turn(0);
    try testing.expect(try f.actor(alice) == null);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.tombstones_pruned.load(.monotonic));
    try testing.expect(f.owner.counters.changes_trimmed.load(.monotonic) > 0);
    const counted = f.owner.health();
    try testing.expectEqual(f.now(), counted.taken_at);
    try testing.expect(counted.disk == null);
}

const rescore_created = "2023-04-27T00:00:00.000Z";
const rescore_dids = [_][]const u8{ "did:plc:a1", "did:plc:a2", "did:plc:a3", "did:plc:a4", "did:plc:a5" };

/// Five accounts holding a score under the old formula, the last one never
/// answered for (score 0).
fn seedOldScores(f: *Fixture) !void {
    var seeds: std.ArrayList(store_mod.Change) = .empty;
    const a = f.arena.allocator();
    for (rescore_dids, 0..) |did, i| {
        try seeds.append(a, .{ .profile = .{ .did = did, .handle = try std.fmt.allocPrint(a, "a{d}.test", .{i}) } });
        try seeds.append(a, .{ .enrichment = .{ .did = did, .source = .bsky, .created_at = rescore_created, .followers_count = 1000 * (i + 1), .follows_count = 10, .posts_count = 50_000, .quality_score = if (i == 4) 0 else 999 } });
    }
    _ = try f.commit(seeds.items, .{ .bridge = 1 });
}

fn newScore(f: *Fixture, i: usize) u32 {
    const authority = @import("authority_score.zig");
    const profile = @import("profile.zig");
    return @bitCast(authority.stored(authority.score(.{
        .followers = @floatFromInt(1000 * (i + 1)),
        .follows = 10,
        .created_ms = profile.dateMs(rescore_created),
        .now_ms = @intCast(f.now() * 1000),
    })));
}

test "the rescore pass waits for the bridge, then recomputes every stored score from its counts once" {
    const authority = @import("authority_score.zig");
    const f = try Fixture.create(.{ .rescore = true, .rescore_scan = 2, .rescore_max_lag = 0 });
    defer f.destroy();
    try seedOldScores(f);
    _ = f.owner.turn(0);
    try testing.expectEqual(@as(f32, 999), (try f.actor(rescore_dids[0])).?.quality_score);
    try testing.expectEqual(@as(u64, 0), f.owner.counters.rescore_examined.load(.monotonic));

    f.owner.options.rescore_max_lag = 1_000_000;
    // two actors a step, so the pass is part-way after one turn
    _ = f.owner.turn(0);
    try testing.expect((try f.cursors()).rescore_next != null);
    for (0..5) |_| _ = f.owner.turn(0);
    const done = try f.cursors();
    try testing.expectEqual(@as(?u64, authority.version), done.score_version);
    try testing.expectEqual(@as(?[]const u8, null), done.rescore_next);
    for (rescore_dids[0..4], 0..) |did, i| try testing.expectEqual(newScore(f, i), @as(u32, @bitCast((try f.actor(did)).?.quality_score)));
    try testing.expectEqual(@as(f32, 0), (try f.actor(rescore_dids[4])).?.quality_score);
    try testing.expectEqual(@as(u64, 4), f.owner.counters.rescore_changed.load(.monotonic));

    try f.restart();
    _ = f.owner.turn(0);
    try testing.expectEqual(@as(u64, 0), f.owner.counters.rescore_examined.load(.monotonic));
}

test "a restarted rescore pass resumes from its stored position" {
    const authority = @import("authority_score.zig");
    const f = try Fixture.create(.{ .rescore = true, .rescore_scan = 2, .rescore_max_lag = 0 });
    defer f.destroy();
    try seedOldScores(f);
    var buf: store_mod.keys.Buf = undefined;
    const next = try store_mod.keys.actor(&buf, rescore_dids[2]);
    _ = try f.commit(&.{}, .{ .rescore = .{ .version = authority.version, .next = next } });
    try f.restart();
    f.owner.options.rescore_max_lag = 1_000_000;
    for (0..5) |_| _ = f.owner.turn(0);
    try testing.expectEqual(@as(f32, 999), (try f.actor(rescore_dids[0])).?.quality_score);
    try testing.expectEqual(@as(f32, 999), (try f.actor(rescore_dids[1])).?.quality_score);
    try testing.expectEqual(newScore(f, 2), @as(u32, @bitCast((try f.actor(rescore_dids[2])).?.quality_score)));
    try testing.expectEqual(newScore(f, 3), @as(u32, @bitCast((try f.actor(rescore_dids[3])).?.quality_score)));
    try testing.expectEqual(@as(?u64, authority.version), (try f.cursors()).score_version);
}
