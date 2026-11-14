//! The enrichment loop against a fake appview, a fake Blacksky and fake
//! PDSes, under a clock that moves only when the loop sleeps.

const std = @import("std");
const bridge_mod = @import("../bridge/root.zig");
const store_mod = @import("../store/root.zig");
const corpus_mod = @import("corpus.zig");
const enrich = @import("enrich.zig");
const http = @import("http.zig");
const index_bridge = @import("index_bridge.zig");
const harness = @import("testing.zig");
const Change = store_mod.Change;
const Fixture = harness.Fixture;
const testing = std.testing;

const appview = "https://appview.test";
const blacksky = "https://blacksky.test";
const mod = store_mod.moderation.bsky_moderation;

const Request = struct { url: []const u8, at_ms: i64 };

const World = struct {
    f: *Fixture,
    arena: std.mem.Allocator,
    ms: i64,
    requests: std.ArrayList(Request) = .empty,
    /// Profile JSON by DID, as each appview holds it.
    bsky: std.StringHashMapUnmanaged([]const u8) = .empty,
    black: std.StringHashMapUnmanaged([]const u8) = .empty,
    /// Record bodies by `origin collection did`.
    records: std.StringHashMapUnmanaged([]const u8) = .empty,
    /// Why `getProfile` refuses a DID; anything else is "Profile not found".
    refusals: std.StringHashMapUnmanaged([]const u8) = .empty,
    /// Answers for the next `getProfiles` calls, before the map is consulted.
    scripted: std.ArrayList(enrich.Response) = .empty,
    probe_status: u16 = 400,
    /// A DID whose presence makes `getProfiles` answer 400.
    refused: []const u8 = "",

    fn nowMs(context: *anyopaque) i64 {
        const self: *World = @ptrCast(@alignCast(context));
        return self.ms;
    }

    fn sleepMs(context: *anyopaque, ms: u64) bool {
        const self: *World = @ptrCast(@alignCast(context));
        self.ms += @intCast(ms);
        self.f.clock.at.store(@intCast(@divFloor(self.ms, 1000)), .monotonic);
        return true;
    }

    fn decoded(self: *World, text: []const u8) []const u8 {
        const out = self.arena.dupe(u8, text) catch @panic("out of memory");
        return std.Uri.percentDecodeInPlace(out);
    }

    fn parameter(self: *World, url: []const u8, name: []const u8) []const u8 {
        const query = url[std.mem.indexOfScalar(u8, url, '?').? + 1 ..];
        var parts = std.mem.splitScalar(u8, query, '&');
        while (parts.next()) |part| {
            if (std.mem.startsWith(u8, part, name) and part.len > name.len and part[name.len] == '=') return self.decoded(part[name.len + 1 ..]);
        }
        return "";
    }

    fn get(context: *anyopaque, _: std.mem.Allocator, url: []const u8) enrich.Response {
        const self: *World = @ptrCast(@alignCast(context));
        self.requests.append(self.arena, .{ .url = self.arena.dupe(u8, url) catch @panic("out of memory"), .at_ms = self.ms }) catch @panic("out of memory");
        if (std.mem.startsWith(u8, url, appview ++ "/xrpc/app.bsky.actor.getProfiles?")) {
            if (self.scripted.items.len > 0) return self.scripted.orderedRemove(0);
            var body: std.ArrayList(u8) = .empty;
            body.appendSlice(self.arena, "{\"profiles\":[") catch @panic("out of memory");
            var parts = std.mem.splitScalar(u8, url[std.mem.indexOfScalar(u8, url, '?').? + 1 ..], '&');
            var asked: usize = 0;
            var held: usize = 0;
            while (parts.next()) |part| {
                const did = self.decoded(part["actors=".len..]);
                asked += 1;
                if (self.refused.len > 0 and std.mem.eql(u8, did, self.refused)) return .{ .status = 400, .body = "{\"error\":\"InvalidRequest\"}" };
                const found = self.bsky.get(did) orelse continue;
                if (held > 0) body.append(self.arena, ',') catch @panic("out of memory");
                body.appendSlice(self.arena, found) catch @panic("out of memory");
                held += 1;
            }
            std.debug.assert(asked <= enrich.get_profiles_max);
            body.appendSlice(self.arena, "]}") catch @panic("out of memory");
            return .{ .status = 200, .body = body.items };
        }
        if (std.mem.startsWith(u8, url, appview ++ "/xrpc/app.bsky.actor.getProfile?")) {
            const did = self.parameter(url, "actor");
            if (self.probe_status != 400) return .{ .status = self.probe_status };
            const reason = self.refusals.get(did) orelse "InvalidRequest";
            return .{ .status = 400, .body = std.fmt.allocPrint(self.arena, "{{\"error\":\"{s}\",\"message\":\"no\"}}", .{reason}) catch @panic("out of memory") };
        }
        if (std.mem.startsWith(u8, url, blacksky ++ "/xrpc/app.bsky.actor.getProfile?")) {
            const found = self.black.get(self.parameter(url, "actor")) orelse return .{ .status = 400, .body = "{\"error\":\"InvalidRequest\"}" };
            return .{ .status = 200, .body = found };
        }
        if (std.mem.indexOf(u8, url, "/xrpc/com.atproto.repo.getRecord?")) |at| {
            const key = std.fmt.allocPrint(self.arena, "{s} {s} {s}", .{ url[0..at], self.parameter(url, "collection"), self.parameter(url, "repo") }) catch @panic("out of memory");
            const found = self.records.get(key) orelse return .{ .status = 400, .body = "{\"error\":\"RecordNotFound\"}" };
            return .{ .status = 200, .body = found };
        }
        return .{ .status = 404 };
    }

    fn count(self: *const World, needle: []const u8) usize {
        var n: usize = 0;
        for (self.requests.items) |request| n += @intFromBool(std.mem.indexOf(u8, request.url, needle) != null);
        return n;
    }
};

const Rig = struct {
    f: *Fixture,
    world: *World,
    enricher: enrich.Enricher,

    fn create(options: enrich.Options) !*Rig {
        const f = try Fixture.create(.{ .idle_wait_ms = 1 });
        errdefer f.destroy();
        const arena = f.arena.allocator();
        const self = try arena.create(Rig);
        const world = try arena.create(World);
        world.* = .{ .f = f, .arena = arena, .ms = @as(i64, harness.t0) * 1000 };
        var with_hosts = options;
        with_hosts.appview_url = appview;
        with_hosts.blacksky_url = blacksky;
        self.* = .{ .f = f, .world = world, .enricher = .{
            .gpa = testing.allocator,
            .owner = f.owner,
            .transport = .{ .context = world, .get = World.get },
            .timer = .{ .context = world, .now_ms = World.nowMs, .sleep_ms = World.sleepMs },
            .options = with_hosts,
        } };
        return self;
    }

    fn destroy(self: *Rig) void {
        self.f.destroy();
    }

    fn did(self: *Rig, n: usize) []const u8 {
        return std.fmt.allocPrint(self.f.arena.allocator(), "did:plc:{d:0>24}", .{n}) catch @panic("out of memory");
    }

    /// An actor with a handle and a PDS, queued for refresh as a profile
    /// write queues it.
    fn seed(self: *Rig, n: usize) ![]const u8 {
        const subject = self.did(n);
        const handle = try std.fmt.allocPrint(self.f.arena.allocator(), "user{d}.test", .{n});
        _ = try self.f.commit(&.{.{ .profile = .{ .did = subject, .handle = handle, .pds = "https://pds.test" } }}, null);
        return subject;
    }

    fn known(self: *Rig, subject: []const u8, rest: []const u8) !void {
        const body = try std.fmt.allocPrint(self.f.arena.allocator(), "{{\"did\":\"{s}\"{s}{s}}}", .{ subject, if (rest.len > 0) "," else "", rest });
        try self.world.bsky.put(self.f.arena.allocator(), subject, body);
    }

    /// Run `count` steps with the owner on its own thread.
    fn steps(self: *Rig, times: usize) !enrich.Step {
        var running = try harness.Running.start(self.f);
        defer {
            running.stop();
            self.f.owner.stopped = false;
        }
        var last: enrich.Step = .idle;
        for (0..times) |_| last = try self.enricher.step();
        return last;
    }

    fn drain(self: *Rig) !usize {
        var running = try harness.Running.start(self.f);
        defer {
            running.stop();
            self.f.owner.stopped = false;
        }
        var worked: usize = 0;
        while (try self.enricher.step() == .worked) worked += 1;
        return worked;
    }

    fn queued(self: *Rig) !usize {
        const store = try self.f.owner.acquire();
        defer self.f.owner.release();
        return (try store.timed(self.f.arena.allocator(), .refresh, null, std.math.maxInt(u64), 100_000)).len;
    }

    /// How many live actors the handle range lists under `handle`.
    fn holders(self: *Rig, handle: []const u8) !usize {
        const store = try self.f.owner.acquire();
        defer self.f.owner.release();
        return (try store.byHandle(self.f.arena.allocator(), handle)).len;
    }

    fn load(self: *const Rig, counter: *const std.atomic.Value(u64)) u64 {
        _ = self;
        return counter.load(.monotonic);
    }
};

const quiet: enrich.Options = .{ .requests_per_second = 2, .walk_per_hour = 0, .sweep_per_hour = 0 };
const counts = "\"handle\":\"fresh.test\",\"displayName\":\"Fresh\",\"followersCount\":1234,\"followsCount\":321,\"postsCount\":4567,\"createdAt\":\"2024-01-01T00:00:00.000Z\"";

test "due entries are fetched twenty-five at a time and settled" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    for (0..60) |n| try rig.known(try rig.seed(n), counts);
    try testing.expectEqual(@as(usize, 60), try rig.queued());

    try testing.expectEqual(@as(usize, 3), try rig.drain());
    try testing.expectEqual(@as(usize, 3), rig.world.count("getProfiles"));
    try testing.expectEqual(@as(usize, 3), rig.world.requests.items.len);
    try testing.expectEqual(@as(usize, 25), std.mem.count(u8, rig.world.requests.items[0].url, "actors="));
    try testing.expectEqual(@as(usize, 10), std.mem.count(u8, rig.world.requests.items[2].url, "actors="));
    try testing.expect(std.mem.indexOf(u8, rig.world.requests.items[0].url, "actors=did%3Aplc%3A000000000000000000000000&actors=") != null);
    try testing.expectEqual(@as(usize, 0), try rig.queued());

    const actor = try rig.f.live(rig.did(7));
    try testing.expectEqualStrings("user7.test", actor.handle);
    try testing.expectEqualStrings("Fresh", actor.display_name);
    try testing.expectEqual(@as(u64, 1234), actor.followers_count);
    try testing.expectEqual(store_mod.record.ProfileSource.bsky, actor.profile_source);
    try testing.expect(actor.quality_score > 0 and actor.profile_checked_at >= harness.t0);
    try testing.expectEqual(@as(u64, 60), rig.load(&rig.enricher.stats.leased));
    try testing.expectEqual(@as(u64, 60), rig.load(&rig.enricher.stats.enriched));
    try testing.expectEqual(@as(u64, 60), rig.load(&rig.enricher.stats.changed));
    try testing.expect(rig.load(&rig.enricher.stats.last_success_at) >= harness.t0);
}

test "a burst of due entries never exceeds the configured request rate" {
    const rig = try Rig.create(.{ .requests_per_second = 4, .jitter = 0.5, .walk_per_hour = 0, .sweep_per_hour = 0 });
    defer rig.destroy();
    for (0..400) |n| {
        const subject = try rig.seed(n);
        if (n % 3 != 0) try rig.known(subject, counts);
    }
    _ = try rig.drain();
    const requests = rig.world.requests.items;
    try testing.expect(requests.len > 16 + 100);
    var widest: i64 = 0;
    for (requests[0 .. requests.len - 1], requests[1..]) |a, b| {
        try testing.expect(b.at_ms - a.at_ms >= 250);
        widest = @max(widest, b.at_ms - a.at_ms);
    }
    try testing.expect(widest <= 375);
    const elapsed = requests[requests.len - 1].at_ms - requests[0].at_ms;
    try testing.expect(@as(f64, @floatFromInt(requests.len - 1)) * 1000 / @as(f64, @floatFromInt(elapsed)) <= 4.0);
    try testing.expectEqual(@as(usize, 0), try rig.queued());
}

test "a DID the appview leaves out goes to Blacksky, then its PDS" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    const a = rig.f.arena.allocator();
    const taken_down = try rig.seed(1);
    const on_pds = try rig.seed(2);
    const tangled = try rig.seed(3);
    const shown = try rig.seed(4);
    const hidden = try rig.seed(5);
    const deactivated = try rig.seed(6);
    const nowhere = try rig.seed(7);
    const no_pds = rig.did(8);
    const bad_pds = rig.did(9);
    _ = try rig.f.commit(&.{
        .{ .profile = .{ .did = no_pds, .handle = "nopds.test" } },
        .{ .profile = .{ .did = bad_pds, .handle = "badpds.test", .pds = "https://user:pw@pds.test" } },
        .{ .override_set = .{ .did = shown, .action = .show } },
        .{ .override_set = .{ .did = hidden, .action = .hide } },
        .{ .label = .{ .did = on_pds, .source = mod, .value = "!takedown", .created = "2026-11-01T00:00:00.000Z", .sequence = 9 } },
    }, null);
    for ([_][]const u8{ taken_down, on_pds, tangled, nowhere, bad_pds }) |subject| try rig.world.refusals.put(a, subject, "AccountTakedown");
    try rig.world.refusals.put(a, deactivated, "AccountDeactivated");
    const black = try std.fmt.allocPrint(a, "{{\"did\":\"{s}\",\"handle\":\"elsewhere.test\",\"displayName\":\"On Blacksky\",\"followersCount\":25800,\"followsCount\":10,\"postsCount\":900,\"labels\":[]}}", .{taken_down});
    try rig.world.black.put(a, taken_down, black);
    try rig.world.black.put(a, shown, try std.fmt.allocPrint(a, "{{\"did\":\"{s}\",\"displayName\":\"Shown\",\"followersCount\":7}}", .{shown}));
    try rig.world.black.put(a, tangled, "{\"did\":\"did:plc:someoneelse\",\"displayName\":\"Wrong\"}");
    try rig.world.records.put(a, try std.fmt.allocPrint(a, "https://pds.test app.bsky.actor.profile {s}", .{on_pds}), "{\"value\":{\"displayName\":\"From PDS\",\"description\":\"dropped\",\"avatar\":{\"ref\":{\"$link\":\"bafkreiavatar\"}},\"createdAt\":\"2024-05-05T00:00:00.000Z\"}}");
    try rig.world.records.put(a, try std.fmt.allocPrint(a, "https://pds.test sh.tangled.actor.profile {s}", .{tangled}), "{\"value\":{\"description\":\"tangled only\"}}");

    try testing.expectEqual(@as(usize, 1), try rig.drain());
    const urls = rig.world.requests.items;
    var order: std.ArrayList(u8) = .empty;
    for (urls) |request| {
        const kind: []const u8 = if (std.mem.indexOf(u8, request.url, "getProfiles") != null) "batch" else if (std.mem.startsWith(u8, request.url, blacksky)) "blacksky" else if (std.mem.indexOf(u8, request.url, "sh.tangled") != null) "tangled" else if (std.mem.indexOf(u8, request.url, "getRecord") != null) "record" else "probe";
        const subject = rig.world.parameter(request.url, if (std.mem.indexOf(u8, request.url, "getRecord") != null) "repo" else if (std.mem.eql(u8, kind, "batch")) "actors" else "actor");
        try order.print(a, "{s}:{s} ", .{ kind, subject[subject.len - 1 ..] });
    }
    try testing.expectEqualStrings(
        "batch:1 probe:1 blacksky:1 probe:2 blacksky:2 record:2 probe:3 blacksky:3 record:3 tangled:3 blacksky:4 probe:6 probe:7 blacksky:7 record:7 tangled:7 probe:9 blacksky:9 ",
        order.items,
    );

    const first = try rig.f.live(taken_down);
    try testing.expectEqual(store_mod.record.ProfileSource.blacksky, first.profile_source);
    try testing.expectEqualStrings("user1.test", first.handle);
    try testing.expect(try rig.f.queued(taken_down) != null);
    try testing.expect(try rig.f.queued(on_pds) == null);
    try testing.expectEqualStrings("On Blacksky", first.display_name);
    try testing.expectEqual(@as(u64, 25800), first.followers_count);
    try testing.expect(first.quality_score > 400);

    const second = try rig.f.live(on_pds);
    try testing.expectEqual(store_mod.record.ProfileSource.pds, second.profile_source);
    try testing.expectEqualStrings("From PDS", second.display_name);
    try testing.expectEqualStrings("bafkreiavatar", second.avatar);
    try testing.expectEqualStrings("", second.description);
    try testing.expectEqualStrings("2024-05-05T00:00:00.000Z", second.created_at);
    try testing.expectEqual(@as(f32, 0), second.quality_score);
    try testing.expectEqual(@as(usize, 1), second.labels.len);
    try testing.expect(second.hidden);

    try testing.expectEqual(store_mod.record.ProfileSource.pds, (try rig.f.live(tangled)).profile_source);
    try testing.expectEqualStrings("Shown", (try rig.f.live(shown)).display_name);
    try testing.expectEqual(store_mod.record.ProfileSource.blacksky, (try rig.f.live(shown)).profile_source);
    for ([_][]const u8{ hidden, deactivated, nowhere, no_pds, bad_pds }) |subject| {
        const actor = try rig.f.live(subject);
        try testing.expectEqual(store_mod.record.ProfileSource.none, actor.profile_source);
        try testing.expect(actor.profile_checked_at >= harness.t0);
        try testing.expectEqual(@as(u64, 0), actor.refresh_due);
    }
    try testing.expectEqual(@as(usize, 0), try rig.queued());
    try testing.expectEqual(@as(u64, 2), rig.load(&rig.enricher.stats.from_blacksky));
    try testing.expectEqual(@as(u64, 2), rig.load(&rig.enricher.stats.from_pds));
    try testing.expectEqual(@as(u64, 5), rig.load(&rig.enricher.stats.not_found));
    try testing.expectEqual(@as(u64, 0), rig.f.owner.counters.actors_gone.load(.monotonic));
}

fn refreshNow(rig: *Rig, subject: []const u8) !void {
    _ = try rig.f.commit(&.{.{ .refresh_enqueue = .{ .did = subject, .due = rig.f.now() } }}, null);
}

/// Settle `subject`'s identity queue entry as this role's resolver would
/// after verifying `handle`.
fn resolveTo(rig: *Rig, subject: []const u8, handle: []const u8) !void {
    const state = (try rig.f.queued(subject)).?;
    _ = try rig.f.commit(&.{.{ .identity_complete = .{ .did = subject, .generation = state.generation, .observed_at = rig.f.now(), .outcome = .{ .resolved = .{ .handle = handle } } } }}, null);
}

test "an appview that answers handle.invalid leaves a valid handle and queues its verification" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    const subject = try rig.seed(1);
    try rig.known(subject, "\"handle\":\"handle.invalid\",\"displayName\":\"Still Here\",\"followersCount\":10");
    try testing.expectEqual(@as(usize, 1), try rig.drain());

    var actor = try rig.f.live(subject);
    try testing.expectEqualStrings("user1.test", actor.handle);
    try testing.expectEqualStrings("Still Here", actor.display_name);
    try testing.expectEqual(@as(u64, 10), actor.followers_count);
    try testing.expect(!actor.hidden);
    try testing.expectEqual(@as(usize, 1), try rig.holders("user1.test"));
    try testing.expectEqual(@as(usize, 0), try rig.holders("handle.invalid"));
    try testing.expectEqual(store_mod.record.IdentityReason.identity, (try rig.f.queued(subject)).?.reason);

    try resolveTo(rig, subject, "user1.test");
    try testing.expectEqual(@as(usize, 1), try rig.drain());
    actor = try rig.f.live(subject);
    try testing.expectEqualStrings("user1.test", actor.handle);
    try testing.expectEqual(@as(u64, 0), actor.identity_due);

    _ = World.sleepMs(rig.world, 86_400_000);
    try refreshNow(rig, subject);
    try testing.expectEqual(@as(usize, 1), try rig.drain());
    try testing.expectEqualStrings("user1.test", (try rig.f.live(subject)).handle);
    try testing.expect(try rig.f.queued(subject) != null);
}

test "an appview that still serves an old handle does not overwrite the resolved one" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    const subject = rig.did(1);
    try rig.known(subject, "\"handle\":\"old.scenius.test\",\"followersCount\":10");
    _ = try rig.f.commit(&.{
        .{ .observe = .{ .did = subject } },
        .{ .identity_resolved = .{ .did = subject, .observed_at = rig.f.now() - 90_000, .handle = "new.techne.test", .pds = "https://pds.test" } },
    }, null);
    try testing.expectEqual(@as(usize, 1), try rig.drain());

    const actor = try rig.f.live(subject);
    try testing.expectEqualStrings("new.techne.test", actor.handle);
    try testing.expectEqual(@as(u64, 10), actor.followers_count);
    try testing.expect(try rig.f.queued(subject) != null);

    try resolveTo(rig, subject, "new.techne.test");
    try testing.expectEqual(@as(usize, 1), try rig.drain());
    try testing.expectEqualStrings("new.techne.test", (try rig.f.live(subject)).handle);
    try testing.expect(try rig.f.queued(subject) == null);
}

test "an appview's handle for an actor without one is verified, not written" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    const subject = rig.did(1);
    try rig.known(subject, "\"handle\":\"claimed.test\",\"followersCount\":10");
    _ = try rig.f.commit(&.{.{ .observe = .{ .did = subject } }}, null);
    try refreshNow(rig, subject);
    try testing.expectEqual(@as(usize, 1), try rig.drain());

    const actor = try rig.f.live(subject);
    try testing.expectEqualStrings("", actor.handle);
    try testing.expectEqual(@as(u64, 10), actor.followers_count);
    try testing.expect(try rig.f.queued(subject) != null);
    try testing.expectEqual(@as(usize, 0), try rig.holders("claimed.test"));
}

test "handle disagreements queue no more identity work than the backlog limit, and leave queued work alone" {
    const rig = try Rig.create(.{ .requests_per_second = 2, .walk_per_hour = 0, .sweep_per_hour = 0, .handle_check = .{ .backlog_limit = 3 } });
    defer rig.destroy();
    const waiting = try rig.seed(0);
    _ = try rig.f.commit(&.{
        .{ .identity_enqueue = .{ .did = waiting, .reason = .plc } },
        .{ .identity_lease = .{ .limit = 1 } },
    }, null);
    try rig.known(waiting, "\"handle\":\"handle.invalid\"");
    for (1..11) |n| try rig.known(try rig.seed(n), "\"handle\":\"handle.invalid\"");
    try testing.expectEqual(@as(usize, 1), try rig.drain());

    var queued: usize = 0;
    for (1..11) |n| {
        try testing.expectEqualStrings(try std.fmt.allocPrint(rig.f.arena.allocator(), "user{d}.test", .{n}), (try rig.f.live(rig.did(n))).handle);
        queued += @intFromBool(try rig.f.queued(rig.did(n)) != null);
    }
    try testing.expectEqual(@as(usize, 3), queued);
    try testing.expectEqual(@as(u64, 3), rig.load(&rig.enricher.stats.handle_queued));
    try testing.expectEqual(@as(u64, 7), rig.load(&rig.enricher.stats.handle_deferred));
    try testing.expectEqual(@as(u64, 1), rig.load(&rig.enricher.stats.handle_pending));
    try testing.expectEqual(@as(u64, 0), rig.load(&rig.enricher.stats.handle_settled));
    const held = (try rig.f.queued(waiting)).?;
    try testing.expectEqual(store_mod.record.IdentityReason.plc, held.reason);
    try testing.expectEqual(@as(u64, 1), held.generation);
    try testing.expectEqual(@as(u64, 1), held.attempts);

    for (1..11) |n| {
        if (try rig.f.queued(rig.did(n)) != null) try resolveTo(rig, rig.did(n), try std.fmt.allocPrint(rig.f.arena.allocator(), "user{d}.test", .{n}));
        try refreshNow(rig, rig.did(n));
    }
    try testing.expectEqual(@as(usize, 1), try rig.drain());
    queued = 0;
    for (1..11) |n| queued += @intFromBool(try rig.f.queued(rig.did(n)) != null);
    try testing.expectEqual(@as(usize, 3), queued);
    try testing.expectEqual(@as(u64, 6), rig.load(&rig.enricher.stats.handle_queued));
    try testing.expectEqual(@as(u64, 11), rig.load(&rig.enricher.stats.handle_deferred));
    try testing.expectEqual(@as(u64, 3), rig.load(&rig.enricher.stats.handle_settled));

    const reader = try store_mod.Reader.open(testing.allocator, testing.io, rig.f.tmp.dir, "store", rig.f.owner.options.store.layout);
    defer reader.close();
    var signals: @import("intake.zig").Signals = .{};
    var ignored: std.atomic.Value(u64) = .init(0);
    const context: http.Context = .{ .owner = rig.f.owner, .reader = reader, .intake = &signals, .labels_ignored = &ignored, .read_secret = null, .enricher = &rig.enricher };
    const a = rig.f.arena.allocator();
    const response = try http.respond(&context, a, .GET, "/health", null, "");
    const body = try std.json.parseFromSliceLeaky(http.HealthBody, a, response.body, .{ .ignore_unknown_fields = true });
    const handles = body.enrichment.?.handles;
    try testing.expectEqual(@as(u64, 86_400), handles.settle_seconds);
    try testing.expectEqual(@as(u64, 3), handles.backlog_limit);
    try testing.expectEqual(@as(u64, 21), handles.disagreed);
    try testing.expectEqual(@as(u64, 6), handles.queued);
    try testing.expectEqual(@as(u64, 1), handles.pending);
    try testing.expectEqual(@as(u64, 3), handles.settled);
    try testing.expectEqual(@as(u64, 11), handles.deferred);
}

test "a request that teaches nothing reschedules its entries and backs the loop off" {
    const rig = try Rig.create(.{ .requests_per_second = 2, .jitter = 0, .walk_per_hour = 0, .sweep_per_hour = 0, .backoff_seconds = 30, .backoff_max_seconds = 100 });
    defer rig.destroy();
    for (0..3) |n| try rig.known(try rig.seed(n), counts);
    const w = rig.world;
    const a = rig.f.arena.allocator();
    try w.scripted.append(a, .{ .status = 429 });
    try w.scripted.append(a, .{ .status = 503 });
    try w.scripted.append(a, .{ .status = 0 });
    try w.scripted.append(a, .{ .status = 200, .body = "{\"oops\":true}" });
    try w.scripted.append(a, .{ .status = 429, .retry_after_seconds = 70 });

    const start = w.ms;
    try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
    try testing.expectEqual(@as(u64, 3), rig.load(&rig.enricher.stats.retried));
    try testing.expectEqual(@as(u64, 1), rig.load(&rig.enricher.stats.rate_limited));
    var actor = try rig.f.live(rig.did(0));
    try testing.expectEqual(@as(u64, 0), actor.profile_checked_at);
    try testing.expectEqual(@as(u64, harness.t0 + 31), actor.refresh_due);
    try testing.expectEqual(@as(usize, 3), try rig.queued());
    try testing.expectEqual(@as(u64, harness.t0 + 30), rig.load(&rig.enricher.stats.backoff_until));

    try testing.expectEqual(enrich.Step.idle, try rig.steps(1));
    try testing.expectEqual(@as(usize, 1), w.requests.items.len);

    var waits: [5]i64 = undefined;
    waits[0] = 0;
    for (1..5) |round| {
        _ = World.sleepMs(w, 200_000);
        const before = w.ms;
        try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
        waits[round] = rig.enricher.blocked_until_ms - before;
    }
    try testing.expectEqual(@as(i64, 60_000), waits[1]);
    try testing.expectEqual(@as(i64, 100_000), waits[2]);
    try testing.expectEqual(@as(i64, 100_000), waits[3]);
    try testing.expectEqual(@as(i64, 100_000), waits[4]);
    try testing.expectEqual(@as(u64, 5), rig.load(&rig.enricher.stats.failed));
    try testing.expectEqual(@as(u64, 2), rig.load(&rig.enricher.stats.rate_limited));
    try testing.expectEqual(@as(u64, 5), rig.load(&rig.enricher.stats.consecutive_failures));
    try testing.expectEqual(@as(u64, 0), rig.load(&rig.enricher.stats.enriched));
    try testing.expect(w.ms > start);

    _ = World.sleepMs(w, 200_000);
    const before = w.ms;
    try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
    try testing.expectEqual(before, w.requests.items[w.requests.items.len - 1].at_ms);
    try testing.expectEqual(@as(u64, 0), rig.load(&rig.enricher.stats.consecutive_failures));
    try testing.expectEqual(@as(u64, 3), rig.load(&rig.enricher.stats.enriched));
    actor = try rig.f.live(rig.did(0));
    try testing.expectEqual(@as(u64, 1234), actor.followers_count);
    try testing.expectEqual(@as(usize, 0), try rig.queued());
}

test "the loop waits out a pause before its next request" {
    const rig = try Rig.create(.{ .requests_per_second = 2, .jitter = 0, .walk_per_hour = 0, .sweep_per_hour = 0, .backoff_seconds = 30 });
    defer rig.destroy();
    for (0..30) |n| try rig.known(try rig.seed(n), counts);
    try rig.world.scripted.append(rig.f.arena.allocator(), .{ .status = 429 });
    try testing.expectEqual(enrich.Step.worked, try rig.steps(2));
    const requests = rig.world.requests.items;
    try testing.expectEqual(@as(usize, 2), requests.len);
    try testing.expectEqual(@as(i64, 30_000), requests[1].at_ms - requests[0].at_ms);
    try testing.expectEqual(@as(u64, 5), rig.load(&rig.enricher.stats.enriched));
}

test "a failed takedown probe leaves its DID for later and settles the rest" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    try rig.known(try rig.seed(1), counts);
    const missing = try rig.seed(2);
    rig.world.probe_status = 429;
    try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
    try testing.expectEqual(@as(u64, 1), rig.load(&rig.enricher.stats.enriched));
    try testing.expectEqual(@as(u64, 1), rig.load(&rig.enricher.stats.retried));
    const actor = try rig.f.live(missing);
    try testing.expectEqual(@as(u64, 0), actor.profile_checked_at);
    try testing.expect(actor.refresh_due > rig.f.now());
    try testing.expectEqual(@as(usize, 1), try rig.queued());
}

test "a batch the appview refuses is split until the refused DID stands alone" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    for (0..25) |n| try rig.known(try rig.seed(n), counts);
    rig.world.refused = rig.did(13);
    try testing.expectEqual(@as(usize, 1), try rig.drain());
    try testing.expectEqual(@as(u64, 24), rig.load(&rig.enricher.stats.enriched));
    try testing.expectEqual(@as(u64, 1), rig.load(&rig.enricher.stats.not_found));
    try testing.expectEqual(@as(u64, 0), rig.load(&rig.enricher.stats.failed));
    try testing.expect(rig.load(&rig.enricher.stats.split) >= 4);
    try testing.expectEqual(@as(u64, 0), (try rig.f.live(rig.did(13))).followers_count);
    try testing.expectEqual(@as(u64, 1234), (try rig.f.live(rig.did(12))).followers_count);
    try testing.expectEqual(@as(usize, 0), try rig.queued());
}

test "zero counts and a countless profile keep the stored counts and score" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    const zeroed = try rig.seed(1);
    const countless = try rig.seed(2);
    const partial = try rig.seed(3);
    for ([_][]const u8{ zeroed, countless, partial }) |subject| try rig.known(subject, counts);
    _ = try rig.drain();
    const before = try rig.f.live(zeroed);
    try testing.expect(before.quality_score > 300);

    try rig.known(zeroed, "\"followersCount\":0,\"followsCount\":0,\"postsCount\":0");
    try rig.known(countless, "\"displayName\":\"No Counts\",\"createdAt\":\"2020-01-01T00:00:00.000Z\"");
    try rig.known(partial, "\"followersCount\":0,\"followsCount\":9,\"postsCount\":0,\"createdAt\":\"2024-01-01T00:00:00.000Z\"");
    _ = try rig.f.commit(&.{
        .{ .refresh_enqueue = .{ .did = zeroed, .due = rig.f.now() } },
        .{ .refresh_enqueue = .{ .did = countless, .due = rig.f.now() } },
        .{ .refresh_enqueue = .{ .did = partial, .due = rig.f.now() } },
    }, null);
    _ = try rig.drain();

    for ([_][]const u8{ zeroed, countless }) |subject| {
        const after = try rig.f.live(subject);
        try testing.expectEqual(@as(u64, 1234), after.followers_count);
        try testing.expectEqual(@as(u64, 321), after.follows_count);
        try testing.expectEqual(@as(u64, 4567), after.posts_count);
        try testing.expectEqual(@as(u32, @bitCast(before.quality_score)), @as(u32, @bitCast(after.quality_score)));
    }
    try testing.expectEqualStrings("No Counts", (try rig.f.live(countless)).display_name);
    const mixed = try rig.f.live(partial);
    try testing.expectEqual(@as(u64, 1234), mixed.followers_count);
    try testing.expectEqual(@as(u64, 9), mixed.follows_count);
    try testing.expectEqual(@as(u64, 4567), mixed.posts_count);
    try testing.expectEqual(@as(f32, 30), mixed.quality_score);
}

test "an enrichment moves the actor's quality key, logs one change, and reaches the index" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    const subject = try rig.seed(1);
    try rig.known(subject, "\"handle\":\"user1.test\",\"followersCount\":10,\"followsCount\":5,\"postsCount\":3");
    _ = try rig.drain();
    const a = rig.f.arena.allocator();
    const low = try rig.f.live(subject);
    var buf: store_mod.keys.Buf = undefined;

    var running = try harness.Running.start(rig.f);
    var source = try index_bridge.StoreSource.open(testing.allocator, rig.f.owner);
    var server: bridge_mod.testing.ModelServer = .init(testing.allocator);
    var bridge = bridge_mod.Bridge.init(testing.allocator, testing.io, .{ .source_id = "store-under-test" }, source.interface(), server.transport());
    defer {
        bridge.deinit();
        running.stop();
        source.close();
        server.deinit();
    }
    for (0..200) |_| if ((try bridge.step()).idle) break;
    try testing.expectEqual(@as(f64, low.quality_score), switch ((try server.field(a, subject, "quality_score")).?) {
        .float => |value| value,
        .integer => |value| @as(f64, @floatFromInt(value)),
        else => unreachable,
    });

    const logged = (try rig.f.cursors()).next_change;
    try rig.known(subject, "\"handle\":\"user1.test\",\"followersCount\":99999,\"followsCount\":5,\"postsCount\":3,\"labels\":[{\"src\":\"did:plc:other\",\"uri\":\"did:plc:000000000000000000000001\",\"val\":\"bot\",\"cts\":\"2026-01-01T00:00:00.000Z\"}]");
    const ask = try @import("owner.zig").Submission.create(testing.allocator);
    defer ask.destroy();
    ask.changes = &.{.{ .refresh_enqueue = .{ .did = subject, .due = rig.f.now() } }};
    try rig.f.owner.call(ask);
    try testing.expectEqual(enrich.Step.worked, try rig.enricher.step());
    for (0..200) |_| if ((try bridge.step()).idle) break;
    running.stop();
    rig.f.owner.stopped = false;

    const high = try rig.f.live(subject);
    try testing.expect(high.quality_score > low.quality_score + 300);
    try testing.expectEqual(@as(u64, 99999), high.followers_count);
    try testing.expectEqual(logged + 1, (try rig.f.cursors()).next_change);
    const store = try rig.f.owner.acquire();
    const walk = try store.quality(a, null, 10);
    const old_key = try store.db().get(try store_mod.keys.quality(&buf, low.quality_score, subject));
    rig.f.owner.release();
    try testing.expectEqual(@as(usize, 1), walk.len);
    try testing.expectEqual(@as(u32, @bitCast(high.quality_score)), @as(u32, @bitCast(walk[0].score)));
    try testing.expect(old_key == null);

    const indexed = (try server.field(a, subject, "quality_score")).?;
    try testing.expectEqual(@as(f64, high.quality_score), indexed.float);
    try testing.expectEqual(@as(i64, 99999), (try server.field(a, subject, "followers_count")).?.integer);
}

test "leased entries are due again after a restart and nothing is lost" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    for (0..40) |n| try rig.known(try rig.seed(n), counts);
    const leased = try rig.f.commit(&.{.{ .refresh_lease = .{ .limit = 25, .lease_seconds = 300 } }}, null);
    try testing.expectEqual(@as(usize, 25), leased.refresh_jobs.len);

    try rig.f.restart();
    rig.enricher.owner = rig.f.owner;
    try testing.expectEqual(@as(usize, 40), try rig.queued());
    try testing.expectEqual(@as(usize, 1), try rig.drain());
    try testing.expectEqual(@as(u64, 15), rig.load(&rig.enricher.stats.enriched));
    try testing.expectEqual(@as(usize, 25), try rig.queued());

    _ = World.sleepMs(rig.world, 301_000);
    try testing.expectEqual(@as(usize, 1), try rig.drain());
    try testing.expectEqual(@as(u64, 40), rig.load(&rig.enricher.stats.enriched));
    try testing.expectEqual(@as(usize, 0), try rig.queued());
    for (0..40) |n| try testing.expectEqual(@as(u64, 1234), (try rig.f.live(rig.did(n))).followers_count);
}

test "the walks queue ranked actors at their hourly rate and the rest only while the queue keeps up" {
    const rig = try Rig.create(.{ .requests_per_second = 2, .jitter = 0, .walk_per_hour = 3600, .sweep_per_hour = 1800 });
    defer rig.destroy();
    var seeds: std.ArrayList(Change) = .empty;
    const a = rig.f.arena.allocator();
    for (0..120) |n| {
        const subject = rig.did(n);
        try rig.known(subject, counts);
        try seeds.append(a, .{ .profile = .{ .did = subject, .handle = try std.fmt.allocPrint(a, "w{d}.test", .{n}), .pds = "https://pds.test" } });
        try seeds.append(a, if (n < 80) .{ .enrichment = .{ .did = subject, .source = .bsky, .quality_score = @floatFromInt(1000 - n) } } else .{ .refresh_complete = .{ .did = subject } });
    }
    _ = try rig.f.commit(seeds.items, null);
    try testing.expectEqual(@as(usize, 0), try rig.queued());

    try testing.expectEqual(enrich.Step.idle, try rig.steps(1));
    _ = World.sleepMs(rig.world, 30_000);
    try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
    try testing.expectEqual(@as(u64, 30), rig.load(&rig.enricher.stats.walk_queued));
    try testing.expectEqual(@as(u64, 0), rig.load(&rig.enricher.stats.sweep_queued));
    try testing.expectEqual(@as(u64, 25), rig.load(&rig.enricher.stats.leased));
    try testing.expect(rig.enricher.stats.backed_up.load(.monotonic));
    try testing.expectEqual(@as(u64, harness.t0 + 30), (try rig.f.live(rig.did(0))).profile_checked_at);
    try testing.expectEqual(@as(u64, harness.t0), (try rig.f.live(rig.did(40))).profile_checked_at);

    _ = World.sleepMs(rig.world, 60_000);
    try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
    try testing.expect(rig.enricher.stats.backed_up.load(.monotonic));
    try testing.expectEqual(@as(u64, 0), rig.load(&rig.enricher.stats.sweep_queued));
    _ = try rig.drain();
    try testing.expectEqual(@as(u64, 40), rig.load(&rig.enricher.stats.sweep_queued));
    for (0..120) |n| try testing.expect((try rig.f.live(rig.did(n))).profile_checked_at >= harness.t0 + 30);
    try testing.expectEqual(@as(usize, 0), try rig.queued());
    const health = blk: {
        _ = try rig.f.commit(&.{}, null);
        rig.f.advance(61);
        _ = rig.f.owner.turn(0);
        break :blk rig.f.owner.health();
    };
    try testing.expect(health.walks.get(.quality).score > 0);
    try testing.expectEqual(@as(u64, 1), health.walks.get(.rest).passes);
}

test "health reports the loop, both walks with their periods, and the corpus count" {
    const rig = try Rig.create(.{ .requests_per_second = 2, .jitter = 0, .walk_per_hour = 7200, .sweep_per_hour = 7200 });
    defer rig.destroy();
    const a = rig.f.arena.allocator();
    var seeds: std.ArrayList(Change) = .empty;
    for (0..50) |n| {
        const subject = rig.did(n);
        if (n != 49) try rig.known(subject, counts);
        try seeds.append(a, .{ .profile = .{ .did = subject, .handle = try std.fmt.allocPrint(a, "h{d}.test", .{n}), .avatar = if (n < 10) "bafkreiavatar" else "" } });
        try seeds.append(a, if (n < 30) .{ .enrichment = .{ .did = subject, .source = .bsky, .quality_score = @floatFromInt(1000 - n) } } else .{ .refresh_complete = .{ .did = subject } });
    }
    _ = try rig.f.commit(seeds.items, null);
    _ = try rig.steps(1);
    _ = World.sleepMs(rig.world, 20_000);
    try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
    rig.f.advance(61);
    _ = rig.f.owner.turn(0);

    var scanner: corpus_mod.Scanner = .{ .gpa = testing.allocator, .io = testing.io, .root = rig.f.tmp.dir, .path = "store", .layout = rig.f.owner.options.store.layout, .interval_seconds = 3600 };
    scanner.scan();
    const reader = try store_mod.Reader.open(testing.allocator, testing.io, rig.f.tmp.dir, "store", rig.f.owner.options.store.layout);
    defer reader.close();
    var signals: @import("intake.zig").Signals = .{};
    var ignored: std.atomic.Value(u64) = .init(0);
    const context: http.Context = .{ .owner = rig.f.owner, .reader = reader, .intake = &signals, .labels_ignored = &ignored, .read_secret = null, .enricher = &rig.enricher, .corpus = &scanner };
    const response = try http.respond(&context, a, .GET, "/health", null, "");
    const body = try std.json.parseFromSliceLeaky(http.HealthBody, a, response.body, .{ .ignore_unknown_fields = true });

    const loop = body.enrichment.?;
    try testing.expectEqual(@as(f64, 2), loop.requests_per_second);
    try testing.expectEqual(@as(u64, 25), loop.leased);
    try testing.expectEqual(@as(u64, 25), loop.completed);
    try testing.expectEqual(@as(u64, 1), loop.requests);
    try testing.expectEqual(@as(u64, 0), loop.failed + loop.rate_limited + loop.retried);
    try testing.expect(loop.last_success_at >= harness.t0 + 20 and loop.backed_up);
    try testing.expectEqual(@as(u64, 7200), loop.quality_walk.per_hour);
    try testing.expectEqual(@as(u64, 30), loop.quality_walk.queued);
    try testing.expectEqual(@as(u64, 1), loop.quality_walk.passes);
    try testing.expectEqual(@as(?u64, 30), loop.quality_walk.pool);
    try testing.expectEqual(@as(?u64, 15), loop.quality_walk.period_seconds);
    try testing.expectEqual(@as(u64, 20), loop.rest_walk.queued);
    try testing.expectEqual(@as(u64, 1), loop.rest_walk.passes);
    try testing.expectEqual(@as(?u64, 20), loop.rest_walk.pool);
    try testing.expectEqual(@as(?u64, 10), loop.rest_walk.period_seconds);
    try testing.expectEqual(@as(u64, 25), body.queues.refresh_due);
    try testing.expectEqual(@as(u64, 0), body.queues.refresh_leased);
    try testing.expectEqual(@as(u64, 25), body.queues.refresh_pending);

    const corpus = body.corpus.?;
    try testing.expectEqual(@as(?u64, 50), corpus.live);
    try testing.expectEqual(@as(?u64, 50), corpus.total);
    try testing.expectEqual(@as(?u64, 50), corpus.with_handles);
    try testing.expectEqual(@as(?u64, 10), corpus.with_avatars);
    try testing.expectEqual(@as(?u64, 0), corpus.hidden);
    try testing.expectEqual(@as(u64, 1), corpus.scans);
}

test "the role's transport reads status, body and Retry-After over a real connection" {
    const allocator = testing.allocator;
    var threaded: std.Io.Threaded = .init(allocator, .{});
    defer threaded.deinit();
    const io = threaded.io();
    var listener = try (std.Io.net.IpAddress{ .ip4 = .loopback(0) }).listen(io, .{ .reuse_address = true });
    defer listener.deinit(io);
    var seen: [2][512]u8 = undefined;
    var seen_len: [2]usize = .{ 0, 0 };

    var server = try std.Thread.spawn(.{}, struct {
        fn run(l: *std.Io.net.Server, server_io: std.Io, heads: *[2][512]u8, lens: *[2]usize) void {
            const answers = [_][]const u8{
                "HTTP/1.1 429 Too Many Requests\r\nRetry-After: 7\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{\"profiles\":[]}",
            };
            for (answers, 0..) |answer, index| {
                var conn = l.accept(server_io) catch return;
                defer conn.close(server_io);
                var rbuf: [4096]u8 = undefined;
                var reader = conn.reader(server_io, &rbuf);
                var slices = [_][]u8{&heads[index]};
                lens[index] = reader.interface.readVec(&slices) catch 0;
                var wbuf: [256]u8 = undefined;
                var writer = conn.writer(server_io, &wbuf);
                writer.interface.writeAll(answer) catch {};
                writer.interface.flush() catch {};
            }
        }
    }.run, .{ &listener, io, &seen, &seen_len });
    defer server.join();

    var arena: std.heap.ArenaAllocator = .init(allocator);
    defer arena.deinit();
    var client = enrich.Http.init(io, allocator);
    defer client.deinit();
    client.client.keep_alive = false;
    const transport = client.transport();
    const url = try std.fmt.allocPrint(arena.allocator(), "http://127.0.0.1:{d}/xrpc/app.bsky.actor.getProfiles?actors=did%3Aplc%3Aabc", .{listener.socket.address.getPort()});

    const limited = transport.get(transport.context, arena.allocator(), url);
    try testing.expectEqual(@as(u16, 429), limited.status);
    try testing.expectEqual(@as(?u64, 7), limited.retry_after_seconds);
    const ok = transport.get(transport.context, arena.allocator(), url);
    try testing.expectEqual(@as(u16, 200), ok.status);
    try testing.expectEqualStrings("{\"profiles\":[]}", ok.body);
    try testing.expectEqual(@as(?u64, null), ok.retry_after_seconds);
    try testing.expect(std.mem.startsWith(u8, seen[0][0..seen_len[0]], "GET /xrpc/app.bsky.actor.getProfiles?actors=did%3Aplc%3Aabc HTTP/1.1\r\n"));
    var lowered: [512]u8 = undefined;
    try testing.expect(std.mem.indexOf(u8, std.ascii.lowerString(&lowered, seen[0][0..seen_len[0]]), "user-agent: typeahead.waow.tech/") != null);

    const refused = transport.get(transport.context, arena.allocator(), "http://127.0.0.1:1/xrpc/x");
    try testing.expectEqual(@as(u16, 0), refused.status);
}

test "a new account is fetched as soon as its handle resolves" {
    const rig = try Rig.create(quiet);
    defer rig.destroy();
    const subject = rig.did(1);
    _ = try rig.f.commit(&.{.{ .observe = .{ .did = subject, .enqueue_identity = true } }}, null);
    try testing.expectEqual(@as(usize, 0), try rig.queued());
    try testing.expectEqual(enrich.Step.idle, try rig.steps(1));

    try rig.known(subject, counts);
    _ = try rig.f.commit(&.{.{ .identity_resolved = .{ .did = subject, .observed_at = rig.f.now(), .handle = "new.test", .pds = "https://pds.test" } }}, null);
    try testing.expectEqual(@as(usize, 1), try rig.queued());
    try testing.expectEqual(enrich.Step.worked, try rig.steps(1));
    const actor = try rig.f.live(subject);
    try testing.expectEqual(@as(u64, 1234), actor.followers_count);
    try testing.expect(actor.quality_score > 0);
    try testing.expectEqual(@as(usize, 1), rig.world.requests.items.len);
}
