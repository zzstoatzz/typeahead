//! The Worker's write-path cases, asserted on store state. Each test names
//! the case it mirrors: `ingest.zig`, `src/handlers/ingest.test.ts`,
//! `src/identity-queue.test.ts` or `src/account-labels.test.ts`.

const std = @import("std");
const zat = @import("zat");
const store_mod = @import("../store/root.zig");
const identity = @import("../identity.zig");
const label_tail = @import("../label_tail.zig");
const events = @import("events.zig");
const harness = @import("testing.zig");
const Fixture = harness.Fixture;
const Change = store_mod.Change;
const testing = std.testing;

const mod = label_tail.ADOPTED_SOURCE;
const alice = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const bob = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";
const carol = "did:plc:cccccccccccccccccccccccc";
const stamp = "2026-11-01T00:00:00.000Z";

fn post(did: []const u8) zat.FirehoseEvent {
    return .{ .commit = .{ .seq = 0, .repo = did, .rev = "", .time = stamp, .ops = &.{
        .{ .action = .create, .collection = "app.bsky.feed.post", .rkey = "3k" },
    } } };
}

fn profile(f: *Fixture, did: []const u8, record: zat.cbor.Value) !zat.FirehoseEvent {
    const ops = try f.arena.allocator().dupe(zat.firehose.RepoOp, &.{
        .{ .action = .update, .collection = "app.bsky.actor.profile", .rkey = "self", .record = record },
    });
    return .{ .commit = .{ .seq = 0, .repo = did, .rev = "", .time = stamp, .ops = ops } };
}

const named: zat.cbor.Value = .{ .map = &.{
    .{ .key = "displayName", .value = .{ .text = "New" } },
    .{ .key = "description", .value = .{ .text = "about me" } },
} };

fn signal(did: []const u8) zat.FirehoseEvent {
    return .{ .identity = .{ .seq = 0, .did = did, .time = stamp } };
}

fn account(did: []const u8, active: bool, status: ?zat.AccountStatus) zat.FirehoseEvent {
    return .{ .account = .{ .seq = 0, .did = did, .time = stamp, .active = active, .status = status } };
}

/// Commit the given relay events as one batch ending at `seq`.
fn hear(f: *Fixture, seq: u64, heard: []const zat.FirehoseEvent) !void {
    var changes = f.changes();
    for (heard) |event| try changes.firehose(event);
    _ = try f.commit(changes.items(), .{ .relay = seq });
}

fn lease(f: *Fixture, limit: usize) ![]const store_mod.change.Job {
    return (try f.commit(&.{.{ .identity_lease = .{ .limit = limit } }}, null)).jobs;
}

fn jobOf(leased: store_mod.change.Job) identity.Job {
    return .{ .did = identity.Did.fromSlice(leased.did).?, .gen = leased.generation, .attempts = @intCast(leased.attempts) };
}

fn settle(f: *Fixture, leased: store_mod.change.Job, observed_at: u64, outcome: identity.Outcome) !void {
    const job = jobOf(leased);
    _ = try f.commit(&.{events.completion(&job, .{ .observed_at = @intCast(observed_at), .outcome = outcome })}, null);
}

fn frame(uri: []const u8, val: []const u8, seq: i64) label_tail.Wire {
    return .{ .src = mod, .uri = uri, .val = val, .cts = stamp, .seq = seq };
}

fn stream(f: *Fixture, wires: []const label_tail.Wire, cursor: u64) !usize {
    var changes = f.changes();
    const ignored = try changes.labels(wires);
    _ = try f.commit(changes.items(), .{ .labeler = cursor });
    return ignored;
}

fn expectLabels(f: *Fixture, did: []const u8, expected: []const []const u8) !void {
    const actor = (try f.actor(did)).?;
    try testing.expectEqual(expected.len, actor.labels.len);
    for (expected, actor.labels) |want, got| {
        const kind = if (std.mem.eql(u8, got.source, mod)) "mod" else "self";
        const subject = if (std.mem.eql(u8, got.uri, did)) "account" else "profile";
        const text = try std.fmt.allocPrint(f.arena.allocator(), "{s}:{s}:{s}", .{ kind, subject, got.value });
        try testing.expectEqualStrings(want, text);
    }
}

fn logLength(f: *Fixture) !u64 {
    return (try f.cursors()).next_change - 1;
}

// -- ingest.zig and src/handlers/ingest.test.ts --

test "counts all new actors, bare and profile-bearing, and none that exist" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{post(carol)});
    try testing.expectEqual(@as(u64, 1), f.owner.counters.actors_created.load(.monotonic));

    try hear(f, 2, &.{ post(alice), try profile(f, bob, named), try profile(f, carol, named) });
    try testing.expectEqual(@as(u64, 3), f.owner.counters.actors_created.load(.monotonic));
    try testing.expectEqualStrings("New", (try f.live(bob)).display_name);
    try testing.expectEqualStrings("New", (try f.live(carol)).display_name);

    try hear(f, 3, &.{ post(alice), try profile(f, bob, named) });
    try testing.expectEqual(@as(u64, 3), f.owner.counters.actors_created.load(.monotonic));
}

test "a profile keeps stored fields it does not carry" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{try profile(f, alice, named)});
    try hear(f, 2, &.{try profile(f, alice, .{ .map = &.{.{ .key = "displayName", .value = .{ .text = "Renamed" } }} })});
    const actor = try f.live(alice);
    try testing.expectEqualStrings("Renamed", actor.display_name);
    try testing.expectEqualStrings("about me", actor.description);
}

test "a profile with nothing we keep is a bare sighting" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{try profile(f, alice, .{ .map = &.{.{ .key = "pronouns", .value = .{ .text = "they" } }} })});
    const actor = try f.live(alice);
    try testing.expectEqualStrings("", actor.display_name);
    try testing.expectEqual(harness.t0, actor.last_activity_at);
    try testing.expectEqual(store_mod.record.IdentityReason.discovered, (try f.queued(alice)).?.reason);
}

test "records outside the allowlist and deletes discover nobody" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{
        .{ .commit = .{ .seq = 0, .repo = alice, .rev = "", .time = stamp, .ops = &.{
            .{ .action = .create, .collection = "app.bsky.feed.repost", .rkey = "3k" },
            .{ .action = .delete, .collection = "app.bsky.feed.post", .rkey = "3k" },
            .{ .action = .delete, .collection = "app.bsky.actor.profile", .rkey = "self" },
        } } },
        .{ .sync = .{ .seq = 0, .did = bob, .rev = "", .time = stamp, .blocks = "" } },
    });
    try testing.expect(try f.actor(alice) == null);
    try testing.expect(try f.actor(bob) == null);
    try testing.expectEqual(@as(?u64, 1), (try f.cursors()).relay);
}

test "activity is stamped at most hourly, whatever the event" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{post(alice)});
    f.advance(1800);
    try hear(f, 2, &.{ post(alice), signal(alice), account(alice, true, null) });
    try testing.expectEqual(harness.t0, (try f.live(alice)).last_activity_at);
    f.advance(1801);
    try hear(f, 3, &.{signal(alice)});
    try testing.expectEqual(harness.t0 + 3601, (try f.live(alice)).last_activity_at);
}

test "#identity rides the cursor-protected batch as durable identity work" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 7, &.{signal(alice)});
    try testing.expectEqual(@as(?u64, 7), (try f.cursors()).relay);
    const state = (try f.queued(alice)).?;
    try testing.expectEqual(store_mod.record.IdentityReason.identity, state.reason);

    try f.restart();
    try testing.expectEqual(@as(?u64, 7), (try f.cursors()).relay);
    try testing.expect(try f.queued(alice) != null);
}

test "an #identity event's handle is never written, valid or not" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{
        .{ .identity = .{ .seq = 0, .did = alice, .time = stamp, .handle = "example.bsky.social" } },
        .{ .identity = .{ .seq = 0, .did = bob, .time = stamp, .handle = "handle.invalid" } },
    });
    try testing.expectEqualStrings("", (try f.live(alice)).handle);
    try testing.expectEqualStrings("", (try f.live(bob)).handle);
    try testing.expect(try f.queued(alice) != null and try f.queued(bob) != null);
}

test "a deactivated or deleted account is gone, with its cursor, in one commit" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 10, &.{ post(alice), post(bob), post(carol) });
    try hear(f, 12, &.{ account(alice, false, .deactivated), account(bob, false, .deleted), account(carol, false, .takendown) });
    try testing.expectEqual(store_mod.record.Status.deactivated, (try f.actor(alice)).?.status);
    try testing.expectEqual(store_mod.record.Status.deleted, (try f.actor(bob)).?.status);
    try testing.expectEqual(store_mod.record.Status.deactivated, (try f.actor(carol)).?.status);
    try testing.expectEqual(@as(?u64, 12), (try f.cursors()).relay);
    try testing.expectEqual(@as(u64, 3), f.owner.counters.actors_gone.load(.monotonic));
    try testing.expect(try f.queued(alice) == null);
}

test "an inactive account we never saw leaves nothing behind" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{account(alice, false, .deleted)});
    try testing.expect(try f.actor(alice) == null);
}

test "a reactivated account is restored and its deactivation in the same batch does not win" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{ post(alice), post(bob) });
    try hear(f, 3, &.{ account(alice, false, .deactivated), account(bob, false, .deactivated), account(alice, true, null) });
    try testing.expect((try f.actor(alice)).?.live());
    try testing.expect(!(try f.actor(bob)).?.live());
    try testing.expectEqual(store_mod.record.IdentityReason.account, (try f.queued(alice)).?.reason);
}

test "an active #account for an unknown account creates and queues it" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{account(alice, true, null)});
    _ = try f.live(alice);
    try testing.expectEqual(store_mod.record.IdentityReason.account, (try f.queued(alice)).?.reason);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.actors_created.load(.monotonic));
}

test "a gone account ignores what it emits until it is reactivated" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{try profile(f, alice, named)});
    try hear(f, 2, &.{account(alice, false, .deactivated)});
    try hear(f, 3, &.{ post(alice), signal(alice), try profile(f, alice, .{ .map = &.{.{ .key = "displayName", .value = .{ .text = "Ghost" } }} }) });
    const gone = (try f.actor(alice)).?;
    try testing.expect(!gone.live());
    try testing.expectEqualStrings("New", gone.display_name);
    try testing.expect(try f.queued(alice) == null);

    try hear(f, 4, &.{account(alice, true, null)});
    try testing.expectEqualStrings("New", (try f.live(alice)).display_name);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.actors_reactivated.load(.monotonic));
}

// -- src/identity-queue.test.ts --

test "#identity signals coalesce into one entry per DID and bump its generation" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    try hear(f, 2, &.{ signal(alice), account(bob, true, null) });
    const a = (try f.queued(alice)).?;
    try testing.expectEqual(@as(u64, 2), a.generation);
    try testing.expectEqual(store_mod.record.IdentityReason.identity, a.reason);
    const b = (try f.queued(bob)).?;
    try testing.expectEqual(@as(u64, 1), b.generation);
    try testing.expectEqual(store_mod.record.IdentityReason.account, b.reason);
}

test "discovery enqueues only actors this batch created" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{post(alice)});
    const jobs = try lease(f, 10);
    try settle(f, jobs[0], f.now(), .{ .resolved = .{} });
    try testing.expect(try f.queued(alice) == null);

    try hear(f, 2, &.{ post(alice), try profile(f, bob, named) });
    try testing.expect(try f.queued(alice) == null);
    try testing.expectEqual(store_mod.record.IdentityReason.discovered, (try f.queued(bob)).?.reason);
}

test "discovery never resets a pending entry's backoff" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    const jobs = try lease(f, 10);
    try settle(f, jobs[0], f.now(), .{ .retry = .{ .delay_seconds = 3600, .@"error" = "did_unavailable" } });
    const before = (try f.actor(alice)).?.identity_due;
    try hear(f, 2, &.{ post(alice), try profile(f, alice, named) });
    try testing.expectEqual(before, (try f.actor(alice)).?.identity_due);
    try testing.expectEqual(@as(u64, 1), (try f.queued(alice)).?.attempts);
    try testing.expect(before > f.now() + 3000);
}

test "a lease hides its work until it lapses, so a crashed resolver's work returns" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{ signal(alice), signal(bob) });
    const first = try lease(f, 10);
    try testing.expectEqual(@as(usize, 2), first.len);
    try testing.expectEqualStrings(alice, first[0].did);
    try testing.expectEqual(@as(u64, 1), first[0].attempts);
    try testing.expectEqual(@as(usize, 0), (try lease(f, 10)).len);

    try f.restart();
    f.advance(300);
    const again = try lease(f, 1);
    try testing.expectEqual(@as(usize, 1), again.len);
    try testing.expectEqualStrings(alice, again[0].did);
    try testing.expectEqual(@as(u64, 2), again[0].attempts);
}

test "a resolved result writes the identity and settles its generation" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    const jobs = try lease(f, 10);
    try settle(f, jobs[0], f.now(), .{ .resolved = .{ .handle = "alice.example", .handle_verified = true, .pds = "https://pds.example" } });
    const actor = try f.live(alice);
    try testing.expectEqualStrings("alice.example", actor.handle);
    try testing.expectEqualStrings("https://pds.example", actor.pds);
    try testing.expectEqual(f.now(), actor.identity_resolved_at);
    try testing.expect(try f.queued(alice) == null);
    const store = try f.owner.acquire();
    defer f.owner.release();
    try testing.expectEqualStrings(alice, (try store.byHandle(f.arena.allocator(), "ALICE.example"))[0]);
}

test "a signal that arrives during resolution survives the older completion" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    const jobs = try lease(f, 10);
    try hear(f, 2, &.{signal(alice)});
    try settle(f, jobs[0], f.now(), .{ .resolved = .{ .handle = "old.example", .handle_verified = true } });
    const state = (try f.queued(alice)).?;
    try testing.expectEqual(jobs[0].generation + 1, state.generation);
    try testing.expect((try f.actor(alice)).?.identity_due <= f.now());
    try testing.expectEqualStrings("old.example", (try f.live(alice)).handle);
}

test "an older observation never overwrites a newer one" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    const newer = f.now();
    try settle(f, (try lease(f, 10))[0], newer, .{ .resolved = .{ .pds = "https://new.example" } });
    try hear(f, 2, &.{signal(alice)});
    try settle(f, (try lease(f, 10))[0], newer - 60, .{ .resolved = .{ .pds = "https://old.example" } });
    const actor = try f.live(alice);
    try testing.expectEqualStrings("", actor.handle);
    try testing.expectEqualStrings("https://new.example", actor.pds);
    try testing.expectEqual(newer, actor.identity_resolved_at);
    try testing.expect(try f.queued(alice) == null);
}

test "unverified and quarantined handles are not written; handle.invalid is" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{ signal(alice), signal(bob) });
    var jobs = try lease(f, 10);
    try settle(f, jobs[0], f.now(), .{ .resolved = .{ .handle = "real.example", .handle_verified = true } });
    try settle(f, jobs[1], f.now(), .{ .resolved = .{ .handle = "donald.trump.com", .handle_verified = true } });
    try testing.expectEqualStrings("", (try f.live(bob)).handle);

    try hear(f, 2, &.{signal(alice)});
    jobs = try lease(f, 10);
    try settle(f, jobs[0], f.now(), .{ .resolved = .{ .handle = "claimed.example", .handle_verified = false } });
    try testing.expectEqualStrings("real.example", (try f.live(alice)).handle);

    try hear(f, 3, &.{signal(alice)});
    jobs = try lease(f, 10);
    try settle(f, jobs[0], f.now(), .{ .resolved = .{ .handle = identity.INVALID_HANDLE } });
    try testing.expectEqualStrings("handle.invalid", (try f.live(alice)).handle);
}

test "a new verified handle replaces the old one; a sighting keeps it" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    try settle(f, (try lease(f, 10))[0], f.now(), .{ .resolved = .{ .handle = "moltrust.bsky.social", .handle_verified = true } });
    try hear(f, 2, &.{signal(alice)});
    try settle(f, (try lease(f, 10))[0], f.now(), .{ .resolved = .{ .handle = "moltrust.ch", .handle_verified = true } });
    try testing.expectEqualStrings("moltrust.ch", (try f.live(alice)).handle);
    try hear(f, 3, &.{post(alice)});
    try testing.expectEqualStrings("moltrust.ch", (try f.live(alice)).handle);
    const store = try f.owner.acquire();
    defer f.owner.release();
    try testing.expectEqual(@as(usize, 0), (try store.byHandle(f.arena.allocator(), "moltrust.bsky.social")).len);
}

test "a PDS migration replaces the old host and a lookup without one preserves it" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    for ([_][]const u8{ "https://old.example", "https://new.example" }, 1..) |pds, seq| {
        try hear(f, seq, &.{signal(alice)});
        f.advance(1);
        try settle(f, (try lease(f, 10))[0], f.now(), .{ .resolved = .{ .pds = pds } });
        try testing.expectEqualStrings(pds, (try f.live(alice)).pds);
    }
    try hear(f, 3, &.{ signal(alice), try profile(f, alice, named) });
    f.advance(1);
    try settle(f, (try lease(f, 10))[0], f.now(), .{ .resolved = .{} });
    try testing.expectEqualStrings("https://new.example", (try f.live(alice)).pds);
}

test "a retry keeps the entry, records the PDS it learned, and clamps the delay" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    try settle(f, (try lease(f, 10))[0], f.now(), .{ .retry = .{ .pds = "https://pds.example", .delay_seconds = 10 * 86_400, .@"error" = "handle_unverified" } });
    try testing.expectEqualStrings("handle_unverified", (try f.queued(alice)).?.last_error);
    try testing.expectEqual(f.now() + 86_400, (try f.actor(alice)).?.identity_due);
    try testing.expectEqualStrings("https://pds.example", (try f.live(alice)).pds);
}

test "work that keeps failing is abandoned after its attempt budget" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    var attempts: u64 = 0;
    while (try f.queued(alice) != null) {
        const jobs = try lease(f, 10);
        try testing.expectEqual(@as(usize, 1), jobs.len);
        attempts = jobs[0].attempts;
        try settle(f, jobs[0], f.now(), identity.decide(null, @intCast(attempts)));
        f.advance(86_400);
    }
    try testing.expectEqual(@as(u64, store_mod.change.max_attempts), attempts);
    _ = try f.live(alice);
}

test "a result the key layout cannot hold is refused without writing anything" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{signal(alice)});
    const job = (try lease(f, 10))[0];
    const long: [254]u8 = @splat('a');
    const result = try f.commit(&.{.{ .identity_complete = .{ .did = alice, .generation = job.generation, .observed_at = f.now(), .outcome = .{ .resolved = .{ .handle = &long } } } }}, null);
    try testing.expectEqual(@as(u32, 1), result.summary.rejected);
    try testing.expect(try f.queued(alice) != null);
    try testing.expectEqualStrings("", (try f.live(alice)).handle);
    try testing.expectEqual(@as(u64, 1), f.owner.counters.changes_rejected.load(.monotonic));
}

test "PLC operations re-request resolution only for accounts already held" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    try hear(f, 1, &.{post(alice)});
    try settle(f, (try lease(f, 10))[0], f.now(), .{ .resolved = .{} });
    var changes = f.changes();
    try changes.plc(&.{ alice, bob, alice });
    try testing.expectEqual(@as(usize, 2), changes.items().len);
    _ = try f.commit(changes.items(), .{ .plc = "2026-10-31T00:00:00.000Z" });
    try testing.expectEqual(store_mod.record.IdentityReason.plc, (try f.queued(alice)).?.reason);
    try testing.expect(try f.actor(bob) == null);
    try testing.expectEqualStrings("2026-10-31T00:00:00.000Z", (try f.cursors()).plc.?);
}

test "the PLC cursor only moves forward" {
    const f = try Fixture.create(.{});
    defer f.destroy();
    _ = try f.commit(&.{}, .{ .plc = "2026-10-31T12:00:00.000Z" });
    _ = try f.commit(&.{}, .{ .plc = "2026-10-31T11:00:00.000Z" });
    try testing.expectEqualStrings("2026-10-31T12:00:00.000Z", (try f.cursors()).plc.?);
    _ = try f.commit(&.{}, .{ .plc = "2026-10-31T13:00:00.000Z" });
    try testing.expectEqualStrings("2026-10-31T13:00:00.000Z", (try f.cursors()).plc.?);
}

test "the health snapshot reads zero when nothing is pending" {
    const f = try Fixture.create(.{ .health_seconds = 0 });
    defer f.destroy();
    try testing.expectEqual(@as(u64, 0), f.owner.health().identity_pending);
    try hear(f, 1, &.{ signal(alice), signal(bob) });
    _ = try lease(f, 1);
    _ = f.owner.turn(0);
    const health = f.owner.health();
    try testing.expectEqual(@as(u64, 2), health.identity_pending);
    try testing.expectEqual(@as(u64, 1), health.identity_due);
    try testing.expectEqual(@as(?u64, 1), health.cursors.relay);
}

// -- src/account-labels.test.ts --

const self_label: store_mod.record.Label = .{ .source = alice, .uri = "at://" ++ alice ++ "/app.bsky.actor.profile/self", .value = "!no-unauthenticated", .created = stamp };

/// Alice and Bob are held, Alice with a self-label, as the Worker's fixture has them.
fn labelled() !*Fixture {
    const f = try Fixture.create(.{});
    errdefer f.destroy();
    try hear(f, 1, &.{ post(alice), post(bob) });
    _ = try f.commit(&.{.{ .enrichment = .{ .did = alice, .source = .bsky, .labels = &.{self_label} } }}, null);
    return f;
}

test "a stream label hides the account and keeps the account's self-labels" {
    const f = try labelled();
    defer f.destroy();
    try testing.expectEqual(@as(usize, 0), try stream(f, &.{frame(alice, "spam", 10)}, 10));
    try expectLabels(f, alice, &.{ "self:profile:!no-unauthenticated", "mod:account:spam" });
    try testing.expect((try f.live(alice)).hidden);
    try testing.expectEqual(@as(?u64, 10), (try f.cursors()).labeler);
}

test "a negation withdraws the label and a delayed older assertion cannot restore it" {
    const f = try labelled();
    defer f.destroy();
    _ = try stream(f, &.{frame(alice, "!hide", 10)}, 10);
    var negation = frame(alice, "!hide", 12);
    negation.neg = true;
    _ = try stream(f, &.{negation}, 12);
    try expectLabels(f, alice, &.{"self:profile:!no-unauthenticated"});
    try testing.expect(!(try f.live(alice)).hidden);

    _ = try stream(f, &.{frame(alice, "!hide", 11)}, 11);
    try expectLabels(f, alice, &.{"self:profile:!no-unauthenticated"});
    try testing.expect(!(try f.live(alice)).hidden);
    try testing.expectEqual(@as(?u64, 12), (try f.cursors()).labeler);
}

test "within one batch the higher seq wins whatever the order" {
    const f = try labelled();
    defer f.destroy();
    var negation = frame(bob, "spam", 21);
    negation.neg = true;
    _ = try stream(f, &.{ negation, frame(bob, "spam", 20) }, 21);
    try expectLabels(f, bob, &.{});
    try testing.expect(!(try f.live(bob)).hidden);
}

test "labels for accounts we do not hold, other labelers and malformed frames are skipped" {
    const f = try labelled();
    defer f.destroy();
    var other = frame(alice, "spam", 3);
    other.src = "did:plc:someotherlabelerxxxxxxxx";
    var undated = frame(alice, "spam", 4);
    undated.cts = "yesterday";
    var bad_expiry = frame(alice, "spam", 5);
    bad_expiry.exp = "never";
    const long: [129]u8 = @splat('v');
    const ignored = try stream(f, &.{
        frame(carol, "spam", 1),
        frame("at://" ++ alice ++ "/app.bsky.feed.post/3kabc", "spam", 2),
        other,
        undated,
        bad_expiry,
        frame(alice, "", 6),
        frame(alice, &long, 7),
        frame("did:plc:short", "spam", 8),
        frame(alice, "spam", 0),
    }, 8);
    try testing.expectEqual(@as(usize, 8), ignored);
    try expectLabels(f, alice, &.{"self:profile:!no-unauthenticated"});
    try testing.expect(try f.actor(carol) == null);
    try testing.expectEqual(@as(?u64, 8), (try f.cursors()).labeler);
}

test "a profile-record label is stored with its record uri" {
    const f = try labelled();
    defer f.destroy();
    _ = try stream(f, &.{frame("at://" ++ bob ++ "/app.bsky.actor.profile/self", "!takedown", 5)}, 5);
    try expectLabels(f, bob, &.{"mod:profile:!takedown"});
    try testing.expect((try f.live(bob)).hidden);
}

test "an expiring label is projected with its expiry and stops hiding once it lapses" {
    const f = try labelled();
    defer f.destroy();
    var lapsed = frame(bob, "!hide", 6);
    lapsed.exp = "2020-01-01T00:00:00.000Z";
    var pending = frame(alice, "!hide", 7);
    pending.exp = "2026-11-08T22:00:00.000Z";
    _ = try stream(f, &.{ lapsed, pending }, 7);
    const b = try f.live(bob);
    try testing.expectEqualStrings("2020-01-01T00:00:00.000Z", b.labels[0].expires);
    try testing.expect(!b.hidden);
    try testing.expect((try f.live(alice)).hidden);

    const expiry: u64 = @intCast(@divFloor(store_mod.time.parseMs("2026-11-08T22:00:00.000Z").?, 1000));
    f.clock.at.store(expiry - 1, .monotonic);
    _ = f.owner.turn(0);
    try testing.expect((try f.live(alice)).hidden);
    const logged = try logLength(f);
    f.clock.at.store(expiry + f.owner.options.expiry_seconds, .monotonic);
    _ = f.owner.turn(0);
    try testing.expect(!(try f.live(alice)).hidden);
    try testing.expectEqual(logged + 1, try logLength(f));
    try testing.expectEqual(@as(u64, 1), f.owner.counters.hidden_refreshed.load(.monotonic));
}

test "replaying a label batch has no second effect" {
    const f = try labelled();
    defer f.destroy();
    const batch = [_]label_tail.Wire{ frame(alice, "spam", 30), frame(bob, "spam", 31) };
    _ = try stream(f, &batch, 31);
    const logged = try logLength(f);
    _ = try stream(f, &batch, 31);
    try testing.expectEqual(logged, try logLength(f));
    try expectLabels(f, alice, &.{ "self:profile:!no-unauthenticated", "mod:account:spam" });
    try expectLabels(f, bob, &.{"mod:account:spam"});
    try testing.expectEqual(@as(?u64, 31), (try f.cursors()).labeler);
}

test "the labeler cursor never moves backward" {
    const f = try labelled();
    defer f.destroy();
    _ = try stream(f, &.{}, 50);
    _ = try stream(f, &.{}, 40);
    try testing.expectEqual(@as(?u64, 50), (try f.cursors()).labeler);
}

fn answer(uri: []const u8, val: []const u8, created: []const u8, negated: bool) store_mod.record.Label {
    return .{ .source = mod, .uri = uri, .value = val, .created = created, .negated = negated };
}

fn reconcile(f: *Fixture, did: []const u8, labels: []const store_mod.record.Label) !void {
    _ = try f.commit(&.{.{ .labels_reconciled = .{ .did = did, .labels = labels } }}, null);
}

test "reconciliation never overwrites a stream entry" {
    const f = try labelled();
    defer f.destroy();
    var negation = frame(alice, "spam", 40);
    negation.neg = true;
    _ = try stream(f, &.{negation}, 40);
    try reconcile(f, alice, &.{answer(alice, "spam", stamp, false)});
    try expectLabels(f, alice, &.{"self:profile:!no-unauthenticated"});
    try testing.expect(!(try f.live(alice)).hidden);
}

test "reconciliation folds label history and replaces its own earlier answer" {
    const f = try labelled();
    defer f.destroy();
    try reconcile(f, bob, &.{
        answer(bob, "spam", "2026-03-01T00:00:00.000Z", false),
        answer(bob, "spam", "2026-04-01T00:00:00.000Z", true),
        answer(bob, "!hide", "2026-05-01T00:00:00.000Z", false),
        answer(alice, "!takedown", "2026-05-01T00:00:00.000Z", false),
    });
    try expectLabels(f, bob, &.{"mod:account:!hide"});
    try testing.expect((try f.live(bob)).hidden);

    try reconcile(f, bob, &.{});
    try expectLabels(f, bob, &.{});
    try testing.expect(!(try f.live(bob)).hidden);
    try testing.expect(!(try f.live(alice)).hidden);
}

test "moderation labels a profile fetch left behind are cleared by the next answer" {
    const f = try labelled();
    defer f.destroy();
    _ = try f.commit(&.{.{ .enrichment = .{ .did = alice, .source = .bsky, .labels = &.{ self_label, answer(alice, "spam", "2026-01-01T00:00:00.000Z", false) } } }}, null);
    try testing.expect((try f.live(alice)).hidden);
    try reconcile(f, alice, &.{});
    try expectLabels(f, alice, &.{"self:profile:!no-unauthenticated"});
    try testing.expect(!(try f.live(alice)).hidden);
}

test "an unchanged account is not rewritten" {
    const f = try labelled();
    defer f.destroy();
    const logged = try logLength(f);
    const updated = (try f.live(bob)).updated_at;
    f.advance(100);
    try reconcile(f, alice, &.{});
    try reconcile(f, bob, &.{});
    var negation = frame(bob, "spam", 9);
    negation.neg = true;
    _ = try stream(f, &.{negation}, 9);
    try testing.expectEqual(logged, try logLength(f));
    try testing.expectEqual(updated, (try f.live(bob)).updated_at);
}

test "a show override keeps a labelled account visible" {
    const f = try labelled();
    defer f.destroy();
    _ = try f.commit(&.{.{ .override_set = .{ .did = alice, .action = .show } }}, null);
    _ = try stream(f, &.{frame(alice, "!takedown", 60)}, 60);
    try expectLabels(f, alice, &.{ "self:profile:!no-unauthenticated", "mod:account:!takedown" });
    try testing.expect(!(try f.live(alice)).hidden);
}
