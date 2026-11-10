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

    const zkv = b.dependency("zkv", .{
        .target = target,
        .optimize = optimize,
    });

    // The handle quarantine list is the Worker's own data file, so the two
    // cannot drift. An image build whose context holds only services/ passes
    // the path it copied the file to.
    const quarantine: std.Build.LazyPath = if (b.option([]const u8, "identity-quarantine", "path to src/identity-quarantine.json")) |path|
        .{ .cwd_relative = path }
    else
        b.path("../src/identity-quarantine.json");

    const role_imports: []const std.Build.Module.Import = &.{
        .{ .name = "zat", .module = zat.module("zat") },
        .{ .name = "logfire", .module = logfire.module("logfire") },
        .{ .name = "zug", .module = zug.module("zug") },
        .{ .name = "poolio", .module = poolio.module("poolio") },
        .{ .name = "zkv", .module = zkv.module("zkv") },
    };

    const exe = b.addExecutable(.{
        .name = "typeahead-ingester",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = role_imports,
        }),
    });
    exe.root_module.addAnonymousImport("identity-quarantine", .{ .root_source_file = quarantine });

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

    const store_module = b.createModule(.{
        .root_source_file = b.path("src/store/root.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .imports = &.{.{ .name = "zkv", .module = zkv.module("zkv") }},
    });
    const store_tests = b.addTest(.{ .root_module = store_module });
    const run_store_tests = b.addRunArtifact(store_tests);

    // Kill tests and the cross-process reader test: the executable spawns
    // itself as the writer it then kills or reads beside.
    const store_process_tests = b.addExecutable(.{
        .name = "store-process-tests",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/store/process_tests.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{.{ .name = "zkv", .module = zkv.module("zkv") }},
        }),
    });
    const run_store_process_tests = b.addRunArtifact(store_process_tests);
    run_store_process_tests.has_side_effects = true;

    const bridge_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/bridge/root.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{
                .{ .name = "zat", .module = zat.module("zat") },
            },
        }),
    });
    const run_bridge_tests = b.addRunArtifact(bridge_tests);

    // Starts the real Tantivy server named by TANTIVY_TEST_BIN, so it is
    // its own step: `just --no-dotenv test-bridge` builds that binary first.
    const bridge_integration = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/bridge/integration.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{
                .{ .name = "zat", .module = zat.module("zat") },
            },
        }),
    });
    const run_bridge_integration = b.addRunArtifact(bridge_integration);
    run_bridge_integration.has_side_effects = true;
    const bridge_integration_step = b.step("test-bridge", "Run the index bridge against a real Tantivy server");
    bridge_integration_step.dependOn(&run_bridge_integration.step);

    const store_role_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/store_role_tests.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = role_imports,
        }),
    });
    store_role_tests.root_module.addAnonymousImport("identity-quarantine", .{ .root_source_file = quarantine });
    const run_store_role_tests = b.addRunArtifact(store_role_tests);

    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_store_role_tests.step);
    test_step.dependOn(&run_unit_tests.step);
    test_step.dependOn(&run_ingest_tests.step);
    test_step.dependOn(&run_store_tests.step);
    test_step.dependOn(&run_store_process_tests.step);
    test_step.dependOn(&run_bridge_tests.step);
}
