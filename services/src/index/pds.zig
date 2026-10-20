const std = @import("std");

pub fn host(input: []const u8) ?[]const u8 {
    var value = input;
    if (std.ascii.startsWithIgnoreCase(value, "https://")) value = value[8..] else if (std.ascii.startsWithIgnoreCase(value, "http://")) value = value[7..];
    if (std.mem.endsWith(u8, value, "/")) value = value[0 .. value.len - 1];
    if (std.mem.endsWith(u8, value, ".")) value = value[0 .. value.len - 1];
    if (value.len == 0 or value.len > 253 or std.mem.indexOfScalar(u8, value, '.') == null) return null;
    var labels = std.mem.splitScalar(u8, value, '.');
    while (labels.next()) |label| {
        if (label.len == 0 or label.len > 63 or label[0] == '-' or label[label.len - 1] == '-') return null;
        for (label) |c| if (!std.ascii.isAlphanumeric(c) and c != '-') return null;
    }
    return value;
}

pub fn normalize(arena: std.mem.Allocator, input: []const u8) !?[]const u8 {
    const value = host(input) orelse return null;
    return try std.ascii.allocLowerString(arena, value);
}

pub fn scopedKey(arena: std.mem.Allocator, key: []const u8, hostname: []const u8) ![]const u8 {
    return std.fmt.allocPrint(arena, "{s}\x1f{s}", .{ key, hostname });
}

pub fn matches(endpoint: []const u8, hostname: []const u8) bool {
    return std.ascii.eqlIgnoreCase(host(endpoint) orelse return false, hostname);
}

test "PDS hostname normalization rejects ambiguous scopes" {
    const t = std.testing;
    try t.expectEqualStrings("Acme.com", host("https://Acme.com./").?);
    for ([_][]const u8{ "", "acme", "evil@acme.com", "acme.com/path", "acme.com?x", "acme.com:443", "*.acme.com", "a..com", "-a.com", "acme.com\x1fh:b" }) |v| try t.expect(host(v) == null);
    try t.expect(matches("https://ACME.com/", "acme.com"));
    try t.expect(!matches("https://sub.acme.com", "acme.com"));
}
