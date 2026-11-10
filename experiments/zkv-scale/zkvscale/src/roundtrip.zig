const std = @import("std");
const actor = @import("actor.zig");
const keys = @import("keys.zig");
const load = @import("load.zig");
const scan = @import("scan.zig");
const store = @import("store.zig");

const Collect = struct {
    gpa: std.mem.Allocator,
    dids: std.ArrayList([]u8) = .empty,
    offset: usize,

    pub fn entry(self: *Collect, key: []const u8, _: []const u8) !void {
        try self.dids.append(self.gpa, try self.gpa.dupe(u8, key[self.offset..]));
    }

    fn deinit(self: *Collect) void {
        for (self.dids.items) |did| self.gpa.free(did);
        self.dids.deinit(self.gpa);
    }
};

fn expectOrder(db: *store.Db, prefix: []const u8, offset: usize, page: usize, expected: []const []const u8) !void {
    const gpa = std.testing.allocator;
    var collect: Collect = .{ .gpa = gpa, .offset = offset };
    defer collect.deinit();
    const totals = try scan.prefixScan(gpa, db, prefix, .{ .page = page }, &collect);
    try std.testing.expectEqual(expected.len, totals.entries);
    for (expected, collect.dids.items) |want, got| try std.testing.expectEqualStrings(want, got);
}

test "loaded actors are reachable through every index in the documented order" {
    const gpa = std.testing.allocator;
    const io = std.testing.io;
    var tmp = std.testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    var dir_store: store.Store = .init(gpa, io, tmp.dir);
    defer dir_store.deinit();
    const db = try store.Db.open(gpa, &dir_store, .{});
    defer db.close();

    const lines = [_][]const u8{
        \\{"did":"did:plc:low","handle":"Low.Test","quality_score":1.5,"version":300,"deleted":false,"hidden":false}
        ,
        \\{"did":"did:plc:high","handle":"high.test","quality_score":900.0,"version":100,"deleted":false,"hidden":false}
        ,
        \\{"did":"did:plc:hidden","handle":"hidden.test","quality_score":50.0,"version":200,"deleted":false,"hidden":true}
        ,
        \\{"did":"did:plc:gone","handle":"","quality_score":0.0,"version":400,"deleted":true,"hidden":false}
    };
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    var counts: load.Counts = .{};
    for (lines) |line| {
        try std.testing.expect(try load.writeActor(db, &counts, try actor.parse(arena.allocator(), line), line));
    }
    try db.commit();
    try db.flush();
    try std.testing.expectEqual(@as(u64, 4), counts.a);
    try std.testing.expectEqual(@as(u64, 3), counts.h);
    try std.testing.expectEqual(@as(u64, 2), counts.q);
    try std.testing.expectEqual(@as(u64, 1), counts.t);

    try expectOrder(db, "a", 1, 2, &.{ "did:plc:gone", "did:plc:hidden", "did:plc:high", "did:plc:low" });
    try expectOrder(db, "u", 9, 1, &.{ "did:plc:high", "did:plc:hidden", "did:plc:low", "did:plc:gone" });
    try expectOrder(db, "q", 5, 100, &.{ "did:plc:high", "did:plc:low" });
    try expectOrder(db, "t", 9, 100, &.{"did:plc:gone"});

    var moved = try actor.parse(arena.allocator(), lines[1]);
    try load.eraseStamps(db, moved);
    moved.version = 500;
    var scratch: load.Counts = .{};
    try load.putStamps(db, &scratch, moved);
    try db.commit();
    try expectOrder(db, "u", 9, 3, &.{ "did:plc:hidden", "did:plc:low", "did:plc:gone", "did:plc:high" });

    var buf: keys.Buf = undefined;
    try expectOrder(db, keys.handlePrefix(&buf, "LOW.test"), "hlow.test\x00".len, 100, &.{"did:plc:low"});
    try std.testing.expectEqualStrings(lines[1], (try db.get(keys.actor(&buf, "did:plc:high"))).?);
}
