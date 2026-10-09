const std = @import("std");
const promote = @import("promote.zig");
const io = std.Options.debug_io;
extern "c" fn fsync(fd: c_int) c_int;

pub const Entry = struct {
    manifest: promote.Manifest,
    location: enum { generation, slot_a, slot_b } = .generation,

    pub fn path(self: Entry, allocator: std.mem.Allocator, root: []const u8) ![]u8 {
        if (!promote.validBuildId(self.manifest.active_build)) return error.InvalidActivation;
        return switch (self.location) {
            .generation => std.fmt.allocPrint(allocator, "{s}/generations/{s}/index.db", .{ root, self.manifest.active_build }),
            .slot_a => std.fmt.allocPrint(allocator, "{s}/index.A.db", .{root}),
            .slot_b => std.fmt.allocPrint(allocator, "{s}/index.B.db", .{root}),
        };
    }
};

pub const State = struct {
    version: u32 = 1,
    current: Entry,
    previous: ?Entry = null,
};

pub fn readSmall(allocator: std.mem.Allocator, path: []const u8) ![]u8 {
    const file = try std.Io.Dir.openFileAbsolute(io, path, .{});
    defer file.close(io);
    var data: std.ArrayListUnmanaged(u8) = .empty;
    errdefer data.deinit(allocator);
    var buffer: [4096]u8 = undefined;
    while (true) {
        const n = file.readStreaming(io, &.{&buffer}) catch |err| switch (err) {
            error.EndOfStream => break,
            else => return err,
        };
        if (n == 0) break;
        if (data.items.len + n > 65536) return error.ManifestTooLarge;
        try data.appendSlice(allocator, buffer[0..n]);
    }
    return data.toOwnedSlice(allocator);
}

pub fn load(allocator: std.mem.Allocator, root: []const u8) !?std.json.Parsed(State) {
    const path = try std.fmt.allocPrint(allocator, "{s}/active.json", .{root});
    defer allocator.free(path);
    const bytes = readSmall(allocator, path) catch |err| switch (err) {
        error.FileNotFound => return null,
        else => return err,
    };
    defer allocator.free(bytes);
    const parsed = try std.json.parseFromSlice(State, allocator, bytes, .{ .allocate = .alloc_always });
    errdefer parsed.deinit();
    if (parsed.value.version != 1) return error.InvalidActivation;
    try promote.validateManifest(parsed.value.current.manifest);
    if (parsed.value.previous) |previous| try promote.validateManifest(previous.manifest);
    return parsed;
}

pub fn syncDirectory(path: []const u8) !void {
    var dir = try std.Io.Dir.openDirAbsolute(io, path, .{});
    defer dir.close(io);
    if (fsync(dir.handle) != 0) return error.DirectorySyncFailed;
}

pub fn writeFile(path: []const u8, bytes: []const u8) !void {
    const file = try std.Io.Dir.createFileAbsolute(io, path, .{ .truncate = true });
    defer file.close(io);
    var buffer: [4096]u8 = undefined;
    var writer = std.Io.File.Writer.init(file, io, &buffer);
    try writer.interface.writeAll(bytes);
    try writer.interface.flush();
    try file.sync(io);
}

pub fn save(allocator: std.mem.Allocator, root: []const u8, value: State) !void {
    const bytes = try std.json.Stringify.valueAlloc(allocator, value, .{});
    defer allocator.free(bytes);
    const temporary = try std.fmt.allocPrint(allocator, "{s}/active.json.part", .{root});
    defer allocator.free(temporary);
    const destination = try std.fmt.allocPrint(allocator, "{s}/active.json", .{root});
    defer allocator.free(destination);
    try writeFile(temporary, bytes);
    try std.Io.Dir.renameAbsolute(temporary, destination, io);
    try syncDirectory(root);
}

pub fn rollbackAllowed(current: promote.Manifest, previous: promote.Manifest) bool {
    return previous.source_watermark >= current.source_watermark;
}
