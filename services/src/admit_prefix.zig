const std = @import("std");
const Map = @import("index/flat_prefix.zig").Map;

pub fn main() !void {
    const path = std.mem.span(std.c.getenv("TYPEAHEAD_ADMIT_PREFIX_PATH") orelse return error.MissingPrefixPath);
    const digest = std.mem.span(std.c.getenv("TYPEAHEAD_ADMIT_PREFIX_SHA256") orelse return error.MissingPrefixDigest);
    const map = try Map.admitFile(std.heap.page_allocator, path, digest);
    defer map.close();
    std.log.info("admitted {s}: {d} bytes, {d} pages", .{ path, map.file_size, map.pages });
}
