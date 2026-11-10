const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const zat = b.dependency("zat", .{
        .target = target,
        .optimize = optimize,
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
                .{ .name = "logfire", .module = logfire.module("logfire") },
                .{ .name = "zug", .module = zug.module("zug") },
                .{ .name = "poolio", .module = poolio.module("poolio") },
            },
        }),
    });

    b.installArtifact(exe);

    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    run_cmd.addPassthruArgs();

    const run_step = b.step("run", "Run the ingester");
    run_step.dependOn(&run_cmd.step);

    // Unit tests for the shared normalizer sources (also linked into the
    // Tantivy reader via services/tantivy-normalizer) and the Turso client.
    const unit_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/test_units.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
        }),
    });
    const run_unit_tests = b.addRunArtifact(unit_tests);

    // Ingest tests. Rooted at src/ingest.zig so the firehose decode path
    // (collection allowlist, DAG-CBOR avatar CID rendering) is covered — both
    // are new with the jetstream→relay cutover and both fail silently.
    const ingest_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/ingest.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{
                .{ .name = "zat", .module = zat.module("zat") },
                .{ .name = "logfire", .module = logfire.module("logfire") },
                .{ .name = "zug", .module = zug.module("zug") },
                .{ .name = "poolio", .module = poolio.module("poolio") },
            },
        }),
    });
    const run_ingest_tests = b.addRunArtifact(ingest_tests);

    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_unit_tests.step);
    test_step.dependOn(&run_ingest_tests.step);
}
