//! A read-only handle on a store another process is writing. It fences
//! nobody and never writes. It shows whole commits only, as of its last
//! `refresh`.

const std = @import("std");
const engine = @import("engine.zig");
const keys = @import("keys.zig");
const read = @import("read.zig");
const record = @import("record.zig");
const scan = @import("scan.zig");
const Io = std.Io;

pub const Reader = struct {
    gpa: std.mem.Allocator,
    handle: *engine.Handle,

    pub fn open(gpa: std.mem.Allocator, io: Io, root: Io.Dir, path: []const u8, layout: engine.Layout) !*Reader {
        const handle = try engine.Handle.open(gpa, io, root, path, layout, .reader);
        errdefer handle.close();
        const self = try gpa.create(Reader);
        self.* = .{ .gpa = gpa, .handle = handle };
        return self;
    }

    pub fn close(self: *Reader) void {
        self.handle.close();
        self.gpa.destroy(self);
    }

    pub fn db(self: *Reader) *engine.Db {
        return self.handle.db;
    }

    /// Catch up to the writer's newest durable commit.
    pub fn refresh(self: *Reader) !void {
        try self.db().refresh();
    }

    /// Deliver every entry of `prefix` from `position` on to
    /// `visitor.entry(key, value)`. The scan pages, refreshes between pages
    /// and survives the writer collecting tables under it; see `scan.zig`
    /// for what that means for keys that move. A scan stopped by
    /// `options.limit` continues from the same `position`.
    pub fn scanRange(self: *Reader, position: *scan.Position, prefix: keys.Prefix, options: scan.Options, visitor: anytype) !scan.Stats {
        const bounds = prefix.bounds();
        if (position.next.items.len == 0 and !position.done) try position.reset(self.gpa, &bounds[0]);
        return scan.run(self.gpa, self.db(), position, &bounds[1], options, visitor);
    }

    pub fn actor(self: *Reader, arena: std.mem.Allocator, did: []const u8) !?record.Actor {
        return read.actor(self.db(), arena, did);
    }

    pub fn actors(self: *Reader, arena: std.mem.Allocator, dids: []const []const u8) ![]?record.Actor {
        return read.actors(self.db(), arena, dids);
    }

    pub fn walk(self: *Reader, arena: std.mem.Allocator, after_did: []const u8, limit: usize) ![]const []const u8 {
        return read.walk(self.db(), arena, after_did, limit);
    }

    pub fn byHandle(self: *Reader, arena: std.mem.Allocator, handle: []const u8) ![]const []const u8 {
        return read.byHandle(self.db(), arena, handle);
    }

    pub fn timed(self: *Reader, arena: std.mem.Allocator, prefix: keys.Prefix, after: ?read.Timed, until: u64, limit: usize) ![]const read.Timed {
        return read.timed(self.db(), arena, prefix, after, until, limit);
    }

    pub fn quality(self: *Reader, arena: std.mem.Allocator, after: ?read.Quality, limit: usize) ![]const read.Quality {
        return read.quality(self.db(), arena, after, limit);
    }

    pub fn changes(self: *Reader, arena: std.mem.Allocator, from: u64, limit: usize) !read.Changes {
        return read.changes(self.db(), arena, from, limit);
    }

    pub fn cursors(self: *Reader, arena: std.mem.Allocator) !read.Cursors {
        return read.cursors(self.db(), arena);
    }

    pub fn counts(self: *Reader, options: scan.Options) !read.Counts {
        return read.counts(self.gpa, self.db(), options);
    }
};
