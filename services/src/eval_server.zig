const std = @import("std");
const LocalDb = @import("db/LocalDb.zig");
const server = @import("server.zig");
const EvalWriter = @import("eval_writer.zig").Writer;

var threaded_io: std.Io.Threaded = undefined;
pub const std_options_debug_threaded_io: ?*std.Io.Threaded = &threaded_io;

pub fn main() !void {
    threaded_io = std.Io.Threaded.init(std.heap.page_allocator, .{
        .stack_size = 8 * 1024 * 1024,
        .async_limit = .limited(8),
    });
    const io = std.Options.debug_io;
    const nonce = std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_NONCE") orelse return error.MissingEvaluationNonce);
    if (nonce.len < 16 or nonce.len > 128) return error.InvalidEvaluationNonce;
    const input_id = std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_INPUT_ID") orelse "");
    const snapshot = std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_SNAPSHOT") orelse return error.MissingEvaluationSnapshot);
    _ = std.c.getenv("LOCAL_DB_PATH") orelse return error.MissingEvaluationOverlay;
    const port = try std.fmt.parseInt(u16, std.mem.span(std.c.getenv("PORT") orelse return error.MissingEvaluationPort), 10);
    var local = LocalDb.init(std.heap.page_allocator);
    defer local.deinit();
    try local.open();
    if (std.c.getenv("TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT") != null) {
        const app = std.mem.span(std.c.getenv("FLY_APP_NAME") orelse return error.MissingEvaluationApp);
        if (!std.mem.startsWith(u8, app, "typeahead-startup-")) return error.InvalidEvaluationApp;
        var prepared = try local.prepareAdmittedSnapshot(snapshot);
        defer prepared.deinit();
        local.activateSnapshot(&prepared);
    } else try local.swapSnapshot(snapshot);
    if (try local.attachedMetaText(std.heap.page_allocator, "pds_scope_policy")) |policy| {
        defer std.heap.page_allocator.free(policy);
        const conn = local.conn.?;
        const ready = blk: {
            const row = (try conn.row("SELECT value FROM sync_meta WHERE key='pds_scope_policy'", .{})) orelse break :blk false;
            defer row.deinit();
            break :blk std.mem.eql(u8, row.text(0), policy);
        };
        if (!ready) {
            const row = (try conn.row("SELECT EXISTS(SELECT 1 FROM actor_overlay LIMIT 1) OR EXISTS(SELECT 1 FROM prefix_overlay LIMIT 1)", .{})).?;
            defer row.deinit();
            if (row.int(0) != 0) return error.UnreadyEvaluationOverlay;
            try local.exec("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('pds_scope_policy',?)", .{policy});
        }
    }
    var writer_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer writer_arena.deinit();
    var writer = try EvalWriter.init(writer_arena.allocator(), &local);
    if (writer) |*value| try value.prefill();
    const slots = try std.fmt.parseInt(usize, std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_BACKGROUND_SLOTS") orelse if (writer != null) "3" else "0"), 10);
    if (slots > 7 or (writer != null and slots == 0)) return error.InvalidBackgroundSlots;
    var addr = std.Io.net.IpAddress{ .ip4 = .{ .bytes = .{ 127, 0, 0, 1 }, .port = port } };
    var listener = try addr.listen(io, .{ .reuse_address = true });
    defer listener.deinit(io);
    var tasks: std.Io.Group = .init;
    defer tasks.cancel(io);
    if (writer) |*value| try tasks.concurrent(io, EvalWriter.run, .{value});
    for (0..slots - @as(usize, @intFromBool(writer != null))) |_| try tasks.concurrent(io, idleLoop, .{});
    while (true) {
        const stream = try listener.accept(io);
        tasks.async(io, server.handleEvaluationConnection, .{ stream, &local, std.Thread.getCurrentId(), server.EvaluationIdentity{ .nonce = nonce, .input_id = input_id, .writer = if (writer) |*value| &value.counters else null } });
    }
}

fn idleLoop() void {
    while (true) std.Options.debug_io.sleep(std.Io.Duration.fromSeconds(60), .awake) catch return;
}
