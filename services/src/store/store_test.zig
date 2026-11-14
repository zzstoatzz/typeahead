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

test "an enrichment's handle is never written, and one that differs queues identity work within its bounds" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const check: change.HandleCheck = .{ .settle_seconds = 1000, .backlog_limit = 2 };
    for ([_][2][]const u8{ .{ alice, "alice.test" }, .{ bob, "bob.test" }, .{ carol, "carol.test" } }) |row| {
        _ = try f.apply(t0, .{ .profile = .{ .did = row[0], .handle = row[1] } });
    }

    const same = try f.apply(t0 + 1, .{ .enrichment = .{ .did = alice, .source = .bsky, .handle = "Alice.Test", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.none, same.handle);
    const unasked = try f.apply(t0 + 1, .{ .enrichment = .{ .did = alice, .source = .bsky, .handle = "handle.invalid" } });
    try testing.expectEqual(change.HandleVerdict.none, unasked.handle);
    try testing.expectEqual(@as(u64, 0), try f.count(.identity));

    const differs = try f.apply(t0 + 2, .{ .enrichment = .{ .did = alice, .source = .bsky, .handle = "handle.invalid", .followers_count = 7, .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.queued, differs.handle);
    var actor = try f.actor(alice);
    try testing.expectEqualStrings("alice.test", actor.handle);
    try testing.expectEqual(@as(u64, 7), actor.followers_count);
    try testing.expectEqual(@as(u64, t0 + 2), actor.identity_due);
    try expectDids(&.{alice}, try f.handleDids("alice.test"));
    try expectDids(&.{}, try f.handleDids("handle.invalid"));
    const state = (try read.queueState(f.store.db(), f.arena.allocator(), .{ .at = t0 + 2, .did = alice })).?;
    try testing.expectEqual(record.IdentityReason.identity, state.reason);
    try testing.expectEqual(@as(u64, 1), state.generation);

    const again = try f.apply(t0 + 3, .{ .enrichment = .{ .did = alice, .source = .blacksky, .handle = "elsewhere.test", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.pending, again.handle);
    try testing.expectEqual(@as(u64, t0 + 2), (try f.actor(alice)).identity_due);

    const second = try f.apply(t0 + 3, .{ .enrichment = .{ .did = bob, .source = .bsky, .handle = "stale.test", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.queued, second.handle);
    const third = try f.apply(t0 + 3, .{ .enrichment = .{ .did = carol, .source = .bsky, .handle = "stale.test", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.deferred, third.handle);
    try testing.expectEqual(@as(u64, 0), (try f.actor(carol)).identity_due);
    try testing.expectEqualStrings("carol.test", (try f.actor(carol)).handle);

    _ = try f.apply(t0 + 5, .{ .identity_complete = .{ .did = alice, .generation = 1, .observed_at = t0 + 5, .outcome = .{ .resolved = .{ .handle = "alice.test" } } } });
    const settled = try f.apply(t0 + 6, .{ .enrichment = .{ .did = alice, .source = .bsky, .handle = "handle.invalid", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.settled, settled.handle);
    try testing.expectEqual(@as(u64, 0), (try f.actor(alice)).identity_due);
    const later = try f.apply(t0 + 1005, .{ .enrichment = .{ .did = alice, .source = .bsky, .handle = "handle.invalid", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.queued, later.handle);
    actor = try f.actor(alice);
    try testing.expectEqualStrings("alice.test", actor.handle);
    try testing.expectEqual(@as(u64, t0 + 1005), actor.identity_due);

    const full = try f.apply(t0 + 1005, .{ .enrichment = .{ .did = carol, .source = .bsky, .handle = "stale.test", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.deferred, full.handle);
    _ = try f.apply(t0 + 1005, .{ .identity_lease = .{ .limit = 1 } });
    const room = try f.apply(t0 + 1005, .{ .enrichment = .{ .did = carol, .source = .bsky, .handle = "stale.test", .handle_check = check } });
    try testing.expectEqual(change.HandleVerdict.queued, room.handle);
    try testing.expectEqualStrings("carol.test", (try f.actor(carol)).handle);

    _ = try f.apply(t0 + 1006, .{ .observe = .{ .did = "did:plc:dave" } });
    _ = try f.apply(t0 + 1006, .{ .refresh_enqueue = .{ .did = "did:plc:dave", .due = t0 + 1006 } });
    const empty = try f.apply(t0 + 1006, .{ .enrichment = .{ .did = "did:plc:dave", .source = .bsky, .handle = "claimed.test", .handle_check = .{} } });
    try testing.expectEqual(change.HandleVerdict.queued, empty.handle);
    try testing.expectEqualStrings("", (try f.actor("did:plc:dave")).handle);
    try expectDids(&.{}, try f.handleDids("claimed.test"));
}

fn generationOf(f: *Fixture, did: []const u8) !?u64 {
    const actor = try f.actor(did);
    if (actor.identity_due == 0) return null;
    return (try read.queueState(f.store.db(), f.arena.allocator(), .{ .at = actor.identity_due, .did = did })).?.generation;
}

/// A store whose every actor was checked at `t0` and is out of the refresh
/// queue.
fn loadStamped(f: *Fixture, did: []const u8, handle: []const u8, fetched: change.Enrichment) !void {
    _ = try f.apply(t0, .{ .profile = .{ .did = did, .handle = handle } });
    var enrichment = fetched;
    enrichment.did = did;
    _ = try f.apply(t0, .{ .enrichment = enrichment });
}

test "the first-check pass queues each actor no source answered for once, in steps, and survives a reopen" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const bare: change.Enrichment = .{ .did = "", .source = .bsky };
    for ([_][]const u8{ "did:plc:a", "did:plc:c", "did:plc:e", "did:plc:g" }) |did| try loadStamped(&f, did, "bare.test", bare);
    try loadStamped(&f, "did:plc:b", "scored.test", .{ .did = "", .source = .bsky, .quality_score = 30 });
    try loadStamped(&f, "did:plc:d", "counted.test", .{ .did = "", .source = .bsky, .followers_count = 2 });
    try loadStamped(&f, "did:plc:f", "dated.test", .{ .did = "", .source = .bsky, .created_at = "2026-11-01T00:00:00.000Z" });
    try loadStamped(&f, "did:plc:h", "", bare);
    try loadStamped(&f, "did:plc:i", "gone.test", bare);
    _ = try f.apply(t0, .{ .gone = .{ .did = "did:plc:i", .status = .deactivated } });
    // Checked since the load, and found to have no profile anywhere.
    try loadStamped(&f, "did:plc:j", "absent.test", bare);
    _ = try f.apply(t0 + 5, .{ .refresh_complete = .{ .did = "did:plc:j" } });
    // Queued already, and leased: the pass leaves the entry as it is.
    _ = try f.apply(t0 + 5, .{ .refresh_enqueue = .{ .did = "did:plc:e", .due = t0 + 300 } });
    try testing.expectEqual(@as(u64, 1), try f.count(.refresh));

    const pass: Store.FirstCheck = .{ .stamp = t0, .limit = 2, .scan = 3, .backlog_limit = 100 };
    try testing.expectEqual(Store.FirstChecked{ .examined = 3, .queued = 2 }, try f.store.firstCheck(t0 + 10, pass));
    try testing.expectEqual(@as(u64, t0 + 10), (try f.actor("did:plc:a")).refresh_due);
    try testing.expectEqual(@as(u64, t0 + 10), (try f.actor("did:plc:c")).refresh_due);
    const changes = (try f.store.cursors(f.arena.allocator())).next_change;

    try f.reopen();
    var state = try read.firstCheckState(f.store.db(), f.arena.allocator());
    try testing.expectEqualStrings("did:plc:c", state.position);
    try testing.expectEqual(@as(u64, t0), state.stamp);
    try testing.expectEqual(@as(u64, t0 + 10), state.started_at);
    try testing.expectEqual(@as(u64, 0), state.finished_at);

    // A step that fills its limit before its scan ends stops there.
    try testing.expectEqual(Store.FirstChecked{ .examined = 4, .queued = 1 }, try f.store.firstCheck(t0 + 20, .{ .stamp = t0, .limit = 1, .scan = 50, .backlog_limit = 100 }));
    try testing.expectEqual(@as(u64, t0 + 20), (try f.actor("did:plc:g")).refresh_due);
    try testing.expectEqual(@as(u64, t0 + 300), (try f.actor("did:plc:e")).refresh_due);
    // The pass is finished by a scan that comes back short of its limit.
    try testing.expectEqual(Store.FirstChecked{ .examined = 3 }, try f.store.firstCheck(t0 + 25, pass));
    try testing.expectEqual(Store.FirstChecked{ .finished = true }, try f.store.firstCheck(t0 + 30, pass));
    for ([_][]const u8{ "did:plc:b", "did:plc:d", "did:plc:f", "did:plc:h", "did:plc:i", "did:plc:j" }) |did| try testing.expectEqual(@as(u64, 0), (try f.actor(did)).refresh_due);
    try testing.expectEqual(@as(u64, 4), try f.count(.refresh));
    // Queuing a refresh is not a change to the actor.
    try testing.expectEqual(changes, (try f.store.cursors(f.arena.allocator())).next_change);

    try loadStamped(&f, "did:plc:late", "late.test", bare);
    try f.reopen();
    try testing.expectEqual(Store.FirstChecked{ .finished = true }, try f.store.firstCheck(t0 + 50, pass));
    try testing.expectEqual(@as(u64, 0), (try f.actor("did:plc:late")).refresh_due);
    state = try read.firstCheckState(f.store.db(), f.arena.allocator());
    try testing.expectEqual(@as(u64, t0 + 30), state.finished_at);
    try testing.expectEqual(@as(u64, 10), state.examined);
    try testing.expectEqual(@as(u64, 3), state.queued);
    try testing.expectEqualStrings("did:plc:j", state.position);
}

test "the first-check pass starts over when it is asked for another check time" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const bare: change.Enrichment = .{ .did = "", .source = .bsky };
    try loadStamped(&f, "did:plc:stamped", "stamped.test", bare);
    // What a load leaves that does not stamp an actor no source answered for.
    _ = try f.apply(t0, .{ .profile = .{ .did = "did:plc:unchecked", .handle = "unchecked.test" } });
    var unchecked = bare;
    unchecked.did = "did:plc:unchecked";
    unchecked.checked = false;
    _ = try f.apply(t0, .{ .enrichment = unchecked });
    try testing.expectEqual(@as(u64, 0), (try f.actor("did:plc:unchecked")).profile_checked_at);
    try testing.expectEqual(@as(u64, 0), try f.count(.refresh));

    const never: Store.FirstCheck = .{ .limit = 10, .scan = 10, .backlog_limit = 100 };
    try testing.expectEqual(Store.FirstChecked{ .examined = 2, .queued = 1, .finished = true }, try f.store.firstCheck(t0 + 10, never));
    try testing.expectEqual(@as(u64, t0 + 10), (try f.actor("did:plc:unchecked")).refresh_due);
    try testing.expectEqual(@as(u64, 0), (try f.actor("did:plc:stamped")).refresh_due);
    try testing.expectEqual(Store.FirstChecked{ .finished = true }, try f.store.firstCheck(t0 + 20, never));

    var stamped = never;
    stamped.stamp = t0;
    try testing.expectEqual(Store.FirstChecked{ .examined = 2, .queued = 1, .finished = true }, try f.store.firstCheck(t0 + 30, stamped));
    try testing.expectEqual(@as(u64, t0 + 30), (try f.actor("did:plc:stamped")).refresh_due);
    const state = try read.firstCheckState(f.store.db(), f.arena.allocator());
    try testing.expectEqual(@as(u64, t0), state.stamp);
    try testing.expectEqual(@as(u64, t0 + 30), state.started_at);
    try testing.expectEqual(@as(u64, 2), state.examined);
    try testing.expectEqual(@as(u64, 1), state.queued);
}

test "the first-check pass queues nothing while the refresh backlog is at its limit" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    var buf: [32]u8 = undefined;
    for (0..8) |n| try loadStamped(&f, try std.fmt.bufPrint(&buf, "did:plc:{d}", .{n}), "bare.test", .{ .did = "", .source = .bsky });
    _ = try f.apply(t0, .{ .profile = .{ .did = "did:plc:written", .handle = "written.test" } });
    try testing.expectEqual(@as(u64, 1), try f.count(.refresh));

    const pass: Store.FirstCheck = .{ .stamp = t0, .limit = 5, .scan = 50, .backlog_limit = 3 };
    try testing.expectEqual(Store.FirstChecked{ .examined = 2, .queued = 2 }, try f.store.firstCheck(t0 + 1, pass));
    try testing.expectEqual(Store.FirstChecked{}, try f.store.firstCheck(t0 + 2, pass));
    try testing.expectEqual(@as(u64, 3), try f.count(.refresh));
    try testing.expectEqualStrings("did:plc:1", (try read.firstCheckState(f.store.db(), f.arena.allocator())).position);

    // A lease takes the due entries out of the backlog, and a fetch settles them.
    _ = try f.apply(t0 + 3, .{ .refresh_lease = .{ .limit = 2 } });
    try testing.expectEqual(Store.FirstChecked{ .examined = 2, .queued = 2 }, try f.store.firstCheck(t0 + 3, pass));
    _ = try f.apply(t0 + 4, .{ .refresh_lease = .{ .limit = 50 } });
    try testing.expectEqual(Store.FirstChecked{ .examined = 5, .queued = 4, .finished = true }, try f.store.firstCheck(t0 + 4, .{ .stamp = t0, .limit = 5, .scan = 50, .backlog_limit = 100 }));
    try testing.expectEqual(@as(u64, 9), try f.count(.refresh));
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

test "a lease moves due refreshes ahead and settles only what still carries it" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    for ([_][]const u8{ alice, bob, carol }) |did| _ = try f.apply(t0, .{ .profile = .{ .did = did, .handle = "a.test", .pds = "https://pds.test" } });
    _ = try f.apply(t0, .{ .override_set = .{ .did = bob, .action = .show } });
    try testing.expectEqual(@as(u64, 3), try f.count(.refresh));

    const batch = try f.store.begin(t0 + 10);
    _ = try batch.apply(.{ .refresh_lease = .{ .limit = 2, .lease_seconds = 300 } });
    const jobs = batch.refreshLeased();
    try testing.expectEqual(@as(usize, 2), jobs.len);
    try testing.expectEqualStrings(alice, jobs[0].did);
    try testing.expectEqualStrings(bob, jobs[1].did);
    try testing.expectEqualStrings("a.test", jobs[0].handle);
    try testing.expectEqualStrings("https://pds.test", jobs[0].pds);
    try testing.expectEqual(record.Override.show, jobs[1].override);
    try testing.expectEqual(@as(u64, t0 + 310), jobs[0].lease);
    try batch.commit();
    try testing.expectEqual(@as(usize, 1), (try f.store.timed(f.arena.allocator(), .refresh, null, t0 + 10, 10)).len);
    try testing.expectEqual(@as(usize, 3), (try f.store.timed(f.arena.allocator(), .refresh, null, t0 + 310, 10)).len);

    _ = try f.apply(t0 + 20, .{ .enrichment = .{ .did = alice, .source = .bsky, .followers_count = 5, .quality_score = 70, .lease = t0 + 310 } });
    try testing.expectEqual(@as(u64, 0), (try f.actor(alice)).refresh_due);

    _ = try f.apply(t0 + 20, .{ .profile = .{ .did = bob, .display_name = "Bob" } });
    try testing.expectEqual(@as(u64, t0 + 20), (try f.actor(bob)).refresh_due);
    _ = try f.apply(t0 + 30, .{ .enrichment = .{ .did = bob, .source = .bsky, .followers_count = 9, .lease = t0 + 310 } });
    try testing.expectEqual(@as(u64, t0 + 20), (try f.actor(bob)).refresh_due);
    try testing.expectEqual(@as(u64, 9), (try f.actor(bob)).followers_count);
    _ = try f.apply(t0 + 30, .{ .refresh_complete = .{ .did = bob, .lease = t0 + 310 } });
    try testing.expectEqual(@as(u64, t0 + 20), (try f.actor(bob)).refresh_due);
    _ = try f.apply(t0 + 30, .{ .refresh_retry = .{ .did = bob, .lease = t0 + 310, .due = t0 + 900 } });
    try testing.expectEqual(@as(u64, t0 + 20), (try f.actor(bob)).refresh_due);

    _ = try f.apply(t0 + 40, .{ .refresh_lease = .{ .limit = 5, .lease_seconds = 300 } });
    try testing.expectEqual(@as(u64, t0 + 340), (try f.actor(carol)).refresh_due);
    _ = try f.apply(t0 + 50, .{ .refresh_retry = .{ .did = carol, .lease = t0 + 340, .due = t0 + 100 } });
    try testing.expectEqual(@as(u64, t0 + 100), (try f.actor(carol)).refresh_due);
    try testing.expectEqual(@as(u64, 0), (try f.actor(carol)).profile_checked_at);

    try f.reopen();
    try testing.expectEqual(@as(usize, 2), (try f.store.timed(f.arena.allocator(), .refresh, null, t0 + 340, 10)).len);
}

test "a lease reads its queue from the first time an entry can have, not from time 0" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    for ([_][]const u8{ alice, bob }) |did| _ = try f.apply(t0, .{ .profile = .{ .did = did, .handle = "a.test" } });
    _ = try f.apply(t0 + 10, .{ .refresh_lease = .{ .limit = 5, .lease_seconds = 300 } });
    for ([_][]const u8{ alice, bob }) |did| _ = try f.apply(t0 + 20, .{ .enrichment = .{ .did = did, .source = .bsky, .lease = t0 + 310 } });
    _ = try f.apply(t0 + 30, .{ .refresh_lease = .{ .limit = 5 } });
    _ = try f.apply(t0 + 30, .{ .identity_lease = .{ .limit = 5 } });
    try testing.expectEqual(@as(u64, 0), try f.count(.refresh));

    // Keys no lease leaves behind, put back by hand for an actor the store
    // does not hold: a lease that still walked the dead range from time 0
    // would find them and fail on the missing actor.
    var key: keys.Buf = undefined;
    try f.store.db().put(try keys.timed(&key, .refresh, t0 + 5, "did:plc:ghost"), "");
    try f.store.db().put(try keys.timed(&key, .identity, t0 + 5, "did:plc:ghost"), "");
    try f.store.db().commit();
    const batch = try f.store.begin(t0 + 40);
    _ = try batch.apply(.{ .refresh_lease = .{ .limit = 5 } });
    _ = try batch.apply(.{ .identity_lease = .{ .limit = 5 } });
    try testing.expectEqual(@as(usize, 0), batch.refreshLeased().len);
    try testing.expectEqual(@as(usize, 0), batch.leased().len);
    try batch.commit();
}

test "an entry queued for a time before the last lease is still leased" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    for ([_][]const u8{ alice, bob }) |did| _ = try f.apply(t0, .{ .profile = .{ .did = did, .handle = "a.test" } });
    _ = try f.apply(t0 + 10, .{ .refresh_lease = .{ .limit = 5, .lease_seconds = 300 } });
    for ([_][]const u8{ alice, bob }) |did| _ = try f.apply(t0 + 20, .{ .enrichment = .{ .did = did, .source = .bsky, .lease = t0 + 310 } });
    _ = try f.apply(t0 + 500, .{ .refresh_lease = .{ .limit = 5 } });
    _ = try f.apply(t0 + 500, .{ .identity_lease = .{ .limit = 5 } });

    _ = try f.apply(t0 + 500, .{ .refresh_enqueue = .{ .did = bob, .due = t0 + 1 } });
    // A commit stamped earlier than the last one, as after a clock step.
    _ = try f.apply(t0 + 2, .{ .identity_enqueue = .{ .did = alice, .reason = .identity } });
    const batch = try f.store.begin(t0 + 600);
    _ = try batch.apply(.{ .refresh_lease = .{ .limit = 5 } });
    _ = try batch.apply(.{ .identity_lease = .{ .limit = 5 } });
    try testing.expectEqual(@as(usize, 1), batch.refreshLeased().len);
    try testing.expectEqualStrings(bob, batch.refreshLeased()[0].did);
    try testing.expectEqual(@as(usize, 1), batch.leased().len);
    try testing.expectEqualStrings(alice, batch.leased()[0].did);
    try batch.commit();

    _ = try f.apply(t0 + 700, .{ .gone = .{ .did = alice, .status = .deleted } });
    _ = try f.apply(t0 + 800, .{ .tombstones_pruned = .{ .before = t0 + 100, .limit = 5 } });
    _ = try f.apply(t0 + 50, .{ .gone = .{ .did = bob, .status = .deleted } });
    try testing.expectEqual(@as(u64, 1), (try f.apply(t0 + 900, .{ .tombstones_pruned = .{ .before = t0 + 100, .limit = 5 } })).changed);
    try testing.expectEqual(@as(u64, 1), try f.count(.tombstone));
}

test "a lease in a batch that is dropped does not hide what it leased" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "a.test" } });
    {
        const batch = try f.store.begin(t0 + 10);
        _ = try batch.apply(.{ .refresh_lease = .{ .limit = 5 } });
        _ = try batch.apply(.{ .refresh_lease = .{ .limit = 5 } });
        _ = try batch.apply(.{ .identity_enqueue = .{ .did = alice, .reason = .identity } });
        _ = try batch.apply(.{ .identity_lease = .{ .limit = 5 } });
        _ = try batch.apply(.{ .identity_lease = .{ .limit = 5 } });
        try testing.expectEqual(@as(usize, 1), batch.refreshLeased().len);
        batch.abort();
    }
    _ = try f.apply(t0 + 5, .{ .identity_enqueue = .{ .did = alice, .reason = .identity } });
    const batch = try f.store.begin(t0 + 10);
    _ = try batch.apply(.{ .refresh_lease = .{ .limit = 5 } });
    _ = try batch.apply(.{ .identity_lease = .{ .limit = 5 } });
    try testing.expectEqual(@as(usize, 1), batch.refreshLeased().len);
    try testing.expectEqual(@as(usize, 1), batch.leased().len);
    try batch.commit();

    try f.reopen();
    _ = try f.apply(t0 + 20, .{ .enrichment = .{ .did = alice, .source = .bsky, .lease = t0 + 310 } });
    try testing.expectEqual(@as(u64, 0), try f.count(.refresh));
    const again = try f.store.begin(t0 + 400);
    _ = try again.apply(.{ .identity_lease = .{ .limit = 5 } });
    try testing.expectEqual(@as(usize, 1), again.leased().len);
    try again.commit();
}

const dave = "did:plc:dddddddddddddddddddddddd";
const erin = "did:plc:eeeeeeeeeeeeeeeeeeeeeeee";

test "a sighting of a known actor inside its hour reads no record, and the stamp still lands on the hour" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    try testing.expect((try f.apply(t0, .{ .observe = .{ .did = dave } })).created);

    // The record taken away by hand: a sighting that still read it would
    // find no actor and create one.
    var key: keys.Buf = undefined;
    const stored = try f.arena.allocator().dupe(u8, (try f.store.db().get(try keys.actor(&key, dave))).?);
    try f.store.db().del(try keys.actor(&key, dave));
    try f.store.db().commit();
    const outcome = try f.apply(t0 + change.activity_interval, .{ .observe = .{ .did = dave } });
    try testing.expect(outcome.applied and !outcome.created and outcome.changed == 0);
    try testing.expect(try f.store.actor(f.arena.allocator(), dave) == null);
    try f.store.db().put(try keys.actor(&key, dave), stored);
    try f.store.db().commit();

    _ = try f.apply(t0 + change.activity_interval + 1, .{ .observe = .{ .did = dave } });
    try testing.expectEqual(@as(u64, t0 + change.activity_interval + 1), (try f.actor(dave)).last_activity_at);
    _ = try f.apply(t0 + 2 * change.activity_interval + 1, .{ .observe = .{ .did = dave } });
    try testing.expectEqual(@as(u64, t0 + change.activity_interval + 1), (try f.actor(dave)).last_activity_at);
    try testing.expectEqual(@as(usize, 1), (try f.logged()).len);
}

test "a sighting still sees an actor that left, a label that lapsed and a domain that was blocked" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .profile = .{ .did = dave, .handle = "dave.spam.example" } });
    _ = try f.apply(t0, .{ .profile = .{ .did = erin, .handle = "erin.test" } });
    for ([_][]const u8{ dave, erin }) |did| _ = try f.apply(t0 + 1, .{ .observe = .{ .did = did } });

    _ = try f.apply(t0 + 2, .{ .domain_blocked = .{ .domain = "spam.example" } });
    try testing.expect(!(try f.actor(dave)).hidden);
    try testing.expectEqual(@as(u64, 1), (try f.apply(t0 + 3, .{ .observe = .{ .did = dave } })).changed);
    try testing.expect((try f.actor(dave)).hidden);
    _ = try f.apply(t0 + 4, .{ .domain_unblocked = "spam.example" });
    try testing.expectEqual(@as(u64, 1), (try f.apply(t0 + 5, .{ .observe = .{ .did = dave } })).changed);
    try testing.expect(!(try f.actor(dave)).hidden);

    _ = try f.apply(t0 + 6, .{ .label = .{ .did = erin, .source = moderation.bsky_moderation, .value = "!hide", .created = "2026-11-01T00:00:00Z", .expires = "2026-11-08T00:00:00Z", .sequence = 1 } });
    try testing.expect((try f.actor(erin)).hidden);
    try testing.expectEqual(@as(u64, 0), (try f.apply(t0 + 7, .{ .observe = .{ .did = erin } })).changed);
    try testing.expectEqual(@as(u64, 1), (try f.apply(t0 + 86_400 * 30, .{ .observe = .{ .did = erin } })).changed);
    try testing.expect(!(try f.actor(erin)).hidden);

    _ = try f.apply(t0 + 86_400 * 30, .{ .gone = .{ .did = dave, .status = .deactivated } });
    try testing.expect(!(try f.apply(t0 + 86_400 * 30 + 1, .{ .observe = .{ .did = dave } })).applied);
    try testing.expect(!(try f.actor(dave)).live());
    _ = try f.apply(t0 + 86_400 * 30 + 2, .{ .reactivated = dave });
    try testing.expect((try f.apply(t0 + 86_400 * 30 + 3, .{ .observe = .{ .did = dave } })).applied);
}

test "a sighting in a batch that is dropped is not remembered" {
    var f = try Fixture.init(.{ .seen_limit = 2 });
    defer f.deinit();
    {
        const batch = try f.store.begin(t0);
        try testing.expect((try batch.apply(.{ .observe = .{ .did = dave } })).created);
        try testing.expect(!(try batch.apply(.{ .observe = .{ .did = dave } })).created);
        batch.abort();
    }
    try testing.expect((try f.apply(t0 + 1, .{ .observe = .{ .did = dave } })).created);

    var buf: [32]u8 = undefined;
    for (0..8) |n| _ = try f.apply(t0 + 2, .{ .observe = .{ .did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>24}", .{n}) } });
    for (0..8) |n| {
        const outcome = try f.apply(t0 + 3, .{ .observe = .{ .did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>24}", .{n}) } });
        try testing.expect(outcome.applied and !outcome.created);
    }
    try testing.expectEqual(@as(usize, 9), (try f.logged()).len);

    try f.reopen();
    try testing.expect(!(try f.apply(t0 + 4, .{ .observe = .{ .did = dave } })).created);
    try testing.expectEqual(@as(u64, t0 + 1), (try f.actor(dave)).last_activity_at);
}

test "the quality walk queues ranked actors best first and starts again at the end" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const dids = [_][]const u8{ alice, bob, carol, "did:plc:dave", "did:plc:erin" };
    const scores = [_]f32{ 300, 500, 0, 100, 400 };
    for (dids, scores, 0..) |did, value, index| {
        _ = try f.apply(t0, .{ .profile = .{ .did = did, .handle = if (index == 3) "" else "h.test" } });
        _ = try f.apply(t0, .{ .enrichment = .{ .did = did, .source = .bsky, .quality_score = value } });
    }
    try testing.expectEqual(@as(u64, 0), try f.count(.refresh));
    const logged = (try f.logged()).len;

    const step: change.Change = .{ .refresh_walk = .{ .pool = .quality, .limit = 2, .scan = 2 } };
    const first = try f.store.begin(t0 + 100);
    _ = try first.apply(step);
    try testing.expectEqual(change.Walked{ .examined = 2, .queued = 2 }, first.walked(.quality));
    try first.commit();
    try testing.expectEqual(@as(u64, t0 + 100), (try f.actor(bob)).refresh_due);
    try testing.expectEqual(@as(u64, t0 + 100), (try f.actor("did:plc:erin")).refresh_due);
    try testing.expectEqual(@as(u64, 0), (try f.actor(alice)).refresh_due);
    try testing.expectEqual(logged, (try f.logged()).len);

    const second = try f.store.begin(t0 + 160);
    _ = try second.apply(step);
    try testing.expectEqual(change.Walked{ .examined = 1, .queued = 1, .wrapped = 1 }, second.walked(.quality));
    try second.commit();
    try testing.expectEqual(@as(u64, t0 + 160), (try f.actor(alice)).refresh_due);
    const state = try read.walkState(f.store.db(), f.arena.allocator(), .walk_quality);
    try testing.expectEqual(@as(u64, 1), state.passes);
    try testing.expectEqual(@as(u64, 60), state.last_pass_seconds);
    try testing.expectEqualStrings("", state.position);

    const third = try f.store.begin(t0 + 200);
    _ = try third.apply(step);
    try testing.expectEqual(change.Walked{ .examined = 2, .queued = 0 }, third.walked(.quality));
    try third.commit();

    try f.reopen();
    const resumed = try f.store.begin(t0 + 300);
    _ = try resumed.apply(step);
    try testing.expectEqual(change.Walked{ .examined = 1, .queued = 0, .wrapped = 1 }, resumed.walked(.quality));
    try resumed.commit();
}

test "the rest walk covers live actors with a handle that the quality walk does not" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "ranked.test" } });
    _ = try f.apply(t0, .{ .enrichment = .{ .did = alice, .source = .bsky, .quality_score = 300 } });
    _ = try f.apply(t0, .{ .profile = .{ .did = bob, .handle = "unscored.test" } });
    _ = try f.apply(t0, .{ .refresh_complete = .{ .did = bob } });
    _ = try f.apply(t0, .{ .profile = .{ .did = carol, .handle = "hidden.test" } });
    _ = try f.apply(t0, .{ .enrichment = .{ .did = carol, .source = .bsky, .quality_score = 200 } });
    _ = try f.apply(t0, .{ .override_set = .{ .did = carol, .action = .hide } });
    _ = try f.apply(t0, .{ .observe = .{ .did = "did:plc:nohandle" } });
    _ = try f.apply(t0, .{ .profile = .{ .did = "did:plc:gone", .handle = "gone.test" } });
    _ = try f.apply(t0, .{ .gone = .{ .did = "did:plc:gone", .status = .deleted } });

    const batch = try f.store.begin(t0 + 5);
    _ = try batch.apply(.{ .refresh_walk = .{ .pool = .rest, .limit = 10, .scan = 100 } });
    try testing.expectEqual(change.Walked{ .examined = 5, .queued = 2, .wrapped = 1 }, batch.walked(.rest));
    try batch.commit();
    try testing.expectEqual(@as(u64, t0 + 5), (try f.actor(bob)).refresh_due);
    try testing.expectEqual(@as(u64, t0 + 5), (try f.actor(carol)).refresh_due);
    try testing.expectEqual(@as(u64, 0), (try f.actor(alice)).refresh_due);
    try testing.expectEqual(@as(u64, 2), try f.count(.refresh));
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
    try batch.setCursor(.{ .control_policy = .{ .generation = 7, .at = 0 } });
    try batch.setCursor(.{ .control_policy = .{ .generation = 7, .at = 12 } });
    try batch.setCursor(.{ .control_policy = .{ .generation = 7, .at = 11 } });
    try batch.setCursor(.{ .control_requests = .{ .generation = 7, .at = 300 } });
    try batch.setCursor(.{ .control_requests = .{ .generation = 7, .at = 299 } });
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
    try testing.expectEqual(@as(?u64, 7), cursors.control_generation);
    try testing.expectEqual(@as(?u64, 12), cursors.control_policy);
    try testing.expectEqual(@as(?u64, 300), cursors.control_requests);
    try testing.expectEqual(@as(u64, 4), cursors.next_change);

    try f.reopen();
    try testing.expectEqual(@as(?u64, 42), (try f.store.cursors(mem)).relay);
    _ = try f.apply(t0 + 2, .{ .profile = .{ .did = alice, .display_name = "A" } });
    try testing.expectEqual(@as(u64, 4), (try f.logged())[3].sequence);
}

test "control positions hold within a generation and are dropped when the generation changes" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();
    try f.store.setCursor(.{ .control_policy = .{ .generation = 7, .at = 12 } });
    try f.store.setCursor(.{ .control_requests = .{ .generation = 7, .at = 300 } });
    try f.store.setCursor(.{ .control_policy = .{ .generation = 7, .at = 5 } });
    var cursors = try f.store.cursors(mem);
    try testing.expectEqual(@as(?u64, 12), cursors.control_policy);

    // Another control database: its revision 3 replaces 12, and the request
    // sequence taken from the old one goes with it, in the same commit.
    try f.store.setCursor(.{ .control_policy = .{ .generation = 9, .at = 3 } });
    cursors = try f.store.cursors(mem);
    try testing.expectEqual(@as(?u64, 9), cursors.control_generation);
    try testing.expectEqual(@as(?u64, 3), cursors.control_policy);
    try testing.expectEqual(@as(?u64, null), cursors.control_requests);

    // And the other way round, from the request side.
    try f.store.setCursor(.{ .control_requests = .{ .generation = 9, .at = 40 } });
    try f.store.setCursor(.{ .control_requests = .{ .generation = 2, .at = 1 } });
    cursors = try f.store.cursors(mem);
    try testing.expectEqual(@as(?u64, 2), cursors.control_generation);
    try testing.expectEqual(@as(?u64, null), cursors.control_policy);
    try testing.expectEqual(@as(?u64, 1), cursors.control_requests);
    try f.reopen();
    cursors = try f.store.cursors(mem);
    try testing.expectEqual(@as(?u64, 2), cursors.control_generation);
    try testing.expectEqual(@as(?u64, 1), cursors.control_requests);
}

test "an owed sweep is marked with its cause and cleared by the step that finishes it" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const mem = f.arena.allocator();
    _ = try f.apply(t0, .{ .profile = .{ .did = alice, .handle = "a.spam.example" } });
    _ = try f.apply(t0, .{ .profile = .{ .did = bob, .handle = "b.spam.example" } });

    var batch = try f.store.begin(t0 + 1);
    _ = try batch.apply(.{ .domain_blocked = .{ .domain = "Spam.Example" } });
    try batch.oweSweep("Spam.Example");
    batch.abort();
    try testing.expectEqual(@as(usize, 0), (try f.store.owedSweeps(mem)).len);

    batch = try f.store.begin(t0 + 1);
    _ = try batch.apply(.{ .domain_blocked = .{ .domain = "Spam.Example" } });
    try batch.oweSweep("Spam.Example");
    try batch.commit();
    try testing.expectEqualStrings("spam.example", (try f.store.owedSweeps(mem))[0]);
    try testing.expectEqual(@as(usize, 1), (try read.blockedDomains(f.store.db(), mem)).len);

    // One step short of the end leaves the mark; a reopened store still
    // lists it, and a sweep from the top clears it with its last step.
    var sweep: Store.Sweep = .{};
    defer sweep.deinit(testing.allocator);
    try f.store.sweepDomain(&sweep, "spam.example", t0 + 2, 1);
    try testing.expect(!sweep.done());
    try testing.expect((try f.actor(alice)).hidden and !(try f.actor(bob)).hidden);
    try f.reopen();
    try testing.expectEqualStrings("spam.example", (try f.store.owedSweeps(mem))[0]);

    var again: Store.Sweep = .{};
    defer again.deinit(testing.allocator);
    while (!again.done()) {
        try testing.expectEqual(@as(usize, 1), (try f.store.owedSweeps(mem)).len);
        try f.store.sweepDomain(&again, "spam.example", t0 + 3, 1);
    }
    try testing.expectEqual(@as(usize, 0), (try f.store.owedSweeps(mem)).len);
    try testing.expect((try f.actor(alice)).hidden and (try f.actor(bob)).hidden);
    try f.reopen();
    try testing.expectEqual(@as(usize, 0), (try f.store.owedSweeps(mem)).len);
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

test "a trim starts at the floor and reads nothing below it" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    var buf: [32]u8 = undefined;
    for (0..10) |n| _ = try f.apply(t0, .{ .observe = .{ .did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>4}", .{n}) } });
    try testing.expectEqual(@as(u64, 6), (try f.store.trimChanges(6, 100)).floor);

    // An entry no trim leaves behind, put back by hand: a trim that still
    // walked the dead range from 0 would find it and remove it.
    var key: keys.Buf = undefined;
    try f.store.db().put(keys.change(&key, 2), "did:plc:0001");
    try f.store.db().commit();
    const trimmed = try f.store.trimChanges(9, 100);
    try testing.expectEqual(@as(u64, 3), trimmed.removed);
    try testing.expectEqual(@as(u64, 9), trimmed.floor);
    try testing.expectEqual(@as(u64, 3), try f.count(.change));
    try testing.expect(try f.store.db().get(keys.change(&key, 2)) != null);
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

/// Has the writer compact and collect every table once the scan has
/// delivered `at` entries, so the page in flight loses its tables.
const Collected = struct {
    writer: *Store,
    reader: *Reader,
    at: u64,
    entries: u64 = 0,

    pub fn entry(self: *Collected, _: []const u8, _: []const u8) !void {
        self.entries += 1;
        if (self.entries != self.at) return;
        try self.writer.maintainer.settle();
        self.reader.handle.objects.deinit();
    }
};

test "a limited scan delivers no more than its limit when a table is collected mid-page" {
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

    var collected: Collected = .{ .writer = writer, .reader = reader, .at = 30 };
    var position: scan.Position = .{};
    defer position.deinit(testing.allocator);
    const stats = try reader.scanRange(&position, .actor, .{ .page = 50, .refresh = false, .limit = 50 }, &collected);
    try testing.expectEqual(@as(u64, 1), stats.missing);
    try testing.expectEqual(@as(u64, 50), collected.entries);
    try testing.expectEqual(@as(u64, 50), stats.entries);
    try testing.expect(!position.done);
    try testing.expectEqualStrings("did:plc:0049", try keys.parseActor(position.next.items[0 .. position.next.items.len - 1]));
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

test "the actor walk pages every record of any status in DID order" {
    var f = try Fixture.init(.{});
    defer f.deinit();
    const dids = [_][]const u8{ "did:plc:b", "did:web:example.com", "did:plc:a", "did:plc:ab", "did:plc:c" };
    for (dids) |did| _ = try f.apply(t0, .{ .profile = .{ .did = did, .handle = "h.test" } });
    _ = try f.apply(t0, .{ .gone = .{ .did = "did:plc:ab", .status = .deleted } });
    _ = try f.apply(t0, .{ .gone = .{ .did = "did:plc:c", .status = .deactivated } });
    const arena = f.arena.allocator();

    try expectDids(&.{ "did:plc:a", "did:plc:ab", "did:plc:b", "did:plc:c", "did:web:example.com" }, try f.store.walk(arena, "", 10));
    try expectDids(&.{ "did:plc:a", "did:plc:ab" }, try f.store.walk(arena, "", 2));
    try expectDids(&.{ "did:plc:b", "did:plc:c" }, try f.store.walk(arena, "did:plc:ab", 2));
    try expectDids(&.{"did:web:example.com"}, try f.store.walk(arena, "did:plc:c", 2));
    try expectDids(&.{}, try f.store.walk(arena, "did:web:example.com", 2));
    try expectDids(&.{ "did:plc:ab", "did:plc:b" }, try f.store.walk(arena, "did:plc:aa", 2));
    try expectDids(&.{}, try f.store.walk(arena, "", 0));
    try testing.expectError(error.InvalidKeyPart, f.store.walk(arena, "did:plc:a\x00", 2));

    _ = try f.apply(t0 + 10, .{ .tombstones_pruned = .{ .before = t0 + 5, .limit = 10 } });
    try expectDids(&.{ "did:plc:a", "did:plc:b", "did:web:example.com" }, try f.store.walk(arena, "", 10));

    const reader = try Reader.open(testing.allocator, testing.io, f.tmp.dir, "store", .{});
    defer reader.close();
    try expectDids(&.{ "did:plc:b", "did:web:example.com" }, try reader.walk(arena, "did:plc:a", 10));
}
