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

    const poolio = b.dependency("poolio", .{
        .target = target,
        .optimize = optimize,
    });

    const zkv = b.dependency("zkv", .{
        .target = target,
        .optimize = optimize,
    });

    // The handle quarantine list is the Worker's own data file, so the two
    // cannot drift.
    const quarantine = b.path("../src/identity-quarantine.json");

    const role_imports: []const std.Build.Module.Import = &.{
        .{ .name = "zat", .module = zat.module("zat") },
        .{ .name = "logfire", .module = logfire.module("logfire") },
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

    const run_step = b.step("run", "Run the store process");
    run_step.dependOn(&run_cmd.step);

    // Unit tests for the shared normalizer sources, also linked into the
    // Tantivy reader via services/tantivy-normalizer.
    const unit_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/test_units.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
        }),
    });
    const run_unit_tests = b.addRunArtifact(unit_tests);

    // Rooted at src/ingest.zig so the relay decode path (collection
    // allowlist, DAG-CBOR avatar CID rendering) is covered: both fail
    // silently. It also reaches the identity, PLC and label modules' tests.
    const ingest_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/ingest.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = &.{
                .{ .name = "zat", .module = zat.module("zat") },
                .{ .name = "logfire", .module = logfire.module("logfire") },
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

    // The control loop, the store, the bridge and the same real server,
    // from a policy change to what a search returns.
    const control_e2e = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/control_e2e_test.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = role_imports,
        }),
    });
    control_e2e.root_module.addAnonymousImport("identity-quarantine", .{ .root_source_file = quarantine });
    const run_control_e2e = b.addRunArtifact(control_e2e);
    run_control_e2e.has_side_effects = true;
    bridge_integration_step.dependOn(&run_control_e2e.step);

    const stub_options = b.addOptions();
    stub_options.addOptionPath("script_path", b.path("src/testing/identity_stub.py"));

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
    // The Worker's own vectors for what a moderation rule's subject may be,
    // so its validator and the slot's cannot drift apart.
    store_role_tests.root_module.addAnonymousImport("control-rule-vectors", .{ .root_source_file = b.path("../src/control-rule-vectors.json") });
    store_role_tests.root_module.addOptions("identity_stub_options", stub_options);
    const run_store_role_tests = b.addRunArtifact(store_role_tests);

    // The store role killed between and during commits: the executable
    // spawns itself as the role it then kills.
    const store_role_process_tests = b.addExecutable(.{
        .name = "store-role-process-tests",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/store_role_process_tests.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = role_imports,
        }),
    });
    store_role_process_tests.root_module.addAnonymousImport("identity-quarantine", .{ .root_source_file = quarantine });
    const run_store_role_process_tests = b.addRunArtifact(store_role_process_tests);
    run_store_role_process_tests.has_side_effects = true;

    // Needs python3 and openssl on PATH for the loopback stand-in.
    const identity_bench = b.addExecutable(.{
        .name = "identity-bench",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/identity_bench.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = role_imports,
        }),
    });
    identity_bench.root_module.addOptions("identity_stub_options", stub_options);
    const run_identity_bench = b.addRunArtifact(identity_bench);
    run_identity_bench.addPassthruArgs();
    const identity_bench_step = b.step("bench-identity", "Measure the CPU cost of identity resolution against a loopback stand-in");
    identity_bench_step.dependOn(&run_identity_bench.step);

    // Builds its synthetic store under .zig-cache/collector-bench on the
    // first run; docs/evals/collector-cpu-20261112 has the method.
    const collector_bench = b.addExecutable(.{
        .name = "collector-bench",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/collector_bench.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
            .imports = role_imports,
        }),
    });
    collector_bench.root_module.addAnonymousImport("identity-quarantine", .{ .root_source_file = quarantine });
    const run_collector_bench = b.addRunArtifact(collector_bench);
    run_collector_bench.addPassthruArgs();
    const collector_bench_step = b.step("bench-collector", "Measure the CPU cost of the store role's relay path on a synthetic stream");
    collector_bench_step.dependOn(&run_collector_bench.step);

    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_store_role_tests.step);
    test_step.dependOn(&run_store_role_process_tests.step);
    test_step.dependOn(&run_unit_tests.step);
    test_step.dependOn(&run_ingest_tests.step);
    test_step.dependOn(&run_store_tests.step);
    test_step.dependOn(&run_store_process_tests.step);
    test_step.dependOn(&run_bridge_tests.step);
}
