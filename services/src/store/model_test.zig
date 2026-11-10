//! Model-based test: a long random sequence of every kind of change goes to
//! a real directory store and to the in-memory model, and every so often
//! the whole store is scanned and compared.
//!
//! A plain seeded loop, not hegel-zig. Each case is one store on disk
//! carried through thousands of changes, with reopens, maintenance and
//! trimming in between; that is one long history per seed, where hegel's
//! strength is many short ones it can shrink. A failure prints its seed and
//! batch; `STORE_TEST_SEED=<n> zig build test` runs that seed alone.

const std = @import("std");
const harness = @import("testing/harness.zig");
const verify = @import("testing/verify.zig");
const workload = @import("testing/workload.zig");
const Model = @import("testing/model.zig").Model;
const Store = @import("store.zig").Store;
const Reader = @import("reader.zig").Reader;

const Plan = struct {
    seed: u64,
    batches: u64 = 220,
    batch_size: usize = 24,
    verify_every: u64 = 20,
    trim: bool = true,
};

const Result = struct {
    tally: harness.Tally,
    verifications: u64 = 0,
    reopens: u64 = 0,
    trims: u64 = 0,
    flushes: u64 = 0,
    compactions: u64 = 0,
    consumed: u64 = 0,
};

fn run(plan: Plan) !Result {
    const gpa = std.testing.allocator;
    const io = std.testing.io;
    var tmp = std.testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    var prng: std.Random.DefaultPrng = .init(plan.seed ^ 0x5eed);
    const random = prng.random();

    var model: Model = .init(gpa);
    defer model.deinit();
    model.flagged = workload.flagged;
    var bridge: harness.Bridge = .init(gpa);
    defer bridge.deinit();
    var store = try Store.open(gpa, io, tmp.dir, "store", harness.options(null));
    defer store.close();
    var result: Result = .{ .tally = .{} };
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();

    var n: u64 = 1;
    errdefer std.debug.print("model test failed: seed {d}, batch {d} of {d}\n", .{ plan.seed, n, plan.batches });
    while (n <= plan.batches) : (n += 1) {
        _ = arena.reset(.retain_capacity);
        const step = try workload.batch(arena.allocator(), .{ .seed = plan.seed }, n, plan.batch_size);
        try harness.applyStep(store, &model, step, &result.tally);
        try harness.sweepDomains(gpa, store, &model, step, 16, &result.tally);
        _ = try store.maintainStep();
        try bridge.consume(store);

        if (random.uintLessThan(u8, 10) == 0) {
            try store.setCursor(.{ .bridge = bridge.position });
            model.setCursor(.{ .bridge = bridge.position });
        }
        if (plan.trim and random.uintLessThan(u8, 12) == 0) {
            const below = bridge.position -| random.uintLessThan(u64, 30);
            while ((try store.trimChanges(below, 7)).more) {}
            model.trim(below);
            result.trims += 1;
        }
        if (random.uintLessThan(u8, 25) == 0) {
            const stats = store.stats();
            result.flushes += stats.flushes.count;
            result.compactions += stats.compactions;
            store.close();
            store = try Store.open(gpa, io, tmp.dir, "store", harness.options(null));
            result.reopens += 1;
        }
        if (n % plan.verify_every == 0 or n == plan.batches) {
            _ = try verify.check(gpa, store.db(), &model);
            try verify.checkReads(gpa, store, &model, random);
            try bridge.check(&model);
            result.verifications += 1;
        }
    }

    const stats = store.stats();
    result.flushes += stats.flushes.count;
    result.compactions += stats.compactions;
    result.consumed = bridge.consumed;

    const reader = try Reader.open(gpa, io, tmp.dir, "store", harness.small_layout);
    defer reader.close();
    _ = try verify.check(gpa, reader.db(), &model);
    try verify.checkReads(gpa, reader, &model, random);
    return result;
}

fn seedFromEnvironment() ?u64 {
    const text = std.testing.environ.getPosix("STORE_TEST_SEED") orelse return null;
    return std.fmt.parseInt(u64, text, 10) catch null;
}

test "random changes of every kind keep the store equal to the model" {
    const seeds: []const u64 = if (seedFromEnvironment()) |seed| &.{seed} else &.{ 1, 20261110 };
    for (seeds) |seed| {
        const result = try run(.{ .seed = seed });
        if (std.testing.environ.getPosix("STORE_TEST_REPORT") != null) report(seed, result);
        try std.testing.expectEqual(@as(u64, 220 * 24), result.tally.changes);
        try std.testing.expect(result.tally.everyKindApplied());
        try std.testing.expect(result.verifications >= 11);
        try std.testing.expect(result.flushes > 0 and result.compactions > 0);
        try std.testing.expect(result.reopens > 0 and result.trims > 0);
    }
}

fn report(seed: u64, result: Result) void {
    std.debug.print("seed {d}: {d} changes in {d} batches, {d} verifications, {d} reopens, {d} trims, {d} flushes, {d} compactions, {d} sweeps over {d} handle keys, {d} log entries consumed\n", .{
        seed,           result.tally.changes, result.tally.batches, result.verifications, result.reopens,  result.trims,
        result.flushes, result.compactions,   result.tally.sweeps,  result.tally.swept,   result.consumed,
    });
    for (std.enums.values(harness.Kind)) |kind| {
        std.debug.print("  {t}: {d} generated, {d} applied, {d} altered an actor\n", .{ kind, result.tally.seen.get(kind), result.tally.applied.get(kind), result.tally.altered.get(kind) });
    }
}

test "replaying the change log from zero rebuilds what a scan of the actors shows" {
    const gpa = std.testing.allocator;
    const io = std.testing.io;
    var tmp = std.testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    var model: Model = .init(gpa);
    defer model.deinit();
    model.flagged = workload.flagged;
    var tally: harness.Tally = .{};
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    {
        const store = try Store.open(gpa, io, tmp.dir, "store", harness.options(null));
        defer store.close();
        for (1..121) |n| {
            _ = arena.reset(.retain_capacity);
            const step = try workload.batch(arena.allocator(), .{ .seed = 99 }, n, 20);
            try harness.applyStep(store, &model, step, &tally);
            try harness.sweepDomains(gpa, store, &model, step, 9, &tally);
            _ = try store.maintainStep();
        }
    }
    const reader = try Reader.open(gpa, io, tmp.dir, "store", harness.small_layout);
    defer reader.close();
    var bridge: harness.Bridge = .init(gpa);
    defer bridge.deinit();
    try bridge.consume(reader);
    try bridge.check(&model);
    try std.testing.expectEqual(model.nextChange(), bridge.position);
    try std.testing.expect(bridge.consumed > model.actors.count());

    var visible: usize = 0;
    for (model.actors.keys(), model.actors.values()) |did, actor| {
        const indexed = bridge.index.get(did).?;
        try std.testing.expectEqual(actor.ranked(), indexed.ranked());
        try std.testing.expectEqual(actor.hidden, indexed.hidden);
        try std.testing.expectEqual(actor.status, indexed.status);
        visible += @intFromBool(actor.status == .live and !actor.hidden and actor.handle.len > 0);
    }
    try std.testing.expect(visible > 0);
}
