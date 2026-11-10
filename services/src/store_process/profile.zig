//! What a profile response says about its actor: a port of
//! `extractProfileFields`, `extractAvatarCid` and `cleanAssociated` in
//! `src/utils.ts`. The input is an appview `profileViewDetailed` (Bluesky's
//! or Blacksky's) or the object `fetchProfileFromPds` builds from a record.
//!
//! `hidden` is not computed here: the store derives it on every write from
//! the labels, the override and the blocked domains (`store/moderation.zig`).
//!
//! Where JavaScript's behaviour depends on a value having the wrong type,
//! this port takes the lexicon's type or nothing: a string field that is not
//! a string is empty, a label that is not an object is dropped, and a count
//! that is a string is read as zero but still counts as present.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const authority = @import("authority_score.zig");
const Allocator = std.mem.Allocator;
const Value = std.json.Value;
const Label = store_mod.record.Label;

pub const Fields = struct {
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar: []const u8 = "",
    description: []const u8 = "",
    banner: []const u8 = "",
    created_at: []const u8 = "",
    /// `JSON.stringify(cleanAssociated(...))`: `{}` when nothing is kept.
    associated: []const u8 = "{}",
    followers_count: u64 = 0,
    follows_count: u64 = 0,
    posts_count: u64 = 0,
    /// Zero when the response carried no count at all, so the store keeps
    /// the score it has.
    quality_score: f64 = 0,
    labels: []const Label = &.{},
};

fn field(profile: Value, name: []const u8) ?Value {
    return switch (profile) {
        .object => |object| object.get(name),
        else => null,
    };
}

fn text(profile: Value, name: []const u8) []const u8 {
    return switch (field(profile, name) orelse return "") {
        .string => |s| s,
        else => "",
    };
}

fn present(profile: Value, name: []const u8) bool {
    return switch (field(profile, name) orelse return false) {
        .null => false,
        else => true,
    };
}

/// `Number(value) || 0` for the types a count arrives as.
fn number(profile: Value, name: []const u8) f64 {
    const n: f64 = switch (field(profile, name) orelse return 0) {
        .integer => |i| @floatFromInt(i),
        .float => |f| f,
        .number_string => |s| std.fmt.parseFloat(f64, s) catch 0,
        .bool => |b| @floatFromInt(@intFromBool(b)),
        else => 0,
    };
    return if (std.math.isNan(n)) 0 else n;
}

fn count(n: f64) u64 {
    if (!(n >= 1)) return 0;
    return if (n >= 0x1p63) std.math.maxInt(i64) else @intFromFloat(@floor(n));
}

fn truthy(value: Value) bool {
    return switch (value) {
        .null => false,
        .bool => |b| b,
        .integer => |i| i != 0,
        .float => |f| f != 0 and !std.math.isNan(f),
        .number_string => true,
        .string => |s| s.len > 0,
        .array, .object => true,
    };
}

fn percentDecoded(arena: Allocator, raw: []const u8) ![]const u8 {
    if (std.mem.indexOfScalar(u8, raw, '%') == null) return raw;
    const out = try arena.alloc(u8, raw.len);
    var len: usize = 0;
    var at: usize = 0;
    while (at < raw.len) : (at += 1) {
        if (raw[at] != '%') {
            out[len] = raw[at];
        } else {
            if (at + 2 >= raw.len) return "";
            out[len] = std.fmt.parseInt(u8, raw[at + 1 .. at + 3], 16) catch return "";
            at += 2;
        }
        len += 1;
    }
    return if (std.unicode.utf8ValidateSlice(out[0..len])) out[0..len] else "";
}

/// The CID inside an avatar or banner value: a bare CID as it is, the `cid`
/// parameter of a getBlob URL, or the last path segment of a CDN URL without
/// its `@jpeg` suffix. A `cid` parameter `decodeURIComponent` would throw on
/// yields nothing.
pub fn blobCid(arena: Allocator, value: []const u8) ![]const u8 {
    if (value.len == 0) return "";
    if (std.mem.indexOfScalar(u8, value, '/') == null) return value;
    var from: usize = 0;
    while (std.mem.indexOfPos(u8, value, from, "cid=")) |at| : (from = at + 1) {
        if (at == 0 or (value[at - 1] != '?' and value[at - 1] != '&')) continue;
        const start = at + "cid=".len;
        const end = std.mem.indexOfScalarPos(u8, value, start, '&') orelse value.len;
        if (end == start) continue;
        return percentDecoded(arena, value[start..end]);
    }
    const segment = value[std.mem.lastIndexOfScalar(u8, value, '/').? + 1 ..];
    if (segment.len == 0) return "";
    if (std.mem.lastIndexOfScalar(u8, segment, '@')) |at| {
        const suffix = segment[at + 1 ..];
        const lower = for (suffix) |c| {
            if (c < 'a' or c > 'z') break false;
        } else true;
        if (at > 0 and suffix.len > 0 and lower) return segment[0..at];
    }
    return segment;
}

fn writeString(out: *std.ArrayList(u8), arena: Allocator, s: []const u8) !void {
    try out.append(arena, '"');
    for (s) |c| switch (c) {
        '"' => try out.appendSlice(arena, "\\\""),
        '\\' => try out.appendSlice(arena, "\\\\"),
        0x08 => try out.appendSlice(arena, "\\b"),
        0x0c => try out.appendSlice(arena, "\\f"),
        '\n' => try out.appendSlice(arena, "\\n"),
        '\r' => try out.appendSlice(arena, "\\r"),
        '\t' => try out.appendSlice(arena, "\\t"),
        else => if (c < 0x20) {
            var buf: [6]u8 = undefined;
            try out.appendSlice(arena, std.fmt.bufPrint(&buf, "\\u{x:0>4}", .{c}) catch unreachable);
        } else try out.append(arena, c),
    };
    try out.append(arena, '"');
}

/// `JSON.stringify` of a parsed value, for the shapes `associated` holds.
/// Keys keep the order they arrived in; JavaScript moves keys that are
/// array indices to the front, which no key of this object is.
fn stringify(out: *std.ArrayList(u8), arena: Allocator, value: Value) !void {
    switch (value) {
        .null => try out.appendSlice(arena, "null"),
        .bool => |b| try out.appendSlice(arena, if (b) "true" else "false"),
        .integer => |i| {
            var buf: [24]u8 = undefined;
            try out.appendSlice(arena, std.fmt.bufPrint(&buf, "{d}", .{i}) catch unreachable);
        },
        .float => |f| {
            var buf: [40]u8 = undefined;
            try out.appendSlice(arena, if (std.math.isFinite(f)) std.fmt.bufPrint(&buf, "{d}", .{f}) catch unreachable else "null");
        },
        .number_string => |s| try out.appendSlice(arena, s),
        .string => |s| try writeString(out, arena, s),
        .array => |list| {
            try out.append(arena, '[');
            for (list.items, 0..) |item, index| {
                if (index > 0) try out.append(arena, ',');
                try stringify(out, arena, item);
            }
            try out.append(arena, ']');
        },
        .object => |object| {
            try out.append(arena, '{');
            var first = true;
            var it = object.iterator();
            while (it.next()) |entry| {
                if (!first) try out.append(arena, ',');
                first = false;
                try writeString(out, arena, entry.key_ptr.*);
                try out.append(arena, ':');
                try stringify(out, arena, entry.value_ptr.*);
            }
            try out.append(arena, '}');
        },
    }
}

const associated_keys = [_][]const u8{ "lists", "feedgens", "starterPacks", "labeler", "chat", "activitySubscription" };

/// The truthy members among the six the typeahead shape carries, in that
/// order, as JSON text.
pub fn associated(arena: Allocator, profile: Value) ![]const u8 {
    const source = field(profile, "associated") orelse return "{}";
    if (source != .object) return "{}";
    var out: std.ArrayList(u8) = .empty;
    try out.append(arena, '{');
    for (associated_keys) |key| {
        const value = source.object.get(key) orelse continue;
        if (!truthy(value)) continue;
        if (out.items.len > 1) try out.append(arena, ',');
        try writeString(&out, arena, key);
        try out.append(arena, ':');
        try stringify(&out, arena, value);
    }
    try out.append(arena, '}');
    return out.items;
}

pub fn labels(arena: Allocator, profile: Value) ![]const Label {
    const source = field(profile, "labels") orelse return &.{};
    if (source != .array) return &.{};
    var out: std.ArrayList(Label) = .empty;
    for (source.array.items) |one| {
        if (one != .object) continue;
        try out.append(arena, .{
            .source = text(one, "src"),
            .uri = text(one, "uri"),
            .value = text(one, "val"),
            .created = text(one, "cts"),
            .cid = text(one, "cid"),
            .expires = text(one, "exp"),
            .negated = if (field(one, "neg")) |neg| truthy(neg) else false,
            .version = count(number(one, "ver")),
        });
    }
    return out.items;
}

/// `Date.parse` for the timestamps an appview sends. Text that is not an
/// RFC 3339 timestamp is unparsed here; JavaScript also reads other forms.
pub fn dateMs(value: []const u8) ?i64 {
    if (value.len >= 19 and std.mem.eql(u8, value[17..19], "60")) return null;
    return store_mod.time.parseMs(value);
}

/// Strings borrow from `profile`; the rest is allocated in `arena`.
pub fn extract(arena: Allocator, profile: Value, now_ms: i64) !Fields {
    const followers = number(profile, "followersCount");
    const follows = number(profile, "followsCount");
    const posts = number(profile, "postsCount");
    const created_at = text(profile, "createdAt");
    const has_aggregates = present(profile, "followersCount") or present(profile, "followsCount") or present(profile, "postsCount");
    return .{
        .handle = text(profile, "handle"),
        .display_name = text(profile, "displayName"),
        .avatar = try blobCid(arena, text(profile, "avatar")),
        .description = text(profile, "description"),
        .banner = try blobCid(arena, text(profile, "banner")),
        .created_at = created_at,
        .associated = try associated(arena, profile),
        .followers_count = count(followers),
        .follows_count = count(follows),
        .posts_count = count(posts),
        .quality_score = if (has_aggregates) authority.score(.{
            .followers = followers,
            .follows = follows,
            .posts = posts,
            .created_ms = dateMs(created_at),
            .now_ms = now_ms,
        }) else 0,
        .labels = try labels(arena, profile),
    };
}
