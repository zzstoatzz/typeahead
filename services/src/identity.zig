//! Identity resolution for DIDs received from the relay's firehose `#identity` events.
//!
//! Per the sync spec, `#identity` is a prod to invalidate cached identity data:
//! it "does not … reliably indicate what the current state of the identity is",
//! and `handle` is optional (the bsky relay omits it on every event sampled,
//! 21/21 on 2026-09-25). Identity is not self-certifying, so the consumer does
//! its own resolution. For every `#identity` DID this resolver:
//!   1. fetches the DID document from its authority (plc.directory, or the
//!      did:web host) — never from an identity cache
//!   2. verifies the claimed handle bidirectionally (well-known / DNS TXT must
//!      point back at the DID)
//!   3. POSTs {did, handle, pds} to the worker's /admin/ingest
//!
//! Until 2026-09-25 step 1 went through slingshot, a cache nobody had prodded:
//! it answered seconds after the PLC op and wrote the OLD handle for ~20–26% of
//! handle changes (sampled), which nothing downstream ever corrected.
//!
//! A handle that does not verify gets one delayed retry (a fresh custom domain
//! can lag its DNS), then the DID and pds are written without it. A resolution
//! failure never drops the DID: we still POST the bare DID so the row exists,
//! because the handle-enrichment drain retries `WHERE handle = ''` and can only
//! see rows. Dropping it made birdlover.selfhosted.social invisible (2026-08-18).

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

/// How long an unverified handle waits before its one retry.
pub const RETRY_DELAY_SECS: i64 = 60;

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
    retried: bool = false,
};

/// Outcome counters, served on the ingester's /health.
pub const Stats = struct {
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

pub const Decision = struct {
    event: ingest.ActorEvent,
    retry: bool,
};

/// What to record for a DID, given its DID-doc lookup (null = the DID
/// document could not be fetched) and whether this is already the retry.
///
/// Split out from `process` so the rules are testable without HTTP:
///   - a verified handle is written, replacing whatever the row had
///   - an unverified handle is never written; first time round we retry
///   - the DID (and any pds) is always recorded
pub fn decide(did: []const u8, lookup: ?Lookup, retried: bool) Decision {
    const l = lookup orelse return .{ .event = .{ .did = did }, .retry = false };
    if (l.handle != null and l.verified) {
        return .{ .event = .{ .did = did, .handle = l.handle, .pds = l.pds }, .retry = false };
    }
    return .{
        .event = .{ .did = did, .pds = l.pds },
        .retry = l.handle != null and !retried,
    };
}

pub const Resolver = struct {
    allocator: Allocator,
    io: Io,
    worker_url: []const u8,
    worker_secret: []const u8,
    retry_pool: ?*RetryPool = null,

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
        const did_slice = job.did.slice();

        var arena = std.heap.ArenaAllocator.init(self.allocator);
        defer arena.deinit();

        const lookup = self.fetchLookup(arena.allocator(), did_slice) catch |err| blk: {
            log.warn("resolve {s}: {s}", .{ did_slice, @errorName(err) });
            _ = stats.did_failed.fetchAdd(1, .monotonic);
            break :blk null;
        };

        const d = decide(did_slice, lookup, job.retried);
        if (lookup) |l| {
            if (l.handle == null) {
                _ = stats.no_handle.fetchAdd(1, .monotonic);
            } else if (l.verified) {
                _ = stats.verified.fetchAdd(1, .monotonic);
            } else if (!d.retry) {
                _ = stats.unverified.fetchAdd(1, .monotonic);
                log.info("handle unverified after retry {s}: {s}", .{ did_slice, l.handle.? });
            }
        }
        if (d.retry) {
            if (self.retry_pool) |rp| {
                const queued = rp.offer(self.io, .{
                    .job = .{ .did = job.did, .retried = true },
                    .due = ingest.timestamp() + RETRY_DELAY_SECS,
                });
                if (queued) _ = stats.retried.fetchAdd(1, .monotonic);
            }
        }

        // Pool workers share Resolver as their context. std.http.Client is not
        // safe to use concurrently, so each worker invocation owns its client.
        var transport = HttpTransport.init(self.io, self.allocator);
        transport.keep_alive = false;
        defer transport.deinit();
        self.postIdentity(&transport, d.event) catch |err| {
            _ = stats.post_failed.fetchAdd(1, .monotonic);
            log.warn("post identity {s}: {s}", .{ did_slice, @errorName(err) });
        };
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

    fn postIdentity(self: *Resolver, transport: *HttpTransport, event: ingest.ActorEvent) !void {
        var output: std.Io.Writer.Allocating = .init(self.allocator);
        defer output.deinit();

        var jw: json.Stringify = .{
            .writer = &output.writer,
            .options = .{ .emit_null_optional_fields = false },
        };

        // single-event batch, no cursor (we don't want to clobber the relay cursor)
        try jw.write(.{ .events = &[_]ingest.ActorEvent{event} });

        var url_buf: [512]u8 = undefined;
        const url = try std.fmt.bufPrint(&url_buf, "{s}/admin/ingest", .{self.worker_url});

        var auth_buf: [256]u8 = undefined;
        const auth = try std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.worker_secret});

        const result = try transport.fetch(.{
            .url = url,
            .method = .POST,
            .payload = output.written(),
            .authorization = auth,
        });
        defer self.allocator.free(result.body);

        if (result.status != .ok) {
            log.warn("post identity HTTP {d}: {s}", .{ @intFromEnum(result.status), result.body });
            return error.HttpError;
        }
    }
};

/// The handle a DID document claims: its first alsoKnownAs entry that is
/// valid handle syntax. zat strips `at://` but keeps other URI schemes, so
/// those are skipped here by failing handle syntax.
fn firstHandle(handles: []const []const u8) ?zat.Handle {
    for (handles) |h| {
        if (zat.Handle.parse(h)) |parsed| return parsed;
    }
    return null;
}

pub const Delayed = struct {
    job: Job,
    due: i64,
};

/// Holds unverified handles for RETRY_DELAY_SECS, then re-offers them to the
/// resolver pool. Runs with one worker: every item gets the same delay, so the
/// FIFO is already in due order and sleeping on the head is exact.
pub const Retrier = struct {
    pool: *Pool,

    pub fn process(self: *Retrier, io: Io, item: Delayed) void {
        const wait = item.due - ingest.timestamp();
        if (wait > 0) io.sleep(Io.Duration.fromSeconds(wait), .awake) catch return;
        _ = self.pool.offer(io, item.job);
    }
};

/// Convenience aliases — the poolio instantiations used throughout the ingester.
pub const Pool = poolio.Pool(Job, Resolver);
pub const RetryPool = poolio.Pool(Delayed, Retrier);

const testing = std.testing;

test "decide: an unreachable DID document still records the DID" {
    // Regression: dropping here meant a DID whose identity could not be
    // resolved never became a row — and the handle drain, which retries
    // `WHERE handle = ''`, only ever sees rows. birdlover.selfhosted.social sat
    // in the relay with a healthy repo and was absent from the corpus entirely.
    const d = decide("did:plc:iu2klrrbxplyyqgltk7sg5l3", null, false);
    try testing.expectEqualStrings("did:plc:iu2klrrbxplyyqgltk7sg5l3", d.event.did);
    try testing.expect(d.event.handle == null);
    try testing.expect(d.event.pds == null);
    try testing.expect(!d.retry);
}

test "decide: a verified handle change is written immediately" {
    // Regression (2026-09-25): moltrust.bsky.social -> moltrust.ch was written
    // back as the OLD handle because resolution went through a cache. The
    // write must carry the handle the authority now claims and that verifies.
    const d = decide("did:plc:bjrgfiemnynipuvv4h6md4qi", .{
        .handle = "moltrust.ch",
        .pds = "https://morel.us-east.host.bsky.network",
        .verified = true,
    }, false);
    try testing.expectEqualStrings("moltrust.ch", d.event.handle.?);
    try testing.expectEqualStrings("https://morel.us-east.host.bsky.network", d.event.pds.?);
    try testing.expect(!d.retry);
}

test "decide: an unverified handle is not written and is retried once" {
    const first = decide("did:plc:abc", .{
        .handle = "new.example.com",
        .pds = "https://selfhosted.social",
        .verified = false,
    }, false);
    try testing.expect(first.event.handle == null);
    try testing.expectEqualStrings("https://selfhosted.social", first.event.pds.?);
    try testing.expect(first.retry);

    const second = decide("did:plc:abc", .{
        .handle = "new.example.com",
        .pds = "https://selfhosted.social",
        .verified = false,
    }, true);
    try testing.expect(second.event.handle == null);
    try testing.expectEqualStrings("https://selfhosted.social", second.event.pds.?);
    try testing.expect(!second.retry);
}

test "decide: a document without a handle records DID and pds, no retry" {
    const d = decide("did:plc:abc", .{ .pds = "https://pds.example" }, false);
    try testing.expect(d.event.handle == null);
    try testing.expectEqualStrings("https://pds.example", d.event.pds.?);
    try testing.expect(!d.retry);
}

test "firstHandle skips non-handle alsoKnownAs entries" {
    const handles = [_][]const u8{ "https://example.com/profile", "alice.example.com" };
    try testing.expectEqualStrings("alice.example.com", firstHandle(&handles).?.str());
    const none = [_][]const u8{"https://example.com/profile"};
    try testing.expect(firstHandle(&none) == null);
}
