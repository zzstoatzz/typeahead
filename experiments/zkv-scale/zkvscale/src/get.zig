const std = @import("std");
const actor = @import("actor.zig");
const args_mod = @import("args.zig");
const keys = @import("keys.zig");
const out = @import("out.zig");
const scan_mod = @import("scan.zig");
const store = @import("store.zig");
const sys = @import("sys.zig");

pub const Dids = struct {
    arena: std.heap.ArenaAllocator,
    items: []const []const u8,
    sampled_in_process: bool,

    pub fn deinit(self: *Dids) void {
        self.arena.deinit();
    }

    pub fn at(self: Dids, index: usize) []const u8 {
        return self.items[index % self.items.len];
    }
};

const Reservoir = struct {
    arena: std.mem.Allocator,
    limit: usize,
    random: std.Random,
    items: std.ArrayList([]u8) = .empty,
    seen: u64 = 0,

    pub fn entry(self: *Reservoir, key: []const u8, _: []const u8) !void {
        const did = keys.didOfActorKey(key);
        self.seen += 1;
        if (self.items.items.len < self.limit) {
            try self.items.append(self.arena, try self.arena.dupe(u8, did));
            return;
        }
        const slot = self.random.uintLessThan(u64, self.seen);
        if (slot >= self.limit) return;
        const held = &self.items.items[slot];
        if (held.len >= did.len) {
            @memcpy(held.*[0..did.len], did);
            held.* = held.*[0..did.len];
        } else held.* = try self.arena.dupe(u8, did);
    }
};

pub fn sample(gpa: std.mem.Allocator, db: *store.Db, limit: usize, seed: u64) !Dids {
    var arena: std.heap.ArenaAllocator = .init(gpa);
    errdefer arena.deinit();
    var prng: std.Random.DefaultPrng = .init(seed);
    var reservoir: Reservoir = .{ .arena = arena.allocator(), .limit = limit, .random = prng.random() };
    _ = try scan_mod.prefixScan(gpa, db, "a", .{ .page = 100_000, .refresh = true }, &reservoir);
    prng.random().shuffle([]u8, reservoir.items.items);
    if (reservoir.items.items.len == 0) return error.EmptyStore;
    return .{ .arena = arena, .items = reservoir.items.items, .sampled_in_process = true };
}

pub fn read(gpa: std.mem.Allocator, io: std.Io, path: []const u8) !Dids {
    var arena: std.heap.ArenaAllocator = .init(gpa);
    errdefer arena.deinit();
    const bytes = try std.Io.Dir.cwd().readFileAlloc(io, path, arena.allocator(), .unlimited);
    var items: std.ArrayList([]const u8) = .empty;
    var lines = std.mem.tokenizeScalar(u8, bytes, '\n');
    while (lines.next()) |line| try items.append(arena.allocator(), line);
    if (items.items.len == 0) return error.EmptySample;
    return .{ .arena = arena, .items = items.items, .sampled_in_process = false };
}

pub fn obtain(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args, db: *store.Db, limit: usize) !Dids {
    if (args.get("--dids")) |path| return read(gpa, io, path);
    return sample(gpa, db, limit, try args.int(u64, "--seed", 1));
}

pub const sample_flags = store.option_flags ++ [_]args_mod.Flag{
    .{ .name = "--n" },
    .{ .name = "--seed" },
    .{ .name = "--out" },
};

pub fn runSample(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const limit = try args.int(usize, "--n", 10_000);
    const path = try args.required("--out");
    const handle = try store.Handle.open(gpa, io, args, .reader, false);
    defer handle.close();
    const started = sys.now(io);
    var dids = try sample(gpa, handle.db, limit, try args.int(u64, "--seed", 1));
    defer dids.deinit();
    const file = try std.Io.Dir.cwd().createFile(io, path, .{});
    defer file.close(io);
    var buffer: [64 << 10]u8 = undefined;
    var writer = file.writerStreaming(io, &buffer);
    for (dids.items) |did| {
        try writer.interface.writeAll(did);
        try writer.interface.writeByte('\n');
    }
    try writer.interface.flush();
    try out.emit(io, .{ .event = "sample", .dids = dids.items.len, .wall_ms = sys.msSince(io, started) });
}

pub const get_flags = store.option_flags ++ [_]args_mod.Flag{
    .{ .name = "--n" },
    .{ .name = "--seed" },
    .{ .name = "--dids" },
};

pub fn runGet(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const wanted = try args.int(usize, "--n", 10_000);
    const handle = try store.Handle.open(gpa, io, args, .reader, false);
    defer handle.close();
    const db = handle.db;
    var dids = try obtain(gpa, io, args, db, wanted);
    defer dids.deinit();
    const n = @min(wanted, dids.items.len);

    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    const point = try gpa.alloc(u64, n);
    defer gpa.free(point);
    var handles: std.ArrayList([]const u8) = .empty;
    defer handles.deinit(gpa);
    var expected: std.ArrayList([]const u8) = .empty;
    defer expected.deinit(gpa);
    var buf: keys.Buf = undefined;
    var point_misses: u64 = 0;

    const point_started = sys.now(io);
    for (dids.items[0..n], point) |did, *elapsed| {
        if (did.len > keys.max_did) return error.DidTooLong;
        const key = keys.actor(&buf, did);
        const started = sys.now(io);
        const value = try db.get(key);
        elapsed.* = sys.now(io) - started;
        const json = value orelse {
            point_misses += 1;
            continue;
        };
        const fields = try actor.parse(arena.allocator(), json);
        if (fields.handle.len == 0 or fields.handle.len > keys.max_handle) continue;
        try handles.append(gpa, try arena.allocator().dupe(u8, fields.handle));
        try expected.append(gpa, did);
    }
    const point_wall = sys.now(io) - point_started;

    const lookup = try gpa.alloc(u64, handles.items.len);
    defer gpa.free(lookup);
    var handle_misses: u64 = 0;
    var handle_other_did: u64 = 0;
    const lookup_started = sys.now(io);
    for (handles.items, expected.items, lookup) |name, did, *elapsed| {
        const start = keys.handlePrefix(&buf, name);
        var end_buf: keys.Buf = undefined;
        @memcpy(end_buf[0..start.len], start);
        end_buf[start.len - 1] = 1;
        const started = sys.now(io);
        var found = try db.scan(.{ .start = start, .end = end_buf[0..start.len], .limit = 1 });
        const entry = try found.next();
        elapsed.* = sys.now(io) - started;
        if (entry) |e| {
            if (!std.mem.eql(u8, keys.didOfHandleKey(e.key) orelse "", did)) handle_other_did += 1;
        } else handle_misses += 1;
        found.deinit();
    }
    const lookup_wall = sys.now(io) - lookup_started;

    try out.emit(io, .{
        .event = "get",
        .open_us = handle.open_us,
        .sampled_in_process = dids.sampled_in_process,
        .fence_stride = db.options.fence_stride,
        .cache_bytes = db.options.cache_bytes,
        .point = sys.summarize(point),
        .point_misses = point_misses,
        .point_wall_ms = point_wall / std.time.ns_per_ms,
        .handle = sys.summarize(lookup),
        .handle_misses = handle_misses,
        .handle_other_did = handle_other_did,
        .handle_wall_ms = lookup_wall / std.time.ns_per_ms,
        .cache = db.cacheStats(),
        .resident_metadata_bytes = db.residentMetadataBytes(),
        .rss_bytes = sys.currentRssBytes(io),
        .peak_rss_bytes = sys.peakRssBytes(io),
    });
}
