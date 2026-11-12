//! Pushes the store's changes into the Tantivy server.
//!
//! The index's committed source cursor is the only record of what the index
//! has applied. It commits atomically with the documents, so the bridge never
//! has to guess whether a request landed: it reads the cursor back. The
//! position kept in the store is a copy, written only after the index has
//! committed it, and exists so the store can trim its change log.
//!
//! One push:
//!   1. read change-log entries from the cursor's `position`
//!   2. coalesce repeated DIDs and read each one's current record
//!   3. ask the index for those documents' hashes and its revision
//!   4. reconcile, with the cursor and revision as compare-and-set guards and
//!      the new `position` inside the new cursor
//!   5. store the acknowledged position
//!
//! A crash anywhere leaves either the old cursor (the push is rebuilt from
//! current state) or the new one (the next start copies it to the store).
//! Pushes carry current state, never a recorded event, so repeating or
//! reordering them cannot move the index backward.
//!
//! Document `version` is the change-log sequence that caused the write (the
//! bridge's position for a resync write). The server does not order reconciled
//! observations by version; the cursor and revision guards do that.
//!
//! `watermark` is the wall-clock second, taken before the log read, at which
//! a push reached the end of the log. It is withheld until one full resync
//! has completed, so an index that might be missing documents goes stale
//! instead of reporting fresh.

const Bridge = @This();

const std = @import("std");
const Allocator = std.mem.Allocator;
const source = @import("source.zig");
const wire = @import("wire.zig");

const log = std.log.scoped(.index_bridge);

pub const Config = struct {
    /// Names the store these sequences belong to. A cursor written under a
    /// different name is not continued; the index is resynced instead.
    source_id: []const u8,
    log_read_limit: usize = 2000,
    max_actors: usize = wire.max_batch,
    /// Stop adding documents to a push once it holds this many bytes of
    /// profile data. One record is at most about 2.4 MiB (see
    /// `wire.rejection`), so a push stays under the server's 16 MiB limit.
    max_profile_bytes: usize = 8 * 1024 * 1024,
    max_batches_per_step: usize = 16,
    /// Longest the index waits for a watermark while nothing changes. The
    /// production server stops serving at 120 s of sync lag.
    heartbeat_s: u64 = 30,
    resync_pages_per_step: usize = 8,
    /// Seconds between automatic resync rounds; 0 leaves resyncs to
    /// bootstrap, detected divergence and `requestResync`.
    resync_interval_s: u64 = 0,
    /// Unix seconds. A round is owed while the last completed one started
    /// before this, and it begins once the clock has reached it: one full
    /// rewrite, asked for when what a document holds has changed, that a
    /// restart neither repeats nor loses. 0 asks for none.
    resync_after: u64 = 0,
    /// Take over an index whose cursor another controller wrote. The first
    /// resync round then decides what the index holds.
    adopt_foreign_cursor: bool = false,
    /// How long the loop rests after a step that left nothing to do. Changes
    /// that arrive meanwhile go out in one push, so while the bridge keeps
    /// up this is the most often the index commits, and the longest a
    /// change waits in the log before it is read. A step that stopped short
    /// of the end of the log is followed at once.
    poll_interval_ms: u64 = 1000,
    error_backoff_ms: u64 = 1000,
    clock: ?Clock = null,
};

pub const Clock = struct {
    ptr: *anyopaque,
    now: *const fn (ptr: *anyopaque) u64,
};

pub const Health = struct {
    /// Position last handed to the store.
    position: ?u64 = null,
    /// Position in the index's committed cursor, as last read or written.
    index_position: u64 = 0,
    /// The store's `next_change` at the last log read.
    next_change: u64 = 0,
    /// Entries the index has not applied: `next_change - index_position`.
    lag: u64 = 0,
    /// Unix second of the last committed push; 0 before the first.
    last_push_at: u64 = 0,
    watermark: u64 = 0,
    pushes: u64 = 0,
    applied: u64 = 0,
    unchanged: u64 = 0,
    /// Records the index cannot hold, sent as tombstones, plus change-log
    /// entries whose DID the index would refuse.
    rejected: u64 = 0,
    conflicts: u64 = 0,
    errors: u64 = 0,
    /// Starts that found the index ahead of the stored position: a push
    /// whose acknowledgement or position write was lost.
    recovered: u64 = 0,
    resyncs_started: u64 = 0,
    resync_round: u64 = 0,
    resync_phase: wire.Phase = .idle,
    resync_completed_at: ?u64 = null,
    last_error_at: u64 = 0,
    last_error_len: usize = 0,
    last_error_buf: [240]u8 = undefined,

    pub fn lastError(self: *const Health) []const u8 {
        return self.last_error_buf[0..self.last_error_len];
    }
};

pub const StepResult = struct {
    pushes: usize = 0,
    resync_pages: usize = 0,
    /// The log is drained and no resync is in progress.
    idle: bool = false,
};

pub const Error = error{
    /// The committed cursor belongs to another controller.
    ForeignCursor,
    /// The index has no completed import to reconcile into.
    IndexNotReady,
    /// A compare-and-set guard failed; the next step re-reads the cursor.
    Conflict,
    /// The server refused the request body; `Health.lastError` has its answer.
    Rejected,
    /// The store broke the interface contract.
    SourceContract,
};

const Known = struct {
    arena: std.heap.ArenaAllocator,
    cursor: wire.Cursor,
    /// The committed cursor's JSON, sent back as `expected_source_cursor`.
    committed: []const u8,
    revision: u64,
    watermark: u64,
    policy_clock: u64,
};

gpa: Allocator,
io: std.Io,
config: Config,
store: source.Source,
client: wire.Client,
scratch: std.heap.ArenaAllocator,
known: ?Known = null,
batch_cap: usize,
/// Backend sweep progress past pages that needed no write.
backend_after: ?[]u8 = null,
last_beat_at: u64 = 0,
resync_requested: std.atomic.Value(bool) = .init(false),
mutex: std.Io.Mutex = .init,
stats: Health = .{},

pub fn init(gpa: Allocator, io: std.Io, config: Config, store: source.Source, transport: wire.Transport) Bridge {
    std.debug.assert(config.max_actors >= 1 and config.max_actors <= wire.max_batch);
    std.debug.assert(config.log_read_limit >= 1);
    return .{
        .gpa = gpa,
        .io = io,
        .config = config,
        .store = store,
        .client = .{ .transport = transport },
        .scratch = .init(gpa),
        .batch_cap = config.max_actors,
    };
}

pub fn deinit(self: *Bridge) void {
    self.forget();
    self.scratch.deinit();
}

pub fn health(self: *Bridge) Health {
    self.mutex.lockUncancelable(self.io);
    defer self.mutex.unlock(self.io);
    return self.stats;
}

/// Start a full resync round at the next step, or after the current round.
pub fn requestResync(self: *Bridge) void {
    self.resync_requested.store(true, .release);
}

pub fn run(self: *Bridge, stop: *const std.atomic.Value(bool)) void {
    while (!stop.load(.acquire)) {
        const result = self.step() catch {
            self.io.sleep(.fromMilliseconds(@intCast(self.config.error_backoff_ms)), .awake) catch return;
            continue;
        };
        if (self.rest(result)) |ms| self.io.sleep(.fromMilliseconds(@intCast(ms)), .awake) catch return;
    }
}

/// Milliseconds the loop waits before the step that follows `result`.
pub fn rest(self: *const Bridge, result: StepResult) ?u64 {
    return if (result.idle) self.config.poll_interval_ms else null;
}

/// One bounded unit of work: up to `max_batches_per_step` pushes from the
/// change log, then up to `resync_pages_per_step` resync pages.
pub fn step(self: *Bridge) !StepResult {
    _ = self.scratch.reset(.retain_capacity);
    return self.work() catch |err| {
        self.forget();
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        self.stats.errors += 1;
        if (err == error.Conflict) self.stats.conflicts += 1;
        if (err != error.Rejected) self.noteLocked("{s}", .{@errorName(err)});
        return err;
    };
}

fn work(self: *Bridge) !StepResult {
    if (self.known == null) try self.refresh();
    var result: StepResult = .{};
    var drained = false;
    while (result.pushes < self.config.max_batches_per_step) {
        const pushed = try self.tail(&drained);
        if (pushed) result.pushes += 1;
        if (drained) break;
    }
    while (result.resync_pages < self.config.resync_pages_per_step) {
        if (!try self.resyncPage()) break;
        result.resync_pages += 1;
    }
    try self.flushBackendProgress();
    // A round that completed in this step makes the watermark sendable.
    if (result.resync_pages > 0 and try self.tail(&drained)) result.pushes += 1;
    result.idle = drained and self.known.?.cursor.reconciliation.phase == .idle and !self.resync_requested.load(.acquire);
    return result;
}

fn now(self: *Bridge) u64 {
    if (self.config.clock) |clock| return clock.now(clock.ptr);
    return @intCast(@divFloor(std.Io.Timestamp.now(self.io, .real).nanoseconds, std.time.ns_per_s));
}

fn noteLocked(self: *Bridge, comptime format: []const u8, args: anytype) void {
    const text = std.fmt.bufPrint(&self.stats.last_error_buf, format, args) catch blk: {
        const tail_text = "...";
        @memcpy(self.stats.last_error_buf[self.stats.last_error_buf.len - tail_text.len ..], tail_text);
        break :blk self.stats.last_error_buf[0..];
    };
    self.stats.last_error_len = text.len;
    self.stats.last_error_at = self.now();
}

fn note(self: *Bridge, comptime format: []const u8, args: anytype) void {
    self.mutex.lockUncancelable(self.io);
    defer self.mutex.unlock(self.io);
    self.noteLocked(format, args);
}

fn forget(self: *Bridge) void {
    if (self.known) |*known| known.arena.deinit();
    self.known = null;
    if (self.backend_after) |after| self.gpa.free(after);
    self.backend_after = null;
}

fn adopt(self: *Bridge, cursor: wire.Cursor, committed: []const u8, revision: u64, watermark: u64, policy_clock: u64) !void {
    var arena: std.heap.ArenaAllocator = .init(self.gpa);
    errdefer arena.deinit();
    const a = arena.allocator();
    var copy = cursor;
    copy.source_policy = wire.source_policy;
    copy.source_id = try a.dupe(u8, cursor.source_id);
    copy.build_id = try a.dupe(u8, cursor.build_id);
    copy.reconciliation.after_did = try a.dupe(u8, cursor.reconciliation.after_did);
    const committed_copy = try a.dupe(u8, committed);
    const next: Known = .{
        .arena = arena,
        .cursor = copy,
        .committed = committed_copy,
        .revision = revision,
        .watermark = watermark,
        .policy_clock = policy_clock,
    };
    if (self.known) |*old| old.arena.deinit();
    self.known = next;
}

/// Read what the index has committed and decide where to continue.
fn refresh(self: *Bridge) !void {
    const arena = self.scratch.allocator();
    const status = try self.client.status(arena);
    if (!status.import_complete) return error.IndexNotReady;
    const state = try self.client.sourceState(arena, &.{});
    if (state.revision != status.source_observation_revision) return error.Conflict;
    const stored = try self.store.cursors();
    const policy_clock = status.policy_observed_at orelse 0;

    if (wire.parseCursor(arena, state.cursor)) |cursor| {
        if (std.mem.eql(u8, cursor.source_id, self.config.source_id)) {
            try self.adopt(cursor, state.cursor, state.revision, status.watermark, policy_clock);
            const differs = stored.bridge == null or stored.bridge.? != cursor.position;
            if (differs) try self.store.setPosition(cursor.position);
            self.mutex.lockUncancelable(self.io);
            defer self.mutex.unlock(self.io);
            if (stored.bridge != null and stored.bridge.? < cursor.position) self.stats.recovered += 1;
            self.stats.position = cursor.position;
            self.stats.index_position = cursor.position;
            self.stats.watermark = status.watermark;
            self.publishResync(cursor.reconciliation);
            return;
        }
    } else if (!std.mem.eql(u8, state.cursor, "null") and !self.config.adopt_foreign_cursor) {
        return error.ForeignCursor;
    }

    try self.adopt(.{
        .source_id = self.config.source_id,
        .build_id = status.build_id,
        .snapshot_watermark = status.snapshot_watermark,
        .position = stored.next_change,
        .through = status.watermark,
    }, state.cursor, state.revision, status.watermark, policy_clock);
    self.restart(stored.next_change);
    self.mutex.lockUncancelable(self.io);
    defer self.mutex.unlock(self.io);
    self.stats.position = stored.bridge;
    self.stats.watermark = status.watermark;
}

/// Begin a resync the index cannot be fresh without: the position moves to
/// the end of the log, and no watermark is sent until the round completes.
/// Every change below that position was committed before the walk starts,
/// so the walk sees it. Nothing is committed here; the first resync page
/// carries the new cursor.
fn restart(self: *Bridge, next_change: u64) void {
    const known = &self.known.?;
    known.cursor.position = next_change;
    known.cursor.reconciliation = .{
        .round = known.cursor.reconciliation.round + 1,
        .phase = .source,
        .round_started_at = self.now(),
    };
    if (self.backend_after) |after| self.gpa.free(after);
    self.backend_after = null;
    self.mutex.lockUncancelable(self.io);
    defer self.mutex.unlock(self.io);
    self.stats.resyncs_started += 1;
    self.publishResync(known.cursor.reconciliation);
}

fn publishResync(self: *Bridge, r: wire.Reconciliation) void {
    self.stats.resync_round = r.round;
    self.stats.resync_phase = r.phase;
    self.stats.resync_completed_at = r.last_completed_at;
}

const Batch = struct {
    entries: std.ArrayList(wire.Entry) = .empty,
    bytes: usize = 0,
    rejected: u64 = 0,
};

/// Add the document to send for `did`: its current record, or a tombstone
/// when the store has no live record or the index could not hold it. A DID
/// the index refuses outright adds nothing.
fn document(self: *Bridge, batch: *Batch, did: []const u8, version: u64) !void {
    const arena = self.scratch.allocator();
    if (!wire.validDid(did)) {
        batch.rejected += 1;
        self.note("change log names an inadmissible DID ({d} bytes)", .{did.len});
        return;
    }
    var entry: wire.Entry = .{ .did = did, .version = version, .record = null };
    if (try self.store.actor(arena, did)) |actor| {
        // The index refuses a live actor without a handle; the Turso adapter
        // sends the same record as a deletion.
        if (actor.status == .live and actor.handle.len > 0) {
            if (wire.rejection(arena, actor)) |reason| {
                batch.rejected += 1;
                self.note("{s}: {s}; sent as a tombstone", .{ did, reason });
            } else {
                entry.record = actor;
                batch.bytes += wire.profileBytes(actor);
            }
        }
    }
    batch.bytes += did.len + 256;
    try batch.entries.append(arena, entry);
}

/// Push the next coalesced run of change-log entries. Returns whether a
/// request was committed; `drained` reports that the end of the log was
/// reached.
fn tail(self: *Bridge, drained: *bool) !bool {
    const arena = self.scratch.allocator();
    const observed_at = self.now();
    const cursor = self.known.?.cursor;
    const stored = try self.store.cursors();
    {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        self.stats.next_change = stored.next_change;
        self.stats.lag = stored.next_change -| cursor.position;
    }
    if (cursor.position > stored.next_change) {
        self.note("position {d} is past the change log's end {d}; resyncing", .{ cursor.position, stored.next_change });
        self.restart(stored.next_change);
        drained.* = true;
        return false;
    }
    const entries = switch (try self.store.changes(arena, cursor.position, self.config.log_read_limit)) {
        .entries => |entries| entries,
        .trimmed => |floor| {
            self.note("position {d} is below the change log's floor {d}; resyncing", .{ cursor.position, floor });
            self.restart((try self.store.cursors()).next_change);
            drained.* = true;
            return false;
        },
    };

    var batch: Batch = .{};
    var index_of: std.StringArrayHashMapUnmanaged(?usize) = .empty;
    var next_position = cursor.position;
    var consumed: usize = 0;
    for (entries) |change| {
        if (change.sequence < next_position) return error.SourceContract;
        if (index_of.get(change.did)) |slot| {
            if (slot) |index| batch.entries.items[index].version = change.sequence;
        } else {
            if (batch.entries.items.len >= self.batch_cap or batch.bytes >= self.config.max_profile_bytes) break;
            const before = batch.entries.items.len;
            try self.document(&batch, change.did, change.sequence);
            try index_of.put(arena, change.did, if (batch.entries.items.len > before) before else null);
        }
        next_position = change.sequence + 1;
        consumed += 1;
    }
    drained.* = consumed == entries.len and entries.len < self.config.log_read_limit;

    const complete = cursor.reconciliation.last_completed_at != null;
    const moved = next_position != cursor.position;
    const beat = drained.* and complete and (moved or observed_at >= self.last_beat_at +| self.config.heartbeat_s);
    if (!moved and !beat) return false;

    var next = cursor;
    next.position = next_position;
    const known = &self.known.?;
    const watermark: ?u64 = if (beat) @max(observed_at, known.watermark) else null;
    if (watermark) |value| next.through = value;
    const policy: ?wire.PolicySnapshot = if (try self.store.policy(arena)) |value|
        .{ .policy = value, .observed_at = @max(observed_at, known.policy_clock) }
    else
        null;
    try self.push(batch, next, watermark, policy);
    if (beat) self.last_beat_at = observed_at;
    return true;
}

/// One compare-and-set reconcile. On success the new cursor is the known
/// cursor and its position is stored; on any failure nothing is recorded.
fn push(self: *Bridge, batch: Batch, next: wire.Cursor, watermark: ?u64, policy: ?wire.PolicySnapshot) !void {
    const arena = self.scratch.allocator();
    const known = &self.known.?;
    const dids = try arena.alloc([]const u8, batch.entries.items.len);
    for (batch.entries.items, dids) |entry, *did| did.* = entry.did;

    const state = try self.client.sourceState(arena, dids);
    if (state.revision != known.revision) return error.Conflict;

    const body = try wire.encodeReconcile(arena, .{
        .entries = batch.entries.items,
        .expected = state.documents,
        .cursor = next,
        .expected_cursor = known.committed,
        .expected_revision = known.revision,
        .watermark = watermark,
        .policy = policy,
    });
    const receipt = switch (try self.client.reconcile(arena, body)) {
        .committed => |receipt| receipt,
        .conflict => return error.Conflict,
        .invalid => |answer| {
            self.note("server refused {d} documents (first {s}): {s}", .{
                dids.len,
                if (dids.len > 0) dids[0] else "none",
                answer[0..@min(answer.len, 120)],
            });
            if (dids.len > 1) self.batch_cap = dids.len / 2;
            return error.Rejected;
        },
    };
    if (receipt.source_observation_revision != known.revision + 1) return error.MalformedReply;

    const committed = try wire.encodeCursor(arena, next);
    const policy_clock = if (policy) |value| value.observed_at else known.policy_clock;
    try self.adopt(next, committed, receipt.source_observation_revision, receipt.watermark, policy_clock);
    self.batch_cap = @min(self.config.max_actors, self.batch_cap * 2);
    {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        self.stats.pushes += 1;
        if (self.stats.pushes == 1) log.info("first push committed: position {d}, watermark {d}{s}", .{ next.position, receipt.watermark, if (watermark == null) " (not moved)" else "" });
        self.stats.applied += receipt.applied;
        self.stats.unchanged += receipt.replayed;
        self.stats.rejected += batch.rejected;
        self.stats.index_position = next.position;
        self.stats.lag = self.stats.next_change -| next.position;
        self.stats.watermark = receipt.watermark;
        self.stats.last_push_at = self.now();
        self.publishResync(next.reconciliation);
    }
    if (self.stats.position == null or self.stats.position.? != next.position) {
        try self.store.setPosition(next.position);
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        self.stats.position = next.position;
    }
}

/// One page of the full resync. The source phase walks every record in the
/// store and reconciles it; the backend phase walks every DID in the index
/// and tombstones the ones the store has no record of. Progress lives in the
/// cursor, so a round resumes where it stopped.
fn resyncPage(self: *Bridge) !bool {
    const arena = self.scratch.allocator();
    const known = &self.known.?;
    const clock = self.now();
    if (known.cursor.reconciliation.phase == .idle) {
        const r = known.cursor.reconciliation;
        const interval = self.config.resync_interval_s;
        const scheduled = interval > 0 and r.last_completed_at != null and clock >= r.last_completed_at.? +| interval;
        const after = self.config.resync_after;
        const owed = after > 0 and clock >= after and (r.last_completed_started_at orelse 0) < after;
        if (!self.resync_requested.swap(false, .acq_rel) and !scheduled and !owed) return false;
        known.cursor.reconciliation.round = r.round + 1;
        known.cursor.reconciliation.phase = .source;
        known.cursor.reconciliation.round_started_at = clock;
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        self.stats.resyncs_started += 1;
    }

    var next = known.cursor;
    var batch: Batch = .{};
    switch (next.reconciliation.phase) {
        .idle => unreachable,
        .source => {
            // A walk that returns more than it was asked for is cut to the
            // cap: the rest comes back in the page after `last`.
            const walked = try self.store.walk(arena, next.reconciliation.after_did, self.batch_cap);
            const dids = walked[0..@min(walked.len, self.batch_cap)];
            if (dids.len == 0) {
                next.reconciliation.phase = .backend;
                next.reconciliation.after_did = "";
            }
            var last = next.reconciliation.after_did;
            for (dids) |did| {
                if (std.mem.order(u8, did, last) != .gt) return error.SourceContract;
                if (batch.bytes >= self.config.max_profile_bytes) break;
                try self.document(&batch, did, next.position);
                last = did;
            }
            next.reconciliation.after_did = last;
        },
        .backend => {
            const after = self.backend_after orelse next.reconciliation.after_did;
            const page = try self.client.sourcePage(arena, after, self.batch_cap);
            if (page.revision != known.revision) return error.Conflict;
            if (page.dids.len == 0) {
                next.reconciliation = .{
                    .round = next.reconciliation.round,
                    .round_started_at = next.reconciliation.round_started_at,
                    .last_completed_at = clock,
                    .last_completed_started_at = next.reconciliation.round_started_at,
                };
            } else {
                for (page.dids) |did| {
                    if (try self.store.actor(arena, did) == null) {
                        try batch.entries.append(arena, .{ .did = did, .version = next.position, .record = null });
                    }
                }
                const last = page.dids[page.dids.len - 1];
                if (batch.entries.items.len == 0) {
                    const copy = try self.gpa.dupe(u8, last);
                    if (self.backend_after) |old| self.gpa.free(old);
                    self.backend_after = copy;
                    return true;
                }
                next.reconciliation.after_did = last;
            }
            if (self.backend_after) |old| self.gpa.free(old);
            self.backend_after = null;
        },
    }
    try self.push(batch, next, null, null);
    return true;
}

fn flushBackendProgress(self: *Bridge) !void {
    const after = self.backend_after orelse return;
    var next = self.known.?.cursor;
    next.reconciliation.after_did = after;
    try self.push(.{}, next, null, null);
    self.gpa.free(after);
    self.backend_after = null;
}
