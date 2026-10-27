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
    const overlay_path = std.mem.span(std.c.getenv("LOCAL_DB_PATH") orelse return error.MissingEvaluationOverlay);
    const port = try std.fmt.parseInt(u16, std.mem.span(std.c.getenv("PORT") orelse return error.MissingEvaluationPort), 10);
    var local = LocalDb.init(std.heap.page_allocator);
    defer local.deinit();
    try local.open();
    if (std.c.getenv("TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT") != null) {
        if (std.c.getenv("FLY_APP_NAME")) |value| {
            if (!std.mem.startsWith(u8, std.mem.span(value), "typeahead-startup-")) return error.InvalidEvaluationApp;
        } else {
            const root = std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_PRIVATE_ROOT") orelse return error.MissingEvaluationPrivateRoot);
            if (root.len < 2 or root[0] != '/' or root[root.len - 1] == '/') return error.InvalidEvaluationPrivateRoot;
            for ([_][]const u8{ snapshot, overlay_path }) |path| {
                if (path.len <= root.len or !std.mem.startsWith(u8, path, root) or path[root.len] != '/') return error.InvalidEvaluationPrivateRoot;
            }
        }
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
    if (std.c.getenv("TYPEAHEAD_EVAL_CAPTURE_FEED")) |output| {
        var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer arena_state.deinit();
        const arena = arena_state.allocator();
        const count = try std.fmt.parseInt(usize, std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_CAPTURE_COUNT") orelse "5000"), 10);
        const map = local.prefix_map orelse return error.NoCaptureSnapshot;
        const dids = try map.actorFenceKeys(arena, count);
        var bytes: std.Io.Writer.Allocating = .init(std.heap.page_allocator);
        defer bytes.deinit();
        var row_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer row_arena.deinit();
        var jw: std.json.Stringify = .{ .writer = &bytes.writer };
        try jw.beginArray();
        for (dids) |did| {
            const row_allocator = row_arena.allocator();
            const blob = (try map.findTagged(row_allocator, .actor, did)) orelse return error.MissingCaptureActor;
            const hex = try row_allocator.alloc(u8, blob.len * 2);
            for (blob, 0..) |value, index| {
                hex[index * 2] = "0123456789abcdef"[value >> 4];
                hex[index * 2 + 1] = "0123456789abcdef"[value & 15];
            }
            try jw.write(.{ .did = did, .blob_hex = hex });
            if (bytes.written().len > 128 * 1024 * 1024) return error.CaptureTooLarge;
            _ = row_arena.reset(.retain_capacity);
        }
        try jw.endArray();
        const file = try std.Io.Dir.cwd().createFile(io, std.mem.span(output), .{ .exclusive = true });
        defer file.close(io);
        try file.writeStreamingAll(io, bytes.written());
        try file.sync(io);
        return;
    }
    var writer_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer writer_arena.deinit();
    var writer = try EvalWriter.init(writer_arena.allocator(), &local);
    if (std.c.getenv("TYPEAHEAD_EVAL_SKIP_PREFILL") == null) {
        if (writer) |*value| try value.prefill();
    }
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
