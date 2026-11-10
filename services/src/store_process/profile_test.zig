//! The profile mapping and the quality score against vectors the Worker's
//! own TypeScript produced: `testdata/profile_vectors.json`, written by
//! `bun scripts/profile-vectors.ts` and kept current by `bun test`.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const authority = @import("authority_score.zig");
const profile = @import("profile.zig");
const testing = std.testing;
const Value = std.json.Value;

const vectors = @embedFile("testdata/profile_vectors.json");

fn str(object: Value, name: []const u8) []const u8 {
    return object.object.get(name).?.string;
}

fn int(object: Value, name: []const u8) i64 {
    return object.object.get(name).?.integer;
}

fn float(value: Value) f64 {
    return switch (value) {
        .integer => |i| @floatFromInt(i),
        .float => |f| f,
        else => unreachable,
    };
}

fn expectCase(arena: std.mem.Allocator, case: Value, now_ms: i64) !void {
    const want = case.object.get("expected").?;
    const got = try profile.extract(arena, case.object.get("profile").?, now_ms);
    try testing.expectEqualStrings(str(want, "handle"), got.handle);
    try testing.expectEqualStrings(str(want, "displayName"), got.display_name);
    try testing.expectEqualStrings(str(want, "avatarCid"), got.avatar);
    try testing.expectEqualStrings(str(want, "description"), got.description);
    try testing.expectEqualStrings(str(want, "bannerCid"), got.banner);
    try testing.expectEqualStrings(str(want, "createdAt"), got.created_at);
    try testing.expectEqualStrings(str(want, "associated"), got.associated);
    try testing.expectEqual(@as(u64, @intCast(int(want, "followersCount"))), got.followers_count);
    try testing.expectEqual(@as(u64, @intCast(int(want, "followsCount"))), got.follows_count);
    try testing.expectEqual(@as(u64, @intCast(int(want, "postsCount"))), got.posts_count);
    try testing.expectEqual(@as(u64, @bitCast(float(want.object.get("qualityScore").?))), @as(u64, @bitCast(got.quality_score)));
    try testing.expectEqual(@as(u32, @intCast(int(want, "qualityScoreF32Bits"))), @as(u32, @bitCast(authority.stored(got.quality_score))));

    const labels = try std.json.parseFromSliceLeaky(Value, arena, str(want, "labels"), .{});
    try testing.expectEqual(labels.array.items.len, got.labels.len);
    for (labels.array.items, got.labels) |wire, label| {
        const optional = struct {
            fn text(object: Value, name: []const u8) []const u8 {
                return if (object.object.get(name)) |value| value.string else "";
            }
        };
        try testing.expectEqualStrings(optional.text(wire, "src"), label.source);
        try testing.expectEqualStrings(optional.text(wire, "uri"), label.uri);
        try testing.expectEqualStrings(optional.text(wire, "val"), label.value);
        try testing.expectEqualStrings(optional.text(wire, "cts"), label.created);
        try testing.expectEqualStrings(optional.text(wire, "cid"), label.cid);
        try testing.expectEqualStrings(optional.text(wire, "exp"), label.expires);
        try testing.expectEqual(if (wire.object.get("neg")) |neg| neg.bool else false, label.negated);
        try testing.expectEqual(@as(u64, if (wire.object.get("ver")) |ver| @intCast(ver.integer) else 0), label.version);
    }

    const override: store_mod.record.Override = switch (case.object.get("override").?) {
        .null => .none,
        .string => |action| if (std.mem.eql(u8, action, "show")) .show else .hide,
        else => unreachable,
    };
    const hidden = store_mod.moderation.resolveHidden(.{
        .labels = got.labels,
        .handle = got.handle,
        .override = override,
        .handle_flagged = false,
        .domain_blocked = case.object.get("domain_blocked").?.bool,
        .source = store_mod.moderation.bsky_moderation,
        .now = @intCast(@divFloor(now_ms, 1000)),
    });
    try testing.expectEqual(int(want, "hidden") == 1, hidden);
}

test "every field the Worker extracts from a profile comes out the same" {
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const root = try std.json.parseFromSliceLeaky(Value, a, vectors, .{});
    const now_ms = int(root, "now_ms");
    const cases = root.object.get("cases").?.array.items;
    try testing.expect(cases.len > 40);
    for (cases) |case| {
        expectCase(a, case, now_ms) catch |err| {
            std.debug.print("profile vector failed: {s}\n", .{str(case, "name")});
            return err;
        };
    }
}

test "the score matches the Worker's to the bit, as an f64 and as the stored f32" {
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    const root = try std.json.parseFromSliceLeaky(Value, arena.allocator(), vectors, .{});
    const now_ms = int(root, "now_ms");
    const rows = root.object.get("scores").?.array.items;
    try testing.expectEqual(@as(usize, 2000), rows.len);
    for (rows) |row| {
        const cells = row.array.items;
        const got = authority.score(.{
            .followers = float(cells[0]),
            .follows = float(cells[1]),
            .posts = float(cells[2]),
            .created_ms = switch (cells[3]) {
                .null => null,
                .integer => |ms| ms,
                else => unreachable,
            },
            .now_ms = now_ms,
        });
        errdefer std.debug.print("score vector failed: {d} {d} {d}\n", .{ float(cells[0]), float(cells[1]), float(cells[2]) });
        try testing.expectEqual(@as(u64, @bitCast(float(cells[4]))), @as(u64, @bitCast(got)));
        try testing.expectEqual(@as(u32, @intCast(cells[5].integer)), @as(u32, @bitCast(authority.stored(got))));
    }
}
