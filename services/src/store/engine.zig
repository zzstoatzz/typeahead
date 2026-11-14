const std = @import("std");
const builtin = @import("builtin");
const zkv = @import("zkv");
const Io = std.Io;

pub const Db = zkv.Db(zkv.DirStore);
pub const level_count = @typeInfo(@FieldType(@typeInfo(@FieldType(Db, "version")).pointer.child, "tables")).array.len;

/// Table layout and memory settings, fixed for the life of a handle.
///
/// The defaults follow the 2026-11-10 measurement of 13.2 million actors
/// (docs/history/evals/zkv-scale-20261110). `fence_stride` 32 cut a handle's resident
/// table metadata from 212 MB to 60 MB and moved cold point reads by a few
/// microseconds. 4 KiB blocks are the only size whose read latency was
/// measured (p99 under 200 µs). 64 KiB blocks loaded faster, opened in half
/// the time and held 66 MB at stride 1, but their read latency and their
/// combination with a sparse fence index were not measured.
pub const Layout = struct {
    fence_stride: u32 = 32,
    block_bytes: usize = 4096,
    cache_bytes: usize = 1 << 20,
    table_bytes: usize = 8 << 20,
    l0_tables: usize = 4,
    level_bytes: u64 = 64 << 20,

    fn engine(self: Layout) zkv.Options {
        return .{
            .fence_stride = self.fence_stride,
            .block_bytes = self.block_bytes,
            .cache_bytes = self.cache_bytes,
            .table_bytes = self.table_bytes,
            .l0_tables = self.l0_tables,
            .level_bytes = self.level_bytes,
        };
    }
};

pub const Mode = enum { writer, reader };

/// A zkv handle with the directory and object store it reads through. Heap
/// allocated because the engine keeps a pointer to the object store.
pub const Handle = struct {
    gpa: std.mem.Allocator,
    io: Io,
    dir: Io.Dir,
    objects: zkv.DirStore,
    db: *Db,

    /// `path` is relative to `root`; a writer creates it when it is missing.
    pub fn open(gpa: std.mem.Allocator, io: Io, root: Io.Dir, path: []const u8, layout: Layout, mode: Mode) !*Handle {
        if (mode == .writer) try root.createDirPath(io, path);
        const self = try gpa.create(Handle);
        errdefer gpa.destroy(self);
        const dir = try root.openDir(io, path, .{ .iterate = true });
        errdefer dir.close(io);
        self.* = .{ .gpa = gpa, .io = io, .dir = dir, .objects = .init(gpa, io, dir), .db = undefined };
        errdefer self.objects.deinit();
        if (mode == .writer) prefetchTableMetadata(io, dir);
        self.db = switch (mode) {
            .writer => try Db.open(gpa, &self.objects, layout.engine()),
            .reader => try Db.openReader(gpa, &self.objects, layout.engine()),
        };
        return self;
    }

    pub fn close(self: *Handle) void {
        self.db.close();
        self.objects.deinit();
        self.dir.close(self.io);
        self.gpa.destroy(self);
    }
};

/// The end of a table file this far back is asked for ahead of an open. A
/// table of the default 8 MiB carried about 170 KiB of metadata on the
/// shadow store, 2026-11-11.
const metadata_tail_bytes: u64 = 256 << 10;
const readahead_chunk_bytes: u64 = 128 << 10;

/// Ask the kernel to start reading the end of every table file, which is
/// where zkv 0.1.2 keeps a table's metadata. An open reads that metadata
/// one file after another and waits for each, and beside another reader of
/// the same disk it gets one request's share at a time: opening 641 tables
/// took 43 s while the index process warmed its files on a machine start,
/// against 4 s with the disk to itself. Asking first puts every read in
/// the device's queue at once. A layout this guesses wrong costs reads the
/// open did not need and nothing else. Linux only.
fn prefetchTableMetadata(io: Io, dir: Io.Dir) void {
    if (builtin.os.tag != .linux) return;
    var tables = dir.openDir(io, "sst", .{ .iterate = true }) catch return;
    defer tables.close(io);
    var entries = tables.iterate();
    while (entries.next(io) catch return) |entry| {
        if (entry.kind != .file) continue;
        const file = tables.openFile(io, entry.name, .{}) catch continue;
        defer file.close(io);
        const size = (file.stat(io) catch continue).size;
        var at = size -| metadata_tail_bytes;
        while (at < size) : (at += readahead_chunk_bytes) {
            _ = std.os.linux.fadvise(file.handle, @intCast(at), @intCast(@min(readahead_chunk_bytes, size - at)), std.os.linux.POSIX_FADV.WILLNEED);
        }
    }
}

test "a store with flushed tables opens the same after the readahead hint" {
    const testing = std.testing;
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    {
        const handle = try Handle.open(testing.allocator, testing.io, tmp.dir, "store", .{}, .writer);
        defer handle.close();
        try handle.db.put("a", "one");
        try handle.db.commit();
        try handle.db.flush();
        try handle.db.put("b", "two");
        try handle.db.commit();
    }
    var dir = try tmp.dir.openDir(testing.io, "store", .{ .iterate = true });
    defer dir.close(testing.io);
    prefetchTableMetadata(testing.io, dir);
    const handle = try Handle.open(testing.allocator, testing.io, tmp.dir, "store", .{}, .writer);
    defer handle.close();
    try testing.expectEqualStrings("one", (try handle.db.get("a")).?);
    try testing.expectEqualStrings("two", (try handle.db.get("b")).?);
}
