const std = @import("std");
const actor = @import("actor.zig");
const args_mod = @import("args.zig");
const keys = @import("keys.zig");
const maintain = @import("maintain.zig");
const out = @import("out.zig");
const store = @import("store.zig");
const sys = @import("sys.zig");

pub const flags = store.option_flags ++ maintain.flags ++ [_]args_mod.Flag{
    .{ .name = "--input" },
    .{ .name = "--commit-bytes" },
    .{ .name = "--disk-sample-ms" },
    .{ .name = "--progress-records" },
};

const identity_delay = 3600;

pub const Counts = struct {
    a: u64 = 0,
    h: u64 = 0,
    u: u64 = 0,
    q: u64 = 0,
    t: u64 = 0,
    r: u64 = 0,
    i: u64 = 0,
    key_bytes: u64 = 0,
    value_bytes: u64 = 0,

    pub fn entries(self: Counts) u64 {
        return self.a + self.h + self.u + self.q + self.t + self.r + self.i;
    }

    fn put(self: *Counts, db: *store.Db, comptime prefix: []const u8, key: []const u8, value: []const u8) !void {
        try db.put(key, value);
        @field(self, prefix) += 1;
        self.key_bytes += key.len;
        self.value_bytes += value.len;
    }
};

pub fn writeActor(db: *store.Db, counts: *Counts, fields: actor.Fields, json: []const u8) !bool {
    const did = fields.did;
    if (did.len == 0 or did.len > keys.max_did) return false;
    var buf: keys.Buf = undefined;
    try counts.put(db, "a", keys.actor(&buf, did), json);
    const has_handle = fields.handle.len > 0 and fields.handle.len <= keys.max_handle;
    if (has_handle) try counts.put(db, "h", keys.handleKey(&buf, fields.handle, did), "");
    if (has_handle and !fields.deleted and !fields.hidden and fields.quality_score > 0) {
        try counts.put(db, "q", keys.quality(&buf, fields.quality_score, did), "");
    }
    try putStamps(db, counts, fields);
    return true;
}

pub fn putStamps(db: *store.Db, counts: *Counts, fields: actor.Fields) !void {
    const did = fields.did;
    var buf: keys.Buf = undefined;
    try counts.put(db, "u", keys.stamped(&buf, 'u', fields.version, did), "");
    if (fields.deleted) try counts.put(db, "t", keys.stamped(&buf, 't', fields.version, did), "");
    if (keys.inRefreshSubset(did)) try counts.put(db, "r", keys.stamped(&buf, 'r', fields.version, did), "");
    if (keys.inIdentitySubset(did)) {
        var value: [16]u8 = undefined;
        std.mem.writeInt(u64, value[0..8], 0, .big);
        std.mem.writeInt(u64, value[8..16], fields.version, .big);
        try counts.put(db, "i", keys.stamped(&buf, 'i', fields.version + identity_delay, did), &value);
    }
}

pub fn eraseStamps(db: *store.Db, fields: actor.Fields) !void {
    const did = fields.did;
    var buf: keys.Buf = undefined;
    try db.del(keys.stamped(&buf, 'u', fields.version, did));
    if (fields.deleted) try db.del(keys.stamped(&buf, 't', fields.version, did));
    if (keys.inRefreshSubset(did)) try db.del(keys.stamped(&buf, 'r', fields.version, did));
    if (keys.inIdentitySubset(did)) try db.del(keys.stamped(&buf, 'i', fields.version + identity_delay, did));
}

pub fn run(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const input = try args.required("--input");
    const commit_bytes = try args.int(usize, "--commit-bytes", 256 << 10);
    const progress_records = try args.int(u64, "--progress-records", 1_000_000);
    const policy = try maintain.Policy.parse(args);

    const handle = try store.Handle.open(gpa, io, args, .writer, true);
    defer handle.close();
    const db = handle.db;
    var maintainer: maintain.Maintainer = .{ .db = db, .io = io, .policy = policy };
    defer maintainer.deinit();
    var watch = try sys.DiskWatch.init(io, gpa, handle.dir, try args.int(u64, "--disk-sample-ms", 5000));

    const from_stdin = std.mem.eql(u8, input, "-");
    const file = if (from_stdin) std.Io.File.stdin() else try std.Io.Dir.cwd().openFile(io, input, .{});
    defer if (!from_stdin) file.close(io);
    const read_buffer = try gpa.alloc(u8, 4 << 20);
    defer gpa.free(read_buffer);
    var reader = file.readerStreaming(io, read_buffer);

    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    var commits: std.ArrayList(u64) = .empty;
    defer commits.deinit(gpa);
    var counts: Counts = .{};
    var records: u64 = 0;
    var skipped: u64 = 0;
    var input_bytes: u64 = 0;
    var read_ns: u64 = 0;
    var version_min: u64 = std.math.maxInt(u64);
    var version_max: u64 = 0;
    var version_min_records: u64 = 0;

    const started = sys.now(io);
    while (true) {
        const read_started = sys.now(io);
        const line = try reader.interface.takeDelimiter('\n') orelse break;
        read_ns += sys.now(io) - read_started;
        if (line.len == 0) continue;
        input_bytes += line.len + 1;
        _ = arena.reset(.retain_capacity);
        const fields = try actor.parse(arena.allocator(), line);
        if (try writeActor(db, &counts, fields, line)) records += 1 else skipped += 1;
        if (fields.version < version_min) {
            version_min = fields.version;
            version_min_records = 0;
        }
        version_min_records += @intFromBool(fields.version == version_min);
        version_max = @max(version_max, fields.version);
        if (db.stagedBytes() >= commit_bytes) {
            const commit_started = sys.now(io);
            try db.commit();
            try commits.append(gpa, sys.now(io) - commit_started);
            _ = try maintainer.step();
            try maintainer.relieve();
            try watch.poll();
        }
        if (progress_records > 0 and records % progress_records == 0 and records > 0) {
            try out.emit(io, .{
                .event = "load_progress",
                .records = records,
                .elapsed_ms = sys.msSince(io, started),
                .disk_peak_bytes = watch.peak_bytes,
                .l0_tables = db.version.tables[0].len,
                .peak_rss_bytes = sys.peakRssBytes(io),
            });
        }
    }
    if (db.stagedBytes() > 0) {
        const commit_started = sys.now(io);
        try db.commit();
        try commits.append(gpa, sys.now(io) - commit_started);
    }
    try maintainer.flush();
    const elapsed = sys.now(io) - started;
    const end = try watch.sample();

    try out.emit(io, .{
        .event = "load",
        .records = records,
        .skipped = skipped,
        .input_bytes = input_bytes,
        .version_min = if (records + skipped == 0) 0 else version_min,
        .version_max = version_max,
        .version_min_records = version_min_records,
        .entries = counts.entries(),
        .entries_by_prefix = .{ .a = counts.a, .h = counts.h, .u = counts.u, .q = counts.q, .t = counts.t, .r = counts.r, .i = counts.i },
        .logical_key_bytes = counts.key_bytes,
        .logical_value_bytes = counts.value_bytes,
        .wall_ms = elapsed / std.time.ns_per_ms,
        .read_wait_ms = read_ns / std.time.ns_per_ms,
        .records_per_s = sys.perSecond(records, elapsed),
        .entries_per_s = sys.perSecond(counts.entries(), elapsed),
        .commit = sys.summarize(commits.items),
        .maintenance = maintainer.report(),
        .peak_rss_bytes = sys.peakRssBytes(io),
        .disk_peak_bytes = watch.peak_bytes,
        .disk_samples = watch.samples,
        .disk_end = end,
        .shape = store.shape(db),
        .options = .{
            .commit_bytes = commit_bytes,
            .block_bytes = db.options.block_bytes,
            .fence_stride = db.options.fence_stride,
            .cache_bytes = db.options.cache_bytes,
            .table_bytes = db.options.table_bytes,
            .policy = policy,
        },
    });
}
