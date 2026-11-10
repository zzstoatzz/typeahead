const std = @import("std");
const args_mod = @import("args.zig");
const maintain = @import("maintain.zig");
const out = @import("out.zig");
const store = @import("store.zig");
const sys = @import("sys.zig");

pub const flags = store.option_flags ++ [_]args_mod.Flag{
    .{ .name = "--step-bytes" },
    .{ .name = "--disk-sample-ms" },
};

pub fn run(gpa: std.mem.Allocator, io: std.Io, args: args_mod.Args) !void {
    const step_bytes = try args.int(usize, "--step-bytes", 64 << 20);
    const handle = try store.Handle.open(gpa, io, args, .writer, false);
    defer handle.close();
    const db = handle.db;
    var maintainer: maintain.Maintainer = .{ .db = db, .io = io, .policy = .{} };
    defer maintainer.deinit();
    var watch = try sys.DiskWatch.init(io, gpa, handle.dir, try args.int(u64, "--disk-sample-ms", 2000));
    const before = store.shape(db);

    const started = sys.now(io);
    try maintainer.flush();
    try maintainer.drain(step_bytes, &watch);
    const elapsed = sys.now(io) - started;
    const end = try watch.sample();

    try out.emit(io, .{
        .event = "settle",
        .open_us = handle.open_us,
        .wall_ms = elapsed / std.time.ns_per_ms,
        .step_bytes = step_bytes,
        .maintenance = maintainer.report(),
        .peak_rss_bytes = sys.peakRssBytes(io),
        .disk_start = watch.start,
        .disk_peak_bytes = watch.peak_bytes,
        .disk_samples = watch.samples,
        .disk_end = end,
        .shape_before = before,
        .shape = store.shape(db),
    });
}
