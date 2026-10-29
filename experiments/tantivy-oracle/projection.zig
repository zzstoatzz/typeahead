const std = @import("std");
const native = @import("native_normalize");
const pds = @import("native_pds");

const Projection = extern struct {
    handle: [256]u8,
    display: [256]u8,
    host: [253]u8,
    handle_len: usize,
    display_len: usize,
    host_len: usize,
};

fn norm(allocator: std.mem.Allocator, input: []const u8, is_handle: bool) ![]const u8 {
    return if (is_handle) native.handle(allocator, input) else native.display(allocator, input);
}

fn project(input: []const u8, is_handle: bool, out: []u8) !usize {
    if (input.len > 1024) return error.BudgetExceeded;
    if (!std.unicode.utf8ValidateSlice(input)) return error.InvalidInput;
    var buffer: [1024]u8 = undefined;
    var arena = std.heap.FixedBufferAllocator.init(&buffer);
    const value = try norm(arena.allocator(), input, is_handle);
    if (value.len > native.MAX_OUTPUT) return error.BudgetExceeded;
    @memcpy(out[0..value.len], value);
    const length = value.len;
    if (length < native.MAX_OUTPUT) return length;
    var end: usize = 1;
    while (end <= input.len) : (end += 1) {
        if (end < input.len and input[end] & 0xc0 == 0x80) continue;
        arena.reset();
        const prefix = try norm(arena.allocator(), input[0..end], is_handle);
        if (prefix.len >= native.MAX_OUTPUT) {
            arena.reset();
            const tail = try norm(arena.allocator(), input[end..], is_handle);
            if (tail.len != 0) return error.BudgetExceeded;
            return length;
        }
    }
    return length;
}

export fn oracle_project(handle_ptr: [*]const u8, handle_len: usize, display_ptr: [*]const u8, display_len: usize, pds_ptr: [*]const u8, pds_len: usize, output: *Projection) i32 {
    output.handle_len = project(handle_ptr[0..handle_len], true, &output.handle) catch |err| return if (err == error.BudgetExceeded) 1 else 2;
    output.display_len = project(display_ptr[0..display_len], false, &output.display) catch |err| return if (err == error.BudgetExceeded) 1 else 2;
    const handle = output.handle[0..output.handle_len];
    const display = output.display[0..output.display_len];
    var key_count: usize = if (handle.len == 0) 0 else 1;
    var iterator = (std.unicode.Utf8View.initUnchecked(handle)).iterator();
    var offset: usize = 0;
    while (iterator.nextCodepointSlice()) |cp| {
        key_count += 1;
        offset += cp.len;
        if (std.mem.eql(u8, cp, ".")) key_count += std.unicode.utf8CountCodepoints(handle[offset..]) catch return 2;
    }
    iterator = (std.unicode.Utf8View.initUnchecked(display)).iterator();
    while (iterator.nextCodepointSlice()) |cp| if (!std.mem.eql(u8, cp, " ")) { key_count += 1; };
    if (key_count > 8192) return 1;
    output.host_len = 0;
    if (pds_len <= 263) {
        if (pds.host(pds_ptr[0..pds_len])) |host| {
            for (host, 0..) |byte, index| output.host[index] = std.ascii.toLower(byte);
            output.host_len = host.len;
        }
    }
    return 0;
}
