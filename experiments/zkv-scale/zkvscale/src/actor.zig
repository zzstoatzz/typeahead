const std = @import("std");

pub const Fields = struct {
    did: []const u8,
    handle: []const u8 = "",
    quality_score: f32,
    version: u64,
    deleted: bool = false,
    hidden: bool = false,
};

pub fn parse(arena: std.mem.Allocator, json: []const u8) !Fields {
    return std.json.parseFromSliceLeaky(Fields, arena, json, .{ .ignore_unknown_fields = true });
}

pub fn bumpVersion(gpa: std.mem.Allocator, out: *std.ArrayList(u8), json: []const u8, old: u64, new: u64) !void {
    var needle_buf: [40]u8 = undefined;
    const needle = try std.fmt.bufPrint(&needle_buf, "\"version\":{d}", .{old});
    const at = std.mem.lastIndexOf(u8, json, needle) orelse return error.VersionFieldNotFound;
    const after = at + needle.len;
    if (after >= json.len or (json[after] != ',' and json[after] != '}')) return error.VersionFieldNotFound;
    out.clearRetainingCapacity();
    try out.appendSlice(gpa, json[0..at]);
    try out.print(gpa, "\"version\":{d}", .{new});
    try out.appendSlice(gpa, json[after..]);
}

const sample =
    \\{"did":"did:plc:abc","handle":"Alice.Test","display_name":"say \"version\":7 twice","avatar_url":"","labels":[{"val":"x","version":7}],"associated":{},"created_at":"","pds":"","pds_host":"","quality_score":12.5,"version":7,"deleted":false,"hidden":true,"followers_count":3,"follows_count":0,"posts_count":0,"last_activity_at":0}
;

test "parse reads the indexed fields and ignores the rest" {
    var arena: std.heap.ArenaAllocator = .init(std.testing.allocator);
    defer arena.deinit();
    const fields = try parse(arena.allocator(), sample);
    try std.testing.expectEqualStrings("did:plc:abc", fields.did);
    try std.testing.expectEqualStrings("Alice.Test", fields.handle);
    try std.testing.expectEqual(@as(f32, 12.5), fields.quality_score);
    try std.testing.expectEqual(@as(u64, 7), fields.version);
    try std.testing.expect(fields.hidden and !fields.deleted);
}

test "bumpVersion rewrites only the top-level version" {
    var arena: std.heap.ArenaAllocator = .init(std.testing.allocator);
    defer arena.deinit();
    var out: std.ArrayList(u8) = .empty;
    defer out.deinit(std.testing.allocator);
    try bumpVersion(std.testing.allocator, &out, sample, 7, 1_800_000_000);
    const fields = try parse(arena.allocator(), out.items);
    try std.testing.expectEqual(@as(u64, 1_800_000_000), fields.version);
    try std.testing.expectEqual(sample.len + 9, out.items.len);
    try std.testing.expect(std.mem.indexOf(u8, out.items, "say \\\"version\\\":7 twice") != null);
    try std.testing.expect(std.mem.indexOf(u8, out.items, "\"val\":\"x\",\"version\":7}") != null);
    try std.testing.expectError(error.VersionFieldNotFound, bumpVersion(std.testing.allocator, &out, sample, 8, 9));
}
