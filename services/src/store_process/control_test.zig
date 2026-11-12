//! The control loop against a stand-in for the Worker's `/slot/control/*`
//! endpoints, under a clock the test moves, and its HTTP transport against a
//! loopback server.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const store_harness = @import("../store/testing/harness.zig");
const bootstrap = @import("bootstrap.zig");
const control = @import("control.zig");
const http = @import("http.zig");
const index_bridge = @import("index_bridge.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const harness = @import("testing.zig");
const Fixture = harness.Fixture;
const config_mod = @import("config.zig");
const role = @import("role.zig");
const zat = @import("zat");
const Io = std.Io;
const testing = std.testing;

const origin = "https://worker.test";
const secret = "0123456789abcdef0123456789abcdef";
const bearer = "Bearer " ++ secret;

const alice = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
const bob = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";
const carol = "did:plc:cccccccccccccccccccccccc";
const dave = "did:plc:dddddddddddddddddddddddd";

const Seen = struct { url: []const u8, authorization: []const u8 };
const Asked = struct { seq: u64, did: []const u8 };

/// The Worker's control record and the two endpoints that serve it, as
/// `src/handlers/slot-control.ts` does.
const Worker = struct {
    arena: std.mem.Allocator,
    revision: u64 = 1,
    overrides: std.StringArrayHashMapUnmanaged([]const u8) = .empty,
    domains: std.StringArrayHashMapUnmanaged(void) = .empty,
    requests: std.ArrayList(Asked) = .empty,
    next_seq: u64 = 1,
    /// Answers for the next requests, before the record is consulted.
    scripted: std.ArrayList(control.Response) = .empty,
    seen: std.ArrayList(Seen) = .empty,

    fn override(self: *Worker, did: []const u8, action: []const u8) void {
        self.overrides.put(self.arena, did, action) catch @panic("out of memory");
        self.revision += 1;
    }

    fn clear(self: *Worker, did: []const u8) void {
        _ = self.overrides.orderedRemove(did);
        self.revision += 1;
    }

    fn block(self: *Worker, domain: []const u8) void {
        self.domains.put(self.arena, domain, {}) catch @panic("out of memory");
        self.revision += 1;
    }

    fn unblock(self: *Worker, domain: []const u8) void {
        _ = self.domains.orderedRemove(domain);
        self.revision += 1;
    }

    fn ask(self: *Worker, did: []const u8) void {
        self.requests.append(self.arena, .{ .seq = self.next_seq, .did = did }) catch @panic("out of memory");
        self.next_seq += 1;
    }

    fn script(self: *Worker, response: control.Response) void {
        self.scripted.append(self.arena, response) catch @panic("out of memory");
    }

    fn parameter(url: []const u8, name: []const u8) ?u64 {
        const query = url[(std.mem.indexOfScalar(u8, url, '?') orelse return null) + 1 ..];
        var parts = std.mem.splitScalar(u8, query, '&');
        while (parts.next()) |part| {
            if (std.mem.startsWith(u8, part, name) and part.len > name.len and part[name.len] == '=') return std.fmt.parseInt(u64, part[name.len + 1 ..], 10) catch null;
        }
        return null;
    }

    fn snapshot(self: *Worker) []const u8 {
        var out: Io.Writer.Allocating = .init(self.arena);
        const w = &out.writer;
        w.print("{{\"revision\":{d},\"overrides\":[", .{self.revision}) catch @panic("out of memory");
        for (self.overrides.keys(), self.overrides.values(), 0..) |did, action, i| {
            w.print("{s}{{\"did\":\"{s}\",\"action\":\"{s}\"}}", .{ if (i > 0) "," else "", did, action }) catch @panic("out of memory");
        }
        w.writeAll("],\"blocked_domains\":[") catch @panic("out of memory");
        for (self.domains.keys(), 0..) |domain, i| w.print("{s}\"{s}\"", .{ if (i > 0) "," else "", domain }) catch @panic("out of memory");
        w.writeAll("]}") catch @panic("out of memory");
        return out.written();
    }

    fn page(self: *Worker, after: u64, limit: u64) []const u8 {
        var out: Io.Writer.Allocating = .init(self.arena);
        const w = &out.writer;
        w.writeAll("{\"requests\":[") catch @panic("out of memory");
        var sent: u64 = 0;
        var more = false;
        for (self.requests.items) |one| {
            if (one.seq <= after) continue;
            if (sent == limit) {
                more = true;
                break;
            }
            w.print("{s}{{\"seq\":{d},\"did\":\"{s}\"}}", .{ if (sent > 0) "," else "", one.seq, one.did }) catch @panic("out of memory");
            sent += 1;
        }
        w.print("],\"more\":{}}}", .{more}) catch @panic("out of memory");
        return out.written();
    }

    fn get(context: *anyopaque, _: std.mem.Allocator, url: []const u8, authorization: []const u8) control.Response {
        const self: *Worker = @ptrCast(@alignCast(context));
        self.seen.append(self.arena, .{
            .url = self.arena.dupe(u8, url) catch @panic("out of memory"),
            .authorization = self.arena.dupe(u8, authorization) catch @panic("out of memory"),
        }) catch @panic("out of memory");
        if (self.scripted.items.len > 0) return self.scripted.orderedRemove(0);
        if (!std.mem.eql(u8, authorization, bearer)) return .{ .status = 401, .body = "{\"error\":\"unauthorized\"}" };
        if (!std.mem.startsWith(u8, url, origin)) return .{ .status = 404 };
        const target = url[origin.len..];
        if (std.mem.startsWith(u8, target, control.policy_path)) {
            if (parameter(url, "after")) |after| if (after == self.revision) return .{ .status = 304 };
            return .{ .status = 200, .body = self.snapshot() };
        }
        if (std.mem.startsWith(u8, target, control.requests_path)) {
            return .{ .status = 200, .body = self.page(parameter(url, "after") orelse 0, parameter(url, "limit") orelse 100) };
        }
        return .{ .status = 404 };
    }

    fn count(self: *const Worker, needle: []const u8) usize {
        var n: usize = 0;
        for (self.seen.items) |one| n += @intFromBool(std.mem.indexOf(u8, one.url, needle) != null);
        return n;
    }

    fn last(self: *const Worker, path: []const u8) []const u8 {
        var i = self.seen.items.len;
        while (i > 0) : (i -= 1) if (std.mem.indexOf(u8, self.seen.items[i - 1].url, path) != null) return self.seen.items[i - 1].url;
        return "";
    }
};

const Clock = struct {
    f: *Fixture,
    ms: i64,

    fn nowMs(context: *anyopaque) i64 {
        const self: *Clock = @ptrCast(@alignCast(context));
        return self.ms;
    }

    fn sleepMs(context: *anyopaque, ms: u64) bool {
        const self: *Clock = @ptrCast(@alignCast(context));
        self.ms += @intCast(ms);
        self.f.clock.at.store(@intCast(@divFloor(self.ms, 1000)), .monotonic);
        return true;
    }
};

const Rig = struct {
    f: *Fixture,
    worker: *Worker,
    clock: *Clock,
    loop: control.Controller,

    fn create() !*Rig {
        const f = try Fixture.create(.{ .idle_wait_ms = 1, .sweep_scan = 2 });
        errdefer f.destroy();
        return attach(f, null);
    }

    /// A loop over `f`'s store, as a process started on it would have.
    fn attach(f: *Fixture, worker: ?*Worker) !*Rig {
        const arena = f.arena.allocator();
        const self = try arena.create(Rig);
        const clock = try arena.create(Clock);
        clock.* = .{ .f = f, .ms = @as(i64, @intCast(f.now())) * 1000 };
        const served = worker orelse blk: {
            const new = try arena.create(Worker);
            new.* = .{ .arena = arena };
            break :blk new;
        };
        self.* = .{ .f = f, .worker = served, .clock = clock, .loop = .{
            .gpa = testing.allocator,
            .owner = f.owner,
            .transport = .{ .context = served, .get = Worker.get },
            .timer = .{ .context = clock, .now_ms = Clock.nowMs, .sleep_ms = Clock.sleepMs },
            .options = .{ .url = origin, .secret = secret, .request_page = 3, .request_pages = 2 },
        } };
        try self.loop.start();
        return self;
    }

    fn destroy(self: *Rig) void {
        self.loop.deinit();
        self.f.destroy();
    }

    /// The process dies and another starts on the same store.
    fn restart(self: *Rig) !*Rig {
        self.loop.deinit();
        try self.f.restart();
        return attach(self.f, self.worker);
    }

    /// One poll with the owner on its own thread, then the owner's turns
    /// until every domain sweep the poll started has finished.
    fn poll(self: *Rig) !void {
        {
            var running = try harness.Running.start(self.f);
            defer {
                running.stop();
                self.f.owner.stopped = false;
            }
            try self.loop.step();
        }
        while (self.f.owner.sweeps.items.len > 0) _ = self.f.owner.turn(0);
    }

    fn advance(self: *Rig, seconds: u64) void {
        _ = Clock.sleepMs(self.clock, seconds * 1000);
    }

    fn seed(self: *Rig, did: []const u8, handle: []const u8) !void {
        _ = try self.f.commit(&.{.{ .profile = .{ .did = did, .handle = handle } }}, null);
    }

    fn hidden(self: *Rig, did: []const u8) !bool {
        return (try self.f.live(did)).hidden;
    }

    /// The store's policy as the index bridge sends it to the index.
    fn bridgePolicy(self: *Rig) ![]const u8 {
        var source = try index_bridge.StoreSource.open(testing.allocator, self.f.owner);
        defer source.close();
        const policy = (try source.interface().policy(self.f.arena.allocator())).?;
        var text: Io.Writer.Allocating = .init(self.f.arena.allocator());
        for (policy.overrides) |one| try text.writer.print("{s}={t} ", .{ one.did, one.action });
        for (policy.blocked_domains) |one| try text.writer.print("{s} ", .{one});
        return text.written();
    }

    fn commits(self: *Rig) u64 {
        return self.f.owner.counters.commits.load(.monotonic);
    }
};

test "a policy snapshot is applied through the owner and its revision stored with it" {
    const rig = try Rig.create();
    defer rig.destroy();
    try rig.seed(alice, "alice.test");
    try rig.seed(bob, "bob.spam.example");
    try rig.seed(carol, "carol.Spam.Example");
    try rig.seed(dave, "dave.test");
    rig.worker.override(alice, "hide");
    rig.worker.override(carol, "show");
    rig.worker.override("did:web:not-stored.test", "hide");
    rig.worker.block("spam.example");

    try testing.expectEqual(@as(?u64, null), (try rig.f.cursors()).control_policy);
    try rig.poll();

    try testing.expect(try rig.hidden(alice));
    try testing.expect(try rig.hidden(bob));
    try testing.expect(!try rig.hidden(carol));
    try testing.expect(!try rig.hidden(dave));
    try testing.expectEqual(@as(?u64, 5), (try rig.f.cursors()).control_policy);
    try testing.expectEqualStrings(
        alice ++ "=hide " ++ carol ++ "=show did:web:not-stored.test=hide spam.example ",
        try rig.bridgePolicy(),
    );
    try testing.expectEqualStrings(origin ++ "/slot/control/policy", rig.worker.seen.items[0].url);
    try testing.expectEqualStrings(bearer, rig.worker.seen.items[0].authorization);

    const report = rig.loop.report();
    try testing.expectEqual(@as(?u64, 5), report.policy_revision);
    try testing.expectEqual(@as(u64, 1), report.snapshots_applied);
    try testing.expectEqual(@as(u64, 4), report.rules_changed);
    try testing.expectEqual(@as(u64, 0), report.failures);
    try testing.expectEqual(@as(u64, 0), report.age_seconds);
}

test "a held revision is answered 304 and commits nothing" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.worker.block("spam.example");
    try rig.poll();
    const before = rig.commits();
    rig.advance(30);
    try rig.poll();
    try rig.poll();
    try testing.expectEqual(before, rig.commits());
    try testing.expectEqualStrings(origin ++ "/slot/control/policy?after=2", rig.worker.last(control.policy_path));
    try testing.expectEqual(@as(u64, 1), rig.loop.report().snapshots_applied);
    try testing.expectEqual(@as(u64, 0), rig.loop.report().age_seconds);
}

test "a rule the Worker removed is removed from the store" {
    const rig = try Rig.create();
    defer rig.destroy();
    try rig.seed(alice, "alice.test");
    try rig.seed(bob, "bob.spam.example");
    try rig.seed(carol, "carol.spam.example");
    rig.worker.override(alice, "hide");
    rig.worker.override(carol, "show");
    rig.worker.block("spam.example");
    rig.worker.block("other.example");
    try rig.poll();
    try testing.expect(try rig.hidden(alice) and try rig.hidden(bob) and !try rig.hidden(carol));

    rig.worker.clear(alice);
    rig.worker.unblock("spam.example");
    try rig.poll();
    try testing.expect(!try rig.hidden(alice));
    try testing.expect(!try rig.hidden(bob));
    try testing.expectEqualStrings(carol ++ "=show other.example ", try rig.bridgePolicy());

    rig.worker.override(carol, "hide");
    try rig.poll();
    try testing.expect(try rig.hidden(carol));

    rig.worker.clear(carol);
    rig.worker.unblock("other.example");
    try rig.poll();
    try testing.expect(!try rig.hidden(carol));
    try testing.expectEqualStrings("", try rig.bridgePolicy());
    try testing.expectEqual(@as(?u64, rig.worker.revision), (try rig.f.cursors()).control_policy);
}

test "a slot that misses ten changes ends where a slot that saw each one does" {
    const every = try Rig.create();
    defer every.destroy();
    const late = try Rig.create();
    defer late.destroy();
    for ([_]*Rig{ every, late }) |rig| {
        try rig.seed(alice, "alice.test");
        try rig.seed(bob, "bob.spam.example");
        try rig.seed(carol, "carol.farm.example");
        try rig.seed(dave, "dave.test");
    }
    late.worker = every.worker;
    late.loop.transport.context = every.worker;
    const worker = every.worker;

    const Edit = struct { kind: enum { override, clear, block, unblock }, subject: []const u8, action: []const u8 = "" };
    const edits = [_]Edit{
        .{ .kind = .override, .subject = alice, .action = "hide" },
        .{ .kind = .block, .subject = "spam.example" },
        .{ .kind = .override, .subject = bob, .action = "show" },
        .{ .kind = .block, .subject = "farm.example" },
        .{ .kind = .override, .subject = alice, .action = "show" },
        .{ .kind = .unblock, .subject = "spam.example" },
        .{ .kind = .clear, .subject = bob },
        .{ .kind = .override, .subject = dave, .action = "hide" },
        .{ .kind = .clear, .subject = alice },
        .{ .kind = .block, .subject = "spam.example" },
    };
    for (edits) |edit| {
        switch (edit.kind) {
            .override => worker.override(edit.subject, edit.action),
            .clear => worker.clear(edit.subject),
            .block => worker.block(edit.subject),
            .unblock => worker.unblock(edit.subject),
        }
        try every.poll();
    }
    try late.poll();

    try testing.expectEqual(@as(u64, 10), every.loop.report().snapshots_applied);
    try testing.expectEqual(@as(u64, 1), late.loop.report().snapshots_applied);
    try testing.expectEqualStrings(try every.bridgePolicy(), try late.bridgePolicy());
    try testing.expectEqualStrings(dave ++ "=hide farm.example spam.example ", try late.bridgePolicy());
    for ([_][]const u8{ alice, bob, carol, dave }) |did| try testing.expectEqual(try every.hidden(did), try late.hidden(did));
    try testing.expect(!try late.hidden(alice) and try late.hidden(bob) and try late.hidden(carol) and try late.hidden(dave));
    try testing.expectEqual((try every.f.cursors()).control_policy, (try late.f.cursors()).control_policy);

    // The same snapshot again, to a store that already equals it.
    const store = try late.f.owner.acquire();
    defer late.f.owner.release();
    const arena = late.f.arena.allocator();
    const policy = try control.parsePolicy(arena, worker.snapshot());
    const changes = try control.difference(arena, try store_mod.read.overrides(store.db(), arena), try store_mod.read.blockedDomains(store.db(), arena), policy);
    try testing.expectEqual(@as(usize, 0), changes.len);
}

test "a restart resumes from the stored revision, and a poll that died before its commit is made again" {
    var rig = try Rig.create();
    defer rig.destroy();
    try rig.seed(alice, "alice.test");
    rig.worker.override(alice, "hide");

    // The process dies after the answer arrives and before the commit.
    rig.f.owner.stop();
    try testing.expectError(error.OwnerStopped, rig.loop.step());
    try testing.expectEqual(control.Failure.commit, rig.loop.report().last_failure);
    rig = try rig.restart();
    try testing.expectEqual(@as(?u64, null), (try rig.f.cursors()).control_policy);
    try testing.expect(!try rig.hidden(alice));

    try rig.poll();
    try testing.expect(try rig.hidden(alice));
    try testing.expectEqual(@as(?u64, 2), (try rig.f.cursors()).control_policy);

    // The process dies after the commit: the next one asks from revision 2
    // and applies nothing again.
    rig = try rig.restart();
    try testing.expectEqual(@as(?u64, 2), rig.loop.report().policy_revision);
    const before = rig.commits();
    try rig.poll();
    try testing.expectEqualStrings(origin ++ "/slot/control/policy?after=2", rig.worker.last(control.policy_path));
    try testing.expectEqual(before, rig.commits());
    try testing.expectEqual(@as(u64, 0), rig.loop.report().snapshots_applied);
    try testing.expect(try rig.hidden(alice));

    rig.worker.clear(alice);
    rig = try rig.restart();
    try rig.poll();
    try testing.expect(!try rig.hidden(alice));
    try testing.expectEqual(@as(?u64, 3), (try rig.f.cursors()).control_policy);
}

test "an unreachable or refusing Worker leaves the policy in force and the age rising" {
    const rig = try Rig.create();
    defer rig.destroy();
    try rig.seed(alice, "alice.test");
    rig.worker.override(alice, "hide");
    try rig.poll();
    rig.worker.clear(alice);

    const refusals = [_]struct { response: control.Response, failure: control.Failure }{
        .{ .response = .{}, .failure = .unreachable_worker },
        .{ .response = .{ .status = 500, .body = "internal error" }, .failure = .status },
        .{ .response = .{ .status = 503, .body = "{\"error\":\"slot control is not configured\"}" }, .failure = .status },
        .{ .response = .{ .status = 401, .body = "{\"error\":\"unauthorized\"}" }, .failure = .unauthorized },
        .{ .response = .{ .status = 403 }, .failure = .unauthorized },
        .{ .response = .{ .status = 429 }, .failure = .status },
        .{ .response = .{ .status = 302 }, .failure = .status },
        .{ .response = .{ .status = 204 }, .failure = .status },
    };
    for (refusals, 1..) |refusal, n| {
        rig.worker.script(refusal.response);
        rig.worker.script(refusal.response);
        rig.advance(30);
        try rig.poll();
        const report = rig.loop.report();
        try testing.expectEqual(refusal.failure, report.last_failure);
        try testing.expectEqual(refusal.response.status, report.last_failure_status);
        try testing.expectEqual(@as(u64, 2 * n), report.failures);
        try testing.expectEqual(@as(u64, 30 * n), report.age_seconds);
        try testing.expect(try rig.hidden(alice));
        try testing.expectEqual(@as(?u64, 2), (try rig.f.cursors()).control_policy);
    }
    try testing.expectEqual(@as(u64, 2), rig.loop.report().unreachable_worker);
    try testing.expectEqual(@as(u64, 4), rig.loop.report().unauthorized);

    rig.advance(30);
    try rig.poll();
    try testing.expect(!try rig.hidden(alice));
    try testing.expectEqual(@as(u64, 0), rig.loop.report().age_seconds);
    try testing.expectEqual(@as(?u64, 3), rig.loop.report().policy_revision);
}

test "the age counts from the start until both endpoints have answered" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.worker.script(.{});
    rig.worker.script(.{});
    rig.advance(45);
    try rig.poll();
    try testing.expectEqual(@as(u64, 45), rig.loop.report().age_seconds);
    // The policy answers and the requests do not: still not current.
    rig.advance(30);
    try rig.poll();
    rig.worker.script(.{ .status = 304 });
    rig.worker.script(.{ .status = 500 });
    rig.advance(30);
    try rig.poll();
    try testing.expectEqual(@as(u64, 30), rig.loop.report().age_seconds);
    try testing.expectEqual(rig.loop.report().policy_checked_at, rig.loop.report().requests_checked_at + 30);
}

test "a malformed snapshot is refused whole" {
    const rig = try Rig.create();
    defer rig.destroy();
    try rig.seed(alice, "alice.test");
    try rig.seed(bob, "bob.spam.example");
    rig.worker.override(alice, "hide");
    rig.worker.block("spam.example");
    try rig.poll();

    const long: [store_mod.keys.max_did + 1]u8 = @splat('a');
    const bodies = [_][]const u8{
        "",
        "not json",
        "[]",
        "{}",
        "{\"revision\":9}",
        "{\"revision\":9,\"overrides\":[]}",
        "{\"revision\":9,\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[]",
        "{\"revision\":-1,\"overrides\":[],\"blocked_domains\":[]}",
        "{\"revision\":9.5,\"overrides\":[],\"blocked_domains\":[]}",
        "{\"revision\":null,\"overrides\":[],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":{},\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"" ++ alice ++ "\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"" ++ alice ++ "\",\"action\":\"delete\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"" ++ alice ++ "\",\"action\":\"none\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"\",\"action\":\"hide\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"alice.test\",\"action\":\"hide\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"did:plc:a\\u0000b\",\"action\":\"hide\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"did:" ++ long ++ "\",\"action\":\"hide\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[{\"did\":\"" ++ alice ++ "\",\"action\":\"hide\"},{\"did\":\"" ++ alice ++ "\",\"action\":\"show\"}],\"blocked_domains\":[]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[\"\"]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[\"nodot\"]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[\"has space.example\"]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[\"a\\u0000.example\"]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[\"spam.example\",\"SPAM.example\"]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[{\"domain\":\"spam.example\"}]}",
        "{\"revision\":9,\"overrides\":[],\"blocked_domains\":[7]}",
    };
    for (bodies, 1..) |body, n| {
        rig.worker.script(.{ .status = 200, .body = body });
        try rig.poll();
        try testing.expectEqual(control.Failure.malformed, rig.loop.report().last_failure);
        try testing.expectEqual(@as(u64, n), rig.loop.report().malformed);
        try testing.expectEqual(@as(?u64, 3), (try rig.f.cursors()).control_policy);
        try testing.expect(try rig.hidden(alice) and try rig.hidden(bob));
    }
    try testing.expectEqualStrings(alice ++ "=hide spam.example ", try rig.bridgePolicy());

    // Fields this build does not know are passed over.
    rig.worker.script(.{ .status = 200, .body = "{\"revision\":9,\"overrides\":[{\"did\":\"" ++ alice ++ "\",\"action\":\"show\",\"reason\":\"x\"}],\"blocked_domains\":[\"Farm.Example\"],\"generated_at\":5}" });
    try rig.poll();
    try testing.expectEqualStrings(alice ++ "=show farm.example ", try rig.bridgePolicy());
    try testing.expectEqual(@as(?u64, 9), (try rig.f.cursors()).control_policy);
}

test "a revision older than the one applied is refused, and the same one changes nothing" {
    const rig = try Rig.create();
    defer rig.destroy();
    try rig.seed(alice, "alice.test");
    rig.worker.override(alice, "hide");
    rig.worker.block("spam.example");
    try rig.poll();
    try testing.expectEqual(@as(?u64, 3), (try rig.f.cursors()).control_policy);

    // A stale answer: the policy as it was two changes ago.
    rig.worker.script(.{ .status = 200, .body = "{\"revision\":1,\"overrides\":[],\"blocked_domains\":[]}" });
    try rig.poll();
    try testing.expectEqual(control.Failure.older_revision, rig.loop.report().last_failure);
    try testing.expectEqual(@as(u64, 1), rig.loop.report().older_revision);
    try testing.expect(try rig.hidden(alice));
    try testing.expectEqual(@as(?u64, 3), (try rig.f.cursors()).control_policy);
    try testing.expectEqualStrings(alice ++ "=hide spam.example ", try rig.bridgePolicy());

    // The applied revision with other contents, which the Worker cannot
    // send: the revision names one policy, and the store holds it.
    const before = rig.commits();
    rig.worker.script(.{ .status = 200, .body = "{\"revision\":3,\"overrides\":[],\"blocked_domains\":[]}" });
    try rig.poll();
    try testing.expectEqual(before, rig.commits());
    try testing.expect(try rig.hidden(alice));
    try testing.expectEqual(@as(u64, 1), rig.loop.report().failures);

    // A 304 to a store that holds no revision is not an answer.
    const fresh = try Rig.create();
    defer fresh.destroy();
    fresh.worker.script(.{ .status = 304 });
    try fresh.poll();
    try testing.expectEqual(control.Failure.status, fresh.loop.report().last_failure);
    try testing.expectEqual(@as(?u64, null), (try fresh.f.cursors()).control_policy);
}

const bootstrap_policy =
    \\{"overrides":[{"did":"did:plc:aaaaaaaaaaaaaaaaaaaaaaaa","action":"hide","reason":"withdrawn since the bundle"},
    \\              {"did":"did:plc:bbbbbbbbbbbbbbbbbbbbbbbb","action":"show","reason":"kept"},
    \\              {"did":"did:plc:cccccccccccccccccccccccc","action":"show","reason":"reversed since"}],
    \\ "blocked_domains":[{"domain":"Stale.Example","reason":"unblocked since"},{"domain":"spam.example","reason":"kept"}]}
;

const bootstrap_actors =
    \\{"did":"did:plc:aaaaaaaaaaaaaaaaaaaaaaaa","handle":"alice.test","quality_score":1}
    \\{"did":"did:plc:bbbbbbbbbbbbbbbbbbbbbbbb","handle":"bob.spam.example","quality_score":1}
    \\{"did":"did:plc:cccccccccccccccccccccccc","handle":"carol.test","quality_score":1}
    \\{"did":"did:plc:dddddddddddddddddddddddd","handle":"dave.stale.example","quality_score":1}
    \\{"did":"did:plc:eeeeeeeeeeeeeeeeeeeeeeee","handle":"erin.new.example","quality_score":1}
;

test "a bootstrapped store converges to the Worker's policy whatever its policy file held" {
    const f = try Fixture.create(.{ .idle_wait_ms = 1, .sweep_scan = 2 });
    var rig: ?*Rig = null;
    defer if (rig) |attached| attached.destroy() else f.destroy();
    {
        const store = try f.owner.acquire();
        defer f.owner.release();
        var summary: bootstrap.Summary = .{};
        try bootstrap.applyPolicy(store, f.arena.allocator(), f.now(), bootstrap_policy, &summary);
        var reader: Io.Reader = .fixed(bootstrap_actors);
        try bootstrap.load(testing.allocator, store, f.now(), &reader, .{ .relay = 32_234_719_514 }, null, &summary);
        try testing.expectEqual(@as(u64, 5), summary.loaded);
    }
    try testing.expectEqual(@as(?u64, null), (try f.cursors()).control_policy);
    const erin = "did:plc:eeeeeeeeeeeeeeeeeeeeeeee";
    try testing.expect((try f.live(alice)).hidden and !(try f.live(bob)).hidden and !(try f.live(carol)).hidden);
    try testing.expect((try f.live(dave)).hidden and !(try f.live(erin)).hidden);

    // The Worker has never changed its policy since the store's bundle was
    // cut, so it is still at the revision the bundle's era had: nothing but
    // the absence of a stored revision makes the slot ask.
    const worker = try f.arena.allocator().create(Worker);
    worker.* = .{ .arena = f.arena.allocator(), .revision = 0 };
    worker.overrides.put(worker.arena, bob, "show") catch unreachable;
    worker.overrides.put(worker.arena, carol, "hide") catch unreachable;
    worker.domains.put(worker.arena, "spam.example", {}) catch unreachable;
    worker.domains.put(worker.arena, "new.example", {}) catch unreachable;

    rig = try Rig.attach(f, worker);
    try rig.?.poll();
    try testing.expectEqualStrings(origin ++ "/slot/control/policy", worker.seen.items[0].url);
    try testing.expectEqualStrings(bob ++ "=show " ++ carol ++ "=hide new.example spam.example ", try rig.?.bridgePolicy());
    try testing.expect(!try rig.?.hidden(alice));
    try testing.expect(!try rig.?.hidden(bob));
    try testing.expect(try rig.?.hidden(carol));
    try testing.expect(!try rig.?.hidden(dave));
    try testing.expect(try rig.?.hidden(erin));
    try testing.expectEqual(@as(?u64, 0), (try f.cursors()).control_policy);
    try testing.expectEqual(@as(u64, 4), rig.?.loop.report().rules_changed);

    // From here it holds a revision like any other slot.
    try rig.?.poll();
    try testing.expectEqualStrings(origin ++ "/slot/control/policy?after=0", worker.last(control.policy_path));
    try testing.expectEqual(@as(u64, 1), rig.?.loop.report().snapshots_applied);
}

test "an indexing request creates the actor and queues its identity, as an #identity event does" {
    var rig = try Rig.create();
    defer rig.destroy();
    try rig.seed(bob, "bob.test");
    _ = try rig.f.commit(&.{ .{ .observe = .{ .did = carol } }, .{ .gone = .{ .did = carol, .status = .deleted } } }, null);
    try testing.expect(try rig.f.queued(bob) == null);

    rig.worker.ask(alice);
    rig.worker.ask(bob);
    rig.worker.ask(carol);
    try rig.poll();

    const created = try rig.f.live(alice);
    try testing.expectEqualStrings("", created.handle);
    const queued = (try rig.f.queued(alice)).?;
    try testing.expectEqual(store_mod.record.IdentityReason.identity, queued.reason);
    try testing.expectEqual(rig.f.now(), created.identity_due);
    try testing.expectEqual(store_mod.record.IdentityReason.identity, (try rig.f.queued(bob)).?.reason);
    try testing.expect(!(try rig.f.actor(carol)).?.live());
    try testing.expect(try rig.f.queued(carol) == null);
    try testing.expectEqual(@as(?u64, 3), (try rig.f.cursors()).control_requests);
    try testing.expectEqualStrings(origin ++ "/slot/control/index-requests?after=0&limit=3", rig.worker.seen.items[1].url);
    try testing.expectEqualStrings(bearer, rig.worker.seen.items[1].authorization);

    // The same as the relay's #identity for the same account.
    var twin = try Rig.create();
    defer twin.destroy();
    var changes = twin.f.changes();
    try changes.firehose(.{ .identity = .{ .seq = 0, .did = alice, .time = "2026-11-12T00:00:00.000Z" } });
    _ = try twin.f.commit(changes.items(), null);
    const heard = try twin.f.live(alice);
    try testing.expectEqual(heard.identity_due, created.identity_due);
    try testing.expectEqualDeep((try twin.f.queued(alice)).?, queued);

    // A second request for an account still queued raises its generation
    // and leaves one entry.
    rig.worker.ask(alice);
    try rig.poll();
    try testing.expectEqual(@as(u64, 2), (try rig.f.queued(alice)).?.generation);
    try testing.expectEqual(@as(?u64, 4), (try rig.f.cursors()).control_requests);

    // A restart asks from the stored sequence and queues nothing again.
    rig = try rig.restart();
    try rig.poll();
    try testing.expectEqualStrings(origin ++ "/slot/control/index-requests?after=4&limit=3", rig.worker.last(control.requests_path));
    try testing.expectEqual(@as(u64, 2), (try rig.f.queued(alice)).?.generation);
    try testing.expectEqual(@as(u64, 0), rig.loop.report().requests_queued);
}

test "requests are taken a bounded number of pages at a time, in order" {
    const rig = try Rig.create();
    defer rig.destroy();
    var dids: [8][]const u8 = undefined;
    for (&dids, 0..) |*did, n| {
        did.* = try std.fmt.allocPrint(rig.f.arena.allocator(), "did:plc:{c}{c}{c}{c}aaaaaaaaaaaaaaaaaaaa", .{ 'a' + @as(u8, @intCast(n)), 'b', 'c', 'd' });
        rig.worker.ask(did.*);
    }
    try rig.poll();
    try testing.expectEqual(@as(?u64, 6), (try rig.f.cursors()).control_requests);
    try testing.expectEqual(@as(usize, 2), rig.worker.count(control.requests_path));
    for (dids[0..6]) |did| try testing.expect(try rig.f.queued(did) != null);
    try testing.expect(try rig.f.actor(dids[6]) == null);

    try rig.poll();
    try testing.expectEqual(@as(?u64, 8), (try rig.f.cursors()).control_requests);
    try testing.expectEqual(@as(usize, 3), rig.worker.count(control.requests_path));
    try testing.expectEqual(@as(u64, 8), rig.loop.report().requests_queued);
    for (dids) |did| try testing.expect(try rig.f.queued(did) != null);
}

test "a request that is not a DID this role indexes is passed over, and the sequence still moves" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.worker.ask("alice.test");
    rig.worker.ask("did:plc:short");
    rig.worker.ask(alice);
    try rig.poll();
    try testing.expect(try rig.f.actor("alice.test") == null);
    try testing.expect(try rig.f.actor("did:plc:short") == null);
    try testing.expect(try rig.f.queued(alice) != null);
    try testing.expectEqual(@as(?u64, 3), (try rig.f.cursors()).control_requests);
    try testing.expectEqual(@as(u64, 1), rig.loop.report().requests_queued);
    try testing.expectEqual(@as(u64, 2), rig.loop.report().requests_skipped);
}

test "a page that is malformed or out of order is refused whole" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.worker.ask(alice);
    rig.worker.ask(bob);
    try rig.poll();

    const entry = "{\"seq\":3,\"did\":\"" ++ carol ++ "\"}";
    const bodies = [_][]const u8{
        "",
        "{}",
        "{\"requests\":[" ++ entry ++ "]}",
        "{\"requests\":{},\"more\":false}",
        "{\"requests\":[{\"seq\":3}],\"more\":false}",
        "{\"requests\":[{\"seq\":-3,\"did\":\"" ++ carol ++ "\"}],\"more\":false}",
        // at or below the stored sequence
        "{\"requests\":[{\"seq\":2,\"did\":\"" ++ carol ++ "\"}],\"more\":false}",
        "{\"requests\":[{\"seq\":1,\"did\":\"" ++ carol ++ "\"}," ++ entry ++ "],\"more\":false}",
        // not rising
        "{\"requests\":[{\"seq\":4,\"did\":\"" ++ carol ++ "\"}," ++ entry ++ "],\"more\":false}",
        "{\"requests\":[" ++ entry ++ "," ++ entry ++ "],\"more\":false}",
        // more than was asked for
        "{\"requests\":[" ++ entry ++ ",{\"seq\":4,\"did\":\"" ++ carol ++ "\"},{\"seq\":5,\"did\":\"" ++ carol ++ "\"},{\"seq\":6,\"did\":\"" ++ carol ++ "\"}],\"more\":false}",
        // more to come after nothing
        "{\"requests\":[],\"more\":true}",
    };
    for (bodies, 1..) |body, n| {
        rig.worker.script(.{ .status = 304 });
        rig.worker.script(.{ .status = 200, .body = body });
        try rig.poll();
        try testing.expectEqual(control.Failure.malformed, rig.loop.report().last_failure);
        try testing.expectEqual(@as(u64, n), rig.loop.report().malformed);
        try testing.expectEqual(@as(?u64, 2), (try rig.f.cursors()).control_requests);
        try testing.expect(try rig.f.actor(carol) == null);
    }
    for ([_]control.Response{ .{}, .{ .status = 304 }, .{ .status = 401 }, .{ .status = 500 } }) |response| {
        rig.worker.script(.{ .status = 304 });
        rig.worker.script(response);
        try rig.poll();
        try testing.expectEqual(@as(?u64, 2), (try rig.f.cursors()).control_requests);
    }
}

test "the loop runs until it is stopped, resting a jittered interval between polls" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.loop.options.poll_seconds = 30;
    for (0..200) |_| {
        const ms = rig.loop.rest();
        try testing.expect(ms >= 30_000 and ms <= 36_000);
    }

    const Stopper = struct {
        rig: *Rig,
        sleeps: usize = 0,
        slept_ms: u64 = 0,

        fn sleepMs(context: *anyopaque, ms: u64) bool {
            const self: *@This() = @ptrCast(@alignCast(context));
            self.sleeps += 1;
            self.slept_ms += ms;
            _ = Clock.sleepMs(self.rig.clock, ms);
            if (self.sleeps == 3) self.rig.loop.stop();
            return true;
        }

        fn nowMs(context: *anyopaque) i64 {
            const self: *@This() = @ptrCast(@alignCast(context));
            return self.rig.clock.ms;
        }
    };
    var stopper: Stopper = .{ .rig = rig };
    rig.loop.timer = .{ .context = &stopper, .now_ms = Stopper.nowMs, .sleep_ms = Stopper.sleepMs };
    rig.worker.block("spam.example");
    {
        var running = try harness.Running.start(rig.f);
        defer {
            running.stop();
            rig.f.owner.stopped = false;
        }
        rig.loop.run();
    }
    try testing.expectEqual(@as(u64, 3), rig.loop.report().polls);
    try testing.expectEqual(@as(usize, 3), rig.worker.count(control.policy_path));
    try testing.expect(stopper.slept_ms >= 90_000 and stopper.slept_ms <= 108_000);
    try testing.expectEqual(@as(?u64, 2), (try rig.f.cursors()).control_policy);
}

test "/health reports the loop, and says so when it was asked for and cannot run" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.worker.override(alice, "hide");
    rig.worker.ask(alice);
    try rig.poll();
    rig.worker.script(.{});
    rig.worker.script(.{ .status = 401 });
    rig.advance(90);
    try rig.poll();

    var signals: intake_mod.Signals = .{};
    var ignored: std.atomic.Value(u64) = .init(0);
    var context: http.Context = .{ .owner = rig.f.owner, .reader = null, .intake = &signals, .labels_ignored = &ignored, .read_secret = null, .control = &rig.loop };
    const arena = rig.f.arena.allocator();
    const Body = struct { control: ?http.ControlBody = null };
    const on = (try std.json.parseFromSliceLeaky(Body, arena, (try http.respond(&context, arena, .GET, "/health", null, "")).body, .{ .ignore_unknown_fields = true })).control.?;
    try testing.expectEqualStrings("on", on.state);
    try testing.expectEqual(@as(?u64, 90), on.age_seconds);
    try testing.expectEqual(@as(?u64, 30), on.poll_seconds);
    try testing.expectEqual(@as(?u64, 2), on.policy.?.revision);
    try testing.expectEqual(@as(u64, 1), on.policy.?.rules_changed);
    try testing.expectEqual(@as(u64, 1), on.index_requests.?.sequence);
    try testing.expectEqual(@as(u64, 1), on.index_requests.?.queued);
    try testing.expectEqual(@as(u64, 2), on.failures.?.total);
    try testing.expectEqual(@as(u64, 1), on.failures.?.no_response);
    try testing.expectEqual(@as(u64, 1), on.failures.?.unauthorized);
    try testing.expectEqualStrings("unauthorized", on.failures.?.last);
    try testing.expectEqual(@as(u16, 401), on.failures.?.last_status);

    context.control = null;
    const off = try http.respond(&context, arena, .GET, "/health", null, "");
    try testing.expect(std.mem.indexOf(u8, off.body, "\"control\"") == null);

    context.control_problem = "CONTROL_URL is set without SLOT_CONTROL_SECRET";
    const broken = (try std.json.parseFromSliceLeaky(Body, arena, (try http.respond(&context, arena, .GET, "/health", null, "")).body, .{ .ignore_unknown_fields = true })).control.?;
    try testing.expectEqualStrings("misconfigured", broken.state);
    try testing.expectEqualStrings("CONTROL_URL is set without SLOT_CONTROL_SECRET", broken.problem.?);
    try testing.expectEqual(@as(?u64, null), broken.age_seconds);
}

/// A loopback HTTP server that answers each request from `answer`.
const Stub = struct {
    io: Io,
    listener: Io.net.Server,
    thread: ?std.Thread = null,
    stop: std.atomic.Value(bool) = .init(false),
    targets: std.ArrayList([]const u8) = .empty,
    authorizations: std.ArrayList([]const u8) = .empty,
    big: []const u8,

    fn start(self: *Stub) !void {
        self.thread = try std.Thread.spawn(.{}, serve, .{self});
    }

    fn deinit(self: *Stub) void {
        self.stop.store(true, .release);
        if (self.listener.socket.address.connect(self.io, .{ .mode = .stream })) |wake| wake.close(self.io) else |_| {}
        if (self.thread) |thread| thread.join();
        self.listener.deinit(self.io);
        for (self.targets.items) |one| testing.allocator.free(one);
        for (self.authorizations.items) |one| testing.allocator.free(one);
        self.targets.deinit(testing.allocator);
        self.authorizations.deinit(testing.allocator);
    }

    fn url(self: *Stub, buf: []u8, path: []const u8) []const u8 {
        return std.fmt.bufPrint(buf, "http://127.0.0.1:{d}{s}", .{ self.listener.socket.address.getPort(), path }) catch unreachable;
    }

    fn serve(self: *Stub) void {
        while (true) {
            const stream = self.listener.accept(self.io) catch return;
            defer stream.close(self.io);
            if (self.stop.load(.acquire)) return;
            var read_buffer: [8192]u8 = undefined;
            var write_buffer: [8192]u8 = undefined;
            var reader = stream.reader(self.io, &read_buffer);
            var writer = stream.writer(self.io, &write_buffer);
            var server = std.http.Server.init(&reader.interface, &writer.interface);
            var request = server.receiveHead() catch continue;
            var authorization: []const u8 = "";
            var headers = request.iterateHeaders();
            while (headers.next()) |header| {
                if (std.ascii.eqlIgnoreCase(header.name, "authorization")) authorization = header.value;
            }
            const target = request.head.target;
            // Recorded before the answer is sent, so the test reads these
            // only after the request that added them has returned.
            self.targets.append(testing.allocator, testing.allocator.dupe(u8, target) catch continue) catch continue;
            self.authorizations.append(testing.allocator, testing.allocator.dupe(u8, authorization) catch continue) catch continue;
            const json: []const std.http.Header = &.{.{ .name = "content-type", .value = "application/json" }};
            if (std.mem.startsWith(u8, target, "/redirect")) {
                request.respond("", .{ .status = .found, .keep_alive = false, .extra_headers = &.{.{ .name = "location", .value = "/slot/control/policy" }} }) catch {};
            } else if (std.mem.startsWith(u8, target, "/big")) {
                request.respond(self.big, .{ .keep_alive = false, .extra_headers = json }) catch {};
            } else if (std.mem.startsWith(u8, target, "/unchanged")) {
                request.respond("", .{ .status = .not_modified, .keep_alive = false }) catch {};
            } else if (!std.mem.eql(u8, authorization, bearer)) {
                request.respond("{\"error\":\"unauthorized\"}", .{ .status = .unauthorized, .keep_alive = false, .extra_headers = json }) catch {};
            } else if (std.mem.startsWith(u8, target, control.requests_path)) {
                const body = if (std.mem.indexOf(u8, target, "after=0&") != null) "{\"requests\":[{\"seq\":4,\"did\":\"" ++ alice ++ "\"}],\"more\":false}" else "{\"requests\":[],\"more\":false}";
                request.respond(body, .{ .keep_alive = false, .extra_headers = json }) catch {};
            } else if (std.mem.endsWith(u8, target, "after=7")) {
                request.respond("", .{ .status = .not_modified, .keep_alive = false }) catch {};
            } else {
                request.respond("{\"revision\":7,\"overrides\":[{\"did\":\"" ++ bob ++ "\",\"action\":\"hide\"}],\"blocked_domains\":[\"spam.example\"]}", .{ .keep_alive = false, .extra_headers = json }) catch {};
            }
        }
    }
};

test "the transport sends the bearer secret, bounds the answer and follows no redirect" {
    const io = testing.io;
    const big = try testing.allocator.alloc(u8, control.max_response_bytes + 1);
    defer testing.allocator.free(big);
    @memset(big, ' ');
    var address: Io.net.IpAddress = .{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = 0 } };
    var stub: Stub = .{ .io = io, .listener = try address.listen(io, .{ .reuse_address = true }), .big = big };
    try stub.start();
    defer stub.deinit();

    var client = control.Http.init(io, testing.allocator);
    defer client.deinit();
    const transport = client.transport();
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var buf: [128]u8 = undefined;

    const ok = transport.get(transport.context, arena, stub.url(&buf, control.policy_path ++ "?after=3"), bearer);
    try testing.expectEqual(@as(u16, 200), ok.status);
    try testing.expectEqual(@as(u64, 7), (try control.parsePolicy(arena, ok.body)).revision);
    try testing.expectEqualStrings(control.policy_path ++ "?after=3", stub.targets.items[0]);
    try testing.expectEqualStrings(bearer, stub.authorizations.items[0]);

    try testing.expectEqual(@as(u16, 401), transport.get(transport.context, arena, stub.url(&buf, control.policy_path), "Bearer wrong").status);
    try testing.expectEqual(@as(u16, 304), transport.get(transport.context, arena, stub.url(&buf, "/unchanged"), bearer).status);

    // A redirect is not followed: the secret goes to the configured origin
    // and nowhere a response names.
    const seen = stub.targets.items.len;
    try testing.expectEqual(@as(u16, 0), transport.get(transport.context, arena, stub.url(&buf, "/redirect"), bearer).status);
    try testing.expectEqual(seen + 1, stub.targets.items.len);

    try testing.expectEqual(@as(u16, 0), transport.get(transport.context, arena, stub.url(&buf, "/big"), bearer).status);

    // Nothing listens on port 1.
    try testing.expectEqual(@as(u16, 0), transport.get(transport.context, arena, "http://127.0.0.1:1" ++ control.policy_path, bearer).status);
}

const RunningRole = struct {
    listener: *Io.net.Server,
    config: config_mod.Config,
    options: role.Options,
    result: anyerror!void = {},

    fn run(self: *RunningRole) void {
        self.result = role.run(testing.allocator, testing.io, self.listener, self.config, self.options);
    }
};

test "the role pulls the control record while it runs, reports it in /health and keeps it across a restart" {
    const io = testing.io;
    var tmp = testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [128]u8 = undefined;
    const path = try std.fmt.bufPrint(&path_buf, ".zig-cache/tmp/{s}/store", .{tmp.sub_path});

    var stub_address: Io.net.IpAddress = .{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = 0 } };
    var stub: Stub = .{ .io = io, .listener = try stub_address.listen(io, .{ .reuse_address = true }), .big = "" };
    try stub.start();
    defer stub.deinit();
    var url_buf: [64]u8 = undefined;

    var address: Io.net.IpAddress = .{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = 0 } };
    var listener = try address.listen(io, .{ .reuse_address = true });
    defer listener.deinit(io);
    var stop: std.atomic.Value(bool) = .init(false);
    var running: RunningRole = .{
        .listener = &listener,
        .config = .{
            .store_dir = path,
            .offline = true,
            .corpus_scan_seconds = 0,
            .control = .{ .on = .{ .url = stub.url(&url_buf, ""), .secret = secret, .poll_seconds = 5 } },
        },
        .options = .{ .startup_seconds = 1, .corpus_delay_seconds = 3600, .stop = &stop },
    };
    const thread = try std.Thread.spawn(.{}, RunningRole.run, .{&running});

    var client = zat.HttpTransport.init(io, testing.allocator);
    defer client.deinit();
    var health_buf: [64]u8 = undefined;
    const health_url = try std.fmt.bufPrint(&health_buf, "http://127.0.0.1:{d}/health", .{listener.socket.address.getPort()});
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    const Body = struct { control: ?http.ControlBody = null };
    var reported: ?http.ControlBody = null;
    for (0..500) |_| {
        var result = client.fetch(.{ .url = health_url, .deadline_ns = 2 * std.time.ns_per_s }) catch {
            try io.sleep(Io.Duration.fromMilliseconds(20), .awake);
            continue;
        };
        defer result.deinit(testing.allocator);
        const body = try std.json.parseFromSliceLeaky(Body, arena, try arena.dupe(u8, result.body), .{ .ignore_unknown_fields = true });
        reported = body.control;
        if (reported) |seen| if (seen.policy.?.revision == 7 and seen.index_requests.?.sequence == 4) break;
        try io.sleep(Io.Duration.fromMilliseconds(20), .awake);
    }

    stop.store(true, .release);
    const wake = try listener.socket.address.connect(io, .{ .mode = .stream });
    wake.close(io);
    thread.join();
    try running.result;

    const seen = reported.?;
    try testing.expectEqualStrings("on", seen.state);
    try testing.expectEqual(@as(?u64, 7), seen.policy.?.revision);
    try testing.expectEqual(@as(u64, 2), seen.policy.?.rules_changed);
    try testing.expectEqual(@as(u64, 4), seen.index_requests.?.sequence);
    try testing.expectEqual(@as(u64, 1), seen.index_requests.?.queued);
    try testing.expectEqual(@as(u64, 0), seen.failures.?.total);
    try testing.expectEqualStrings(control.policy_path, stub.targets.items[0]);
    try testing.expectEqualStrings(bearer, stub.authorizations.items[0]);

    const reopened = try owner_mod.Owner.open(testing.allocator, io, Io.Dir.cwd(), path, .{});
    defer reopened.close();
    const cursors = reopened.health().cursors;
    try testing.expectEqual(@as(?u64, 7), cursors.control_policy);
    try testing.expectEqual(@as(?u64, 4), cursors.control_requests);
    const store = try reopened.acquire();
    defer reopened.release();
    try testing.expectEqualStrings("spam.example", (try store_mod.read.blockedDomains(store.db(), arena))[0].subject);
    try testing.expectEqual(store_mod.record.Override.hide, (try store_mod.read.override(store.db(), arena, bob)).?.action);
    try testing.expect((try store.actor(arena, alice)).?.identity_due != 0);
}
