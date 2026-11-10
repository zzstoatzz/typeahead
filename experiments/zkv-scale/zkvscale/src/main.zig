const std = @import("std");
const args_mod = @import("args.zig");
const churn = @import("churn.zig");
const get = @import("get.zig");
const load = @import("load.zig");
const scan = @import("scan.zig");
const settle = @import("settle.zig");

const Command = struct {
    name: []const u8,
    flags: []const args_mod.Flag,
    run: *const fn (std.mem.Allocator, std.Io, args_mod.Args) anyerror!void,
};

const commands = [_]Command{
    .{ .name = "load", .flags = &load.flags, .run = load.run },
    .{ .name = "settle", .flags = &settle.flags, .run = settle.run },
    .{ .name = "stat", .flags = &scan.stat_flags, .run = scan.runStat },
    .{ .name = "count", .flags = &scan.count_flags, .run = scan.runCount },
    .{ .name = "scan", .flags = &scan.scan_flags, .run = scan.runScan },
    .{ .name = "sample", .flags = &get.sample_flags, .run = get.runSample },
    .{ .name = "get", .flags = &get.get_flags, .run = get.runGet },
    .{ .name = "churn", .flags = &churn.churn_flags, .run = churn.runChurn },
    .{ .name = "storm", .flags = &churn.storm_flags, .run = churn.runStorm },
};

pub fn main(init: std.process.Init) !void {
    const arena = init.arena.allocator();
    const argv = try init.minimal.args.toSlice(arena);
    if (argv.len >= 2) for (commands) |command| {
        if (!std.mem.eql(u8, command.name, argv[1])) continue;
        const rest = try arena.alloc([]const u8, argv.len - 2);
        for (rest, argv[2..]) |*slot, arg| slot.* = arg;
        return command.run(std.heap.c_allocator, init.io, try args_mod.Args.init(arena, rest, command.flags));
    };
    std.debug.print("usage: zkvscale <command> --store DIR [flags]\ncommands:", .{});
    for (commands) |command| std.debug.print(" {s}", .{command.name});
    std.debug.print("\n", .{});
    std.process.exit(2);
}

test {
    _ = @import("actor.zig");
    _ = @import("args.zig");
    _ = @import("keys.zig");
    _ = @import("scan.zig");
    _ = @import("sys.zig");
    _ = @import("roundtrip.zig");
}
