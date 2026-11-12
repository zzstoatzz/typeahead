//! The writer handle: the only code that opens the zkv store for writing.
//!
//! Every change goes through a `Batch`. A batch stages each affected key,
//! and its commit appends one change-log entry per actor whose content it
//! altered, stores any cursor it carries, and lands as one zkv commit. So
//! the derived ranges always agree with the actor records, the change log
//! lists changed actors in commit order, and a cursor never moves apart
//! from the data it covers.
//!
//! All times are unix seconds passed in by the caller; the store reads no
//! clock for data. The stored `hidden` flag is as of the last write to its
//! actor: a label expiry takes effect when something next recomputes it.

const std = @import("std");
const change = @import("change.zig");
const engine = @import("engine.zig");
const keys = @import("keys.zig");
const maintain = @import("maintain.zig");
const moderation = @import("moderation.zig");
const read = @import("read.zig");
const record = @import("record.zig");
const scan = @import("scan.zig");
const time = @import("time.zig");
const Io = std.Io;
const Actor = record.Actor;
const Change = change.Change;
const Outcome = change.Outcome;

pub const format = 1;

pub const Options = struct {
    layout: engine.Layout = .{},
    maintenance: maintain.Policy = .steady,
    trace: ?maintain.Trace = null,
    /// The labeler whose labels are stored and can hide an actor.
    moderation_source: []const u8 = moderation.bsky_moderation,
    /// Whether a handle is hidden on sight. The Worker's rule is a set of
    /// Unicode regexes (src/moderation-regexes.json) that Zig cannot run, so
    /// the owner supplies the predicate; without one no handle is flagged.
    handle_flagged: ?*const fn (handle: []const u8) bool = null,
};

pub const Error = error{
    /// A commit failed, so what landed is unknown. Close and reopen.
    StoreBroken,
    BatchOpen,
    UnsupportedStoreFormat,
    InconsistentStore,
    InvalidCursor,
};

pub const Store = struct {
    gpa: std.mem.Allocator,
    handle: *engine.Handle,
    options: Options,
    maintainer: maintain.Maintainer,
    domains: moderation.Domains = .empty,
    next_change: u64 = 1,
    broken: bool = false,
    batch_open: bool = false,
    batch: Batch = undefined,

    /// Open the store at `path` under `root` for writing, creating it when
    /// missing. Opening fences any earlier writer on the same directory: its
    /// next commit fails.
    pub fn open(gpa: std.mem.Allocator, io: Io, root: Io.Dir, path: []const u8, options: Options) !*Store {
        const handle = try engine.Handle.open(gpa, io, root, path, options.layout, .writer);
        errdefer handle.close();
        const self = try gpa.create(Store);
        errdefer gpa.destroy(self);
        self.* = .{
            .gpa = gpa,
            .handle = handle,
            .options = options,
            .maintainer = .{ .db = handle.db, .io = io, .policy = options.maintenance, .trace = options.trace },
        };
        errdefer self.clearDomains();
        var buf: keys.Buf = undefined;
        if (try read.counter(handle.db, .format)) |found| {
            if (found != format) return error.UnsupportedStoreFormat;
        } else {
            try handle.db.put(keys.cursor(&buf, .format), &record.encodeU64(format));
            try handle.db.commit();
        }
        self.next_change = try read.counter(handle.db, .next_change) orelse 1;
        try self.loadDomains();
        return self;
    }

    pub fn close(self: *Store) void {
        if (self.batch_open) self.batch.release();
        self.maintainer.deinit();
        self.clearDomains();
        self.handle.close();
        self.gpa.destroy(self);
    }

    fn clearDomains(self: *Store) void {
        var it = self.domains.keyIterator();
        while (it.next()) |name| self.gpa.free(name.*);
        self.domains.deinit(self.gpa);
        self.domains = .empty;
    }

    fn loadDomains(self: *Store) !void {
        self.clearDomains();
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        for (try read.blockedDomains(self.handle.db, arena.allocator())) |policy| {
            const name = try self.gpa.dupe(u8, policy.subject);
            errdefer self.gpa.free(name);
            try self.domains.put(self.gpa, name, {});
        }
    }

    pub fn db(self: *Store) *engine.Db {
        return self.handle.db;
    }

    /// Start a batch at time `now`. One batch at a time; end it with
    /// `commit` or `abort`.
    pub fn begin(self: *Store, now: u64) Error!*Batch {
        if (self.broken) return error.StoreBroken;
        if (self.batch_open) return error.BatchOpen;
        self.batch = .{ .store = self, .now = now, .arena = .init(self.gpa), .scratch = .init(self.gpa) };
        self.batch_open = true;
        return &self.batch;
    }

    /// One change, with the cursor it covers, in one commit.
    pub fn apply(self: *Store, now: u64, one: Change, cursor: ?change.CursorUpdate) !Outcome {
        const batch = try self.begin(now);
        errdefer batch.abort();
        const outcome = try batch.apply(one);
        if (cursor) |update| try batch.setCursor(update);
        try batch.commit();
        return outcome;
    }

    /// Move a cursor with no data, as the index bridge does after it has
    /// applied a stretch of the change log.
    pub fn setCursor(self: *Store, update: change.CursorUpdate) !void {
        const batch = try self.begin(0);
        errdefer batch.abort();
        try batch.setCursor(update);
        try batch.commit();
    }

    pub const Sweep = struct {
        position: scan.Position = .{},
        started: bool = false,
        scanned: u64 = 0,
        changed: u64 = 0,

        pub fn done(self: *const Sweep) bool {
            return self.started and self.position.done;
        }

        pub fn deinit(self: *Sweep, gpa: std.mem.Allocator) void {
            self.position.deinit(gpa);
        }
    };

    /// Bring stored `hidden` flags in line after a domain was blocked or
    /// unblocked. Blocking records the policy at once, and every later write
    /// to a matching actor honours it; actors already stored are found here,
    /// by walking the handle range `scan_limit` keys per call, one commit
    /// each. Call until `sweep.done()`, with other commits free to run in
    /// between.
    pub fn sweepDomain(self: *Store, sweep: *Sweep, domain: []const u8, now: u64, scan_limit: usize) !void {
        const bounds = keys.Prefix.handle.bounds();
        if (!sweep.started) {
            try sweep.position.reset(self.gpa, &bounds[0]);
            sweep.started = true;
        }
        const batch = try self.begin(now);
        errdefer batch.abort();
        var matched: Matched = .{ .arena = batch.arena.allocator(), .domain = domain };
        const scanned = try scan.run(self.gpa, self.db(), &sweep.position, &bounds[1], .{ .limit = scan_limit }, &matched);
        sweep.scanned += scanned.entries;
        for (matched.dids.items) |did| {
            const outcome = try batch.apply(.{ .hidden_refreshed = did });
            sweep.changed += outcome.changed;
        }
        try batch.commit();
    }

    const Matched = struct {
        arena: std.mem.Allocator,
        domain: []const u8,
        dids: std.ArrayList([]const u8) = .empty,

        pub fn entry(self: *Matched, key: []const u8, _: []const u8) !void {
            const parsed = try keys.parseHandle(key);
            if (moderation.matchesDomain(parsed.handle, self.domain)) try self.dids.append(self.arena, try self.arena.dupe(u8, parsed.did));
        }
    };

    pub const Reverified = struct { examined: usize = 0, queued: usize = 0, finished: bool = false };

    /// One step of a pass, made once in the life of a store, that sends the
    /// live actors holding `handle` back through identity resolution: for
    /// `handle.invalid`, the accounts an earlier build marked unresolvable
    /// on the appview's word. A step looks at up to `limit` holders in DID
    /// order, fewer when that would bring the identity entries due past
    /// `backlog_limit`, and queues those not queued already. Where the pass
    /// stands is stored in the step's own commit, so a restart resumes it,
    /// and once it has reached the end every later call does nothing.
    pub fn reverifyHandle(self: *Store, now: u64, handle: []const u8, limit: usize, backlog_limit: usize) !Reverified {
        const batch = try self.begin(now);
        errdefer batch.abort();
        const a = batch.arena.allocator();
        var state = try read.repairState(self.db(), a);
        var step: Reverified = .{ .finished = state.finished_at != 0 };
        const room = if (step.finished) 0 else @min(limit, backlog_limit -| try batch.identityBacklog(backlog_limit));
        if (room > 0) {
            var buf: keys.Buf = undefined;
            const prefix = try keys.handlePrefix(&buf, handle);
            var start: std.ArrayList(u8) = .empty;
            try start.appendSlice(a, prefix);
            if (state.position.len > 0) {
                try start.appendSlice(a, state.position);
                try start.append(a, 0);
            }
            const end = try a.dupe(u8, prefix);
            end[end.len - 1] = 1;
            var found: Keys = .{ .arena = a };
            _ = try scan.all(a, self.db(), start.items, end, .{ .limit = room }, &found);
            for (found.keys.items) |key| {
                const did = (try keys.parseHandle(key)).did;
                state.position = did;
                step.examined += 1;
                _ = batch.scratch.reset(.retain_capacity);
                const holder = try batch.load(did) orelse return error.InconsistentStore;
                if (holder.identity_due != 0) continue;
                _ = try batch.apply(.{ .identity_enqueue = .{ .did = did, .reason = .identity } });
                step.queued += 1;
            }
            if (state.started_at == 0) state.started_at = now;
            state.examined += step.examined;
            state.queued += step.queued;
            if (found.keys.items.len < room) {
                state.finished_at = now;
                step.finished = true;
            }
            _ = batch.scratch.reset(.retain_capacity);
            var value: std.ArrayList(u8) = .empty;
            try state.encode(batch.tmp(), &value);
            try self.db().put(keys.cursor(&buf, .handle_repair), value.items);
        }
        try batch.commit();
        return step;
    }

    pub const Trimmed = struct { removed: u64, floor: u64, more: bool };

    /// Drop change-log entries below `below`, the lowest position any
    /// consumer still needs, at most `limit` per call. Afterwards a consumer
    /// asking for a position under the floor is told the log was trimmed.
    pub fn trimChanges(self: *Store, below: u64, limit: usize) !Trimmed {
        const batch = try self.begin(0);
        errdefer batch.abort();
        const arena = batch.scratch.allocator();
        const target = @min(below, self.next_change);
        const old_floor = try read.counter(self.db(), .change_floor) orelse 0;
        var start_buf: keys.Buf = undefined;
        var end_buf: keys.Buf = undefined;
        var found: Keys = .{ .arena = arena };
        // Nothing is left below the floor but the deletions of earlier
        // trims, and a scan from 0 walks every one of them to reach the
        // first entry: 52 s for 11.5 million on a cold start, 2026-11-11.
        _ = try scan.all(arena, self.db(), keys.change(&start_buf, @min(old_floor, target)), keys.change(&end_buf, target), .{ .limit = limit }, &found);
        var floor = @max(old_floor, target);
        const more = found.keys.items.len == limit and limit > 0 and target > 0;
        if (more) floor = @max(old_floor, 1 + try keys.parseChange(found.keys.items[found.keys.items.len - 1]));
        for (found.keys.items) |key| try self.db().del(key);
        if (floor != old_floor) try self.db().put(keys.cursor(&start_buf, .change_floor), &record.encodeU64(floor));
        try batch.commit();
        return .{ .removed = found.keys.items.len, .floor = floor, .more = more };
    }

    /// Call between commits: one maintenance round, then relief if level 0
    /// has built up. The result says whether the writer should slow down.
    pub fn maintainStep(self: *Store) !maintain.Pressure {
        if (self.broken) return error.StoreBroken;
        _ = try self.maintainer.step();
        return self.maintainer.relieve();
    }

    pub fn stats(self: *Store) maintain.Stats {
        return self.maintainer.stats();
    }

    pub fn diskUsage(self: *Store) !maintain.Disk {
        return maintain.disk(self.gpa, self.handle.io, self.handle.dir);
    }

    pub fn actor(self: *Store, arena: std.mem.Allocator, did: []const u8) !?Actor {
        return read.actor(self.db(), arena, did);
    }

    pub fn actors(self: *Store, arena: std.mem.Allocator, dids: []const []const u8) ![]?Actor {
        return read.actors(self.db(), arena, dids);
    }

    pub fn walk(self: *Store, arena: std.mem.Allocator, after_did: []const u8, limit: usize) ![]const []const u8 {
        return read.walk(self.db(), arena, after_did, limit);
    }

    pub fn byHandle(self: *Store, arena: std.mem.Allocator, handle: []const u8) ![]const []const u8 {
        return read.byHandle(self.db(), arena, handle);
    }

    pub fn timed(self: *Store, arena: std.mem.Allocator, prefix: keys.Prefix, after: ?read.Timed, until: u64, limit: usize) ![]const read.Timed {
        return read.timed(self.db(), arena, prefix, after, until, limit);
    }

    pub fn quality(self: *Store, arena: std.mem.Allocator, after: ?read.Quality, limit: usize) ![]const read.Quality {
        return read.quality(self.db(), arena, after, limit);
    }

    pub fn changes(self: *Store, arena: std.mem.Allocator, from: u64, limit: usize) !read.Changes {
        return read.changes(self.db(), arena, from, limit);
    }

    pub fn cursors(self: *Store, arena: std.mem.Allocator) !read.Cursors {
        return read.cursors(self.db(), arena);
    }

    pub fn counts(self: *Store) !read.Counts {
        return read.counts(self.gpa, self.db(), .{});
    }
};

const Keys = struct {
    arena: std.mem.Allocator,
    keys: std.ArrayList([]const u8) = .empty,

    pub fn entry(self: *Keys, key: []const u8, _: []const u8) !void {
        try self.keys.append(self.arena, try self.arena.dupe(u8, key));
    }
};

const derived = [_]keys.Prefix{ .handle, .quality, .refresh, .tombstone };

fn derivedKey(buf: *keys.Buf, prefix: keys.Prefix, did: []const u8, actor: ?Actor) !?[]const u8 {
    const a = actor orelse return null;
    return switch (prefix) {
        .handle => if (a.live() and a.handle.len > 0) try keys.handle(buf, a.handle, did) else null,
        .quality => if (a.ranked()) try keys.quality(buf, a.quality_score, did) else null,
        .refresh => if (a.live() and a.refresh_due != 0) try keys.timed(buf, .refresh, a.refresh_due, did) else null,
        .tombstone => if (!a.live()) try keys.timed(buf, .tombstone, a.deleted_at, did) else null,
        else => unreachable,
    };
}

pub const Batch = struct {
    store: *Store,
    now: u64,
    /// Lives until the batch ends: changed DIDs and leased jobs.
    arena: std.heap.ArenaAllocator,
    /// Reset before every change.
    scratch: std.heap.ArenaAllocator,
    changed: std.ArrayList([]const u8) = .empty,
    changed_set: std.StringHashMapUnmanaged(void) = .empty,
    jobs: std.ArrayList(change.Job) = .empty,
    refresh_jobs: std.ArrayList(change.RefreshJob) = .empty,
    walk_tally: change.WalkTally = .initFill(.{}),
    domains_dirty: bool = false,
    encoded: std.ArrayList(u8) = .empty,

    fn release(self: *Batch) void {
        self.arena.deinit();
        self.scratch.deinit();
        self.store.batch_open = false;
    }

    /// Drop everything staged. The store is as it was before `begin`.
    pub fn abort(self: *Batch) void {
        const store = self.store;
        if (!store.batch_open) return;
        store.db().discard() catch {
            store.broken = true;
        };
        if (self.domains_dirty) store.loadDomains() catch {
            store.broken = true;
        };
        self.release();
    }

    /// Identity jobs leased by this batch, valid until it ends.
    pub fn leased(self: *const Batch) []const change.Job {
        return self.jobs.items;
    }

    /// Refreshes leased by this batch, valid until it ends.
    pub fn refreshLeased(self: *const Batch) []const change.RefreshJob {
        return self.refresh_jobs.items;
    }

    /// What this batch's walk steps did, summed by pool.
    pub fn walked(self: *const Batch, pool: change.WalkPool) change.Walked {
        return self.walk_tally.get(pool);
    }

    /// Carry a cursor in this batch's commit.
    pub fn setCursor(self: *Batch, update: change.CursorUpdate) !void {
        var buf: keys.Buf = undefined;
        const engine_db = self.store.db();
        switch (update) {
            .relay => |value| try engine_db.put(keys.cursor(&buf, .relay), &record.encodeU64(value)),
            .bridge => |value| try engine_db.put(keys.cursor(&buf, .bridge), &record.encodeU64(value)),
            .labeler => |value| {
                const key = keys.cursor(&buf, .labeler);
                const stored = try read.counter(engine_db, .labeler) orelse 0;
                if (value > stored) try engine_db.put(key, &record.encodeU64(value));
            },
            .control_policy => |value| try advance(engine_db, .control_policy, value),
            .control_requests => |value| try advance(engine_db, .control_requests, value),
            .plc => |value| {
                if (value.len == 0 or value.len > change.max_cursor_bytes) return error.InvalidCursor;
                const key = keys.cursor(&buf, .plc);
                if (try engine_db.get(key)) |stored| if (std.mem.order(u8, value, stored) != .gt) return;
                try engine_db.put(key, value);
            },
        }
    }

    /// Store `value` under `which` unless a value at least as high is there.
    fn advance(engine_db: *engine.Db, which: keys.Cursor, value: u64) !void {
        var buf: keys.Buf = undefined;
        if (try read.counter(engine_db, which)) |stored| if (value <= stored) return;
        try engine_db.put(keys.cursor(&buf, which), &record.encodeU64(value));
    }

    /// Land the batch as one commit. A failed commit leaves the store
    /// broken: zkv cannot say whether the log segment landed, so the handle
    /// must be closed and reopened to learn the truth.
    pub fn commit(self: *Batch) !void {
        const store = self.store;
        const engine_db = store.db();
        var buf: keys.Buf = undefined;
        var next = store.next_change;
        for (self.changed.items) |did| {
            try engine_db.put(keys.change(&buf, next), did);
            next += 1;
        }
        if (next != store.next_change) try engine_db.put(keys.cursor(&buf, .next_change), &record.encodeU64(next));
        if (engine_db.stagedBytes() > 0) {
            engine_db.commit() catch |err| {
                store.broken = true;
                self.release();
                return err;
            };
        }
        store.next_change = next;
        self.release();
    }

    /// Stage one change. A change that fails validation stages nothing and
    /// leaves the batch usable; after any other error the batch must be
    /// aborted, because part of the change may be staged.
    pub fn apply(self: *Batch, one: Change) !Outcome {
        try change.validate(one);
        _ = self.scratch.reset(.retain_capacity);
        self.encoded = .empty;
        return switch (one) {
            .observe => |c| self.observe(c),
            .profile => |c| self.profile(c),
            .identity_resolved => |c| self.identityResolved(c),
            .gone => |c| self.gone(c),
            .reactivated => |did| self.reactivated(did),
            .label => |c| self.label(c),
            .labels_reconciled => |c| self.labelsReconciled(c),
            .override_set => |c| self.overrideSet(c),
            .override_cleared => |did| self.overrideCleared(did),
            .domain_blocked => |c| self.domainBlocked(c),
            .domain_unblocked => |name| self.domainUnblocked(name),
            .enrichment => |c| self.enrichment(c),
            .identity_enqueue => |c| self.identityEnqueue(c),
            .identity_lease => |c| self.identityLease(c),
            .identity_complete => |c| self.identityComplete(c),
            .refresh_enqueue => |c| self.refreshEnqueue(c),
            .refresh_complete => |c| self.refreshComplete(c),
            .refresh_lease => |c| self.refreshLease(c),
            .refresh_retry => |c| self.refreshRetry(c),
            .refresh_walk => |c| self.refreshWalk(c),
            .hidden_refreshed => |did| self.hiddenRefreshed(did),
            .tombstones_pruned => |c| self.tombstonesPruned(c),
        };
    }

    fn tmp(self: *Batch) std.mem.Allocator {
        return self.scratch.allocator();
    }

    fn load(self: *Batch, did: []const u8) !?Actor {
        return read.actor(self.store.db(), self.tmp(), did);
    }

    fn fresh(self: *Batch, did: []const u8) !Actor {
        var actor: Actor = .{};
        if (try read.override(self.store.db(), self.tmp(), did)) |policy| actor.override = policy.action;
        return actor;
    }

    fn markChanged(self: *Batch, did: []const u8) !void {
        if (self.changed_set.contains(did)) return;
        const owned = try self.arena.allocator().dupe(u8, did);
        try self.changed_set.put(self.arena.allocator(), owned, {});
        try self.changed.append(self.arena.allocator(), owned);
    }

    fn hidden(self: *Batch, actor: Actor) bool {
        const options = &self.store.options;
        return moderation.resolveHidden(.{
            .labels = actor.labels,
            .handle = actor.handle,
            .override = actor.override,
            .handle_flagged = if (options.handle_flagged) |flagged| actor.handle.len > 0 and flagged(actor.handle) else false,
            .domain_blocked = moderation.domainBlocked(actor.handle, &self.store.domains),
            .source = options.moderation_source,
            .now = self.now,
        });
    }

    /// Write `wanted` for `did`: recompute `hidden`, stamp `updated_at` when
    /// the content changed, move every derived key whose inputs moved, and
    /// note the actor for the change log. `queue` is the identity queue
    /// state to store when the actor is, or stays, queued under a new due
    /// time or state.
    fn save(self: *Batch, did: []const u8, old: ?Actor, wanted: Actor, queue: ?record.QueueState) !Outcome {
        const engine_db = self.store.db();
        var new = wanted;
        if (!new.live()) {
            new.identity_due = 0;
            new.refresh_due = 0;
        } else new.deleted_at = 0;
        new.hidden = self.hidden(new);
        const content_changed = if (old) |o| !o.contentEql(new) else true;
        if (content_changed) new.updated_at = @max(self.now, if (old) |o| o.updated_at else 0);

        var old_buf: keys.Buf = undefined;
        var new_buf: keys.Buf = undefined;
        for (derived) |prefix| {
            const was = try derivedKey(&old_buf, prefix, did, old);
            const is = try derivedKey(&new_buf, prefix, did, new);
            if (was != null and is != null and std.mem.eql(u8, was.?, is.?)) continue;
            if (was) |key| try engine_db.del(key);
            if (is) |key| try engine_db.put(key, "");
        }

        const was_due = if (old) |o| o.identity_due else 0;
        if (was_due != 0 and (was_due != new.identity_due or queue != null)) try engine_db.del(try keys.timed(&old_buf, .identity, was_due, did));
        if (new.identity_due != 0 and (was_due != new.identity_due or queue != null)) {
            const state = queue orelse return error.InconsistentStore;
            var value: std.ArrayList(u8) = .empty;
            try state.encode(self.tmp(), &value);
            try engine_db.put(try keys.timed(&new_buf, .identity, new.identity_due, did), value.items);
        }

        if (old == null or !old.?.eql(new)) {
            try new.encode(self.tmp(), &self.encoded);
            try engine_db.put(try keys.actor(&new_buf, did), self.encoded.items);
        }
        if (content_changed) try self.markChanged(did);
        return .{ .applied = true, .changed = @intFromBool(content_changed), .created = old == null };
    }

    fn queueState(self: *Batch, did: []const u8, actor: Actor) !?record.QueueState {
        if (actor.identity_due == 0) return null;
        return try read.queueState(self.store.db(), self.tmp(), .{ .at = actor.identity_due, .did = did }) orelse error.InconsistentStore;
    }

    fn stampActivity(self: *Batch, actor: *Actor) void {
        if (actor.last_activity_at + change.activity_interval < self.now) actor.last_activity_at = self.now;
    }

    fn requestRefresh(self: *Batch, actor: *Actor) void {
        if (actor.handle.len == 0) return;
        actor.refresh_due = if (actor.refresh_due == 0) self.now else @min(actor.refresh_due, self.now);
    }

    fn discovered(self: *Batch, enqueue: bool, actor: *Actor) ?record.QueueState {
        if (!enqueue) return null;
        actor.identity_due = self.now;
        return .{ .reason = .discovered, .enqueued_at = self.now };
    }

    fn keep(stored: []const u8, incoming: []const u8) []const u8 {
        return if (incoming.len > 0) incoming else stored;
    }

    fn observe(self: *Batch, c: change.Observe) !Outcome {
        const old = try self.load(c.did);
        var new = old orelse try self.fresh(c.did);
        if (!new.live()) return .{};
        self.stampActivity(&new);
        const queue = if (old == null) self.discovered(c.enqueue_identity, &new) else null;
        return self.save(c.did, old, new, queue);
    }

    fn profile(self: *Batch, c: change.Profile) !Outcome {
        const old = try self.load(c.did);
        var new = old orelse try self.fresh(c.did);
        if (!new.live()) return .{};
        new.handle = keep(new.handle, c.handle);
        new.display_name = keep(new.display_name, c.display_name);
        new.avatar = keep(new.avatar, c.avatar);
        new.pds = keep(new.pds, c.pds);
        new.description = keep(new.description, c.description);
        new.banner = keep(new.banner, c.banner);
        self.stampActivity(&new);
        self.requestRefresh(&new);
        const queue = if (old == null) self.discovered(c.enqueue_identity, &new) else null;
        return self.save(c.did, old, new, queue);
    }

    fn resolve(self: *Batch, actor: *Actor, observed_at: u64, handle: []const u8, pds: []const u8) bool {
        if (actor.identity_resolved_at > observed_at) return false;
        actor.handle = keep(actor.handle, handle);
        actor.pds = keep(actor.pds, pds);
        actor.identity_resolved_at = observed_at;
        self.requestRefresh(actor);
        return true;
    }

    fn identityResolved(self: *Batch, c: change.IdentityResolved) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        if (!old.live()) return .{};
        var new = old;
        if (!self.resolve(&new, c.observed_at, c.handle, c.pds)) return .{};
        return self.save(c.did, old, new, null);
    }

    fn gone(self: *Batch, c: change.Gone) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        var new = old;
        new.status = switch (c.status) {
            .deactivated => .deactivated,
            .deleted => .deleted,
        };
        new.deleted_at = self.now;
        return self.save(c.did, old, new, null);
    }

    fn reactivated(self: *Batch, did: []const u8) !Outcome {
        const old = try self.load(did);
        var new = old orelse try self.fresh(did);
        if (old != null and new.live()) return .{};
        new.status = .live;
        self.stampActivity(&new);
        self.requestRefresh(&new);
        return self.save(did, old, new, null);
    }

    /// Rebuild the actor's served label list: every label from another
    /// source as stored, then the moderation service's from the label range.
    fn project(self: *Batch, did: []const u8, actor: *Actor, others: ?[]const record.Label) !void {
        const source = self.store.options.moderation_source;
        var list: std.ArrayList(record.Label) = .empty;
        for (others orelse actor.labels) |one| {
            if (!std.mem.eql(u8, one.source, source)) try list.append(self.tmp(), one);
        }
        for (try read.labels(self.store.db(), self.tmp(), did)) |row| {
            if (row.stored.negated) continue;
            try list.append(self.tmp(), .{
                .source = row.source,
                .uri = try change.subjectUri(self.tmp(), did, row.subject),
                .value = row.value,
                .created = row.stored.created,
                .cid = row.stored.cid,
                .expires = row.stored.expires,
            });
        }
        actor.labels = list.items;
    }

    fn putLabel(self: *Batch, key: []const u8, stored: record.StoredLabel) !void {
        var value: std.ArrayList(u8) = .empty;
        try stored.encode(self.tmp(), &value);
        try self.store.db().put(key, value.items);
    }

    fn label(self: *Batch, c: change.LabelEvent) !Outcome {
        if (!std.mem.eql(u8, c.source, self.store.options.moderation_source)) return .{};
        const old = try self.load(c.did) orelse return .{};
        if (!old.live()) return .{};
        var buf: keys.Buf = undefined;
        const key = try keys.label(&buf, c.did, c.source, c.value, c.subject);
        if (try self.store.db().get(key)) |borrowed| {
            if (c.sequence < (try record.StoredLabel.decode(borrowed)).sequence) return .{};
        }
        try self.putLabel(key, .{ .sequence = c.sequence, .negated = c.negated, .created = c.created, .cid = c.cid, .expires = c.expires });
        var new = old;
        try self.project(c.did, &new, null);
        return self.save(c.did, old, new, null);
    }

    /// Replace what an earlier answer stored for `did` with `current`, and
    /// keep every entry the stream delivered.
    fn reconcile(self: *Batch, did: []const u8, current: []const record.Label) !void {
        const engine_db = self.store.db();
        const source = self.store.options.moderation_source;
        var buf: keys.Buf = undefined;
        const rows = try read.labels(engine_db, self.tmp(), did);
        for (rows) |row| {
            if (row.stored.sequence == 0) try engine_db.del(try keys.label(&buf, did, row.source, row.value, row.subject));
        }
        for (current, 0..) |one, index| {
            if (!std.mem.eql(u8, one.source, source)) continue;
            const subject = change.subjectOf(did, one.uri) orelse continue;
            if (one.value.len == 0 or one.value.len > keys.max_label_value) continue;
            if (!isCurrent(current, index)) continue;
            const streamed = for (rows) |row| {
                if (row.stored.sequence > 0 and row.subject == subject and std.mem.eql(u8, row.value, one.value) and std.mem.eql(u8, row.source, one.source)) break true;
            } else false;
            if (streamed) continue;
            try self.putLabel(try keys.label(&buf, did, one.source, one.value, subject), .{ .sequence = 0, .negated = one.negated, .created = one.created, .cid = one.cid, .expires = one.expires });
        }
    }

    fn labelsReconciled(self: *Batch, c: change.LabelsReconciled) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        if (!old.live()) return .{};
        try self.reconcile(c.did, c.labels);
        var new = old;
        try self.project(c.did, &new, null);
        return self.save(c.did, old, new, null);
    }

    fn overrideSet(self: *Batch, c: change.OverrideSet) !Outcome {
        var buf: keys.Buf = undefined;
        const action: record.Override = switch (c.action) {
            .show => .show,
            .hide => .hide,
        };
        var value: std.ArrayList(u8) = .empty;
        try (record.PolicyEntry{ .action = action, .created_at = self.now, .reason = c.reason }).encode(self.tmp(), &value);
        try self.store.db().put(try keys.override(&buf, c.did), value.items);
        const old = try self.load(c.did) orelse return .{ .applied = true };
        var new = old;
        new.override = action;
        return self.save(c.did, old, new, null);
    }

    fn overrideCleared(self: *Batch, did: []const u8) !Outcome {
        var buf: keys.Buf = undefined;
        try self.store.db().del(try keys.override(&buf, did));
        const old = try self.load(did) orelse return .{ .applied = true };
        var new = old;
        new.override = .none;
        return self.save(did, old, new, null);
    }

    fn domainBlocked(self: *Batch, c: change.DomainBlocked) !Outcome {
        const store = self.store;
        var buf: keys.Buf = undefined;
        const key = try keys.domain(&buf, c.domain);
        var value: std.ArrayList(u8) = .empty;
        try (record.PolicyEntry{ .created_at = self.now, .reason = c.reason }).encode(self.tmp(), &value);
        try store.db().put(key, value.items);
        const name = try keys.parseDomain(key);
        if (!store.domains.contains(name)) {
            self.domains_dirty = true;
            const owned = try store.gpa.dupe(u8, name);
            errdefer store.gpa.free(owned);
            try store.domains.put(store.gpa, owned, {});
        }
        return .{ .applied = true };
    }

    fn domainUnblocked(self: *Batch, domain: []const u8) !Outcome {
        const store = self.store;
        var buf: keys.Buf = undefined;
        const key = try keys.domain(&buf, domain);
        try store.db().del(key);
        if (store.domains.fetchRemove(try keys.parseDomain(key))) |removed| {
            self.domains_dirty = true;
            store.gpa.free(removed.key);
        }
        return .{ .applied = true };
    }

    fn keepCount(stored: u64, incoming: u64) u64 {
        return if (incoming != 0) incoming else stored;
    }

    /// Identity queue entries due now, counted up to `limit`. The count
    /// includes what this batch has staged.
    fn identityBacklog(self: *Batch, limit: usize) !usize {
        var start: keys.Buf = undefined;
        var end: keys.Buf = undefined;
        var count: scan.Count = .{};
        _ = try scan.all(self.tmp(), self.store.db(), keys.timedBound(&start, .identity, 0), keys.timedBound(&end, .identity, self.now + 1), .{ .limit = limit }, &count);
        return count.entries;
    }

    fn handleVerdict(self: *Batch, old: Actor, c: change.Enrichment) !change.HandleVerdict {
        const check = c.handle_check orelse return .none;
        if (c.handle.len == 0 or std.ascii.eqlIgnoreCase(c.handle, old.handle)) return .none;
        if (old.identity_due != 0) return .pending;
        if (old.identity_resolved_at +| check.settle_seconds > self.now) return .settled;
        if (try self.identityBacklog(check.backlog_limit) >= check.backlog_limit) return .deferred;
        return .queued;
    }

    fn enrichment(self: *Batch, c: change.Enrichment) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        if (!old.live()) return .{};
        var new = old;
        const verdict = try self.handleVerdict(old, c);
        var queue: ?record.QueueState = null;
        if (verdict == .queued) {
            new.identity_due = self.now;
            queue = .{ .reason = .identity, .enqueued_at = self.now };
        }
        new.display_name = keep(new.display_name, c.display_name);
        new.avatar = keep(new.avatar, c.avatar);
        new.description = keep(new.description, c.description);
        new.banner = keep(new.banner, c.banner);
        new.created_at = keep(new.created_at, c.created_at);
        if (c.associated.len > 0 and !std.mem.eql(u8, c.associated, "{}")) new.associated = c.associated;
        new.followers_count = keepCount(new.followers_count, c.followers_count);
        new.follows_count = keepCount(new.follows_count, c.follows_count);
        new.posts_count = keepCount(new.posts_count, c.posts_count);
        if (c.quality_score != 0) new.quality_score = c.quality_score;
        new.profile_source = c.source;
        new.profile_checked_at = self.now;
        if (settles(old, c.lease)) new.refresh_due = c.next_refresh orelse 0;
        if (c.labels) |list| {
            try self.reconcile(c.did, list);
            try self.project(c.did, &new, list);
        }
        var outcome = try self.save(c.did, old, new, queue);
        outcome.handle = verdict;
        return outcome;
    }

    fn identityEnqueue(self: *Batch, c: change.IdentityEnqueue) !Outcome {
        const old = try self.load(c.did);
        if (old == null and c.reason == .plc) return .{};
        if (old != null and c.reason == .discovered) return .{};
        var new = old orelse try self.fresh(c.did);
        if (!new.live()) return .{};
        var state: record.QueueState = .{ .reason = c.reason, .enqueued_at = self.now };
        if (try self.queueState(c.did, new)) |queued| {
            state = queued;
            state.generation += 1;
            state.attempts = 0;
            state.reason = c.reason;
            new.identity_due = @min(new.identity_due, self.now);
        } else new.identity_due = self.now;
        return self.save(c.did, old, new, state);
    }

    fn identityLease(self: *Batch, c: change.IdentityLease) !Outcome {
        const due = try read.timed(self.store.db(), self.arena.allocator(), .identity, null, self.now, c.limit);
        for (due) |entry| {
            _ = self.scratch.reset(.retain_capacity);
            self.encoded = .empty;
            const old = try self.load(entry.did) orelse return error.InconsistentStore;
            if (!old.live() or old.identity_due != entry.at) return error.InconsistentStore;
            var state = (try self.queueState(entry.did, old)).?;
            state.attempts += 1;
            var new = old;
            new.identity_due = self.now + c.lease_seconds;
            _ = try self.save(entry.did, old, new, state);
            try self.jobs.append(self.arena.allocator(), .{ .did = entry.did, .generation = state.generation, .attempts = state.attempts, .reason = state.reason });
        }
        return .{ .applied = due.len > 0 };
    }

    fn identityComplete(self: *Batch, c: change.IdentityResult) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        if (!old.live()) return .{};
        var new = old;
        var applied = false;
        var state = try self.queueState(c.did, old);
        const current = if (state) |queued| queued.generation == c.generation else false;
        switch (c.outcome) {
            .resolved => |resolved| {
                applied = self.resolve(&new, c.observed_at, resolved.handle, c.pds);
                if (current) new.identity_due = 0;
            },
            .retry => |retry| {
                if (c.pds.len > 0) applied = self.resolve(&new, c.observed_at, "", c.pds);
                if (current) {
                    if (retry.attempts >= change.max_attempts) {
                        new.identity_due = 0;
                    } else {
                        new.identity_due = self.now + change.retryDelay(retry.delay_seconds);
                        state.?.last_error = retry.reason;
                    }
                }
            },
        }
        var outcome = try self.save(c.did, old, new, if (current and new.identity_due != 0) state else null);
        outcome.applied = applied or current;
        return outcome;
    }

    fn refreshEnqueue(self: *Batch, c: change.RefreshEnqueue) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        if (!old.live() or c.due == 0) return .{};
        var new = old;
        new.refresh_due = if (old.refresh_due == 0) c.due else @min(old.refresh_due, c.due);
        return self.save(c.did, old, new, null);
    }

    fn refreshComplete(self: *Batch, c: change.RefreshComplete) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        if (!old.live()) return .{};
        var new = old;
        new.profile_checked_at = self.now;
        if (settles(old, c.lease)) new.refresh_due = c.next_due orelse 0;
        return self.save(c.did, old, new, null);
    }

    fn settles(old: Actor, lease: ?u64) bool {
        return if (lease) |held| old.refresh_due == held else true;
    }

    fn refreshLease(self: *Batch, c: change.RefreshLease) !Outcome {
        const a = self.arena.allocator();
        const due = try read.timed(self.store.db(), a, .refresh, null, self.now, c.limit);
        for (due) |entry| {
            _ = self.scratch.reset(.retain_capacity);
            self.encoded = .empty;
            const old = try self.load(entry.did) orelse return error.InconsistentStore;
            if (!old.live() or old.refresh_due != entry.at) return error.InconsistentStore;
            var new = old;
            new.refresh_due = self.now + c.lease_seconds;
            _ = try self.save(entry.did, old, new, null);
            try self.refresh_jobs.append(a, .{
                .did = entry.did,
                .lease = new.refresh_due,
                .handle = try a.dupe(u8, old.handle),
                .pds = try a.dupe(u8, old.pds),
                .override = old.override,
            });
        }
        return .{ .applied = due.len > 0 };
    }

    fn refreshRetry(self: *Batch, c: change.RefreshRetry) !Outcome {
        const old = try self.load(c.did) orelse return .{};
        if (!old.live() or c.due == 0 or old.refresh_due != c.lease) return .{};
        var new = old;
        new.refresh_due = c.due;
        return self.save(c.did, old, new, null);
    }

    fn refreshWalk(self: *Batch, c: change.RefreshWalk) !Outcome {
        const engine_db = self.store.db();
        const a = self.arena.allocator();
        var buf: keys.Buf = undefined;
        var state = try read.walkState(engine_db, a, c.pool.cursor());
        if (state.position.len == 0) state.started_at = self.now;
        const prefix: keys.Prefix = switch (c.pool) {
            .quality => .quality,
            .rest => .actor,
        };
        const bounds = prefix.bounds();
        var start: std.ArrayList(u8) = .empty;
        try start.appendSlice(a, if (state.position.len == 0) &bounds[0] else state.position);
        if (state.position.len != 0) try start.append(a, 0);
        var found: Keys = .{ .arena = a };
        _ = try scan.all(a, engine_db, start.items, &bounds[1], .{ .limit = c.scan }, &found);

        var examined: usize = 0;
        var queued: usize = 0;
        for (found.keys.items) |key| {
            if (queued == c.limit) break;
            examined += 1;
            state.position = key;
            const did = switch (c.pool) {
                .quality => (try keys.parseQuality(key)).did,
                .rest => try keys.parseActor(key),
            };
            _ = self.scratch.reset(.retain_capacity);
            self.encoded = .empty;
            const old = try self.load(did) orelse return error.InconsistentStore;
            if (!old.live() or old.handle.len == 0 or old.refresh_due != 0) continue;
            if (c.pool == .rest and old.ranked()) continue;
            var new = old;
            new.refresh_due = self.now;
            _ = try self.save(did, old, new, null);
            queued += 1;
        }
        const tally = self.walk_tally.getPtr(c.pool);
        tally.examined += examined;
        tally.queued += queued;
        if (examined == found.keys.items.len and found.keys.items.len < c.scan) {
            state.passes += 1;
            state.last_pass_seconds = self.now - state.started_at;
            state.position = "";
            tally.wrapped += 1;
        }
        _ = self.scratch.reset(.retain_capacity);
        var value: std.ArrayList(u8) = .empty;
        try state.encode(self.tmp(), &value);
        try engine_db.put(keys.cursor(&buf, c.pool.cursor()), value.items);
        return .{ .applied = queued > 0 };
    }

    fn hiddenRefreshed(self: *Batch, did: []const u8) !Outcome {
        const old = try self.load(did) orelse return .{};
        return self.save(did, old, old, null);
    }

    fn tombstonesPruned(self: *Batch, c: change.TombstonesPruned) !Outcome {
        if (c.before == 0) return .{};
        const engine_db = self.store.db();
        const due = try read.timed(engine_db, self.arena.allocator(), .tombstone, null, c.before - 1, c.limit);
        var buf: keys.Buf = undefined;
        for (due) |entry| {
            _ = self.scratch.reset(.retain_capacity);
            const old = try self.load(entry.did) orelse return error.InconsistentStore;
            if (old.live() or old.deleted_at != entry.at) return error.InconsistentStore;
            for (try read.labels(engine_db, self.tmp(), entry.did)) |row| {
                try engine_db.del(try keys.label(&buf, entry.did, row.source, row.value, row.subject));
            }
            try engine_db.del(try keys.timed(&buf, .tombstone, entry.at, entry.did));
            try engine_db.del(try keys.actor(&buf, entry.did));
            try self.markChanged(entry.did);
        }
        return .{ .applied = due.len > 0, .changed = due.len };
    }
};

fn sameLabel(a: record.Label, b: record.Label) bool {
    return std.mem.eql(u8, a.uri, b.uri) and std.mem.eql(u8, a.value, b.value) and std.mem.eql(u8, a.source, b.source);
}

fn atLeast(a: []const u8, b: []const u8) bool {
    const x = time.parseMs(a) orelse return false;
    const y = time.parseMs(b) orelse return false;
    return x >= y;
}

/// A labeler's answer can hold a label's whole history, negations included.
/// The current entry per subject and value is the first listed, replaced by
/// each later one created at the same time or after it.
fn isCurrent(list: []const record.Label, index: usize) bool {
    var winner: ?usize = null;
    for (list, 0..) |other, at| {
        if (!sameLabel(other, list[index])) continue;
        if (winner == null or atLeast(other.created, list[winner.?].created)) winner = at;
    }
    return winner.? == index;
}
