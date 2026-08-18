//! Identity resolution for DIDs received from relay's firehose #identity events.
//!
//! Background: the `#identity` event has `handle` as a spec-optional field
//! ("validated or passed-through from an upstream source"). In practice the bsky
//! relay omits it entirely on every event we've sampled. So the ingester forwards
//! every identity-event DID to this resolver, which:
//!   1. resolves the authoritative handle via microcosm's slingshot
//!   2. POSTs {did, handle} back to the cloudflare worker's /admin/ingest
//!   3. lets the worker's existing COALESCE-or-overwrite upsert do the rest
//!
//! A resolution failure never drops the DID: we still POST the bare DID so the row
//! exists, because the hourly enrichIdentity cron retries `WHERE handle = ''` and can
//! only ever see actors that are already rows. Dropping here made an account with a
//! wedged .well-known permanently invisible (birdlover.selfhosted.social, 2026-08-18).

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

const SLINGSHOT_URL = "https://slingshot.microcosm.blue/xrpc/blue.microcosm.identity.resolveMiniDoc";

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

    /// poolio process function — called per dequeued Did.
    /// Signature matches poolio.Pool(Did, Resolver).ProcessFn.
    pub fn process(self: *Resolver, io: Io, did: Did) void {
        _ = io;
        const did_slice = did.slice();

        // Pool workers share Resolver as their context. std.http.Client is not
        // safe to use concurrently, so each worker invocation owns its client.
        // Identity resolution is only two requests and they target different
        // hosts, making cross-request keep-alive useless here anyway.
        var transport = HttpTransport.init(self.io, self.allocator);
        transport.keep_alive = false;
        defer transport.deinit();

        const maybe = self.resolveSlingshot(&transport, did_slice) catch |err| blk: {
            log.warn("slingshot {s}: {s}", .{ did_slice, @errorName(err) });
            break :blk null;
        };

        // An unresolvable handle must not drop the DID. enrichIdentity's retry
        // drain selects `WHERE handle = ''`, so it can only retry a row that
        // already exists — a DID dropped here is invisible forever, leaving only
        // a log line to say it was ever seen. Recording the bare DID is what makes
        // that cron the safety net this file's header claims it is.
        defer if (maybe) |r| {
            self.allocator.free(r.handle);
            if (r.pds) |pds| self.allocator.free(pds);
        };

        const event = eventFor(did_slice, maybe);
        self.postIdentity(&transport, event) catch |err| {
            log.warn("post identity {s}: {s}", .{ did_slice, @errorName(err) });
        };
    }

    /// The row we record for a DID, given whatever slingshot could tell us.
    ///
    /// Split out from `process` so the "never drop the DID" rule is testable
    /// without HTTP: an unresolvable or unvalidated handle still yields an event
    /// carrying the DID, which `/admin/ingest` turns into a bare
    /// `INSERT OR IGNORE INTO actors (did)`. That row lands in enrichIdentity's
    /// `WHERE handle = ''` drain; without it the actor is never retried at all.
    fn eventFor(did: []const u8, resolved: ?Resolved) ingest.ActorEvent {
        const r = resolved orelse return .{ .did = did };
        // atproto convention: failed validation surfaces as "handle.invalid".
        // Keep the DID and any pds we learned; let the cron retry the handle.
        if (mem.eql(u8, r.handle, "handle.invalid")) return .{ .did = did, .pds = r.pds };
        return .{ .did = did, .handle = r.handle, .pds = r.pds };
    }

    const Resolved = struct {
        handle: []const u8, // owned, free with self.allocator
        /// slingshot returns this in the same response the handle came from.
        /// It used to be dropped on the floor, which left the enrichment cron
        /// backfilling pds one DID at a time — 9.3M rows deep at 500/run.
        pds: ?[]const u8 = null, // owned, free with self.allocator
    };

    fn resolveSlingshot(self: *Resolver, transport: *HttpTransport, did: []const u8) !?Resolved {
        var url_buf: [512]u8 = undefined;
        const url = try std.fmt.bufPrint(&url_buf, "{s}?identifier={s}", .{ SLINGSHOT_URL, did });

        const result = try transport.fetch(.{
            .url = url,
            .extra_headers = &.{.{ .name = "user-agent", .value = "typeahead (zzstoatzz.io)" }},
        });
        defer self.allocator.free(result.body);

        if (result.status != .ok) return null;

        var parsed = json.parseFromSlice(json.Value, self.allocator, result.body, .{}) catch return null;
        defer parsed.deinit();

        if (parsed.value != .object) return null;
        const handle_node = parsed.value.object.get("handle") orelse return null;
        const handle_str = switch (handle_node) {
            .string => |s| s,
            else => return null,
        };
        if (handle_str.len == 0) return null;

        var pds_owned: ?[]const u8 = null;
        if (parsed.value.object.get("pds")) |pds_node| {
            if (pds_node == .string and pds_node.string.len > 0) {
                pds_owned = try self.allocator.dupe(u8, pds_node.string);
            }
        }

        return .{ .handle = try self.allocator.dupe(u8, handle_str), .pds = pds_owned };
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

/// Convenience alias — the poolio instantiation used throughout the ingester.
pub const Pool = poolio.Pool(Did, Resolver);

const testing = std.testing;

test "eventFor: unresolvable handle still records the DID" {
    // Regression: this returned nothing, so a DID whose handle could not be
    // resolved never became a row — and enrichIdentity, which retries
    // `WHERE handle = ''`, only ever sees rows. birdlover.selfhosted.social sat
    // in the relay with a healthy repo and was absent from the corpus entirely.
    const event = Resolver.eventFor("did:plc:iu2klrrbxplyyqgltk7sg5l3", null);
    try testing.expectEqualStrings("did:plc:iu2klrrbxplyyqgltk7sg5l3", event.did);
    try testing.expect(event.handle == null);
    try testing.expect(event.pds == null);
}

test "eventFor: handle.invalid records the DID and keeps the pds" {
    const event = Resolver.eventFor("did:plc:abc", .{
        .handle = "handle.invalid",
        .pds = "https://selfhosted.social",
    });
    try testing.expectEqualStrings("did:plc:abc", event.did);
    try testing.expect(event.handle == null);
    try testing.expectEqualStrings("https://selfhosted.social", event.pds.?);
}

test "eventFor: a resolved handle is passed through intact" {
    const event = Resolver.eventFor("did:plc:abc", .{
        .handle = "birdlover.waow.tech",
        .pds = "https://selfhosted.social",
    });
    try testing.expectEqualStrings("birdlover.waow.tech", event.handle.?);
    try testing.expectEqualStrings("https://selfhosted.social", event.pds.?);
}
