const std = @import("std");
const c = std.c;
const io = std.Options.debug_io;
pub const block_size = 4096;
pub const header_size = 128;
pub const metadata_prefault_limit = 64 * 1024 * 1024;
extern "c" fn mkstemp(template: [*:0]u8) c_int;

fn file(fd: c.fd_t) std.Io.File {
    return .{ .handle = fd, .flags = .{ .nonblocking = false } };
}

pub fn identity(fd: c.fd_t, expected: []const u8) ![header_size]u8 {
    if (expected.len != 64) return error.InvalidAdmissionDigest;
    const stat = try file(fd).stat(io);
    if (stat.kind != .file or stat.size % block_size != 0) return error.InvalidAdmissionFile;
    var bytes: [header_size]u8 = @splat(0);
    @memcpy(bytes[0..8], "FLATAD01");
    _ = try std.fmt.hexToBytes(bytes[8..40], expected);
    std.mem.writeInt(u64, bytes[40..48], stat.size, .little);
    std.mem.writeInt(u64, bytes[48..56], @intCast(stat.inode), .little);
    std.mem.writeInt(i128, bytes[56..72], stat.mtime.nanoseconds, .little);
    std.mem.writeInt(i128, bytes[72..88], stat.ctime.nanoseconds, .little);
    std.mem.writeInt(u32, bytes[88..92], block_size, .little);
    return bytes;
}

pub fn record(hashes: []u8, bytes: []const u8, offset: usize) void {
    std.debug.assert(offset % block_size == 0 and bytes.len % block_size == 0);
    for (0..bytes.len / block_size) |i| {
        const position = i * block_size;
        const slot = (offset / block_size + i) * 32;
        std.crypto.hash.sha2.Sha256.hash(bytes[position..][0..block_size], hashes[slot..][0..32], .{});
    }
}

pub fn publish(allocator: std.mem.Allocator, path: []const u8, fd: c.fd_t, before: [header_size]u8, expected: []const u8, hashes: []const u8) !void {
    try file(fd).sync(io);
    const after = try identity(fd, expected);
    if (!std.mem.eql(u8, &before, &after)) return error.SnapshotChangedDuringAdmission;
    const destination = try std.fmt.allocPrint(allocator, "{s}.admitted", .{path});
    defer allocator.free(destination);
    const temporary = try std.fmt.allocPrintSentinel(allocator, "{s}.admitted.XXXXXX", .{path}, 0);
    defer allocator.free(temporary);
    const writer_fd = mkstemp(temporary);
    if (writer_fd < 0) return error.AdmissionCreateFailed;
    const writer = file(writer_fd);
    defer writer.close(io);
    defer _ = c.unlink(temporary);
    try writer.writeStreamingAll(io, &after);
    try writer.writeStreamingAll(io, hashes);
    try writer.sync(io);
    try std.Io.Dir.renameAbsolute(temporary, destination, io);
    var directory = try std.Io.Dir.cwd().openDir(io, std.fs.path.dirname(path) orelse ".", .{ .iterate = true });
    defer directory.close(io);
    if (c.fsync(directory.handle) != 0) return error.AdmissionDirectorySyncFailed;
}

pub const Admission = struct {
    mapping: std.Io.File.MemoryMap,

    pub fn open(allocator: std.mem.Allocator, path: []const u8, fd: c.fd_t, expected: []const u8) !Admission {
        const binding = try identity(fd, expected);
        const size = std.mem.readInt(u64, binding[40..48], .little);
        const receipt_path = try std.fmt.allocPrint(allocator, "{s}.admitted", .{path});
        defer allocator.free(receipt_path);
        const receipt = std.Io.Dir.openFileAbsolute(io, receipt_path, .{}) catch |err| switch (err) {
            error.FileNotFound => return error.SnapshotNotAdmitted,
            else => return err,
        };
        defer receipt.close(io);
        const stat = try receipt.stat(io);
        const length = std.math.cast(usize, header_size + size / block_size * 32) orelse return error.AdmissionTooLarge;
        if (stat.size != length) return error.InvalidAdmission;
        var stored: [header_size]u8 = undefined;
        if (try receipt.readPositionalAll(io, &stored, 0) != stored.len or !std.mem.eql(u8, &binding, &stored)) return error.StaleAdmission;
        const mapping = try std.Io.File.MemoryMap.create(io, receipt, .{
            .len = length,
            .protection = .{ .read = true, .write = false },
            .populate = length <= metadata_prefault_limit,
        });
        return .{ .mapping = mapping };
    }

    pub fn close(self: Admission) void {
        var mapping = self.mapping;
        mapping.destroy(io);
    }

    pub fn verify(self: Admission, bytes: []const u8, offset: usize) !void {
        if (offset % block_size != 0 or bytes.len % block_size != 0) return error.InvalidAdmissionRange;
        for (0..bytes.len / block_size) |i| {
            const slot = header_size + (offset / block_size + i) * 32;
            if (slot > self.mapping.memory.len or self.mapping.memory.len - slot < 32) return error.InvalidAdmissionRange;
            var digest: [32]u8 = undefined;
            std.crypto.hash.sha2.Sha256.hash(bytes[i * block_size ..][0..block_size], &digest, .{});
            if (!std.mem.eql(u8, &digest, self.mapping.memory[slot..][0..32])) return error.FlatBlockChecksumMismatch;
        }
    }

    pub fn verifyRoot(self: Admission, roots: []const u8, start: usize, end: usize) !void {
        if (start > end or end > roots.len) return error.InvalidAdmissionRange;
        const first = start / block_size * block_size;
        const last = (end + block_size - 1) / block_size * block_size;
        if (last > roots.len) return error.InvalidAdmissionRange;
        try self.verify(roots[first..last], first);
    }
};
