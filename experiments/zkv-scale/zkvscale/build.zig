const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const zkv = b.dependency("zkv", .{ .target = target, .optimize = optimize });
    const module = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .imports = &.{.{ .name = "zkv", .module = zkv.module("zkv") }},
    });
    b.installArtifact(b.addExecutable(.{ .name = "zkvscale", .root_module = module }));
    const tests = b.addTest(.{ .root_module = module });
    b.step("test", "run unit tests").dependOn(&b.addRunArtifact(tests).step);
}
