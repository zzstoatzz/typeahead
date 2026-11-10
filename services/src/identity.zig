//! Identity resolution for DIDs in the worker's durable `identity_queue`.
//!
//! Per the sync spec, `#identity` is a prod to invalidate cached identity data:
//! it "does not … reliably indicate what the current state of the identity is",
//! and `handle` is optional (the bsky relay omits it on every event sampled,
//! 21/21 on 2026-09-25). Identity is not self-certifying, so the consumer does
//! its own resolution.
//!
//! Signals never come here directly. `#identity`, a reactivating `#account`
//! and a newly discovered actor ride the cursor-protected ingest batch, and the
//! worker enqueues them in the same request that may advance the relay cursor,
//! so a restart cannot lose one (it was all in memory until 2026-10-31). This
//! file drains that queue:
//!   1. lease due work from /admin/identity/lease (leasing pushes its due time
//!      out, so work a crashed resolver held comes back on its own)
//!   2. fetch the DID document from its authority (plc.directory, or the
//!      did:web host) — never from an identity cache
//!   3. verify the claimed handle bidirectionally (well-known / DNS TXT must
//!      point back at the DID)
//!   4. report the observation, with the time it was made and the lease's
//!      generation, to /admin/identity/complete
//!
//! Until 2026-09-25 step 2 went through slingshot, a cache nobody had prodded:
//! it answered seconds after the PLC op and wrote the OLD handle for ~20–26% of
//! handle changes (sampled), which nothing downstream ever corrected.
//!
//! A handle that does not verify is retried after 60 s and again after 15 min
//! (a fresh domain can lag its DNS or well-known), then recorded as
//! `handle.invalid` — the atproto convention the appview also serves — rather
//! than keeping a previous handle that no longer resolves to this DID either.
//! An unreachable DID document backs off exponentially; the worker abandons it
//! after its attempt budget. The actor row itself is written by discovery, so
//! a DID that never resolves still exists for the handle-enrichment drain.

const std = @import("std");
const Io = std.Io;
const Allocator = std.mem.Allocator;
const mem = std.mem;
const json = std.json;
const zat = @import("zat");
const HttpTransport = zat.HttpTransport;
const ingest = @import("ingest.zig");
const poolio = @import("poolio");

const log = std.log.scoped(.identity);

/// Delay before each retry of an unverified handle.
pub const HANDLE_RETRY_DELAYS_SECS = [_]i64{ 60, 15 * 60 };
/// First delay after an unreachable DID document; doubles per attempt.
pub const UNAVAILABLE_BASE_SECS: i64 = 60;
pub const UNAVAILABLE_MAX_SECS: i64 = 6 * 60 * 60;

/// Jobs leased per request. With the pool's queue capacity this bounds work in
/// flight to well under the worker's lease, so leased work is resolved before
/// it lapses rather than leased again.
pub const LEASE_BATCH = 50;
const IDLE_POLL_SECS = 5;
const PARTIAL_POLL_SECS = 1;
const LEASE_FAILURE_BACKOFF_SECS = 10;

pub const INVALID_HANDLE = "handle.invalid";

/// Inline DID storage — no allocation, fits the longest realistic DID.
/// PLC DIDs are ~32 chars; web DIDs can be longer. 96 covers everything we'll see.
pub const DID_MAX = 96;

pub const Did = struct {
    buf: [DID_MAX]u8,
    len: u8,

    pub fn fromSlice(s: []const u8) ?Did {
        if (s.len == 0 or s.len > DID_MAX) return null;
        var d: Did = undefined;
        d.len = @intCast(s.len);
        @memcpy(d.buf[0..s.len], s);
        return d;
    }

    pub fn slice(self: *const Did) []const u8 {
        return self.buf[0..self.len];
    }
};

pub const Job = struct {
    did: Did,
    /// queue generation this lease covers; settles only that generation
    gen: u64,
    /// leases taken, including this one
    attempts: u32,
};

/// Outcome counters, served on the ingester's /health.
pub const Stats = struct {
    leased: std.atomic.Value(u64) = .init(0),
    lease_failed: std.atomic.Value(u64) = .init(0),
    verified: std.atomic.Value(u64) = .init(0),
    unverified: std.atomic.Value(u64) = .init(0),
    no_handle: std.atomic.Value(u64) = .init(0),
    did_failed: std.atomic.Value(u64) = .init(0),
    retried: std.atomic.Value(u64) = .init(0),
    post_failed: std.atomic.Value(u64) = .init(0),
};
pub var stats: Stats = .{};

/// What we learned about a DID, before deciding what to write.
pub const Lookup = struct {
    /// first alsoKnownAs entry with valid handle syntax, lowercased
    handle: ?[]const u8 = null,
    pds: ?[]const u8 = null,
    /// true iff `handle` resolved back to this DID
    verified: bool = false,
};

pub const Outcome = union(enum) {
    resolved: struct {
        handle: ?[]const u8 = null,
        handle_verified: bool = false,
        pds: ?[]const u8 = null,
    },
    retry: struct {
        pds: ?[]const u8 = null,
        delay_seconds: i64,
        @"error": []const u8,
    },
};

pub const Observation = struct { observed_at: i64, outcome: Outcome };

pub fn unavailableDelay(attempts: u32) i64 {
    const doublings = @min(attempts -| 1, 20);
    return @min(UNAVAILABLE_MAX_SECS, UNAVAILABLE_BASE_SECS << @intCast(doublings));
}

/// What to report for a DID, given its DID-doc lookup (null = the document
/// could not be fetched) and how many leases it has taken, including this one.
///
/// Split out from `process` so the rules are testable without HTTP:
///   - a verified handle is written, replacing whatever the row had
///   - an unverified handle is retried while stages remain, reporting only the
///     pds meanwhile; once they are spent it is recorded as handle.invalid
///   - an unreachable document is retried with exponential backoff
pub fn decide(lookup: ?Lookup, attempts: u32) Outcome {
    const l = lookup orelse return .{ .retry = .{ .delay_seconds = unavailableDelay(attempts), .@"error" = "did_unavailable" } };
    const handle = l.handle orelse return .{ .resolved = .{ .pds = l.pds } };
    if (l.verified) return .{ .resolved = .{ .handle = handle, .handle_verified = true, .pds = l.pds } };
    const spent = attempts -| 1;
    if (spent < HANDLE_RETRY_DELAYS_SECS.len) {
        return .{ .retry = .{ .pds = l.pds, .delay_seconds = HANDLE_RETRY_DELAYS_SECS[spent], .@"error" = "handle_unverified" } };
    }
    return .{ .resolved = .{ .handle = INVALID_HANDLE, .pds = l.pds } };
}

const ResultJson = struct {
    outcome: []const u8,
    did: []const u8,
    gen: u64,
    observed_at: i64,
    attempts: ?u32 = null,
    handle: ?[]const u8 = null,
    handle_verified: ?bool = null,
    pds: ?[]const u8 = null,
    delay_seconds: ?i64 = null,
    @"error": ?[]const u8 = null,
};

fn resultJson(job: *const Job, observed_at: i64, outcome: Outcome) ResultJson {
    return switch (outcome) {
        .resolved => |r| .{
            .outcome = "resolved",
            .did = job.did.slice(),
            .gen = job.gen,
            .observed_at = observed_at,
            .handle = r.handle,
            .handle_verified = r.handle_verified,
            .pds = r.pds,
        },
        .retry => |r| .{
            .outcome = "retry",
            .did = job.did.slice(),
            .gen = job.gen,
            .observed_at = observed_at,
            .attempts = job.attempts,
            .pds = r.pds,
            .delay_seconds = r.delay_seconds,
            .@"error" = r.@"error",
        },
    };
}

pub const Resolver = struct {
    allocator: Allocator,
    io: Io,
    worker_url: []const u8,
    worker_secret: []const u8,

    pub fn init(
        allocator: Allocator,
        io: Io,
        worker_url: []const u8,
        worker_secret: []const u8,
    ) Resolver {
        return .{
            .allocator = allocator,
            .io = io,
            .worker_url = worker_url,
            .worker_secret = worker_secret,
        };
    }

    pub fn deinit(_: *Resolver) void {}

    /// poolio process function — called per dequeued Job.
    pub fn process(self: *Resolver, io: Io, job: Job) void {
        _ = io;
        var arena = std.heap.ArenaAllocator.init(self.allocator);
        defer arena.deinit();
        const seen = self.observe(arena.allocator(), job);

        // Pool workers share Resolver as their context. std.http.Client is not
        // safe to use concurrently, so each worker invocation owns its client.
        var transport = HttpTransport.init(self.io, self.allocator);
        transport.keep_alive = false;
        defer transport.deinit();
        // A lost report is not lost work: the lease lapses and it is leased again.
        self.postResult(&transport, resultJson(&job, seen.observed_at, seen.outcome)) catch |err| {
            _ = stats.post_failed.fetchAdd(1, .monotonic);
            log.warn("post identity {s}: {s}", .{ job.did.slice(), @errorName(err) });
        };
    }

    /// Resolve one job from its authority and decide what to report. Strings
    /// in the outcome are owned by `arena`.
    pub fn observe(self: *Resolver, arena: Allocator, job: Job) Observation {
        const did_slice = job.did.slice();

        // Taken before the fetch: the worker keeps the newest observation, and
        // a document fetched after this instant can only be newer.
        const observed_at = ingest.timestamp();
        const lookup = self.fetchLookup(arena, did_slice) catch |err| blk: {
            log.warn("resolve {s}: {s}", .{ did_slice, @errorName(err) });
            _ = stats.did_failed.fetchAdd(1, .monotonic);
            break :blk null;
        };

        const outcome = decide(lookup, job.attempts);
        switch (outcome) {
            .retry => _ = stats.retried.fetchAdd(1, .monotonic),
            .resolved => |r| if (r.handle == null) {
                _ = stats.no_handle.fetchAdd(1, .monotonic);
            } else if (r.handle_verified) {
                _ = stats.verified.fetchAdd(1, .monotonic);
            } else {
                _ = stats.unverified.fetchAdd(1, .monotonic);
                log.info("handle unverified after retries {s}: {s}", .{ did_slice, lookup.?.handle.? });
            },
        }
        return .{ .observed_at = observed_at, .outcome = outcome };
    }

    /// Resolve the DID document from its authority and verify its handle.
    /// Strings in the result are owned by `arena`.
    fn fetchLookup(self: *Resolver, arena: Allocator, did_slice: []const u8) !Lookup {
        const did = zat.Did.parse(did_slice) orelse return error.InvalidDid;

        var did_resolver = zat.DidResolver.initWithOptions(self.io, arena, .{ .keep_alive = false });
        defer did_resolver.deinit();
        var doc = try did_resolver.resolve(did);
        defer doc.deinit();

        var out: Lookup = .{};
        if (doc.pdsEndpoint()) |pds| out.pds = try arena.dupe(u8, pds);
        const claimed = firstHandle(doc.handles) orelse return out;
        out.handle = try std.ascii.allocLowerString(arena, claimed.str());

        var handle_resolver = zat.HandleResolver.init(self.io, arena);
        handle_resolver.transport.keep_alive = false;
        defer handle_resolver.deinit();
        const resolved = handle_resolver.resolve(claimed) catch |err| {
            log.debug("verify {s} -> {s}: {s}", .{ out.handle.?, did_slice, @errorName(err) });
            return out;
        };
        out.verified = mem.eql(u8, resolved, did_slice);
        return out;
    }

    fn post(self: *Resolver, transport: *HttpTransport, path: []const u8, payload: []const u8) ![]u8 {
        var url_buf: [512]u8 = undefined;
        const url = try std.fmt.bufPrint(&url_buf, "{s}{s}", .{ self.worker_url, path });

        var auth_buf: [256]u8 = undefined;
        const auth = try std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.worker_secret});

        const result = try transport.fetch(ingest.workerPost(url, payload, auth));
        if (result.status != .ok) {
            log.warn("{s} HTTP {d}: {s}", .{ path, @backingInt(result.status), result.body });
            self.allocator.free(result.body);
            return error.HttpError;
        }
        return result.body;
    }

    fn postResult(self: *Resolver, transport: *HttpTransport, result: ResultJson) !void {
        var output: std.Io.Writer.Allocating = .init(self.allocator);
        defer output.deinit();
        var jw: json.Stringify = .{
            .writer = &output.writer,
            .options = .{ .emit_null_optional_fields = false },
        };
        try jw.write(.{ .results = &[_]ResultJson{result} });
        const body = try self.post(transport, "/admin/identity/complete", output.written());
        self.allocator.free(body);
    }

    const LeasedJson = struct { did: []const u8, gen: u64, attempts: u32 };
    const LeaseResponse = struct { jobs: []const LeasedJson };

    /// Lease due work into `out`; returns how many jobs were filled.
    fn lease(self: *Resolver, transport: *HttpTransport, out: []Job) !usize {
        var payload_buf: [64]u8 = undefined;
        const payload = try std.fmt.bufPrint(&payload_buf, "{{\"limit\":{d}}}", .{out.len});
        const body = try self.post(transport, "/admin/identity/lease", payload);
        defer self.allocator.free(body);

        const parsed = try json.parseFromSlice(LeaseResponse, self.allocator, body, .{ .ignore_unknown_fields = true });
        defer parsed.deinit();
        var n: usize = 0;
        for (parsed.value.jobs) |j| {
            if (n == out.len) break;
            const did = Did.fromSlice(j.did) orelse {
                log.warn("lease returned an unusable did ({d} bytes)", .{j.did.len});
                continue;
            };
            out[n] = .{ .did = did, .gen = j.gen, .attempts = j.attempts };
            n += 1;
        }
        return n;
    }

    /// The drain loop: lease due work and hand it to the resolver pool.
    /// `put` blocks while the pool is full, which is the backpressure that
    /// keeps leased work bounded.
    pub fn drain(self: *Resolver, pool: *Pool) void {
        var transport = HttpTransport.init(self.io, self.allocator);
        transport.keep_alive = true;
        defer transport.deinit();
        var queue: WorkerQueue = .{ .resolver = self, .transport = &transport };
        drainFrom(self.io, &queue, pool);
    }

    const WorkerQueue = struct {
        resolver: *Resolver,
        transport: *HttpTransport,

        pub fn lease(self: *WorkerQueue, out: []Job) !usize {
            return self.resolver.lease(self.transport, out);
        }
    };
};

/// Lease from `queue` (`lease(out: []Job) !usize`) into `pool` until
/// cancelled, pacing by how much each lease returned.
pub fn drainFrom(io: Io, queue: anytype, pool: anytype) void {
    var jobs: [LEASE_BATCH]Job = undefined;
    while (true) {
        const n = queue.lease(&jobs) catch |err| {
            _ = stats.lease_failed.fetchAdd(1, .monotonic);
            log.warn("identity lease failed: {s}", .{@errorName(err)});
            io.sleep(Io.Duration.fromSeconds(LEASE_FAILURE_BACKOFF_SECS), .awake) catch return;
            continue;
        };
        _ = stats.leased.fetchAdd(n, .monotonic);
        for (jobs[0..n]) |job| pool.put(io, job) catch return;
        const wait: i64 = if (n == 0) IDLE_POLL_SECS else if (n < LEASE_BATCH) PARTIAL_POLL_SECS else 0;
        if (wait > 0) io.sleep(Io.Duration.fromSeconds(wait), .awake) catch return;
    }
}

/// The handle a DID document claims: its first alsoKnownAs entry that is
/// valid handle syntax. zat strips `at://` but keeps other URI schemes, so
/// those are skipped here by failing handle syntax.
fn firstHandle(handles: []const []const u8) ?zat.Handle {
    for (handles) |h| {
        if (zat.Handle.parse(h)) |parsed| return parsed;
    }
    return null;
}

/// Convenience alias — the poolio instantiation used throughout the ingester.
pub const Pool = poolio.Pool(Job, Resolver);

const testing = std.testing;

test "decide: an unreachable DID document is retried with exponential backoff" {
    // Regression: dropping here meant a DID whose identity could not be
    // resolved never became a row (birdlover.selfhosted.social, 2026-08-18).
    // Rows now come from discovery; the retry keeps resolving it.
    var attempts: u32 = 1;
    var previous: i64 = 0;
    while (attempts <= 12) : (attempts += 1) {
        const o = decide(null, attempts);
        try testing.expectEqualStrings("did_unavailable", o.retry.@"error");
        try testing.expect(o.retry.pds == null);
        try testing.expect(o.retry.delay_seconds >= previous);
        try testing.expect(o.retry.delay_seconds <= UNAVAILABLE_MAX_SECS);
        previous = o.retry.delay_seconds;
    }
    try testing.expectEqual(UNAVAILABLE_BASE_SECS, decide(null, 1).retry.delay_seconds);
    try testing.expectEqual(UNAVAILABLE_MAX_SECS, decide(null, 12).retry.delay_seconds);
    try testing.expectEqual(UNAVAILABLE_MAX_SECS, unavailableDelay(std.math.maxInt(u32)));
}

test "decide: a verified handle change is written immediately" {
    // Regression (2026-09-25): moltrust.bsky.social -> moltrust.ch was written
    // back as the OLD handle because resolution went through a cache. The
    // write must carry the handle the authority now claims and that verifies.
    const o = decide(.{
        .handle = "moltrust.ch",
        .pds = "https://morel.us-east.host.bsky.network",
        .verified = true,
    }, 1);
    try testing.expectEqualStrings("moltrust.ch", o.resolved.handle.?);
    try testing.expect(o.resolved.handle_verified);
    try testing.expectEqualStrings("https://morel.us-east.host.bsky.network", o.resolved.pds.?);
}

test "decide: an unverified handle is retried per stage, then recorded as handle.invalid" {
    // Regression (2026-09-26): keeping the previous handle on a failed
    // verification served a name that no longer resolves to the DID either;
    // the appview serves handle.invalid. streetnewzvlogger.sprk.so failed its
    // first check and verified minutes later, hence more than one stage.
    const l: Lookup = .{ .handle = "new.example.com", .pds = "https://selfhosted.social", .verified = false };
    for (HANDLE_RETRY_DELAYS_SECS, 1..) |delay, attempts| {
        const o = decide(l, @intCast(attempts));
        try testing.expectEqual(delay, o.retry.delay_seconds);
        try testing.expectEqualStrings("https://selfhosted.social", o.retry.pds.?);
        try testing.expectEqualStrings("handle_unverified", o.retry.@"error");
    }
    const last = decide(l, HANDLE_RETRY_DELAYS_SECS.len + 1);
    try testing.expectEqualStrings(INVALID_HANDLE, last.resolved.handle.?);
    try testing.expect(!last.resolved.handle_verified);
    try testing.expectEqualStrings("https://selfhosted.social", last.resolved.pds.?);
}

test "decide: a document without a handle reports its pds and settles" {
    const o = decide(.{ .pds = "https://pds.example" }, 1);
    try testing.expect(o.resolved.handle == null);
    try testing.expectEqualStrings("https://pds.example", o.resolved.pds.?);
}

test "result json carries what the worker's schema requires for each outcome" {
    const job: Job = .{ .did = Did.fromSlice("did:plc:abc").?, .gen = 3, .attempts = 2 };
    var buf: std.Io.Writer.Allocating = .init(testing.allocator);
    defer buf.deinit();
    var jw: json.Stringify = .{ .writer = &buf.writer, .options = .{ .emit_null_optional_fields = false } };
    try jw.write(resultJson(&job, 1700000000, decide(null, 2)));
    try testing.expectEqualStrings(
        \\{"outcome":"retry","did":"did:plc:abc","gen":3,"observed_at":1700000000,"attempts":2,"delay_seconds":120,"error":"did_unavailable"}
    , buf.written());

    var resolved: std.Io.Writer.Allocating = .init(testing.allocator);
    defer resolved.deinit();
    var rw: json.Stringify = .{ .writer = &resolved.writer, .options = .{ .emit_null_optional_fields = false } };
    try rw.write(resultJson(&job, 1700000000, decide(.{ .handle = "a.example", .pds = "https://p", .verified = true }, 2)));
    try testing.expectEqualStrings(
        \\{"outcome":"resolved","did":"did:plc:abc","gen":3,"observed_at":1700000000,"handle":"a.example","handle_verified":true,"pds":"https://p"}
    , resolved.written());
}

test "firstHandle skips non-handle alsoKnownAs entries" {
    const handles = [_][]const u8{ "https://example.com/profile", "alice.example.com" };
    try testing.expectEqualStrings("alice.example.com", firstHandle(&handles).?.str());
    const none = [_][]const u8{"https://example.com/profile"};
    try testing.expect(firstHandle(&none) == null);
}

fn integrationEnv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return mem.span(p);
    return null;
}

// Runs the real lease → resolve → complete path against a worker (local
// `wrangler dev` on local sqld); skipped unless TYPEAHEAD_IDENTITY_E2E_URL is
// set. The caller enqueues the DIDs first; this resolves each from its
// authority and the caller checks what the worker recorded.
test "integration: leased identity work is resolved and reported" {
    const url = integrationEnv("TYPEAHEAD_IDENTITY_E2E_URL") orelse return error.SkipZigTest;
    const secret = integrationEnv("TYPEAHEAD_IDENTITY_E2E_SECRET") orelse return error.SkipZigTest;
    const io = std.Options.debug_io;
    var resolver = Resolver.init(testing.allocator, io, url, secret);
    var transport = HttpTransport.init(io, testing.allocator);
    defer transport.deinit();

    var jobs: [LEASE_BATCH]Job = undefined;
    const n = try resolver.lease(&transport, &jobs);
    try testing.expect(n > 0);
    const before = stats.post_failed.load(.monotonic);
    for (jobs[0..n]) |job| resolver.process(io, job);
    try testing.expectEqual(before, stats.post_failed.load(.monotonic));
    try testing.expectEqual(@as(usize, 0), try resolver.lease(&transport, &jobs));
}
