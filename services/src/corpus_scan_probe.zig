const std = @import("std");
const corpus = @import("store_process/corpus.zig");

var threaded: std.Io.Threaded = undefined;
pub const std_options_debug_threaded_io: ?*std.Io.Threaded = &threaded;

fn env(name: [*:0]const u8) ?[]const u8 {
    return if (std.c.getenv(name)) |value| std.mem.span(value) else null;
}

pub fn main() !void {
    const gpa = std.heap.smp_allocator;
    threaded = .init(gpa, .{ .async_limit = .limited(8) });
    defer threaded.deinit();
    const io = std.Options.debug_io;
    const path = env("PROBE_STORE_DIR") orelse return error.StorePathRequired;
    const rest = try std.fmt.parseInt(u32, env("PROBE_PAGE_REST_MS") orelse "0", 10);
    if (rest > 1000) return error.RestTooLong;
    var scanner: corpus.Scanner = .{ .gpa = gpa, .io = io, .root = .cwd(), .path = path, .layout = .{}, .interval_seconds = 0, .page_rest_ms = rest };
    const started = std.Io.Timestamp.now(io, .awake).nanoseconds;
    const counts = try scanner.count();
    const elapsed = std.Io.Timestamp.now(io, .awake).nanoseconds - started;
    std.debug.print("{{\"event\":\"scan_complete\",\"page_rest_ms\":{d},\"elapsed_ms\":{d},\"records\":{d},\"live\":{d},\"with_handles\":{d},\"with_avatars\":{d},\"hidden\":{d},\"ranked\":{d},\"rest\":{d}}}\n", .{ rest, @divFloor(elapsed, std.time.ns_per_ms), counts.records, counts.live, counts.with_handles, counts.with_avatars, counts.hidden, counts.ranked, counts.rest });
}
