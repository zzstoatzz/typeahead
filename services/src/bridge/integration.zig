//! The bridge against the real `typeahead-tantivy` binary named by
//! TANTIVY_TEST_BIN, on loopback, from an in-memory store. Run with
//! `just --no-dotenv test-bridge`, which builds the binary first.

const std = @import("std");
const testing = std.testing;
const json = std.json;
const zat = @import("zat");
const bridge_module = @import("root.zig");
const Bridge = bridge_module.Bridge;
const source = bridge_module.source;
const wire = bridge_module.wire;
const doubles = bridge_module.testing;

const secret = "bridge-integration-secret";
const actor_count = 3000;

const Fixture = struct {
    gpa: std.mem.Allocator,
    io: std.Io,
    arena: std.heap.ArenaAllocator,
    tmp: testing.TmpDir,
    binary: []const u8,
    data: []const u8,
    public: []const u8,
    admin: []const u8,
    listen: []const u8,
    admin_listen: []const u8,
    environment: std.process.Environ.Map,
    http: zat.HttpTransport,
    endpoint: wire.Http,
    chaos: doubles.Chaos,
    store: doubles.MemoryStore,
    server: ?std.process.Child = null,

    fn init(self: *Fixture) !void {
        const gpa = testing.allocator;
        const io = testing.io;
        self.* = .{
            .gpa = gpa,
            .io = io,
            .arena = .init(gpa),
            .tmp = testing.tmpDir(.{}),
            .binary = undefined,
            .data = undefined,
            .public = undefined,
            .admin = undefined,
            .listen = undefined,
            .admin_listen = undefined,
            .environment = try testing.environ.createMap(gpa),
            .http = .init(io, gpa),
            .endpoint = undefined,
            .chaos = undefined,
            .store = .init(gpa),
        };
        const a = self.arena.allocator();
        self.binary = self.environment.get("TANTIVY_TEST_BIN") orelse {
            std.debug.print("TANTIVY_TEST_BIN must name a built typeahead-tantivy; `just --no-dotenv test-bridge` sets it\n", .{});
            return error.MissingTantivyTestBin;
        };
        try self.environment.put("ADMIN_SECRET", secret);
        _ = self.environment.swapRemove("TYPEAHEAD_TEST_CRASH");
        self.data = try std.fmt.allocPrint(a, ".zig-cache/tmp/{s}/data", .{self.tmp.sub_path});
        const public_port = try self.freePort();
        const admin_port = try self.freePort();
        self.listen = try std.fmt.allocPrint(a, "127.0.0.1:{d}", .{public_port});
        self.admin_listen = try std.fmt.allocPrint(a, "127.0.0.1:{d}", .{admin_port});
        self.public = try std.fmt.allocPrint(a, "http://{s}", .{self.listen});
        self.admin = try std.fmt.allocPrint(a, "http://{s}", .{self.admin_listen});
        self.endpoint = .{ .http = &self.http, .base_url = self.admin, .authorization = "Bearer " ++ secret };
        self.chaos = .{ .inner = self.endpoint.transport() };
    }

    fn deinit(self: *Fixture) void {
        self.stopServer();
        self.store.deinit();
        self.http.deinit();
        self.environment.deinit();
        self.tmp.cleanup();
        self.arena.deinit();
    }

    fn freePort(self: *Fixture) !u16 {
        var listener = try (std.Io.net.IpAddress{ .ip4 = .loopback(0) }).listen(self.io, .{ .reuse_address = true });
        defer listener.deinit(self.io);
        return listener.socket.address.getPort();
    }

    fn now(self: *Fixture) u64 {
        return @intCast(@divFloor(std.Io.Timestamp.now(self.io, .real).nanoseconds, std.time.ns_per_s));
    }

    /// An empty, verified, full-source index whose watermark is a day old,
    /// so the server is not ready until the bridge has made it fresh.
    fn importEmptyIndex(self: *Fixture) !void {
        const a = self.arena.allocator();
        const seed = try std.fmt.allocPrint(a, "{{\"type\":\"manifest\",\"build_id\":\"bridge-integration\",\"source_watermark\":{d}," ++
            "\"actor_count\":0,\"source_actor_count\":0,\"partial\":false}}\n" ++
            "{{\"type\":\"end\",\"build_id\":\"bridge-integration\",\"actor_count\":0," ++
            "\"actors_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}}\n", .{self.now() - 86_400});
        try self.tmp.dir.writeFile(self.io, .{ .sub_path = "seed.jsonl", .data = seed });
        const seed_path = try std.fmt.allocPrint(a, ".zig-cache/tmp/{s}/seed.jsonl", .{self.tmp.sub_path});
        var child = try std.process.spawn(self.io, .{
            .argv = &.{ self.binary, "import", "--data", self.data, "--input", seed_path },
            .stdout = .ignore,
            .stderr = .ignore,
        });
        const term = try child.wait(self.io);
        if (!term.success()) return error.ImportFailed;
    }

    fn startServer(self: *Fixture, crash_at: ?[]const u8) !void {
        std.debug.assert(self.server == null);
        var environment = try self.environment.clone(self.gpa);
        defer environment.deinit();
        if (crash_at) |point| try environment.put("TYPEAHEAD_TEST_CRASH", point);
        self.server = try std.process.spawn(self.io, .{
            .argv = &.{ self.binary, "serve", "--data", self.data, "--listen", self.listen, "--admin-listen", self.admin_listen },
            .environ_map = &environment,
            .stdout = .ignore,
            .stderr = .ignore,
        });
        for (0..400) |_| {
            if (self.status(self.public, "/health")) |code| {
                if (code == 200) return;
            } else |_| {}
            try self.io.sleep(.fromMilliseconds(25), .awake);
        }
        return error.ServerDidNotStart;
    }

    fn stopServer(self: *Fixture) void {
        if (self.server) |*child| child.kill(self.io);
        self.server = null;
    }

    /// Step until the server, started with a crash point, has aborted on
    /// the push. A first step can fail earlier, on a pooled connection to
    /// the previous server process, without reaching this one.
    fn stepUntilCrash(self: *Fixture, b: *Bridge) !void {
        for (0..5) |_| {
            try testing.expectError(error.IndexUnavailable, b.step());
            if (self.status(self.public, "/health")) |_| continue else |_| {}
            const term = try self.server.?.wait(self.io);
            self.server = null;
            try testing.expect(term != .exited);
            return;
        }
        return error.ServerDidNotCrash;
    }

    fn fetch(self: *Fixture, base: []const u8, path: []const u8) !zat.HttpTransport.FetchResult {
        const url = try std.fmt.allocPrint(self.arena.allocator(), "{s}{s}", .{ base, path });
        return self.http.fetch(.{ .url = url, .deadline_ns = 10 * std.time.ns_per_s });
    }

    fn status(self: *Fixture, base: []const u8, path: []const u8) !u16 {
        var result = try self.fetch(base, path);
        defer result.deinit(self.gpa);
        return @backingInt(result.status);
    }

    fn getJson(self: *Fixture, base: []const u8, path: []const u8) !json.Value {
        var result = try self.fetch(base, path);
        defer result.deinit(self.gpa);
        if (result.status != .ok) {
            std.debug.print("GET {s} answered {d}: {s}\n", .{ path, @backingInt(result.status), result.body });
            return error.UnexpectedStatus;
        }
        return json.parseFromSliceLeaky(json.Value, self.arena.allocator(), result.body, .{ .allocate = .alloc_always });
    }

    fn indexStatus(self: *Fixture) !json.ObjectMap {
        return (try self.getJson(self.admin, "/index/status")).object;
    }

    /// The public profile `/search` returns for `did` when asked for `term`,
    /// or null when the answer does not include it.
    fn find(self: *Fixture, term: []const u8, did: []const u8) !?json.ObjectMap {
        const path = try std.fmt.allocPrint(self.arena.allocator(), "/search?q={s}&limit=10", .{term});
        const answer = try self.getJson(self.public, path);
        for (answer.object.get("actors").?.array.items) |item| {
            if (std.mem.eql(u8, item.object.get("did").?.string, did)) return item.object;
        }
        return null;
    }

    fn id(self: *Fixture, n: usize) []const u8 {
        return std.fmt.allocPrint(self.arena.allocator(), "did:plc:bridge{d:0>5}", .{n}) catch @panic("out of memory");
    }

    fn make(self: *Fixture, n: usize, generation: usize) !source.Actor {
        const a = self.arena.allocator();
        return .{
            .handle = try std.fmt.allocPrint(a, "u{d:0>5}g{d}.bridge.test", .{ n, generation }),
            .display_name = try std.fmt.allocPrint(a, "Person{d:0>5}g{d}", .{ n, generation }),
            .avatar = "bafkreibridgeavatar",
            .pds = "https://pds.bridge.test",
            .created_at = "2026-11-10T00:00:00Z",
            .associated = if (n % 3 == 0) "{\"chat\":{\"allowIncoming\":\"all\"}}" else "",
            .followers_count = n,
            .posts_count = generation,
            .quality_score = @floatFromInt(1 + n % 97),
            .labels = if (n % 5 == 0) &.{.{
                .source = "did:plc:labeler",
                .uri = "did:plc:subject",
                .value = "bridge-test",
                .created = "2026-11-10T00:00:00Z",
                .expires = "2099-01-01T00:00:00Z",
            }} else &.{},
        };
    }

    fn bridge(self: *Fixture, transport: wire.Transport) Bridge {
        return Bridge.init(self.gpa, self.io, .{ .source_id = "integration-store" }, self.store.interface(), transport);
    }

    /// Every document the index holds, through /admin/source-page.
    fn indexedDids(self: *Fixture) !std.StringArrayHashMapUnmanaged(void) {
        const a = self.arena.allocator();
        const client: wire.Client = .{ .transport = self.endpoint.transport() };
        var all: std.StringArrayHashMapUnmanaged(void) = .empty;
        var after: []const u8 = "";
        while (true) {
            const page = try client.sourcePage(a, after, 500);
            if (page.dids.len == 0) return all;
            for (page.dids) |did| try all.put(a, did, {});
            after = page.dids[page.dids.len - 1];
        }
    }

    /// Every document hash, through /admin/source-state.
    fn documentHashes(self: *Fixture, dids: []const []const u8) ![]const ?[]const u8 {
        const a = self.arena.allocator();
        const client: wire.Client = .{ .transport = self.endpoint.transport() };
        const hashes = try a.alloc(?[]const u8, dids.len);
        var at: usize = 0;
        while (at < dids.len) : (at += 500) {
            const end = @min(dids.len, at + 500);
            const state = try client.sourceState(a, dids[at..end]);
            for (state.documents, hashes[at..end]) |document, *slot| slot.* = document.document_sha256;
        }
        return hashes;
    }

    /// What the index stores for each DID, read back through
    /// /admin/source-state with `fields`, against the store's record.
    fn expectStoredFieldsMatch(self: *Fixture) !void {
        const a = self.arena.allocator();
        const dids = self.store.actors.keys();
        const actors = self.store.actors.values();
        var at: usize = 0;
        while (at < dids.len) : (at += 500) {
            const end = @min(dids.len, at + 500);
            const url = try std.fmt.allocPrint(a, "{s}/admin/source-state", .{self.admin});
            var result = try self.http.fetch(.{
                .url = url,
                .method = .POST,
                .payload = try json.Stringify.valueAlloc(a, .{ .dids = dids[at..end], .fields = true }, .{}),
                .content_type = "application/json",
                .authorization = "Bearer " ++ secret,
                .deadline_ns = 30 * std.time.ns_per_s,
            });
            defer result.deinit(self.gpa);
            try testing.expectEqual(std.http.Status.ok, result.status);
            const answer = try json.parseFromSliceLeaky(json.Value, a, result.body, .{ .allocate = .alloc_always });
            for (answer.object.get("actors").?.array.items, actors[at..end]) |item, actor| {
                const fields = item.object.get("fields").?.object;
                const live = actor.status == .live and actor.handle.len > 0;
                try testing.expectEqual(!live, fields.get("deleted").?.bool);
                if (!live) continue;
                try testing.expectEqualStrings(actor.handle, fields.get("handle").?.string);
                try testing.expectEqualStrings(actor.pds, fields.get("pds").?.string);
                try testing.expectEqualStrings("pds.bridge.test", fields.get("pds_host").?.string);
                try testing.expectEqual(actor.hidden, fields.get("hidden").?.bool);
                const score: f32 = switch (fields.get("quality_score").?) {
                    .integer => |n| @floatFromInt(n),
                    .float => |x| @floatCast(x),
                    else => return error.TestUnexpectedResult,
                };
                try testing.expectEqual(actor.quality_score, score);
            }
        }
    }

    /// The index holds exactly the store's state. A full resync must find
    /// nothing to write: the server compares each document the bridge sends
    /// with the one it has stored, so an unchanged document count and
    /// unchanged hashes mean every stored document already equalled its
    /// source record. Counts and a sample of searches check the rest.
    fn expectIndexMatchesStore(self: *Fixture, live_bridge: *Bridge) !void {
        try settle(live_bridge);
        var live: u64 = 0;
        for (self.store.actors.values()) |actor| {
            if (actor.status == .live and actor.handle.len > 0) live += 1;
        }
        const indexed = try self.indexedDids();
        const hashes_before = try self.documentHashes(indexed.keys());
        const before = try self.indexStatus();
        const writes_before = before.get("expiry_coverage").?.object.get("source_document_changes").?.integer;

        live_bridge.requestResync();
        try settle(live_bridge);
        const after = try self.indexStatus();
        try testing.expectEqual(writes_before, after.get("expiry_coverage").?.object.get("source_document_changes").?.integer);
        const hashes_after = try self.documentHashes(indexed.keys());
        for (hashes_before, hashes_after) |x, y| try testing.expectEqualStrings(x.?, y.?);

        try self.expectStoredFieldsMatch();
        try testing.expectEqual(@as(i64, @intCast(live)), after.get("actor_count").?.integer);
        try testing.expectEqual(@as(i64, @intCast(indexed.count() - live)), after.get("tombstone_count").?.integer);
        for (self.store.actors.keys()) |did| try testing.expect(indexed.contains(did));

        const cursor = wire.parseCursor(self.arena.allocator(), try json.Stringify.valueAlloc(self.arena.allocator(), after.get("source_cursor").?, .{})).?;
        try testing.expectEqual(self.store.next_change, cursor.position);
        try testing.expectEqual(@as(?u64, self.store.next_change), self.store.bridge);

        for (self.store.actors.keys(), self.store.actors.values(), 0..) |did, actor, n| {
            if (n % 11 != 0 or actor.handle.len == 0) continue;
            const visible = actor.status == .live and !actor.hidden;
            const by_handle = try self.find(actor.handle, did);
            try testing.expectEqual(visible, by_handle != null);
            if (by_handle) |profile| {
                try testing.expectEqualStrings(actor.handle, profile.get("handle").?.string);
                try testing.expectEqualStrings(actor.display_name, profile.get("displayName").?.string);
                try testing.expectEqual(actor.labels.len, profile.get("labels").?.array.items.len);
                try testing.expectEqual(actor.associated.len > 0, profile.get("associated") != null);
            }
            try testing.expectEqual(visible, try self.find(actor.display_name, did) != null);
        }
    }
};

fn settle(b: *Bridge) !void {
    for (0..2000) |_| {
        if ((try b.step()).idle) return;
    }
    return error.DidNotSettle;
}

test "the bridge keeps a real Tantivy server equal to the store through changes, kills and a server restart" {
    var fixture: Fixture = undefined;
    try fixture.init();
    defer fixture.deinit();
    const f = &fixture;
    try f.importEmptyIndex();
    try f.startServer(null);
    try testing.expectEqual(@as(u16, 503), try f.status(f.public, "/ready"));

    for (0..actor_count) |n| try f.store.put(f.id(n), try f.make(n, 0));
    var b = f.bridge(f.endpoint.transport());
    defer b.deinit();

    // Bootstrap: the index is not fresh until one resync round completes.
    const first = try b.step();
    try testing.expect(first.resync_pages > 0 and !first.idle);
    try testing.expectEqual(@as(u16, 503), try f.status(f.public, "/ready"));
    try settle(&b);
    try testing.expectEqual(@as(u16, 200), try f.status(f.public, "/ready"));
    try f.expectIndexMatchesStore(&b);
    try testing.expectEqual(@as(u64, actor_count), b.health().applied);

    // Each kind of change, checked through /search.
    const did = f.id(1234);
    var subject = try f.make(1234, 0);
    try testing.expect(try f.find("u01234g0.bridge.test", did) != null);

    subject.handle = "renamed-by-bridge.bridge.test";
    try f.store.put(did, subject);
    try settle(&b);
    try testing.expect(try f.find("renamed-by-bridge.bridge.test", did) != null);
    try testing.expect(try f.find("u01234g0.bridge.test", did) == null);

    subject.display_name = "Quokkabridge";
    try f.store.put(did, subject);
    try settle(&b);
    try testing.expectEqualStrings("Quokkabridge", (try f.find("quokkabridge", did)).?.get("displayName").?.string);
    try testing.expect(try f.find("Person01234g0", did) == null);

    subject.hidden = true;
    try f.store.put(did, subject);
    try settle(&b);
    try testing.expect(try f.find("renamed-by-bridge.bridge.test", did) == null);

    subject.hidden = false;
    try f.store.put(did, subject);
    try settle(&b);
    try testing.expect(try f.find("renamed-by-bridge.bridge.test", did) != null);

    try f.store.delete(did);
    try settle(&b);
    try testing.expect(try f.find("renamed-by-bridge.bridge.test", did) == null);
    try testing.expect(try f.find("quokkabridge", did) == null);

    subject.handle = "recreated.bridge.test";
    try f.store.put(did, subject);
    try settle(&b);
    try testing.expect(try f.find("recreated.bridge.test", did) != null);
    try testing.expect(try f.find("renamed-by-bridge.bridge.test", did) == null);
    try f.expectIndexMatchesStore(&b);

    // The server dies right after committing a push: the acknowledgement is
    // lost, the commit is not.
    f.stopServer();
    try f.startServer("after_commit");
    try f.store.put(f.id(7), try f.make(7, 1));
    const stored = f.store.bridge.?;
    try f.stepUntilCrash(&b);
    try testing.expectEqual(stored, f.store.bridge.?);
    try f.startServer(null);
    const recovered = b.health().recovered;
    try settle(&b);
    try testing.expectEqual(recovered + 1, b.health().recovered);
    try testing.expect(try f.find("u00007g1.bridge.test", f.id(7)) != null);

    // The server dies just before committing: nothing landed.
    f.stopServer();
    try f.startServer("before_commit");
    try f.store.put(f.id(8), try f.make(8, 1));
    try f.stepUntilCrash(&b);
    try f.startServer(null);
    try testing.expect(try f.find("u00008g1.bridge.test", f.id(8)) == null);
    try settle(&b);
    try testing.expectEqual(recovered + 1, b.health().recovered);
    try testing.expect(try f.find("u00008g1.bridge.test", f.id(8)) != null);
    try f.expectIndexMatchesStore(&b);

    // Random changes while the bridge is killed before or after a random
    // request, and the server is killed once in the middle.
    var prng: std.Random.DefaultPrng = .init(0x1d3b21d9e);
    const random = prng.random();
    var generation: usize = 2;
    var kills: usize = 0;
    var failed_steps: usize = 0;
    for (0..60) |round| {
        for (0..random.uintLessThan(usize, 40)) |_| {
            const n = random.uintLessThan(usize, actor_count + 200);
            switch (random.uintLessThan(u8, 10)) {
                0, 1 => try f.store.delete(f.id(n)),
                2 => if (f.store.get(f.id(n))) |record| {
                    if (record.status != .live) f.store.prune(f.id(n));
                },
                else => {
                    var changed = try f.make(n, generation);
                    changed.hidden = random.uintLessThan(u8, 5) == 0;
                    try f.store.put(f.id(n), changed);
                    generation += 1;
                },
            }
        }
        if (round == 20) f.store.trim(f.store.next_change);
        if (round == 30) {
            f.stopServer();
            var orphan = f.bridge(f.endpoint.transport());
            defer orphan.deinit();
            try testing.expectError(error.IndexUnavailable, orphan.step());
            try f.startServer(null);
        }

        f.chaos.countdown = random.uintLessThan(usize, 10);
        f.chaos.after_effect = random.boolean();
        f.chaos.fired = false;
        f.store.fail_next_set_position = random.uintLessThan(u8, 8) == 0;
        var doomed = f.bridge(f.chaos.transport());
        defer doomed.deinit();
        if (round % 13 == 0) doomed.requestResync();
        for (0..3) |_| {
            _ = doomed.step() catch {
                failed_steps += 1;
                break;
            };
        }
        if (f.chaos.fired) kills += 1;
        f.chaos.countdown = null;
        f.store.fail_next_set_position = false;
    }
    try testing.expect(kills >= 20);

    var last = f.bridge(f.endpoint.transport());
    defer last.deinit();
    try f.expectIndexMatchesStore(&last);
    try testing.expectEqual(@as(u16, 200), try f.status(f.public, "/ready"));

    const final = try f.indexStatus();
    std.debug.print("bridge integration: {d} actors bootstrapped, {d} change-log entries, {d} bridge kills, {d} failed steps, " ++
        "server killed 5 times; index holds {d} live and {d} tombstones at revision {d}; ready={any} sync_lag_s={d} " ++
        "cutover_source_ready={any} reconciliation={f}\n", .{
        actor_count,                                        f.store.next_change - 1,
        kills,                                              failed_steps,
        final.get("actor_count").?.integer,                 final.get("tombstone_count").?.integer,
        final.get("source_observation_revision").?.integer, final.get("ready").?.bool,
        final.get("sync_lag_s").?.integer,                  final.get("cutover_source_ready").?.bool,
        json.fmt(final.get("reconciliation").?, .{}),
    });
}

test "a policy snapshot from the store decides visibility in a real Tantivy server" {
    var fixture: Fixture = undefined;
    try fixture.init();
    defer fixture.deinit();
    const f = &fixture;
    try f.importEmptyIndex();
    try f.startServer(null);

    for (0..40) |n| try f.store.put(f.id(n), try f.make(n, 0));
    var blocked = try f.make(40, 0);
    blocked.handle = "someone.blocked.bridge.test";
    try f.store.put(f.id(40), blocked);
    f.store.policy_value = .{
        .overrides = &.{.{ .did = "did:plc:bridge00003", .action = .hide }},
        .blocked_domains = &.{"blocked.bridge.test"},
    };
    var b = Bridge.init(f.gpa, f.io, .{ .source_id = "integration-store", .heartbeat_s = 0 }, f.store.interface(), f.endpoint.transport());
    defer b.deinit();
    try settle(&b);

    try testing.expectEqual(@as(u16, 200), try f.status(f.public, "/ready"));
    const moderation = (try f.indexStatus()).get("moderation").?.object;
    try testing.expect(moderation.get("enabled").?.bool and moderation.get("fresh").?.bool);
    try testing.expectEqual(@as(i64, 1), moderation.get("override_count").?.integer);
    try testing.expectEqual(@as(i64, 1), moderation.get("blocked_domain_count").?.integer);
    try testing.expect(try f.find("u00002g0.bridge.test", f.id(2)) != null);
    try testing.expect(try f.find("u00003g0.bridge.test", f.id(3)) == null);
    try testing.expect(try f.find("someone.blocked.bridge.test", f.id(40)) == null);

    // The policy changes with no change-log entry; the heartbeat carries it.
    f.store.policy_value = .{ .overrides = &.{.{ .did = "did:plc:bridge00002", .action = .hide }} };
    try settle(&b);
    try testing.expect(try f.find("u00002g0.bridge.test", f.id(2)) == null);
    try testing.expect(try f.find("u00003g0.bridge.test", f.id(3)) != null);
    try testing.expect(try f.find("someone.blocked.bridge.test", f.id(40)) != null);
}
