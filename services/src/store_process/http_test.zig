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
    reader: *store_mod.Reader,
    signals: intake_mod.Signals = .{},
    ignored: std.atomic.Value(u64) = .init(0),
    read_secret: ?[]const u8 = secret,

    fn open(f: *Fixture) !Surface {
        return .{ .f = f, .reader = try store_mod.Reader.open(testing.allocator, testing.io, f.tmp.dir, "store", f.owner.options.store.layout) };
    }

    fn close(self: *Surface) void {
        self.reader.close();
    }

    fn send(self: *Surface, method: std.http.Method, target: []const u8, authorization: ?[]const u8, body: []const u8) !http.Response {
        const context: http.Context = .{ .owner = self.f.owner, .reader = self.reader, .intake = &self.signals, .labels_ignored = &self.ignored, .read_secret = self.read_secret };
        return http.respond(&context, self.f.arena.allocator(), method, target, authorization, body);
    }

    fn get(self: *Surface, target: []const u8, authorization: ?[]const u8) !http.Response {
        return self.send(.GET, target, authorization, "");
    }

    fn parsed(self: *Surface, comptime T: type, target: []const u8) !T {
        const response = try self.get(target, bearer);
        try testing.expectEqual(std.http.Status.ok, response.status);
        return std.json.parseFromSliceLeaky(T, self.f.arena.allocator(), response.body, .{ .ignore_unknown_fields = true });
    }

    fn posted(self: *Surface, target: []const u8, body: []const u8) !http.ActorsBody {
        const response = try self.send(.POST, target, bearer, body);
        try testing.expectEqual(std.http.Status.ok, response.status);
        return std.json.parseFromSliceLeaky(http.ActorsBody, self.f.arena.allocator(), response.body, .{});
    }

    fn status(self: *Surface, target: []const u8, body: []const u8) !std.http.Status {
        return (try self.send(.POST, target, bearer, body)).status;
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
    var surface = try Surface.open(f);
    defer surface.close();
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

test "an actor is read by DID and by handle without case" {
    const f = try seeded();
    defer f.destroy();
    var surface = try Surface.open(f);
    defer surface.close();
    const One = struct { actor: http.ActorBody };
    const by_did = (try surface.parsed(One, "/store/actor?did=did%3Aplc%3Aaaaaaaaaaaaaaaaaaaaaaaaa")).actor;
    try testing.expectEqualStrings(alice, by_did.did);
    try testing.expectEqualStrings("live", by_did.status);
    try testing.expectEqualStrings("Alice.Example", by_did.handle);
    try testing.expectEqualStrings("https://pds.example", by_did.pds);
    try testing.expect(by_did.hidden and by_did.identity_pending);
    try testing.expectEqual(@as(usize, 1), by_did.labels.len);
    try testing.expectEqualStrings("spam", by_did.labels[0].value);
    try testing.expectEqualStrings(store_mod.moderation.bsky_moderation, by_did.labels[0].source);
    try testing.expectEqualStrings("2027-01-01T00:00:00.000Z", by_did.labels[0].expires);
    try testing.expect(by_did.labels[0].cid.len == 0 and !by_did.labels[0].negated);

    const Many = struct { actors: []const ?http.ActorBody };
    const by_handle = try surface.parsed(Many, "/store/handle?handle=alice.EXAMPLE");
    try testing.expectEqual(@as(usize, 1), by_handle.actors.len);
    try testing.expectEqualStrings(alice, by_handle.actors[0].?.did);
    try testing.expectEqual(@as(usize, 0), (try surface.parsed(Many, "/store/handle?handle=nobody.example")).actors.len);

    try testing.expectEqual(std.http.Status.not_found, (try surface.get("/store/actor?did=did:plc:unknown", bearer)).status);
    try testing.expectEqual(std.http.Status.bad_request, (try surface.get("/store/actor", bearer)).status);
    try testing.expectEqual(std.http.Status.method_not_allowed, (try surface.send(.POST, "/store/actor?did=" ++ alice, bearer, "")).status);
}

test "the change log is read from a position, and a named consumer holds trimming back" {
    const f = try seeded();
    defer f.destroy();
    var surface = try Surface.open(f);
    defer surface.close();
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
    var surface = try Surface.open(f);
    defer surface.close();
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

test "a batch answers in request order, null where the store holds nothing" {
    const f = try seeded();
    defer f.destroy();
    var surface = try Surface.open(f);
    defer surface.close();
    const batch = try surface.posted("/store/actors/batch", "{\"dids\":[\"" ++ bob ++ "\",\"did:plc:unknown\",\"" ++ alice ++ "\"]}");
    try testing.expectEqual(@as(usize, 3), batch.actors.len);
    try testing.expectEqualStrings(bob, batch.actors[0].?.did);
    try testing.expectEqualStrings("deleted", batch.actors[0].?.status);
    try testing.expectEqual(f.now(), batch.actors[0].?.deleted_at);
    try testing.expect(batch.actors[1] == null);
    try testing.expectEqualStrings(alice, batch.actors[2].?.did);
    try testing.expectEqualStrings("live", batch.actors[2].?.status);
    try testing.expectEqualStrings("Alice.Example", batch.actors[2].?.handle);
    try testing.expectEqual(f.now(), batch.actors[2].?.updated_at);
    try testing.expectEqual(@as(u64, 3), batch.next_change);

    const raw = (try surface.send(.POST, "/store/actors/batch", bearer, "{\"dids\":[\"did:plc:unknown\"]}")).body;
    try testing.expect(std.mem.startsWith(u8, raw, "{\"actors\":[null],"));
    const marker = try surface.posted("/store/actors/batch", "{\"dids\":[]}");
    try testing.expectEqual(@as(usize, 0), marker.actors.len);
    try testing.expectEqual(@as(u64, 3), marker.next_change);
}

test "a batch is bounded, well formed and behind the bearer" {
    const f = try seeded();
    defer f.destroy();
    var surface = try Surface.open(f);
    defer surface.close();
    var body: std.ArrayList(u8) = .empty;
    const arena = f.arena.allocator();
    try body.appendSlice(arena, "{\"dids\":[");
    for (0..501) |n| try body.print(arena, "{s}\"did:plc:n{d}\"", .{ if (n == 0) "" else ",", n });
    try body.appendSlice(arena, "]}");
    try testing.expectEqual(std.http.Status.bad_request, try surface.status("/store/actors/batch", body.items));
    try testing.expectEqual(std.http.Status.bad_request, try surface.status("/store/actors/batch", "{\"dids\":[\"\"]}"));
    try testing.expectEqual(std.http.Status.bad_request, try surface.status("/store/actors/batch", "not json"));
    try testing.expectEqual(std.http.Status.bad_request, try surface.status("/store/actors/batch", ""));
    try testing.expectEqual(std.http.Status.unauthorized, (try surface.send(.POST, "/store/actors/batch", null, "{\"dids\":[]}")).status);
    try testing.expectEqual(std.http.Status.unauthorized, (try surface.send(.POST, "/store/actors/page", "Bearer nope", "{\"limit\":1}")).status);
    try testing.expectEqual(std.http.Status.method_not_allowed, (try surface.get("/store/actors/batch", bearer)).status);
    try testing.expectEqual(std.http.Status.method_not_allowed, (try surface.get("/store/actors/page", bearer)).status);
    surface.read_secret = null;
    try testing.expectEqual(std.http.Status.not_found, try surface.status("/store/actors/page", "{\"limit\":1}"));
}

test "the page walk visits every actor of any status once, in ascending DID order" {
    const f = try seeded();
    defer f.destroy();
    const arena = f.arena.allocator();
    var expected: std.ArrayList([]const u8) = .empty;
    try expected.appendSlice(arena, &.{ alice, bob });
    for (0..23) |n| {
        const did = try std.fmt.allocPrint(arena, "did:plc:{c}{d:0>3}", .{ @as(u8, if (n % 2 == 0) 'c' else 'A'), n });
        try expected.append(arena, did);
        _ = try f.commit(&.{.{ .observe = .{ .did = did } }}, null);
        if (n % 5 == 0) _ = try f.commit(&.{.{ .gone = .{ .did = did, .status = .deactivated } }}, null);
    }
    std.mem.sort([]const u8, expected.items, {}, struct {
        fn before(_: void, x: []const u8, y: []const u8) bool {
            return std.mem.order(u8, x, y) == .lt;
        }
    }.before);

    var surface = try Surface.open(f);
    defer surface.close();
    var walked: std.ArrayList([]const u8) = .empty;
    var statuses: std.StringHashMapUnmanaged(void) = .empty;
    var after: []const u8 = "";
    while (true) {
        const body = try std.fmt.allocPrint(arena, "{{\"after_did\":\"{s}\",\"limit\":4}}", .{after});
        const reply = try surface.posted("/store/actors/page", body);
        try testing.expect(reply.actors.len <= 4);
        for (reply.actors) |actor| {
            try walked.append(arena, actor.?.did);
            try statuses.put(arena, actor.?.status, {});
        }
        try testing.expectEqual((try f.cursors()).next_change, reply.next_change);
        if (reply.actors.len < 4) break;
        after = reply.actors[3].?.did;
    }
    try testing.expectEqual(expected.items.len, walked.items.len);
    for (expected.items, walked.items) |want, got| try testing.expectEqualStrings(want, got);
    try testing.expectEqual(@as(u32, 3), statuses.count());

    const whole = try surface.posted("/store/actors/page", "{\"limit\":500}");
    try testing.expectEqual(expected.items.len, whole.actors.len);
    const tail = try surface.posted("/store/actors/page", "{\"after_did\":\"did:plc:zzzz\",\"limit\":500}");
    try testing.expectEqual(@as(usize, 0), tail.actors.len);
    const between = try surface.posted("/store/actors/page", "{\"after_did\":\"did:plc:B\",\"limit\":1}");
    try testing.expectEqualStrings(alice, between.actors[0].?.did);

    try testing.expectEqual(std.http.Status.bad_request, try surface.status("/store/actors/page", "{\"after_did\":\"\",\"limit\":0}"));
    try testing.expectEqual(std.http.Status.bad_request, try surface.status("/store/actors/page", "{\"after_did\":\"\",\"limit\":501}"));
    try testing.expectEqual(std.http.Status.bad_request, try surface.status("/store/actors/page", "{\"after_did\":\"\"}"));
}

test "a walk reads beside the owner without taking its lock" {
    const f = try seeded();
    defer f.destroy();
    var surface = try Surface.open(f);
    defer surface.close();
    _ = try f.owner.acquire();
    const held = try surface.posted("/store/actors/page", "{\"limit\":500}");
    f.owner.release();
    try testing.expectEqual(@as(usize, 2), held.actors.len);

    _ = try f.commit(&.{.{ .observe = .{ .did = "did:plc:later" } }}, null);
    const fresh = try surface.posted("/store/actors/page", "{\"limit\":500}");
    try testing.expectEqual(@as(usize, 3), fresh.actors.len);
}
