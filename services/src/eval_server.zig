const std = @import("std");
const LocalDb = @import("db/LocalDb.zig");
const server = @import("server.zig");

pub fn main() !void {
    const io = std.Options.debug_io;
    const snapshot = std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_SNAPSHOT") orelse return error.MissingEvaluationSnapshot);
    _ = std.c.getenv("LOCAL_DB_PATH") orelse return error.MissingEvaluationOverlay;
    const port = try std.fmt.parseInt(u16, std.mem.span(std.c.getenv("PORT") orelse return error.MissingEvaluationPort), 10);
    var local = LocalDb.init(std.heap.page_allocator);
    defer local.deinit();
    try local.open();
    try local.swapSnapshot(snapshot);
    if (try local.attachedMetaText(std.heap.page_allocator, "pds_scope_policy")) |policy| {
        defer std.heap.page_allocator.free(policy);
        try local.exec("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('pds_scope_policy',?)", .{policy});
    }
    var addr = std.Io.net.IpAddress{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = port } };
    var listener = try addr.listen(io, .{ .reuse_address = true });
    defer listener.deinit(io);
    var tasks: std.Io.Group = .init;
    defer tasks.cancel(io);
    while (true) {
        const stream = try listener.accept(io);
        tasks.async(io, server.handleConnection, .{ stream, &local, null, std.Thread.getCurrentId() });
    }
}
