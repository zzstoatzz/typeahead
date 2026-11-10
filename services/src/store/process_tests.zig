//! Tests that need more than one process. Run with no arguments this is the
//! parent: it spawns itself as a writer, kills it or reads beside it, and
//! checks the store against the model. `zig build test` runs it.
//!
//! The kill test's workload is deterministic; where a kill lands is not. A
//! round is classified by the last thing the child reported before it died.

const std = @import("std");
const harness = @import("testing/harness.zig");
const maintain = @import("maintain.zig");
const scan = @import("scan.zig");
const verify = @import("testing/verify.zig");
const workload = @import("testing/workload.zig");
const Model = @import("testing/model.zig").Model;
const Reader = @import("reader.zig").Reader;
const Store = @import("store.zig").Store;
const Io = std.Io;

const kill_batch = 12;
const done_mark = 0x646f6e65;

pub fn main(init: std.process.Init) !void {
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    if (args.len >= 4 and std.mem.eql(u8, args[1], "kill-child")) return killChild(init, args[2], try std.fmt.parseInt(u64, args[3], 10));
    if (args.len >= 6 and std.mem.eql(u8, args[1], "write-child")) {
        return writeChild(init, args[2], try std.fmt.parseInt(u64, args[3], 10), try std.fmt.parseInt(usize, args[4], 10), try std.fmt.parseInt(u64, args[5], 10));
    }

    const seed = if (init.environ_map.get("STORE_TEST_SEED")) |text| try std.fmt.parseInt(u64, text, 10) else 20261110;
    var random_bytes: [8]u8 = undefined;
    init.io.random(&random_bytes);
    var name_buf: [64]u8 = undefined;
    const scratch = try std.fmt.bufPrint(&name_buf, ".zig-cache/tmp/store-process-{x}", .{std.mem.readInt(u64, &random_bytes, .little)});
    const cwd = Io.Dir.cwd();
    try cwd.createDirPath(init.io, scratch);
    defer cwd.deleteTree(init.io, scratch) catch {};
    const self_path = try std.process.executablePathAlloc(init.io, init.arena.allocator());

    var out_buf: [512]u8 = undefined;
    var out = Io.File.stdout().writerStreaming(init.io, &out_buf);
    const parent: Parent = .{ .gpa = init.gpa, .io = init.io, .scratch = scratch, .self_path = self_path, .seed = seed, .out = &out.interface };
    try parent.killTest(std.mem.readInt(u64, &random_bytes, .little));
    try parent.readerTest();
    try out.interface.flush();
}

fn mark(io: Io, comptime fmt: []const u8, args: anytype) void {
    var buf: [48]u8 = undefined;
    const line = std.fmt.bufPrint(&buf, fmt ++ "\n", args) catch unreachable;
    Io.File.stdout().writeStreamingAll(io, line) catch std.process.exit(3);
}

const Tracer = struct {
    io: Io,

    fn call(context: ?*anyopaque, phase: maintain.Phase, edge: maintain.Edge) void {
        const self: *Tracer = @ptrCast(@alignCast(context.?));
        const letter: u8 = switch (phase) {
            .flush => 'F',
            .compact => 'K',
            .collect_tables => 'T',
            .collect_log => 'L',
        };
        mark(self.io, "{c}", .{if (edge == .begin) letter else std.ascii.toLower(letter)});
    }
};

fn killOptions(trace: ?maintain.Trace) @import("store.zig").Options {
    var options = harness.options(trace);
    options.maintenance.compact_bytes = 16 << 10;
    return options;
}

/// Apply batches from the stored relay cursor on, forever, reporting each
/// stage on stdout. The parent kills this process.
fn killChild(init: std.process.Init, path: []const u8, seed: u64) !void {
    const gpa = init.gpa;
    var tracer: Tracer = .{ .io = init.io };
    const store = try Store.open(gpa, init.io, Io.Dir.cwd(), path, killOptions(.{ .context = &tracer, .call = Tracer.call }));
    defer store.close();
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    var n = ((try store.cursors(arena.allocator())).relay orelse 0) + 1;
    while (true) : (n += 1) {
        _ = arena.reset(.retain_capacity);
        const step = try workload.batch(arena.allocator(), .{ .seed = seed, .inline_sweep = true }, n, kill_batch);
        mark(init.io, "B {d}", .{n});
        const batch = try store.begin(step.now);
        for (step.changes) |one| _ = try batch.apply(one);
        for (step.cursors) |update| try batch.setCursor(update);
        mark(init.io, "C {d}", .{n});
        try batch.commit();
        mark(init.io, "A {d}", .{n});
        _ = try store.maintainStep();
    }
}

/// Apply `count` more batches with maintenance after each, settle, and
/// leave a mark the reader can see.
fn writeChild(init: std.process.Init, path: []const u8, seed: u64, universe: usize, count: u64) !void {
    const gpa = init.gpa;
    const store = try Store.open(gpa, init.io, Io.Dir.cwd(), path, harness.options(null));
    defer store.close();
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    const first = ((try store.cursors(arena.allocator())).relay orelse 0) + 1;
    for (first..first + count) |n| {
        _ = arena.reset(.retain_capacity);
        const step = try workload.batch(arena.allocator(), .{ .seed = seed, .universe = universe, .inline_sweep = true, .keep_actors = true }, n, 30);
        const batch = try store.begin(step.now);
        for (step.changes) |one| _ = try batch.apply(one);
        for (step.cursors) |update| try batch.setCursor(update);
        try batch.commit();
        _ = try store.maintainStep();
        if (n % 40 == 0) try store.maintainer.settle();
    }
    try store.maintainer.settle();
    const stats = store.stats();
    try store.setCursor(.{ .bridge = done_mark });
    mark(init.io, "{d} {d} {d} {d}", .{ stats.flushes.count, stats.compactions, stats.table_collections.count, stats.log_collections.count });
}

/// A finished compaction collects its inputs inside the same step, so the
/// child reports table collection nested in a compaction step.
const Landed = enum { staging, commit, acknowledged, flush, compaction, table_collection, log_collection };

const Round = struct {
    landed: Landed,
    acknowledged: u64,
    committing: u64,
};

const Target = enum { commit, flush, compaction, collection, anywhere };

const Parent = struct {
    gpa: std.mem.Allocator,
    io: Io,
    scratch: []const u8,
    self_path: []const u8,
    seed: u64,
    out: *Io.Writer,

    fn fail(comptime fmt: []const u8, args: anytype) error{ProcessTestFailed} {
        std.debug.print("store process test failed: " ++ fmt ++ "\n", args);
        return error.ProcessTestFailed;
    }

    /// Start a kill child, wait for the `occurrence`-th time it enters
    /// `target`, wait `delay_us` more, and SIGKILL it.
    fn killRound(self: Parent, path: []const u8, target: Target, occurrence: u32, delay_us: u32, started_after: u64) !Round {
        var seed_buf: [24]u8 = undefined;
        var child = try std.process.spawn(self.io, .{
            .argv = &.{ self.self_path, "kill-child", path, try std.fmt.bufPrint(&seed_buf, "{d}", .{self.seed}) },
            .stdout = .pipe,
        });
        const pipe = child.stdout.?;
        var round: Round = .{ .landed = .staging, .acknowledged = started_after, .committing = started_after };
        var buf: [4096]u8 = undefined;
        var held: usize = 0;
        var seen: u32 = 0;
        var lines: u32 = 0;
        var killed = false;
        while (true) {
            var slices = [_][]u8{buf[held..]};
            const got = pipe.readStreaming(self.io, &slices) catch |err| switch (err) {
                error.EndOfStream => 0,
                else => return err,
            };
            if (got == 0) break;
            held += got;
            var start: usize = 0;
            while (std.mem.indexOfScalarPos(u8, buf[0..held], start, '\n')) |end| : (start = end + 1) {
                const line = buf[start..end];
                lines += 1;
                const entered: ?Target = switch (line[0]) {
                    'B' => blk: {
                        round.landed = .staging;
                        break :blk null;
                    },
                    'C' => blk: {
                        round.landed = .commit;
                        round.committing = try std.fmt.parseInt(u64, line[2..], 10);
                        break :blk .commit;
                    },
                    'A' => blk: {
                        round.landed = .acknowledged;
                        round.acknowledged = try std.fmt.parseInt(u64, line[2..], 10);
                        break :blk null;
                    },
                    'F' => blk: {
                        round.landed = .flush;
                        break :blk .flush;
                    },
                    'K' => blk: {
                        round.landed = .compaction;
                        break :blk .compaction;
                    },
                    'T' => blk: {
                        round.landed = .table_collection;
                        break :blk .collection;
                    },
                    'L' => blk: {
                        round.landed = .log_collection;
                        break :blk .collection;
                    },
                    't' => blk: {
                        round.landed = .compaction;
                        break :blk null;
                    },
                    'f', 'k', 'l' => blk: {
                        round.landed = .acknowledged;
                        break :blk null;
                    },
                    else => return fail("the kill child wrote {s}", .{line}),
                };
                if (killed) continue;
                if (entered != null and (entered.? == target or target == .anywhere)) seen += 1;
                if (seen >= occurrence or lines > 4000) {
                    if (delay_us > 0) try self.io.sleep(.fromMicroseconds(delay_us), .awake);
                    try std.posix.kill(child.id.?, .KILL);
                    killed = true;
                }
            }
            std.mem.copyForwards(u8, &buf, buf[start..held]);
            held -= start;
        }
        const term = try child.wait(self.io);
        if (!killed or term != .signal or term.signal != .KILL) return fail("the kill child ended on its own: {f}", .{term});
        return round;
    }

    fn killTest(self: Parent, entropy: u64) !void {
        var path_buf: [128]u8 = undefined;
        const path = try std.fmt.bufPrint(&path_buf, "{s}/kill", .{self.scratch});
        var prng: std.Random.DefaultPrng = .init(entropy);
        const random = prng.random();
        var model: Model = .init(self.gpa);
        defer model.deinit();
        model.flagged = workload.flagged;
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();

        var landed: std.EnumArray(Landed, u32) = .initFill(0);
        var applied: u64 = 0;
        var unacknowledged: u32 = 0;
        var rounds: u32 = 0;
        const wanted = 10;
        while (rounds < 400) : (rounds += 1) {
            if (rounds >= 40 and landed.get(.commit) >= wanted and landed.get(.flush) >= wanted and landed.get(.compaction) >= wanted) break;
            const target: Target = if (landed.get(.compaction) < wanted and rounds % 5 >= 3) .compaction else std.enums.values(Target)[rounds % 5];
            const delay_us = if (target == .collection or target == .compaction) 0 else random.uintLessThan(u32, 3) * random.uintLessThan(u32, 900);
            const round = try self.killRound(path, target, 1 + random.uintLessThan(u32, 6), delay_us, applied);
            landed.getPtr(round.landed).* += 1;

            const with_reader = rounds % 2 == 0;
            const store: ?*Store = if (with_reader) null else try Store.open(self.gpa, self.io, Io.Dir.cwd(), path, killOptions(null));
            defer if (store) |s| s.close();
            const reader: ?*Reader = if (with_reader) try Reader.open(self.gpa, self.io, Io.Dir.cwd(), path, harness.small_layout) else null;
            defer if (reader) |r| r.close();
            const db = if (store) |s| s.db() else reader.?.db();

            _ = arena.reset(.retain_capacity);
            const cursor = (try @import("read.zig").cursors(db, arena.allocator())).relay orelse 0;
            if (cursor < round.acknowledged) return fail("batch {d} was acknowledged, the store holds up to {d}", .{ round.acknowledged, cursor });
            if (cursor > round.acknowledged) {
                if (cursor != round.acknowledged + 1 or round.committing != cursor) return fail("the store holds batch {d}, the child had acknowledged {d} and was committing {d}", .{ cursor, round.acknowledged, round.committing });
                unacknowledged += 1;
            }
            while (applied < cursor) {
                applied += 1;
                _ = arena.reset(.retain_capacity);
                const step = try workload.batch(arena.allocator(), .{ .seed = self.seed, .inline_sweep = true }, applied, kill_batch);
                model.begin(step.now);
                for (step.changes) |one| _ = model.apply(one);
                for (step.cursors) |update| model.setCursor(update);
                model.commit();
            }
            errdefer std.debug.print("after kill {d} in {t}: acknowledged {d}, committing {d}, stored {d}, seed {d}\n", .{ rounds, round.landed, round.acknowledged, round.committing, cursor, self.seed });
            _ = try verify.check(self.gpa, db, &model);
            if (store) |s| try verify.checkReads(self.gpa, s, &model, random) else try verify.checkReads(self.gpa, reader.?, &model, random);
        }
        if (landed.get(.commit) < wanted or landed.get(.flush) < wanted or landed.get(.compaction) < wanted) {
            return fail("{d} kills did not cover every phase: commit {d}, flush {d}, compaction {d}", .{ rounds, landed.get(.commit), landed.get(.flush), landed.get(.compaction) });
        }
        try self.out.print("store kill test: seed {d}, {d} kills, {d} batches verified ({d} changes); landed in commit {d}, flush {d}, compaction step {d}, table collection {d}, log collection {d}, staging {d}, between operations {d}; {d} commits were durable before their acknowledgement\n", .{
            self.seed,                     rounds,                      applied,              applied * kill_batch,      landed.get(.commit), landed.get(.flush), landed.get(.compaction),
            landed.get(.table_collection), landed.get(.log_collection), landed.get(.staging), landed.get(.acknowledged), unacknowledged,
        });
    }

    const Ordered = struct {
        gpa: std.mem.Allocator,
        last: std.ArrayList(u8) = .empty,
        entries: u64 = 0,

        pub fn entry(self: *Ordered, key: []const u8, _: []const u8) !void {
            if (std.mem.order(u8, self.last.items, key) != .lt) return error.OutOfOrder;
            self.last.clearRetainingCapacity();
            try self.last.appendSlice(self.gpa, key);
            self.entries += 1;
        }
    };

    fn readerTest(self: Parent) !void {
        const universe = 1500;
        const preload = 10;
        const written = 160;
        var path_buf: [128]u8 = undefined;
        const path = try std.fmt.bufPrint(&path_buf, "{s}/read", .{self.scratch});
        var model: Model = .init(self.gpa);
        defer model.deinit();
        model.flagged = workload.flagged;
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const config: workload.Config = .{ .seed = self.seed, .universe = universe, .inline_sweep = true, .keep_actors = true };

        {
            const store = try Store.open(self.gpa, self.io, Io.Dir.cwd(), path, harness.options(null));
            defer store.close();
            var tally: harness.Tally = .{};
            var at: usize = 0;
            while (at < universe) : (at += 250) {
                _ = arena.reset(.retain_capacity);
                var changes: std.ArrayList(@import("change.zig").Change) = .empty;
                for (at..@min(at + 250, universe)) |index| {
                    const did = try workload.didOf(arena.allocator(), index);
                    try changes.append(arena.allocator(), .{ .profile = .{ .did = did, .handle = try std.fmt.allocPrint(arena.allocator(), "seed{d}.example.test", .{index % 700}) } });
                }
                try harness.applyStep(store, &model, .{ .now = workload.base_time, .changes = changes.items, .cursors = &.{} }, &tally);
            }
            for (1..preload + 1) |n| {
                _ = arena.reset(.retain_capacity);
                try harness.applyStep(store, &model, try workload.batch(arena.allocator(), config, n, 30), &tally);
                _ = try store.maintainStep();
            }
        }

        var number_bufs: [3][24]u8 = undefined;
        var child = try std.process.spawn(self.io, .{
            .argv = &.{
                self.self_path,                                             "write-child",                                             path,
                try std.fmt.bufPrint(&number_bufs[0], "{d}", .{self.seed}), try std.fmt.bufPrint(&number_bufs[1], "{d}", .{universe}), try std.fmt.bufPrint(&number_bufs[2], "{d}", .{written}),
            },
            .stdout = .pipe,
        });
        errdefer child.kill(self.io);

        const reader = try Reader.open(self.gpa, self.io, Io.Dir.cwd(), path, harness.small_layout);
        defer reader.close();
        var scans: u64 = 0;
        var overlapping: u64 = 0;
        var totals: scan.Stats = .{};
        var finished = false;
        while (!finished) : (scans += 1) {
            _ = arena.reset(.retain_capacity);
            try reader.refresh();
            const before = try reader.cursors(arena.allocator());
            var ordered: Ordered = .{ .gpa = self.gpa };
            defer ordered.last.deinit(self.gpa);
            var position: scan.Position = .{};
            defer position.deinit(self.gpa);
            const stats = try reader.scanRange(&position, .actor, .{ .page = 40, .refresh = scans % 3 != 2 }, &ordered);
            if (ordered.entries != universe) return fail("scan {d} of the actors delivered {d} of {d}", .{ scans, ordered.entries, universe });
            totals.pages += stats.pages;
            totals.refreshes += stats.refreshes;
            totals.missing += stats.missing;
            totals.entries += stats.entries;
            try reader.refresh();
            const after = try reader.cursors(arena.allocator());
            overlapping += @intFromBool((after.relay orelse 0) > (before.relay orelse 0));
            finished = before.bridge == done_mark;
        }

        var line_buf: [128]u8 = undefined;
        var pipe_reader = child.stdout.?.readerStreaming(self.io, &line_buf);
        const line = try pipe_reader.interface.takeDelimiter('\n') orelse return fail("the writer reported nothing", .{});
        var fields = std.mem.tokenizeScalar(u8, line, ' ');
        var writer_stats: [4]u64 = undefined;
        for (&writer_stats) |*slot| slot.* = try std.fmt.parseInt(u64, fields.next() orelse return fail("the writer reported {s}", .{line}), 10);
        const term = try child.wait(self.io);
        if (!term.success()) return fail("the writer {f}", .{term});

        for (preload + 1..preload + written + 1) |n| {
            _ = arena.reset(.retain_capacity);
            const step = try workload.batch(arena.allocator(), config, n, 30);
            model.begin(step.now);
            for (step.changes) |one| _ = model.apply(one);
            for (step.cursors) |update| model.setCursor(update);
            model.commit();
        }
        model.setCursor(.{ .bridge = done_mark });
        try reader.refresh();
        var prng: std.Random.DefaultPrng = .init(self.seed);
        const summary = try verify.check(self.gpa, reader.db(), &model);
        try verify.checkReads(self.gpa, reader, &model, prng.random());
        if (summary.actors != universe) return fail("the store ends with {d} actors", .{summary.actors});
        if (overlapping < 5 or writer_stats[1] == 0 or writer_stats[2] == 0) {
            return fail("the scans did not run beside the writer: {d} of {d} overlapped commits, {d} compactions, {d} table collections", .{ overlapping, scans, writer_stats[1], writer_stats[2] });
        }
        try self.out.print("store reader test: seed {d}, {d} actors, {d} full scans while the writer ran ({d} spanned commits), every scan delivered {d} actors in order; {d} pages, {d} refreshes, {d} resumed after a collected table; writer: {d} batches, {d} flushes, {d} compactions, {d} table collections, {d} log collection steps; final state equal to the model through the reader\n", .{
            self.seed,       universe,        scans,           overlapping,     universe, totals.pages, totals.refreshes, totals.missing, written,
            writer_stats[0], writer_stats[1], writer_stats[2], writer_stats[3],
        });
    }
};
