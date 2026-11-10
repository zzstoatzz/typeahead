//! Value encodings. Every value starts with a format byte so a later layout
//! can be read beside this one.
//!
//! Actor record, format 1:
//!
//! - format byte, then a flags byte: status in bits 0-1, hidden in bit 2,
//!   policy override in bits 3-4, profile source in bits 5-7;
//! - eight strings, each a LEB128 length and its bytes: handle, display name,
//!   avatar CID, description, banner CID, PDS, created-at, associated;
//! - LEB128 followers, follows and posts counts;
//! - the quality score as four little-endian IEEE-754 bytes;
//! - seven LEB128 times in unix seconds: last activity, updated, profile
//!   checked, identity resolved, deleted, identity due, refresh due;
//! - a LEB128 label count, then per label a flags byte (bit 0 negated), a
//!   LEB128 version and six strings: source, URI, value, created, CID, expiry.
//!
//! `associated` is the one field the store does not interpret: the caller's
//! canonical JSON object text, empty when the object is empty.

const std = @import("std");

pub const actor_format = 1;
pub const label_format = 1;
pub const queue_format = 1;
pub const policy_format = 1;

pub const Error = error{ CorruptRecord, UnsupportedRecordFormat };

pub const Status = enum(u2) { live, deactivated, deleted };
pub const Override = enum(u2) { none, show, hide };
pub const ProfileSource = enum(u3) { none, bsky, blacksky, pds };

pub const Label = struct {
    source: []const u8,
    uri: []const u8,
    value: []const u8,
    created: []const u8,
    cid: []const u8 = "",
    expires: []const u8 = "",
    negated: bool = false,
    version: u64 = 0,

    pub fn eql(a: Label, b: Label) bool {
        return a.negated == b.negated and a.version == b.version and
            std.mem.eql(u8, a.source, b.source) and std.mem.eql(u8, a.uri, b.uri) and
            std.mem.eql(u8, a.value, b.value) and std.mem.eql(u8, a.created, b.created) and
            std.mem.eql(u8, a.cid, b.cid) and std.mem.eql(u8, a.expires, b.expires);
    }
};

pub const Actor = struct {
    status: Status = .live,
    hidden: bool = false,
    override: Override = .none,
    profile_source: ProfileSource = .none,
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar: []const u8 = "",
    description: []const u8 = "",
    banner: []const u8 = "",
    pds: []const u8 = "",
    created_at: []const u8 = "",
    associated: []const u8 = "",
    followers_count: u64 = 0,
    follows_count: u64 = 0,
    posts_count: u64 = 0,
    quality_score: f32 = 0,
    last_activity_at: u64 = 0,
    updated_at: u64 = 0,
    profile_checked_at: u64 = 0,
    identity_resolved_at: u64 = 0,
    deleted_at: u64 = 0,
    identity_due: u64 = 0,
    refresh_due: u64 = 0,
    labels: []const Label = &.{},

    const strings = [_][]const u8{ "handle", "display_name", "avatar", "description", "banner", "pds", "created_at", "associated" };
    const counts = [_][]const u8{ "followers_count", "follows_count", "posts_count" };
    const times = [_][]const u8{ "last_activity_at", "updated_at", "profile_checked_at", "identity_resolved_at", "deleted_at", "identity_due", "refresh_due" };

    pub fn live(self: Actor) bool {
        return self.status == .live;
    }

    /// What a search could return: the rule behind the quality walk.
    pub fn ranked(self: Actor) bool {
        return self.live() and !self.hidden and self.handle.len > 0 and self.quality_score > 0;
    }

    /// Equality of everything a consumer of the change log reads. Scheduling
    /// and audit fields (the seven times and the override copy) are left out:
    /// a lease or an hourly activity stamp is not a change to the actor.
    pub fn contentEql(a: Actor, b: Actor) bool {
        if (a.status != b.status or a.hidden != b.hidden or a.profile_source != b.profile_source) return false;
        inline for (strings) |name| if (!std.mem.eql(u8, @field(a, name), @field(b, name))) return false;
        inline for (counts) |name| if (@field(a, name) != @field(b, name)) return false;
        if (@as(u32, @bitCast(a.quality_score)) != @as(u32, @bitCast(b.quality_score))) return false;
        if (a.labels.len != b.labels.len) return false;
        for (a.labels, b.labels) |x, y| if (!x.eql(y)) return false;
        return true;
    }

    pub fn eql(a: Actor, b: Actor) bool {
        if (!a.contentEql(b) or a.override != b.override) return false;
        inline for (times) |name| if (@field(a, name) != @field(b, name)) return false;
        return true;
    }

    pub fn encode(self: Actor, gpa: std.mem.Allocator, out: *std.ArrayList(u8)) !void {
        out.clearRetainingCapacity();
        try out.append(gpa, actor_format);
        try out.append(gpa, @as(u8, @backingInt(self.status)) | @as(u8, @intFromBool(self.hidden)) << 2 |
            @as(u8, @backingInt(self.override)) << 3 | @as(u8, @backingInt(self.profile_source)) << 5);
        inline for (strings) |name| try putBytes(gpa, out, @field(self, name));
        inline for (counts) |name| try putInt(gpa, out, @field(self, name));
        try out.appendSlice(gpa, &std.mem.toBytes(std.mem.nativeToLittle(u32, @bitCast(self.quality_score))));
        inline for (times) |name| try putInt(gpa, out, @field(self, name));
        try putInt(gpa, out, self.labels.len);
        for (self.labels) |label| {
            try out.append(gpa, @intFromBool(label.negated));
            try putInt(gpa, out, label.version);
            inline for (.{ "source", "uri", "value", "created", "cid", "expires" }) |name| try putBytes(gpa, out, @field(label, name));
        }
    }

    /// Strings and labels borrow from `bytes`; `arena` holds only the label list.
    pub fn decode(arena: std.mem.Allocator, bytes: []const u8) (Error || std.mem.Allocator.Error)!Actor {
        var in: Input = .{ .bytes = bytes };
        if (try in.byte() != actor_format) return error.UnsupportedRecordFormat;
        const flags = try in.byte();
        var actor: Actor = .{
            .status = std.enums.fromInt(Status, flags & 3) orelse return error.CorruptRecord,
            .hidden = flags & 4 != 0,
            .override = std.enums.fromInt(Override, (flags >> 3) & 3) orelse return error.CorruptRecord,
            .profile_source = std.enums.fromInt(ProfileSource, flags >> 5) orelse return error.CorruptRecord,
        };
        inline for (strings) |name| @field(actor, name) = try in.slice();
        inline for (counts) |name| @field(actor, name) = try in.int();
        actor.quality_score = @bitCast(std.mem.readInt(u32, (try in.take(4))[0..4], .little));
        inline for (times) |name| @field(actor, name) = try in.int();
        const count = try in.int();
        if (count > in.left()) return error.CorruptRecord;
        const labels = try arena.alloc(Label, count);
        for (labels) |*label| {
            const label_flags = try in.byte();
            if (label_flags > 1) return error.CorruptRecord;
            label.* = .{ .source = "", .uri = "", .value = "", .created = "", .negated = label_flags == 1, .version = try in.int() };
            inline for (.{ "source", "uri", "value", "created", "cid", "expires" }) |name| @field(label, name) = try in.slice();
        }
        actor.labels = labels;
        try in.end();
        return actor;
    }
};

pub const IdentityReason = enum(u8) { identity, account, discovered, plc };

/// The value of an identity queue entry. `generation` rises with each new
/// signal, so a completion for an older one cannot remove the entry.
pub const QueueState = struct {
    generation: u64 = 1,
    attempts: u64 = 0,
    reason: IdentityReason,
    enqueued_at: u64,
    last_error: []const u8 = "",

    pub fn encode(self: QueueState, gpa: std.mem.Allocator, out: *std.ArrayList(u8)) !void {
        out.clearRetainingCapacity();
        try out.append(gpa, queue_format);
        try out.append(gpa, @backingInt(self.reason));
        try putInt(gpa, out, self.generation);
        try putInt(gpa, out, self.attempts);
        try putInt(gpa, out, self.enqueued_at);
        try putBytes(gpa, out, self.last_error);
    }

    pub fn decode(bytes: []const u8) Error!QueueState {
        var in: Input = .{ .bytes = bytes };
        if (try in.byte() != queue_format) return error.UnsupportedRecordFormat;
        const state: QueueState = .{
            .reason = std.enums.fromInt(IdentityReason, try in.byte()) orelse return error.CorruptRecord,
            .generation = try in.int(),
            .attempts = try in.int(),
            .enqueued_at = try in.int(),
            .last_error = try in.slice(),
        };
        try in.end();
        return state;
    }
};

/// The value of a label range entry. `sequence` is the label stream's, 0 for
/// an answer obtained by asking the labeler; a negation is kept as an entry
/// so a delayed assertion cannot bring a withdrawn label back.
pub const StoredLabel = struct {
    sequence: u64,
    negated: bool,
    created: []const u8,
    cid: []const u8 = "",
    expires: []const u8 = "",

    pub fn encode(self: StoredLabel, gpa: std.mem.Allocator, out: *std.ArrayList(u8)) !void {
        out.clearRetainingCapacity();
        try out.append(gpa, label_format);
        try out.append(gpa, @intFromBool(self.negated));
        try putInt(gpa, out, self.sequence);
        try putBytes(gpa, out, self.created);
        try putBytes(gpa, out, self.cid);
        try putBytes(gpa, out, self.expires);
    }

    pub fn decode(bytes: []const u8) Error!StoredLabel {
        var in: Input = .{ .bytes = bytes };
        if (try in.byte() != label_format) return error.UnsupportedRecordFormat;
        const negated = try in.byte();
        if (negated > 1) return error.CorruptRecord;
        const label: StoredLabel = .{
            .negated = negated == 1,
            .sequence = try in.int(),
            .created = try in.slice(),
            .cid = try in.slice(),
            .expires = try in.slice(),
        };
        try in.end();
        return label;
    }
};

/// The value of a policy entry: a per-DID override carries its action, a
/// blocked domain carries `.none`.
pub const PolicyEntry = struct {
    action: Override = .none,
    created_at: u64,
    reason: []const u8 = "",

    pub fn encode(self: PolicyEntry, gpa: std.mem.Allocator, out: *std.ArrayList(u8)) !void {
        out.clearRetainingCapacity();
        try out.append(gpa, policy_format);
        try out.append(gpa, @backingInt(self.action));
        try putInt(gpa, out, self.created_at);
        try putBytes(gpa, out, self.reason);
    }

    pub fn decode(bytes: []const u8) Error!PolicyEntry {
        var in: Input = .{ .bytes = bytes };
        if (try in.byte() != policy_format) return error.UnsupportedRecordFormat;
        const entry: PolicyEntry = .{
            .action = std.enums.fromInt(Override, try in.byte()) orelse return error.CorruptRecord,
            .created_at = try in.int(),
            .reason = try in.slice(),
        };
        try in.end();
        return entry;
    }
};

pub fn encodeU64(value: u64) [8]u8 {
    var out: [8]u8 = undefined;
    std.mem.writeInt(u64, &out, value, .big);
    return out;
}

pub fn decodeU64(bytes: []const u8) Error!u64 {
    if (bytes.len != 8) return error.CorruptRecord;
    return std.mem.readInt(u64, bytes[0..8], .big);
}

fn putInt(gpa: std.mem.Allocator, out: *std.ArrayList(u8), value: u64) !void {
    var rest = value;
    while (rest >= 0x80) : (rest >>= 7) try out.append(gpa, @as(u8, @truncate(rest)) | 0x80);
    try out.append(gpa, @truncate(rest));
}

fn putBytes(gpa: std.mem.Allocator, out: *std.ArrayList(u8), bytes: []const u8) !void {
    try putInt(gpa, out, bytes.len);
    try out.appendSlice(gpa, bytes);
}

const Input = struct {
    bytes: []const u8,
    at: usize = 0,

    fn left(self: *const Input) usize {
        return self.bytes.len - self.at;
    }

    fn take(self: *Input, len: usize) Error![]const u8 {
        if (len > self.left()) return error.CorruptRecord;
        defer self.at += len;
        return self.bytes[self.at..][0..len];
    }

    fn byte(self: *Input) Error!u8 {
        return (try self.take(1))[0];
    }

    fn int(self: *Input) Error!u64 {
        var value: u64 = 0;
        for (0..10) |i| {
            const b = try self.byte();
            const bits: u64 = b & 0x7f;
            if (i == 9 and bits > 1) return error.CorruptRecord;
            value |= bits << @intCast(7 * i);
            if (b & 0x80 == 0) {
                if (i > 0 and bits == 0) return error.CorruptRecord;
                return value;
            }
        }
        return error.CorruptRecord;
    }

    fn slice(self: *Input) Error![]const u8 {
        const len = try self.int();
        if (len > self.left()) return error.CorruptRecord;
        return self.take(@intCast(len));
    }

    fn end(self: *const Input) Error!void {
        if (self.left() != 0) return error.CorruptRecord;
    }
};

const testing = std.testing;

const full: Actor = .{
    .status = .deactivated,
    .hidden = true,
    .override = .hide,
    .profile_source = .blacksky,
    .handle = "Alice.Example.com",
    .display_name = "alice \u{1F98A} (she/her)",
    .avatar = "bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku",
    .description = "line one\nline two with \x00 a NUL",
    .banner = "bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq",
    .pds = "https://morel.us-east.host.bsky.network",
    .created_at = "2023-04-12T08:15:30.123Z",
    .associated = "{\"chat\":{\"allowIncoming\":\"following\"},\"lists\":3}",
    .followers_count = 308_412,
    .follows_count = 127,
    .posts_count = std.math.maxInt(u64),
    .quality_score = 812.37,
    .last_activity_at = 1_794_000_000,
    .updated_at = 1_794_000_001,
    .profile_checked_at = 1_794_000_002,
    .identity_resolved_at = 1_794_000_003,
    .deleted_at = 1_794_000_004,
    .identity_due = 1_794_000_005,
    .refresh_due = 1_794_000_006,
    .labels = &.{
        .{ .source = "did:plc:ar7c4by46qjdydhdevvrndac", .uri = "did:plc:abc", .value = "!hide", .created = "2026-01-02T03:04:05.000Z", .cid = "bafy", .expires = "2027-01-02T03:04:05Z", .version = 1 },
        .{ .source = "did:plc:abc", .uri = "at://did:plc:abc/app.bsky.actor.profile/self", .value = "!no-unauthenticated", .created = "1970-01-01T00:00:00.000Z", .negated = true },
    },
};

test "an actor record round trips every field" {
    const gpa = testing.allocator;
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    var out: std.ArrayList(u8) = .empty;
    defer out.deinit(gpa);
    try full.encode(gpa, &out);
    const back = try Actor.decode(arena.allocator(), out.items);
    try testing.expect(full.eql(back));
    try testing.expectEqualDeep(full, back);

    try (Actor{}).encode(gpa, &out);
    try testing.expectEqual(@as(usize, 25), out.items.len);
    try testing.expectEqualDeep(Actor{}, try Actor.decode(arena.allocator(), out.items));
}

test "every enum value and every single-field difference survives the encoding" {
    const gpa = testing.allocator;
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    var out: std.ArrayList(u8) = .empty;
    defer out.deinit(gpa);
    for (std.enums.values(Status)) |status| for (std.enums.values(Override)) |override| for (std.enums.values(ProfileSource)) |source| for ([_]bool{ false, true }) |hidden| {
        const actor: Actor = .{ .status = status, .override = override, .profile_source = source, .hidden = hidden };
        try actor.encode(gpa, &out);
        try testing.expectEqualDeep(actor, try Actor.decode(arena.allocator(), out.items));
    };
    inline for (comptime std.meta.fieldNames(Actor)) |name| {
        var changed = full;
        switch (@FieldType(Actor, name)) {
            []const u8 => @field(changed, name) = "changed",
            u64 => @field(changed, name) = 7,
            f32 => changed.quality_score = 1.25,
            bool => changed.hidden = false,
            Status => changed.status = .deleted,
            Override => changed.override = .show,
            ProfileSource => changed.profile_source = .pds,
            []const Label => changed.labels = full.labels[0..1],
            else => @compileError("untested field type of " ++ name),
        }
        try testing.expect(!full.eql(changed));
        try changed.encode(gpa, &out);
        const back = try Actor.decode(arena.allocator(), out.items);
        try testing.expect(changed.eql(back));
        try testing.expect(!full.eql(back));
    }
}

test "content equality ignores scheduling fields and nothing else" {
    inline for (comptime std.meta.fieldNames(Actor)) |name| {
        var changed = full;
        switch (@FieldType(Actor, name)) {
            []const u8 => @field(changed, name) = "changed",
            u64 => @field(changed, name) = 7,
            f32 => changed.quality_score = 1.25,
            bool => changed.hidden = false,
            Status => changed.status = .deleted,
            Override => changed.override = .show,
            ProfileSource => changed.profile_source = .pds,
            []const Label => changed.labels = full.labels[0..1],
            else => unreachable,
        }
        const scheduling = comptime blk: {
            for (Actor.times) |time| if (std.mem.eql(u8, time, name)) break :blk true;
            break :blk std.mem.eql(u8, name, "override");
        };
        try testing.expectEqual(scheduling, full.contentEql(changed));
    }
    var label_changed = full;
    label_changed.labels = &.{ full.labels[0], .{ .source = "did:plc:abc", .uri = full.labels[1].uri, .value = "!no-unauthenticated", .created = full.labels[1].created } };
    try testing.expect(!full.contentEql(label_changed));
}

test "a damaged or foreign actor record is refused" {
    const gpa = testing.allocator;
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    var out: std.ArrayList(u8) = .empty;
    defer out.deinit(gpa);
    try full.encode(gpa, &out);
    for (0..out.items.len) |len| {
        try testing.expectError(error.CorruptRecord, Actor.decode(arena.allocator(), out.items[0..len]));
    }
    try out.append(gpa, 0);
    try testing.expectError(error.CorruptRecord, Actor.decode(arena.allocator(), out.items));
    out.items[0] = actor_format + 1;
    try testing.expectError(error.UnsupportedRecordFormat, Actor.decode(arena.allocator(), out.items));
    try testing.expectError(error.CorruptRecord, Actor.decode(arena.allocator(), &.{ actor_format, 3 }));
    try testing.expectError(error.CorruptRecord, Actor.decode(arena.allocator(), &.{ actor_format, 0, 0x80, 0 }));
}

test "queue state, stored labels and policy entries round trip" {
    const gpa = testing.allocator;
    var out: std.ArrayList(u8) = .empty;
    defer out.deinit(gpa);

    for (std.enums.values(IdentityReason)) |reason| {
        const state: QueueState = .{ .generation = 300, .attempts = 12, .reason = reason, .enqueued_at = 1_794_000_000, .last_error = "HTTP 503" };
        try state.encode(gpa, &out);
        try testing.expectEqualDeep(state, try QueueState.decode(out.items));
    }
    try testing.expectError(error.CorruptRecord, QueueState.decode(out.items[0 .. out.items.len - 1]));
    try testing.expectError(error.CorruptRecord, QueueState.decode(&.{ queue_format, 9, 1, 0, 0, 0 }));

    const label: StoredLabel = .{ .sequence = 91_000_000, .negated = true, .created = "2026-01-02T03:04:05Z", .cid = "bafy", .expires = "2027-01-01T00:00:00Z" };
    try label.encode(gpa, &out);
    try testing.expectEqualDeep(label, try StoredLabel.decode(out.items));
    try testing.expectError(error.CorruptRecord, StoredLabel.decode(out.items[0 .. out.items.len - 1]));

    for (std.enums.values(Override)) |action| {
        const entry: PolicyEntry = .{ .action = action, .created_at = 1_794_000_000, .reason = "spam farm" };
        try entry.encode(gpa, &out);
        try testing.expectEqualDeep(entry, try PolicyEntry.decode(out.items));
    }
    try testing.expectError(error.UnsupportedRecordFormat, PolicyEntry.decode(&.{ policy_format + 1, 0, 0, 0 }));

    for ([_]u64{ 0, 1, 1 << 40, std.math.maxInt(u64) }) |value| try testing.expectEqual(value, try decodeU64(&encodeU64(value)));
    try testing.expectError(error.CorruptRecord, decodeU64("1234567"));
}

test "integers keep their value at every LEB128 length" {
    const gpa = testing.allocator;
    var out: std.ArrayList(u8) = .empty;
    defer out.deinit(gpa);
    var value: u64 = 0;
    for (0..65) |shift| {
        for ([_]u64{ value -| 1, value, value +| 1 }) |sample| {
            out.clearRetainingCapacity();
            try putInt(gpa, &out, sample);
            var in: Input = .{ .bytes = out.items };
            try testing.expectEqual(sample, try in.int());
            try in.end();
        }
        value = if (shift == 64) std.math.maxInt(u64) else @as(u64, 1) << @intCast(shift);
    }
    var overlong: Input = .{ .bytes = &.{ 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x02 } };
    try testing.expectError(error.CorruptRecord, overlong.int());
    var padded: Input = .{ .bytes = &.{ 0x81, 0x00 } };
    try testing.expectError(error.CorruptRecord, padded.int());
}

test "a typical actor record is smaller than its JSON document" {
    const gpa = testing.allocator;
    var out: std.ArrayList(u8) = .empty;
    defer out.deinit(gpa);
    const typical: Actor = .{
        .handle = "alice-example.bsky.social",
        .display_name = "Alice Example",
        .avatar = "bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku",
        .pds = "https://morel.us-east.host.bsky.network",
        .created_at = "2024-11-17T08:15:30.123Z",
        .followers_count = 212,
        .follows_count = 340,
        .posts_count = 1_250,
        .quality_score = 294.6,
        .profile_source = .bsky,
        .last_activity_at = 1_794_000_000,
        .updated_at = 1_794_000_000,
        .profile_checked_at = 1_794_000_000,
        .identity_resolved_at = 1_794_000_000,
        .refresh_due = 1_794_600_000,
    };
    try typical.encode(gpa, &out);
    const json =
        \\{"did":"did:plc:abcdefghijklmnopqrstuvwx","handle":"alice-example.bsky.social","display_name":"Alice Example","avatar_url":"bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku","labels":[],"associated":{},"created_at":"2024-11-17T08:15:30.123Z","pds":"https://morel.us-east.host.bsky.network","pds_host":"morel.us-east.host.bsky.network","quality_score":294.6,"version":1794000000,"deleted":false,"hidden":false,"followers_count":212,"follows_count":340,"posts_count":1250,"last_activity_at":1794000000}
    ;
    try testing.expectEqual(@as(usize, 208), out.items.len);
    try testing.expectEqual(@as(usize, 511), json.len);
}
