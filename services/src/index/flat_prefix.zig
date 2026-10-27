const std = @import("std");
const c = std.c;
const admission = @import("flat_admission.zig");
extern "c" fn posix_fadvise(fd: c_int, offset: c.off_t, len: c.off_t, advice: c_int) c_int;
const max_key = 8193;
const normal_page = 8192;
const max_extent = 16 * 1024 * 1024;
const validation_read_size = 1024 * 1024;
pub const Namespace = enum(u8) { actor = 1, prefix = 2 };

pub const Cache = struct {
    pages: [128]?usize,
    bytes: [128][normal_page]u8,
    root_pages: [256]?usize,

    pub fn clear(self: *Cache) void {
        self.pages = @splat(null);
        self.beginQuery();
    }

    pub fn beginQuery(self: *Cache) void {
        self.root_pages = @splat(null);
    }

    fn read(self: *Cache, fd: c.fd_t, index: usize, offset: usize) ![]const u8 {
        const slot = index % self.pages.len;
        if (self.pages[slot] != index) {
            self.pages[slot] = null;
            try readExact(fd, &self.bytes[slot], offset);
            self.pages[slot] = index;
        }
        return &self.bytes[slot];
    }
};

pub const Map = struct {
    bytes: []const u8,
    file_size: usize,
    fd: ?c.fd_t = null,
    owner: ?std.mem.Allocator = null,
    mapping: ?std.Io.File.MemoryMap = null,
    admitted: ?admission.Admission = null,
    pages: usize,
    records: usize,
    page_data: usize,
    const Record = struct { key: []const u8, offset: usize };
    const Page = struct { bytes: []const u8, count: usize, key_end: usize, used: usize };
    const Extent = struct { start: usize, end: usize };

    fn validateRoot(bytes: []const u8, file_size: usize) !Map {
        if (bytes.len < 84 or !std.mem.eql(u8, bytes[0..8], "FLATPX05")) return error.InvalidFlatHeader;
        if (std.mem.readInt(u32, bytes[8..12], .little) != 5 or std.mem.readInt(u64, bytes[16..24], .little) != 0) return error.InvalidFlatVersion;
        for ([_]usize{ 48, 56, 64, 72 }) |offset| if (std.mem.readInt(u64, bytes[offset..][0..8], .little) != 0) return error.InvalidFlatVersion;
        const pages: usize = std.mem.readInt(u32, bytes[12..16], .little);
        if (pages >= (bytes.len - 80) / 4) return error.InvalidFlatSections;
        var sections: [3]usize = undefined;
        for (&sections, 0..) |*section, i| {
            const value = std.mem.readInt(u64, bytes[24 + 8 * i ..][0..8], .little);
            if (value > file_size) return error.InvalidFlatSections;
            section.* = @intCast(value);
        }
        const self: Map = .{ .bytes = bytes, .file_size = file_size, .pages = pages, .records = sections[0], .page_data = sections[1] };
        if (sections[2] != file_size or self.records != 80 + (pages + 1) * 4 or self.records > self.page_data or self.page_data > bytes.len or self.page_data % 4096 != 0 or file_size % 4096 != 0) return error.InvalidFlatSections;
        if (std.mem.readInt(u32, bytes[80..84], .little) != 0 or std.mem.readInt(u32, bytes[80 + pages * 4 ..][0..4], .little) > self.page_data - self.records) return error.InvalidFlatDirectory;
        var previous: ?[]const u8 = null;
        for (0..pages) |i| {
            const root = try self.record(i);
            _ = try self.extent(i);
            if (previous) |prev| if (std.mem.order(u8, prev, root.key) != .lt) return error.InvalidFlatDirectory;
            previous = root.key;
        }
        if (pages == 0 and self.page_data != file_size) return error.InvalidFlatSections;
        return self;
    }

    pub fn validate(bytes: []const u8, expected: []const u8) !Map {
        const self = try validateRoot(bytes, bytes.len);
        for (0..self.pages) |i| {
            const current = try self.page(i);
            const root = try self.record(i);
            const next_key: ?[]const u8 = if (i + 1 < self.pages) (try self.record(i + 1)).key else null;
            try checkPage(current, root.key, next_key);
        }
        var digest: [32]u8 = undefined;
        std.crypto.hash.sha2.Sha256.hash(bytes, &digest, .{});
        const hex = std.fmt.bytesToHex(digest, .lower);
        if (!std.mem.eql(u8, &hex, expected)) return error.FlatDigestMismatch;
        return self;
    }

    fn record(self: Map, index: usize) !Record {
        return self.recordCached(index, null);
    }

    fn verifyRoot(self: Map, cache: ?*Cache, start: usize, end: usize) !void {
        const receipt = self.admitted orelse return;
        if (start > end or end > self.bytes.len) return error.InvalidAdmissionRange;
        if (cache) |cached| {
            const first = start / admission.block_size;
            const last = (end + admission.block_size - 1) / admission.block_size;
            for (first..last) |page_index| {
                const slot = page_index % cached.root_pages.len;
                if (cached.root_pages[slot] == page_index) continue;
                const offset = page_index * admission.block_size;
                try receipt.verifyRoot(self.bytes, offset, offset + admission.block_size);
                cached.root_pages[slot] = page_index;
            }
        } else try receipt.verifyRoot(self.bytes, start, end);
    }

    fn recordCached(self: Map, index: usize, cache: ?*Cache) !Record {
        if (index >= self.pages) return error.InvalidFlatDirectory;
        try self.verifyRoot(cache, 80 + index * 4, 80 + (index + 2) * 4);
        const start: usize = std.mem.readInt(u32, self.bytes[80 + index * 4 ..][0..4], .little);
        const end: usize = std.mem.readInt(u32, self.bytes[80 + (index + 1) * 4 ..][0..4], .little);
        if (start > end or end > self.page_data - self.records or end - start < 10) return error.InvalidFlatDirectory;
        try self.verifyRoot(cache, self.records + start, self.records + end);
        const bytes = self.bytes[self.records + start .. self.records + end];
        const size: usize = std.mem.readInt(u16, bytes[0..2], .little);
        if (size == 0 or size > max_key or size + 10 != bytes.len) return error.InvalidFlatDirectory;
        if (bytes[2] != 1 and bytes[2] != 2) return error.InvalidFlatDirectory;
        const offset = std.mem.readInt(u64, bytes[size + 2 ..][0..8], .little);
        if (offset > self.file_size - self.page_data or offset % 4096 != 0) return error.InvalidFlatDirectory;
        return .{ .key = bytes[2 .. 2 + size], .offset = @intCast(offset) };
    }

    fn extent(self: Map, index: usize) !Extent {
        return self.extentCached(index, null);
    }

    fn extentCached(self: Map, index: usize, cache: ?*Cache) !Extent {
        const start = (try self.recordCached(index, cache)).offset;
        const end = if (index + 1 < self.pages) (try self.recordCached(index + 1, cache)).offset else self.file_size - self.page_data;
        if (end < start or end - start < normal_page or end - start > max_extent or (index == 0 and start != 0)) return error.InvalidFlatDirectory;
        return .{ .start = self.page_data + start, .end = self.page_data + end };
    }

    fn page(self: Map, index: usize) !Page {
        const bounds = try self.extent(index);
        return parsePage(self.bytes[bounds.start..bounds.end]);
    }

    fn parsePage(bytes: []const u8) !Page {
        const count: usize = std.mem.readInt(u32, bytes[0..4], .little);
        const key_end: usize = std.mem.readInt(u32, bytes[4..8], .little);
        const used: usize = std.mem.readInt(u32, bytes[8..12], .little);
        if (count == 0 or count > 4096 or (bytes.len > normal_page and count != 1) or std.mem.readInt(u32, bytes[12..16], .little) != 0) return error.InvalidFlatDirectory;
        const key_start = 16 + 2 * ((count + 15) / 16);
        if (key_start > key_end or key_end > used or used > bytes.len or key_end > 65535 or std.mem.readInt(u16, bytes[16..18], .little) != key_start) return error.InvalidFlatDirectory;
        return .{ .bytes = bytes, .count = count, .key_end = key_end, .used = used };
    }

    fn checkPage(current: Page, root_key: []const u8, next_key: ?[]const u8) !void {
        var previous: ?[]const u8 = null;
        for (0..(current.count + 15) / 16) |i| {
            const key = try firstKey(try group(current, i));
            if (i == 0 and !std.mem.eql(u8, root_key, key)) return error.InvalidFlatDirectory;
            if (previous) |prev| if (std.mem.order(u8, prev, key) != .lt) return error.InvalidFlatDirectory;
            previous = key;
        }
        if (next_key) |next| if (std.mem.order(u8, previous.?, next) != .lt) return error.InvalidFlatDirectory;
    }

    fn group(current: Page, index: usize) ![]const u8 {
        const count = (current.count + 15) / 16;
        if (index >= count) return error.InvalidFlatDirectory;
        const start: usize = std.mem.readInt(u16, current.bytes[16 + index * 2 ..][0..2], .little);
        const end: usize = if (index + 1 < count) std.mem.readInt(u16, current.bytes[18 + index * 2 ..][0..2], .little) else current.key_end;
        if (start < 16 + 2 * count or start > end or end > current.key_end or end - start < 4) return error.InvalidFlatDirectory;
        return current.bytes[start..end];
    }

    fn firstKey(blob: []const u8) ![]const u8 {
        var pos: usize = 0;
        if (try readVarint(blob, &pos) != 0) return error.InvalidFlatBlock;
        const length = try readVarint(blob, &pos);
        if (length == 0 or blob.len - pos < 2 or blob.len - pos - 2 < length) return error.InvalidFlatBlock;
        if (blob[pos + 2] != 1 and blob[pos + 2] != 2) return error.InvalidFlatBlock;
        return blob[pos + 2 ..][0..length];
    }

    fn posting(current: Page, offset: usize) ![]const u8 {
        if (offset < current.key_end or offset > current.used or current.used - offset < 4) return error.InvalidFlatPosting;
        const size: usize = std.mem.readInt(u32, current.bytes[offset..][0..4], .little);
        if (size > current.used - offset - 4) return error.InvalidFlatPosting;
        const blob = current.bytes[offset + 4 ..][0..size];
        return blob;
    }

    pub fn findTagged(self: Map, arena: std.mem.Allocator, tag: Namespace, key: []const u8) !?[]const u8 {
        return self.findTaggedCached(arena, tag, key, null);
    }

    pub fn findTaggedCached(self: Map, arena: std.mem.Allocator, tag: Namespace, key: []const u8, cache: ?*Cache) !?[]const u8 {
        if (key.len > max_key - 1) return error.FlatKeyTooLong;
        var tagged: [max_key]u8 = undefined;
        tagged[0] = @backingInt(tag);
        @memcpy(tagged[1..][0..key.len], key);
        return self.findCached(arena, tagged[0 .. key.len + 1], cache);
    }

    pub fn find(self: Map, arena: std.mem.Allocator, target: []const u8) !?[]const u8 {
        return self.findCached(arena, target, null);
    }

    fn pageIndex(self: Map, target: []const u8, cache: ?*Cache) !?usize {
        var low: usize = 0;
        var high = self.pages;
        while (low < high) {
            const mid = low + (high - low) / 2;
            const item = try self.recordCached(mid, cache);
            if (std.mem.order(u8, item.key, target) != .gt) low = mid + 1 else high = mid;
        }
        if (low == 0) return null;
        return low - 1;
    }

    pub fn prefetchActors(self: Map, dids: []const []const u8, cache: ?*Cache) !void {
        if (@import("builtin").os.tag != .linux) return;
        const fd = self.fd orelse return;
        var seen: [50]usize = undefined;
        var count: usize = 0;
        for (dids[0..@min(dids.len, seen.len)]) |did| {
            if (did.len > max_key - 1) return error.FlatKeyTooLong;
            var tagged: [max_key]u8 = undefined;
            tagged[0] = @backingInt(Namespace.actor);
            @memcpy(tagged[1..][0..did.len], did);
            const index = (try self.pageIndex(tagged[0 .. did.len + 1], cache)) orelse continue;
            if (cache) |cached| if (cached.pages[index % cached.pages.len] == index) continue;
            if (std.mem.indexOfScalar(usize, seen[0..count], index) != null) continue;
            const bounds = try self.extentCached(index, cache);
            if (bounds.end - bounds.start != normal_page) continue;
            seen[count] = index;
            count += 1;
            _ = posix_fadvise(fd, @intCast(bounds.start), normal_page, std.os.linux.POSIX_FADV.WILLNEED);
        }
    }

    fn findCached(self: Map, arena: std.mem.Allocator, target: []const u8, cache: ?*Cache) !?[]const u8 {
        const page_index = (try self.pageIndex(target, cache)) orelse return null;
        const bounds = try self.extentCached(page_index, cache);
        var scratch: [2 * normal_page]u8 = undefined;
        var temporary: ?[]u8 = null;
        defer if (temporary) |buffer| std.heap.page_allocator.free(buffer);
        const bytes = if (self.fd) |fd| blk: {
            const size = bounds.end - bounds.start;
            if (size == normal_page) {
                if (cache) |cached| break :blk try cached.read(fd, page_index, bounds.start);
            }
            const buffer = if (size <= scratch.len) scratch[0..size] else allocation: {
                temporary = try std.heap.page_allocator.alloc(u8, size);
                break :allocation temporary.?;
            };
            try readExact(fd, buffer, bounds.start);
            break :blk buffer;
        } else self.bytes[bounds.start..bounds.end];
        if (self.admitted) |receipt| try receipt.verify(bytes, bounds.start);
        const current = try parsePage(bytes);
        if (!std.mem.eql(u8, (try self.recordCached(page_index, cache)).key, try firstKey(try group(current, 0)))) return error.InvalidFlatDirectory;
        const groups = (current.count + 15) / 16;
        var low: usize = 0;
        var high = groups;
        while (low < high) {
            const mid = low + (high - low) / 2;
            const key = try firstKey(try group(current, mid));
            if (std.mem.order(u8, key, target) != .gt) low = mid + 1 else high = mid;
        }
        if (low == 0) return null;
        const index = low - 1;
        const blob = try group(current, index);
        const first = try firstKey(blob);
        const count = @min(16, current.count - 16 * index);
        var key: [max_key]u8 = undefined;
        var key_size: usize = 0;
        var pos: usize = 0;
        var found: ?usize = null;
        for (0..count) |i| {
            const shared = try readVarint(blob, &pos);
            const suffix = try readVarint(blob, &pos);
            if (shared > key_size or shared + suffix > key.len or blob.len - pos < 2 or blob.len - pos - 2 < suffix) return error.InvalidFlatBlock;
            const offset: usize = std.mem.readInt(u16, blob[pos..][0..2], .little);
            if (offset < current.key_end or offset > current.used or current.used - offset < 4) return error.InvalidFlatPosting;
            pos += 2;
            if (i > 0 and std.mem.order(u8, key[shared..key_size], blob[pos..][0..suffix]) != .lt) return error.InvalidFlatBlock;
            @memcpy(key[shared..][0..suffix], blob[pos..][0..suffix]);
            key_size = shared + suffix;
            if (key_size == 0 or (key[0] != 1 and key[0] != 2)) return error.InvalidFlatBlock;
            pos += suffix;
            if (i == 0 and !std.mem.eql(u8, key[0..key_size], first)) return error.InvalidFlatBlock;
            if (std.mem.eql(u8, key[0..key_size], target)) found = offset;
        }
        if (pos != blob.len) return error.InvalidFlatBlock;
        const next_key: ?[]const u8 = if (index + 1 < groups) try firstKey(try group(current, index + 1)) else if (page_index + 1 < self.pages) (try self.record(page_index + 1)).key else null;
        if (next_key) |boundary| if (std.mem.order(u8, key[0..key_size], boundary) != .lt) return error.InvalidFlatBlock;
        const offset = found orelse return null;
        const value = try posting(current, offset);
        return if (self.fd != null) try arena.dupe(u8, value) else value;
    }
    pub fn open(allocator: std.mem.Allocator, path: []const u8, expected: []const u8) !Map {
        const full = try std.fmt.allocPrint(allocator, "{s}.prefix", .{path});
        defer allocator.free(full);
        return openFile(allocator, full, expected);
    }

    pub fn openFile(allocator: std.mem.Allocator, path: []const u8, expected: []const u8) !Map {
        return openFileMode(allocator, path, expected, .verify);
    }

    pub fn admitFile(allocator: std.mem.Allocator, path: []const u8, expected: []const u8) !Map {
        const verified = try openFileMode(allocator, path, expected, .admit);
        verified.close();
        return openAdmittedFile(allocator, path, expected);
    }

    pub fn openAdmittedFile(allocator: std.mem.Allocator, path: []const u8, expected: []const u8) !Map {
        return openFileMode(allocator, path, expected, .cached);
    }

    fn openFileMode(allocator: std.mem.Allocator, path: []const u8, expected: []const u8, mode: enum { verify, admit, cached }) !Map {
        const zpath = try allocator.allocSentinel(u8, path.len, 0);
        @memcpy(zpath, path);
        defer allocator.free(zpath);
        const fd = c.open(zpath.ptr, .{});
        if (fd < 0) return error.FlatOpenFailed;
        errdefer _ = c.close(fd);
        const before = if (mode == .admit) try admission.identity(fd, expected) else undefined;
        const receipt: ?admission.Admission = if (mode == .cached) try admission.Admission.open(allocator, path, fd, expected) else null;
        errdefer if (receipt) |value| value.close();
        const length = c.lseek(fd, 0, c.SEEK.END);
        if (length < 84) return error.InvalidFlatHeader;
        const size: usize = std.math.cast(usize, length) orelse return error.FlatTooLarge;
        var header: [80]u8 = undefined;
        try readExact(fd, &header, 0);
        if (!std.mem.eql(u8, header[0..8], "FLATPX05") or std.mem.readInt(u32, header[8..12], .little) != 5) return error.InvalidFlatHeader;
        if (std.mem.readInt(u64, header[16..24], .little) != 0) return error.InvalidFlatVersion;
        for ([_]usize{ 48, 56, 64, 72 }) |position| if (std.mem.readInt(u64, header[position..][0..8], .little) != 0) return error.InvalidFlatVersion;
        const pages: u64 = std.mem.readInt(u32, header[12..16], .little);
        const records64 = std.mem.readInt(u64, header[24..32], .little);
        const root_size64 = std.mem.readInt(u64, header[32..40], .little);
        if (std.mem.readInt(u64, header[40..48], .little) != size or size % 4096 != 0 or records64 != 80 + 4 * (pages + 1) or root_size64 < records64 or root_size64 > size or root_size64 % 4096 != 0) return error.InvalidFlatSections;
        if (pages > (size - root_size64) / normal_page or (pages == 0 and root_size64 != size)) return error.InvalidFlatSections;
        var terminal_bytes: [4]u8 = undefined;
        try readExact(fd, &terminal_bytes, @intCast(records64 - 4));
        const terminal: u64 = std.mem.readInt(u32, &terminal_bytes, .little);
        if (terminal < pages * 10 or terminal > pages * (max_key + 10) or terminal > root_size64 - records64) return error.InvalidFlatDirectory;
        if ((records64 + terminal + 4095) / 4096 * 4096 != root_size64) return error.InvalidFlatSections;
        const root_size: usize = @intCast(root_size64);
        if (mode == .cached) {
            var mapping = try std.Io.File.MemoryMap.create(std.Options.debug_io, .{ .handle = fd, .flags = .{ .nonblocking = false } }, .{
                .len = root_size,
                .protection = .{ .read = true, .write = false },
                .populate = root_size <= admission.metadata_prefault_limit,
            });
            errdefer mapping.destroy(std.Options.debug_io);
            try receipt.?.verifyRoot(mapping.memory, 0, 84);
            try receipt.?.verifyRoot(mapping.memory, @intCast(records64 - 4), @intCast(records64));
            if (@import("builtin").os.tag == .linux) {
                if (posix_fadvise(fd, 0, 0, std.os.linux.POSIX_FADV.RANDOM) != 0) return error.FlatAdviceFailed;
            }
            return .{
                .bytes = mapping.memory,
                .file_size = size,
                .fd = fd,
                .mapping = mapping,
                .admitted = receipt,
                .pages = @intCast(pages),
                .records = @intCast(records64),
                .page_data = root_size,
            };
        }
        const roots = try allocator.alloc(u8, root_size);
        errdefer allocator.free(roots);
        try readExact(fd, roots, 0);
        var result = try validateRoot(roots, size);
        const hashes = if (mode == .admit) try allocator.alloc(u8, size / admission.block_size * 32) else null;
        defer if (hashes) |bytes| allocator.free(bytes);
        if (hashes) |bytes| admission.record(bytes, roots, 0);
        var hasher = std.crypto.hash.sha2.Sha256.init(.{});
        hasher.update(roots);
        var buffer = try allocator.alloc(u8, @min(validation_read_size, @max(normal_page, size - root_size)));
        defer allocator.free(buffer);
        var buffered_start: usize = 0;
        var buffered_len: usize = 0;
        for (0..result.pages) |i| {
            const bounds = try result.extent(i);
            const extent_size = bounds.end - bounds.start;
            if (extent_size > buffer.len) {
                buffer = try allocator.realloc(buffer, extent_size);
                buffered_len = 0;
            }
            if (bounds.start < buffered_start or bounds.end > buffered_start + buffered_len) {
                buffered_start = bounds.start;
                buffered_len = @min(buffer.len, size - buffered_start);
                try readExact(fd, buffer[0..buffered_len], buffered_start);
            }
            const chunk = buffer[bounds.start - buffered_start .. bounds.end - buffered_start];
            const current = try parsePage(chunk);
            const root = try result.record(i);
            const next_key: ?[]const u8 = if (i + 1 < result.pages) (try result.record(i + 1)).key else null;
            try checkPage(current, root.key, next_key);
            hasher.update(chunk);
            if (hashes) |bytes| admission.record(bytes, chunk, bounds.start);
        }
        var digest: [32]u8 = undefined;
        hasher.final(&digest);
        const hex = std.fmt.bytesToHex(digest, .lower);
        if (!std.mem.eql(u8, &hex, expected)) return error.FlatDigestMismatch;
        if (hashes) |bytes| try admission.publish(allocator, path, fd, before, expected, bytes);
        if (@import("builtin").os.tag == .linux) {
            if (posix_fadvise(fd, 0, 0, std.os.linux.POSIX_FADV.RANDOM) != 0) return error.FlatAdviceFailed;
        }
        result.fd = fd;
        result.owner = allocator;
        return result;
    }

    pub fn actorFenceKeys(self: Map, arena: std.mem.Allocator, limit: usize) ![]const []const u8 {
        if (limit == 0 or limit > 200000) return error.InvalidCaptureLimit;
        var keys: std.ArrayListUnmanaged([]const u8) = .empty;
        for (0..@min(self.pages, limit)) |index| {
            const root = try self.record(index);
            if (root.key[0] != @backingInt(Namespace.actor)) break;
            if (root.key.len < 2) return error.InvalidFlatDirectory;
            try keys.append(arena, try arena.dupe(u8, root.key[1..]));
        }
        return keys.toOwnedSlice(arena);
    }

    pub fn close(self: Map) void {
        if (self.admitted) |receipt| receipt.close();
        if (self.mapping) |value| {
            var mapping = value;
            mapping.destroy(std.Options.debug_io);
        }
        if (self.fd) |fd| _ = c.close(fd);
        if (self.owner) |allocator| allocator.free(self.bytes);
    }
};

fn readExact(fd: c.fd_t, buffer: []u8, offset: usize) !void {
    var read: usize = 0;
    while (read < buffer.len) {
        const position = std.math.cast(c.off_t, offset + read) orelse return error.FlatTooLarge;
        const count = c.pread(fd, buffer[read..].ptr, buffer.len - read, position);
        if (count < 0) {
            if (c.errno(count) == .INTR) continue;
            return error.FlatReadFailed;
        }
        if (count == 0) return error.FlatTruncatedRead;
        read += @intCast(count);
    }
}

fn readVarint(blob: []const u8, pos: *usize) !usize {
    var value: usize = 0;
    inline for (.{ 0, 7, 14 }) |shift| {
        if (pos.* >= blob.len) return error.InvalidFlatBlock;
        const byte = blob[pos.*];
        pos.* += 1;
        value |= @as(usize, byte & 127) << shift;
        if (byte & 128 == 0) {
            if (value > max_key or (shift > 0 and byte == 0)) return error.InvalidFlatBlock;
            return value;
        }
    }
    return error.InvalidFlatBlock;
}
fn digestOf(bytes: []const u8) [64]u8 {
    var digest: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(bytes, &digest, .{});
    return std.fmt.bytesToHex(digest, .lower);
}

test "unified namespaces retain opaque empty and non posting values" {
    const bytes = @embedFile("unified_fixture.bin");
    const digest = digestOf(bytes);
    const map = try Map.validate(bytes, &digest);
    try std.testing.expectEqualSlices(u8, &.{0}, (try map.findTagged(std.testing.allocator, .actor, "same")).?);
    try std.testing.expectEqualSlices(u8, &.{255}, (try map.findTagged(std.testing.allocator, .prefix, "same")).?);
    try std.testing.expectEqual(@as(usize, 0), (try map.findTagged(std.testing.allocator, .actor, "東京")).?.len);
    try std.testing.expect((try map.findTagged(std.testing.allocator, .prefix, "東京")) == null);
    var invalid = bytes.*;
    invalid[map.records + 2] = 3;
    const invalid_digest = digestOf(&invalid);
    try std.testing.expectError(error.InvalidFlatDirectory, Map.validate(&invalid, &invalid_digest));
}

test "paged fences preserve all postings and boundary misses" {
    const fixture = @embedFile("flat_prefix_fixture.bin");
    const digest = digestOf(fixture);
    const map = try Map.validate(fixture, &digest);
    for (0..3000) |i| {
        var buf: [32]u8 = undefined;
        const key = try std.fmt.bufPrint(&buf, "n:key{d:0>8}", .{i});
        const blob = (try map.findTagged(std.testing.allocator, .prefix, key)) orelse return error.TestExpectedMatch;
        try std.testing.expectEqual(@as(u32, @intCast(0x80000000 + i)), std.mem.readInt(u32, blob[3..7], .little));
        try std.testing.expectEqualStrings("did", blob[9..]);
    }
    var long_key: [8152]u8 = @splat('x');
    @memcpy(long_key[0..2], "z:");
    try std.testing.expectEqualSlices(u8, &.{ 1, 0, 0 }, (try map.findTagged(std.testing.allocator, .prefix, &long_key)).?);
    try std.testing.expect((try map.findTagged(std.testing.allocator, .prefix, "東京")) != null);
    for ([_][]const u8{ "", "a", "n:key", "n:key00000000x", "n:key00003000", "zzzz", "東京x" }) |key| try std.testing.expect((try map.findTagged(std.testing.allocator, .prefix, key)) == null);
}

test "unified oversized posting and maximum key preserve raw bytes" {
    const fixture = @embedFile("flat_prefix_fixture.bin");
    const digest = digestOf(fixture);
    const map = try Map.validate(fixture, &digest);
    try std.testing.expectEqual(@as(usize, 20009), (try map.findTagged(std.testing.allocator, .prefix, "zlarge")).?.len);
    var key: [8183]u8 = @splat('x');
    @memcpy(key[0..2], "y:");
    const blob = (try map.findTagged(std.testing.allocator, .prefix, &key)).?;
    try std.testing.expectEqual(@as(usize, 80015), blob.len);
    try std.testing.expectEqual(@as(u16, 2), std.mem.readInt(u16, blob[1..3], .little));
    try std.testing.expectEqual(@as(u16, 40000), std.mem.readInt(u16, blob[7..9], .little));
}

test "unified rejects malformed restart posting offset and length" {
    const fixture = @embedFile("flat_prefix_fixture.bin");
    const digest = digestOf(fixture);
    var length: usize = 0;
    while (length < fixture.len) : (length += 127) {
        if (Map.validate(fixture[0..length], &digest)) |_| return error.TestUnexpectedSuccess else |_| {}
    }
    const original = try Map.validate(fixture, &digest);
    const current = try original.page(0);
    const page_start = @intFromPtr(current.bytes.ptr) - @intFromPtr(fixture.ptr);
    var bad = fixture.*;
    std.mem.writeInt(u16, bad[page_start + 16 ..][0..2], 65535, .little);
    var hash = digestOf(&bad);
    try std.testing.expectError(error.InvalidFlatDirectory, Map.validate(&bad, &hash));
    bad = fixture.*;
    const blob = try Map.group(current, 0);
    var pos: usize = 0;
    _ = try readVarint(blob, &pos);
    _ = try readVarint(blob, &pos);
    const offset_pos = @intFromPtr(blob.ptr) - @intFromPtr(fixture.ptr) + pos;
    std.mem.writeInt(u16, bad[offset_pos..][0..2], 0, .little);
    hash = digestOf(&bad);
    var map = try Map.validate(&bad, &hash);
    try std.testing.expectError(error.InvalidFlatPosting, map.findTagged(std.testing.allocator, .prefix, "n:key00000000"));
    bad = fixture.*;
    const posting_offset = std.mem.readInt(u16, fixture[offset_pos..][0..2], .little);
    std.mem.writeInt(u32, bad[page_start + posting_offset ..][0..4], 0xffffffff, .little);
    hash = digestOf(&bad);
    map = try Map.validate(&bad, &hash);
    try std.testing.expectError(error.InvalidFlatPosting, map.findTagged(std.testing.allocator, .prefix, "n:key00000000"));
    bad = fixture.*;
    bad[bad.len - 1] ^= 1;
    try std.testing.expectError(error.FlatDigestMismatch, Map.validate(&bad, &digest));
}

test "unified cross restart order and empty map" {
    const fixture = @embedFile("flat_prefix_fixture.bin");
    const digest = digestOf(fixture);
    const original = try Map.validate(fixture, &digest);
    const blob = try Map.group(try original.page(0), 0);
    var pos: usize = 0;
    var suffix_start: usize = 0;
    var suffix_size: usize = 0;
    for (0..16) |_| {
        _ = try readVarint(blob, &pos);
        suffix_size = try readVarint(blob, &pos);
        pos += 2;
        suffix_start = pos;
        pos += suffix_size;
    }
    try std.testing.expect(suffix_size > 0);
    var bad = fixture.*;
    bad[@intFromPtr(blob.ptr) - @intFromPtr(fixture.ptr) + suffix_start + suffix_size - 1] = '9';
    const hash = digestOf(&bad);
    const map = try Map.validate(&bad, &hash);
    try std.testing.expectError(error.InvalidFlatBlock, map.findTagged(std.testing.allocator, .prefix, "n:key00000000"));
    var bytes: [4096]u8 = @splat(0);
    @memcpy(bytes[0..8], "FLATPX05");
    std.mem.writeInt(u32, bytes[8..12], 5, .little);
    std.mem.writeInt(u64, bytes[24..32], 84, .little);
    for ([_]usize{ 32, 40 }) |offset| std.mem.writeInt(u64, bytes[offset..][0..8], 4096, .little);
    const empty_digest = digestOf(&bytes);
    const empty = try Map.validate(&bytes, &empty_digest);
    try std.testing.expect((try empty.findTagged(std.testing.allocator, .prefix, "anything")) == null);
}

extern "c" fn mkstemp(template: [*:0]u8) c_int;

test "admission binds the opened inode and rejects modified data and receipts" {
    const fixture = @embedFile("flat_prefix_fixture.bin");
    const digest = digestOf(fixture);
    var path = "/tmp/typeahead-admission-XXXXXX".*;
    const writer = mkstemp(&path);
    if (writer < 0) return error.TestFileCreateFailed;
    defer _ = c.close(writer);
    defer _ = c.unlink(&path);
    try writeTestFile(writer, fixture);
    const receipt_path = try std.fmt.allocPrintSentinel(std.testing.allocator, "{s}.admitted", .{path}, 0);
    defer std.testing.allocator.free(receipt_path);
    defer _ = c.unlink(receipt_path);
    try std.testing.expectError(error.SnapshotNotAdmitted, Map.openAdmittedFile(std.testing.allocator, &path, &digest));
    try std.testing.expectError(error.FlatDigestMismatch, Map.admitFile(std.testing.allocator, &path, "0000000000000000000000000000000000000000000000000000000000000000"));
    try std.testing.expectError(error.SnapshotNotAdmitted, Map.openAdmittedFile(std.testing.allocator, &path, &digest));
    const first = try Map.admitFile(std.testing.allocator, &path, &digest);
    first.close();
    try std.testing.expectError(error.StaleAdmission, Map.openAdmittedFile(std.testing.allocator, &path, "0000000000000000000000000000000000000000000000000000000000000000"));
    {
        const cached = try Map.openAdmittedFile(std.testing.allocator, &path, &digest);
        defer cached.close();
        const root = try cached.record(0);
        const bounds = try cached.extent(0);
        var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
        defer arena.deinit();
        try std.testing.expect((try cached.find(arena.allocator(), root.key)) != null);
        const cache = try std.testing.allocator.create(Cache);
        defer std.testing.allocator.destroy(cache);
        cache.clear();
        try std.testing.expect((try cached.findCached(arena.allocator(), root.key, cache)) != null);
        const changed_header: [1]u8 = .{fixture[72] ^ 1};
        try std.testing.expectEqual(@as(isize, 1), c.pwrite(writer, &changed_header, 1, 72));
        cache.beginQuery();
        try std.testing.expectError(error.FlatBlockChecksumMismatch, cached.findCached(arena.allocator(), root.key, cache));
        try std.testing.expectEqual(@as(isize, 1), c.pwrite(writer, fixture[72..].ptr, 1, 72));
        cache.beginQuery();
        const offset = bounds.start + 4096;
        const changed: [1]u8 = .{fixture[offset] ^ 1};
        try std.testing.expectEqual(@as(isize, 1), c.pwrite(writer, &changed, 1, @intCast(offset)));
        try std.testing.expectError(error.FlatBlockChecksumMismatch, cached.find(arena.allocator(), root.key));
        try std.testing.expectError(error.StaleAdmission, Map.openAdmittedFile(std.testing.allocator, &path, &digest));
        try std.testing.expectEqual(@as(isize, 1), c.pwrite(writer, fixture[offset..].ptr, 1, @intCast(offset)));
    }
    const renewed = try Map.admitFile(std.testing.allocator, &path, &digest);
    renewed.close();
    {
        const receipt_fd = c.open(receipt_path, .{ .ACCMODE = .RDWR });
        if (receipt_fd < 0) return error.TestFileOpenFailed;
        defer _ = c.close(receipt_fd);
        var original: [1]u8 = undefined;
        try readExact(receipt_fd, &original, admission.header_size);
        const changed: [1]u8 = .{original[0] ^ 1};
        try std.testing.expectEqual(@as(isize, 1), c.pwrite(receipt_fd, &changed, 1, admission.header_size));
        try std.testing.expectError(error.FlatBlockChecksumMismatch, Map.openAdmittedFile(std.testing.allocator, &path, &digest));
        try std.testing.expect(c.ftruncate(receipt_fd, admission.header_size) == 0);
        try std.testing.expectError(error.InvalidAdmission, Map.openAdmittedFile(std.testing.allocator, &path, &digest));
    }
    const restored = try Map.admitFile(std.testing.allocator, &path, &digest);
    restored.close();
    var replacement_path = "/tmp/typeahead-replacement-XXXXXX".*;
    const replacement = mkstemp(&replacement_path);
    if (replacement < 0) return error.TestFileCreateFailed;
    defer _ = c.close(replacement);
    defer _ = c.unlink(&replacement_path);
    try writeTestFile(replacement, fixture);
    try std.testing.expect(c.rename(&replacement_path, &path) == 0);
    try std.testing.expectError(error.StaleAdmission, Map.openAdmittedFile(std.testing.allocator, &path, &digest));
}

fn writeTestFile(fd: c.fd_t, bytes: []const u8) !void {
    var offset: usize = 0;
    while (offset < bytes.len) {
        const count = c.write(fd, bytes[offset..].ptr, bytes.len - offset);
        if (count <= 0) return error.TestFileWriteFailed;
        offset += @intCast(count);
    }
}

extern "c" fn mkdtemp(template: [*:0]u8) ?[*:0]u8;

test "admission survives atomic generation directory publication" {
    const io = std.Options.debug_io;
    const allocator = std.testing.allocator;
    const fixture = @embedFile("flat_prefix_fixture.bin");
    const digest = digestOf(fixture);
    var directory = "/tmp/typeahead-admitted-dir-XXXXXX".*;
    _ = mkdtemp(&directory) orelse return error.TestDirectoryFailed;
    defer std.Io.Dir.deleteTree(.cwd(), io, &directory) catch {};
    const path = try std.fmt.allocPrint(allocator, "{s}/index.db.prefix", .{directory});
    defer allocator.free(path);
    {
        const writer = try std.Io.Dir.createFileAbsolute(io, path, .{});
        defer writer.close(io);
        try writer.writeStreamingAll(io, fixture);
    }
    const admitted = try Map.admitFile(allocator, path, &digest);
    admitted.close();
    const published = try std.fmt.allocPrint(allocator, "{s}-published", .{directory});
    defer allocator.free(published);
    defer std.Io.Dir.deleteTree(.cwd(), io, published) catch {};
    try std.Io.Dir.renameAbsolute(&directory, published, io);
    const published_path = try std.fmt.allocPrint(allocator, "{s}/index.db.prefix", .{published});
    defer allocator.free(published_path);
    const recovered = try Map.openAdmittedFile(allocator, published_path, &digest);
    defer recovered.close();
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const blob = (try recovered.findTagged(arena.allocator(), .prefix, "n:key00000000")).?;
    try std.testing.expectEqualStrings("did", blob[9..]);
}

test "pread real file owns root closes fd and keeps request bytes alive" {
    const fixture = @embedFile("flat_prefix_fixture.bin");
    const digest = digestOf(fixture);
    var path = "/tmp/typeahead-local16pread-XXXXXX".*;
    const writer = mkstemp(&path);
    if (writer < 0) return error.TestFileCreateFailed;
    defer _ = c.close(writer);
    defer _ = c.unlink(&path);
    var written: usize = 0;
    while (written < fixture.len) {
        const n = c.write(writer, fixture[written..].ptr, fixture.len - written);
        if (n <= 0) return error.TestFileWriteFailed;
        written += @intCast(n);
    }
    var forged_root: [8]u8 = undefined;
    std.mem.writeInt(u64, &forged_root, fixture.len - 4096, .little);
    try std.testing.expectEqual(@as(isize, 8), c.pwrite(writer, &forged_root, 8, 32));
    try std.testing.expectError(error.InvalidFlatSections, Map.openFile(std.testing.allocator, &path, &digest));
    try std.testing.expectEqual(@as(isize, 8), c.pwrite(writer, fixture[32..40].ptr, 8, 32));
    try std.testing.expectError(error.FlatDigestMismatch, Map.openFile(std.testing.allocator, &path, "wrong"));
    for (0..16) |iteration| {
        const map = try Map.openFile(std.testing.allocator, &path, &digest);
        var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
        defer arena.deinit();
        const blob = (try map.findTagged(arena.allocator(), .prefix, "n:key00000000")).?;
        _ = try map.findTagged(arena.allocator(), .prefix, "n:key00000001");
        try std.testing.expectEqual(@as(u32, 0x80000000), std.mem.readInt(u32, blob[3..7], .little));
        var large: ?[]const u8 = null;
        if (iteration == 0) {
            var long_key: [8183]u8 = @splat('x');
            @memcpy(long_key[0..2], "y:");
            large = (try map.findTagged(arena.allocator(), .prefix, &long_key)).?;
            try std.testing.expectEqual(@as(usize, 80015), large.?.len);
        }
        const cache = try std.testing.allocator.create(Cache);
        defer std.testing.allocator.destroy(cache);
        cache.clear();
        for (0..2) |_| {
            for (0..64) |number| {
                var key_buffer: [32]u8 = undefined;
                const key = try std.fmt.bufPrint(&key_buffer, "n:key{d:0>8}", .{number});
                const expected = (try map.findTagged(arena.allocator(), .prefix, key)).?;
                const actual = (try map.findTaggedCached(arena.allocator(), .prefix, key, cache)).?;
                try std.testing.expectEqualSlices(u8, expected, actual);
            }
        }
        const fd = map.fd.?;
        map.close();
        if (large) |value| {
            try std.testing.expectEqual(@as(u16, 2), std.mem.readInt(u16, value[1..3], .little));
            try std.testing.expectEqual(@as(u8, 'x'), value[value.len - 1]);
        }
        try std.testing.expectEqualStrings("did", blob[9..]);
        try std.testing.expectEqual(@as(c_int, -1), c.close(fd));
    }
    const map = try Map.openFile(std.testing.allocator, &path, &digest);
    defer map.close();
    const receipt_path = try std.fmt.allocPrintSentinel(std.testing.allocator, "{s}.admitted", .{path}, 0);
    defer std.testing.allocator.free(receipt_path);
    defer _ = c.unlink(receipt_path);
    const admitted = try Map.admitFile(std.testing.allocator, &path, &digest);
    defer admitted.close();
    try std.testing.expect(admitted.mapping != null);
    for (0..64) |number| {
        var key_buffer: [32]u8 = undefined;
        const key = try std.fmt.bufPrint(&key_buffer, "n:key{d:0>8}", .{number});
        var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
        defer arena.deinit();
        const expected = (try map.findTagged(arena.allocator(), .prefix, key)).?;
        const actual = (try admitted.findTagged(arena.allocator(), .prefix, key)).?;
        try std.testing.expectEqualSlices(u8, expected, actual);
    }
    try std.testing.expect(c.ftruncate(writer, @intCast(map.page_data + 1)) == 0);
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    try std.testing.expectError(error.FlatTruncatedRead, map.findTagged(arena.allocator(), .prefix, "n:key00000000"));
    try std.testing.expectError(error.FlatTruncatedRead, admitted.findTagged(arena.allocator(), .prefix, "n:key00000000"));
}
