//! One query per process, intended for a supervisor with time/memory limits.
//! The only DB access is an explicit read-only overlay + immutable snapshot.
const std = @import("std");
const zqlite = @import("zqlite");
const LocalDb = @import("db/LocalDb.zig");
const index = @import("index/index_search.zig");
extern "c" fn write(fd: c_int, buf: [*]const u8, count: usize) isize;
fn env(comptime name: [:0]const u8) ![:0]const u8 {
    return std.mem.span(std.c.getenv(name) orelse return error.MissingEvaluationInput);
}
pub fn main() !void {
    const query = try env("TYPEAHEAD_EVAL_QUERY");
    if (query.len > 256) return error.QueryTooLong;
    var conn = try zqlite.open(try env("TYPEAHEAD_EVAL_OVERLAY"), zqlite.OpenFlags.ReadOnly);
    defer conn.close();
    // Match LocalDb.applyReadPragmas before ATTACH can contend with a writer.
    try conn.exec("PRAGMA query_only=ON", .{});
    try conn.exec("PRAGMA busy_timeout=1000", .{});
    try conn.exec("ATTACH DATABASE ? AS idx", .{try env("TYPEAHEAD_EVAL_SNAPSHOT")});
    try conn.exec("PRAGMA cache_size=-20000", .{});
    try conn.exec("PRAGMA mmap_size=268435456", .{});
    // A full comparison must use one SQLite snapshot even if a supervisor
    // later supplies a changing overlay. The subprocess timeout bounds it.
    try conn.exec("BEGIN", .{});
    defer conn.exec("ROLLBACK", .{}) catch {};
    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var local = LocalDb.init(arena);
    const result = try index.evaluateFrozen(arena, .{ .db = &local, .slot = 0, .conn = conn }, query);
    var out: std.Io.Writer.Allocating = .init(arena);
    var jw: std.json.Stringify = .{ .writer = &out.writer };
    try jw.write(result);
    try out.writer.writeByte('\n');
    var bytes = out.written();
    while (bytes.len > 0) {
        const n = write(1, bytes.ptr, bytes.len);
        if (n <= 0) return error.OutputFailed;
        bytes = bytes[@intCast(n)..];
    }
    if (!result.equal) return error.ResultMismatch;
}
