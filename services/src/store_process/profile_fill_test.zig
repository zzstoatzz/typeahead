//! The profile fill against a stand-in for the Worker's
//! `/slot/control/profile-fill`, under a clock the test moves, and the role
//! running it against a loopback server.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const control = @import("control.zig");
const control_test = @import("control_test.zig");
const http = @import("http.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const profile_fill = @import("profile_fill.zig");
const harness = @import("testing.zig");
const Fixture = harness.Fixture;
const zat = @import("zat");
const Io = std.Io;
const testing = std.testing;

const origin = control_test.origin;
const secret = control_test.secret;

const Row = struct { did: []const u8, description: []const u8 = "", banner: []const u8 = "" };

/// Turso's `actors` table in DID order, and the endpoint that pages it as
/// `handleSlotProfileFill` in `src/handlers/slot-control.ts` does.
const Worker = struct {
    arena: std.mem.Allocator,
    table: []const Row,
    /// Answers for the next requests, before the table is consulted.
    scripted: std.ArrayList(control.Response) = .empty,
    seen: std.ArrayList([]const u8) = .empty,

    fn script(self: *Worker, response: control.Response) void {
        self.scripted.append(self.arena, response) catch @panic("out of memory");
    }

    fn parameter(url: []const u8, name: []const u8) []const u8 {
        const query = url[(std.mem.indexOfScalar(u8, url, '?') orelse return "") + 1 ..];
        var parts = std.mem.splitScalar(u8, query, '&');
        while (parts.next()) |part| {
            if (std.mem.startsWith(u8, part, name) and part.len > name.len and part[name.len] == '=') return part[name.len + 1 ..];
        }
        return "";
    }

    fn get(context: *anyopaque, _: std.mem.Allocator, url: []const u8, authorization: []const u8) control.Response {
        const self: *Worker = @ptrCast(@alignCast(context));
        self.seen.append(self.arena, self.arena.dupe(u8, url) catch @panic("out of memory")) catch @panic("out of memory");
        if (self.scripted.items.len > 0) return self.scripted.orderedRemove(0);
        if (!std.mem.eql(u8, authorization, control_test.bearer)) return .{ .status = 401, .body = "{\"error\":\"unauthorized\"}" };
        if (!std.mem.startsWith(u8, url, origin ++ profile_fill.path ++ "?")) return .{ .status = 404 };
        const after = parameter(url, "after");
        const limit = std.fmt.parseInt(usize, parameter(url, "limit"), 10) catch return .{ .status = 400 };

        var out: Io.Writer.Allocating = .init(self.arena);
        const w = &out.writer;
        w.writeAll("{\"rows\":[") catch @panic("out of memory");
        var read: usize = 0;
        var sent: usize = 0;
        var last: []const u8 = "";
        for (self.table) |row| {
            if (std.mem.order(u8, row.did, after) != .gt) continue;
            if (read == limit) break;
            read += 1;
            last = row.did;
            if (row.description.len == 0 and row.banner.len == 0) continue;
            w.print("{s}{{\"did\":{f},\"description\":{f},\"banner\":{f}}}", .{
                if (sent > 0) "," else "", std.json.fmt(row.did, .{}), std.json.fmt(row.description, .{}), std.json.fmt(row.banner, .{}),
            }) catch @panic("out of memory");
            sent += 1;
        }
        if (read == limit) {
            w.print("],\"next\":{f}}}", .{std.json.fmt(last, .{})}) catch @panic("out of memory");
        } else {
            w.writeAll("],\"next\":null}") catch @panic("out of memory");
        }
        return .{ .status = 200, .body = out.written() };
    }
};

const Clock = struct {
    f: *Fixture,
    ms: i64,
    sleeps: std.ArrayList(u64) = .empty,
    /// Called before each sleep, for a test that changes the world between
    /// steps of `run`.
    before_sleep: ?*const fn (rig: *Rig) void = null,
    rig: *Rig = undefined,

    fn nowMs(context: *anyopaque) i64 {
        const self: *Clock = @ptrCast(@alignCast(context));
        return self.ms;
    }

    fn sleepMs(context: *anyopaque, ms: u64) bool {
        const self: *Clock = @ptrCast(@alignCast(context));
        if (self.before_sleep) |call| call(self.rig);
        self.sleeps.append(self.f.arena.allocator(), ms) catch @panic("out of memory");
        self.ms += @intCast(ms);
        self.f.clock.at.store(@intCast(@divFloor(self.ms, 1000)), .monotonic);
        return true;
    }
};

// Ten accounts at the Worker. The store holds a, b, c, e and g; d is gone.
const table = [_]Row{
    .{ .did = "did:plc:a", .description = "a from turso", .banner = "bafka" },
    .{ .did = "did:plc:b", .description = "b from turso", .banner = "bafkb" },
    .{ .did = "did:plc:c" },
    .{ .did = "did:plc:d", .description = "d from turso" },
    .{ .did = "did:plc:e", .banner = "bafke" },
    .{ .did = "did:plc:f", .description = "not in the store" },
    .{ .did = "did:plc:g", .description = "g \"quoted\"\nsecond line" },
    .{ .did = "did:plc:h" },
    .{ .did = "did:plc:i" },
    .{ .did = "did:plc:j" },
};

const Rig = struct {
    f: *Fixture,
    worker: *Worker,
    clock: *Clock,
    loop: profile_fill.Filler,

    fn create() !*Rig {
        const f = try Fixture.create(.{ .idle_wait_ms = 1, .health_seconds = 0 });
        errdefer f.destroy();
        const loaded: store_mod.change.Enrichment = .{ .did = "", .source = .bsky, .followers_count = 12, .quality_score = 9.5, .created_at = "2024-01-01T00:00:00.000Z", .checked = false };
        for ([_][]const u8{ "did:plc:a", "did:plc:b", "did:plc:c", "did:plc:d", "did:plc:e", "did:plc:g" }) |did| {
            var enrichment = loaded;
            enrichment.did = did;
            if (std.mem.eql(u8, did, "did:plc:b")) enrichment.description = "b's own";
            _ = try f.commit(&.{ .{ .profile = .{ .did = did, .handle = "loaded.test" } }, .{ .enrichment = enrichment } }, null);
        }
        _ = try f.commit(&.{
            .{ .gone = .{ .did = "did:plc:d", .status = .deactivated } },
            .{ .refresh_enqueue = .{ .did = "did:plc:a", .due = harness.t0 + 5000 } },
            .{ .refresh_enqueue = .{ .did = "did:plc:b", .due = harness.t0 + 5000 } },
        }, null);
        return attach(f, null);
    }

    /// A loop over `f`'s store, as a process started on it would have.
    fn attach(f: *Fixture, worker: ?*Worker) !*Rig {
        const arena = f.arena.allocator();
        const self = try arena.create(Rig);
        const clock = try arena.create(Clock);
        clock.* = .{ .f = f, .ms = @as(i64, @intCast(f.now())) * 1000, .rig = self };
        const served = worker orelse blk: {
            const new = try arena.create(Worker);
            new.* = .{ .arena = arena, .table = &table };
            break :blk new;
        };
        self.* = .{ .f = f, .worker = served, .clock = clock, .loop = .{
            .gpa = testing.allocator,
            .owner = f.owner,
            .transport = .{ .context = served, .get = Worker.get },
            .timer = .{ .context = clock, .now_ms = Clock.nowMs, .sleep_ms = Clock.sleepMs },
            .options = .{ .url = origin, .secret = secret, .page_size = 3 },
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

    const Running = struct {
        running: harness.Running,
        owner: *owner_mod.Owner,

        fn stop(self: *Running) void {
            self.running.stop();
            self.owner.stopped = false;
        }
    };

    fn owning(self: *Rig) !Running {
        return .{ .running = try harness.Running.start(self.f), .owner = self.f.owner };
    }

    /// One step with the owner on its own thread.
    fn step(self: *Rig) !profile_fill.Step {
        var running = try self.owning();
        defer running.stop();
        return self.loop.step();
    }

    fn state(self: *Rig) !store_mod.record.ProfileFillState {
        const store = try self.f.owner.acquire();
        defer self.f.owner.release();
        return store_mod.read.profileFillState(store.db(), self.f.arena.allocator());
    }

    fn head(self: *Rig) !u64 {
        return (try self.f.cursors()).next_change;
    }

    fn health(self: *Rig) !http.ProfileFillBody {
        _ = self.f.owner.turn(0);
        var signals: intake_mod.Signals = .{};
        var ignored: std.atomic.Value(u64) = .init(0);
        const context: http.Context = .{ .owner = self.f.owner, .reader = null, .intake = &signals, .labels_ignored = &ignored, .read_secret = null, .profile_fill = &self.loop };
        const arena = self.f.arena.allocator();
        const Body = struct { profile_fill: http.ProfileFillBody };
        return (try std.json.parseFromSliceLeaky(Body, arena, (try http.respond(&context, arena, .GET, "/health", null, "")).body, .{ .ignore_unknown_fields = true })).profile_fill;
    }
};

test "pages are pulled from the stored position and fill only what the store lacks" {
    var rig = try Rig.create();
    defer rig.destroy();
    const before = .{ try rig.f.live("did:plc:a"), try rig.f.live("did:plc:b"), try rig.f.live("did:plc:c") };
    const log_head = try rig.head();
    rig.clock.ms += 100_000;
    rig.f.advance(100);

    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    try testing.expectEqualStrings(origin ++ "/slot/control/profile-fill?after=&limit=3", rig.worker.seen.items[0]);

    const a = try rig.f.live("did:plc:a");
    try testing.expectEqualStrings("a from turso", a.description);
    try testing.expectEqualStrings("bafka", a.banner);
    const b = try rig.f.live("did:plc:b");
    try testing.expectEqualStrings("b's own", b.description);
    try testing.expectEqualStrings("bafkb", b.banner);
    // Only the two fields and the content time moved: no check, no refresh
    // queued or settled, and the counts and the score are the loaded ones.
    inline for (.{ a, b }, .{ before[0], before[1] }) |after, was| {
        var expected = was;
        expected.description = after.description;
        expected.banner = after.banner;
        expected.updated_at = harness.t0 + 100;
        try testing.expect(expected.eql(after));
        try testing.expectEqual(@as(u64, 0), after.profile_checked_at);
        try testing.expectEqual(harness.t0 + 5000, after.refresh_due);
        try testing.expectEqual(@as(u64, 12), after.followers_count);
        try testing.expectEqual(@as(f32, 9.5), after.quality_score);
    }
    try testing.expect(before[2].eql(try rig.f.live("did:plc:c")));
    // Each filled actor is in the change log, for the bridge to send on.
    try testing.expectEqual(log_head + 2, try rig.head());
    var state = try rig.state();
    try testing.expectEqualStrings("did:plc:c", state.position);
    try testing.expectEqual(@as(u64, 1), state.pages);
    try testing.expectEqual(@as(u64, 2), state.filled);

    // d is gone and f was never seen: neither is written or created.
    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    try testing.expectEqualStrings(origin ++ "/slot/control/profile-fill?after=did:plc:c&limit=3", rig.worker.seen.items[1]);
    try testing.expectEqualStrings("", (try rig.f.actor("did:plc:d")).?.description);
    try testing.expect(try rig.f.actor("did:plc:f") == null);
    try testing.expectEqualStrings("bafke", (try rig.f.live("did:plc:e")).banner);
    try testing.expectEqual(log_head + 3, try rig.head());

    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    try testing.expectEqualStrings("g \"quoted\"\nsecond line", (try rig.f.live("did:plc:g")).description);
    state = try rig.state();
    try testing.expectEqualStrings("did:plc:i", state.position);
    try testing.expectEqual(@as(u64, 0), state.finished_at);

    // The last page is short, so the Worker says its table has ended.
    try testing.expectEqual(profile_fill.Step.finished, try rig.step());
    state = try rig.state();
    try testing.expectEqual(@as(u64, 4), state.pages);
    try testing.expectEqual(@as(u64, 6), state.rows);
    try testing.expectEqual(@as(u64, 4), state.filled);
    try testing.expectEqual(harness.t0 + 100, state.started_at);
    try testing.expectEqual(harness.t0 + 100, state.finished_at);

    // Finished: nothing more is asked of the Worker, by this process or the next.
    try testing.expectEqual(profile_fill.Step.finished, try rig.step());
    try testing.expectEqual(@as(usize, 4), rig.worker.seen.items.len);
    rig = try rig.restart();
    try testing.expectEqual(profile_fill.Step.finished, try rig.step());
    rig.loop.run();
    try testing.expectEqual(@as(usize, 4), rig.worker.seen.items.len);
    try testing.expectEqual(@as(usize, 0), rig.clock.sleeps.items.len);
    try testing.expectEqual(log_head + 4, try rig.head());
}

test "a restart resumes after the last page that was committed" {
    var rig = try Rig.create();
    defer rig.destroy();
    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    rig = try rig.restart();
    try testing.expectEqualStrings("did:plc:f", rig.loop.position.items);
    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    try testing.expectEqualStrings(origin ++ "/slot/control/profile-fill?after=did:plc:f&limit=3", rig.worker.seen.items[2]);
    try testing.expectEqual(profile_fill.Step.finished, try rig.step());
    try testing.expectEqual(@as(usize, 4), rig.worker.seen.items.len);
    const state = try rig.state();
    try testing.expectEqual(@as(u64, 4), state.pages);
    try testing.expectEqual(@as(u64, 4), state.filled);
}

test "no page is asked for while the index bridge is behind" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.loop.options.lag_limit = 3;
    const log_head = try rig.head();
    _ = try rig.f.commit(&.{}, .{ .bridge = log_head - 4 });
    try testing.expectEqual(profile_fill.Step.deferred, try rig.step());
    try testing.expectEqual(profile_fill.Step.deferred, try rig.step());
    try testing.expectEqual(@as(usize, 0), rig.worker.seen.items.len);
    var report = rig.loop.report();
    try testing.expectEqual(@as(u64, 2), report.deferred);
    try testing.expectEqual(@as(u64, 4), report.bridge_lag);
    try testing.expectEqual(@as(u64, 0), report.failures);
    try testing.expectEqual(@as(u64, 1000), rig.loop.rest(.deferred));

    // At the limit is not above it. The page's own two changes then put
    // the bridge behind again until it has sent them.
    _ = try rig.f.commit(&.{}, .{ .bridge = log_head - 3 });
    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    try testing.expectEqual(profile_fill.Step.deferred, try rig.step());
    _ = try rig.f.commit(&.{}, .{ .bridge = log_head + 2 });
    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    report = rig.loop.report();
    try testing.expectEqual(@as(u64, 3), report.deferred);
    try testing.expectEqual(@as(u64, 2), report.pages);
}

test "a page that cannot be had is counted, changes nothing and is asked for again" {
    const rig = try Rig.create();
    defer rig.destroy();
    const log_head = try rig.head();
    const commits = rig.f.owner.counters.commits.load(.monotonic);
    const refusals = [_]control.Response{
        .{},
        .{ .status = 401, .body = "{\"error\":\"unauthorized\"}" },
        .{ .status = 410, .body = "{\"error\":\"Turso's actor tables are retired\"}" },
        .{ .status = 500 },
        .{ .status = 200, .body = "<html>" },
        .{ .status = 200, .body = "{\"rows\":[{\"did\":\"did:plc:b\",\"description\":\"x\",\"banner\":\"\"},{\"did\":\"did:plc:a\",\"description\":\"y\",\"banner\":\"\"}],\"next\":\"did:plc:c\"}" },
        .{ .status = 200, .body = "{\"rows\":[{\"did\":\"did:plc:a\",\"description\":\"x\",\"banner\":\"\"}]}" },
    };
    for (refusals) |response| {
        rig.worker.script(response);
        try testing.expectEqual(profile_fill.Step.failed, try rig.step());
    }
    const report = rig.loop.report();
    try testing.expectEqual(@as(u64, 7), report.failures);
    try testing.expectEqual(@as(u64, 1), report.unreachable_worker);
    try testing.expectEqual(@as(u64, 1), report.unauthorized);
    try testing.expectEqual(@as(u64, 3), report.malformed);
    try testing.expectEqual(profile_fill.Failure.malformed, report.last_failure);
    try testing.expectEqual(@as(u64, 0), report.pages);
    try testing.expectEqual(log_head, try rig.head());
    try testing.expectEqual(commits, rig.f.owner.counters.commits.load(.monotonic));
    try testing.expectEqualStrings("", (try rig.state()).position);
    try testing.expectEqualStrings("", (try rig.f.live("did:plc:a")).description);
    for (0..200) |_| {
        const ms = rig.loop.rest(.failed);
        try testing.expect(ms >= 30_000 and ms <= 36_000);
    }

    // A wrong secret is the Worker's 401, counted the same way.
    const good = rig.loop.authorization;
    rig.loop.authorization = "Bearer wrong";
    try testing.expectEqual(profile_fill.Step.failed, try rig.step());
    try testing.expectEqual(@as(u64, 2), rig.loop.report().unauthorized);
    rig.loop.authorization = good;

    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    try testing.expectEqualStrings("a from turso", (try rig.f.live("did:plc:a")).description);
    for (rig.worker.seen.items) |url| try testing.expectEqualStrings(origin ++ "/slot/control/profile-fill?after=&limit=3", url);
}

test "a store that will not commit is counted as a failure of the loop" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.f.owner.stop();
    try testing.expectError(error.OwnerStopped, rig.loop.step());
    const report = rig.loop.report();
    try testing.expectEqual(@as(u64, 1), report.failures);
    try testing.expectEqual(profile_fill.Failure.store, report.last_failure);
}

fn bridgeCatchesUp(rig: *Rig) void {
    const store = rig.f.owner.acquire() catch return;
    const next = store.next_change;
    rig.f.owner.release();
    const submission = owner_mod.Submission.create(testing.allocator) catch return;
    defer submission.destroy();
    submission.cursor = .{ .bridge = next };
    rig.f.owner.call(submission) catch {};
}

test "the loop takes one page a step, rests longer after a failure, waits for the bridge and ends when the pass is finished" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.loop.options.step_seconds = 2;
    rig.loop.options.lag_limit = 0;
    _ = try rig.f.commit(&.{}, .{ .bridge = try rig.head() });
    rig.worker.script(.{ .status = 503 });
    // The bridge sends on what a page changed only at every other rest, so
    // two of the pages wait a step for it.
    const Lazy = struct {
        var rests: usize = 0;
        fn call(r: *Rig) void {
            rests += 1;
            if (rests % 2 == 0) bridgeCatchesUp(r);
        }
    };
    Lazy.rests = 0;
    rig.clock.before_sleep = Lazy.call;
    {
        var running = try rig.owning();
        defer running.stop();
        rig.loop.run();
    }
    const report = rig.loop.report();
    try testing.expect(report.finished);
    try testing.expectEqual(@as(u64, 4), report.pages);
    try testing.expectEqual(@as(u64, 1), report.failures);
    try testing.expectEqual(@as(u16, 503), report.last_failure_status);
    try testing.expectEqual(@as(u64, 2), report.deferred);
    try testing.expectEqual(@as(usize, 5), rig.worker.seen.items.len);
    // A rest after everything but the page that finished the pass.
    try testing.expectEqual(@as(usize, 6), rig.clock.sleeps.items.len);
    try testing.expect(rig.clock.sleeps.items[0] >= 30_000 and rig.clock.sleeps.items[0] <= 36_000);
    for (rig.clock.sleeps.items[1..]) |ms| try testing.expectEqual(@as(u64, 2000), ms);
    try testing.expect((try rig.state()).finished_at != 0);
}

test "/health reports where the pass stands and what the loop met" {
    const rig = try Rig.create();
    defer rig.destroy();
    rig.clock.ms += 50_000;
    rig.f.advance(50);
    try testing.expectEqual(profile_fill.Step.applied, try rig.step());
    rig.worker.script(.{ .status = 401 });
    try testing.expectEqual(profile_fill.Step.failed, try rig.step());

    var body = try rig.health();
    try testing.expectEqualStrings("on", body.state);
    try testing.expect(!body.finished and body.problem == null);
    try testing.expectEqual(harness.t0 + 50, body.started_at);
    try testing.expectEqual(@as(u64, 0), body.finished_at);
    try testing.expectEqual(@as(u64, 1), body.pages);
    try testing.expectEqual(@as(u64, 2), body.rows);
    try testing.expectEqual(@as(u64, 2), body.filled);
    try testing.expectEqualStrings("did:plc:c", body.position_did);
    try testing.expectEqual(@as(?usize, 3), body.page_size);
    try testing.expectEqual(@as(?u64, 1), body.step_seconds);
    try testing.expectEqual(@as(?u64, 50_000), body.lag_limit);
    try testing.expectEqual(@as(?u64, 1), body.process_pages);
    try testing.expectEqual(@as(?u64, harness.t0 + 50), body.last_page_at);
    try testing.expectEqual(@as(?u64, 0), body.deferred);
    try testing.expectEqual(@as(u64, 1), body.failures.?.total);
    try testing.expectEqual(@as(u64, 1), body.failures.?.unauthorized);
    try testing.expectEqualStrings("unauthorized", body.failures.?.last);
    try testing.expectEqual(@as(u16, 401), body.failures.?.last_status);

    while (try rig.step() != .finished) {}
    body = try rig.health();
    try testing.expect(body.finished);
    try testing.expectEqual(harness.t0 + 50, body.finished_at);
    try testing.expectEqual(@as(u64, 4), body.pages);

    // A process that does not run the pass still says where it stands, and
    // one that was asked to and cannot says why.
    var signals: intake_mod.Signals = .{};
    var ignored: std.atomic.Value(u64) = .init(0);
    var context: http.Context = .{ .owner = rig.f.owner, .reader = null, .intake = &signals, .labels_ignored = &ignored, .read_secret = null };
    const arena = rig.f.arena.allocator();
    const Body = struct { profile_fill: http.ProfileFillBody };
    const off = (try std.json.parseFromSliceLeaky(Body, arena, (try http.respond(&context, arena, .GET, "/health", null, "")).body, .{ .ignore_unknown_fields = true })).profile_fill;
    try testing.expectEqualStrings("off", off.state);
    try testing.expect(off.finished and off.failures == null and off.page_size == null);
    try testing.expectEqual(@as(u64, 4), off.filled);
    context.profile_fill_problem = "PROFILE_FILL_PAGE is not a number from 1 to 1000";
    const broken = (try std.json.parseFromSliceLeaky(Body, arena, (try http.respond(&context, arena, .GET, "/health", null, "")).body, .{ .ignore_unknown_fields = true })).profile_fill;
    try testing.expectEqualStrings("misconfigured", broken.state);
    try testing.expectEqualStrings("PROFILE_FILL_PAGE is not a number from 1 to 1000", broken.problem.?);
}

test "the role runs the pass to its end over HTTP and never asks again after a restart" {
    const io = testing.io;
    var tmp = testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [128]u8 = undefined;
    const path = try std.fmt.bufPrint(&path_buf, ".zig-cache/tmp/{s}/store", .{tmp.sub_path});

    var stub_address: Io.net.IpAddress = .{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = 0 } };
    var stub: control_test.Stub = .{ .io = io, .listener = try stub_address.listen(io, .{ .reuse_address = true }), .big = "" };
    try stub.start();
    defer stub.deinit();
    var url_buf: [64]u8 = undefined;
    const url = stub.url(&url_buf, "");

    var asked: [2]usize = undefined;
    for (&asked) |*count| {
        var address: Io.net.IpAddress = .{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = 0 } };
        var listener = try address.listen(io, .{ .reuse_address = true });
        defer listener.deinit(io);
        var stop: std.atomic.Value(bool) = .init(false);
        var running: control_test.RunningRole = .{
            .listener = &listener,
            .config = .{
                .store_dir = path,
                .offline = true,
                .corpus_scan_seconds = 0,
                .control = .{ .on = .{ .url = url, .secret = secret, .poll_seconds = 5 } },
                .profile_fill = .{ .on = .{ .url = url, .secret = secret, .step_seconds = 1 } },
            },
            .options = .{ .startup_seconds = 1, .corpus_delay_seconds = 3600, .stop = &stop },
        };
        const thread = try std.Thread.spawn(.{}, control_test.RunningRole.run, .{&running});

        var client = zat.HttpTransport.init(io, testing.allocator);
        defer client.deinit();
        var health_buf: [64]u8 = undefined;
        const health_url = try std.fmt.bufPrint(&health_buf, "http://127.0.0.1:{d}/health", .{listener.socket.address.getPort()});
        var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
        defer arena_state.deinit();
        const arena = arena_state.allocator();
        const Body = struct { profile_fill: http.ProfileFillBody };
        var reported: ?http.ProfileFillBody = null;
        // Two minutes at most, for a machine busy with other work; it ends
        // as soon as the role reports the pass finished.
        for (0..6000) |_| {
            var result = client.fetch(.{ .url = health_url, .deadline_ns = 2 * std.time.ns_per_s }) catch {
                try io.sleep(Io.Duration.fromMilliseconds(20), .awake);
                continue;
            };
            defer result.deinit(testing.allocator);
            const body = try std.json.parseFromSliceLeaky(Body, arena, try arena.dupe(u8, result.body), .{ .ignore_unknown_fields = true });
            reported = body.profile_fill;
            if (body.profile_fill.finished) break;
            try io.sleep(Io.Duration.fromMilliseconds(20), .awake);
        }

        stop.store(true, .release);
        const wake = try listener.socket.address.connect(io, .{ .mode = .stream });
        wake.close(io);
        thread.join();
        try running.result;

        const seen = reported.?;
        try testing.expectEqualStrings("on", seen.state);
        try testing.expect(seen.finished);
        try testing.expectEqual(@as(u64, 0), seen.failures.?.total);
        count.* = 0;
        for (stub.targets.items, stub.authorizations.items) |target, authorization| {
            if (!std.mem.startsWith(u8, target, profile_fill.path)) continue;
            count.* += 1;
            try testing.expectEqualStrings(control_test.bearer, authorization);
        }
    }
    // Two pages in the first life of the store, none in the second.
    try testing.expectEqual([2]usize{ 2, 2 }, asked);
    try testing.expectEqualStrings("/slot/control/profile-fill?after=&limit=500", for (stub.targets.items) |target| {
        if (std.mem.startsWith(u8, target, profile_fill.path)) break target;
    } else "");

    const reopened = try owner_mod.Owner.open(testing.allocator, io, Io.Dir.cwd(), path, .{});
    defer reopened.close();
    const stored = reopened.health().profile_fill;
    try testing.expect(stored.finished_at != 0);
    try testing.expectEqual(@as(u64, 2), stored.pages);
    try testing.expectEqual(@as(u64, 1), stored.rows);
    try testing.expectEqualStrings("did:plc:cccccccccccccccccccccccc", stored.position());
}
