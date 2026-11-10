const std = @import("std");
const zkv = @import("zkv");
const Io = std.Io;

pub const Db = zkv.Db(zkv.DirStore);
pub const level_count = @typeInfo(@FieldType(@typeInfo(@FieldType(Db, "version")).pointer.child, "tables")).array.len;

/// Table layout and memory settings, fixed for the life of a handle.
///
/// The defaults follow the 2026-11-10 measurement of 13.2 million actors
/// (docs/evals/zkv-scale-20261110). `fence_stride` 32 cut a handle's resident
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
