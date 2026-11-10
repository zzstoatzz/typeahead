const std = @import("std");
const change = @import("change.zig");
const engine = @import("engine.zig");
const keys = @import("keys.zig");
const maintain = @import("maintain.zig");
const moderation = @import("moderation.zig");
const read = @import("read.zig");
const record = @import("record.zig");
const scan = @import("scan.zig");
const store_mod = @import("store.zig");
const Reader = @import("reader.zig").Reader;
const Store = store_mod.Store;
const testing = std.testing;

const alice = "did:plc:alice";
const bob = "did:plc:bob";
const carol = "did:plc:carol";
const t0 = 1_794_000_000;
const filler: [300]u8 = @splat('d');

const Fixture = struct {
    tmp: testing.TmpDir,
    arena: std.heap.ArenaAllocator,
    store: *Store,
    options: store_mod.Options,

    fn init(options: store_mod.Options) !Fixture {
        var tmp = testing.tmpDir(.{ .iterate = true });
        errdefer tmp.cleanup();
        return .{ .tmp = tmp, .arena = .init(testing.allocator), .options = options, .store = try Store.open(testing.allocator, testing.io, tmp.dir, "store", options) };
    }

    fn deinit(self: *Fixture) void {
        self.store.close();
        self.arena.deinit();
        self.tmp.cleanup();
    }

    fn reopen(self: *Fixture) !void {
        self.store.close();
        self.store = try Store.open(testing.allocator, testing.io, self.tmp.dir, "store", self.options);
    }

    fn apply(self: *Fixture, now: u64, one: change.Change) !change.Outcome {
        return self.store.apply(now, one, null);
    }

    fn actor(self: *Fixture, did: []const u8) !record.Actor {
        return (try self.store.actor(self.arena.allocator(), did)).?;
    }

    fn handleDids(self: *Fixture, handle: []const u8) ![]const []const u8 {
        return self.store.byHandle(self.arena.allocator(), handle);
    }

    fn logged(self: *Fixture) ![]const read.ChangeEntry {
        return (try self.store.changes(self.arena.allocator(), 0, 1000)).entries;
    }

    fn count(self: *Fixture, prefix: keys.Prefix) !u64 {
        return (try self.store.counts()).get(prefix);
    }
};

fn expectDids(expected: []const []const u8, got: []const []const u8) !void {
    try testing.expectEqual(expected.len, got.len);
    for (expected, got) |want, found| try testing.expectEqualStrings(want, found);
}

test "a bare observation creates an actor once" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const first = try f.apply(t0, .{ .observe = .{ .did = alice } });
    try testing.expect(first.created and first.changed == 1);
    const again = try f.apply(t0 + 10, .{ .observe = .{ .did = alice } });
    try testing.expect(again.applied and !again.created and again.changed == 0);

    const stored = try f.actor(alice);
    try testing.expectEqual(@as(u64, t0), stored.updated_at);
    try testing.expectEqual(@as(u64, t0), stored.last_activity_at);
    try testing.expectEqual(@as(usize, 1), (try f.logged()).len);
    try testing.expectEqual(@as(u64, 0), try f.count(.handle));

    _ = try f.apply(t0 + 3601, .{ .observe = .{ .did = alice } });
    try testing.expectEqual(@as(u64, t0 + 3601), (try f.actor(alice)).last_activity_at);
    try testing.expectEqual(@as(u64, t0), (try f.actor(alice)).updated_at);
    try testing.expectEqual(@as(usize, 1), (try f.logged()).len);
}

test "a profile update keeps stored fields an event leaves empty" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "Alice.Test", .display_name = "Alice", .avatar = "bafyavatar", .pds = "https://pds.test" } });
    _ = try f.apply(t0 + 5, .{ .profile = .{ .did = alice, .display_name = "Alice A.", .description = "hello" } });
    const stored = try f.actor(alice);
    try testing.expectEqualStrings("Alice.Test", stored.handle);
    try testing.expectEqualStrings("Alice A.", stored.display_name);
    try testing.expectEqualStrings("bafyavatar", stored.avatar);
    try testing.expectEqualStrings("https://pds.test", stored.pds);
    try testing.expectEqualStrings("hello", stored.description);
    try testing.expectEqual(@as(u64, t0 + 5), stored.updated_at);
    try testing.expectEqual(@as(u64, t0), stored.refresh_due);

    const unchanged = try f.apply(t0 + 9, .{ .profile = .{ .did = alice, .display_name = "Alice A." } });
    try testing.expectEqual(@as(usize, 0), unchanged.changed);
    try testing.expectEqual(@as(u64, t0 + 5), (try f.actor(alice)).updated_at);
    try testing.expectEqual(@as(usize, 2), (try f.logged()).len);
}

test "a handle change moves the handle key, and lookups ignore case" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .profile = .{ .did = bob, .handle = "shared.test" } });
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "Shared.Test" } });
    _ = try f.apply(t0, .{ .profile = .{ .did = carol, .handle = "shared.tests" } });
    try expectDids(&.{ alice, bob }, try f.handleDids("SHARED.test"));
    try expectDids(&.{carol}, try f.handleDids("shared.tests"));

    _ = try f.apply(t0 + 1, .{ .profile = .{ .did = alice, .handle = "alice.test" } });
    try expectDids(&.{bob}, try f.handleDids("shared.test"));
    try expectDids(&.{alice}, try f.handleDids("ALICE.TEST"));
    try testing.expectEqual(@as(u64, 3), try f.count(.handle));

    _ = try f.apply(t0 + 2, .{ .profile = .{ .did = alice, .handle = "ALICE.test" } });
    try expectDids(&.{alice}, try f.handleDids("alice.test"));
    try testing.expectEqualStrings("ALICE.test", (try f.actor(alice)).handle);
    try testing.expectEqual(@as(u64, 3), try f.count(.handle));
}

test "an older identity resolution cannot overwrite a newer one" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .observe = .{ .did = alice } });
    const newer = try f.apply(t0 + 10, .{ .identity_resolved = .{ .did = alice, .observed_at = t0 + 8, .handle = "new.test", .pds = "https://new.test" } });
    try testing.expect(newer.applied);
    const older = try f.apply(t0 + 11, .{ .identity_resolved = .{ .did = alice, .observed_at = t0 + 7, .handle = "old.test", .pds = "https://old.test" } });
    try testing.expect(!older.applied);
    const same = try f.apply(t0 + 12, .{ .identity_resolved = .{ .did = alice, .observed_at = t0 + 8, .pds = "https://moved.test" } });
    try testing.expect(same.applied);

    const stored = try f.actor(alice);
    try testing.expectEqualStrings("new.test", stored.handle);
    try testing.expectEqualStrings("https://moved.test", stored.pds);
    try testing.expectEqual(@as(u64, t0 + 8), stored.identity_resolved_at);
    try expectDids(&.{}, try f.handleDids("old.test"));
    try testing.expect(!(try f.apply(t0, .{ .identity_resolved = .{ .did = bob, .observed_at = t0, .handle = "bob.test" } })).applied);
    try testing.expectEqual(@as(?record.Actor, null), try f.store.actor(f.arena.allocator(), bob));
}

test "the quality walk follows score, visibility and handle" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();
    for ([_]struct { []const u8, []const u8, f32 }{ .{ alice, "alice.test", 300 }, .{ bob, "bob.test", 900.5 }, .{ carol, "", 5000 } }) |row| {
        _ = try f.apply(t0, .{ .profile = .{ .did = row[0], .handle = row[1], .display_name = "x" } });
        _ = try f.apply(t0, .{ .enrichment = .{ .did = row[0], .source = .bsky, .quality_score = row[2], .followers_count = 10 } });
    }
    var walk = try f.store.quality(mem, null, 10);
    try testing.expectEqual(@as(usize, 2), walk.len);
    try testing.expectEqualStrings(bob, walk[0].did);
    try testing.expectEqual(@as(f32, 900.5), walk[0].score);
    try testing.expectEqualStrings(alice, walk[1].did);

    _ = try f.apply(t0 + 1, .{ .enrichment = .{ .did = alice, .source = .bsky, .quality_score = 1200 } });
    walk = try f.store.quality(mem, null, 1);
    try testing.expectEqualStrings(alice, walk[0].did);
    walk = try f.store.quality(mem, walk[0], 10);
    try testing.expectEqual(@as(usize, 1), walk.len);
    try testing.expectEqualStrings(bob, walk[0].did);

    _ = try f.apply(t0 + 2, .{ .enrichment = .{ .did = alice, .source = .pds, .quality_score = 0, .followers_count = 0 } });
    try testing.expectEqual(@as(f32, 1200), (try f.actor(alice)).quality_score);
    try testing.expectEqual(@as(u64, 10), (try f.actor(alice)).followers_count);

    _ = try f.apply(t0 + 3, .{ .override_set = .{ .did = alice, .action = .hide } });
    _ = try f.apply(t0 + 3, .{ .profile = .{ .did = carol, .handle = "carol.test" } });
    walk = try f.store.quality(mem, null, 10);
    try testing.expectEqual(@as(usize, 2), walk.len);
    try testing.expectEqualStrings(carol, walk[0].did);
    try testing.expectEqualStrings(bob, walk[1].did);
    try testing.expectEqual(@as(u64, 2), try f.count(.quality));
}

test "a deleted account leaves a tombstone and nothing else, and comes back whole" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "alice.test", .display_name = "Alice", .enqueue_identity = true } });
    _ = try f.apply(t0, .{ .enrichment = .{ .did = alice, .source = .bsky, .quality_score = 300, .next_refresh = t0 + 500 } });
    try testing.expectEqual(@as(u64, 1), try f.count(.identity));
    try testing.expectEqual(@as(u64, 1), try f.count(.refresh));

    const gone = try f.apply(t0 + 10, .{ .gone = .{ .did = alice, .status = .deactivated } });
    try testing.expectEqual(@as(usize, 1), gone.changed);
    for ([_]keys.Prefix{ .handle, .quality, .identity, .refresh }) |prefix| try testing.expectEqual(@as(u64, 0), try f.count(prefix));
    const tombstones = try f.store.timed(mem, .tombstone, null, std.math.maxInt(u64), 10);
    try testing.expectEqual(@as(usize, 1), tombstones.len);
    try testing.expectEqual(@as(u64, t0 + 10), tombstones[0].at);
    try testing.expectEqual(record.Status.deactivated, (try f.actor(alice)).status);
    try expectDids(&.{}, try f.handleDids("alice.test"));

    try testing.expect(!(try f.apply(t0 + 11, .{ .profile = .{ .did = alice, .display_name = "ignored" } })).applied);
    try testing.expect(!(try f.apply(t0 + 11, .{ .observe = .{ .did = alice } })).applied);
    try testing.expect(!(try f.apply(t0 + 11, .{ .gone = .{ .did = bob, .status = .deleted } })).applied);

    const back = try f.apply(t0 + 20, .{ .reactivated = alice });
    try testing.expectEqual(@as(usize, 1), back.changed);
    const stored = try f.actor(alice);
    try testing.expectEqual(record.Status.live, stored.status);
    try testing.expectEqualStrings("Alice", stored.display_name);
    try testing.expectEqual(@as(u64, 0), stored.deleted_at);
    try testing.expectEqual(@as(u64, t0 + 20), stored.refresh_due);
    try testing.expectEqual(@as(u64, 0), try f.count(.tombstone));
    try testing.expectEqual(@as(u64, 1), try f.count(.quality));
    try expectDids(&.{alice}, try f.handleDids("alice.test"));

    _ = try f.apply(t0 + 30, .{ .gone = .{ .did = alice, .status = .deleted } });
    try testing.expect(!(try f.apply(t0 + 40, .{ .tombstones_pruned = .{ .before = t0 + 30, .limit = 10 } })).applied);
    const pruned = try f.apply(t0 + 40, .{ .tombstones_pruned = .{ .before = t0 + 31, .limit = 10 } });
    try testing.expectEqual(@as(usize, 1), pruned.changed);
    try testing.expectEqual(@as(?record.Actor, null), try f.store.actor(mem, alice));
    try testing.expectEqual(@as(u64, 0), try f.count(.tombstone));
    const log = try f.logged();
    try testing.expectEqualStrings(alice, log[log.len - 1].did);
}

const mod = moderation.bsky_moderation;

fn labelEvent(did: []const u8, value: []const u8, sequence: u64, negated: bool) change.Change {
    return .{ .label = .{ .did = did, .source = mod, .value = value, .created = "2026-11-10T00:00:00Z", .negated = negated, .sequence = sequence } };
}

test "labels hide, negations and older frames do not, and the override wins" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "alice.test" } });
    try testing.expect(!(try f.apply(t0, labelEvent(bob, "spam", 1, false))).applied);
    try testing.expect(!(try f.apply(t0, .{ .label = .{ .did = alice, .source = "did:plc:other", .value = "spam", .created = "", .sequence = 1 } })).applied);
    try testing.expectEqual(@as(u64, 0), try f.count(.label));

    _ = try f.apply(t0 + 1, labelEvent(alice, "!no-unauthenticated", 5, false));
    try testing.expect(!(try f.actor(alice)).hidden);
    try testing.expectEqual(@as(usize, 1), (try f.actor(alice)).labels.len);

    _ = try f.apply(t0 + 2, labelEvent(alice, "spam", 10, false));
    try testing.expect((try f.actor(alice)).hidden);
    try testing.expectEqual(@as(usize, 2), (try f.actor(alice)).labels.len);

    _ = try f.apply(t0 + 3, labelEvent(alice, "spam", 12, true));
    try testing.expect(!(try f.actor(alice)).hidden);
    try testing.expectEqual(@as(usize, 1), (try f.actor(alice)).labels.len);
    try testing.expectEqual(@as(u64, 2), try f.count(.label));

    try testing.expect(!(try f.apply(t0 + 4, labelEvent(alice, "spam", 11, false))).applied);
    try testing.expect(!(try f.actor(alice)).hidden);

    _ = try f.apply(t0 + 5, .{ .label = .{ .did = alice, .source = mod, .value = "!takedown", .subject = .profile, .created = "2026-11-10T00:00:00Z", .expires = "2026-11-01T00:00:00Z", .sequence = 13 } });
    try testing.expect(!(try f.actor(alice)).hidden);
    _ = try f.apply(t0 + 6, .{ .label = .{ .did = alice, .source = mod, .value = "!takedown", .subject = .profile, .created = "2026-11-10T00:00:00Z", .expires = "2099-01-01T00:00:00Z", .sequence = 14 } });
    try testing.expect((try f.actor(alice)).hidden);
    const stored = try f.actor(alice);
    try testing.expectEqualStrings("at://did:plc:alice/app.bsky.actor.profile/self", stored.labels[stored.labels.len - 1].uri);

    _ = try f.apply(t0 + 7, .{ .override_set = .{ .did = alice, .action = .show, .reason = "appeal" } });
    try testing.expect(!(try f.actor(alice)).hidden);
    _ = try f.apply(t0 + 8, .{ .override_cleared = alice });
    try testing.expect((try f.actor(alice)).hidden);

    _ = try f.apply(t0 + 9, .{ .override_set = .{ .did = bob, .action = .hide } });
    _ = try f.apply(t0 + 10, .{ .observe = .{ .did = bob } });
    try testing.expect((try f.actor(bob)).hidden);
}

test "an enrichment's labels replace the earlier answer and keep what the stream delivered" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "alice.test" } });
    _ = try f.apply(t0, labelEvent(alice, "spam", 9, true));
    const self_label: record.Label = .{ .source = alice, .uri = "at://did:plc:alice/app.bsky.actor.profile/self", .value = "!no-unauthenticated", .created = "1970-01-01T00:00:00Z" };
    const answered = [_]record.Label{
        self_label,
        .{ .source = mod, .uri = alice, .value = "spam", .created = "2026-11-01T00:00:00Z" },
        .{ .source = mod, .uri = alice, .value = "!hide", .created = "2026-11-01T00:00:00Z" },
        .{ .source = mod, .uri = alice, .value = "!hide", .created = "2026-11-02T00:00:00Z", .negated = true },
        .{ .source = mod, .uri = alice, .value = "!takedown", .created = "2026-11-01T00:00:00Z" },
        .{ .source = mod, .uri = "at://did:plc:alice/app.bsky.feed.post/3k", .value = "!suspend", .created = "2026-11-01T00:00:00Z" },
    };
    _ = try f.apply(t0 + 1, .{ .enrichment = .{ .did = alice, .source = .bsky, .labels = &answered } });
    var stored = try f.actor(alice);
    try testing.expect(stored.hidden);
    try testing.expectEqual(@as(usize, 2), stored.labels.len);
    try testing.expectEqualStrings("!no-unauthenticated", stored.labels[0].value);
    try testing.expectEqualStrings("!takedown", stored.labels[1].value);
    try testing.expectEqual(@as(u64, 3), try f.count(.label));

    _ = try f.apply(t0 + 2, .{ .enrichment = .{ .did = alice, .source = .pds } });
    try testing.expectEqual(@as(usize, 2), (try f.actor(alice)).labels.len);

    _ = try f.apply(t0 + 3, .{ .enrichment = .{ .did = alice, .source = .bsky, .labels = &.{} } });
    stored = try f.actor(alice);
    try testing.expect(!stored.hidden);
    try testing.expectEqual(@as(usize, 0), stored.labels.len);
    try testing.expectEqual(@as(u64, 1), try f.count(.label));
    try testing.expectEqual(@as(u64, t0 + 3), stored.profile_checked_at);
}

test "blocking a domain hides new writes at once and stored actors by sweep" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    for ([_]struct { []const u8, []const u8 }{ .{ alice, "alice.spam.test" }, .{ bob, "SPAM.test" }, .{ carol, "notspam.test" } }) |row| {
        _ = try f.apply(t0, .{ .profile = .{ .did = row[0], .handle = row[1] } });
    }
    _ = try f.apply(t0 + 1, .{ .domain_blocked = .{ .domain = "Spam.Test", .reason = "farm" } });
    try testing.expect(!(try f.actor(alice)).hidden);

    _ = try f.apply(t0 + 2, .{ .profile = .{ .did = "did:plc:dave", .handle = "dave.spam.test" } });
    try testing.expect((try f.actor("did:plc:dave")).hidden);

    var sweep: Store.Sweep = .{};
    defer sweep.deinit(testing.allocator);
    var commits: usize = 0;
    while (!sweep.done()) : (commits += 1) try f.store.sweepDomain(&sweep, "spam.test", t0 + 3, 2);
    try testing.expect(commits >= 2);
    try testing.expectEqual(@as(u64, 4), sweep.scanned);
    try testing.expectEqual(@as(u64, 2), sweep.changed);
    try testing.expect((try f.actor(alice)).hidden and (try f.actor(bob)).hidden and !(try f.actor(carol)).hidden);

    try f.reopen();
    _ = try f.apply(t0 + 4, .{ .profile = .{ .did = carol, .handle = "carol.spam.test" } });
    try testing.expect((try f.actor(carol)).hidden);

    _ = try f.apply(t0 + 5, .{ .override_set = .{ .did = bob, .action = .show } });
    _ = try f.apply(t0 + 6, .{ .domain_unblocked = "SPAM.TEST" });
    var undo: Store.Sweep = .{};
    defer undo.deinit(testing.allocator);
    while (!undo.done()) try f.store.sweepDomain(&undo, "spam.test", t0 + 7, 100);
    try testing.expectEqual(@as(u64, 3), undo.changed);
    try testing.expect(!(try f.actor(alice)).hidden and !(try f.actor(carol)).hidden);
    try testing.expectEqual(@as(u64, 1), try f.count(.policy));
}

test "the identity queue coalesces signals, leases by due time and settles by generation" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();
    _ = try f.apply(t0, .{ .observe = .{ .did = alice, .enqueue_identity = true } });
    _ = try f.apply(t0 + 5, .{ .observe = .{ .did = bob, .enqueue_identity = true } });
    try testing.expect(!(try f.apply(t0 + 6, .{ .identity_enqueue = .{ .did = carol, .reason = .plc } })).applied);
    try testing.expect(!(try f.apply(t0 + 6, .{ .identity_enqueue = .{ .did = alice, .reason = .discovered } })).applied);

    var batch = try f.store.begin(t0 + 10);
    _ = try batch.apply(.{ .identity_lease = .{ .limit = 1 } });
    try testing.expectEqual(@as(usize, 1), batch.leased().len);
    try testing.expectEqualStrings(alice, batch.leased()[0].did);
    try testing.expectEqual(@as(u64, 1), batch.leased()[0].attempts);
    try testing.expectEqual(record.IdentityReason.discovered, batch.leased()[0].reason);
    try batch.commit();
    try testing.expectEqual(@as(u64, t0 + 310), (try f.actor(alice)).identity_due);

    _ = try f.apply(t0 + 20, .{ .identity_enqueue = .{ .did = alice, .reason = .identity } });
    var queued = try f.store.timed(mem, .identity, null, std.math.maxInt(u64), 10);
    try testing.expectEqual(@as(usize, 2), queued.len);
    try testing.expectEqualStrings(bob, queued[0].did);
    try testing.expectEqual(@as(u64, t0 + 20), queued[1].at);
    var state = (try read.queueState(f.store.db(), mem, queued[1])).?;
    try testing.expectEqual(@as(u64, 2), state.generation);
    try testing.expectEqual(@as(u64, 0), state.attempts);
    try testing.expectEqual(record.IdentityReason.identity, state.reason);
    try testing.expectEqual(@as(u64, t0), state.enqueued_at);

    const stale = try f.apply(t0 + 30, .{ .identity_complete = .{ .did = alice, .generation = 1, .observed_at = t0 + 25, .pds = "https://pds.test", .outcome = .{ .resolved = .{ .handle = "alice.test" } } } });
    try testing.expect(stale.applied);
    try testing.expectEqualStrings("alice.test", (try f.actor(alice)).handle);
    try testing.expectEqual(@as(u64, t0 + 20), (try f.actor(alice)).identity_due);

    _ = try f.apply(t0 + 40, .{ .identity_complete = .{ .did = alice, .generation = 2, .observed_at = t0 + 35, .outcome = .{ .retry = .{ .attempts = 3, .delay_seconds = 5, .reason = "HTTP 503" } } } });
    try testing.expectEqual(@as(u64, t0 + 70), (try f.actor(alice)).identity_due);
    queued = try f.store.timed(mem, .identity, null, t0 + 69, 10);
    try testing.expectEqual(@as(usize, 1), queued.len);
    state = (try read.queueState(f.store.db(), mem, .{ .at = t0 + 70, .did = alice })).?;
    try testing.expectEqualStrings("HTTP 503", state.last_error);

    _ = try f.apply(t0 + 80, .{ .identity_complete = .{ .did = alice, .generation = 2, .observed_at = t0 + 75, .outcome = .{ .retry = .{ .attempts = 12, .delay_seconds = 5 } } } });
    try testing.expectEqual(@as(u64, 0), (try f.actor(alice)).identity_due);
    _ = try f.apply(t0 + 90, .{ .identity_complete = .{ .did = bob, .generation = 1, .observed_at = t0 + 85, .outcome = .{ .resolved = .{} } } });
    try testing.expectEqual(@as(u64, 0), try f.count(.identity));
    try testing.expectEqual(@as(u64, t0 + 85), (try f.actor(bob)).identity_resolved_at);
}

test "the refresh queue takes the earliest request and empties on completion" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();
    _ = try f.apply(t0, .{ .observe = .{ .did = alice } });
    try testing.expectEqual(@as(u64, 0), try f.count(.refresh));
    _ = try f.apply(t0, .{ .refresh_enqueue = .{ .did = alice, .due = t0 + 100 } });
    _ = try f.apply(t0, .{ .refresh_enqueue = .{ .did = alice, .due = t0 + 200 } });
    _ = try f.apply(t0, .{ .refresh_enqueue = .{ .did = alice, .due = t0 + 50 } });
    const due = try f.store.timed(mem, .refresh, null, t0 + 50, 10);
    try testing.expectEqual(@as(usize, 1), due.len);
    try testing.expectEqual(@as(u64, t0 + 50), due[0].at);
    try testing.expectEqual(@as(usize, 0), (try f.store.timed(mem, .refresh, null, t0 + 49, 10)).len);

    _ = try f.apply(t0 + 60, .{ .refresh_complete = .{ .did = alice, .next_due = t0 + 900 } });
    try testing.expectEqual(@as(u64, t0 + 60), (try f.actor(alice)).profile_checked_at);
    try testing.expectEqual(@as(u64, t0 + 900), (try f.actor(alice)).refresh_due);
    _ = try f.apply(t0 + 70, .{ .refresh_complete = .{ .did = alice } });
    try testing.expectEqual(@as(u64, 0), try f.count(.refresh));
    try testing.expectEqual(@as(usize, 1), (try f.logged()).len);
}

test "a batch lands whole with its cursor, or not at all" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();

    var batch = try f.store.begin(t0);
    try testing.expectError(error.BatchOpen, f.store.begin(t0));
    _ = try batch.apply(.{ .profile = .{ .did = alice, .handle = "alice.test" } });
    _ = try batch.apply(.{ .profile = .{ .did = bob, .handle = "bob.test" } });
    _ = try batch.apply(.{ .profile = .{ .did = alice, .display_name = "Alice" } });
    _ = try batch.apply(.{ .domain_blocked = .{ .domain = "blocked.test" } });
    try batch.setCursor(.{ .relay = 41 });
    batch.abort();
    try testing.expectEqual(@as(?record.Actor, null), try f.store.actor(mem, alice));
    try testing.expectEqual(@as(?u64, null), (try f.store.cursors(mem)).relay);
    try testing.expectEqual(@as(u64, 1), (try f.store.cursors(mem)).next_change);
    _ = try f.apply(t0, .{ .profile = .{ .did = carol, .handle = "x.blocked.test" } });
    try testing.expect(!(try f.actor(carol)).hidden);

    batch = try f.store.begin(t0 + 1);
    _ = try batch.apply(.{ .profile = .{ .did = alice, .handle = "alice.test" } });
    _ = try batch.apply(.{ .profile = .{ .did = bob, .handle = "bob.test" } });
    _ = try batch.apply(.{ .profile = .{ .did = alice, .display_name = "Alice" } });
    try testing.expectError(error.InvalidChange, batch.apply(.{ .observe = .{ .did = "" } }));
    try batch.setCursor(.{ .relay = 42 });
    try batch.setCursor(.{ .labeler = 9 });
    try batch.setCursor(.{ .labeler = 7 });
    try batch.setCursor(.{ .plc = "2026-11-10T00:00:02.000Z" });
    try batch.setCursor(.{ .plc = "2026-11-10T00:00:01.000Z" });
    try batch.commit();

    const log = try f.logged();
    try testing.expectEqual(@as(usize, 3), log.len);
    try testing.expectEqualStrings(carol, log[0].did);
    try testing.expectEqualStrings(alice, log[1].did);
    try testing.expectEqualStrings(bob, log[2].did);
    try testing.expectEqual(@as(u64, 3), log[2].sequence);
    const cursors = try f.store.cursors(mem);
    try testing.expectEqual(@as(?u64, 42), cursors.relay);
    try testing.expectEqual(@as(?u64, 9), cursors.labeler);
    try testing.expectEqualStrings("2026-11-10T00:00:02.000Z", cursors.plc.?);
    try testing.expectEqual(@as(u64, 4), cursors.next_change);

    try f.reopen();
    try testing.expectEqual(@as(?u64, 42), (try f.store.cursors(mem)).relay);
    _ = try f.apply(t0 + 2, .{ .profile = .{ .did = alice, .display_name = "A" } });
    try testing.expectEqual(@as(u64, 4), (try f.logged())[3].sequence);
}

test "trimming the change log moves its floor, and a position below it is reported" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();
    var buf: [32]u8 = undefined;
    for (0..10) |n| _ = try f.apply(t0, .{ .observe = .{ .did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>4}", .{n}) } });

    var trimmed = try f.store.trimChanges(4, 2);
    try testing.expectEqual(@as(u64, 2), trimmed.removed);
    try testing.expect(trimmed.more);
    try testing.expectEqual(@as(u64, 3), trimmed.floor);
    trimmed = try f.store.trimChanges(4, 2);
    try testing.expectEqual(@as(u64, 1), trimmed.removed);
    try testing.expect(!trimmed.more);
    try testing.expectEqual(@as(u64, 4), trimmed.floor);

    try testing.expectEqual(read.Changes{ .trimmed = 4 }, try f.store.changes(mem, 0, 100));
    try testing.expectEqual(read.Changes{ .trimmed = 4 }, try f.store.changes(mem, 3, 100));
    const rest = (try f.store.changes(mem, 4, 3)).entries;
    try testing.expectEqual(@as(usize, 3), rest.len);
    try testing.expectEqual(@as(u64, 4), rest[0].sequence);
    try testing.expectEqualStrings("did:plc:0003", rest[0].did);
    try testing.expectEqual(@as(usize, 0), (try f.store.changes(mem, 11, 100)).entries.len);

    try testing.expectEqual(@as(u64, 4), (try f.store.trimChanges(2, 100)).floor);
    trimmed = try f.store.trimChanges(1000, 100);
    try testing.expectEqual(@as(u64, 7), trimmed.removed);
    try testing.expectEqual(@as(u64, 11), trimmed.floor);
    try testing.expectEqual(@as(u64, 0), try f.count(.change));
    _ = try f.apply(t0, .{ .observe = .{ .did = "did:plc:late" } });
    try testing.expectEqual(@as(u64, 11), (try f.store.changes(mem, 11, 100)).entries[0].sequence);
}

test "a store written under another format is refused" {
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    {
        const handle = try engine.Handle.open(testing.allocator, testing.io, tmp.dir, "store", .{}, .writer);
        defer handle.close();
        var buf: keys.Buf = undefined;
        try handle.db.put(keys.cursor(&buf, .format), &record.encodeU64(store_mod.format + 1));
        try handle.db.commit();
    }
    try testing.expectError(error.UnsupportedStoreFormat, Store.open(testing.allocator, testing.io, tmp.dir, "store", .{}));
}

test "a second writer fences the first" {
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    const first = try Store.open(testing.allocator, testing.io, tmp.dir, "store", .{});
    defer first.close();
    _ = try first.apply(t0, .{ .observe = .{ .did = alice } }, null);
    const second = try Store.open(testing.allocator, testing.io, tmp.dir, "store", .{});
    defer second.close();
    try testing.expectError(error.Fenced, first.apply(t0, .{ .observe = .{ .did = bob } }, null));
    try testing.expectError(error.StoreBroken, first.begin(t0));
    _ = try second.apply(t0, .{ .observe = .{ .did = carol } }, null);
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    try testing.expect(try second.actor(arena.allocator(), alice) != null);
    try testing.expect(try second.actor(arena.allocator(), bob) == null);
}

const Seen = struct {
    last: std.ArrayList(u8) = .empty,
    entries: u64 = 0,

    pub fn entry(self: *Seen, key: []const u8, _: []const u8) !void {
        try testing.expect(std.mem.order(u8, self.last.items, key) == .lt);
        self.last.clearRetainingCapacity();
        try self.last.appendSlice(testing.allocator, key);
        self.entries += 1;
    }
};

test "a reader sees whole commits after a refresh and scans across collected tables" {
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    const layout: engine.Layout = .{ .block_bytes = 512, .cache_bytes = 0, .l0_tables = 2, .fence_stride = 2 };
    const writer = try Store.open(testing.allocator, testing.io, tmp.dir, "store", .{ .layout = layout });
    defer writer.close();
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    var buf: [32]u8 = undefined;

    for (0..2) |half| {
        const batch = try writer.begin(t0);
        for (0..100) |n| _ = try batch.apply(.{ .profile = .{ .did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>4}", .{half * 100 + n}), .handle = "h.test", .description = filler[0..200] } });
        try batch.commit();
        try writer.maintainer.flush();
    }

    const reader = try Reader.open(testing.allocator, testing.io, tmp.dir, "store", layout);
    defer reader.close();
    try testing.expectEqual(@as(usize, 200), (try reader.byHandle(arena.allocator(), "H.TEST")).len);
    try testing.expectError(error.ReadOnly, reader.db().put("a", "b"));

    const staged = try writer.begin(t0);
    _ = try staged.apply(.{ .observe = .{ .did = "did:plc:9000" } });
    try reader.refresh();
    try testing.expect(try reader.actor(arena.allocator(), "did:plc:9000") == null);
    try staged.commit();
    try testing.expect(try reader.actor(arena.allocator(), "did:plc:9000") == null);
    try reader.refresh();
    try testing.expect(try reader.actor(arena.allocator(), "did:plc:9000") != null);

    var seen: Seen = .{};
    defer seen.last.deinit(testing.allocator);
    var position: scan.Position = .{};
    defer position.deinit(testing.allocator);
    var stats = try reader.scanRange(&position, .actor, .{ .page = 20, .refresh = false, .limit = 60 }, &seen);
    try testing.expectEqual(@as(u64, 60), seen.entries);
    try testing.expect(!position.done);

    try writer.maintainer.settle();
    reader.handle.objects.deinit();
    stats = try reader.scanRange(&position, .actor, .{ .page = 20, .refresh = false }, &seen);
    try testing.expect(position.done);
    try testing.expectEqual(@as(u64, 201), seen.entries);
    try testing.expectEqual(@as(u64, 1), stats.missing);
    try testing.expectEqual(@as(u64, 1), stats.refreshes);
}

test "maintenance flushes, compacts in steps, collects, and accounts for stalls" {
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    var marks: std.EnumArray(maintain.Phase, [2]u32) = .initFill(.{ 0, 0 });
    const options: store_mod.Options = .{
        .layout = .{ .block_bytes = 512, .table_bytes = 8 << 10, .l0_tables = 2, .level_bytes = 32 << 10, .fence_stride = 4 },
        .maintenance = .{ .mode = .bulk_load, .flush_bytes = 4 << 10, .flush_segments = 4, .compact_bytes = 2 << 10, .log_names = 3, .stall_tables = 2 },
        .trace = .{ .context = &marks, .call = struct {
            fn call(context: ?*anyopaque, phase: maintain.Phase, edge: maintain.Edge) void {
                const counts: *std.EnumArray(maintain.Phase, [2]u32) = @ptrCast(@alignCast(context.?));
                counts.getPtr(phase)[@backingInt(edge)] += 1;
            }
        }.call },
    };
    const store = try Store.open(testing.allocator, testing.io, tmp.dir, "store", options);
    defer store.close();
    var buf: [32]u8 = undefined;
    var stalled_without_relief: u64 = 0;
    for (0..400) |n| {
        _ = try store.apply(t0 + n, .{ .profile = .{ .did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>4}", .{n % 150}), .handle = "h.test", .description = try std.fmt.bufPrint(buf[16..], "{d}", .{n}) } }, .{ .relay = n });
        if (n % 3 != 0) continue;
        if (store.maintainer.flushDue()) try store.maintainer.flush();
        const planned = maintain.compactionPlanned(store.db());
        try testing.expectEqual(planned, try store.maintainer.compactStep());
        if (store.maintainer.log_pending) try store.maintainer.collectLogStep();
        if (store.maintainer.pressure() == .stalled) stalled_without_relief += 1;
        try testing.expectEqual(maintain.Pressure.none, try store.maintainer.relieve());
        try testing.expect(store.db().version.tables[0].len <= options.maintenance.stall_tables);
    }
    var stats = store.stats();
    try testing.expect(stats.flushes.count > 10);
    try testing.expect(stats.compactions > 2 and stats.compaction_steps.count > stats.compactions);
    try testing.expect(stats.table_collections.count >= stats.compactions);
    try testing.expect(stats.log_collections.count > 0);
    try testing.expect(stats.last_compaction_at > 1_700_000_000 and stats.last_compaction_ns > 0);
    try testing.expectEqual(stalled_without_relief, stats.stalls.count);
    try testing.expect(stats.stalls.count > 0 and stats.stall_steps > 0 and stats.stalls.total_ns >= stats.stalls.longest_ns and stats.stalls.longest_ns > 0);
    try testing.expectEqual(@as(u64, 0), stats.unrelieved);
    try testing.expect(stats.resident_metadata_bytes > 0 and stats.tables > 0);
    for (std.enums.values(maintain.Phase)) |phase| {
        try testing.expect(marks.get(phase)[0] > 0);
        try testing.expectEqual(marks.get(phase)[0], marks.get(phase)[1]);
    }
    try testing.expectEqual(@as(u32, @intCast(stats.compaction_steps.count)), marks.get(.compact)[0]);

    try store.maintainer.settle();
    stats = store.stats();
    var levels_in_use: usize = 0;
    var bytes: u64 = 0;
    for (stats.levels) |level| {
        levels_in_use += @intFromBool(level.tables > 0);
        bytes += level.bytes;
    }
    try testing.expectEqual(@as(usize, 0), stats.levels[0].tables);
    try testing.expectEqual(stats.table_bytes, bytes);
    try testing.expectEqual(@as(u64, 0), stats.unflushed_segments);
    try testing.expect(!stats.compaction_active);
    const disk = try store.diskUsage();
    try testing.expect(disk.bytes >= stats.table_bytes and disk.files > stats.tables);
    try testing.expectEqual(@as(u64, 150), (try store.counts()).get(.actor));
}

test "steady relief is bounded and reports the pressure it leaves" {
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    const store = try Store.open(testing.allocator, testing.io, tmp.dir, "store", .{
        .layout = .{ .block_bytes = 512, .table_bytes = 8 << 10, .l0_tables = 2, .level_bytes = 32 << 10 },
        .maintenance = .{ .mode = .steady, .compact_bytes = 64, .stall_tables = 2, .relief_steps = 1 },
    });
    defer store.close();
    var buf: [32]u8 = undefined;
    for (0..6) |round| {
        for (0..40) |n| _ = try store.apply(t0, .{ .profile = .{ .did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>4}", .{round * 40 + n}), .handle = "h.test", .description = &filler } }, null);
        try store.maintainer.flush();
    }
    try testing.expectEqual(maintain.Pressure.stalled, store.maintainer.pressure());
    try testing.expectEqual(maintain.Pressure.stalled, try store.maintainer.relieve());
    var stats = store.stats();
    try testing.expectEqual(@as(u64, 1), stats.stalls.count);
    try testing.expectEqual(@as(u64, 1), stats.stall_steps);
    try testing.expectEqual(@as(u64, 1), stats.unrelieved);
    var calls: u64 = 1;
    var left: maintain.Pressure = .stalled;
    while (left == .stalled) : (calls += 1) left = try store.maintainer.relieve();
    stats = store.stats();
    try testing.expectEqual(calls, stats.stall_steps);
    try testing.expectEqual(calls, stats.stalls.count);
    try testing.expectEqual(calls - 1, stats.unrelieved);
    try testing.expect(calls > 3);
}
