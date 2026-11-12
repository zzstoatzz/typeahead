//! CPU cost of identity resolution against a loopback stand-in.
//!
//!   zig build bench-identity -Doptimize=ReleaseFast -- [resolutions] [distinct] [workers] [reuse|fresh] [kind]
//!
//! Runs `resolutions` jobs through `identity.Resolver.observe` on a poolio pool
//! of `workers`, as the store role does, and prints this process's user+system
//! CPU per 1,000 resolutions and the TLS handshakes the stand-in completed.
//! `distinct` is how many different accounts the jobs cover: equal to
//! `resolutions`, every handle host is new; smaller, hosts repeat with a skew
//! toward the low indices. `fresh` opens new connections for every lookup, as
//! the resolver did before it kept sessions. `kind` gives every account one
//! of the stand-in's behaviours, such as `noha` for a document without a
//! handle; without it one account in seven has a custom-domain handle and the
//! rest a bsky.social one. The stand-in is a separate process, so its CPU is
//! not counted.
//!
//! `other` above zero means lookups failed and the numbers are not a
//! measurement. On macOS that is usually `AddressUnavailable`: a `fresh` run
//! of 2,000 uses 8,000 ephemeral ports that stay in TIME_WAIT for 30 s, so
//! leave that long between such runs.

const std = @import("std");
const poolio = @import("poolio");
const identity = @import("identity.zig");
const stub_mod = @import("testing/identity_stub.zig");
const Io = std.Io;

const Tally = struct {
    arena_gpa: std.mem.Allocator,
    resolver: *identity.Resolver,
    verified: std.atomic.Value(u64) = .init(0),
    other: std.atomic.Value(u64) = .init(0),

    fn process(self: *Tally, _: Io, job: identity.Job) Io.Cancelable!void {
        var arena: std.heap.ArenaAllocator = .init(self.arena_gpa);
        defer arena.deinit();
        const seen = self.resolver.observe(arena.allocator(), job);
        const ok = seen.outcome == .resolved and seen.outcome.resolved.handle_verified;
        _ = (if (ok) &self.verified else &self.other).fetchAdd(1, .monotonic);
    }
};

const Pool = poolio.Pool(identity.Job, Tally);

fn cpuSeconds() f64 {
    const usage = std.posix.getrusage(std.posix.rusage.SELF);
    const user = @as(f64, @floatFromInt(usage.utime.sec)) + @as(f64, @floatFromInt(usage.utime.usec)) / 1e6;
    const system = @as(f64, @floatFromInt(usage.stime.sec)) + @as(f64, @floatFromInt(usage.stime.usec)) / 1e6;
    return user + system;
}

pub fn main(init: std.process.Init) !void {
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    const resolutions: u64 = if (args.len > 1) try std.fmt.parseInt(u64, args[1], 10) else 2000;
    const distinct: u64 = if (args.len > 2) try std.fmt.parseInt(u64, args[2], 10) else resolutions;
    const workers: usize = if (args.len > 3) try std.fmt.parseInt(usize, args[3], 10) else 8;
    const reuse = args.len <= 4 or !std.mem.eql(u8, args[4], "fresh");
    const only_kind: ?*const [4]u8 = if (args.len > 5 and args[5].len == 4) args[5][0..4] else null;
    const gpa = std.heap.smp_allocator;

    var stub: stub_mod.Stub = undefined;
    try stub.start(init.io, gpa);
    defer stub.deinit();

    var resolver = identity.Resolver.init(gpa, stub.io(), "", "");
    defer resolver.deinit();
    resolver.upstreams = stub.upstreams();
    resolver.reuse_connections = reuse;
    var tally: Tally = .{ .arena_gpa = gpa, .resolver = &resolver };

    var pool = try Pool.init(gpa, .{ .queue_capacity = 64, .workers = workers, .ctx = &tally, .process = Tally.process });
    defer pool.deinit(init.io);
    try pool.start(init.io);

    var prng: std.Random.DefaultPrng = .init(20261111);
    const random = prng.random();
    const before = try stub.counts();
    const cpu_before = cpuSeconds();
    const started = Io.Clock.awake.now(init.io);
    for (0..resolutions) |i| {
        // Squaring a uniform draw sends half the jobs to the lowest quarter
        // of the accounts.
        const account = if (distinct >= resolutions) i else blk: {
            const u = random.float(f64);
            break :blk @as(u64, @intFromFloat(u * u * @as(f64, @floatFromInt(distinct))));
        };
        const kind = only_kind orelse if (account % 7 == 0) "cust" else "okay";
        try pool.put(init.io, .{ .did = stub_mod.did(kind, account), .gen = 1, .attempts = 1 });
    }
    try pool.shutdown(init.io);
    const wall = @as(f64, @floatFromInt(started.durationTo(Io.Clock.awake.now(init.io)).toNanoseconds())) / 1e9;
    const cpu = cpuSeconds() - cpu_before;
    const after = try stub.counts();

    const per_thousand = 1000.0 / @as(f64, @floatFromInt(resolutions));
    var out_buf: [512]u8 = undefined;
    var out = Io.File.stdout().writer(init.io, &out_buf);
    try out.interface.print(
        "connections={s} resolutions={d} distinct={d} workers={d} verified={d} other={d}\ncpu_s_per_1000={d:.3} wall_s={d:.2}\nhandshakes={d} per_resolution={d:.2} requests={d}\n",
        .{
            if (reuse) "reuse" else "fresh", resolutions, distinct,                             workers,                                                                                              tally.verified.load(.monotonic),      tally.other.load(.monotonic),
            cpu * per_thousand,              wall,        after.handshakes - before.handshakes, @as(f64, @floatFromInt(after.handshakes - before.handshakes)) / @as(f64, @floatFromInt(resolutions)), after.requests - before.requests - 1,
        },
    );
    try out.interface.flush();
}
