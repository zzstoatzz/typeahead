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

    // Unit tests for the index/ module (normalize, prefixes, scoring,
    // pack, schema, builder). Rooted at src/test_index.zig so transitive
    // imports inside builder.zig (e.g. ../db/TursoClient.zig) stay in
    // the module tree.
    const index_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/test_index.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{
                .{ .name = "zqlite", .module = zqlite.module("zqlite") },
            },
        }),
    });
    const run_index_tests = b.addRunArtifact(index_tests);

    // Explicit offline tool: no server, sync loop, or credentials required.
    const evaluator = b.addExecutable(.{
        .name = "eval-lookup",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/eval_lookup.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{.{ .name = "zqlite", .module = zqlite.module("zqlite") }},
        }),
    });
    const install_evaluator = b.addInstallArtifact(evaluator, .{});
    b.step("eval-lookup", "Build the isolated lookup comparator").dependOn(&install_evaluator.step);

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
    test_step.dependOn(&run_index_tests.step);
    test_step.dependOn(&run_ingest_tests.step);
}
