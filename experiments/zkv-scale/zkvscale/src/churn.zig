const std = @import("std");
const actor = @import("actor.zig");
const args_mod = @import("args.zig");
const get = @import("get.zig");
const keys = @import("keys.zig");
const load = @import("load.zig");
const maintain = @import("maintain.zig");
const out = @import("out.zig");
const store = @import("store.zig");
const sys = @import("sys.zig");

const burst_commit_keys = 400;
const slow_commit_ns = std.time.ns_per_s;

const Writer = struct {
    gpa: std.mem.Allocator,
    io: std.Io,
    db: *store.Db,
    maintainer: maintain.Maintainer,
    watch: sys.DiskWatch,
    dids: get.Dids,
    arena: std.heap.ArenaAllocator,
    value: std.ArrayList(u8) = .empty,
    rewritten: std.ArrayList(u8) = .empty,
    next_did: usize = 0,
    applied: u64 = 0,
    missing: u64 = 0,
    read_ns: u64 = 0,
    slow_commits: u64 = 0,

    fn init(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args, handle: *store.Handle, sample_size: usize) !Writer {
        return .{
            .gpa = gpa,
            .io = io,
            .db = handle.db,
            .maintainer = .{ .db = handle.db, .io = io, .policy = try maintain.Policy.parse(args) },
            .dids = try get.obtain(gpa, io, args, handle.db, sample_size),
            .watch = try sys.DiskWatch.init(io, gpa, handle.dir, try args.int(u64, "--disk-sample-ms", 2000)),
            .arena = .init(gpa),
        };
    }

    fn deinit(self: *Writer) void {
        self.maintainer.deinit();
        self.dids.deinit();
        self.arena.deinit();
        self.value.deinit(self.gpa);
        self.rewritten.deinit(self.gpa);
    }

    fn fetch(self: *Writer) !?actor.Fields {
        const did = self.dids.at(self.next_did);
        self.next_did += 1;
        if (did.len > keys.max_did) return error.DidTooLong;
        var buf: keys.Buf = undefined;
        const started = sys.now(self.io);
        const borrowed = try self.db.get(keys.actor(&buf, did));
        self.read_ns += sys.now(self.io) - started;
        const json = borrowed orelse {
            self.missing += 1;
            return null;
        };
        self.value.clearRetainingCapacity();
        try self.value.appendSlice(self.gpa, json);
        _ = self.arena.reset(.retain_capacity);
        return try actor.parse(self.arena.allocator(), self.value.items);
    }

    fn update(self: *Writer, stamp: u64) !void {
        var fields = try self.fetch() orelse return;
        const version = @max(stamp, fields.version + 1);
        try actor.bumpVersion(self.gpa, &self.rewritten, self.value.items, fields.version, version);
        var buf: keys.Buf = undefined;
        try self.db.put(keys.actor(&buf, fields.did), self.rewritten.items);
        try load.eraseStamps(self.db, fields);
        fields.version = version;
        var counts: load.Counts = .{};
        try load.putStamps(self.db, &counts, fields);
        self.applied += 1;
    }

    fn resend(self: *Writer, counts: *load.Counts) !void {
        const fields = try self.fetch() orelse return;
        if (try load.writeActor(self.db, counts, fields, self.value.items)) self.applied += 1;
    }

    fn commit(self: *Writer, commits: *std.ArrayList(u64)) !void {
        const started = sys.now(self.io);
        try self.db.commit();
        const elapsed = sys.now(self.io) - started;
        try commits.append(self.gpa, elapsed);
        if (elapsed > slow_commit_ns) self.slow_commits += 1;
    }

    fn afterCommit(self: *Writer) !void {
        _ = try self.maintainer.step();
        try self.maintainer.relieve();
        try self.watch.poll();
    }
};

pub const churn_flags = store.option_flags ++ maintain.flags ++ [_]args_mod.Flag{
    .{ .name = "--seconds" },
    .{ .name = "--commits-per-minute" },
    .{ .name = "--keys-per-commit" },
    .{ .name = "--burst-every" },
    .{ .name = "--burst-keys" },
    .{ .name = "--dids" },
    .{ .name = "--seed" },
    .{ .name = "--disk-sample-ms" },
};

pub fn runChurn(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const seconds = try args.int(u64, "--seconds", 60);
    const per_minute = try args.int(u64, "--commits-per-minute", 30);
    const per_commit = try args.int(usize, "--keys-per-commit", 60);
    const burst_every = try args.int(u64, "--burst-every", 0);
    const burst_keys = try args.int(usize, "--burst-keys", 0);
    if (per_minute == 0 or per_commit == 0) return error.InvalidRate;
    const bursts = if (burst_every > 0) seconds / burst_every else 0;
    const needed = seconds * per_minute / 60 * per_commit + bursts * burst_keys + per_commit;

    const handle = try store.Handle.open(gpa, io, args, .writer, false);
    defer handle.close();
    var writer = try Writer.init(gpa, io, args, handle, needed);
    defer writer.deinit();
    var steady: std.ArrayList(u64) = .empty;
    defer steady.deinit(gpa);
    var burst: std.ArrayList(u64) = .empty;
    defer burst.deinit(gpa);
    var batches: std.ArrayList(u64) = .empty;
    defer batches.deinit(gpa);
    var burst_batches: std.ArrayList(u64) = .empty;
    defer burst_batches.deinit(gpa);
    var burst_walls: std.ArrayList(u64) = .empty;
    defer burst_walls.deinit(gpa);

    const interval = 60 * std.time.ns_per_s / per_minute;
    const started = sys.now(io);
    const deadline = started + seconds * std.time.ns_per_s;
    var next_commit = started;
    var next_burst = if (burst_every > 0 and burst_keys > 0) started + burst_every * std.time.ns_per_s else deadline;
    var late_commits: u64 = 0;
    while (true) {
        const now = sys.now(io);
        if (now >= deadline) break;
        if (now >= next_burst) {
            var left = burst_keys;
            while (left > 0) {
                const size = @min(left, burst_commit_keys);
                const batch_started = sys.now(io);
                for (0..size) |_| try writer.update(sys.unixSeconds(io));
                try writer.commit(&burst);
                try burst_batches.append(gpa, sys.now(io) - batch_started);
                try writer.afterCommit();
                left -= size;
            }
            try burst_walls.append(gpa, sys.now(io) - now);
            next_burst += burst_every * std.time.ns_per_s;
            continue;
        }
        if (now >= next_commit) {
            if (now - next_commit > interval) late_commits += 1;
            for (0..per_commit) |_| try writer.update(sys.unixSeconds(io));
            try writer.commit(&steady);
            try batches.append(gpa, sys.now(io) - now);
            try writer.afterCommit();
            next_commit += interval;
            continue;
        }
        if (try writer.maintainer.step()) continue;
        try writer.watch.poll();
        const wake = @min(next_commit, next_burst, deadline);
        const idle = @min(wake -| sys.now(io), 20 * std.time.ns_per_ms);
        if (idle > 0) try io.sleep(.fromNanoseconds(idle), .awake);
    }
    const elapsed = sys.now(io) - started;
    const end = try writer.watch.sample();

    try out.emit(io, .{
        .event = "churn",
        .open_us = handle.open_us,
        .sampled_in_process = writer.dids.sampled_in_process,
        .seconds = seconds,
        .commits_per_minute = per_minute,
        .keys_per_commit = per_commit,
        .burst_every = burst_every,
        .burst_keys = burst_keys,
        .wall_ms = elapsed / std.time.ns_per_ms,
        .updates = writer.applied,
        .missing = writer.missing,
        .late_commits = late_commits,
        .read_ms = writer.read_ns / std.time.ns_per_ms,
        .commit = sys.summarize(steady.items),
        .batch = sys.summarize(batches.items),
        .burst_commit = sys.summarize(burst.items),
        .burst_batch = sys.summarize(burst_batches.items),
        .burst_wall = sys.summarize(burst_walls.items),
        .commits_over_1s = writer.slow_commits,
        .any_commit_over_1s = writer.slow_commits > 0,
        .maintenance = writer.maintainer.report(),
        .peak_rss_bytes = sys.peakRssBytes(io),
        .disk_start = writer.watch.start,
        .disk_peak_bytes = writer.watch.peak_bytes,
        .disk_samples = writer.watch.samples,
        .disk_end = end,
        .shape = store.shape(handle.db),
    });
}

pub const storm_flags = store.option_flags ++ maintain.flags ++ [_]args_mod.Flag{
    .{ .name = "--keys" },
    .{ .name = "--commit-keys" },
    .{ .name = "--dids" },
    .{ .name = "--seed" },
    .{ .name = "--disk-sample-ms" },
};

pub fn runStorm(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const total = try args.int(usize, "--keys", 100_000);
    const commit_keys = try args.int(usize, "--commit-keys", burst_commit_keys);
    if (commit_keys == 0) return error.InvalidRate;
    const handle = try store.Handle.open(gpa, io, args, .writer, false);
    defer handle.close();
    var writer = try Writer.init(gpa, io, args, handle, total);
    defer writer.deinit();
    var commits: std.ArrayList(u64) = .empty;
    defer commits.deinit(gpa);
    var batches: std.ArrayList(u64) = .empty;
    defer batches.deinit(gpa);
    var counts: load.Counts = .{};

    const started = sys.now(io);
    var left = total;
    while (left > 0) {
        const size = @min(left, commit_keys);
        const batch_started = sys.now(io);
        for (0..size) |_| try writer.resend(&counts);
        try writer.commit(&commits);
        try batches.append(gpa, sys.now(io) - batch_started);
        try writer.afterCommit();
        left -= size;
    }
    const elapsed = sys.now(io) - started;
    const end = try writer.watch.sample();

    try out.emit(io, .{
        .event = "storm",
        .open_us = handle.open_us,
        .sampled_in_process = writer.dids.sampled_in_process,
        .keys = total,
        .distinct_dids = writer.dids.items.len,
        .commit_keys = commit_keys,
        .wall_ms = elapsed / std.time.ns_per_ms,
        .actors = writer.applied,
        .missing = writer.missing,
        .entries = counts.entries(),
        .logical_bytes = counts.key_bytes + counts.value_bytes,
        .actors_per_s = sys.perSecond(writer.applied, elapsed),
        .entries_per_s = sys.perSecond(counts.entries(), elapsed),
        .read_ms = writer.read_ns / std.time.ns_per_ms,
        .commit = sys.summarize(commits.items),
        .batch = sys.summarize(batches.items),
        .commits_over_1s = writer.slow_commits,
        .maintenance = writer.maintainer.report(),
        .peak_rss_bytes = sys.peakRssBytes(io),
        .disk_start = writer.watch.start,
        .disk_peak_bytes = writer.watch.peak_bytes,
        .disk_samples = writer.watch.samples,
        .disk_end = end,
        .shape = store.shape(handle.db),
    });
}
