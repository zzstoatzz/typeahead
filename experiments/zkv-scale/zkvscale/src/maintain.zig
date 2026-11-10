const std = @import("std");
const args_mod = @import("args.zig");
const store = @import("store.zig");
const sys = @import("sys.zig");
const Db = store.Db;

pub const flags = [_]args_mod.Flag{
    .{ .name = "--flush-bytes" },
    .{ .name = "--max-segments" },
    .{ .name = "--step-bytes" },
    .{ .name = "--log-gc-names" },
    .{ .name = "--l0-stall" },
};

pub const Policy = struct {
    flush_bytes: usize = 8 << 20,
    max_segments: u64 = 256,
    step_bytes: usize = 4 << 20,
    log_gc_names: usize = 64,
    l0_stall: usize = 12,

    pub fn parse(args: args_mod.Args) !Policy {
        const defaults: Policy = .{};
        return .{
            .flush_bytes = try args.int(usize, "--flush-bytes", defaults.flush_bytes),
            .max_segments = try args.int(u64, "--max-segments", defaults.max_segments),
            .step_bytes = try args.int(usize, "--step-bytes", defaults.step_bytes),
            .log_gc_names = try args.int(usize, "--log-gc-names", defaults.log_gc_names),
            .l0_stall = try args.int(usize, "--l0-stall", defaults.l0_stall),
        };
    }
};

pub const Stats = struct {
    steps: u64 = 0,
    slowest_step_us: u64 = 0,
    flushes: u64 = 0,
    flush_ms: u64 = 0,
    flush_max_us: u64 = 0,
    compact_steps: u64 = 0,
    compactions: u64 = 0,
    compact_ms: u64 = 0,
    compact_max_us: u64 = 0,
    table_collections: u64 = 0,
    table_collect_ms: u64 = 0,
    table_collect_max_us: u64 = 0,
    log_gc_steps: u64 = 0,
    log_gc_ms: u64 = 0,
    log_gc_max_us: u64 = 0,
    stalls: u64 = 0,
    stall_steps: u64 = 0,
    stall_ms: u64 = 0,
    stall_max_us: u64 = 0,
};

pub const Maintainer = struct {
    db: *Db,
    io: std.Io,
    policy: Policy,
    collector: Db.LogCollector = .{},
    log_gc_pending: bool = false,
    flush_ns: u64 = 0,
    compact_ns: u64 = 0,
    table_collect_ns: u64 = 0,
    log_gc_ns: u64 = 0,
    stall_ns: u64 = 0,
    stats: Stats = .{},

    pub fn deinit(self: *Maintainer) void {
        self.collector.deinit(self.db.gpa);
    }

    fn timed(self: *Maintainer, started: u64, total: *u64, max_us: *u64) void {
        const elapsed = sys.now(self.io) - started;
        total.* += elapsed;
        max_us.* = @max(max_us.*, elapsed / std.time.ns_per_us);
    }

    pub fn flush(self: *Maintainer) !void {
        const started = sys.now(self.io);
        try self.db.flush();
        self.stats.flushes += 1;
        self.timed(started, &self.flush_ns, &self.stats.flush_max_us);
        self.log_gc_pending = true;
    }

    fn collectTables(self: *Maintainer) !void {
        const started = sys.now(self.io);
        try self.db.collectTables();
        self.stats.table_collections += 1;
        self.timed(started, &self.table_collect_ns, &self.stats.table_collect_max_us);
    }

    fn compactStep(self: *Maintainer, bytes: usize) !bool {
        const started = sys.now(self.io);
        const advanced = try self.db.compactStep(bytes);
        if (!advanced) return false;
        self.stats.compact_steps += 1;
        self.timed(started, &self.compact_ns, &self.stats.compact_max_us);
        if (self.db.compactionPhase() == .idle) {
            self.stats.compactions += 1;
            try self.collectTables();
        }
        return true;
    }

    fn logGcStep(self: *Maintainer) !void {
        const started = sys.now(self.io);
        const applied = if (self.collector.names != null) self.collector.applied else self.db.version.applied;
        const more = try self.db.collectLogStep(&self.collector, self.policy.log_gc_names);
        self.log_gc_pending = more or self.db.version.applied > applied;
        self.stats.log_gc_steps += 1;
        self.timed(started, &self.log_gc_ns, &self.stats.log_gc_max_us);
    }

    pub fn step(self: *Maintainer) !bool {
        const started = sys.now(self.io);
        var worked = false;
        if (self.db.unflushedSegments() >= self.policy.max_segments or self.db.memtableBytes() >= self.policy.flush_bytes) {
            try self.flush();
            worked = true;
        }
        if (try self.compactStep(self.policy.step_bytes)) worked = true;
        if (self.log_gc_pending) {
            try self.logGcStep();
            worked = true;
        }
        if (worked) {
            self.stats.steps += 1;
            self.stats.slowest_step_us = @max(self.stats.slowest_step_us, (sys.now(self.io) - started) / std.time.ns_per_us);
        }
        return worked;
    }

    pub fn relieve(self: *Maintainer) !void {
        if (self.db.version.tables[0].len <= self.policy.l0_stall) return;
        const started = sys.now(self.io);
        self.stats.stalls += 1;
        while (self.db.version.tables[0].len >= self.db.options.l0_tables) {
            if (!try self.step()) break;
            self.stats.stall_steps += 1;
        }
        self.timed(started, &self.stall_ns, &self.stats.stall_max_us);
    }

    pub fn drain(self: *Maintainer, step_bytes: usize, watch: ?*sys.DiskWatch) !void {
        while (try self.compactStep(step_bytes)) {
            if (watch) |w| try w.poll();
        }
        if (watch) |w| _ = try w.sample();
        const started = sys.now(self.io);
        try self.db.collect();
        self.collector.deinit(self.db.gpa);
        self.log_gc_pending = false;
        self.stats.table_collections += 1;
        self.timed(started, &self.table_collect_ns, &self.stats.table_collect_max_us);
    }

    pub fn report(self: *Maintainer) Stats {
        var result = self.stats;
        result.flush_ms = self.flush_ns / std.time.ns_per_ms;
        result.compact_ms = self.compact_ns / std.time.ns_per_ms;
        result.table_collect_ms = self.table_collect_ns / std.time.ns_per_ms;
        result.log_gc_ms = self.log_gc_ns / std.time.ns_per_ms;
        result.stall_ms = self.stall_ns / std.time.ns_per_ms;
        return result;
    }
};
