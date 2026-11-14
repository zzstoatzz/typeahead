//! Stepped maintenance for the single writer. zkv does no background work
//! and has no backpressure of its own: the owner calls `step` between
//! commits, and `relieve` decides what happens when level 0 builds up.
//!
//! The thresholds are the ones the 2026-11-10 measurement ran with
//! (docs/history/evals/zkv-scale-20261110). Under steady writes of 30 commits a
//! minute they never stalled and the slowest step took 40 ms. Under a bulk
//! load of 13.2 million actors they stalled 74 times for 717 s in total,
//! 12 s at the longest. No other thresholds have been measured.

const std = @import("std");
const engine = @import("engine.zig");
const Io = std.Io;
const Db = engine.Db;

pub const Mode = enum {
    /// Serving writes. `relieve` does a bounded number of steps and returns,
    /// so one call never holds the writer for a whole compaction; the caller
    /// reads `pressure` and slows its intake.
    steady,
    /// Loading faster than compaction can absorb. `relieve` blocks until
    /// level 0 is back under the compaction trigger, which is the rule the
    /// measured load ran under.
    bulk_load,
};

pub const Policy = struct {
    mode: Mode = .steady,
    /// Flush when the memtable holds this much.
    flush_bytes: usize = 8 << 20,
    /// Flush when this many committed log segments are unflushed, which
    /// bounds what a restart replays.
    flush_segments: u64 = 256,
    /// Merge budget of one compaction step.
    compact_bytes: usize = 4 << 20,
    /// Log segment names examined by one collection step.
    log_names: usize = 64,
    /// Level-0 tables above which the writer is under pressure.
    stall_tables: usize = 12,
    /// Steps one `relieve` call may take in steady mode.
    relief_steps: usize = 8,

    pub const steady: Policy = .{};
    pub const bulk_load: Policy = .{ .mode = .bulk_load };
};

pub const Phase = enum { flush, compact, collect_tables, collect_log };
pub const Edge = enum { begin, end };

/// Called around every maintenance operation, for spans and for tests that
/// need to know which operation a process was in.
pub const Trace = struct {
    context: ?*anyopaque = null,
    call: *const fn (context: ?*anyopaque, phase: Phase, edge: Edge) void,
};

pub const Pressure = enum { none, stalled };

pub const Timing = struct {
    count: u64 = 0,
    total_ns: u64 = 0,
    longest_ns: u64 = 0,

    fn add(self: *Timing, elapsed: u64) void {
        self.count += 1;
        self.total_ns += elapsed;
        self.longest_ns = @max(self.longest_ns, elapsed);
    }
};

pub const Level = struct { tables: usize = 0, bytes: u64 = 0 };

pub const Disk = struct { bytes: u64 = 0, files: u64 = 0 };

pub const Stats = struct {
    tables: usize,
    table_bytes: u64,
    levels: [engine.level_count]Level,
    memtable_bytes: usize,
    unflushed_segments: u64,
    resident_metadata_bytes: usize,
    cache_bytes: usize,
    compaction_active: bool,
    /// Unix seconds when a compaction last published, 0 if none has since open.
    last_compaction_at: u64,
    last_compaction_ns: u64,
    pressure: Pressure,
    steps: Timing,
    flushes: Timing,
    compaction_steps: Timing,
    compactions: u64,
    table_collections: Timing,
    log_collections: Timing,
    /// Times the writer waited on level 0, how long in total and at worst.
    stalls: Timing,
    stall_steps: u64,
    /// Steady-mode relief that ran out of steps with level 0 still high.
    unrelieved: u64,
};

pub const Maintainer = struct {
    db: *Db,
    io: Io,
    policy: Policy,
    trace: ?Trace = null,
    collector: Db.LogCollector = .{},
    log_pending: bool = false,
    compaction_started: ?u64 = null,
    last_compaction_at: u64 = 0,
    last_compaction_ns: u64 = 0,
    compactions: u64 = 0,
    stall_steps: u64 = 0,
    unrelieved: u64 = 0,
    steps: Timing = .{},
    flushes: Timing = .{},
    compaction_steps: Timing = .{},
    table_collections: Timing = .{},
    log_collections: Timing = .{},
    stalls: Timing = .{},

    pub fn deinit(self: *Maintainer) void {
        self.collector.deinit(self.db.gpa);
    }

    fn clock(self: *const Maintainer) u64 {
        return @intCast(Io.Clock.awake.now(self.io).nanoseconds);
    }

    fn mark(self: *const Maintainer, phase: Phase, edge: Edge) void {
        if (self.trace) |trace| trace.call(trace.context, phase, edge);
    }

    pub fn flushDue(self: *const Maintainer) bool {
        return self.db.unflushedSegments() >= self.policy.flush_segments or self.db.memtableBytes() >= self.policy.flush_bytes;
    }

    pub fn flush(self: *Maintainer) !void {
        const started = self.clock();
        self.mark(.flush, .begin);
        try self.db.flush();
        self.mark(.flush, .end);
        self.flushes.add(self.clock() - started);
        self.log_pending = true;
    }

    /// Advance the planned compaction by one budget. False when nothing is
    /// planned. A finished compaction's inputs are collected at once, so the
    /// disk it doubled is returned before the next one starts.
    pub fn compactStep(self: *Maintainer) !bool {
        const started = self.clock();
        const planned = compactionPlanned(self.db);
        if (planned) self.mark(.compact, .begin);
        const advanced = try self.db.compactStep(self.policy.compact_bytes);
        if (planned) self.mark(.compact, .end);
        if (!advanced) return false;
        const now = self.clock();
        self.compaction_steps.add(now - started);
        if (self.compaction_started == null) self.compaction_started = started;
        if (self.db.compactionPhase() == .idle) {
            self.compactions += 1;
            self.last_compaction_ns = now - self.compaction_started.?;
            self.last_compaction_at = @intCast(@divFloor(Io.Clock.real.now(self.io).nanoseconds, std.time.ns_per_s));
            self.compaction_started = null;
            try self.collectTables();
        }
        return true;
    }

    pub fn collectTables(self: *Maintainer) !void {
        const started = self.clock();
        self.mark(.collect_tables, .begin);
        try self.db.collectTables();
        self.mark(.collect_tables, .end);
        self.table_collections.add(self.clock() - started);
    }

    pub fn collectLogStep(self: *Maintainer) !void {
        const started = self.clock();
        const applied = if (self.collector.names != null) self.collector.applied else self.db.version.applied;
        self.mark(.collect_log, .begin);
        const more = try self.db.collectLogStep(&self.collector, self.policy.log_names);
        self.mark(.collect_log, .end);
        self.log_pending = more or self.db.version.applied > applied;
        self.log_collections.add(self.clock() - started);
    }

    /// One round: a flush if one is due, one compaction step, one log
    /// collection step. False when there was nothing to do.
    pub fn step(self: *Maintainer) !bool {
        const started = self.clock();
        var worked = false;
        if (self.flushDue()) {
            try self.flush();
            worked = true;
        }
        if (try self.compactStep()) worked = true;
        if (self.log_pending) {
            try self.collectLogStep();
            worked = true;
        }
        if (worked) self.steps.add(self.clock() - started);
        return worked;
    }

    pub fn pressure(self: *const Maintainer) Pressure {
        return if (self.db.version.tables[0].len > self.policy.stall_tables) .stalled else .none;
    }

    /// Work level 0 down when it is over `stall_tables`. In bulk-load mode
    /// this returns only once level 0 is under the compaction trigger; in
    /// steady mode it takes at most `relief_steps` steps and reports what
    /// pressure remains.
    pub fn relieve(self: *Maintainer) !Pressure {
        if (self.pressure() == .none) return .none;
        const started = self.clock();
        var taken: usize = 0;
        while (self.db.version.tables[0].len >= self.db.options.l0_tables) {
            if (self.policy.mode == .steady and taken == self.policy.relief_steps) break;
            if (!try self.step()) break;
            taken += 1;
        }
        self.stall_steps += taken;
        self.stalls.add(self.clock() - started);
        const left = self.pressure();
        if (left == .stalled) self.unrelieved += 1;
        return left;
    }

    /// Flush, compact until nothing is planned, and collect everything. A
    /// full compaction holds its inputs and outputs at once: budget twice
    /// the store's size in free disk.
    pub fn settle(self: *Maintainer) !void {
        try self.flush();
        while (try self.compactStep()) {}
        try self.collectTables();
        self.collector.deinit(self.db.gpa);
        self.mark(.collect_log, .begin);
        try self.db.collect();
        self.mark(.collect_log, .end);
        self.log_pending = false;
    }

    pub fn stats(self: *const Maintainer) Stats {
        const db = self.db;
        var result: Stats = .{
            .tables = 0,
            .table_bytes = 0,
            .levels = @splat(.{}),
            .memtable_bytes = db.memtableBytes(),
            .unflushed_segments = db.unflushedSegments(),
            .resident_metadata_bytes = db.residentMetadataBytes(),
            .cache_bytes = db.cacheStats().cached_bytes,
            .compaction_active = db.compactionPhase() != .idle,
            .last_compaction_at = self.last_compaction_at,
            .last_compaction_ns = self.last_compaction_ns,
            .pressure = self.pressure(),
            .steps = self.steps,
            .flushes = self.flushes,
            .compaction_steps = self.compaction_steps,
            .compactions = self.compactions,
            .table_collections = self.table_collections,
            .log_collections = self.log_collections,
            .stalls = self.stalls,
            .stall_steps = self.stall_steps,
            .unrelieved = self.unrelieved,
        };
        for (&result.levels, db.version.tables, 0..) |*level, tables, index| {
            level.* = .{ .tables = tables.len, .bytes = db.version.levelBytes(index) };
            result.tables += level.tables;
            result.table_bytes += level.bytes;
        }
        return result;
    }
};

/// Whether `compactStep` has work, by zkv's own planning rule: a job in
/// flight, level 0 at its table trigger, or a level over its byte target.
/// Only the trace depends on it, so a planner change in zkv costs trace
/// events and nothing else.
pub fn compactionPlanned(db: *const Db) bool {
    if (db.compactionPhase() != .idle) return true;
    if (db.version.tables[0].len >= db.options.l0_tables) return true;
    var target = db.options.level_bytes;
    for (1..engine.level_count - 1) |level| {
        if (db.version.levelBytes(level) > target) return true;
        target *|= db.options.growth;
    }
    return false;
}

/// Bytes and files under the store directory: tables, log segments, the
/// manifest. Walks the directory, so call it for a health report, not per
/// request.
pub fn disk(gpa: std.mem.Allocator, io: Io, dir: Io.Dir) !Disk {
    var walker = try dir.walk(gpa);
    defer walker.deinit();
    var total: Disk = .{};
    while (try walker.next(io)) |entry| {
        if (entry.kind != .file) continue;
        const stat = entry.dir.statFile(io, entry.basename, .{}) catch |err| switch (err) {
            error.FileNotFound => continue,
            else => return err,
        };
        total.bytes += stat.size;
        total.files += 1;
    }
    return total;
}
