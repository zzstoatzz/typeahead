//! Identity resolution against the store's own queue: lease due DIDs,
//! resolve each from its authority with the worker role's resolver, and
//! commit what was observed. A lost report is not lost work: the lease
//! lapses and the DID is leased again.

const std = @import("std");
const poolio = @import("poolio");
const identity = @import("../identity.zig");
const store_mod = @import("../store/root.zig");
const events = @import("events.zig");
const owner_mod = @import("owner.zig");
const Io = std.Io;
const Owner = owner_mod.Owner;
const Submission = owner_mod.Submission;

const log = std.log.scoped(.identity);

pub const lease_seconds = 300;

pub const Work = struct {
    gpa: std.mem.Allocator,
    io: Io,
    owner: *Owner,
    resolver: *identity.Resolver,

    /// poolio process function: resolve one leased job and report it.
    pub fn process(self: *Work, _: Io, job: identity.Job) void {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const seen = self.resolver.observe(arena.allocator(), job);
        self.report(job, seen) catch |err| {
            _ = identity.stats.post_failed.fetchAdd(1, .monotonic);
            log.warn("identity result for {s} not committed: {s}", .{ job.did.slice(), @errorName(err) });
        };
    }

    /// Commit one observation. Returns once it is durable.
    pub fn report(self: *Work, job: identity.Job, seen: identity.Observation) !void {
        const submission = try Submission.create(self.gpa);
        defer submission.destroy();
        const list = try submission.arena.allocator().alloc(store_mod.Change, 1);
        list[0] = events.completion(&job, seen);
        submission.changes = list;
        try self.owner.call(submission);
    }

    /// Lease due work into `out`; returns how many jobs were filled.
    pub fn lease(self: *Work, out: []identity.Job) !usize {
        const submission = try Submission.create(self.gpa);
        defer submission.destroy();
        const list = try submission.arena.allocator().alloc(store_mod.Change, 1);
        list[0] = .{ .identity_lease = .{ .limit = out.len, .lease_seconds = lease_seconds } };
        submission.changes = list;
        try self.owner.call(submission);
        var n: usize = 0;
        for (submission.jobs) |leased| {
            const did = identity.Did.fromSlice(leased.did) orelse {
                log.warn("leased an unusable did ({d} bytes)", .{leased.did.len});
                continue;
            };
            out[n] = .{ .did = did, .gen = leased.generation, .attempts = std.math.cast(u32, leased.attempts) orelse std.math.maxInt(u32) };
            n += 1;
        }
        return n;
    }

    pub fn drain(self: *Work, pool: *Pool) void {
        identity.drainFrom(self.io, self, pool);
    }
};

pub const Pool = poolio.Pool(identity.Job, Work);
