//! The producers against a scratch store: the relay intake, the identity
//! work, the PLC tail and the label sink, fed synthetic input.

const std = @import("std");
const zat = @import("zat");
const store_mod = @import("../store/root.zig");
const identity = @import("../identity.zig");
const label_tail = @import("../label_tail.zig");
const plc_tail = @import("../plc_tail.zig");
const identity_work = @import("identity_work.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const tails = @import("tails.zig");
const harness = @import("testing.zig");
const Fixture = harness.Fixture;
const Intake = intake_mod.Intake;
const Store = store_mod.Store;
const testing = std.testing;

const stamp = "2026-11-01T00:00:00.000Z";
const alice = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const bob = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";

fn didOf(arena: std.mem.Allocator, n: usize) ![]const u8 {
    return std.fmt.allocPrint(arena, "did:plc:{d:0>24}", .{n});
}

fn post(seq: i64, did: []const u8) zat.FirehoseEvent {
    return .{ .commit = .{ .seq = seq, .repo = did, .rev = "", .time = stamp, .ops = &.{
        .{ .action = .create, .collection = "app.bsky.feed.post", .rkey = "3k" },
    } } };
}

fn relayCursor(f: *Fixture) !?u64 {
    return (try f.cursors()).relay;
}

fn drain(f: *Fixture) void {
    while (f.owner.turn(0) > 0) {}
}

test "a full batch is handed over with the seq of its last whole event" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_changes = 3 } };
    defer intake.deinit();
    for (1..6) |n| intake.onEvent(post(@intCast(n), try didOf(f.arena.allocator(), n)));
    drain(f);
    try testing.expectEqual(@as(?u64, 3), try relayCursor(f));
    try testing.expect(try f.actor(try didOf(f.arena.allocator(), 3)) != null);
    try testing.expect(try f.actor(try didOf(f.arena.allocator(), 4)) == null);
    try testing.expectEqual(@as(i64, 3), intake.signals.handed_cursor.load(.monotonic));

    try intake.flush();
    drain(f);
    try testing.expectEqual(@as(?u64, 5), try relayCursor(f));
    try testing.expect(try f.actor(try didOf(f.arena.allocator(), 5)) != null);
    try testing.expectEqual(@as(u64, 2), intake.signals.batches.load(.monotonic));
}

test "a relay event is never split across commits" {
    const f = try Fixture.create(.{ .max_commit_changes = 1 });
    defer f.destroy();
    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_changes = 2 } };
    defer intake.deinit();
    intake.onEvent(.{ .account = .{ .seq = 100, .did = alice, .time = stamp, .active = true } });
    try testing.expectEqual(@as(usize, 1), f.owner.turn(0));
    try testing.expectEqual(@as(?u64, 100), try relayCursor(f));
    try testing.expect(try f.queued(alice) != null);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.commits.load(.monotonic));
}

test "a partial batch waits its interval, then goes with whatever it holds" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner };
    defer intake.deinit();
    intake.onEvent(post(1, alice));
    drain(f);
    try testing.expectEqual(@as(?u64, null), try relayCursor(f));
    f.advance(1);
    intake.onEvent(post(2, bob));
    drain(f);
    try testing.expectEqual(@as(?u64, 2), try relayCursor(f));
    _ = try f.live(alice);
    _ = try f.live(bob);
}

test "events that change nothing still carry the cursor forward" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_age_seconds = 0 } };
    defer intake.deinit();
    intake.onEvent(.{ .commit = .{ .seq = 9, .repo = alice, .rev = "", .time = stamp, .ops = &.{
        .{ .action = .create, .collection = "app.bsky.feed.repost", .rkey = "3k" },
    } } });
    drain(f);
    try testing.expectEqual(@as(?u64, 9), try relayCursor(f));
    try testing.expect(try f.actor(alice) == null);
    try testing.expectEqual(@as(i64, 1_793_491_200), intake.signals.source_event_at.load(.monotonic));
}

test "a restart resumes from the stored cursor and a replay changes nothing" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    {
        var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_changes = 4 } };
        defer intake.deinit();
        for (1..11) |n| intake.onEvent(post(@intCast(n), try didOf(f.arena.allocator(), n)));
        drain(f);
    }
    try f.restart();
    const stored = (try relayCursor(f)).?;
    try testing.expectEqual(@as(u64, 8), stored);
    const logged = (try f.cursors()).next_change;

    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_changes = 4 } };
    defer intake.deinit();
    intake.resumeFrom(@intCast(stored));
    for (5..11) |n| intake.onEvent(post(@intCast(n), try didOf(f.arena.allocator(), n)));
    try intake.flush();
    drain(f);
    try testing.expectEqual(@as(?u64, 10), try relayCursor(f));
    try testing.expectEqual(logged + 2, (try f.cursors()).next_change);
    try testing.expectEqual(@as(u64, 2), f.owner.counters.actors_created.load(.monotonic));
}

var fatal_calls: usize = 0;

fn countFatal() void {
    fatal_calls += 1;
}

test "an event that cannot be handed over stops intake before the cursor passes it" {
    fatal_calls = 0;
    const f = try Fixture.create(.{});
    defer f.destroy();
    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_changes = 1, .on_fatal = countFatal } };
    defer intake.deinit();
    f.owner.stop();
    intake.onEvent(post(1, alice));
    intake.onEvent(post(2, bob));
    try testing.expectEqual(@as(usize, 1), fatal_calls);
    try testing.expect(intake.shouldStop());
    try testing.expectEqual(@as(i64, 0), intake.signals.handed_cursor.load(.monotonic));
}

const Running = harness.Running;

const Gate = struct {
    stalled: std.atomic.Value(bool) = .init(false),

    fn step(context: ?*anyopaque, store: *Store) anyerror!store_mod.maintain.Pressure {
        const self: *Gate = @ptrCast(@alignCast(context.?));
        _ = try store.maintainStep();
        return if (self.stalled.load(.monotonic)) .stalled else .none;
    }
};

fn until(comptime what: []const u8, context: anytype, comptime ready: fn (@TypeOf(context)) bool) !void {
    var spins: usize = 0;
    while (!ready(context)) : (spins += 1) {
        if (spins > 20_000) {
            std.debug.print("timed out waiting until {s}\n", .{what});
            return error.TestTimedOut;
        }
        try testing.io.sleep(.fromMilliseconds(1), .awake);
    }
}

const Feed = struct {
    intake: *Intake,
    arena: std.mem.Allocator,
    count: usize,

    fn run(self: *Feed) void {
        for (1..self.count + 1) |n| self.intake.onEvent(post(@intCast(n), didOf(self.arena, n) catch return));
        self.intake.flush() catch {};
    }
};

test "a stalled store pauses intake, the pause is not a wedge, and intake resumes" {
    // The worker role's equivalent is ingest.zig "a slow backlog drain does
    // not look like a stalled relay" (2026-11-09).
    var gate: Gate = .{};
    const f = try Fixture.create(.{ .max_queued_changes = 8, .idle_wait_ms = 1, .maintenance = .{ .context = &gate, .step = Gate.step } });
    defer f.destroy();
    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_changes = 4 } };
    defer intake.deinit();
    intake.resumeFrom(0);

    gate.stalled.store(true, .monotonic);
    var running = try Running.start(f);
    defer running.stop();
    try until("the owner sees the stall", f, struct {
        fn ready(fixture: *Fixture) bool {
            return fixture.owner.counters.stalled.load(.monotonic);
        }
    }.ready);

    var feed_arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer feed_arena.deinit();
    var feed: Feed = .{ .intake = &intake, .arena = feed_arena.allocator(), .count = 200 };
    var feeding: ?std.Thread = try std.Thread.spawn(.{}, Feed.run, .{&feed});
    defer if (feeding) |thread| {
        running.stop();
        thread.join();
    };

    try until("intake pauses", &intake, struct {
        fn ready(i: *Intake) bool {
            return i.signals.paused.load(.monotonic);
        }
    }.ready);
    try testing.expect(f.owner.counters.stalled.load(.monotonic));
    const committed = f.owner.counters.commits.load(.monotonic);
    const handed = intake.signals.handed_cursor.load(.monotonic);
    try testing.expect(handed < 200);

    f.advance(2 * intake_mod.stall_seconds);
    try until("the owner turns under the new time", f, struct {
        fn ready(fixture: *Fixture) bool {
            return fixture.owner.counters.last_turn_at.load(.monotonic) == fixture.now();
        }
    }.ready);
    const last_event = intake.signals.last_event_at.load(.monotonic);
    try testing.expect(f.now() - last_event >= intake_mod.stall_seconds);
    try testing.expectEqual(@as(?intake_mod.Wedge, null), intake_mod.wedged(f.now(), last_event, intake.signals.paused.load(.monotonic), f.owner.counters.last_turn_at.load(.monotonic)));
    try testing.expectEqual(@as(?intake_mod.Wedge, .relay_silent), intake_mod.wedged(f.now(), last_event, false, f.owner.counters.last_turn_at.load(.monotonic)));
    try testing.expectEqual(committed, f.owner.counters.commits.load(.monotonic));
    try testing.expectEqual(handed, intake.signals.handed_cursor.load(.monotonic));

    gate.stalled.store(false, .monotonic);
    feeding.?.join();
    feeding = null;
    const barrier = try owner_mod.Submission.create(testing.allocator);
    defer barrier.destroy();
    try f.owner.call(barrier);
    running.stop();

    try testing.expect(!intake.signals.paused.load(.monotonic));
    try testing.expectEqual(f.now(), intake.signals.last_event_at.load(.monotonic));
    try testing.expectEqual(@as(?intake_mod.Wedge, null), intake_mod.wedged(f.now(), intake.signals.last_event_at.load(.monotonic), false, f.owner.counters.last_turn_at.load(.monotonic)));
    try testing.expectEqual(@as(?u64, 200), try relayCursor(f));
    try testing.expectEqual(@as(u64, 200), f.owner.counters.actors_created.load(.monotonic));
    try testing.expect(f.owner.counters.posts_blocked.load(.monotonic) > 0);
    try testing.expect(f.owner.counters.stall_turns.load(.monotonic) > 0);
}

test "an owner that stops turning, or a silent relay, is a wedge" {
    const now: u64 = harness.t0;
    try testing.expectEqual(@as(?intake_mod.Wedge, null), intake_mod.wedged(now, now - 599, false, now - 599));
    try testing.expectEqual(@as(?intake_mod.Wedge, .relay_silent), intake_mod.wedged(now, now - 600, false, now));
    try testing.expectEqual(@as(?intake_mod.Wedge, .owner_stuck), intake_mod.wedged(now, now, true, now - 600));
    try testing.expectEqual(@as(?intake_mod.Wedge, null), intake_mod.wedged(now, now - 6000, true, now));
}

test "leased identity work is resolved against the store's own queue" {
    const f = try Fixture.create(.{ .idle_wait_ms = 1 });
    defer f.destroy();
    var intake: Intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_age_seconds = 0 } };
    defer intake.deinit();
    var running = try Running.start(f);
    defer running.stop();
    intake.onEvent(.{ .identity = .{ .seq = 1, .did = alice, .time = stamp } });
    intake.onEvent(.{ .identity = .{ .seq = 2, .did = bob, .time = stamp } });

    var work: identity_work.Work = .{ .gpa = testing.allocator, .io = testing.io, .owner = f.owner, .resolver = undefined };
    var jobs: [identity.LEASE_BATCH]identity.Job = undefined;
    const n = try work.lease(&jobs);
    try testing.expectEqual(@as(usize, 2), n);
    try testing.expectEqualStrings(alice, jobs[0].did.slice());
    try testing.expectEqual(@as(u32, 1), jobs[0].attempts);
    try testing.expectEqual(@as(usize, 0), try work.lease(&jobs));

    try work.report(jobs[0], .{ .observed_at = @intCast(f.now()), .outcome = identity.decide(.{ .handle = "alice.example", .pds = "https://pds.example", .verified = true }, 1) });
    try work.report(jobs[1], .{ .observed_at = @intCast(f.now()), .outcome = identity.decide(.{ .handle = "bob.example", .pds = "https://pds.example", .verified = false }, 1) });
    running.stop();

    try testing.expectEqualStrings("alice.example", (try f.live(alice)).handle);
    try testing.expect(try f.queued(alice) == null);
    const pending = try f.live(bob);
    try testing.expectEqualStrings("", pending.handle);
    try testing.expectEqualStrings("https://pds.example", pending.pds);
    try testing.expectEqual(f.now() + 60, pending.identity_due);
    try testing.expectEqualStrings("handle_unverified", (try f.queued(bob)).?.last_error);
}

test "a PLC export page enqueues held accounts and stores its cursor with them" {
    const f = try Fixture.create(.{ .idle_wait_ms = 1 });
    defer f.destroy();
    _ = try f.commit(&.{.{ .observe = .{ .did = alice } }}, null);
    var running = try Running.start(f);
    defer running.stop();
    var tail: tails.PlcTail = .{ .gpa = testing.allocator, .io = testing.io, .owner = f.owner };
    var cursor: std.ArrayList(u8) = .empty;
    defer cursor.deinit(testing.allocator);
    try tail.startCursor(&cursor);
    var buf: [24]u8 = undefined;
    try testing.expectEqualStrings(tails.isoSeconds(&buf, f.now() - 300), cursor.items);

    const body =
        \\{"did":"did:plc:aaaaaaaaaaaaaaaaaaaaaaaa","createdAt":"2026-11-06T21:16:00.001Z","operation":{}}
        \\{"did":"did:plc:bbbbbbbbbbbbbbbbbbbbbbbb","createdAt":"2026-11-06T21:16:00.002Z"}
        \\{"did":"did:plc:aaaaaaaaaaaaaaaaaaaaaaaa","createdAt":"2026-11-06T21:16:00.003Z"}
        \\
    ;
    const page = try plc_tail.parsePage(f.arena.allocator(), body, cursor.items);
    try tail.apply(page);
    try plc_tail.advance(testing.allocator, &cursor, page);
    try tail.apply(try plc_tail.parsePage(f.arena.allocator(), "", cursor.items));
    running.stop();

    try testing.expectEqualStrings("2026-11-06T21:16:00.003Z", cursor.items);
    try testing.expectEqualStrings("2026-11-06T21:16:00.003Z", (try f.cursors()).plc.?);
    try testing.expectEqual(store_mod.record.IdentityReason.plc, (try f.queued(alice)).?.reason);
    try testing.expect(try f.actor(bob) == null);
    try tail.startCursor(&cursor);
    try testing.expectEqualStrings("2026-11-06T21:16:00.003Z", cursor.items);
}

test "the bootstrap timestamp has the export's form" {
    var buf: [24]u8 = undefined;
    try testing.expectEqualStrings("2026-11-11T00:00:00.000Z", tails.isoSeconds(&buf, 1_794_355_200));
    try testing.expectEqualStrings("2024-02-29T12:34:56.000Z", tails.isoSeconds(&buf, 1_709_210_096));
}

test "label frames land in the store with the stream cursor they reach" {
    const f = try Fixture.create(.{ .idle_wait_ms = 1 });
    defer f.destroy();
    _ = try f.commit(&.{.{ .observe = .{ .did = alice } }}, null);
    var running = try Running.start(f);
    defer running.stop();
    var sink: tails.LabelSink = .{ .gpa = testing.allocator, .owner = f.owner };
    try testing.expectEqual(@as(?i64, null), try sink.cursor());

    var handler: label_tail.Handler(*tails.LabelSink) = .{ .allocator = testing.allocator, .io = testing.io, .sink = &sink, .client = null, .committed = null };
    defer handler.pending.deinit(testing.allocator);
    defer handler.arena.deinit();
    try handler.onLabels(&.{
        .{ .src = label_tail.ADOPTED_SOURCE, .uri = alice, .val = "!takedown", .cts = stamp },
        .{ .src = label_tail.ADOPTED_SOURCE, .uri = "at://" ++ alice ++ "/app.bsky.feed.post/3k", .val = "spam", .cts = stamp },
        .{ .src = label_tail.ADOPTED_SOURCE, .uri = bob, .val = "spam", .cts = "not a time" },
    }, 41);
    try handler.onLabels(&.{}, 42);
    try handler.flush(42);
    running.stop();

    try testing.expectEqual(@as(?i64, 42), handler.committed);
    try testing.expectEqual(@as(?i64, 42), try sink.cursor());
    const actor = try f.live(alice);
    try testing.expect(actor.hidden);
    try testing.expectEqual(@as(usize, 1), actor.labels.len);
    try testing.expectEqual(@as(u64, 1), sink.ignored.load(.monotonic));
    try testing.expectEqual(@as(usize, 0), handler.pending.items.len);
}
