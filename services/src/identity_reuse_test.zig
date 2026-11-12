//! The resolver over kept connections, against the loopback stand-in in
//! testing/identity_stub.py (needs python3 and openssl on PATH).

const std = @import("std");
const poolio = @import("poolio");
const identity = @import("identity.zig");
const stub_mod = @import("testing/identity_stub.zig");
const Io = std.Io;
const testing = std.testing;

const pds = "https://morel.us-east.host.bsky.network";

const Case = struct {
    kind: *const [4]u8,
    attempts: u32 = 1,
    /// `handle verified pds` for a resolved outcome, `retry error delay pds`
    /// for a retry, with `<id>` standing for the DID's identifier.
    expected: []const u8,
};

const cases = [_]Case{
    .{ .kind = "okay", .expected = "<id>.bsky.social true " ++ pds },
    .{ .kind = "cust", .expected = "<id>.handles.test true " ++ pds },
    .{ .kind = "dtxt", .expected = "<id>.bsky.social true " ++ pds },
    .{ .kind = "noha", .expected = "- false " ++ pds },
    .{ .kind = "wron", .expected = "retry handle_unverified 60 " ++ pds },
    .{ .kind = "miss", .expected = "retry handle_unverified 60 " ++ pds },
    .{ .kind = "miss", .attempts = 2, .expected = "retry handle_unverified 900 " ++ pds },
    .{ .kind = "miss", .attempts = 3, .expected = "handle.invalid false " ++ pds },
    .{ .kind = "redi", .expected = "retry handle_unverified 60 " ++ pds },
    .{ .kind = "clos", .expected = "retry handle_unverified 60 " ++ pds },
    .{ .kind = "pmis", .expected = "retry did_unavailable 60 -" },
    .{ .kind = "pmis", .attempts = 3, .expected = "retry did_unavailable 240 -" },
    .{ .kind = "pred", .expected = "retry did_unavailable 60 -" },
    .{ .kind = "pclo", .expected = "retry did_unavailable 60 -" },
};
const slow_case: Case = .{ .kind = "slow", .expected = "retry handle_unverified 60 " ++ pds };

fn describe(buf: []u8, job: identity.Job, outcome: identity.Outcome) ![]const u8 {
    var text_buf: [256]u8 = undefined;
    const text = switch (outcome) {
        .resolved => |r| try std.fmt.bufPrint(&text_buf, "{s} {} {s}", .{ r.handle orelse "-", r.handle_verified, r.pds orelse "-" }),
        .retry => |r| try std.fmt.bufPrint(&text_buf, "retry {s} {d} {s}", .{ r.@"error", r.delay_seconds, r.pds orelse "-" }),
    };
    const size = std.mem.replacementSize(u8, text, job.did.slice()[8..], "<id>");
    _ = std.mem.replace(u8, text, job.did.slice()[8..], "<id>", buf);
    return buf[0..size];
}

fn expectCase(resolver: *identity.Resolver, case: Case, n: u64) !void {
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    const job: identity.Job = .{ .did = stub_mod.did(case.kind, n), .gen = 1, .attempts = case.attempts };
    var buf: [256]u8 = undefined;
    try testing.expectEqualStrings(case.expected, try describe(&buf, job, resolver.observe(arena.allocator(), job).outcome));
}

const Fixture = struct {
    stub: stub_mod.Stub,
    resolver: identity.Resolver,

    fn start(self: *Fixture, reuse: bool) !void {
        // The failing cases are logged as warnings, as they are in service.
        testing.log_level = .err;
        try self.stub.start(testing.io, testing.allocator);
        self.resolver = identity.Resolver.init(testing.allocator, self.stub.io(), "", "");
        self.resolver.upstreams = self.stub.upstreams();
        self.resolver.reuse_connections = reuse;
    }

    fn deinit(self: *Fixture) void {
        self.resolver.deinit();
        self.stub.deinit();
        testing.log_level = .warn;
    }

    fn handshakes(self: *Fixture) !u64 {
        return (try self.stub.counts()).handshakes;
    }
};

test "lookups over kept connections report what lookups over fresh ones do" {
    var fixture: Fixture = undefined;
    try fixture.start(true);
    defer fixture.deinit();
    const kept = &fixture.resolver;
    var fresh = identity.Resolver.init(testing.allocator, fixture.stub.io(), "", "");
    defer fresh.deinit();
    fresh.upstreams = fixture.stub.upstreams();
    fresh.reuse_connections = false;

    const before = try fixture.handshakes();
    for (cases, 0..) |case, n| try expectCase(&fresh, case, n);
    const after_fresh = try fixture.handshakes();
    // The second and third passes run on the session the first one left,
    // behind every kind of failure.
    for (0..3) |pass| for (cases, 0..) |case, n| try expectCase(kept, case, 100 * (pass + 1) + n);
    const after_kept = try fixture.handshakes();
    try testing.expect(after_kept - after_fresh < after_fresh - before);

    var slow_fresh = try testing.io.concurrent(expectCase, .{ &fresh, slow_case, 1 });
    defer slow_fresh.cancel(testing.io) catch {};
    try expectCase(kept, slow_case, 2);
    try slow_fresh.await(testing.io);
    try expectCase(kept, cases[0], 3);
}

test "a kept connection the server closed while idle is replaced without failing the lookup" {
    // The risk in keeping connections: a server's keep-alive timeout closes
    // one, the next request on it fails, and a handle that is fine is
    // reported unverified.
    var fixture: Fixture = undefined;
    try fixture.start(true);
    defer fixture.deinit();
    const verified = cases[0];
    const by_dns = cases[2];

    try expectCase(&fixture.resolver, verified, 1);
    const opened = try fixture.handshakes();
    try expectCase(&fixture.resolver, verified, 2);
    // Only the new handle host: the directory and DNS connections were kept.
    try testing.expectEqual(opened + 1, try fixture.handshakes());

    for ([_]stub_mod.Stub.CloseHow{ .notify, .abrupt }, 3..) |how, n| {
        try fixture.stub.closeIdle(how);
        const closed = try fixture.handshakes();
        try expectCase(&fixture.resolver, verified, n);
        try testing.expectEqual(closed + 3, try fixture.handshakes());
        try fixture.stub.closeIdle(how);
        try expectCase(&fixture.resolver, by_dns, n);
    }
}

test "a session idle past the limit is closed and replaced" {
    var fixture: Fixture = undefined;
    try fixture.start(true);
    defer fixture.deinit();
    fixture.resolver.session_idle_seconds = 0;

    _ = try fixture.handshakes();
    const before_fds = try openDescriptors();
    try expectCase(&fixture.resolver, cases[0], 1);
    const opened = try fixture.handshakes();
    try expectCase(&fixture.resolver, cases[0], 2);
    try testing.expectEqual(opened + 3, try fixture.handshakes());
    try testing.expectEqual(@as(usize, 1), fixture.resolver.sessions_idle);
    // The replaced session's three connections are gone; the live one's remain.
    try testing.expectEqual(before_fds + 3, try openDescriptors());
}

fn openDescriptors() !usize {
    var dir = try Io.Dir.openDirAbsolute(testing.io, "/dev/fd", .{ .iterate = true });
    defer dir.close(testing.io);
    var n: usize = 0;
    var it = dir.iterate();
    while (try it.next(testing.io)) |_| n += 1;
    return n;
}

const Sweep = struct {
    resolver: *identity.Resolver,
    failed: std.atomic.Value(u32) = .init(0),

    fn process(self: *Sweep, _: Io, job: identity.Job) Io.Cancelable!void {
        expectCase(self.resolver, cases[0], job.gen) catch {
            _ = self.failed.fetchAdd(1, .monotonic);
        };
    }
};

test "idle connections stay within the bound over hundreds of handle hosts and close with the resolver" {
    var fixture: Fixture = undefined;
    try fixture.start(true);
    var stub_live = true;
    defer if (stub_live) fixture.stub.deinit();
    var resolver_live = true;
    defer if (resolver_live) fixture.resolver.deinit();

    const before_handshakes = try fixture.handshakes();
    const before_fds = try openDescriptors();

    var sweep: Sweep = .{ .resolver = &fixture.resolver };
    const Pool = poolio.Pool(identity.Job, Sweep);
    var pool = try Pool.init(testing.allocator, .{ .queue_capacity = 64, .workers = 16, .ctx = &sweep, .process = Sweep.process });
    defer pool.deinit(testing.io);
    try pool.start(testing.io);
    const hosts = 400;
    for (0..hosts) |n| try pool.put(testing.io, .{ .did = stub_mod.did("okay", n), .gen = n, .attempts = 1 });
    try pool.shutdown(testing.io);
    try testing.expectEqual(@as(u32, 0), sweep.failed.load(.monotonic));

    // Sixteen workers against eight kept sessions: the extra sessions were
    // closed as their lookups ended.
    try testing.expectEqual(@as(usize, identity.Resolver.max_sessions), fixture.resolver.sessions_idle);
    const held = try openDescriptors() - before_fds;
    try testing.expect(held > 0);
    try testing.expect(held <= identity.Resolver.max_idle_connections);
    // Every handle host is new, so it costs a handshake; the directory and
    // DNS cost two per session, not two per lookup.
    const handshakes = try fixture.handshakes() - before_handshakes;
    try testing.expect(handshakes >= hosts);
    try testing.expect(handshakes < hosts + hosts / 2);

    fixture.resolver.deinit();
    resolver_live = false;
    try testing.expectEqual(before_fds, try openDescriptors());
    fixture.stub.deinit();
    stub_live = false;
    testing.log_level = .warn;
}
