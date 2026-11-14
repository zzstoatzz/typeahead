const std = @import("std");
const store_mod = @import("../store/root.zig");
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
    reader: ?*store_mod.Reader,
    signals: intake_mod.Signals = .{},
    ignored: std.atomic.Value(u64) = .init(0),
    read_secret: ?[]const u8 = secret,

    fn open(f: *Fixture) !Surface {
        return .{ .f = f, .reader = try store_mod.Reader.open(testing.allocator, testing.io, f.tmp.dir, "store", f.owner.options.store.layout) };
    }

    fn close(self: *Surface) void {
        if (self.reader) |reader| reader.close();
    }

    fn send(self: *Surface, method: std.http.Method, target: []const u8, authorization: ?[]const u8) !http.Response {
        const context: http.Context = .{ .owner = self.f.owner, .reader = self.reader, .intake = &self.signals, .labels_ignored = &self.ignored, .read_secret = self.read_secret };
        return http.respond(&context, self.f.arena.allocator(), method, target, authorization);
    }

    fn get(self: *Surface, target: []const u8, authorization: ?[]const u8) !http.Response {
        return self.send(.GET, target, authorization);
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

test "the read endpoints answer 503 and health answers while no reader is open" {
    const f = try seeded();
    defer f.destroy();
    var surface: Surface = .{ .f = f, .reader = null };
    try testing.expectEqual(std.http.Status.service_unavailable, (try surface.get("/store/actor?did=" ++ alice, bearer)).status);
    try testing.expectEqual(std.http.Status.unauthorized, (try surface.get("/store/actor?did=" ++ alice, null)).status);
    try testing.expectEqual(std.http.Status.ok, (try surface.get("/health", null)).status);
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
    try testing.expectEqual(std.http.Status.method_not_allowed, (try surface.send(.POST, "/store/actor?did=" ++ alice, bearer)).status);
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
