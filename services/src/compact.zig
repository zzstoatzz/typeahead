const std = @import("std");
const zqlite = @import("zqlite");
const c = std.c;
const a = std.heap.c_allocator;
const page_size = 8192;
const max_extent = 16 * 1024 * 1024;
const max_key = 8193;
const queries = [_][]const u8{
    "SELECT did,blob FROM actor_compact_base ORDER BY did",
    "SELECT key,dids FROM prefix_top_base ORDER BY key",
};

const Stats = extern struct {
    keys: u64 = 0,
    verified_keys: u64 = 0,
    pages: u64 = 0,
    oversized_pages: u64 = 0,
};

fn writeAll(fd: c.fd_t, bytes: []const u8) !void {
    var pos: usize = 0;
    while (pos < bytes.len) {
        const n = c.write(fd, bytes[pos..].ptr, bytes.len - pos);
        if (n < 0 and c.errno(n) == .INTR) continue;
        if (n <= 0) return error.WriteFailed;
        pos += @intCast(n);
    }
}

fn readAll(fd: c.fd_t, bytes: []u8, offset: usize) !void {
    var pos: usize = 0;
    while (pos < bytes.len) {
        const n = c.pread(fd, bytes[pos..].ptr, bytes.len - pos, @intCast(offset + pos));
        if (n < 0 and c.errno(n) == .INTR) continue;
        if (n <= 0) return error.TruncatedRead;
        pos += @intCast(n);
    }
}

fn put(comptime T: type, bytes: []u8, offset: usize, value: usize) void {
    std.mem.writeInt(T, bytes[offset..][0..@sizeOf(T)], @intCast(value), .little);
}

fn get(comptime T: type, bytes: []const u8, offset: usize) !usize {
    if (offset > bytes.len or bytes.len - offset < @sizeOf(T)) return error.TruncatedField;
    return @intCast(std.mem.readInt(T, bytes[offset..][0..@sizeOf(T)], .little));
}

fn varint(bytes: []u8, value: usize) usize {
    var v = value;
    var pos: usize = 0;
    while (v >= 128) : (pos += 1) {
        bytes[pos] = @as(u8, @intCast(v & 127)) | 128;
        v >>= 7;
    }
    bytes[pos] = @intCast(v);
    return pos + 1;
}

fn readVarint(bytes: []const u8, pos: *usize) !usize {
    var value: usize = 0;
    for ([_]u6{ 0, 7, 14 }) |shift| {
        if (pos.* >= bytes.len) return error.TruncatedVarint;
        const byte = bytes[pos.*];
        pos.* += 1;
        value |= @as(usize, byte & 127) << shift;
        if (byte & 128 == 0) {
            if (value > max_key) return error.KeyTooLong;
            return value;
        }
    }
    return error.InvalidVarint;
}

const Packer = struct {
    entries: [2 * max_key]u8 = undefined,
    entry_len: usize = 0,
    postings: []u8,
    page: []u8,
    posting_len: usize = 0,
    restarts: [4096]u16 = undefined,
    patches: [4096]struct { pos: u16, posting: u16 } = undefined,
    count: usize = 0,
    previous: [max_key]u8 = undefined,
    previous_len: usize = 0,
    first: [max_key]u8 = undefined,
    first_len: usize = 0,
    seen: std.StringHashMapUnmanaged(u16) = .empty,
    roots: std.ArrayList(u8) = .empty,
    offsets: std.ArrayList(u8) = .empty,
    bytes_written: usize = 0,
    stats: Stats = .{},
    fd: c.fd_t,

    fn init(fd: c.fd_t) !Packer {
        const postings = try a.alloc(u8, max_extent);
        errdefer a.free(postings);
        return .{ .fd = fd, .postings = postings, .page = try a.alloc(u8, max_extent) };
    }

    fn deinit(self: *Packer) void {
        a.free(self.postings);
        a.free(self.page);
        self.seen.deinit(a);
        self.roots.deinit(a);
        self.offsets.deinit(a);
    }

    fn add(self: *Packer, key: []const u8, blob: []const u8) !void {
        if (key.len == 0 or key.len > max_key or blob.len > max_extent) return error.SourceBounds;
        while (true) {
            var shared: usize = 0;
            if (self.count % 16 != 0) {
                while (shared < @min(key.len, self.previous_len) and key[shared] == self.previous[shared]) : (shared += 1) {}
            }
            var prefix: [6]u8 = undefined;
            var n = varint(&prefix, shared);
            n += varint(prefix[n..], key.len - shared);
            const entry_size = n + 2 + key.len - shared;
            const known = self.seen.get(blob);
            const posting_size = if (known == null) 4 + blob.len else 0;
            const used = 16 + 2 * ((self.count + 16) / 16) + self.entry_len + entry_size + self.posting_len + posting_size;
            if (self.count > 0 and used > page_size) {
                try self.flush();
                continue;
            }
            if (used > max_extent) return error.ExtentTooLarge;
            if (self.count == 0) {
                @memcpy(self.first[0..key.len], key);
                self.first_len = key.len;
            }
            if (self.count % 16 == 0) self.restarts[self.count / 16] = @intCast(self.entry_len);
            @memcpy(self.entries[self.entry_len..][0..n], prefix[0..n]);
            self.entry_len += n;
            const posting = known orelse blk: {
                const offset = self.posting_len;
                put(u32, self.postings, offset, blob.len);
                @memcpy(self.postings[offset + 4 ..][0..blob.len], blob);
                try self.seen.put(a, self.postings[offset + 4 ..][0..blob.len], @intCast(offset));
                self.posting_len += 4 + blob.len;
                break :blk @as(u16, @intCast(offset));
            };
            self.patches[self.count] = .{ .pos = @intCast(self.entry_len), .posting = posting };
            self.entry_len += 2;
            @memcpy(self.entries[self.entry_len..][0 .. key.len - shared], key[shared..]);
            self.entry_len += key.len - shared;
            @memcpy(self.previous[0..key.len], key);
            self.previous_len = key.len;
            self.count += 1;
            self.stats.keys += 1;
            if (used > page_size) try self.flush();
            return;
        }
    }

    fn flush(self: *Packer) !void {
        if (self.count == 0) return;
        const groups = (self.count + 15) / 16;
        const key_start = 16 + 2 * groups;
        const key_end = key_start + self.entry_len;
        const used = key_end + self.posting_len;
        const extent = @max(page_size, std.mem.alignForward(usize, used, 4096));
        if (extent > max_extent or (extent > page_size and self.count != 1)) return error.ExtentTooLarge;
        var header: [16 + 4096 * 2]u8 = @splat(0);
        put(u32, &header, 0, self.count);
        put(u32, &header, 4, key_end);
        put(u32, &header, 8, used);
        for (0..groups) |i| put(u16, &header, 16 + 2 * i, key_start + self.restarts[i]);
        for (self.patches[0..self.count]) |patch| put(u16, &self.entries, patch.pos, key_end + patch.posting);
        if (self.roots.items.len + self.first_len + 10 > std.math.maxInt(u32)) return error.DirectoryTooLarge;
        var offset: [4]u8 = undefined;
        put(u32, &offset, 0, self.roots.items.len);
        try self.offsets.appendSlice(a, &offset);
        var root: [max_key + 10]u8 = undefined;
        put(u16, &root, 0, self.first_len);
        @memcpy(root[2..][0..self.first_len], self.first[0..self.first_len]);
        put(u64, &root, self.first_len + 2, self.bytes_written);
        try self.roots.appendSlice(a, root[0 .. self.first_len + 10]);
        @memcpy(self.page[0..key_start], header[0..key_start]);
        @memcpy(self.page[key_start..key_end], self.entries[0..self.entry_len]);
        @memcpy(self.page[key_end..used], self.postings[0..self.posting_len]);
        @memset(self.page[used..extent], 0);
        try writeAll(self.fd, self.page[0..extent]);
        self.bytes_written += extent;
        self.stats.pages += 1;
        self.stats.oversized_pages += @intFromBool(extent > page_size);
        self.entry_len = 0;
        self.posting_len = 0;
        self.count = 0;
        self.previous_len = 0;
        self.seen.clearRetainingCapacity();
    }

    fn finish(self: *Packer, path: [*:0]const u8) !void {
        try self.flush();
        var terminal: [4]u8 = undefined;
        put(u32, &terminal, 0, self.roots.items.len);
        try self.offsets.appendSlice(a, &terminal);
        const records = 80 + self.offsets.items.len;
        const page_data = std.mem.alignForward(usize, records + self.roots.items.len, 4096);
        var header: [80]u8 = @splat(0);
        @memcpy(header[0..8], "FLATPX05");
        put(u32, &header, 8, 5);
        put(u32, &header, 12, self.stats.pages);
        put(u64, &header, 24, records);
        put(u64, &header, 32, page_data);
        put(u64, &header, 40, page_data + self.bytes_written);
        const fd = c.open(path, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true }, @as(c.mode_t, 0o600));
        if (fd < 0) return error.CreateFailed;
        defer _ = c.close(fd);
        try writeAll(fd, &header);
        try writeAll(fd, self.offsets.items);
        try writeAll(fd, self.roots.items);
        const zeros: [4096]u8 = @splat(0);
        try writeAll(fd, zeros[0 .. page_data - records - self.roots.items.len]);
        const buffer = try a.alloc(u8, 1024 * 1024);
        defer a.free(buffer);
        var pos: usize = 0;
        while (pos < self.bytes_written) {
            const chunk = buffer[0..@min(buffer.len, self.bytes_written - pos)];
            try readAll(self.fd, chunk, pos);
            try writeAll(fd, chunk);
            pos += chunk.len;
        }
    }
};

fn pack(source: [*:0]const u8, side: [*:0]const u8, scratch: [*:0]const u8) !Stats {
    const db = try zqlite.open(source, zqlite.OpenFlags.ReadOnly);
    defer db.close();
    try db.exec("BEGIN", .{});
    const fd = c.open(scratch, .{ .ACCMODE = .RDWR, .CREAT = true, .EXCL = true }, @as(c.mode_t, 0o600));
    if (fd < 0) return error.CreateFailed;
    defer _ = c.close(fd);
    var writer = try Packer.init(fd);
    defer writer.deinit();
    for (queries, 1..) |query, tag| {
        const stmt = try db.prepare(query);
        defer stmt.deinit();
        while (try stmt.step()) {
            const row: zqlite.Row = .{ .stmt = stmt };
            if (row.columnType(0) != .text or row.columnType(1) != .blob) return error.SourceType;
            const key = row.text(0);
            if (key.len >= max_key) return error.SourceBounds;
            var tagged: [max_key]u8 = undefined;
            tagged[0] = @intCast(tag);
            @memcpy(tagged[1..][0..key.len], key);
            try writer.add(tagged[0 .. key.len + 1], row.blob(1));
        }
    }
    try writer.finish(side);
    return writer.stats;
}

export fn typeahead_compact_pack(source: [*:0]const u8, side: [*:0]const u8, scratch: [*:0]const u8, stats: *Stats) ?[*:0]const u8 {
    stats.* = pack(source, side, scratch) catch |err| return @errorName(err);
    return null;
}

fn verify(source: [*:0]const u8, side: [*:0]const u8, digest: [*:0]const u8) !u64 {
    const map = try @import("index/flat_prefix.zig").Map.openFile(a, std.mem.span(side), std.mem.span(digest));
    defer map.close();
    const db = try zqlite.open(source, zqlite.OpenFlags.ReadOnly);
    defer db.close();
    try db.exec("BEGIN", .{});
    const actors = try db.prepare(queries[0]);
    defer actors.deinit();
    const prefixes = try db.prepare(queries[1]);
    defer prefixes.deinit();
    const statements = [_]zqlite.Stmt{ actors, prefixes };
    var tag: usize = 0;
    var count: u64 = 0;
    var buffer = try a.alloc(u8, page_size);
    defer a.free(buffer);
    var key: [max_key]u8 = undefined;
    var key_len: usize = 0;
    for (0..map.pages) |i| {
        const root_pos = map.records + try get(u32, map.bytes, 80 + 4 * i);
        const root_len = try get(u16, map.bytes, root_pos);
        const start = map.page_data + try get(u64, map.bytes, root_pos + 2 + root_len);
        const end = if (i + 1 < map.pages) blk: {
            const next = map.records + try get(u32, map.bytes, 84 + 4 * i);
            break :blk map.page_data + try get(u64, map.bytes, next + 2 + try get(u16, map.bytes, next));
        } else map.file_size;
        if (end - start > buffer.len) buffer = try a.realloc(buffer, end - start);
        const page = buffer[0 .. end - start];
        try readAll(map.fd.?, page, start);
        const entries = try get(u32, page, 0);
        const key_end = try get(u32, page, 4);
        const used = try get(u32, page, 8);
        var pos: usize = 16 + 2 * ((entries + 15) / 16);
        for (0..entries) |j| {
            if (j % 16 == 0) {
                key_len = 0;
                if (try get(u16, page, 16 + 2 * (j / 16)) != pos) return error.InvalidRestart;
            }
            const shared = try readVarint(page[0..key_end], &pos);
            const suffix = try readVarint(page[0..key_end], &pos);
            const posting = try get(u16, page[0..key_end], pos);
            pos += 2;
            if (shared > key_len or shared + suffix > max_key or pos > key_end or suffix > key_end - pos) return error.InvalidKey;
            @memcpy(key[shared..][0..suffix], page[pos..][0..suffix]);
            key_len = shared + suffix;
            pos += suffix;
            if (j == 0 and !std.mem.eql(u8, key[0..key_len], map.bytes[root_pos + 2 ..][0..root_len])) return error.RootMismatch;
            if (posting < key_end or posting > used or used - posting < 4) return error.InvalidPosting;
            const size = try get(u32, page, posting);
            if (size > used - posting - 4) return error.InvalidPosting;
            while (tag < 2) {
                if (try statements[tag].step()) break;
                tag += 1;
            }
            if (tag == 2) return error.ExtraEntry;
            const row: zqlite.Row = .{ .stmt = statements[tag] };
            if (row.columnType(0) != .text or row.columnType(1) != .blob) return error.SourceType;
            if (key_len == 0 or key[0] != tag + 1 or !std.mem.eql(u8, key[1..key_len], row.text(0)) or !std.mem.eql(u8, page[posting + 4 ..][0..size], row.blob(1))) return error.SourceMismatch;
            count += 1;
        }
        if (pos != key_end) return error.TrailingKeys;
    }
    while (tag < 2) : (tag += 1) {
        if (try statements[tag].step()) return error.MissingEntry;
    }
    return count;
}

export fn typeahead_compact_verify(source: [*:0]const u8, side: [*:0]const u8, digest: [*:0]const u8, stats: *Stats) ?[*:0]const u8 {
    stats.verified_keys = verify(source, side, digest) catch |err| return @errorName(err);
    return null;
}
