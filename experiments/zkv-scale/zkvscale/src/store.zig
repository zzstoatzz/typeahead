const std = @import("std");
const zkv = @import("zkv");
const args_mod = @import("args.zig");
const sys = @import("sys.zig");
const Io = std.Io;

pub const Store = zkv.DirStore;
pub const Db = zkv.Db(Store);
pub const level_count = @typeInfo(@FieldType(@typeInfo(@FieldType(Db, "version")).pointer.child, "tables")).array.len;

pub const option_flags = [_]args_mod.Flag{
    .{ .name = "--store" },
    .{ .name = "--block-bytes" },
    .{ .name = "--fence-stride" },
    .{ .name = "--cache-bytes" },
};

pub fn options(args: args_mod.Args) !zkv.Options {
    const defaults: zkv.Options = .{};
    return .{
        .block_bytes = try args.int(usize, "--block-bytes", defaults.block_bytes),
        .fence_stride = try args.int(u32, "--fence-stride", defaults.fence_stride),
        .cache_bytes = try args.int(usize, "--cache-bytes", defaults.cache_bytes),
    };
}

pub const Mode = enum { writer, reader };

pub const Handle = struct {
    gpa: std.mem.Allocator,
    io: Io,
    dir: Io.Dir,
    store: Store,
    db: *Db,
    mode: Mode,
    open_us: u64,

    pub fn open(gpa: std.mem.Allocator, io: Io, args: args_mod.Args, mode: Mode, create: bool) !*Handle {
        const path = try args.required("--store");
        if (create) try Io.Dir.cwd().createDirPath(io, path);
        const self = try gpa.create(Handle);
        errdefer gpa.destroy(self);
        const dir = try Io.Dir.cwd().openDir(io, path, .{ .iterate = true });
        errdefer dir.close(io);
        self.* = .{ .gpa = gpa, .io = io, .dir = dir, .store = .init(gpa, io, dir), .db = undefined, .mode = mode, .open_us = 0 };
        errdefer self.store.deinit();
        const started = sys.now(io);
        self.db = switch (mode) {
            .writer => try Db.open(gpa, &self.store, try options(args)),
            .reader => try Db.openReader(gpa, &self.store, try options(args)),
        };
        self.open_us = (sys.now(io) - started) / std.time.ns_per_us;
        return self;
    }

    pub fn close(self: *Handle) void {
        self.db.close();
        self.store.deinit();
        self.dir.close(self.io);
        self.gpa.destroy(self);
    }
};

pub const Level = struct { tables: usize, bytes: u64 };

pub const Shape = struct {
    tables: usize,
    table_bytes: u64,
    levels: [level_count]Level,
    unflushed_segments: u64,
    memtable_bytes: usize,
    resident_metadata_bytes: usize,
    compaction_phase: zkv.CompactionPhase,
};

pub fn shape(db: *Db) Shape {
    var result: Shape = .{
        .tables = 0,
        .table_bytes = 0,
        .levels = undefined,
        .unflushed_segments = if (db.reader) 0 else db.unflushedSegments(),
        .memtable_bytes = db.memtableBytes(),
        .resident_metadata_bytes = db.residentMetadataBytes(),
        .compaction_phase = db.compactionPhase(),
    };
    for (&result.levels, db.version.tables, 0..) |*level, tables, index| {
        level.* = .{ .tables = tables.len, .bytes = db.version.levelBytes(index) };
        result.tables += level.tables;
        result.table_bytes += level.bytes;
    }
    return result;
}
