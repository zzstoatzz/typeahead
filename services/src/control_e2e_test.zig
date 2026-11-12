//! End to end: a policy change at the Worker reaches the engine's search.
//!
//! The Worker's control record is a stand-in; everything after it is real:
//! the control loop, the owner and its zkv store, the index bridge reading
//! that store, and the `typeahead-tantivy` binary named by TANTIVY_TEST_BIN,
//! queried through its public `/search`. Run with
//! `just --no-dotenv test-bridge`, which builds the binary first.

const std = @import("std");
const zat = @import("zat");
const bridge_mod = @import("bridge/root.zig");
const control = @import("store_process/control.zig");
const enrich = @import("store_process/enrich.zig");
const index_bridge = @import("store_process/index_bridge.zig");
const owner_mod = @import("store_process/owner.zig");
const store_mod = @import("store/root.zig");
const Bridge = bridge_mod.Bridge;
const Io = std.Io;
const Owner = owner_mod.Owner;
const json = std.json;
const testing = std.testing;

const admin_secret = "control-e2e-admin-secret";
const slot_secret = "0123456789abcdef0123456789abcdef";

const alice = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const bob = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";
const carol = "did:plc:cccccccccccccccccccccccc";
const dave = "did:plc:dddddddddddddddddddddddd";

/// The Worker's `/slot/control/*`, holding whatever policy the test sets.
const WorkerRecord = struct {
    revision: u64 = 1,
    overrides: []const u8 = "",
    domains: []const u8 = "",
    requests: []const u8 = "",
    polls: u64 = 0,

    fn set(self: *WorkerRecord, overrides: []const u8, domains: []const u8) void {
        self.overrides = overrides;
        self.domains = domains;
        self.revision += 1;
    }

    fn get(context: *anyopaque, arena: std.mem.Allocator, url: []const u8, authorization: []const u8) control.Response {
        const self: *WorkerRecord = @ptrCast(@alignCast(context));
        if (!std.mem.eql(u8, authorization, "Bearer " ++ slot_secret)) return .{ .status = 401 };
        if (std.mem.indexOf(u8, url, control.requests_path) != null) {
            const taken = std.mem.indexOf(u8, url, "after=0&") == null;
            const body = std.fmt.allocPrint(arena, "{{\"generation\":1,\"requests\":[{s}],\"more\":false}}", .{if (taken) "" else self.requests}) catch return .{};
            return .{ .status = 200, .body = body };
        }
        self.polls += 1;
        const held = std.fmt.allocPrint(arena, "after={d}", .{self.revision}) catch return .{};
        if (std.mem.endsWith(u8, url, held)) return .{ .status = 304 };
        const body = std.fmt.allocPrint(arena, "{{\"generation\":1,\"revision\":{d},\"overrides\":[{s}],\"blocked_domains\":[{s}]}}", .{ self.revision, self.overrides, self.domains }) catch return .{};
        return .{ .status = 200, .body = body };
    }
};

const Slot = struct {
    gpa: std.mem.Allocator,
    io: Io,
    arena: std.heap.ArenaAllocator,
    tmp: testing.TmpDir,
    environment: std.process.Environ.Map,
    http: zat.HttpTransport,
    public: []const u8 = "",
    admin: []const u8 = "",
    server: ?std.process.Child = null,

    fn init(self: *Slot) !void {
        self.* = .{
            .gpa = testing.allocator,
            .io = testing.io,
            .arena = .init(testing.allocator),
            .tmp = testing.tmpDir(.{}),
            .environment = try testing.environ.createMap(testing.allocator),
            .http = .init(testing.io, testing.allocator),
        };
    }

    fn deinit(self: *Slot) void {
        if (self.server) |*child| child.kill(self.io);
        self.http.deinit();
        self.environment.deinit();
        self.tmp.cleanup();
        self.arena.deinit();
    }

    fn path(self: *Slot, name: []const u8) ![]const u8 {
        return std.fmt.allocPrint(self.arena.allocator(), ".zig-cache/tmp/{s}/{s}", .{ self.tmp.sub_path, name });
    }

    fn freePort(self: *Slot) !u16 {
        var listener = try (Io.net.IpAddress{ .ip4 = .loopback(0) }).listen(self.io, .{ .reuse_address = true });
        defer listener.deinit(self.io);
        return listener.socket.address.getPort();
    }

    /// An empty, verified index, served: not ready until a bridge has made
    /// it fresh.
    fn startIndex(self: *Slot) !void {
        const a = self.arena.allocator();
        const binary = self.environment.get("TANTIVY_TEST_BIN") orelse {
            std.debug.print("TANTIVY_TEST_BIN must name a built typeahead-tantivy; `just --no-dotenv test-bridge` sets it\n", .{});
            return error.MissingTantivyTestBin;
        };
        try self.environment.put("ADMIN_SECRET", admin_secret);
        _ = self.environment.swapRemove("TYPEAHEAD_TEST_CRASH");
        const now: u64 = @intCast(@divFloor(Io.Timestamp.now(self.io, .real).nanoseconds, std.time.ns_per_s));
        const seed = try std.fmt.allocPrint(a, "{{\"type\":\"manifest\",\"build_id\":\"control-e2e\",\"source_watermark\":{d}," ++
            "\"actor_count\":0,\"source_actor_count\":0,\"partial\":false}}\n" ++
            "{{\"type\":\"end\",\"build_id\":\"control-e2e\",\"actor_count\":0," ++
            "\"actors_sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}}\n", .{now - 86_400});
        try self.tmp.dir.writeFile(self.io, .{ .sub_path = "seed.jsonl", .data = seed });
        const data = try self.path("index");
        var import = try std.process.spawn(self.io, .{
            .argv = &.{ binary, "import", "--data", data, "--input", try self.path("seed.jsonl") },
            .stdout = .ignore,
            .stderr = .ignore,
        });
        if (!(try import.wait(self.io)).success()) return error.ImportFailed;

        const listen = try std.fmt.allocPrint(a, "127.0.0.1:{d}", .{try self.freePort()});
        const admin_listen = try std.fmt.allocPrint(a, "127.0.0.1:{d}", .{try self.freePort()});
        self.public = try std.fmt.allocPrint(a, "http://{s}", .{listen});
        self.admin = try std.fmt.allocPrint(a, "http://{s}", .{admin_listen});
        self.server = try std.process.spawn(self.io, .{
            .argv = &.{ binary, "serve", "--data", data, "--listen", listen, "--admin-listen", admin_listen },
            .environ_map = &self.environment,
            .stdout = .ignore,
            .stderr = .ignore,
        });
        for (0..400) |_| {
            if (self.status("/health")) |code| {
                if (code == 200) return;
            } else |_| {}
            try self.io.sleep(.fromMilliseconds(25), .awake);
        }
        return error.ServerDidNotStart;
    }

    fn status(self: *Slot, target: []const u8) !u16 {
        const url = try std.fmt.allocPrint(self.arena.allocator(), "{s}{s}", .{ self.public, target });
        var result = try self.http.fetch(.{ .url = url, .deadline_ns = 10 * std.time.ns_per_s });
        defer result.deinit(self.gpa);
        return @backingInt(result.status);
    }

    /// Whether the engine's public search for `term` returns `did`.
    fn finds(self: *Slot, term: []const u8, did: []const u8) !bool {
        const a = self.arena.allocator();
        const url = try std.fmt.allocPrint(a, "{s}/search?q={s}&limit=10", .{ self.public, term });
        var result = try self.http.fetch(.{ .url = url, .deadline_ns = 10 * std.time.ns_per_s });
        defer result.deinit(self.gpa);
        if (result.status != .ok) {
            std.debug.print("search for {s} answered {d}: {s}\n", .{ term, @backingInt(result.status), result.body });
            return error.SearchFailed;
        }
        const answer = try json.parseFromSliceLeaky(json.Value, a, result.body, .{ .allocate = .alloc_always });
        for (answer.object.get("actors").?.array.items) |item| {
            if (std.mem.eql(u8, item.object.get("did").?.string, did)) return true;
        }
        return false;
    }
};

fn commit(owner: *Owner, changes: []const store_mod.Change) !void {
    const submission = try owner_mod.Submission.create(testing.allocator);
    defer submission.destroy();
    submission.changes = changes;
    try owner.call(submission);
}

/// The policy is applied and the index holds what the store holds: three
/// more of the owner's turns, so a domain sweep has run, then the bridge
/// until it has nothing to push.
fn settle(owner: *Owner, bridge: *Bridge) !void {
    for (0..3) |_| try commit(owner, &.{});
    for (0..2000) |_| {
        if ((try bridge.step()).idle) return;
    }
    return error.DidNotSettle;
}

test "an override and a domain block made at the Worker hide accounts from the engine's search, and withdrawing them restores the accounts" {
    var slot: Slot = undefined;
    try slot.init();
    defer slot.deinit();
    try slot.startIndex();

    const owner = try Owner.open(testing.allocator, slot.io, Io.Dir.cwd(), try slot.path("store"), .{ .idle_wait_ms = 1 });
    defer owner.close();
    const owner_thread = try std.Thread.spawn(.{}, Owner.run, .{owner});
    defer {
        owner.stop();
        owner_thread.join();
    }
    try commit(owner, &.{
        .{ .profile = .{ .did = alice, .handle = "e2ealice.control.test", .display_name = "Alice" } },
        .{ .profile = .{ .did = bob, .handle = "e2ebob.blocked.control.test", .display_name = "Bob" } },
        .{ .profile = .{ .did = carol, .handle = "e2ecarol.control.test", .display_name = "Carol" } },
    });

    var source = try index_bridge.StoreSource.open(testing.allocator, owner);
    defer source.close();
    var endpoint: bridge_mod.wire.Http = .{ .http = &slot.http, .base_url = slot.admin, .authorization = "Bearer " ++ admin_secret };
    var bridge = Bridge.init(testing.allocator, slot.io, .{ .source_id = "control-e2e-store", .heartbeat_s = 0 }, source.interface(), endpoint.transport());
    defer bridge.deinit();

    var worker: WorkerRecord = .{};
    var timer: enrich.WallTimer = .{ .io = slot.io };
    var loop: control.Controller = .{
        .gpa = testing.allocator,
        .owner = owner,
        .transport = .{ .context = &worker, .get = WorkerRecord.get },
        .timer = timer.timer(),
        .options = .{ .url = "https://worker.test", .secret = slot_secret },
    };
    try loop.start();
    defer loop.deinit();

    // An empty policy at the Worker: everyone is found.
    try loop.step();
    try settle(owner, &bridge);
    try testing.expectEqual(@as(u16, 200), try slot.status("/ready"));
    try testing.expect(try slot.finds("e2ealice", alice));
    try testing.expect(try slot.finds("e2ebob", bob));
    try testing.expect(try slot.finds("e2ecarol", carol));

    // The operator hides alice and blocks bob's domain. One poll later
    // neither is in search, and carol still is.
    worker.set("{\"did\":\"" ++ alice ++ "\",\"action\":\"hide\"}", "\"blocked.control.test\"");
    try loop.step();
    try settle(owner, &bridge);
    try testing.expect(!try slot.finds("e2ealice", alice));
    try testing.expect(!try slot.finds("e2ebob", bob));
    try testing.expect(try slot.finds("e2ecarol", carol));

    // A poll that finds nothing new changes nothing.
    try loop.step();
    try settle(owner, &bridge);
    try testing.expect(!try slot.finds("e2ealice", alice));

    // A show override rescues bob from the blocked domain.
    worker.set("{\"did\":\"" ++ alice ++ "\",\"action\":\"hide\"},{\"did\":\"" ++ bob ++ "\",\"action\":\"show\"}", "\"blocked.control.test\"");
    try loop.step();
    try settle(owner, &bridge);
    try testing.expect(!try slot.finds("e2ealice", alice));
    try testing.expect(try slot.finds("e2ebob", bob));

    // Everything is withdrawn: both are back.
    worker.set("", "");
    try loop.step();
    try settle(owner, &bridge);
    try testing.expect(try slot.finds("e2ealice", alice));
    try testing.expect(try slot.finds("e2ebob", bob));
    try testing.expect(try slot.finds("e2ecarol", carol));
    try testing.expectEqual(@as(?u64, 4), loop.report().policy_revision);
    try testing.expectEqual(@as(u64, 0), loop.report().failures);

    // An indexing request reaches the store's identity queue. The account
    // has no handle until this slot's own resolver finds one, so it is not
    // in search yet.
    worker.requests = "{\"seq\":1,\"did\":\"" ++ dave ++ "\"}";
    try loop.step();
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    const store = try owner.acquire();
    defer owner.release();
    const requested = (try store.actor(arena.allocator(), dave)).?;
    try testing.expect(requested.live() and requested.identity_due != 0 and requested.handle.len == 0);
    try testing.expectEqual(@as(?u64, 1), (try store.cursors(arena.allocator())).control_requests);
}
