//! Identity resolution for DIDs the store's identity queue leases.
//!
//! Per the sync spec, `#identity` is a prod to invalidate cached identity data:
//! it "does not … reliably indicate what the current state of the identity is",
//! and `handle` is optional (the bsky relay omits it on every event sampled,
//! 21/21 on 2026-09-25). Identity is not self-certifying, so the consumer does
//! its own resolution.
//!
//! Signals never come here directly. `#identity`, a reactivating `#account`
//! and a newly discovered actor are queued durably by the store, and
//! `store_process/identity_work.zig` leases that queue. For each leased DID
//! this file:
//!   1. fetches the DID document from its authority (plc.directory, or the
//!      did:web host), never from an identity cache
//!   2. verifies the claimed handle bidirectionally (well-known / DNS TXT must
//!      point back at the DID)
//!   3. decides what to record, with the time the observation was made
//!
//! Until 2026-09-25 step 1 went through slingshot, a cache nobody had prodded:
//! it answered seconds after the PLC op and wrote the OLD handle for ~20–26% of
//! handle changes (sampled), which nothing downstream ever corrected.
//!
//! A handle that does not verify is retried after 60 s and again after 15 min
//! (a fresh domain can lag its DNS or well-known), then recorded as
//! `handle.invalid`, the atproto convention the appview also serves, rather
//! than keeping a previous handle that no longer resolves to this DID either.
//! An unreachable DID document backs off exponentially.

const std = @import("std");
const Io = std.Io;
const Allocator = std.mem.Allocator;
const mem = std.mem;
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
/// flight to well under the lease, so leased work is resolved before it
/// lapses rather than leased again.
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

/// Outcome counters, served on the store's /health.
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
/// Split out from `observe` so the rules are testable without HTTP:
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

/// Where resolution goes. The store role leaves every field null; the benchmark and
/// the tests point them at a loopback stand-in.
pub const Upstreams = struct {
    plc_url: ?[]const u8 = null,
    doh_endpoint: ?[]const u8 = null,
    /// Absolute path of a PEM file whose certificates are trusted in addition
    /// to the system's.
    extra_trust_path: ?[]const u8 = null,
};

pub const Resolver = struct {
    allocator: Allocator,
    io: Io,
    upstreams: Upstreams = .{},
    /// Resolve over kept connections. Off, every lookup opens and closes its
    /// own, which is what this did before 2026-11-11 and what the tests
    /// compare outcomes against.
    reuse_connections: bool = true,
    /// A session idle this long is closed and replaced rather than reused.
    /// A server that closes an idle connection is handled by the transport,
    /// which replays the request on a new one. A connection a middlebox
    /// dropped without a FIN would instead hold a lookup for the transport's
    /// 10 s head deadline and fail it; no connection older than this is
    /// offered the chance.
    session_idle_seconds: i64 = 20,
    sessions_mutex: Io.Mutex = .init,
    sessions: [max_sessions]*Session = undefined,
    sessions_idle: usize = 0,

    pub fn init(allocator: Allocator, io: Io) Resolver {
        return .{ .allocator = allocator, .io = io };
    }

    pub fn deinit(self: *Resolver) void {
        for (self.sessions[0..self.sessions_idle]) |session| session.destroy();
        self.sessions_idle = 0;
    }

    /// Resolve one job from its authority and decide what to report. Strings
    /// in the outcome are owned by `arena`.
    pub fn observe(self: *Resolver, arena: Allocator, job: Job) Observation {
        const did_slice = job.did.slice();

        // Taken before the fetch: the store keeps the newest observation, and
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
        if (self.reuse_connections) {
            const session = try self.acquireSession();
            defer self.releaseSession(session);
            return lookupWith(arena, did, &session.did, &session.handle);
        }

        var did_resolver = zat.DidResolver.initWithOptions(self.io, arena, .{ .keep_alive = false });
        defer did_resolver.deinit();
        var handle_resolver = zat.HandleResolver.init(self.io, arena);
        handle_resolver.transport.keep_alive = false;
        defer handle_resolver.deinit();
        try self.configure(&did_resolver, &handle_resolver);
        return lookupWith(arena, did, &did_resolver, &handle_resolver);
    }

    fn lookupWith(arena: Allocator, did: zat.Did, did_resolver: *zat.DidResolver, handle_resolver: *zat.HandleResolver) !Lookup {
        var doc = try did_resolver.resolve(did);
        defer doc.deinit();

        var out: Lookup = .{};
        if (doc.pdsEndpoint()) |pds| out.pds = try arena.dupe(u8, pds);
        const claimed = firstHandle(doc.handles) orelse return out;
        out.handle = try std.ascii.allocLowerString(arena, claimed.str());

        const resolved = handle_resolver.resolve(claimed) catch |err| {
            log.debug("verify {s} -> {s}: {s}", .{ out.handle.?, did.raw, @errorName(err) });
            return out;
        };
        defer handle_resolver.allocator.free(resolved);
        out.verified = mem.eql(u8, resolved, did.raw);
        return out;
    }

    fn configure(self: *Resolver, did_resolver: *zat.DidResolver, handle_resolver: *zat.HandleResolver) !void {
        if (self.upstreams.plc_url) |url| did_resolver.plc_url = url;
        if (self.upstreams.doh_endpoint) |url| {
            did_resolver.doh_endpoint = url;
            handle_resolver.doh_endpoint = url;
        }
        try self.trust(&did_resolver.transport);
        try self.trust(&handle_resolver.transport);
    }

    /// The clients one lookup uses, kept between lookups so the connections
    /// to the PLC directory and the DNS-over-HTTPS endpoint, and to a handle
    /// host that comes up again, are opened once instead of per lookup.
    ///
    /// A session serves one lookup at a time. The transport would allow
    /// concurrent fetches, but it opens TLS connections under one lock, so
    /// lookups sharing a client would queue behind each other's handshakes
    /// to slow hosts.
    const Session = struct {
        gpa: Allocator,
        did: zat.DidResolver,
        handle: zat.HandleResolver,
        released_at: Io.Timestamp = .{ .nanoseconds = 0 },

        fn destroy(self: *Session) void {
            self.did.deinit();
            self.handle.deinit();
            self.gpa.destroy(self);
        }
    };

    /// Sessions kept between lookups: one per identity worker. More lookups
    /// than this at once still each get a session; the extra ones are closed
    /// when their lookup ends.
    pub const max_sessions = 8;
    /// Idle connections a session keeps. The DID client talks to the PLC
    /// directory, plus a did:web host and the DNS endpoint for the rare
    /// did:web; the handle client to the DNS endpoint and to handle hosts,
    /// nearly all of which are never asked twice. When a pool is full the
    /// connection idle longest is closed, so the bound holds however many
    /// hosts go by: at most max_sessions * (4 + 8) = 96 idle sockets.
    pub const did_idle_connections = 4;
    pub const handle_idle_connections = 8;
    pub const max_idle_connections = max_sessions * (did_idle_connections + handle_idle_connections);

    fn acquireSession(self: *Resolver) !*Session {
        if (self.popSession()) |session| {
            if (session.released_at.untilNow(self.io, .awake).toSeconds() < self.session_idle_seconds) {
                dropClosedConnections(self.io, &session.did.transport);
                dropClosedConnections(self.io, &session.handle.transport);
                return session;
            }
            session.destroy();
        }
        const session = try self.allocator.create(Session);
        session.* = .{
            .gpa = self.allocator,
            .did = zat.DidResolver.initWithOptions(self.io, self.allocator, .{ .keep_alive = true }),
            .handle = zat.HandleResolver.init(self.io, self.allocator),
        };
        errdefer session.destroy();
        session.handle.transport.keep_alive = true;
        session.did.transport.http_client.connection_pool.free_size = did_idle_connections;
        session.handle.transport.http_client.connection_pool.free_size = handle_idle_connections;
        try self.configure(&session.did, &session.handle);
        return session;
    }

    /// Close the idle connections whose peer has closed them. Nothing is
    /// owed on an idle connection, so one that is readable holds a close (or
    /// bytes that make it unusable). The transport replays a request whose
    /// kept connection turns out reset or closed with a TLS close_notify,
    /// but a bare FIN on a TLS connection reaches it as
    /// TlsConnectionTruncated, which it reports as a failed request: a
    /// directory lookup would be retried a minute later and a verifiable
    /// handle reported unverified. A close that lands between this check and
    /// the request can still do that.
    fn dropClosedConnections(io: Io, transport: *HttpTransport) void {
        const pool = &transport.http_client.connection_pool;
        pool.mutex.lockUncancelable(io);
        defer pool.mutex.unlock(io);
        var next = pool.free.first;
        while (next) |node| {
            next = node.next;
            const connection: *std.http.Client.Connection = @alignCast(@fieldParentPtr("pool_node", node));
            var fds = [_]std.posix.pollfd{.{ .fd = connection.stream_reader.stream.socket.handle, .events = std.posix.POLL.IN, .revents = 0 }};
            const ready = std.posix.poll(&fds, 0) catch 0;
            if (ready == 0) continue;
            pool.free.remove(node);
            pool.free_len -= 1;
            connection.destroy(io);
        }
    }

    /// The session released last, so a quiet resolver keeps using one
    /// session's connections instead of spreading lookups over all of them.
    fn popSession(self: *Resolver) ?*Session {
        self.sessions_mutex.lockUncancelable(self.io);
        defer self.sessions_mutex.unlock(self.io);
        if (self.sessions_idle == 0) return null;
        self.sessions_idle -= 1;
        return self.sessions[self.sessions_idle];
    }

    fn releaseSession(self: *Resolver, session: *Session) void {
        session.released_at = Io.Timestamp.now(self.io, .awake);
        {
            self.sessions_mutex.lockUncancelable(self.io);
            defer self.sessions_mutex.unlock(self.io);
            if (self.sessions_idle < max_sessions) {
                self.sessions[self.sessions_idle] = session;
                self.sessions_idle += 1;
                return;
            }
        }
        session.destroy();
    }

    /// Load the system's trust anchors plus the extra ones, as the transport
    /// would load the system's alone on its first TLS connection.
    fn trust(self: *Resolver, transport: *HttpTransport) !void {
        const path = self.upstreams.extra_trust_path orelse return;
        const client = &transport.http_client;
        const now = Io.Clock.real.now(self.io);
        try client.ca_bundle.rescan(client.allocator, self.io, now);
        try client.ca_bundle.addCertsFromFilePathAbsolute(client.allocator, self.io, now, path);
        client.now = now;
    }
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

test "firstHandle skips non-handle alsoKnownAs entries" {
    const handles = [_][]const u8{ "https://example.com/profile", "alice.example.com" };
    try testing.expectEqualStrings("alice.example.com", firstHandle(&handles).?.str());
    const none = [_][]const u8{"https://example.com/profile"};
    try testing.expect(firstHandle(&none) == null);
}
