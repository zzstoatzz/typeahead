//! The Tantivy server's private source endpoints, as the bridge uses them.
//!
//! POST /admin/source-state      {"dids":[..]}
//!   -> {"source_cursor":<json|null>,"source_observation_revision":n,
//!       "actors":[{"did":..,"document_sha256":<hex|null>}]}
//! POST /admin/source-page       {"after_did":"..","limit":n}
//!   -> {"dids":[..],"source_cursor":..,"source_observation_revision":n}
//! POST /admin/source-reconcile  {"actors":[..],"expected_documents":[..],
//!       "source_cursor":{..},"expected_source_cursor":<json|null>,
//!       "expected_observation_revision":n,"watermark":n?,
//!       "policy_snapshot":{..}?,"batch_id":<64 hex>}
//!   -> {"applied":n,"replayed":n,"stale":n,"watermark":n,"opstamp":n,
//!       "source_cursor":{..},"source_observation_revision":n}
//! GET  /index/status            -> generation metadata
//!
//! Limits enforced by the server: 500 DIDs per state or page request, 500
//! actors and 16 MiB per reconcile, a 16 KiB cursor.

const std = @import("std");
const Allocator = std.mem.Allocator;
const json = std.json;
const Sha256 = std.crypto.hash.sha2.Sha256;
const zat = @import("zat");
const source = @import("source.zig");

pub const source_policy = "store-change-log-v1";
pub const max_batch = 500;
pub const max_request_bytes = 16 * 1024 * 1024;
const max_profile_field = 65535;
const max_json_field = 1024 * 1024;

pub const Phase = enum { idle, source, backend };

/// Resync progress. The field names `last_completed_at` and
/// `last_completed_started_at` are read by the server's /index/status.
pub const Reconciliation = struct {
    schema_version: u32 = 1,
    round: u64 = 0,
    phase: Phase = .idle,
    after_did: []const u8 = "",
    round_started_at: u64 = 0,
    last_completed_at: ?u64 = null,
    last_completed_started_at: ?u64 = null,
};

/// The opaque cursor the server stores atomically with the documents. Its
/// `position` is the next change-log sequence the index has not applied; it
/// is the only authority on that. `schema_version`, `build_id`,
/// `snapshot_watermark`, `through` and `cycle` are shaped the way the
/// server's /index/status reads them.
pub const Cursor = struct {
    schema_version: u32 = 1,
    source_policy: []const u8 = source_policy,
    source_id: []const u8,
    build_id: []const u8,
    snapshot_watermark: u64,
    position: u64,
    through: u64,
    cycle: ?u8 = null,
    reconciliation: Reconciliation = .{},
};

pub const ExpectedDocument = struct {
    did: []const u8,
    document_sha256: ?[]const u8,
};

pub const State = struct {
    /// Compact JSON of the committed cursor, "null" before the first commit.
    cursor: []const u8,
    revision: u64,
    documents: []const ExpectedDocument,
};

pub const Page = struct {
    dids: []const []const u8,
    cursor: []const u8,
    revision: u64,
};

pub const Receipt = struct {
    applied: u64,
    replayed: u64,
    stale: u64 = 0,
    watermark: u64,
    opstamp: u64 = 0,
    source_observation_revision: u64,
};

pub const Status = struct {
    build_id: []const u8,
    snapshot_watermark: u64,
    watermark: u64,
    import_complete: bool,
    ready: bool,
    source_observation_revision: u64,
    policy_observed_at: ?u64,
};

pub const Outcome = union(enum) {
    committed: Receipt,
    /// 409: the cursor, the revision, a document hash or a reused batch ID
    /// did not match. Nothing was written.
    conflict,
    /// 400-class: the server refused the request body. Nothing was written.
    invalid: []const u8,
};

/// One document in a reconcile request; `record == null` is a tombstone.
pub const Entry = struct {
    did: []const u8,
    version: u64,
    record: ?source.Actor,
};

pub const PolicySnapshot = struct {
    policy: source.Policy,
    observed_at: u64,
};

pub const Reconcile = struct {
    entries: []const Entry,
    expected: []const ExpectedDocument,
    cursor: Cursor,
    expected_cursor: []const u8,
    expected_revision: u64,
    watermark: ?u64 = null,
    policy: ?PolicySnapshot = null,
};

pub fn validDid(did: []const u8) bool {
    if (!std.mem.startsWith(u8, did, "did:") or did.len > 2048) return false;
    for (did) |byte| if (byte <= 0x20 or byte == '|') return false;
    return std.unicode.utf8ValidateSlice(did);
}

/// Why the server would refuse this record, mirroring `Actor::validate`, or
/// null when it is admissible. An empty handle is not checked here: the
/// bridge sends such a record as a tombstone.
pub fn rejection(arena: Allocator, actor: source.Actor) ?[]const u8 {
    if (!std.math.isFinite(actor.quality_score)) return "quality score is not finite";
    inline for (.{ "handle", "display_name", "avatar", "created_at", "pds" }) |name| {
        const value = @field(actor, name);
        if (value.len > max_profile_field) return name ++ " exceeds 65535 bytes";
        if (!std.unicode.utf8ValidateSlice(value)) return name ++ " is not UTF-8";
    }
    if (actor.associated.len > max_json_field) return "associated exceeds 1 MiB";
    if (actor.associated.len > 0) {
        const value = json.parseFromSliceLeaky(json.Value, arena, actor.associated, .{}) catch return "associated is not JSON";
        if (value != .object) return "associated is not a JSON object";
    }
    var label_bytes: usize = 0;
    for (actor.labels) |label| {
        inline for (.{ "source", "uri", "value", "created", "cid", "expires" }) |name| {
            const value = @field(label, name);
            label_bytes += value.len;
            if (!std.unicode.utf8ValidateSlice(value)) return "a label is not UTF-8";
        }
    }
    if (label_bytes > max_json_field) return "labels exceed 1 MiB";
    return null;
}

/// Bytes of profile data `encodeEntry` writes for this record, to bound a
/// request before it is built.
pub fn profileBytes(actor: source.Actor) usize {
    var total = actor.handle.len + actor.display_name.len + actor.avatar.len + actor.created_at.len + actor.pds.len + actor.associated.len;
    for (actor.labels) |label| {
        total += 64 + label.source.len + label.uri.len + label.value.len + label.created.len + label.cid.len + label.expires.len;
    }
    return total;
}

fn digits(text: []const u8) ?u32 {
    var value: u32 = 0;
    for (text) |c| {
        if (!std.ascii.isDigit(c)) return null;
        value = value * 10 + (c - '0');
    }
    return value;
}

/// Whether a label expiry is an RFC 3339 timestamp both sides read the same
/// way. The server stops serving while any live document carries an expiry
/// it cannot evaluate, and the store treats one it cannot parse as never
/// expiring, so the bridge forwards only these and drops the rest: the
/// label then never expires in the index either.
pub fn supportedExpiry(text: []const u8) bool {
    if (text.len < 20 or text[4] != '-' or text[7] != '-' or text[10] != 'T' or text[13] != ':' or text[16] != ':') return false;
    const year = digits(text[0..4]) orelse return false;
    const month = digits(text[5..7]) orelse return false;
    const day = digits(text[8..10]) orelse return false;
    const hour = digits(text[11..13]) orelse return false;
    const minute = digits(text[14..16]) orelse return false;
    const second = digits(text[17..19]) orelse return false;
    const leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0);
    const month_days: u32 = switch (month) {
        1, 3, 5, 7, 8, 10, 12 => 31,
        4, 6, 9, 11 => 30,
        2 => if (leap) 29 else 28,
        else => return false,
    };
    if (year < 1000 or day < 1 or day > month_days or hour > 23 or minute > 59 or second > 59) return false;
    var at: usize = 19;
    if (text[at] == '.') {
        at += 1;
        const start = at;
        while (at < text.len and std.ascii.isDigit(text[at])) at += 1;
        if (at == start or at - start > 9 or at == text.len) return false;
    }
    if (text[at] == 'Z') return at + 1 == text.len;
    if (text[at] != '+' and text[at] != '-') return false;
    if (text.len != at + 6 or text[at + 3] != ':') return false;
    const offset_hours = digits(text[at + 1 .. at + 3]) orelse return false;
    const offset_minutes = digits(text[at + 4 .. at + 6]) orelse return false;
    return offset_hours <= 23 and offset_minutes <= 59;
}

fn string(w: *std.Io.Writer, value: []const u8) !void {
    try json.Stringify.encodeJsonString(value, .{}, w);
}

fn count(value: u64) i64 {
    return @intCast(@min(value, std.math.maxInt(i64)));
}

/// Labels as the public API serves them. Negated labels are withdrawn and
/// are not sent, as in today's `actors.labels` projection.
fn encodeLabels(w: *std.Io.Writer, labels: []const source.Label) !void {
    try w.writeByte('[');
    var first = true;
    for (labels) |label| {
        if (label.negated) continue;
        if (!first) try w.writeByte(',');
        first = false;
        try w.writeAll("{\"src\":");
        try string(w, label.source);
        try w.writeAll(",\"uri\":");
        try string(w, label.uri);
        try w.writeAll(",\"val\":");
        try string(w, label.value);
        try w.writeAll(",\"cts\":");
        try string(w, label.created);
        if (label.cid.len > 0) {
            try w.writeAll(",\"cid\":");
            try string(w, label.cid);
        }
        if (supportedExpiry(label.expires)) {
            try w.writeAll(",\"exp\":");
            try string(w, label.expires);
        }
        try w.writeByte('}');
    }
    try w.writeByte(']');
}

fn encodeEntry(w: *std.Io.Writer, entry: Entry) !void {
    try w.writeAll("{\"did\":");
    try string(w, entry.did);
    try w.print(",\"version\":{d}", .{entry.version});
    if (entry.record) |actor| {
        try w.writeAll(",\"deleted\":false,\"hidden\":");
        try w.writeAll(if (actor.hidden) "true" else "false");
        try w.writeAll(",\"handle\":");
        try string(w, actor.handle);
        try w.writeAll(",\"display_name\":");
        try string(w, actor.display_name);
        try w.writeAll(",\"avatar_url\":");
        try string(w, actor.avatar);
        try w.writeAll(",\"labels\":");
        try encodeLabels(w, actor.labels);
        try w.writeAll(",\"associated\":");
        try w.writeAll(if (actor.associated.len > 0) actor.associated else "{}");
        try w.writeAll(",\"created_at\":");
        try string(w, actor.created_at);
        try w.writeAll(",\"pds\":");
        try string(w, actor.pds);
        // Widened to f64 so the decimal text is exact for the f32 and the
        // server's f64-to-f32 conversion returns the same bits.
        try w.print(",\"quality_score\":{d}", .{@as(f64, actor.quality_score)});
        try w.print(",\"followers_count\":{d},\"follows_count\":{d},\"posts_count\":{d},\"last_activity_at\":0}}", .{
            count(actor.followers_count), count(actor.follows_count), count(actor.posts_count),
        });
    } else {
        try w.writeAll(",\"deleted\":true,\"hidden\":false,\"handle\":\"\",\"display_name\":\"\",\"avatar_url\":\"\"," ++
            "\"labels\":[],\"associated\":{},\"created_at\":\"\",\"pds\":\"\",\"quality_score\":0," ++
            "\"followers_count\":0,\"follows_count\":0,\"posts_count\":0,\"last_activity_at\":0}");
    }
}

pub fn encodeCursor(arena: Allocator, cursor: Cursor) ![]u8 {
    return json.Stringify.valueAlloc(arena, cursor, .{});
}

/// A committed cursor this bridge wrote, or null for any other value
/// (absent, written by another controller, or an unknown schema).
pub fn parseCursor(arena: Allocator, text: []const u8) ?Cursor {
    const cursor = json.parseFromSliceLeaky(Cursor, arena, text, .{ .allocate = .alloc_always }) catch return null;
    if (cursor.schema_version != 1 or !std.mem.eql(u8, cursor.source_policy, source_policy)) return null;
    if (cursor.reconciliation.schema_version != 1 or cursor.cycle != null) return null;
    return cursor;
}

/// The request body. `batch_id` is the SHA-256 of everything before it, so an
/// identical request carries an identical ID and the server answers a repeat
/// with the stored receipt instead of applying it twice.
pub fn encodeReconcile(arena: Allocator, request: Reconcile) ![]u8 {
    var out: std.Io.Writer.Allocating = .init(arena);
    const w = &out.writer;
    try w.writeAll("{\"actors\":[");
    for (request.entries, 0..) |entry, index| {
        if (index > 0) try w.writeByte(',');
        try encodeEntry(w, entry);
    }
    try w.writeAll("],\"expected_documents\":");
    try json.Stringify.value(request.expected, .{}, w);
    try w.writeAll(",\"source_cursor\":");
    try json.Stringify.value(request.cursor, .{}, w);
    try w.writeAll(",\"expected_source_cursor\":");
    try w.writeAll(request.expected_cursor);
    try w.print(",\"expected_observation_revision\":{d}", .{request.expected_revision});
    if (request.watermark) |watermark| try w.print(",\"watermark\":{d}", .{watermark});
    if (request.policy) |snapshot| {
        try w.print(",\"policy_snapshot\":{{\"schema_version\":1,\"complete\":true,\"source_observed_at\":{d},\"overrides\":", .{snapshot.observed_at});
        try json.Stringify.value(snapshot.policy.overrides, .{}, w);
        try w.writeAll(",\"blocked_domains\":");
        try json.Stringify.value(snapshot.policy.blocked_domains, .{}, w);
        try w.writeByte('}');
    }
    var digest: [Sha256.digest_length]u8 = undefined;
    Sha256.hash(out.written(), &digest, .{});
    try w.print(",\"batch_id\":\"{s}\"}}", .{std.fmt.bytesToHex(digest, .lower)});
    return out.written();
}

pub const Request = struct {
    method: std.http.Method,
    path: []const u8,
    body: ?[]const u8 = null,
};

pub const Reply = struct {
    status: u16,
    body: []const u8,
};

/// How requests reach the server. `Http` is the real one; tests substitute a
/// model of the server.
pub const Transport = struct {
    ptr: *anyopaque,
    send: *const fn (ptr: *anyopaque, arena: Allocator, request: Request) anyerror!Reply,
};

pub const Error = error{ Unauthorized, IndexUnavailable, IndexRejected, MalformedReply } || Allocator.Error;

pub const Client = struct {
    transport: Transport,

    fn call(self: Client, arena: Allocator, request: Request) Error!Reply {
        const reply = self.transport.send(self.transport.ptr, arena, request) catch |err| switch (err) {
            error.OutOfMemory => return error.OutOfMemory,
            else => return error.IndexUnavailable,
        };
        return switch (reply.status) {
            200, 400, 409, 413, 422 => reply,
            401 => error.Unauthorized,
            else => error.IndexUnavailable,
        };
    }

    fn parse(comptime T: type, arena: Allocator, reply: Reply) Error!T {
        if (reply.status != 200) return error.IndexRejected;
        return json.parseFromSliceLeaky(T, arena, reply.body, .{
            .ignore_unknown_fields = true,
            .allocate = .alloc_always,
        }) catch |err| switch (err) {
            error.OutOfMemory => error.OutOfMemory,
            else => error.MalformedReply,
        };
    }

    pub fn status(self: Client, arena: Allocator) Error!Status {
        const reply = try self.call(arena, .{ .method = .GET, .path = "/index/status" });
        const wire = try parse(struct {
            build_id: []const u8,
            snapshot_watermark: u64,
            watermark: u64,
            import_complete: bool,
            ready: bool,
            source_observation_revision: u64,
            moderation: struct { source_observed_at: ?u64 = null } = .{},
        }, arena, reply);
        return .{
            .build_id = wire.build_id,
            .snapshot_watermark = wire.snapshot_watermark,
            .watermark = wire.watermark,
            .import_complete = wire.import_complete,
            .ready = wire.ready,
            .source_observation_revision = wire.source_observation_revision,
            .policy_observed_at = wire.moderation.source_observed_at,
        };
    }

    pub fn sourceState(self: Client, arena: Allocator, dids: []const []const u8) Error!State {
        std.debug.assert(dids.len <= max_batch);
        const body = try json.Stringify.valueAlloc(arena, .{ .dids = dids }, .{});
        const reply = try self.call(arena, .{ .method = .POST, .path = "/admin/source-state", .body = body });
        const wire = try parse(struct {
            source_cursor: json.Value,
            source_observation_revision: u64,
            actors: []const ExpectedDocument,
        }, arena, reply);
        if (wire.actors.len != dids.len) return error.MalformedReply;
        for (wire.actors, dids) |document, did| {
            if (!std.mem.eql(u8, document.did, did)) return error.MalformedReply;
            if (document.document_sha256) |hash| if (hash.len != 64) return error.MalformedReply;
        }
        return .{
            .cursor = try json.Stringify.valueAlloc(arena, wire.source_cursor, .{}),
            .revision = wire.source_observation_revision,
            .documents = wire.actors,
        };
    }

    pub fn sourcePage(self: Client, arena: Allocator, after_did: []const u8, limit: usize) Error!Page {
        std.debug.assert(limit >= 1 and limit <= max_batch);
        const body = try json.Stringify.valueAlloc(arena, .{ .after_did = after_did, .limit = limit }, .{});
        const reply = try self.call(arena, .{ .method = .POST, .path = "/admin/source-page", .body = body });
        const wire = try parse(struct {
            dids: []const []const u8,
            source_cursor: json.Value,
            source_observation_revision: u64,
        }, arena, reply);
        if (wire.dids.len > limit) return error.MalformedReply;
        var last = after_did;
        for (wire.dids) |did| {
            if (std.mem.order(u8, did, last) != .gt) return error.MalformedReply;
            last = did;
        }
        return .{
            .dids = wire.dids,
            .cursor = try json.Stringify.valueAlloc(arena, wire.source_cursor, .{}),
            .revision = wire.source_observation_revision,
        };
    }

    pub fn reconcile(self: Client, arena: Allocator, body: []const u8) Error!Outcome {
        const reply = try self.call(arena, .{ .method = .POST, .path = "/admin/source-reconcile", .body = body });
        return switch (reply.status) {
            200 => .{ .committed = try parse(Receipt, arena, reply) },
            409 => .conflict,
            else => .{ .invalid = reply.body },
        };
    }
};

/// The loopback admin listener over zat's HTTP client. Every request has an
/// explicit response deadline: a reconcile commits up to 500 documents before
/// it answers, which the transport's 10 s default does not cover.
pub const Http = struct {
    http: *zat.HttpTransport,
    base_url: []const u8,
    /// The full header value, "Bearer <ADMIN_SECRET>".
    authorization: []const u8,
    head_deadline_ns: u64 = 30 * std.time.ns_per_s,
    deadline_ns: u64 = 60 * std.time.ns_per_s,

    pub fn transport(self: *Http) Transport {
        return .{ .ptr = self, .send = send };
    }

    pub fn options(self: *const Http, url: []const u8, request: Request) zat.HttpTransport.FetchOptions {
        return .{
            .url = url,
            .method = request.method,
            .payload = request.body,
            .content_type = if (request.body != null) "application/json" else null,
            .authorization = self.authorization,
            .head_deadline_ns = self.head_deadline_ns,
            .deadline_ns = self.deadline_ns,
            .max_response_size = 4 * 1024 * 1024,
            .on_redirect = .refuse,
        };
    }

    fn send(ptr: *anyopaque, arena: Allocator, request: Request) anyerror!Reply {
        const self: *Http = @ptrCast(@alignCast(ptr));
        const url = try std.fmt.allocPrint(arena, "{s}{s}", .{ self.base_url, request.path });
        var result = try self.http.fetch(self.options(url, request));
        defer result.deinit(self.http.allocator);
        return .{ .status = @backingInt(result.status), .body = try arena.dupe(u8, result.body) };
    }
};

const testing = std.testing;
const hash_a: [64]u8 = @splat('a');
const hash_c: [64]u8 = @splat('c');

fn testCursor() Cursor {
    return .{ .source_id = "slot-a", .build_id = "b1", .snapshot_watermark = 100, .position = 42, .through = 150 };
}

test "cursor round-trips and keeps the fields /index/status reads" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var cursor = testCursor();
    cursor.reconciliation = .{ .round = 3, .phase = .backend, .after_did = "did:plc:m", .round_started_at = 140, .last_completed_at = 130, .last_completed_started_at = 120 };
    const text = try encodeCursor(arena, cursor);
    const value = try json.parseFromSliceLeaky(json.Value, arena, text, .{});
    try testing.expectEqual(@as(i64, 1), value.object.get("schema_version").?.integer);
    try testing.expectEqual(json.Value.null, value.object.get("cycle").?);
    try testing.expectEqual(@as(i64, 150), value.object.get("through").?.integer);
    try testing.expectEqual(@as(i64, 130), value.object.get("reconciliation").?.object.get("last_completed_at").?.integer);

    const parsed = parseCursor(arena, text).?;
    try testing.expectEqual(@as(u64, 42), parsed.position);
    try testing.expectEqual(Phase.backend, parsed.reconciliation.phase);
    try testing.expectEqualStrings("did:plc:m", parsed.reconciliation.after_did);
    try testing.expectEqualStrings(text, try encodeCursor(arena, parsed));
}

test "cursors written by another controller are not adopted" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    try testing.expect(parseCursor(arena, "null") == null);
    try testing.expect(parseCursor(arena, "{\"schema_version\":1,\"source_policy\":\"turso-authoritative-observations-v2\",\"through\":5}") == null);
    const other = try std.mem.replaceOwned(u8, arena, try encodeCursor(arena, testCursor()), source_policy, "store-change-log-v9");
    try testing.expect(parseCursor(arena, other) == null);
}

test "reconcile body carries every field the server parses" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const request: Reconcile = .{
        .entries = &.{
            .{ .did = "did:plc:a", .version = 7, .record = .{
                .handle = "a.test",
                .display_name = "Quote \" and \n newline",
                .avatar = "bafkavatar",
                .labels = &.{
                    .{ .source = "did:plc:mod", .uri = "did:plc:a", .value = "x", .created = "2026-11-10T00:00:00Z", .cid = "bafylabel", .expires = "2027-01-01T00:00:00.000Z" },
                    .{ .source = "did:plc:mod", .uri = "did:plc:a", .value = "withdrawn", .created = "2026-11-10T00:00:00Z", .negated = true },
                    .{ .source = "did:plc:a", .uri = "at://did:plc:a/app.bsky.actor.profile/self", .value = "self", .created = "2026-11-10T00:00:00Z", .expires = "next tuesday" },
                },
                .associated = "{\"chat\":{\"allowIncoming\":\"all\"}}",
                .created_at = "2026-11-10T00:00:00Z",
                .pds = "https://pds.test",
                .quality_score = 16777217.0,
                .hidden = true,
                .followers_count = 3,
                .follows_count = std.math.maxInt(u64),
                .posts_count = 9,
            } },
            .{ .did = "did:plc:b", .version = 9, .record = null },
        },
        .expected = &.{
            .{ .did = "did:plc:a", .document_sha256 = &hash_a },
            .{ .did = "did:plc:b", .document_sha256 = null },
        },
        .cursor = testCursor(),
        .expected_cursor = "null",
        .expected_revision = 4,
        .watermark = 150,
        .policy = .{ .policy = .{
            .overrides = &.{.{ .did = "did:plc:a", .action = .hide }},
            .blocked_domains = &.{"spam.test"},
        }, .observed_at = 149 },
    };
    const body = try encodeReconcile(arena, request);
    const value = try json.parseFromSliceLeaky(json.Value, arena, body, .{});
    const root = value.object;

    const live = root.get("actors").?.array.items[0].object;
    try testing.expectEqualStrings("Quote \" and \n newline", live.get("display_name").?.string);
    try testing.expectEqualStrings("bafkavatar", live.get("avatar_url").?.string);
    try testing.expectEqual(@as(i64, 7), live.get("version").?.integer);
    try testing.expectEqual(true, live.get("hidden").?.bool);
    try testing.expectEqual(false, live.get("deleted").?.bool);
    const labels = live.get("labels").?.array.items;
    try testing.expectEqual(@as(usize, 2), labels.len);
    try testing.expectEqualStrings("did:plc:mod", labels[0].object.get("src").?.string);
    try testing.expectEqualStrings("x", labels[0].object.get("val").?.string);
    try testing.expectEqualStrings("2026-11-10T00:00:00Z", labels[0].object.get("cts").?.string);
    try testing.expectEqualStrings("bafylabel", labels[0].object.get("cid").?.string);
    try testing.expectEqualStrings("2027-01-01T00:00:00.000Z", labels[0].object.get("exp").?.string);
    try testing.expectEqualStrings("self", labels[1].object.get("val").?.string);
    try testing.expect(labels[1].object.get("exp") == null and labels[1].object.get("cid") == null);
    try testing.expect(live.get("associated").?.object.get("chat") != null);
    try testing.expectEqual(@as(i64, std.math.maxInt(i64)), live.get("follows_count").?.integer);
    try testing.expectEqual(@as(i64, 0), live.get("last_activity_at").?.integer);
    const score: f32 = switch (live.get("quality_score").?) {
        .integer => |n| @floatFromInt(n),
        .float => |f| @floatCast(f),
        else => return error.TestUnexpectedResult,
    };
    try testing.expectEqual(@as(f32, 16777217.0), score);

    const tombstone = root.get("actors").?.array.items[1].object;
    try testing.expectEqual(true, tombstone.get("deleted").?.bool);
    try testing.expectEqualStrings("", tombstone.get("handle").?.string);
    try testing.expectEqual(@as(i64, 9), tombstone.get("version").?.integer);

    try testing.expectEqual(json.Value.null, root.get("expected_documents").?.array.items[1].object.get("document_sha256").?);
    try testing.expectEqual(json.Value.null, root.get("expected_source_cursor").?);
    try testing.expectEqual(@as(i64, 4), root.get("expected_observation_revision").?.integer);
    try testing.expectEqual(@as(i64, 150), root.get("watermark").?.integer);
    try testing.expectEqual(@as(i64, 42), root.get("source_cursor").?.object.get("position").?.integer);
    const policy = root.get("policy_snapshot").?.object;
    try testing.expectEqual(true, policy.get("complete").?.bool);
    try testing.expectEqual(@as(i64, 149), policy.get("source_observed_at").?.integer);
    try testing.expectEqualStrings("hide", policy.get("overrides").?.array.items[0].object.get("action").?.string);
    try testing.expectEqualStrings("spam.test", policy.get("blocked_domains").?.array.items[0].string);

    const batch_id = root.get("batch_id").?.string;
    try testing.expectEqual(@as(usize, 64), batch_id.len);
    try testing.expectEqualStrings(batch_id, (try json.parseFromSliceLeaky(json.Value, arena, try encodeReconcile(arena, request), .{})).object.get("batch_id").?.string);
    var changed = request;
    changed.expected_revision = 5;
    const other = (try json.parseFromSliceLeaky(json.Value, arena, try encodeReconcile(arena, changed), .{})).object.get("batch_id").?.string;
    try testing.expect(!std.mem.eql(u8, batch_id, other));
}

test "optional reconcile fields are omitted, not null" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const body = try encodeReconcile(arena, .{
        .entries = &.{},
        .expected = &.{},
        .cursor = testCursor(),
        .expected_cursor = try encodeCursor(arena, testCursor()),
        .expected_revision = 1,
    });
    const root = (try json.parseFromSliceLeaky(json.Value, arena, body, .{})).object;
    try testing.expect(root.get("watermark") == null);
    try testing.expect(root.get("policy_snapshot") == null);
    try testing.expectEqual(@as(usize, 0), root.get("actors").?.array.items.len);
}

test "local validation matches the server's admission rules" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    try testing.expect(validDid("did:plc:abc"));
    try testing.expect(!validDid("plc:abc"));
    try testing.expect(!validDid("did:plc:a b"));
    try testing.expect(!validDid("did:plc:a|b"));
    try testing.expect(!validDid("did:" ++ &@as([2045]u8, @splat('x'))));

    const good: source.Actor = .{ .handle = "a.test" };
    try testing.expect(rejection(arena, good) == null);
    var bad = good;
    bad.quality_score = std.math.inf(f32);
    try testing.expect(rejection(arena, bad) != null);
    bad = good;
    bad.associated = "[]";
    try testing.expect(rejection(arena, bad) != null);
    bad = good;
    bad.associated = "{";
    try testing.expect(rejection(arena, bad) != null);
    bad = good;
    bad.display_name = &@as([65536]u8, @splat('x'));
    try testing.expect(rejection(arena, bad) != null);
    bad = good;
    bad.handle = "\xff.test";
    try testing.expect(rejection(arena, bad) != null);
    bad = good;
    bad.labels = &.{.{ .source = "did:plc:mod", .uri = "\xff", .value = "x", .created = "" }};
    try testing.expect(rejection(arena, bad) != null);
}

test "only expiries the server and the store read alike are forwarded" {
    for ([_][]const u8{ "2027-01-01T00:00:00Z", "2027-01-01T00:00:00.123Z", "2024-02-29T23:59:59+05:30", "2027-12-31T00:00:00.000000-08:00" }) |text| {
        try testing.expect(supportedExpiry(text));
    }
    for ([_][]const u8{
        "",                      "2027-01-01",               "2027-01-01t00:00:00Z",  "2027-01-01T00:00:60Z",
        "2027-02-29T00:00:00Z",  "2027-13-01T00:00:00Z",     "2027-01-01T24:00:00Z",  "2027-01-01T00:00:00",
        "2027-01-01T00:00:00.Z", "2027-01-01T00:00:00+0530", "2027-01-01T00:00:00Zx", "0999-01-01T00:00:00Z",
        "1793000000",
    }) |text| {
        try testing.expect(!supportedExpiry(text));
    }
}

const Canned = struct {
    reply: Reply,
    seen: ?Request = null,

    fn transport(self: *Canned) Transport {
        return .{ .ptr = self, .send = send };
    }

    fn send(ptr: *anyopaque, arena: Allocator, request: Request) anyerror!Reply {
        const self: *Canned = @ptrCast(@alignCast(ptr));
        self.seen = .{ .method = request.method, .path = request.path, .body = if (request.body) |body| try arena.dupe(u8, body) else null };
        return self.reply;
    }
};

test "client decodes state, page, receipt and status replies" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var canned: Canned = .{ .reply = .{ .status = 200, .body = "{\"source_cursor\":{\"b\":1,\"a\":[2]},\"source_observation_revision\":7," ++
        "\"actors\":[{\"did\":\"did:plc:a\",\"document_sha256\":null},{\"did\":\"did:plc:b\",\"document_sha256\":\"" ++ &hash_c ++ "\"}]}" } };
    const client: Client = .{ .transport = canned.transport() };
    const state = try client.sourceState(arena, &.{ "did:plc:a", "did:plc:b" });
    try testing.expectEqualStrings("/admin/source-state", canned.seen.?.path);
    try testing.expectEqualStrings("{\"dids\":[\"did:plc:a\",\"did:plc:b\"]}", canned.seen.?.body.?);
    try testing.expectEqualStrings("{\"b\":1,\"a\":[2]}", state.cursor);
    try testing.expectEqual(@as(u64, 7), state.revision);
    try testing.expect(state.documents[0].document_sha256 == null);
    try testing.expectEqualStrings(&hash_c, state.documents[1].document_sha256.?);

    canned.reply.body = "{\"dids\":[\"did:plc:b\",\"did:plc:c\"],\"source_cursor\":null,\"source_observation_revision\":0}";
    const page = try client.sourcePage(arena, "did:plc:a", 2);
    try testing.expectEqualStrings("{\"after_did\":\"did:plc:a\",\"limit\":2}", canned.seen.?.body.?);
    try testing.expectEqualStrings("null", page.cursor);
    try testing.expectEqual(@as(usize, 2), page.dids.len);

    canned.reply.body = "{\"dids\":[\"did:plc:c\",\"did:plc:b\"],\"source_cursor\":null,\"source_observation_revision\":0}";
    try testing.expectError(error.MalformedReply, client.sourcePage(arena, "", 2));

    canned.reply.body = "{\"applied\":2,\"replayed\":1,\"stale\":0,\"watermark\":150,\"opstamp\":9,\"source_cursor\":{},\"source_observation_revision\":8}";
    const outcome = try client.reconcile(arena, "{}");
    try testing.expectEqual(@as(u64, 2), outcome.committed.applied);
    try testing.expectEqual(@as(u64, 8), outcome.committed.source_observation_revision);

    canned.reply.body = "{\"build_id\":\"b1\",\"snapshot_watermark\":100,\"watermark\":150,\"import_complete\":true,\"ready\":false," ++
        "\"source_observation_revision\":8,\"moderation\":{\"source_observed_at\":149,\"enabled\":true},\"engine\":\"tantivy\"}";
    const status = try client.status(arena);
    try testing.expectEqual(std.http.Method.GET, canned.seen.?.method);
    try testing.expectEqualStrings("b1", status.build_id);
    try testing.expectEqual(@as(?u64, 149), status.policy_observed_at);
}

test "client separates conflict, refusal, authorization and outage" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var canned: Canned = .{ .reply = .{ .status = 409, .body = "{\"error\":\"SourceVersionConflict\"}" } };
    const client: Client = .{ .transport = canned.transport() };
    try testing.expectEqual(Outcome.conflict, try client.reconcile(arena, "{}"));
    canned.reply = .{ .status = 400, .body = "{\"error\":\"invalid did\"}" };
    try testing.expectEqualStrings("{\"error\":\"invalid did\"}", (try client.reconcile(arena, "{}")).invalid);
    try testing.expectError(error.IndexRejected, client.sourceState(arena, &.{}));
    canned.reply = .{ .status = 401, .body = "{}" };
    try testing.expectError(error.Unauthorized, client.reconcile(arena, "{}"));
    canned.reply = .{ .status = 503, .body = "{\"error\":\"not ready\"}" };
    try testing.expectError(error.IndexUnavailable, client.reconcile(arena, "{}"));
    try testing.expectError(error.IndexUnavailable, client.status(arena));
}

test "http requests carry an explicit deadline and refuse redirects" {
    var http: zat.HttpTransport = undefined;
    const endpoint: Http = .{ .http = &http, .base_url = "http://127.0.0.1:8081", .authorization = "Bearer x" };
    const post = endpoint.options("http://127.0.0.1:8081/admin/source-reconcile", .{ .method = .POST, .path = "/admin/source-reconcile", .body = "{}" });
    try testing.expectEqualStrings("application/json", post.content_type.?);
    try testing.expectEqualStrings("Bearer x", post.authorization.?);
    try testing.expect(post.head_deadline_ns.? > 10 * std.time.ns_per_s);
    try testing.expect(post.deadline_ns != null);
    try testing.expect(post.on_redirect == .refuse);
    const get = endpoint.options("http://127.0.0.1:8081/index/status", .{ .method = .GET, .path = "/index/status" });
    try testing.expect(get.content_type == null and get.payload == null);
}
