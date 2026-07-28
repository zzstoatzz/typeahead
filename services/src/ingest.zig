//! Relay firehose event handler: buffers actor events, flushes batches to worker.
//!
//! Consumes com.atproto.sync.subscribeRepos directly rather than jetstream.
//! jetstream is a lossy projection of the relay: pds.zat.dev repos were absent
//! from a 7-day jetstream replay (0 events, 6/6 repos) while the relay was
//! rev-current with that PDS and re-emitted its commits live (verified
//! 2026-07-28). The loss is silent and partial, so it cannot be spot-checked.

const std = @import("std");
const io = std.Options.debug_io;
const mem = std.mem;
const json = std.json;
const Allocator = mem.Allocator;
const zat = @import("zat");
const HttpTransport = zat.HttpTransport;
const BloomFilter = @import("bloom.zig").BloomFilter;
const identity = @import("identity.zig");
const logfire = @import("logfire");

const log = std.log.scoped(.ingester);

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return mem.span(p);
    return null;
}

fn timestamp() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
}

// per-flush event cap. the flush POST is SYNCHRONOUS — it blocks the jetstream
// read loop for one worker round-trip (~1-2s), so catch-up throughput is
// MAX_BATCH / flush_time. at 300 the consumer capped at ~0.8x of peak firehose
// even on a fast host and the gap ran to ~3h (2026-06-15); 1500 lifts the
// ceiling well above realtime so the backlog drains. the worker accepts
// 10k/POST as one Turso transaction; catch-up batches are mostly bare-DID
// INSERT OR IGNORE (light locks). PROPER fix is decoupling the read loop from
// the flusher so POSTs don't stall reads — this is the stopgap.
// 1500 was tuned for jetstream's server-FILTERED stream. The relay firehose
// delivers ~4x the actor events, and each POST now fans into several sequential
// Turso transactions worker-side (250 statements each, to stay inside Turso's
// 5s transaction window). At 1500 the whole request outran Cloudflare's origin
// timeout, 524'd, and the ingester retried the same oversized batch every 5s —
// a self-sustaining wedge that kept Turso's single writer busy even after the
// job that triggered it was killed (2026-07-28: writes down ~25min, cursor
// frozen, watchdog restarts that could not clear it).
//
// 400 keeps one POST comfortably inside the timeout. Throughput is fine because
// flushes are far more frequent than the old 5s cadence.
pub const MAX_BATCH: usize = 400;
const BLOOM_BITS: usize = 10_000_000; // ~1.2MB fixed
const BLOOM_HASHES: usize = 7;

// liveness watchdog. a wedged process looks healthy from the outside while
// ingesting nothing — process-wide TLS rot + a black-holed websocket silently
// dropped 13 days of firehose (2026-05-29 → 06-12). exiting instead lets fly
// restart us; resume-from-cursor makes that lossless within jetstream's
// ~25h replay window.
const WATCHDOG_STALL_SECS: i64 = 10 * 60;
const WATCHDOG_MAX_CONSECUTIVE_FAILURES: u32 = 60; // ≥5min at the 5s retry cadence
const HEARTBEAT_INTERVAL_SECS: i64 = 60;

pub var last_event_at: std.atomic.Value(i64) = .init(0);
pub var last_flush_ok_at: std.atomic.Value(i64) = .init(0);

pub fn watchdogLoop() void {
    const boot = timestamp();
    last_event_at.store(boot, .monotonic);
    last_flush_ok_at.store(boot, .monotonic);
    while (true) {
        io.sleep(std.Io.Duration.fromSeconds(60), .awake) catch {};
        const now = timestamp();
        const event_age = now - last_event_at.load(.monotonic);
        const flush_age = now - last_flush_ok_at.load(.monotonic);
        if (event_age >= WATCHDOG_STALL_SECS or flush_age >= WATCHDOG_STALL_SECS) {
            log.err("watchdog: stalled (last event {d}s ago, last ok flush {d}s ago), exiting for restart", .{
                event_age, flush_age,
            });
            std.process.exit(1);
        }
    }
}

pub const Config = struct {
    worker_url: []const u8,
    secret: []const u8,
};

pub fn getConfig() Config {
    return .{
        .worker_url = cGetenv("TYPEAHEAD_URL") orelse
            @panic("TYPEAHEAD_URL not set"),
        .secret = cGetenv("TYPEAHEAD_SECRET") orelse
            @panic("TYPEAHEAD_SECRET not set"),
    };
}

/// The relay firehose has no server-side collection filter (jetstream's
/// wantedCollections has no equivalent), so the allowlist moves client-side and
/// every frame is decoded before it can be discarded.
pub fn wantedCollection(collection: []const u8) bool {
    const exact = [_][]const u8{
        "app.bsky.actor.profile",
        "app.bsky.feed.post",
        "app.bsky.feed.like",
        "app.bsky.graph.follow",
        "sh.tangled.actor.profile",
    };
    for (exact) |e| if (mem.eql(u8, collection, e)) return true;
    return mem.startsWith(u8, collection, "site.standard.");
}

/// Render a profile record's avatar blob ref as the base32 CID string the rest
/// of the system stores. DAG-CBOR carries the ref as a real CID (tag 42), where
/// jetstream handed us a pre-rendered `ref.$link` string — so this transform is
/// new with the firehose cutover and is the one that can silently produce wrong
/// avatars rather than failing loudly.
pub fn avatarCidOf(allocator: Allocator, record: zat.cbor.Value) ?[]const u8 {
    const avatar = record.get("avatar") orelse return null;
    const cid = avatar.getCid("ref") orelse return null;
    return zat.multibase.encode(allocator, .base32lower, cid.raw) catch null;
}

/// an actor event to POST to the worker
pub const ActorEvent = struct {
    did: []const u8,
    handle: ?[]const u8 = null,
    display_name: ?[]const u8 = null,
    avatar_cid: ?[]const u8 = null,
    pds: ?[]const u8 = null,
};

pub const IngestHandler = struct {
    allocator: Allocator,
    config: Config,
    transport: *HttpTransport,
    /// nullable so unit tests / minimal modes can run without it.
    /// in production main wires this to a pool with N concurrent slingshot workers.
    identity_pool: ?*identity.Pool,
    buffer: std.ArrayList(ActorEvent),
    /// seq of the firehose event each buffered entry came from, 1:1 with
    /// `buffer`. Without it the flush can only advance the durable cursor when
    /// the buffer fully drains — which never happens at relay volume, so the
    /// cursor stayed 0 and every restart lost its place.
    cursors: std.ArrayList(i64),
    delete_buffer: std.ArrayList([]const u8),
    /// arena owns all string data in buffer/delete_buffer
    arena: std.heap.ArenaAllocator,
    /// bloom filter: fixed-size dedup for non-profile DIDs (~1.2MB)
    bloom: BloomFilter,
    pending_cursor: i64 = 0,
    flushed_cursor: i64 = 0,
    total_ingested: u64 = 0,
    total_deleted: u64 = 0,
    last_flush: i64 = 0,
    retry_after: i64 = 0, // timestamp before which we skip flush attempts
    consecutive_failures: u32 = 0,
    last_heartbeat: i64 = 0,

    pub fn init(
        allocator: Allocator,
        config: Config,
        transport: *HttpTransport,
        identity_pool: ?*identity.Pool,
    ) !IngestHandler {
        return .{
            .allocator = allocator,
            .config = config,
            .transport = transport,
            .identity_pool = identity_pool,
            .buffer = .empty,
            .cursors = .empty,
            .delete_buffer = .empty,
            .arena = std.heap.ArenaAllocator.init(allocator),
            .bloom = try BloomFilter.init(allocator, BLOOM_BITS, BLOOM_HASHES),
            .last_flush = timestamp(),
        };
    }

    pub fn deinit(self: *IngestHandler) void {
        self.bloom.deinit(self.allocator);
        self.arena.deinit();
        self.buffer.deinit(self.allocator);
        self.cursors.deinit(self.allocator);
        self.delete_buffer.deinit(self.allocator);
    }

    /// The ONLY way to buffer an event. Keeps `cursors` 1:1 with `buffer`; a
    /// bare `buffer.append` desynchronizes them and the flush then advances the
    /// durable cursor past events it never sent.
    fn enqueue(self: *IngestHandler, event: ActorEvent) void {
        self.buffer.append(self.allocator, event) catch return;
        self.cursors.append(self.allocator, self.pending_cursor) catch {
            _ = self.buffer.pop();
        };
    }

    fn dupe(self: *IngestHandler, s: []const u8) ?[]const u8 {
        return self.arena.allocator().dupe(u8, s) catch null;
    }

    fn resetBloom(self: *IngestHandler) void {
        log.info("resetting bloom filter ({d} insertions)", .{self.bloom.count});
        self.bloom.reset();
    }

    pub fn onEvent(self: *IngestHandler, event: zat.FirehoseEvent) void {
        // BEFORE dispatch: the handlers append to `buffer`, and each append
        // records `pending_cursor` as that entry's seq. Setting it afterwards
        // would tag every entry with the PREVIOUS event's seq.
        //
        // relay `seq`, NOT jetstream's µs timestamp — different keyspace, which
        // is why the cutover writes a fresh cursor key (see fetchCursor).
        if (event.seq()) |s| self.pending_cursor = s;

        switch (event) {
            .commit => |c| self.handleCommit(c),
            .identity => |id| self.handleIdentity(id),
            .account => |acct| self.handleAccount(acct),
            .sync, .info => {},
        }

        const now = timestamp();
        last_event_at.store(now, .monotonic);
        // drop incoming events if buffers are backed up (persistent failure)
        const MAX_BACKLOG = MAX_BATCH * 10;
        if (self.buffer.items.len >= MAX_BACKLOG or self.delete_buffer.items.len >= MAX_BACKLOG) {
            log.err("backlog overflow ({d} ingest, {d} delete), dropping oldest events", .{
                self.buffer.items.len, self.delete_buffer.items.len,
            });
            self.buffer.clearRetainingCapacity();
            self.cursors.clearRetainingCapacity();
            self.delete_buffer.clearRetainingCapacity();
            _ = self.arena.reset(.retain_capacity);
            self.resetBloom();
            self.flushed_cursor = self.pending_cursor;
            self.retry_after = 0;
        }

        // respect backoff cooldown after failures
        if (now < self.retry_after) return;

        const should_flush = self.buffer.items.len >= MAX_BATCH or
            self.delete_buffer.items.len >= MAX_BATCH or
            (now - self.last_flush >= 5 and (self.buffer.items.len > 0 or self.delete_buffer.items.len > 0));

        if (should_flush) {
            self.flush();
        }
    }

    pub fn onError(_: *IngestHandler, err: anyerror) void {
        log.err("firehose: {s}", .{@errorName(err)});
    }

    pub fn onConnect(_: *IngestHandler, host: []const u8) void {
        log.info("connected to {s}", .{host});
    }

    /// one firehose commit carries many ops, unlike jetstream's one-op events.
    fn handleCommit(self: *IngestHandler, c: zat.firehose.CommitEvent) void {
        for (c.ops) |op| {
            if (!wantedCollection(op.collection)) continue;
            self.handleOp(c.repo, op);
        }
    }

    fn handleOp(self: *IngestHandler, repo: []const u8, op: zat.firehose.RepoOp) void {
        if (mem.eql(u8, op.collection, "app.bsky.actor.profile") or
            mem.eql(u8, op.collection, "sh.tangled.actor.profile"))
        {
            // rich extraction from profile records (bsky and tangled)
            if (op.action != .create and op.action != .update) return;

            const record = op.record orelse return;

            const did = self.dupe(repo) orelse return;
            var event = ActorEvent{ .did = did };

            // bsky profiles have displayName; tangled profiles don't
            if (record.getString("displayName")) |name| {
                event.display_name = self.dupe(name);
            }

            event.avatar_cid = avatarCidOf(self.arena.allocator(), record);

            // ingester only sees self-labels from profile records — it can never
            // correctly determine hidden. let refreshModeration cron handle it.

            self.enqueue(event);
        } else {
            // non-profile collections: just discover the DID
            if (op.action == .delete) return;

            // dedup: skip if bloom filter says we've seen this DID recently
            if (self.bloom.contains(repo)) return;
            self.bloom.insert(repo);

            const did = self.dupe(repo) orelse return;
            self.enqueue(.{ .did = did });
        }
    }

    fn handleIdentity(self: *IngestHandler, id: zat.firehose.IdentityEvent) void {
        // jetstream's #identity event has `handle` as a spec-optional field; in
        // practice the bsky relay omits it on every event. so we always enqueue
        // the DID for slingshot resolution via the identity pool, and *also* take
        // the event handle as a hint if it happens to be present (rare, but harmless).
        if (self.identity_pool) |pool| {
            if (identity.Did.fromSlice(id.did)) |did| {
                _ = pool.offer(io, did); // best-effort; drop count tracked on pool
            }
        }

        if (id.handle) |h| {
            if (!mem.eql(u8, h, "handle.invalid")) {
                const event = ActorEvent{
                    .did = self.dupe(id.did) orelse return,
                    .handle = self.dupe(h),
                };
                self.enqueue(event);
            }
        }
    }

    fn handleAccount(self: *IngestHandler, acct: zat.firehose.AccountEvent) void {
        if (!acct.active) {
            const did = self.dupe(acct.did) orelse return;
            self.delete_buffer.append(self.allocator, did) catch return;
        }
    }

    fn flush(self: *IngestHandler) void {
        self.last_flush = timestamp();
        var any_failure = false;
        var posted_through: ?i64 = null;

        if (self.buffer.items.len > 0) {
            const batch_end = @min(self.buffer.items.len, MAX_BATCH);
            const batch = self.buffer.items[0..batch_end];
            const ok = postBatch(
                self.transport,
                self.config,
                batch,
                self.flushed_cursor,
            );
            if (ok) {
                self.total_ingested += batch_end;
                const rss = getRssKB();
                log.info("+{d} actors (total: {d}, pending: {d}) cursor={d} rss={d}KB", .{
                    batch_end,                         self.total_ingested,
                    self.buffer.items.len - batch_end, self.pending_cursor,
                    rss,
                });
                if (batch_end < self.buffer.items.len) {
                    std.mem.copyForwards(
                        ActorEvent,
                        self.buffer.items[0 .. self.buffer.items.len - batch_end],
                        self.buffer.items[batch_end..],
                    );
                }
                self.buffer.shrinkRetainingCapacity(self.buffer.items.len - batch_end);

                // The durable cursor advances to the last seq we ACTUALLY sent,
                // not to pending_cursor — pending_cursor covers events still in
                // the buffer, so publishing it would skip them on restart.
                if (batch_end <= self.cursors.items.len) {
                    posted_through = self.cursors.items[batch_end - 1];
                    if (batch_end < self.cursors.items.len) {
                        std.mem.copyForwards(
                            i64,
                            self.cursors.items[0 .. self.cursors.items.len - batch_end],
                            self.cursors.items[batch_end..],
                        );
                    }
                    self.cursors.shrinkRetainingCapacity(self.cursors.items.len - batch_end);
                }
            } else {
                log.err("ingest batch failed ({d} events, {d} pending), will retry in 5s", .{
                    batch_end, self.buffer.items.len,
                });
                any_failure = true;
            }
        }

        if (self.delete_buffer.items.len > 0) {
            const batch_end = @min(self.delete_buffer.items.len, MAX_BATCH);
            const batch = self.delete_buffer.items[0..batch_end];
            const ok = deleteActors(
                self.transport,
                self.config,
                batch,
            );
            if (ok) {
                self.total_deleted += batch_end;
                log.info("-{d} moderated (total: {d})", .{
                    batch_end, self.total_deleted,
                });
                if (batch_end < self.delete_buffer.items.len) {
                    const remaining = self.delete_buffer.items.len - batch_end;
                    var i: usize = 0;
                    while (i < remaining) : (i += 1) {
                        self.delete_buffer.items[i] = self.delete_buffer.items[batch_end + i];
                    }
                    self.delete_buffer.shrinkRetainingCapacity(remaining);
                } else {
                    self.delete_buffer.clearRetainingCapacity();
                }
            } else {
                log.err("delete batch failed ({d} dids), will retry in 5s", .{batch_end});
                any_failure = true;
            }
        }

        if (any_failure) {
            self.consecutive_failures += 1;
            if (self.consecutive_failures >= WATCHDOG_MAX_CONSECUTIVE_FAILURES) {
                log.err("watchdog: {d} consecutive flush failures, exiting for restart", .{
                    self.consecutive_failures,
                });
                std.process.exit(1);
            }
            self.retry_after = timestamp() + 5;
        } else {
            self.consecutive_failures = 0;
            const now = timestamp();
            last_flush_ok_at.store(now, .monotonic);
            if (now - self.last_heartbeat >= HEARTBEAT_INTERVAL_SECS) {
                self.last_heartbeat = now;
                const span = logfire.span("ingest.heartbeat", .{});
                defer span.end();
                span.setAttribute("total_ingested", self.total_ingested);
                span.setAttribute("total_deleted", self.total_deleted);
            }
            // Advance only past what was sent, and only while no deletes are
            // queued — a pending delete carries a lower seq, and skipping it
            // would drop a moderation event on restart.
            if (posted_through) |c| {
                if (self.delete_buffer.items.len == 0 and c > self.flushed_cursor) {
                    self.flushed_cursor = c;
                }
            }
            if (self.buffer.items.len == 0 and self.delete_buffer.items.len == 0) {
                if (self.pending_cursor > self.flushed_cursor) {
                    self.flushed_cursor = self.pending_cursor;
                }
                _ = self.arena.reset(.retain_capacity);
            }
        }
    }
};

// -- HTTP client functions (worker API) --

fn postBatch(transport: *HttpTransport, config: Config, events: []const ActorEvent, cursor: i64) bool {
    var output: std.Io.Writer.Allocating = .init(transport.allocator);
    defer output.deinit();

    var jw: json.Stringify = .{
        .writer = &output.writer,
        .options = .{ .emit_null_optional_fields = false },
    };
    jw.write(.{ .events = events, .cursor = cursor }) catch return false;

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/ingest", .{config.worker_url}) catch return false;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return false;

    const result = transport.fetch(.{
        .url = url,
        .method = .POST,
        .payload = output.written(),
        .authorization = auth,
    }) catch return false;
    defer transport.allocator.free(result.body);

    if (result.status != .ok) {
        log.err("ingest HTTP {d}: {s}", .{ @intFromEnum(result.status), result.body });
    }

    return result.status == .ok;
}

fn deleteActors(transport: *HttpTransport, config: Config, dids: []const []const u8) bool {
    var output: std.Io.Writer.Allocating = .init(transport.allocator);
    defer output.deinit();

    var jw: json.Stringify = .{ .writer = &output.writer };
    jw.write(.{ .dids = dids }) catch return false;

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/delete", .{config.worker_url}) catch return false;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return false;

    const result = transport.fetch(.{
        .url = url,
        .method = .POST,
        .payload = output.written(),
        .authorization = auth,
    }) catch return false;
    defer transport.allocator.free(result.body);

    return result.status == .ok;
}

fn fetchCursorOnce(transport: *HttpTransport, config: Config) ?i64 {
    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/cursor", .{config.worker_url}) catch return null;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return null;

    const result = transport.fetch(.{
        .url = url,
        .authorization = auth,
    }) catch return null;
    defer transport.allocator.free(result.body);

    if (result.status != .ok) return null;

    const parsed = json.parseFromSlice(json.Value, transport.allocator, result.body, .{}) catch return null;
    defer parsed.deinit();

    const cursor_val = parsed.value.object.get("cursor") orelse return null;
    return switch (cursor_val) {
        .integer => |n| n,
        .float => |f| @intFromFloat(f),
        else => null,
    };
}

/// A relay `seq` is a small monotonic counter (~8.5e8 as of 2026-07). Jetstream's
/// cursor was a µs timestamp (~1.78e15). During the jetstream→relay cutover a
/// jetstream-era ingester can still be writing µs values into the shared cursor
/// key, so a stale one must be rejected rather than replayed: subscribing at a
/// seq six orders of magnitude in the future yields an empty stream, and the
/// watchdog would read that as a dead upstream instead of a bad cursor.
/// Rejecting means "start live", which is safe — the refresh cron re-covers
/// anything missed.
pub const MAX_PLAUSIBLE_RELAY_SEQ: i64 = 1_000_000_000_000; // 1e12

pub fn plausibleRelayCursor(cursor: ?i64) ?i64 {
    const c = cursor orelse return null;
    if (c <= 0 or c >= MAX_PLAUSIBLE_RELAY_SEQ) return null;
    return c;
}

pub fn fetchCursor(transport: *HttpTransport, config: Config) ?i64 {
    const backoff = [_]u64{ 1, 3, 10 };
    for (backoff, 0..) |delay, attempt| {
        if (fetchCursorOnce(transport, config)) |cursor| {
            if (plausibleRelayCursor(cursor)) |ok| return ok;
            log.warn("rejecting implausible relay cursor {d} (jetstream-era µs value?), starting live", .{cursor});
            return null;
        }
        log.warn("cursor fetch failed (attempt {d}/3), retrying in {d}s", .{ attempt + 1, delay });
        io.sleep(.{ .nanoseconds = delay * std.time.ns_per_s }, .real) catch {};
    }
    return null;
}

// -- utilities --

pub fn getRssKB() u64 {
    const f = std.Io.Dir.openFileAbsolute(io, "/proc/self/statm", .{}) catch return 0;
    defer f.close(io);
    var buf: [128]u8 = undefined;
    const n = f.readStreaming(io, &.{&buf}) catch return 0;
    var it = std.mem.splitScalar(u8, buf[0..n], ' ');
    _ = it.next(); // size
    const rss_pages = std.fmt.parseInt(u64, it.next() orelse return 0, 10) catch return 0;
    return rss_pages * 4;
}

const testing = std.testing;

test "wantedCollection: allowlist survives the loss of jetstream's server-side filter" {
    try testing.expect(wantedCollection("app.bsky.actor.profile"));
    try testing.expect(wantedCollection("app.bsky.feed.post"));
    try testing.expect(wantedCollection("app.bsky.feed.like"));
    try testing.expect(wantedCollection("app.bsky.graph.follow"));
    try testing.expect(wantedCollection("sh.tangled.actor.profile"));
    // jetstream expressed this as the wildcard "site.standard.*"
    try testing.expect(wantedCollection("site.standard.profile"));
    try testing.expect(wantedCollection("site.standard.anything.else"));

    try testing.expect(!wantedCollection("site.standard"));
    try testing.expect(!wantedCollection("app.bsky.feed.repost"));
    try testing.expect(!wantedCollection("com.atproto.lexicon.schema"));
    try testing.expect(!wantedCollection(""));
}

test "avatarCidOf renders a DAG-CBOR blob ref to the stored base32 CID" {
    const allocator = testing.allocator;
    // zzstoatzz.io's real avatar CID as of 2026-07-28. cbor.Cid.raw holds the
    // CID bytes with the 0x00 identity-multibase prefix already stripped, so a
    // decode → wrap → encode round-trip must reproduce the string exactly. A
    // regression here yields a plausible-looking but wrong avatar URL.
    const want = "bafkreiegehiwbazkkpp5nucenajtdjwinxrrnz7ukawpqnudbr47f44zhq";
    const raw = try zat.multibase.decode(allocator, want);
    defer allocator.free(raw);

    const record = zat.cbor.Value{ .map = &.{
        .{ .key = "displayName", .value = .{ .text = "nate" } },
        .{ .key = "avatar", .value = .{ .map = &.{
            .{ .key = "$type", .value = .{ .text = "blob" } },
            .{ .key = "ref", .value = .{ .cid = .{ .raw = raw } } },
            .{ .key = "mimeType", .value = .{ .text = "image/jpeg" } },
        } } },
    } };

    const got = avatarCidOf(allocator, record) orelse return error.NoAvatar;
    defer allocator.free(@constCast(got));
    try testing.expectEqualStrings(want, got);

    try testing.expectEqualStrings("nate", record.getString("displayName").?);
}

test "avatarCidOf returns null when the profile has no avatar" {
    const record = zat.cbor.Value{ .map = &.{
        .{ .key = "displayName", .value = .{ .text = "nate" } },
    } };
    try testing.expect(avatarCidOf(testing.allocator, record) == null);
}

test "plausibleRelayCursor rejects a jetstream-era µs cursor" {
    // the exact value /admin/cursor served mid-cutover on 2026-07-28, written by
    // a still-running jetstream ingester after the worker switched keys
    try testing.expect(plausibleRelayCursor(1785217869995374) == null);
    try testing.expect(plausibleRelayCursor(null) == null);
    try testing.expect(plausibleRelayCursor(0) == null);
    try testing.expect(plausibleRelayCursor(-1) == null);

    // real relay seqs (~8.5e8 in 2026-07) pass through untouched
    try testing.expectEqual(@as(?i64, 853_602_603), plausibleRelayCursor(853_602_603));
    try testing.expectEqual(@as(?i64, 1), plausibleRelayCursor(1));
}

test "relay seq spaces are per-relay: a cursor is only valid for its issuer" {
    // Measured 2026-07-28, first seq from each host in zat's default_hosts:
    //   bsky.network                    32_234_719_514
    //   northamerica.firehose.network    7_620_802_119
    //   europe.firehose.network          7_032_143_016
    //   asia.firehose.network            5_581_515_167
    // These are independent relays, not mirrors. The ingester failed over to
    // asia and wrote 5_581_374_644 into the cursor key while pinned-in-config
    // to bsky — resuming that against bsky would skip ~26.6 BILLION events.
    // All four are individually "plausible", which is exactly why the range
    // guard cannot catch this and why the client must pin to one host.
    const observed = [_]i64{ 32_234_719_514, 7_620_802_119, 7_032_143_016, 5_581_515_167 };
    for (observed) |s| {
        try testing.expect(plausibleRelayCursor(s) != null);
    }
    // the spread between relays dwarfs any tolerance a heuristic could apply
    try testing.expect(observed[0] - observed[3] > 26_000_000_000);
}

test "durable cursor advances past posted batches without draining the buffer" {
    // The bug: flushed_cursor only advanced when buffer AND delete_buffer were
    // both empty. Under jetstream's server-filtered stream the buffer drained
    // constantly so it worked; the relay firehose delivers ~4x the actor events
    // and the buffer is never empty, so the cursor sat at 0 and every restart
    // silently lost its place. Verified in prod 2026-07-28: /admin/cursor
    // returned 0 while the ingester was at seq ~32,235,000,000.
    //
    // This models the invariant the fix relies on: after posting the first N of
    // M buffered entries, the durable cursor is entry N-1's seq — NOT the
    // newest seq seen (which covers entries still buffered) and NOT 0.
    var cursors: std.ArrayList(i64) = .empty;
    defer cursors.deinit(testing.allocator);
    for ([_]i64{ 100, 101, 102, 103, 104 }) |c| {
        try cursors.append(testing.allocator, c);
    }

    const batch_end: usize = 3;
    const posted_through = cursors.items[batch_end - 1];
    try testing.expectEqual(@as(i64, 102), posted_through);

    // and the remainder keeps its own seqs for the next flush
    std.mem.copyForwards(
        i64,
        cursors.items[0 .. cursors.items.len - batch_end],
        cursors.items[batch_end..],
    );
    cursors.shrinkRetainingCapacity(cursors.items.len - batch_end);
    try testing.expectEqualSlices(i64, &.{ 103, 104 }, cursors.items);

    // advancing to the newest seen seq instead would skip 103 and 104
    try testing.expect(posted_through < 104);
}
