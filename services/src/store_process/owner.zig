//! The owner of the store's writer handle.
//!
//! Concurrency model:
//!
//! - One thread, running `run`, is the only one that writes. It takes
//!   submissions from a bounded FIFO queue, applies every one it took as a
//!   single store commit (each submission's cursor in the same commit as its
//!   changes), and between commits does the store's housekeeping: stepped
//!   maintenance, domain sweeps, tombstone pruning, the label-expiry pass,
//!   change-log trimming, the handle repair and the health snapshot. For
//!   `startup_seconds` after it opens the store it leaves out everything in
//!   that list that scans a range, which is all of it but the maintenance
//!   and the sweeps.
//! - Producers (the relay intake, the identity resolvers, the PLC tail, the
//!   label tail, the enrichment loop) run on their own threads and never
//!   touch the store. They
//!   build a `Submission` and hand it over with `post` (the owner frees it
//!   after the commit) or `call` (the producer waits for the commit and reads
//!   the result). Submissions from one producer commit in the order it handed
//!   them over, so a cursor never moves ahead of an earlier batch.
//! - The queue is bounded by queued changes. A producer that finds it full
//!   blocks in `post`, which is the only backpressure there is (the index
//!   bridge alone uses `offer`, which never waits): the relay
//!   intake stops reading its socket and the relay holds the rest. While the
//!   store's maintainer reports a stall the owner takes nothing from the
//!   queue and only works level 0 down, so the queue fills and intake pauses
//!   until the stall clears.
//! - Readers on other threads (the HTTP surface, the index bridge) take
//!   `store_lock` through `acquire`. The owner holds that lock for a whole
//!   commit and for each housekeeping step, so a reader sees whole commits
//!   only and waits at most one commit or one bounded step.
//! - A commit that fails leaves the store handle unusable. The owner closes
//!   and reopens it and applies the same submissions again, which is safe
//!   because every change is idempotent to replay; after
//!   `max_consecutive_failures` it gives up through `on_fatal`.
//!
//! Lock order: `store_lock` before `queue_lock`; the other locks are leaves.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const log_route = @import("../log_route.zig");
const handle_policy = @import("handle_policy.zig");
const Io = std.Io;
const Store = store_mod.Store;
const Change = store_mod.Change;
const change = store_mod.change;
const keys = store_mod.keys;
const maintain = store_mod.maintain;
const scan = store_mod.scan;

const log = std.log.scoped(.store_owner);
const progress = std.log.scoped(log_route.flush_scope);

pub const Clock = struct {
    context: ?*anyopaque = null,
    seconds: *const fn (context: ?*anyopaque, io: Io) u64 = wall,

    fn wall(_: ?*anyopaque, io: Io) u64 {
        return @intCast(@divFloor(Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
    }
};

/// The store's own maintenance step, replaceable so a test can report a stall.
pub const Maintenance = struct {
    context: ?*anyopaque = null,
    step: *const fn (context: ?*anyopaque, store: *Store) anyerror!maintain.Pressure = storeStep,

    fn storeStep(_: ?*anyopaque, store: *Store) anyerror!maintain.Pressure {
        return store.maintainStep();
    }
};

pub const Stage = enum { commit, housekeeping };

/// Called around every commit and housekeeping pass, for tests that need to
/// know what a process was doing when it died.
pub const Trace = struct {
    context: ?*anyopaque = null,
    call: *const fn (context: ?*anyopaque, stage: Stage, edge: maintain.Edge) void,
};

pub const Options = struct {
    store: store_mod.Options = .{},
    clock: Clock = .{},
    maintenance: Maintenance = .{},
    trace: ?Trace = null,
    /// Called when the store cannot be made to commit. Production logs the
    /// error and exits for a restart; the owner stops if this returns.
    on_fatal: *const fn () void = noFatal,
    /// Called once a `health_seconds`, after the snapshot is refreshed.
    on_heartbeat: ?*const fn (owner: *Owner) void = null,

    /// Queued changes above which `post` blocks.
    max_queued_changes: usize = 4000,
    /// Changes one commit may carry. At least one submission is always taken.
    max_commit_changes: usize = 2000,
    /// How long an idle owner waits for a submission before housekeeping.
    idle_wait_ms: u64 = 200,
    max_consecutive_failures: u32 = 5,

    /// Tombstones older than this are pruned; the Worker's cron keeps 7 days.
    tombstone_retention_seconds: u64 = 7 * 86_400,
    prune_seconds: u64 = 60,
    prune_limit: usize = 500,
    /// Label keys examined by one step of the expiry pass.
    expiry_seconds: u64 = 10,
    expiry_scan: usize = 2000,
    /// Handle keys examined by one step of a domain sweep.
    sweep_scan: usize = 5000,
    trim_seconds: u64 = 60,
    trim_limit: usize = 10_000,
    /// Change-log entries kept when no consumer has reported a position.
    retain_without_consumer: u64 = 1_000_000,
    /// Entries kept at most: a consumer further behind is told to resync.
    retain_at_most: u64 = 20_000_000,
    /// The handle repair: a pass, made once in the life of a store, that
    /// sends every live actor whose handle is `handle.invalid` back through
    /// identity resolution, because an earlier build wrote that handle on
    /// the appview's word. A step looks at up to `repair_limit`
    /// holders, fewer when more than `repair_backlog` identity entries
    /// would be due. 0 turns it off, which is the default: the role turns
    /// it on.
    repair_limit: usize = 0,
    repair_seconds: u64 = 5,
    repair_backlog: usize = 100,
    health_seconds: u64 = 60,
    /// How long after it opens the store the owner only commits and
    /// maintains: pruning, the expiry pass, trimming, the handle repair and
    /// the health counts all wait this long, so a restart reaches its first
    /// commits without a range scan in front of them. 0, the default,
    /// starts them on the first turn: the role sets it.
    startup_seconds: u64 = 0,
    /// Keys counted at most when sizing a queue for the health snapshot.
    count_cap: usize = 200_000,

    fn noFatal() void {}
};

pub const Summary = struct {
    applied: u32 = 0,
    changed: u32 = 0,
    created: u32 = 0,
    /// Changes the key layout could not hold; they were skipped.
    rejected: u32 = 0,
    /// What the enrichments did about handles their sources reported.
    handles: std.EnumArray(change.HandleVerdict, u32) = .initFill(0),
};

pub const Error = error{OwnerStopped};

/// A batch of changes from one producer, with the cursor that covers it.
pub const Submission = struct {
    gpa: std.mem.Allocator,
    /// Owns the changes and, after the commit, `jobs`.
    arena: std.heap.ArenaAllocator,
    changes: []const Change = &.{},
    cursor: ?change.CursorUpdate = null,
    /// Identity jobs an `identity_lease` change in this submission leased.
    jobs: []const change.Job = &.{},
    /// Refreshes a `refresh_lease` change in this submission leased.
    refresh_jobs: []const change.RefreshJob = &.{},
    /// What this submission's `refresh_walk` changes did.
    walked: change.WalkTally = .initFill(.{}),
    summary: Summary = .{},
    waited: bool = false,
    failed: bool = false,
    done: Io.Event = .unset,

    pub fn create(gpa: std.mem.Allocator) !*Submission {
        const self = try gpa.create(Submission);
        self.* = .{ .gpa = gpa, .arena = .init(gpa) };
        return self;
    }

    pub fn destroy(self: *Submission) void {
        const gpa = self.gpa;
        self.arena.deinit();
        gpa.destroy(self);
    }
};

pub const Counters = struct {
    commits: std.atomic.Value(u64) = .init(0),
    submissions: std.atomic.Value(u64) = .init(0),
    changes_applied: std.atomic.Value(u64) = .init(0),
    changes_rejected: std.atomic.Value(u64) = .init(0),
    actors_changed: std.atomic.Value(u64) = .init(0),
    /// New actor records since this process started.
    actors_created: std.atomic.Value(u64) = .init(0),
    /// Live actors that became deleted or deactivated.
    actors_gone: std.atomic.Value(u64) = .init(0),
    /// Gone actors that became live again.
    actors_reactivated: std.atomic.Value(u64) = .init(0),
    tombstones_pruned: std.atomic.Value(u64) = .init(0),
    hidden_refreshed: std.atomic.Value(u64) = .init(0),
    expiry_passes: std.atomic.Value(u64) = .init(0),
    domain_sweeps: std.atomic.Value(u64) = .init(0),
    changes_trimmed: std.atomic.Value(u64) = .init(0),
    reopens: std.atomic.Value(u64) = .init(0),
    /// Turns the owner spent relieving a stall instead of committing.
    stall_turns: std.atomic.Value(u64) = .init(0),
    stalled: std.atomic.Value(bool) = .init(false),
    /// Times a producer found the queue full, and how long it waited.
    posts_blocked: std.atomic.Value(u64) = .init(0),
    posts_blocked_ms: std.atomic.Value(u64) = .init(0),
    queued_changes: std.atomic.Value(usize) = .init(0),
    last_commit_at: std.atomic.Value(u64) = .init(0),
    /// Unix seconds of the owner's last completed turn.
    last_turn_at: std.atomic.Value(u64) = .init(0),
};

pub const Consumer = struct {
    name: []const u8,
    position: u64,
    seen_at: u64,
};

/// What `/health` and the heartbeat report about the store, as of `taken_at`.
/// Refreshed by the owner, so reading it never waits on the store.
pub const Health = struct {
    /// 0 from a start until the owner's first refresh. Until then the
    /// queue counts, `repair.remaining` and `disk` are zero: opening the
    /// store reads the cursors, the walks, the repair's position and the
    /// table statistics, and counts nothing.
    taken_at: u64 = 0,
    cursors: store_mod.read.Cursors = .{},
    plc_cursor: [change.max_cursor_bytes]u8 = undefined,
    plc_cursor_len: usize = 0,
    identity_pending: u64 = 0,
    identity_due: u64 = 0,
    refresh_pending: u64 = 0,
    /// Refresh entries due now; the rest of `refresh_pending` is leased.
    refresh_due: u64 = 0,
    walks: std.EnumArray(change.WalkPool, Walk) = .initFill(.{}),
    tombstones: u64 = 0,
    repair: Repair = .{},
    /// True when a count above stopped at `count_cap`.
    counts_capped: bool = false,
    maintenance: ?maintain.Stats = null,
    disk: maintain.Disk = .{},

    pub fn plc(self: *const Health) ?[]const u8 {
        return if (self.plc_cursor_len == 0) null else self.plc_cursor[0..self.plc_cursor_len];
    }
};

/// Where the handle repair stands.
pub const Repair = struct {
    started_at: u64 = 0,
    /// 0 until the pass has reached the end.
    finished_at: u64 = 0,
    examined: u64 = 0,
    queued: u64 = 0,
    /// Holders of `handle.invalid` the pass has yet to look at.
    remaining: u64 = 0,
    did: [max_walk_did]u8 = undefined,
    did_len: usize = 0,

    pub fn position(self: *const Repair) []const u8 {
        return self.did[0..self.did_len];
    }
};

/// Where a refresh walk stands, without the allocation its key needs.
pub const Walk = struct {
    started_at: u64 = 0,
    passes: u64 = 0,
    last_pass_seconds: u64 = 0,
    /// The score the quality walk has reached; 0 at the top of a pass.
    score: f32 = 0,
    did: [max_walk_did]u8 = undefined,
    did_len: usize = 0,

    pub fn position(self: *const Walk) []const u8 {
        return self.did[0..self.did_len];
    }
};

const max_walk_did = 96;
const max_consumers = 8;
const max_consumer_name = 32;

const ConsumerSlot = struct {
    name: [max_consumer_name]u8 = undefined,
    len: usize = 0,
    position: u64 = 0,
    seen_at: u64 = 0,
};

const SweepJob = struct {
    domain: []const u8,
    sweep: Store.Sweep = .{},
};

pub const Owner = struct {
    gpa: std.mem.Allocator,
    io: Io,
    root: Io.Dir,
    path: []const u8,
    options: Options,

    store_lock: Io.Mutex = .init,
    store: *Store,
    opened: bool = true,

    queue_lock: Io.Mutex = .init,
    queue_changed: Io.Condition = .init,
    queue: std.ArrayList(*Submission) = .empty,
    queued_changes: usize = 0,
    stopped: bool = false,

    counters: Counters = .{},
    failures: u32 = 0,
    group: std.ArrayList(*Submission) = .empty,
    scratch: std.heap.ArenaAllocator,
    sweeps: std.ArrayList(SweepJob) = .empty,
    noted_domains: std.ArrayList([]const u8) = .empty,
    expiry: scan.Position = .{},
    next_prune: u64 = 0,
    next_expiry: u64 = 0,
    next_trim: u64 = 0,
    next_repair: u64 = 0,
    repair_finished: bool = false,
    next_health: u64 = 0,

    health_lock: Io.Mutex = .init,
    health_snapshot: Health = .{},
    consumers: [max_consumers]ConsumerSlot = @splat(.{}),
    consumer_count: usize = 0,

    pub fn open(gpa: std.mem.Allocator, io: Io, root: Io.Dir, path: []const u8, options: Options) !*Owner {
        const store = try Store.open(gpa, io, root, path, options.store);
        errdefer store.close();
        const self = try gpa.create(Owner);
        errdefer gpa.destroy(self);
        self.* = .{ .gpa = gpa, .io = io, .root = root, .path = path, .options = options, .store = store, .scratch = .init(gpa) };
        var arena: std.heap.ArenaAllocator = .init(gpa);
        defer arena.deinit();
        if ((try store.cursors(arena.allocator())).bridge) |position| self.noteConsumer("bridge", position);
        const at = self.now();
        const first = at + options.startup_seconds;
        self.next_prune = first;
        self.next_expiry = first;
        self.next_trim = first;
        self.next_repair = first;
        self.next_health = first;
        var opened: Health = .{};
        try self.readState(&opened);
        self.health_snapshot = opened;
        self.counters.last_turn_at.store(at, .monotonic);
        return self;
    }

    /// Stop first, and join whatever runs `run`.
    pub fn close(self: *Owner) void {
        self.failQueued();
        if (self.opened) self.store.close();
        for (self.sweeps.items) |*job| {
            job.sweep.deinit(self.gpa);
            self.gpa.free(job.domain);
        }
        self.sweeps.deinit(self.gpa);
        for (self.noted_domains.items) |domain| self.gpa.free(domain);
        self.noted_domains.deinit(self.gpa);
        self.expiry.deinit(self.gpa);
        self.queue.deinit(self.gpa);
        self.group.deinit(self.gpa);
        self.scratch.deinit();
        self.gpa.destroy(self);
    }

    pub fn now(self: *const Owner) u64 {
        return self.options.clock.seconds(self.options.clock.context, self.io);
    }

    // -- producers --

    /// Hand a submission to the owner, which frees it after its commit.
    /// Blocks while the queue is full, setting `blocked` before it waits;
    /// the caller clears it. On error the caller still owns the submission.
    pub fn post(self: *Owner, submission: *Submission, blocked: ?*std.atomic.Value(bool)) Error!void {
        self.queue_lock.lockUncancelable(self.io);
        defer self.queue_lock.unlock(self.io);
        if (self.queued_changes >= self.options.max_queued_changes and !self.stopped) {
            const started = Io.Timestamp.now(self.io, .awake);
            _ = self.counters.posts_blocked.fetchAdd(1, .monotonic);
            if (blocked) |flag| flag.store(true, .monotonic);
            while (self.queued_changes >= self.options.max_queued_changes and !self.stopped) {
                self.queue_changed.waitUncancelable(self.io, &self.queue_lock);
            }
            const waited = Io.Timestamp.now(self.io, .awake).nanoseconds - started.nanoseconds;
            _ = self.counters.posts_blocked_ms.fetchAdd(@intCast(@divFloor(@max(waited, 0), std.time.ns_per_ms)), .monotonic);
        }
        if (self.stopped) return error.OwnerStopped;
        self.queue.append(self.gpa, submission) catch return error.OwnerStopped;
        self.queued_changes += @max(submission.changes.len, 1);
        self.counters.queued_changes.store(self.queued_changes, .monotonic);
        self.queue_changed.broadcast(self.io);
    }

    /// `post` for a producer that must not wait: false when the queue is
    /// full, and the caller still owns the submission.
    pub fn offer(self: *Owner, submission: *Submission) Error!bool {
        self.queue_lock.lockUncancelable(self.io);
        defer self.queue_lock.unlock(self.io);
        if (self.stopped) return error.OwnerStopped;
        if (self.queued_changes >= self.options.max_queued_changes) return false;
        self.queue.append(self.gpa, submission) catch return error.OwnerStopped;
        self.queued_changes += @max(submission.changes.len, 1);
        self.counters.queued_changes.store(self.queued_changes, .monotonic);
        self.queue_changed.broadcast(self.io);
        return true;
    }

    /// Hand a submission over and wait for its commit. The caller keeps the
    /// submission and reads `summary` and `jobs` from it.
    pub fn call(self: *Owner, submission: *Submission) Error!void {
        submission.waited = true;
        submission.done.reset();
        try self.post(submission, null);
        submission.done.waitUncancelable(self.io);
        if (submission.failed) return error.OwnerStopped;
    }

    // -- readers --

    /// The store, for reading only, until `release`. Blocks the owner, so
    /// read and let go.
    pub fn acquire(self: *Owner) !*Store {
        self.store_lock.lockUncancelable(self.io);
        if (!self.opened) {
            self.store_lock.unlock(self.io);
            return error.StoreBroken;
        }
        return self.store;
    }

    pub fn release(self: *Owner) void {
        self.store_lock.unlock(self.io);
    }

    pub fn health(self: *Owner) Health {
        self.health_lock.lockUncancelable(self.io);
        defer self.health_lock.unlock(self.io);
        return self.health_snapshot;
    }

    /// Record how far a change-log consumer has read. The log is trimmed
    /// below the lowest position reported.
    pub fn noteConsumer(self: *Owner, name: []const u8, position: u64) void {
        self.health_lock.lockUncancelable(self.io);
        defer self.health_lock.unlock(self.io);
        const short = name[0..@min(name.len, max_consumer_name)];
        const slot = for (self.consumers[0..self.consumer_count]) |*slot| {
            if (std.mem.eql(u8, slot.name[0..slot.len], short)) break slot;
        } else blk: {
            if (self.consumer_count == max_consumers) return;
            const slot = &self.consumers[self.consumer_count];
            self.consumer_count += 1;
            @memcpy(slot.name[0..short.len], short);
            slot.len = short.len;
            break :blk slot;
        };
        slot.position = position;
        slot.seen_at = self.now();
    }

    /// Consumers in `out`, borrowed names valid while the owner lives.
    pub fn consumerList(self: *Owner, out: *[max_consumers]Consumer) []const Consumer {
        self.health_lock.lockUncancelable(self.io);
        defer self.health_lock.unlock(self.io);
        for (self.consumers[0..self.consumer_count], out[0..self.consumer_count]) |*slot, *one| {
            one.* = .{ .name = slot.name[0..slot.len], .position = slot.position, .seen_at = slot.seen_at };
        }
        return out[0..self.consumer_count];
    }

    // -- the owner's thread --

    pub fn run(self: *Owner) void {
        while (!self.isStopped()) _ = self.turn(self.options.idle_wait_ms);
    }

    pub fn stop(self: *Owner) void {
        self.queue_lock.lockUncancelable(self.io);
        self.stopped = true;
        self.queue_lock.unlock(self.io);
        self.queue_changed.broadcast(self.io);
    }

    pub fn isStopped(self: *Owner) bool {
        self.queue_lock.lockUncancelable(self.io);
        defer self.queue_lock.unlock(self.io);
        return self.stopped;
    }

    /// One turn: commit what is queued, unless the store is stalled, then
    /// housekeeping. Returns how many submissions were committed.
    pub fn turn(self: *Owner, wait_ms: u64) usize {
        const stalled = self.counters.stalled.load(.monotonic);
        if (stalled) {
            _ = self.counters.stall_turns.fetchAdd(1, .monotonic);
        } else self.take(wait_ms);

        self.store_lock.lockUncancelable(self.io);
        defer self.store_lock.unlock(self.io);
        const taken = self.group.items.len;
        while (true) {
            self.work(stalled) catch |err| {
                if (!self.recover(err)) return 0;
                continue;
            };
            break;
        }
        self.counters.last_turn_at.store(self.now(), .monotonic);
        return taken;
    }

    fn take(self: *Owner, wait_ms: u64) void {
        self.queue_lock.lockUncancelable(self.io);
        defer self.queue_lock.unlock(self.io);
        if (self.queue.items.len == 0 and wait_ms > 0 and !self.stopped) {
            self.queue_changed.waitTimeout(self.io, &self.queue_lock, .{ .duration = .{ .raw = .fromMilliseconds(@intCast(wait_ms)), .clock = .awake } }) catch {};
        }
        var changes: usize = 0;
        var count: usize = 0;
        for (self.queue.items) |submission| {
            const size = @max(submission.changes.len, 1);
            if (count > 0 and changes + size > self.options.max_commit_changes) break;
            changes += size;
            count += 1;
        }
        if (count == 0) return;
        self.group.appendSlice(self.gpa, self.queue.items[0..count]) catch return;
        self.queue.replaceRangeAssumeCapacity(0, count, &.{});
        self.queued_changes -= changes;
        self.counters.queued_changes.store(self.queued_changes, .monotonic);
        self.queue_changed.broadcast(self.io);
    }

    fn work(self: *Owner, stalled: bool) !void {
        if (!self.opened) try self.reopen();
        if (self.group.items.len > 0) {
            try self.commitGroup();
            self.settleGroup(false);
        }
        self.mark(.housekeeping, .begin);
        defer self.mark(.housekeeping, .end);
        const pressure = try self.options.maintenance.step(self.options.maintenance.context, self.store);
        self.counters.stalled.store(pressure == .stalled, .monotonic);
        self.failures = 0;
        if (pressure == .stalled or stalled) return;
        const at = self.now();
        if (self.sweeps.items.len > 0) try self.sweepStep(at);
        if (at >= self.next_prune) {
            try self.prune(at);
            self.next_prune = at + self.options.prune_seconds;
        }
        if (at >= self.next_expiry) {
            try self.expiryStep(at);
            self.next_expiry = at + self.options.expiry_seconds;
        }
        if (at >= self.next_trim) {
            try self.trim();
            self.next_trim = at + self.options.trim_seconds;
        }
        if (self.options.repair_limit > 0 and !self.repair_finished and at >= self.next_repair) {
            try self.repairStep(at);
            self.next_repair = at + self.options.repair_seconds;
        }
        if (at >= self.next_health) {
            try self.refreshHealth(at);
            self.next_health = at + self.options.health_seconds;
            if (self.options.on_heartbeat) |beat| beat(self);
        }
    }

    fn mark(self: *Owner, stage: Stage, edge: maintain.Edge) void {
        if (self.options.trace) |trace| trace.call(trace.context, stage, edge);
    }

    fn commitGroup(self: *Owner) !void {
        const store = self.store;
        const at = self.now();
        _ = self.scratch.reset(.retain_capacity);
        for (self.noted_domains.items) |domain| self.gpa.free(domain);
        self.noted_domains.clearRetainingCapacity();

        var total: Summary = .{};
        var gone: u64 = 0;
        var reactivated: u64 = 0;
        var pruned: u64 = 0;
        var refreshed: u64 = 0;

        const batch = try store.begin(at);
        errdefer batch.abort();
        for (self.group.items) |submission| {
            submission.summary = .{};
            submission.jobs = &.{};
            submission.refresh_jobs = &.{};
            const jobs_before = batch.leased().len;
            const refresh_before = batch.refreshLeased().len;
            var walked_before: change.WalkTally = undefined;
            for (std.enums.values(change.WalkPool)) |pool| walked_before.set(pool, batch.walked(pool));
            for (submission.changes) |one| {
                const was_live = switch (one) {
                    .gone => |c| if (store.actor(self.scratch.allocator(), c.did) catch null) |actor| actor.live() else false,
                    else => false,
                };
                const outcome = batch.apply(one) catch |err| switch (err) {
                    error.InvalidChange => {
                        submission.summary.rejected += 1;
                        log.warn("a {t} change was refused by the key layout", .{one});
                        continue;
                    },
                    else => return err,
                };
                submission.summary.applied += @intFromBool(outcome.applied);
                submission.summary.changed += @intCast(outcome.changed);
                submission.summary.created += @intFromBool(outcome.created);
                submission.summary.handles.getPtr(outcome.handle).* += 1;
                switch (one) {
                    .gone => gone += @intFromBool(was_live and outcome.changed > 0),
                    .reactivated => reactivated += @intFromBool(!outcome.created and outcome.changed > 0),
                    .tombstones_pruned => pruned += outcome.changed,
                    .hidden_refreshed => refreshed += outcome.changed,
                    .domain_blocked => |c| try self.noteDomain(c.domain),
                    .domain_unblocked => |name| try self.noteDomain(name),
                    else => {},
                }
            }
            if (submission.cursor) |update| try batch.setCursor(update);
            const leased = batch.leased()[jobs_before..];
            if (leased.len > 0) {
                const arena = submission.arena.allocator();
                const jobs = try arena.alloc(change.Job, leased.len);
                for (jobs, leased) |*job, source| {
                    job.* = source;
                    job.did = try arena.dupe(u8, source.did);
                }
                submission.jobs = jobs;
            }
            const refreshes = batch.refreshLeased()[refresh_before..];
            if (refreshes.len > 0) {
                const arena = submission.arena.allocator();
                const jobs = try arena.alloc(change.RefreshJob, refreshes.len);
                for (jobs, refreshes) |*job, source| {
                    job.* = source;
                    job.did = try arena.dupe(u8, source.did);
                    job.handle = try arena.dupe(u8, source.handle);
                    job.pds = try arena.dupe(u8, source.pds);
                }
                submission.refresh_jobs = jobs;
            }
            for (std.enums.values(change.WalkPool)) |pool| submission.walked.set(pool, batch.walked(pool).since(walked_before.get(pool)));
            total.applied += submission.summary.applied;
            total.changed += submission.summary.changed;
            total.created += submission.summary.created;
            total.rejected += submission.summary.rejected;
        }
        self.mark(.commit, .begin);
        try batch.commit();
        self.mark(.commit, .end);

        const c = &self.counters;
        _ = c.commits.fetchAdd(1, .monotonic);
        _ = c.submissions.fetchAdd(self.group.items.len, .monotonic);
        _ = c.changes_applied.fetchAdd(total.applied, .monotonic);
        _ = c.changes_rejected.fetchAdd(total.rejected, .monotonic);
        _ = c.actors_changed.fetchAdd(total.changed, .monotonic);
        _ = c.actors_created.fetchAdd(total.created, .monotonic);
        _ = c.actors_gone.fetchAdd(gone, .monotonic);
        _ = c.actors_reactivated.fetchAdd(reactivated, .monotonic);
        _ = c.tombstones_pruned.fetchAdd(pruned, .monotonic);
        _ = c.hidden_refreshed.fetchAdd(refreshed, .monotonic);
        c.last_commit_at.store(at, .monotonic);
        if (total.applied > 0) progress.info("+{d} changes in {d} batches: {d} actors changed, {d} new, {d} refused; {d} queued", .{
            total.applied, self.group.items.len, total.changed, total.created, total.rejected, self.counters.queued_changes.load(.monotonic),
        });

        for (self.noted_domains.items) |domain| {
            self.sweeps.append(self.gpa, .{ .domain = domain }) catch {
                self.gpa.free(domain);
                continue;
            };
        }
        self.noted_domains.clearRetainingCapacity();
    }

    fn noteDomain(self: *Owner, domain: []const u8) !void {
        const owned = try self.gpa.dupe(u8, domain);
        errdefer self.gpa.free(owned);
        try self.noted_domains.append(self.gpa, owned);
    }

    /// Wake or free every submission of the group just committed or abandoned.
    fn settleGroup(self: *Owner, failed: bool) void {
        for (self.group.items) |submission| {
            if (submission.waited) {
                submission.failed = failed;
                submission.done.set(self.io);
            } else submission.destroy();
        }
        self.group.clearRetainingCapacity();
    }

    /// After a failed commit or step: reopen the store so the next attempt
    /// starts from what is durable. False when the owner has given up.
    fn recover(self: *Owner, err: anyerror) bool {
        self.failures += 1;
        _ = self.counters.reopens.fetchAdd(1, .monotonic);
        log.warn("store write failed ({s}), reopening (failure {d} of {d})", .{ @errorName(err), self.failures, self.options.max_consecutive_failures });
        if (self.failures < self.options.max_consecutive_failures) {
            if (self.reopen()) |_| return true else |reopen_err| return self.recover(reopen_err);
        }
        self.options.on_fatal();
        self.settleGroup(true);
        self.stop();
        self.failQueued();
        return false;
    }

    fn reopen(self: *Owner) !void {
        if (self.opened) {
            self.store.close();
            self.opened = false;
        }
        self.store = try Store.open(self.gpa, self.io, self.root, self.path, self.options.store);
        self.opened = true;
    }

    fn failQueued(self: *Owner) void {
        self.queue_lock.lockUncancelable(self.io);
        defer self.queue_lock.unlock(self.io);
        self.stopped = true;
        for (self.queue.items) |submission| {
            if (submission.waited) {
                submission.failed = true;
                submission.done.set(self.io);
            } else submission.destroy();
        }
        self.queue.clearRetainingCapacity();
        self.queued_changes = 0;
        self.counters.queued_changes.store(0, .monotonic);
        self.queue_changed.broadcast(self.io);
    }

    // -- housekeeping, on the owner's thread with `store_lock` held --

    fn sweepStep(self: *Owner, at: u64) !void {
        const job = &self.sweeps.items[0];
        try self.store.sweepDomain(&job.sweep, job.domain, at, self.options.sweep_scan);
        if (!job.sweep.done()) return;
        _ = self.counters.domain_sweeps.fetchAdd(1, .monotonic);
        _ = self.counters.hidden_refreshed.fetchAdd(job.sweep.changed, .monotonic);
        log.info("domain sweep of {s} done: {d} handles, {d} actors changed", .{ job.domain, job.sweep.scanned, job.sweep.changed });
        job.sweep.deinit(self.gpa);
        self.gpa.free(job.domain);
        _ = self.sweeps.orderedRemove(0);
    }

    fn prune(self: *Owner, at: u64) !void {
        if (at <= self.options.tombstone_retention_seconds) return;
        const outcome = try self.store.apply(at, .{ .tombstones_pruned = .{
            .before = at - self.options.tombstone_retention_seconds,
            .limit = self.options.prune_limit,
        } }, null);
        _ = self.counters.tombstones_pruned.fetchAdd(outcome.changed, .monotonic);
    }

    const Expired = struct {
        arena: std.mem.Allocator,
        now_ms: i128,
        dids: std.ArrayList([]const u8) = .empty,

        pub fn entry(self: *Expired, key: []const u8, value: []const u8) !void {
            const stored = try store_mod.record.StoredLabel.decode(value);
            if (stored.negated or stored.expires.len == 0) return;
            const at = store_mod.time.parseMs(stored.expires) orelse return;
            if (at > self.now_ms) return;
            const did = (try keys.parseLabel(key)).did;
            if (self.dids.items.len > 0 and std.mem.eql(u8, self.dids.items[self.dids.items.len - 1], did)) return;
            try self.dids.append(self.arena, try self.arena.dupe(u8, did));
        }
    };

    /// One step of the pass that makes a lapsed label stop hiding its actor:
    /// walk the next stretch of the label range and recompute `hidden` for
    /// every account holding an expired label. An actor whose flag is already
    /// right is read and not rewritten.
    fn expiryStep(self: *Owner, at: u64) !void {
        const bounds = keys.Prefix.label.bounds();
        if (self.expiry.done or self.expiry.next.items.len == 0) {
            if (self.expiry.done) _ = self.counters.expiry_passes.fetchAdd(1, .monotonic);
            try self.expiry.reset(self.gpa, &bounds[0]);
        }
        _ = self.scratch.reset(.retain_capacity);
        const batch = try self.store.begin(at);
        errdefer batch.abort();
        var found: Expired = .{ .arena = self.scratch.allocator(), .now_ms = @as(i128, at) * 1000 };
        _ = try scan.run(self.gpa, self.store.db(), &self.expiry, &bounds[1], .{ .limit = self.options.expiry_scan }, &found);
        var changed: u64 = 0;
        for (found.dids.items) |did| changed += (try batch.apply(.{ .hidden_refreshed = did })).changed;
        try batch.commit();
        _ = self.counters.hidden_refreshed.fetchAdd(changed, .monotonic);
    }

    fn repairStep(self: *Owner, at: u64) !void {
        const step = try self.store.reverifyHandle(at, handle_policy.invalid_handle, self.options.repair_limit, self.options.repair_backlog);
        if (!step.finished) return;
        self.repair_finished = true;
        _ = self.scratch.reset(.retain_capacity);
        const state = try store_mod.read.repairState(self.store.db(), self.scratch.allocator());
        log.info("handle repair finished at {d}: {d} holders of {s} examined, {d} queued for identity resolution", .{ state.finished_at, state.examined, handle_policy.invalid_handle, state.queued });
    }

    /// The position below which the change log may be dropped.
    fn trimTarget(self: *Owner) u64 {
        const next = self.store.next_change;
        self.health_lock.lockUncancelable(self.io);
        defer self.health_lock.unlock(self.io);
        var target: u64 = next -| self.options.retain_without_consumer;
        if (self.consumer_count > 0) {
            target = next;
            for (self.consumers[0..self.consumer_count]) |slot| target = @min(target, slot.position);
        }
        return @max(target, next -| self.options.retain_at_most);
    }

    fn trim(self: *Owner) !void {
        const target = self.trimTarget();
        if (target == 0) return;
        const trimmed = try self.store.trimChanges(target, self.options.trim_limit);
        _ = self.counters.changes_trimmed.fetchAdd(trimmed.removed, .monotonic);
    }

    fn countRange(self: *Owner, start: []const u8, end: []const u8, capped: *bool) !u64 {
        var count: scan.Count = .{};
        _ = try scan.all(self.gpa, self.store.db(), start, end, .{ .limit = self.options.count_cap }, &count);
        if (count.entries >= self.options.count_cap) capped.* = true;
        return count.entries;
    }

    /// What the snapshot takes from point reads and the engine's own
    /// counters: cursors, walks, where the repair stands, table statistics.
    fn readState(self: *Owner, fresh: *Health) !void {
        _ = self.scratch.reset(.retain_capacity);
        fresh.cursors = try self.store.cursors(self.scratch.allocator());
        if (fresh.cursors.plc) |text| {
            fresh.plc_cursor_len = @min(text.len, fresh.plc_cursor.len);
            @memcpy(fresh.plc_cursor[0..fresh.plc_cursor_len], text[0..fresh.plc_cursor_len]);
        }
        fresh.cursors.plc = null;
        for (std.enums.values(change.WalkPool)) |pool| {
            const state = try store_mod.read.walkState(self.store.db(), self.scratch.allocator(), pool.cursor());
            var walk: Walk = .{ .started_at = state.started_at, .passes = state.passes, .last_pass_seconds = state.last_pass_seconds };
            if (state.position.len > 0) {
                const did = switch (pool) {
                    .quality => blk: {
                        const parsed = try keys.parseQuality(state.position);
                        walk.score = parsed.score;
                        break :blk parsed.did;
                    },
                    .rest => try keys.parseActor(state.position),
                };
                walk.did_len = @min(did.len, max_walk_did);
                @memcpy(walk.did[0..walk.did_len], did[0..walk.did_len]);
            }
            fresh.walks.set(pool, walk);
        }
        const repair = try store_mod.read.repairState(self.store.db(), self.scratch.allocator());
        fresh.repair = .{ .started_at = repair.started_at, .finished_at = repair.finished_at, .examined = repair.examined, .queued = repair.queued };
        fresh.repair.did_len = @min(repair.position.len, max_walk_did);
        @memcpy(fresh.repair.did[0..fresh.repair.did_len], repair.position[0..fresh.repair.did_len]);
        fresh.maintenance = self.store.stats();
    }

    /// What the snapshot has to scan for: the size of each queue, the
    /// holders the repair has left, and the files on disk.
    fn countState(self: *Owner, fresh: *Health, at: u64) !void {
        var buf: keys.Buf = undefined;
        const identity_bounds = keys.Prefix.identity.bounds();
        fresh.identity_pending = try self.countRange(&identity_bounds[0], &identity_bounds[1], &fresh.counts_capped);
        fresh.identity_due = try self.countRange(&identity_bounds[0], keys.timedBound(&buf, .identity, at + 1), &fresh.counts_capped);
        const refresh_bounds = keys.Prefix.refresh.bounds();
        fresh.refresh_pending = try self.countRange(&refresh_bounds[0], &refresh_bounds[1], &fresh.counts_capped);
        fresh.refresh_due = try self.countRange(&refresh_bounds[0], keys.timedBound(&buf, .refresh, at + 1), &fresh.counts_capped);
        if (fresh.repair.finished_at == 0) {
            const repair = try store_mod.read.repairState(self.store.db(), self.scratch.allocator());
            var end_buf: keys.Buf = undefined;
            const prefix = try keys.handlePrefix(&buf, handle_policy.invalid_handle);
            var start: std.ArrayList(u8) = .empty;
            try start.appendSlice(self.scratch.allocator(), prefix);
            if (repair.position.len > 0) {
                try start.appendSlice(self.scratch.allocator(), repair.position);
                try start.append(self.scratch.allocator(), 0);
            }
            @memcpy(end_buf[0..prefix.len], prefix);
            end_buf[prefix.len - 1] = 1;
            fresh.repair.remaining = try self.countRange(start.items, end_buf[0..prefix.len], &fresh.counts_capped);
        }
        const tombstone_bounds = keys.Prefix.tombstone.bounds();
        fresh.tombstones = try self.countRange(&tombstone_bounds[0], &tombstone_bounds[1], &fresh.counts_capped);
        fresh.disk = self.store.diskUsage() catch .{};
    }

    fn refreshHealth(self: *Owner, at: u64) !void {
        var fresh: Health = .{ .taken_at = at };
        try self.readState(&fresh);
        try self.countState(&fresh, at);

        self.health_lock.lockUncancelable(self.io);
        defer self.health_lock.unlock(self.io);
        self.health_snapshot = fresh;
    }
};
