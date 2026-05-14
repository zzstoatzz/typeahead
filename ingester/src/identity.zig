//! Identity resolution for DIDs received from jetstream #identity events.
//!
//! Background: jetstream's `#identity` event has `handle` as a spec-optional field
//! ("validated or passed-through from an upstream source"). In practice the bsky
//! relay omits it entirely on every event we've sampled. So the ingester forwards
//! every identity-event DID to this resolver, which:
//!   1. resolves the authoritative handle via microcosm's slingshot
//!   2. POSTs {did, handle} back to the cloudflare worker's /admin/ingest
//!   3. lets the worker's existing COALESCE-or-overwrite upsert do the rest
//!
//! Best-effort: failures are logged and dropped. The hourly enrichActors cron is
//! the safety net for anything this misses (e.g. slingshot outage windows).

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
    transport: HttpTransport,
    worker_url: []const u8,
    worker_secret: []const u8,

    pub fn init(
        allocator: Allocator,
        io: Io,
        worker_url: []const u8,
        worker_secret: []const u8,
    ) Resolver {
        var transport = HttpTransport.init(io, allocator);
        transport.keep_alive = true; // we'll make many calls to slingshot
        return .{
            .allocator = allocator,
            .transport = transport,
            .worker_url = worker_url,
            .worker_secret = worker_secret,
        };
    }

    pub fn deinit(self: *Resolver) void {
        self.transport.deinit();
    }

    /// poolio process function — called per dequeued Did.
    /// Signature matches poolio.Pool(Did, Resolver).ProcessFn.
    pub fn process(self: *Resolver, io: Io, did: Did) void {
        _ = io;
        const did_slice = did.slice();

        const resolved = self.resolveSlingshot(did_slice) catch |err| {
            log.warn("slingshot {s}: {s}", .{ did_slice, @errorName(err) });
            return;
        } orelse return; // unresolvable right now — cron will retry

        defer self.allocator.free(resolved.handle);

        // jetstream/atproto convention: failed validation surfaces as "handle.invalid"
        if (mem.eql(u8, resolved.handle, "handle.invalid")) return;

        self.postIdentity(did_slice, resolved.handle) catch |err| {
            log.warn("post identity {s}: {s}", .{ did_slice, @errorName(err) });
        };
    }

    const Resolved = struct {
        handle: []const u8, // owned, free with self.allocator
    };

    fn resolveSlingshot(self: *Resolver, did: []const u8) !?Resolved {
        var url_buf: [512]u8 = undefined;
        const url = try std.fmt.bufPrint(&url_buf, "{s}?identifier={s}", .{ SLINGSHOT_URL, did });

        const result = try self.transport.fetch(.{ .url = url });
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

        return .{ .handle = try self.allocator.dupe(u8, handle_str) };
    }

    fn postIdentity(self: *Resolver, did: []const u8, handle: []const u8) !void {
        var output: std.Io.Writer.Allocating = .init(self.allocator);
        defer output.deinit();

        var jw: json.Stringify = .{
            .writer = &output.writer,
            .options = .{ .emit_null_optional_fields = false },
        };

        const event = ingest.ActorEvent{ .did = did, .handle = handle };
        // single-event batch, no cursor (we don't want to clobber jetstream's cursor)
        try jw.write(.{ .events = &[_]ingest.ActorEvent{event} });

        var url_buf: [512]u8 = undefined;
        const url = try std.fmt.bufPrint(&url_buf, "{s}/admin/ingest", .{self.worker_url});

        var auth_buf: [256]u8 = undefined;
        const auth = try std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.worker_secret});

        const result = try self.transport.fetch(.{
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
