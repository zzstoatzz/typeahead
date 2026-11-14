//! Today's SQL semantics, in memory, for the differential test. Each method
//! restates the statements one Worker endpoint runs: `handleIngest` and
//! `handleDelete`, the identity queue's enqueue, lease and settle, the PLC
//! report, the label stream, the cron's `refreshModeration` for one
//! selected row, and the admin endpoints that set and clear an override and
//! block and unblock a domain. It shares no code with the store. What a profile maps to
//! arrives already extracted (`Fetched`): that mapping has its own test
//! against the Worker's TypeScript.
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
    /// A profile fetch's moderation labels are the labeler's answer: they
    /// replace an earlier answer in the label range and never what the
    /// stream delivered, and the actor's list is projected from the range.
    /// In SQL the cron overwrites `actors.labels` with the response's list
    /// and takes `hidden` from it, whatever the stream has said since.
    enrichment_reconciles_labels: bool,
    /// A profile record from a PDS carries no labels, so the stored ones
    /// stay; in SQL the cron writes an empty list and recomputes `hidden`
    /// from it, which unhides a taken-down account.
    pds_record_keeps_labels: bool,
    /// A profile fetch never writes a handle. One from Bluesky or Blacksky
    /// that differs from the stored handle queues the account for identity
    /// resolution instead, unless it is queued already, was resolved within
    /// a day, or a hundred queue entries are due. In SQL the cron copies
    /// Bluesky's handle over the stored one, `handle.invalid` included.
    enrichment_verifies_handle: bool,

    pub const store: Divergence = .{
        .gone_keeps_record = true,
        .gone_drops_identity_work = true,
        .prune_forgets_labels = true,
        .hidden_recomputed_on_every_write = true,
        .label_expiry_pass = true,
        .slur_rule_left_to_index = true,
        .enrichment_reconciles_labels = true,
        .pds_record_keeps_labels = true,
        .enrichment_verifies_handle = true,
    };

    pub const production: Divergence = .{
        .gone_keeps_record = false,
        .gone_drops_identity_work = false,
        .prune_forgets_labels = false,
        .hidden_recomputed_on_every_write = false,
        .label_expiry_pass = false,
        .slur_rule_left_to_index = false,
        .enrichment_reconciles_labels = false,
        .pds_record_keeps_labels = false,
        .enrichment_verifies_handle = false,
    };
};

/// Differences on the store module's review list that this model does not
/// need a flag for, with why.
pub const unmodelled = [_][2][]const u8{
    .{ "change log records content changes only; updated_at moves only with content", "neither is part of an actor's visible state; the index bridge reads the current record" },
    .{ "label keys carry a subject byte", "same rows: the SQL key holds the subject URI, which differs between the two subjects" },
    .{ "which actors are refreshed, and when", "the store leases its refresh queue and walks two ranges; the cron and the flows select by SQL. The model is told which rows were fetched" },
    .{ "a failed takedown probe retries the DID; a refused batch is split", "neither changes what a completed fetch writes" },
    .{ "an account the appview does not know is stamped as checked", "the cron leaves it unstamped and the backfill flow stamps it; the check time is not compared" },
    .{ "unknown label fields are dropped", "ver and sig are not served by the SQL projection either" },
    .{ "domain blocks apply at once to policy and by a sweep to stored actors", "the SQL hides or restores every stored actor in the request that changes the block, in the background; the differential run lets the owner's sweep finish before it compares, and the owner's tests cover a sweep a restart interrupts" },
    .{ "an override is held for an account the store has never seen", "same in SQL: the mod_overrides row exists without an actors row, and the account is hidden or shown by it once it is written" },
    .{ "an indexing request is an #identity event", "in SQL /request-indexing resolves the account and writes its row itself; here the request queues the account for this role's own resolution, as the relay's #identity does" },
    .{ "queue due times live in the actor record", "layout only" },
    .{ "reactivation re-requests refresh but not identity", "the role sends identity_enqueue with every active #account, as the Worker enqueues it" },
    .{ "refresh is requested only for actors with a handle", "same in SQL: both of the cron's selectors and the backfill's require a handle" },
    .{ "zero counts and score keep the old value", "same in SQL: COALESCE(NULLIF(?, 0), column)" },
    .{ "labeler and PLC cursors never move backwards", "same in SQL: MAX() for the labeler, a string comparison for PLC" },
    .{ "handles over 253 bytes are rejected", "same bound as the Worker's schema, and the resolver's handle syntax check stops them first" },
};

pub const mod_source = "did:plc:ar7c4by46qjdydhdevvrndac";
const hiding = [_][]const u8{ "!hide", "!takedown", "!suspend", "spam" };
const lease_seconds = 300;
const max_attempts = 12;
const tombstone_seconds = 7 * 86_400;
const handle_settle_seconds = 86_400;
const handle_backlog_limit = 100;

/// The SQL's `handle = ?domain OR handle LIKE '%.' || ?domain`, without case.
fn underDomain(handle: []const u8, domain: []const u8) bool {
    if (handle.len < domain.len or !std.ascii.eqlIgnoreCase(handle[handle.len - domain.len ..], domain)) return false;
    return handle.len == domain.len or handle[handle.len - domain.len - 1] == '.';
}

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
pub const Source = enum { none, bsky, blacksky, pds };

/// A label as a profile response carries it.
pub const ProfileLabel = struct {
    src: []const u8,
    uri: []const u8,
    val: []const u8,
    cts: []const u8,
    cid: []const u8 = "",
    exp: []const u8 = "",
    neg: bool = false,
};

/// `extractProfileFields`' output for one response.
pub const Fetched = struct {
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar: []const u8 = "",
    description: []const u8 = "",
    banner: []const u8 = "",
    created_at: []const u8 = "",
    associated: []const u8 = "{}",
    followers: u64 = 0,
    follows: u64 = 0,
    posts: u64 = 0,
    score: f64 = 0,
    labels: []const ProfileLabel = &.{},
};

/// What the cron's requests would be answered with.
pub const Appview = struct {
    context: *anyopaque,
    /// `getProfiles`.
    bsky: *const fn (context: *anyopaque, did: []const u8) ?Fetched,
    /// `getProfile` refuses the DID with `AccountTakedown`.
    taken_down: *const fn (context: *anyopaque, did: []const u8) bool,
    blacksky: *const fn (context: *anyopaque, did: []const u8) ?Fetched,
    /// The profile record on `pds`.
    record: *const fn (context: *anyopaque, did: []const u8, pds: []const u8) ?Fetched,
};

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
    /// The rest of `actors.labels`.
    other_labels: []const ProfileLabel = &.{},
    created_at: []const u8 = "",
    associated: []const u8 = "",
    followers: u64 = 0,
    follows: u64 = 0,
    posts: u64 = 0,
    score: f64 = 0,
    source: Source = .none,
};

pub const Override = enum { show, hide };

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
    /// `mod_overrides` and `blocked_domains`.
    overrides: std.StringArrayHashMapUnmanaged(Override) = .empty,
    blocked: std.StringArrayHashMapUnmanaged(void) = .empty,
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

    fn labelsHide(self: *Model, list: []const Label) bool {
        for (list) |label| {
            if (label.neg) continue;
            if (label.exp.len > 0) {
                if (self.parse_ms(label.exp)) |at| if (at <= @as(i64, @intCast(self.now)) * 1000) continue;
            }
            for (hiding) |value| if (std.mem.eql(u8, label.val, value)) return true;
        }
        return false;
    }

    /// `isDomainBlocked`: the handle is a blocked domain or sits under one.
    pub fn domainBlocked(self: *Model, handle: []const u8) bool {
        for (self.blocked.keys()) |domain| if (underDomain(handle, domain)) return true;
        return false;
    }

    /// `resolveHidden` as of now: what the engine decides at query time.
    /// Later wins: a hiding label, a slur handle, a blocked domain, then the
    /// account's override.
    pub fn resolveHidden(self: *Model, did: []const u8, row: *const Row) bool {
        var hidden = self.labelsHide(row.labels);
        if (!self.flags.slur_rule_left_to_index) {
            if (self.flagged) |flagged| if (row.handle.len > 0 and flagged(row.handle)) {
                hidden = true;
            };
        }
        if (self.domainBlocked(row.handle)) hidden = true;
        if (self.overrides.get(did)) |override| hidden = override == .hide;
        return hidden;
    }

    fn wrote(self: *Model, did: []const u8, row: *Row) void {
        if (self.flags.hidden_recomputed_on_every_write) row.hidden = self.resolveHidden(did, row);
    }

    // -- handlers/admin.ts, as turso-policy-mirror.ts writes it --

    /// `POST /admin/mod-override`.
    pub fn overrideSet(self: *Model, did: []const u8, action: Override) void {
        self.overrides.put(self.arena, self.own(did), action) catch @panic("out of memory");
        if (self.live(did)) |row| row.hidden = action == .hide;
    }

    /// `DELETE /admin/mod-override`: the flag is recomputed from what is left.
    pub fn overrideCleared(self: *Model, did: []const u8) void {
        _ = self.overrides.orderedRemove(did);
        if (self.live(did)) |row| row.hidden = self.resolveHidden(did, row);
    }

    /// `POST /admin/domain-block` and its backfill: every stored actor under
    /// the domain is hidden, bar the ones an override shows.
    pub fn domainBlockedSet(self: *Model, domain: []const u8) void {
        self.blocked.put(self.arena, self.own(domain), {}) catch @panic("out of memory");
        for (self.actors.keys(), self.actors.values()) |did, *row| {
            if (row.status != .live or !underDomain(row.handle, domain)) continue;
            if (self.overrides.get(did) != .show) row.hidden = true;
        }
    }

    /// `DELETE /admin/domain-block` and its recompute, for every stored
    /// actor under the domain, from the sources that remain.
    pub fn domainUnblocked(self: *Model, domain: []const u8) void {
        _ = self.blocked.orderedRemove(domain);
        for (self.actors.keys(), self.actors.values()) |did, *row| {
            if (row.status == .live and underDomain(row.handle, domain)) row.hidden = self.resolveHidden(did, row);
        }
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
        self.wrote(did, row);
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
        self.wrote(did, row);
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
            row.hidden = self.resolveHidden(did, row);
        }
    }

    // -- cron.ts refreshModeration, for one selected row --

    pub fn refresh(self: *Model, did: []const u8, appview: Appview) void {
        const row = self.live(did) orelse return;
        if (appview.bsky(appview.context, did)) |f| return self.enriched(did, row, f, .bsky);
        // An account Bluesky does not return: a `hide` override ends it
        // there, a `show` override is taken as a takedown without asking.
        const override = self.overrides.get(did);
        if (row.pds.len == 0 or override == .hide) return;
        if (override != .show and !appview.taken_down(appview.context, did)) return;
        if (appview.blacksky(appview.context, did)) |f| return self.enriched(did, row, f, .blacksky);
        if (appview.record(appview.context, did, row.pds)) |f| self.enriched(did, row, f, .pds);
    }

    fn enriched(self: *Model, did: []const u8, row: *Row, f: Fetched, source: Source) void {
        if (self.flags.enrichment_verifies_handle) {
            self.verifyHandle(did, row, f.handle);
        } else if (source == .bsky) row.handle = self.keep(row.handle, f.handle);
        row.display_name = self.keep(row.display_name, f.display_name);
        row.avatar = self.keep(row.avatar, f.avatar);
        row.description = self.keep(row.description, f.description);
        row.banner = self.keep(row.banner, f.banner);
        row.created_at = self.keep(row.created_at, f.created_at);
        if (f.associated.len > 0 and !std.mem.eql(u8, f.associated, "{}")) row.associated = self.own(f.associated);
        if (f.followers != 0) row.followers = f.followers;
        if (f.follows != 0) row.follows = f.follows;
        if (f.posts != 0) row.posts = f.posts;
        if (f.score != 0) row.score = f.score;
        row.source = source;
        if (source == .pds and self.flags.pds_record_keeps_labels) return self.wrote(did, row);

        var others: std.ArrayList(ProfileLabel) = .empty;
        var moderated: std.ArrayList(Label) = .empty;
        for (f.labels) |label| {
            if (std.mem.eql(u8, label.src, mod_source)) {
                moderated.append(self.arena, .{ .uri = self.own(label.uri), .val = self.own(label.val), .cts = self.own(label.cts), .cid = self.own(label.cid), .exp = self.own(label.exp), .neg = label.neg }) catch @panic("out of memory");
            } else others.append(self.arena, .{ .src = self.own(label.src), .uri = self.own(label.uri), .val = self.own(label.val), .cts = self.own(label.cts), .cid = self.own(label.cid), .exp = self.own(label.exp), .neg = label.neg }) catch @panic("out of memory");
        }
        row.other_labels = others.items;
        if (self.flags.enrichment_reconciles_labels) {
            self.reconcile(did, moderated.items);
            self.project(did);
            return self.wrote(did, row);
        }
        row.labels = moderated.items;
        row.hidden = self.resolveHidden(did, row);
    }

    /// The store role's answer to a handle a profile source reports.
    fn verifyHandle(self: *Model, did: []const u8, row: *const Row, reported: []const u8) void {
        if (reported.len == 0 or reported.len > 253 or std.ascii.eqlIgnoreCase(reported, row.handle)) return;
        if (self.queue.contains(did)) return;
        if (row.identity_resolved_at + handle_settle_seconds > self.now) return;
        var due: usize = 0;
        for (self.queue.values()) |queued| due += @intFromBool(queued.due_at <= self.now);
        if (due >= handle_backlog_limit) return;
        self.queue.put(self.arena, self.own(did), .{ .due_at = self.now, .reason = "identity" }) catch @panic("out of memory");
    }

    fn laterOrSame(self: *Model, a: []const u8, b: []const u8) bool {
        const x = self.parse_ms(a) orelse return false;
        const y = self.parse_ms(b) orelse return false;
        return x >= y;
    }

    /// The store module's rule for a labeler's answer: drop what an earlier
    /// answer stored, then store the current entry of each subject and
    /// value unless the stream has delivered one.
    fn reconcile(self: *Model, did: []const u8, answer: []const Label) void {
        const slot = self.account_labels.getOrPut(self.arena, did) catch @panic("out of memory");
        if (!slot.found_existing) {
            slot.key_ptr.* = self.own(did);
            slot.value_ptr.* = .empty;
        }
        const rows = slot.value_ptr;
        var index: usize = 0;
        while (index < rows.items.len) {
            if (rows.items[index].seq == 0) _ = rows.orderedRemove(index) else index += 1;
        }
        const profile_uri = std.fmt.allocPrint(self.arena, "at://{s}/app.bsky.actor.profile/self", .{did}) catch @panic("out of memory");
        for (answer, 0..) |label, at| {
            if (!std.mem.eql(u8, label.uri, did) and !std.mem.eql(u8, label.uri, profile_uri)) continue;
            if (label.val.len == 0 or label.val.len > 128) continue;
            var winner: usize = at;
            var first = true;
            for (answer, 0..) |other, position| {
                if (!std.mem.eql(u8, other.uri, label.uri) or !std.mem.eql(u8, other.val, label.val)) continue;
                if (first or self.laterOrSame(other.cts, answer[winner].cts)) winner = position;
                first = false;
            }
            if (winner != at) continue;
            const streamed = for (rows.items) |existing| {
                if (std.mem.eql(u8, existing.uri, label.uri) and std.mem.eql(u8, existing.val, label.val)) break true;
            } else false;
            if (!streamed) rows.append(self.arena, label) catch @panic("out of memory");
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
            if (lapsed) row.hidden = self.resolveHidden(did, row);
        }
    }

    pub fn pending(self: *const Model, did: []const u8) bool {
        return self.queue.contains(did);
    }
};
