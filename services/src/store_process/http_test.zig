const std = @import("std");
const store_mod = @import("../store/root.zig");
const bridge = @import("bridge.zig");
const http = @import("http.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const harness = @import("testing.zig");
const Fixture = harness.Fixture;
const testing = std.testing;

const alice = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const bob = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";
const secret = "s3cret";
const bearer = "Bearer " ++ secret;

const Surface = struct {
    f: *Fixture,
    signals: intake_mod.Signals = .{},
    ignored: std.atomic.Value(u64) = .init(0),
    read_secret: ?[]const u8 = secret,

    fn get(self: *Surface, target: []const u8, authorization: ?[]const u8) !http.Response {
        const context: http.Context = .{ .owner = self.f.owner, .intake = &self.signals, .labels_ignored = &self.ignored, .read_secret = self.read_secret };
        return http.respond(&context, self.f.arena.allocator(), .GET, target, authorization);
    }

    fn parsed(self: *Surface, comptime T: type, target: []const u8) !T {
        const response = try self.get(target, bearer);
        try testing.expectEqual(std.http.Status.ok, response.status);
        return std.json.parseFromSliceLeaky(T, self.f.arena.allocator(), response.body, .{ .ignore_unknown_fields = true });
    }
};

fn seeded() !*Fixture {
    const f = try Fixture.create(.{ .health_seconds = 0 });
    errdefer f.destroy();
    _ = try f.commit(&.{
        .{ .profile = .{ .did = alice, .handle = "Alice.Example", .display_name = "Alice", .pds = "https://pds.example", .enqueue_identity = true } },
        .{ .label = .{ .did = alice, .source = store_mod.moderation.bsky_moderation, .value = "spam", .created = "2026-11-01T00:00:00.000Z", .expires = "2027-01-01T00:00:00.000Z", .sequence = 4 } },
        .{ .observe = .{ .did = bob } },
        .{ .gone = .{ .did = bob, .status = .deleted } },
    }, .{ .relay = 77 });
    return f;
}

test "the read endpoints are off without a secret and closed without the bearer" {
    const f = try seeded();
    defer f.destroy();
    var surface: Surface = .{ .f = f };
    try testing.expectEqual(std.http.Status.unauthorized, (try surface.get("/store/actor?did=" ++ alice, null)).status);
    try testing.expectEqual(std.http.Status.unauthorized, (try surface.get("/store/actor?did=" ++ alice, "Bearer wrong!")).status);
    try testing.expectEqual(std.http.Status.unauthorized, (try surface.get("/store/actor?did=" ++ alice, secret)).status);
    try testing.expectEqual(std.http.Status.unauthorized, (try surface.get("/store/nothing", null)).status);
    try testing.expectEqual(std.http.Status.not_found, (try surface.get("/store/nothing", bearer)).status);
    try testing.expectEqual(std.http.Status.not_found, (try surface.get("/admin/ingest", bearer)).status);
    try testing.expectEqual(std.http.Status.ok, (try surface.get("/health", null)).status);

    surface.read_secret = null;
    try testing.expectEqual(std.http.Status.not_found, (try surface.get("/store/actor?did=" ++ alice, bearer)).status);
    try testing.expect(!http.authorized("", "Bearer x"));
    try testing.expect(http.authorized("", "Bearer "));
}

test "an actor is read by DID, by handle without case, and in a batch" {
    const f = try seeded();
    defer f.destroy();
    var surface: Surface = .{ .f = f };
    const One = struct { actor: http.ActorBody };
    const by_did = (try surface.parsed(One, "/store/actor?did=did%3Aplc%3Aaaaaaaaaaaaaaaaaaaaaaaaa")).actor;
    try testing.expectEqualStrings(alice, by_did.did);
    try testing.expectEqualStrings("live", by_did.status);
    try testing.expectEqualStrings("Alice.Example", by_did.handle);
    try testing.expectEqualStrings("https://pds.example", by_did.pds);
    try testing.expect(by_did.hidden and by_did.identity_pending);
    try testing.expectEqual(@as(usize, 1), by_did.labels.len);
    try testing.expectEqualStrings("spam", by_did.labels[0].val);
    try testing.expectEqualStrings("2027-01-01T00:00:00.000Z", by_did.labels[0].exp.?);
    try testing.expect(by_did.labels[0].cid == null and by_did.labels[0].neg == null);

    const Many = struct { actors: []const ?http.ActorBody };
    const by_handle = try surface.parsed(Many, "/store/handle?handle=alice.EXAMPLE");
    try testing.expectEqual(@as(usize, 1), by_handle.actors.len);
    try testing.expectEqualStrings(alice, by_handle.actors[0].?.did);
    try testing.expectEqual(@as(usize, 0), (try surface.parsed(Many, "/store/handle?handle=nobody.example")).actors.len);

    const batch = try surface.parsed(Many, "/store/actors?did=" ++ bob ++ "&did=did:plc:unknown&did=" ++ alice);
    try testing.expectEqual(@as(usize, 3), batch.actors.len);
    try testing.expectEqualStrings("deleted", batch.actors[0].?.status);
    try testing.expect(batch.actors[0].?.deleted_at == f.now());
    try testing.expect(batch.actors[1] == null);
    try testing.expectEqualStrings(alice, batch.actors[2].?.did);

    try testing.expectEqual(std.http.Status.not_found, (try surface.get("/store/actor?did=did:plc:unknown", bearer)).status);
    try testing.expectEqual(std.http.Status.bad_request, (try surface.get("/store/actor", bearer)).status);
    try testing.expectEqual(std.http.Status.bad_request, (try surface.get("/store/actors", bearer)).status);
}

test "the change log is read from a position, and a named consumer holds trimming back" {
    const f = try seeded();
    defer f.destroy();
    var surface: Surface = .{ .f = f };
    const first = try surface.parsed(http.ChangesBody, "/store/changes?from=0&limit=1");
    try testing.expectEqual(@as(usize, 1), first.entries.len);
    try testing.expectEqual(@as(u64, 1), first.entries[0].seq);
    try testing.expectEqualStrings(alice, first.entries[0].did);
    try testing.expectEqual(@as(u64, 2), first.next);
    try testing.expectEqual(@as(u64, 3), first.head);

    const rest = try surface.parsed(http.ChangesBody, "/store/changes?from=2&consumer=compare");
    try testing.expectEqual(@as(usize, 1), rest.entries.len);
    try testing.expectEqualStrings(bob, rest.entries[0].did);
    try testing.expectEqual(@as(u64, 3), rest.next);
    const idle = try surface.parsed(http.ChangesBody, "/store/changes?from=3");
    try testing.expectEqual(@as(usize, 0), idle.entries.len);
    try testing.expectEqual(@as(u64, 3), idle.next);

    var buf: [8]owner_mod.Consumer = undefined;
    const consumers = f.owner.consumerList(&buf);
    try testing.expectEqualStrings("compare", consumers[0].name);
    try testing.expectEqual(@as(u64, 2), consumers[0].position);

    const store = try f.owner.acquire();
    _ = try store.trimChanges(3, 100);
    f.owner.release();
    const trimmed = try surface.parsed(http.ChangesBody, "/store/changes?from=1");
    try testing.expectEqual(@as(?u64, 3), trimmed.trimmed);
    try testing.expectEqual(@as(u64, 3), trimmed.next);
    try testing.expectEqual(std.http.Status.bad_request, (try surface.get("/store/changes?from=x", bearer)).status);
    try testing.expectEqual(std.http.Status.bad_request, (try surface.get("/store/changes?limit=5000", bearer)).status);
}

test "health carries the store's numbers" {
    const f = try seeded();
    defer f.destroy();
    f.owner.noteConsumer("bridge", 2);
    _ = f.owner.turn(0);
    var surface: Surface = .{ .f = f };
    surface.signals.handed_cursor.store(80, .monotonic);
    const response = try surface.get("/health", null);
    const body = try std.json.parseFromSliceLeaky(http.HealthBody, f.arena.allocator(), response.body, .{ .ignore_unknown_fields = true });
    try testing.expectEqualStrings("store", body.role);
    try testing.expectEqual(@as(?u64, 77), body.relay.cursor);
    try testing.expectEqual(@as(i64, 80), body.relay.handed_cursor);
    try testing.expectEqual(@as(u64, 3), body.change_log.head);
    try testing.expectEqualStrings("bridge", body.change_log.consumers[0].name);
    try testing.expectEqual(@as(u64, 1), body.queues.identity_pending);
    try testing.expectEqual(@as(u64, 1), body.queues.tombstones);
    try testing.expectEqual(@as(u64, 2), body.actors.created);
    try testing.expectEqual(@as(i64, 1), body.actors.live_delta);
    try testing.expectEqual(@as(u64, 1), body.writes.commits);
    try testing.expectEqual(f.now(), body.store.last_commit_at);
    try testing.expectEqual(f.now(), body.store.taken_at);
    try testing.expect(body.store.levels.len > 0 and body.store.disk_bytes > 0 and body.store.disk_files > 0);
    try testing.expect(!body.store.stalled);
}

const CountActors = struct {
    live: usize = 0,
    total: usize = 0,

    pub fn entry(self: *CountActors, _: []const u8, value: []const u8) !void {
        var arena: std.heap.ArenaAllocator = .init(testing.allocator);
        defer arena.deinit();
        const actor = try store_mod.Actor.decode(arena.allocator(), value);
        self.total += 1;
        self.live += @intFromBool(actor.live());
    }
};

test "the bridge feed follows the change log, acknowledges durably and resyncs from a scan" {
    const f = try seeded();
    defer f.destroy();
    var running = try harness.Running.start(f);
    defer running.stop();
    const feed: bridge.Feed = .{ .owner = f.owner };
    const arena = f.arena.allocator();
    try testing.expectEqual(@as(?u64, null), try feed.position(arena));
    const page = try feed.poll(arena, 0, 10);
    try testing.expectEqual(@as(usize, 2), page.entries.len);
    const actors = try feed.actors(arena, &.{ page.entries[0].did, page.entries[1].did, "did:plc:unknown" });
    try testing.expect(actors[0].?.live() and !actors[1].?.live() and actors[2] == null);
    try feed.ack(testing.allocator, page.entries[1].sequence + 1);
    try testing.expectEqual(@as(?u64, 3), try feed.position(arena));

    const scan = try feed.openScan(testing.allocator);
    var position: store_mod.scan.Position = .{};
    defer position.deinit(testing.allocator);
    var counted: CountActors = .{};
    _ = try scan.scanRange(&position, .actor, .{}, &counted);
    scan.close();
    try testing.expectEqual(@as(usize, 2), counted.total);
    try testing.expectEqual(@as(usize, 1), counted.live);

    running.stop();
    const store = try f.owner.acquire();
    _ = try store.trimChanges(3, 100);
    f.owner.release();
    try testing.expectEqual(@as(u64, 3), (try feed.poll(arena, 1, 10)).resync);

    var buf: [8]owner_mod.Consumer = undefined;
    try testing.expectEqualStrings(bridge.consumer_name, f.owner.consumerList(&buf)[0].name);
}
