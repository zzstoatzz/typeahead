const std = @import("std");

pub const prefixes = "ahiqrtu";
pub const max_did = 512;
pub const max_handle = 512;
pub const Buf = [1 + max_handle + 1 + 8 + max_did]u8;

pub fn actor(buf: *Buf, did: []const u8) []const u8 {
    buf[0] = 'a';
    @memcpy(buf[1..][0..did.len], did);
    return buf[0 .. 1 + did.len];
}

pub fn handlePrefix(buf: *Buf, handle: []const u8) []const u8 {
    buf[0] = 'h';
    for (handle, buf[1..][0..handle.len]) |c, *out| out.* = std.ascii.toLower(c);
    buf[1 + handle.len] = 0;
    return buf[0 .. 2 + handle.len];
}

pub fn handleKey(buf: *Buf, handle: []const u8, did: []const u8) []const u8 {
    const at = handlePrefix(buf, handle).len;
    @memcpy(buf[at..][0..did.len], did);
    return buf[0 .. at + did.len];
}

pub fn stamped(buf: *Buf, prefix: u8, stamp: u64, did: []const u8) []const u8 {
    buf[0] = prefix;
    std.mem.writeInt(u64, buf[1..9], stamp, .big);
    @memcpy(buf[9..][0..did.len], did);
    return buf[0 .. 9 + did.len];
}

pub fn quality(buf: *Buf, score: f32, did: []const u8) []const u8 {
    buf[0] = 'q';
    std.mem.writeInt(u32, buf[1..5], ~@as(u32, @bitCast(score)), .big);
    @memcpy(buf[5..][0..did.len], did);
    return buf[0 .. 5 + did.len];
}

pub fn didOfActorKey(key: []const u8) []const u8 {
    return key[1..];
}

pub fn didOfHandleKey(key: []const u8) ?[]const u8 {
    const at = std.mem.indexOfScalar(u8, key, 0) orelse return null;
    return key[at + 1 ..];
}

fn subsetHash(did: []const u8) u64 {
    return std.hash.Wyhash.hash(0x7a6b76, did);
}

pub fn inRefreshSubset(did: []const u8) bool {
    return subsetHash(did) % 8 == 0;
}

pub fn inIdentitySubset(did: []const u8) bool {
    return (subsetHash(did) >> 8) % 1000 == 0;
}

fn less(a: []const u8, b: []const u8) bool {
    return std.mem.order(u8, a, b) == .lt;
}

test "q keys sort by descending score, then did" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    const scores = [_]f32{ 98765.5, 100.25, 100.0, 1.5, 0.001, 1e-30 };
    for (scores[0 .. scores.len - 1], scores[1..]) |high, low| {
        try std.testing.expect(less(quality(&a, high, "did:plc:zzzz"), quality(&b, low, "did:plc:aaaa")));
    }
    try std.testing.expect(less(quality(&a, 7.0, "did:plc:aaaa"), quality(&b, 7.0, "did:plc:bbbb")));
}

test "stamped keys sort by ascending stamp, then did" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    const stamps = [_]u64{ 0, 1, 255, 256, 65535, 1_793_901_791, 1_793_901_792, 1 << 40, std.math.maxInt(u64) };
    for (stamps[0 .. stamps.len - 1], stamps[1..]) |low, high| {
        try std.testing.expect(less(stamped(&a, 'u', low, "did:plc:zzzz"), stamped(&b, 'u', high, "did:plc:aaaa")));
    }
    try std.testing.expect(less(stamped(&a, 'u', 9, "did:plc:aaaa"), stamped(&b, 'u', 9, "did:plc:bbbb")));
}

test "every prefix owns a disjoint one-byte range" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    try std.testing.expect(less(actor(&a, "did:plc:zzzz"), handleKey(&b, "a.test", "did:plc:aaaa")));
    try std.testing.expect(less(stamped(&a, 'r', std.math.maxInt(u64), "did:plc:zzzz"), stamped(&b, 't', 0, "did:plc:aaaa")));
    try std.testing.expect(less(quality(&a, 1e-30, "did:plc:zzzz"), stamped(&b, 'r', 0, "did:plc:aaaa")));
    try std.testing.expectEqualStrings("did:plc:abc", didOfActorKey(actor(&a, "did:plc:abc")));
}

test "a handle lookup range holds exactly that handle" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    var c: Buf = undefined;
    const start = handlePrefix(&a, "Alice.Test");
    try std.testing.expectEqualStrings("halice.test\x00", start);
    const exact = handleKey(&b, "ALICE.test", "did:plc:abc");
    try std.testing.expect(std.mem.startsWith(u8, exact, start));
    try std.testing.expectEqualStrings("did:plc:abc", didOfHandleKey(exact).?);
    try std.testing.expect(!std.mem.startsWith(u8, handleKey(&c, "alice.tests", "did:plc:abc"), start));
    try std.testing.expect(!std.mem.startsWith(u8, handleKey(&c, "alice.tes", "did:plc:abc"), start));
}

test "subsets are deterministic and near their nominal rates" {
    var refresh: usize = 0;
    var identity: usize = 0;
    var buf: [40]u8 = undefined;
    for (0..200_000) |n| {
        const did = try std.fmt.bufPrint(&buf, "did:plc:{d:0>24}", .{n});
        try std.testing.expectEqual(inRefreshSubset(did), inRefreshSubset(did));
        refresh += @intFromBool(inRefreshSubset(did));
        identity += @intFromBool(inIdentitySubset(did));
    }
    try std.testing.expect(refresh > 24_000 and refresh < 26_000);
    try std.testing.expect(identity > 150 and identity < 250);
}
