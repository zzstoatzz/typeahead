//! Today's SQL semantics, in memory, for the differential test. Each method
//! restates the statements one Worker endpoint runs: `handleIngest` and
//! `handleDelete`, the identity queue's enqueue, lease and settle, the PLC
//! report, and the label stream. It shares no code with the store.
//!
//! Where the store module deliberately differs from the SQL, the difference
//! is a named flag in `Divergence`. With every flag off this is production;
//! with every flag on it must agree with the store role exactly.

const std = @import("std");
const handle_policy = @import("handle_policy.zig");
const Allocator = std.mem.Allocator;

pub const Divergence = struct {
    /// A deleted or deactivated account keeps its record until its tombstone
    /// is pruned, ignores everything but reactivation, and comes back with
    /// its fields; in SQL the row is deleted and any later event makes a
    /// bare one.
    gone_keeps_record: bool,
    /// Deletion drops the account's identity queue entry; in SQL the queue
    /// row outlives the actor and is leased and resolved for nothing.
    gone_drops_identity_work: bool,
    /// Pruning a tombstone removes the account's label rows; in SQL
    /// `account_labels` rows are never removed, and return on the next label.
    prune_forgets_labels: bool,
    /// The stored `hidden` flag is recomputed on every write to the actor;
    /// in SQL only a label batch (and the cron) recomputes it.
    hidden_recomputed_on_every_write: bool,
    /// A pass over the label range recomputes `hidden` once a label has
    /// lapsed; SQL has no such pass, and the stored flag stays set.
    label_expiry_pass: bool,
    /// The store role supplies no slur predicate, so a slur handle does not
    /// set the stored `hidden` flag; the index applies the rule at query time.
    slur_rule_left_to_index: bool,

    pub const store: Divergence = .{
        .gone_keeps_record = true,
        .gone_drops_identity_work = true,
        .prune_forgets_labels = true,
        .hidden_recomputed_on_every_write = true,
        .label_expiry_pass = true,
        .slur_rule_left_to_index = true,
    };

    pub const production: Divergence = .{
        .gone_keeps_record = false,
        .gone_drops_identity_work = false,
        .prune_forgets_labels = false,
        .hidden_recomputed_on_every_write = false,
        .label_expiry_pass = false,
        .slur_rule_left_to_index = false,
    };
};

/// Differences on the store module's review list that this model does not
/// need a flag for, with why.
pub const unmodelled = [_][2][]const u8{
    .{ "change log records content changes only; updated_at moves only with content", "neither is part of an actor's visible state; the index bridge reads the current record" },
    .{ "label keys carry a subject byte", "same rows: the SQL key holds the subject URI, which differs between the two subjects" },
    .{ "moderation labels have one writer; labels = null keeps labels", "enrichment is not part of this role" },
    .{ "unknown label fields are dropped", "ver and sig are not served by the SQL projection either" },
    .{ "domain blocks apply at once to policy and by a sweep to stored actors", "no policy change reaches this role yet; the owner's sweep has its own test" },
    .{ "queue due times live in the actor record", "layout only" },
    .{ "reactivation re-requests refresh but not identity", "the role sends identity_enqueue with every active #account, as the Worker enqueues it" },
    .{ "refresh is requested only for actors with a handle", "the refresh queue feeds enrichment, which is not part of this role" },
    .{ "zero counts and score keep the old value", "enrichment is not part of this role" },
    .{ "labeler and PLC cursors never move backwards", "same in SQL: MAX() for the labeler, a string comparison for PLC" },
    .{ "handles over 253 bytes are rejected", "same bound as the Worker's schema, and the resolver's handle syntax check stops them first" },
};

pub const mod_source = "did:plc:ar7c4by46qjdydhdevvrndac";
const hiding = [_][]const u8{ "!hide", "!takedown", "!suspend", "spam" };
const lease_seconds = 300;
const max_attempts = 12;
const tombstone_seconds = 7 * 86_400;

pub const Label = struct {
    uri: []const u8,
    val: []const u8,
    cts: []const u8,
    cid: []const u8 = "",
    exp: []const u8 = "",
    neg: bool = false,
    seq: u64 = 0,
};

pub const Status = enum { live, deactivated, deleted };

pub const Row = struct {
    status: Status = .live,
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar: []const u8 = "",
    pds: []const u8 = "",
    description: []const u8 = "",
    banner: []const u8 = "",
    hidden: bool = false,
    identity_resolved_at: u64 = 0,
    last_activity_at: u64 = 0,
    deleted_at: u64 = 0,
    /// The moderation service's share of `actors.labels`.
    labels: []const Label = &.{},
};

pub const Queued = struct {
    gen: u64 = 1,
    due_at: u64,
    attempts: u64 = 0,
    reason: []const u8,
    last_error: []const u8 = "",
};

pub const Event = union(enum) {
    /// A wanted record that is not a profile.
    sighting: []const u8,
    profile: struct { did: []const u8, display_name: []const u8 = "", avatar: []const u8 = "", description: []const u8 = "", banner: []const u8 = "" },
    identity: []const u8,
    active: []const u8,
    inactive: struct { did: []const u8, deleted: bool },
};

pub const Job = struct { did: []const u8, gen: u64, attempts: u64 };

pub const Result = struct {
    did: []const u8,
    gen: u64,
    observed_at: u64,
    pds: []const u8 = "",
    outcome: union(enum) {
        resolved: struct { handle: []const u8 = "", verified: bool = false },
        retry: struct { attempts: u64, delay_seconds: u64, reason: []const u8 },
    },
};

pub const WireLabel = struct {
    did: []const u8,
    uri: []const u8,
    val: []const u8,
    cts: []const u8,
    cid: []const u8 = "",
    exp: []const u8 = "",
    neg: bool = false,
    seq: u64,
};

pub const Model = struct {
    arena: Allocator,
    flags: Divergence,
    now: u64 = 0,
    flagged: ?*const fn (handle: []const u8) bool = null,
    actors: std.StringArrayHashMapUnmanaged(Row) = .empty,
    queue: std.StringArrayHashMapUnmanaged(Queued) = .empty,
    account_labels: std.StringArrayHashMapUnmanaged(std.ArrayList(Label)) = .empty,
    tombstones: std.StringArrayHashMapUnmanaged(u64) = .empty,
    label_cursor: ?u64 = null,
    plc_cursor: ?[]const u8 = null,
    parse_ms: *const fn (text: []const u8) ?i64,

    fn own(self: *Model, text: []const u8) []const u8 {
        return self.arena.dupe(u8, text) catch @panic("out of memory");
    }

    fn keep(self: *Model, stored: []const u8, incoming: []const u8) []const u8 {
        return if (incoming.len > 0) self.own(incoming) else stored;
    }

    /// The actor as SQL sees it: a row, when it is not a kept tombstone.
    fn live(self: *Model, did: []const u8) ?*Row {
        const row = self.actors.getPtr(did) orelse return null;
        return if (row.status == .live) row else null;
    }

    fn gone(self: *Model, did: []const u8) bool {
        const row = self.actors.getPtr(did) orelse return false;
        return row.status != .live;
    }

    // -- moderation.ts --

    fn labelsHide(self: *Model, labels: []const Label) bool {
        for (labels) |label| {
            if (label.neg) continue;
            if (label.exp.len > 0) {
                if (self.parse_ms(label.exp)) |at| if (at <= @as(i64, @intCast(self.now)) * 1000) continue;
            }
            for (hiding) |value| if (std.mem.eql(u8, label.val, value)) return true;
        }
        return false;
    }

    fn resolveHidden(self: *Model, row: *const Row) bool {
        var hidden = self.labelsHide(row.labels);
        if (!self.flags.slur_rule_left_to_index) {
            if (self.flagged) |flagged| if (row.handle.len > 0 and flagged(row.handle)) {
                hidden = true;
            };
        }
        return hidden;
    }

    fn wrote(self: *Model, row: *Row) void {
        if (self.flags.hidden_recomputed_on_every_write) row.hidden = self.resolveHidden(row);
    }

    // -- identity-queue.ts enqueue rules --

    fn enqueueIdentity(self: *Model, did: []const u8, reason: []const u8) void {
        if (self.queue.getPtr(did)) |row| {
            row.gen += 1;
            row.due_at = @min(row.due_at, self.now);
            row.attempts = 0;
            row.reason = reason;
        } else self.queue.put(self.arena, self.own(did), .{ .due_at = self.now, .reason = reason }) catch @panic("out of memory");
    }

    fn enqueueDiscovered(self: *Model, did: []const u8) void {
        if (self.queue.contains(did)) return;
        self.queue.put(self.arena, self.own(did), .{ .due_at = self.now, .reason = "discovered" }) catch @panic("out of memory");
    }

    // -- handlers/ingest.ts --

    pub fn ingest(self: *Model, event: Event) void {
        const did = switch (event) {
            .sighting, .identity, .active => |d| d,
            .profile => |p| p.did,
            .inactive => |i| return self.delete(i.did, i.deleted),
        };
        if (self.flags.gone_keeps_record and self.gone(did)) {
            if (event != .active) return;
            const row = self.actors.getPtr(did).?;
            row.status = .live;
            row.deleted_at = 0;
        }
        const probe = self.actors.getOrPut(self.arena, did) catch @panic("out of memory");
        if (!probe.found_existing) {
            probe.key_ptr.* = self.own(did);
            probe.value_ptr.* = .{};
            self.enqueueDiscovered(did);
        }
        const row = probe.value_ptr;
        switch (event) {
            .identity => self.enqueueIdentity(did, "identity"),
            .active => self.enqueueIdentity(did, "account"),
            .profile => |p| {
                row.display_name = self.keep(row.display_name, p.display_name);
                row.avatar = self.keep(row.avatar, p.avatar);
                row.description = self.keep(row.description, p.description);
                row.banner = self.keep(row.banner, p.banner);
            },
            .sighting, .inactive => {},
        }
        if (row.last_activity_at + 3600 < self.now) row.last_activity_at = self.now;
        self.wrote(row);
    }

    // -- handlers/admin.ts handleDelete --

    fn delete(self: *Model, did: []const u8, deleted: bool) void {
        const row = self.actors.getPtr(did) orelse {
            if (!self.flags.gone_keeps_record) self.tombstones.put(self.arena, self.own(did), self.now) catch @panic("out of memory");
            return;
        };
        if (self.flags.gone_keeps_record) {
            row.status = if (deleted) .deleted else .deactivated;
            row.deleted_at = self.now;
        } else {
            _ = self.actors.orderedRemove(did);
            self.tombstones.put(self.arena, self.own(did), self.now) catch @panic("out of memory");
        }
        if (self.flags.gone_drops_identity_work) _ = self.queue.orderedRemove(did);
    }

    /// cron.ts drops tombstones after seven days. With a kept record that is
    /// also when the record goes.
    pub fn prune(self: *Model) void {
        if (self.now <= tombstone_seconds) return;
        const before = self.now - tombstone_seconds;
        var index: usize = 0;
        while (index < self.tombstones.count()) {
            if (self.tombstones.values()[index] < before) self.tombstones.orderedRemoveAt(index) else index += 1;
        }
        if (!self.flags.gone_keeps_record) return;
        index = 0;
        while (index < self.actors.count()) {
            const row = self.actors.values()[index];
            if (row.status == .live or row.deleted_at >= before) {
                index += 1;
                continue;
            }
            const did = self.actors.keys()[index];
            if (self.flags.prune_forgets_labels) _ = self.account_labels.orderedRemove(did);
            self.actors.orderedRemoveAt(index);
        }
    }

    // -- identity-queue.ts lease and settle --

    fn dueBefore(self: *Model, a: usize, b: usize) bool {
        const x = self.queue.values()[a];
        const y = self.queue.values()[b];
        return if (x.due_at != y.due_at) x.due_at < y.due_at else std.mem.order(u8, self.queue.keys()[a], self.queue.keys()[b]) == .lt;
    }

    pub fn lease(self: *Model, limit: usize) []const Job {
        var due: std.ArrayList(usize) = .empty;
        for (self.queue.values(), 0..) |row, index| {
            if (row.due_at <= self.now) due.append(self.arena, index) catch @panic("out of memory");
        }
        std.mem.sort(usize, due.items, self, dueBefore);
        const count = @min(limit, due.items.len);
        const jobs = self.arena.alloc(Job, count) catch @panic("out of memory");
        for (jobs, due.items[0..count]) |*job, index| {
            const row = &self.queue.values()[index];
            row.due_at = self.now + lease_seconds;
            row.attempts += 1;
            job.* = .{ .did = self.queue.keys()[index], .gen = row.gen, .attempts = row.attempts };
        }
        return jobs;
    }

    fn applyIdentity(self: *Model, did: []const u8, observed_at: u64, handle: []const u8, pds: []const u8) void {
        const row = self.live(did) orelse return;
        if (row.identity_resolved_at > observed_at) return;
        row.handle = self.keep(row.handle, handle);
        row.pds = self.keep(row.pds, pds);
        row.identity_resolved_at = observed_at;
        self.wrote(row);
    }

    fn completeQueued(self: *Model, did: []const u8, gen: u64) void {
        const row = self.queue.getPtr(did) orelse return;
        if (row.gen == gen) _ = self.queue.orderedRemove(did);
    }

    pub fn settle(self: *Model, result: Result) void {
        switch (result.outcome) {
            .resolved => |resolved| {
                self.applyIdentity(result.did, result.observed_at, handle_policy.admitted(resolved.handle, resolved.verified), result.pds);
                self.completeQueued(result.did, result.gen);
            },
            .retry => |retry| {
                if (result.pds.len > 0) self.applyIdentity(result.did, result.observed_at, "", result.pds);
                if (retry.attempts >= max_attempts) return self.completeQueued(result.did, result.gen);
                const row = self.queue.getPtr(result.did) orelse return;
                if (row.gen != result.gen) return;
                row.due_at = self.now + @min(86_400, @max(30, retry.delay_seconds));
                row.last_error = self.own(retry.reason);
            },
        }
    }

    // -- identity-queue.ts handlePlcOperations --

    pub fn plc(self: *Model, dids: []const []const u8, cursor: []const u8) void {
        for (dids, 0..) |did, index| {
            const repeat = for (dids[0..index]) |earlier| {
                if (std.mem.eql(u8, earlier, did)) break true;
            } else false;
            if (repeat or self.live(did) == null) continue;
            self.enqueueIdentity(did, "plc");
        }
        if (self.plc_cursor == null or std.mem.order(u8, cursor, self.plc_cursor.?) == .gt) self.plc_cursor = self.own(cursor);
    }

    // -- account-labels.ts applyStream --

    fn project(self: *Model, did: []const u8) void {
        const row = self.live(did) orelse return;
        var list: std.ArrayList(Label) = .empty;
        if (self.account_labels.get(did)) |rows| {
            for (rows.items) |label| {
                if (!label.neg) list.append(self.arena, label) catch @panic("out of memory");
            }
        }
        row.labels = list.items;
    }

    pub fn labels(self: *Model, wires: []const WireLabel, cursor: u64) void {
        var touched: std.StringArrayHashMapUnmanaged(void) = .empty;
        for (wires) |wire| {
            if (self.live(wire.did) == null) continue;
            touched.put(self.arena, wire.did, {}) catch @panic("out of memory");
            const slot = self.account_labels.getOrPut(self.arena, wire.did) catch @panic("out of memory");
            if (!slot.found_existing) {
                slot.key_ptr.* = self.own(wire.did);
                slot.value_ptr.* = .empty;
            }
            const incoming: Label = .{ .uri = self.own(wire.uri), .val = self.own(wire.val), .cts = self.own(wire.cts), .cid = self.own(wire.cid), .exp = self.own(wire.exp), .neg = wire.neg, .seq = wire.seq };
            const existing = for (slot.value_ptr.items) |*label| {
                if (std.mem.eql(u8, label.uri, wire.uri) and std.mem.eql(u8, label.val, wire.val)) break label;
            } else null;
            if (existing) |label| {
                if (wire.seq >= label.seq) label.* = incoming;
            } else slot.value_ptr.append(self.arena, incoming) catch @panic("out of memory");
        }
        for (touched.keys()) |did| self.project(did);
        self.label_cursor = @max(self.label_cursor orelse 0, cursor);
        for (touched.keys()) |did| {
            const row = self.live(did).?;
            row.hidden = self.resolveHidden(row);
        }
    }

    /// The store role's pass over lapsed labels.
    pub fn expiryPass(self: *Model) void {
        if (!self.flags.label_expiry_pass) return;
        for (self.account_labels.keys(), self.account_labels.values()) |did, rows| {
            const row = self.actors.getPtr(did) orelse continue;
            const lapsed = for (rows.items) |label| {
                if (label.neg or label.exp.len == 0) continue;
                if (self.parse_ms(label.exp)) |at| if (at <= @as(i64, @intCast(self.now)) * 1000) break true;
            } else false;
            if (lapsed) row.hidden = self.resolveHidden(row);
        }
    }

    pub fn pending(self: *const Model, did: []const u8) bool {
        return self.queue.contains(did);
    }
};
