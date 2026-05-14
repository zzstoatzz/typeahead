const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const zat = b.dependency("zat", .{
        .target = target,
        .optimize = optimize,
    });

    const zqlite = b.dependency("zqlite", .{
        .target = target,
        .optimize = optimize,
        .sqlite3 = &[_][]const u8{ "-std=c99", "-DSQLITE_ENABLE_FTS5" },
    });

    const logfire = b.dependency("logfire", .{
        .target = target,
        .optimize = optimize,
    });

    const zug = b.dependency("zug", .{
        .target = target,
        .optimize = optimize,
    });

    const poolio = b.dependency("poolio", .{
        .target = target,
        .optimize = optimize,
    });

    const exe = b.addExecutable(.{
        .name = "typeahead-ingester",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{
                .{ .name = "zat", .module = zat.module("zat") },
                .{ .name = "zqlite", .module = zqlite.module("zqlite") },
                .{ .name = "logfire", .module = logfire.module("logfire") },
                .{ .name = "zug", .module = zug.module("zug") },
                .{ .name = "poolio", .module = poolio.module("poolio") },
            },
        }),
    });

    b.installArtifact(exe);

    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    if (b.args) |args| {
        run_cmd.addArgs(args);
    }

    const run_step = b.step("run", "Run the ingester");
    run_step.dependOn(&run_cmd.step);

    // minimal logfire test binary
    const test_lf = b.addExecutable(.{
        .name = "test-logfire",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/test_logfire.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{
                .{ .name = "logfire", .module = logfire.module("logfire") },
            },
        }),
    });

    const run_test_lf = b.addRunArtifact(test_lf);
    run_test_lf.step.dependOn(b.getInstallStep());
    const test_lf_step = b.step("test-logfire", "Run logfire test");
    test_lf_step.dependOn(&run_test_lf.step);
}
