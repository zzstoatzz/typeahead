//! An in-memory statement of what the store should hold after a sequence of
//! changes: plain maps and lists, no keys, no encodings. The tests apply the
//! same changes to this and to a real store and compare.

const std = @import("std");
const change = @import("../change.zig");
const keys = @import("../keys.zig");
const moderation = @import("../moderation.zig");
const record = @import("../record.zig");
const time = @import("../time.zig");
const Actor = record.Actor;
const Change = change.Change;

pub const Row = struct {
    source: []const u8,
    value: []const u8,
    subject: keys.Subject,
    stored: record.StoredLabel,

    fn before(_: void, a: Row, b: Row) bool {
        return switch (std.mem.order(u8, a.source, b.source)) {
            .lt => true,
            .gt => false,
            .eq => switch (std.mem.order(u8, a.value, b.value)) {
                .lt => true,
                .gt => false,
                .eq => @backingInt(a.subject) < @backingInt(b.subject),
            },
        };
    }
};

pub const Model = struct {
    arena: std.heap.ArenaAllocator,
    source: []const u8 = moderation.bsky_moderation,
    flagged: ?*const fn ([]const u8) bool = null,
    actors: std.StringArrayHashMapUnmanaged(Actor) = .empty,
    queue: std.StringHashMapUnmanaged(record.QueueState) = .empty,
    labels: std.StringArrayHashMapUnmanaged(std.ArrayList(Row)) = .empty,
    overrides: std.StringArrayHashMapUnmanaged(record.PolicyEntry) = .empty,
    domains: std.StringArrayHashMapUnmanaged(record.PolicyEntry) = .empty,
    /// `log.items[i]` is the DID of change sequence `i + 1`.
    log: std.ArrayList([]const u8) = .empty,
    floor: u64 = 0,
    relay: ?u64 = null,
    labeler: ?u64 = null,
    plc: ?[]const u8 = null,
    bridge: ?u64 = null,
    control_policy: ?u64 = null,
    control_requests: ?u64 = null,
    walks: std.EnumArray(change.WalkPool, record.WalkState) = .initFill(.{}),
    pending: std.ArrayList([]const u8) = .empty,
    now: u64 = 0,

    pub fn init(gpa: std.mem.Allocator) Model {
        return .{ .arena = .init(gpa) };
    }

    pub fn deinit(self: *Model) void {
        self.arena.deinit();
    }

    fn mem(self: *Model) std.mem.Allocator {
        return self.arena.allocator();
    }

    fn own(self: *Model, text: []const u8) []const u8 {
        return self.mem().dupe(u8, text) catch @panic("out of memory");
    }

    fn ownLabels(self: *Model, list: []const record.Label) []const record.Label {
        const out = self.mem().alloc(record.Label, list.len) catch @panic("out of memory");
        for (out, list) |*slot, one| slot.* = .{
            .source = self.own(one.source),
            .uri = self.own(one.uri),
            .value = self.own(one.value),
            .created = self.own(one.created),
            .cid = self.own(one.cid),
            .expires = self.own(one.expires),
            .negated = one.negated,
            .version = one.version,
        };
        return out;
    }

    pub fn begin(self: *Model, now: u64) void {
        self.now = now;
        self.pending.clearRetainingCapacity();
    }

    pub fn commit(self: *Model) void {
        self.log.appendSlice(self.mem(), self.pending.items) catch @panic("out of memory");
        self.pending.clearRetainingCapacity();
    }

    pub fn nextChange(self: *const Model) u64 {
        return self.log.items.len + 1;
    }

    fn noteChanged(self: *Model, did: []const u8) void {
        for (self.pending.items) |seen| if (std.mem.eql(u8, seen, did)) return;
        self.pending.append(self.mem(), self.own(did)) catch @panic("out of memory");
    }

    fn domainBlocked(self: *const Model, handle: []const u8) bool {
        for (self.domains.keys()) |domain| if (moderation.matchesDomain(handle, domain)) return true;
        return false;
    }

    fn hiddenFor(self: *const Model, actor: Actor) bool {
        if (actor.override == .show) return false;
        if (actor.override == .hide) return true;
        if (moderation.labelsHide(actor.labels, self.source, self.now)) return true;
        if (actor.handle.len > 0) if (self.flagged) |flagged| if (flagged(actor.handle)) return true;
        return self.domainBlocked(actor.handle);
    }

    /// Store `wanted` as the actor's new state. Returns whether its content
    /// changed.
    fn put(self: *Model, did: []const u8, wanted: Actor) bool {
        var new = wanted;
        if (new.status == .live) {
            new.deleted_at = 0;
        } else {
            new.identity_due = 0;
            new.refresh_due = 0;
            _ = self.queue.remove(did);
        }
        new.hidden = self.hiddenFor(new);
        const old = self.actors.get(did);
        const changed = if (old) |o| !o.contentEql(new) else true;
        if (changed) {
            new.updated_at = @max(self.now, if (old) |o| o.updated_at else 0);
            self.noteChanged(did);
        }
        inline for (.{ "handle", "display_name", "avatar", "description", "banner", "pds", "created_at", "associated" }) |name| {
            @field(new, name) = self.own(@field(new, name));
        }
        new.labels = self.ownLabels(new.labels);
        self.actors.put(self.mem(), self.own(did), new) catch @panic("out of memory");
        return changed;
    }

    fn fresh(self: *const Model, did: []const u8) Actor {
        return .{ .override = if (self.overrides.get(did)) |policy| policy.action else .none };
    }

    fn stamp(self: *const Model, actor: *Actor) void {
        if (actor.last_activity_at + 3600 < self.now) actor.last_activity_at = self.now;
    }

    fn wantRefresh(self: *const Model, actor: *Actor) void {
        if (actor.handle.len == 0) return;
        actor.refresh_due = if (actor.refresh_due == 0) self.now else @min(actor.refresh_due, self.now);
    }

    fn discover(self: *Model, did: []const u8, actor: *Actor) void {
        actor.identity_due = self.now;
        self.queue.put(self.mem(), self.own(did), .{ .reason = .discovered, .enqueued_at = self.now }) catch @panic("out of memory");
    }

    /// Whether a handle an enrichment reports sends the actor back to
    /// identity resolution.
    fn shouldVerify(self: *const Model, actor: Actor, c: change.Enrichment) bool {
        const check = c.handle_check orelse return false;
        if (c.handle.len == 0 or std.ascii.eqlIgnoreCase(c.handle, actor.handle)) return false;
        if (actor.identity_due != 0) return false;
        if (actor.identity_resolved_at +| check.settle_seconds > self.now) return false;
        var due: usize = 0;
        for (self.actors.values()) |other| due += @intFromBool(other.identity_due != 0 and other.identity_due <= self.now);
        return due < check.backlog_limit;
    }

    fn orKeep(stored: []const u8, incoming: []const u8) []const u8 {
        return if (incoming.len > 0) incoming else stored;
    }

    fn resolved(self: *const Model, actor: *Actor, observed_at: u64, handle: []const u8, pds: []const u8) bool {
        if (actor.identity_resolved_at > observed_at) return false;
        actor.handle = orKeep(actor.handle, handle);
        actor.pds = orKeep(actor.pds, pds);
        actor.identity_resolved_at = observed_at;
        self.wantRefresh(actor);
        return true;
    }

    fn rows(self: *Model, did: []const u8) *std.ArrayList(Row) {
        const slot = self.labels.getOrPut(self.mem(), did) catch @panic("out of memory");
        if (!slot.found_existing) {
            slot.key_ptr.* = self.own(did);
            slot.value_ptr.* = .empty;
        }
        return slot.value_ptr;
    }

    fn setRow(self: *Model, did: []const u8, row: Row) void {
        const list = self.rows(did);
        const owned: Row = .{
            .source = self.own(row.source),
            .value = self.own(row.value),
            .subject = row.subject,
            .stored = .{
                .sequence = row.stored.sequence,
                .negated = row.stored.negated,
                .created = self.own(row.stored.created),
                .cid = self.own(row.stored.cid),
                .expires = self.own(row.stored.expires),
            },
        };
        for (list.items) |*existing| {
            if (existing.subject == row.subject and std.mem.eql(u8, existing.value, row.value) and std.mem.eql(u8, existing.source, row.source)) {
                existing.* = owned;
                return;
            }
        }
        list.append(self.mem(), owned) catch @panic("out of memory");
        std.mem.sort(Row, list.items, {}, Row.before);
    }

    fn findRow(self: *Model, did: []const u8, source: []const u8, value: []const u8, subject: keys.Subject) ?Row {
        const list = self.labels.getPtr(did) orelse return null;
        for (list.items) |row| {
            if (row.subject == subject and std.mem.eql(u8, row.value, value) and std.mem.eql(u8, row.source, source)) return row;
        }
        return null;
    }

    fn projected(self: *Model, did: []const u8, others: []const record.Label) []const record.Label {
        var out: std.ArrayList(record.Label) = .empty;
        for (others) |one| {
            if (!std.mem.eql(u8, one.source, self.source)) out.append(self.mem(), one) catch @panic("out of memory");
        }
        if (self.labels.getPtr(did)) |list| for (list.items) |row| {
            if (row.stored.negated) continue;
            out.append(self.mem(), .{
                .source = row.source,
                .uri = switch (row.subject) {
                    .account => did,
                    .profile => std.fmt.allocPrint(self.mem(), "at://{s}/app.bsky.actor.profile/self", .{did}) catch @panic("out of memory"),
                },
                .value = row.value,
                .created = row.stored.created,
                .cid = row.stored.cid,
                .expires = row.stored.expires,
            }) catch @panic("out of memory");
        };
        return out.items;
    }

    fn reconcile(self: *Model, did: []const u8, answer: []const record.Label) void {
        const list = self.rows(did);
        var kept: std.ArrayList(Row) = .empty;
        for (list.items) |row| if (row.stored.sequence != 0) kept.append(self.mem(), row) catch @panic("out of memory");
        list.* = kept;
        // the labeler's history: per subject and value the first entry wins
        // until a later one is created at the same time or after it
        const Current = struct { uri: []const u8, value: []const u8, at: usize };
        var current: std.ArrayList(Current) = .empty;
        for (answer, 0..) |one, index| {
            if (!std.mem.eql(u8, one.source, self.source)) continue;
            const slot = for (current.items) |*c| {
                if (std.mem.eql(u8, c.uri, one.uri) and std.mem.eql(u8, c.value, one.value)) break c;
            } else {
                current.append(self.mem(), .{ .uri = one.uri, .value = one.value, .at = index }) catch @panic("out of memory");
                continue;
            };
            const mine = time.parseMs(one.created) orelse continue;
            const theirs = time.parseMs(answer[slot.at].created) orelse continue;
            if (mine >= theirs) slot.at = index;
        }
        for (current.items) |c| {
            const one = answer[c.at];
            const subject = change.subjectOf(did, one.uri) orelse continue;
            if (one.value.len == 0 or one.value.len > keys.max_label_value) continue;
            if (self.findRow(did, one.source, one.value, subject) != null) continue;
            self.setRow(did, .{ .source = one.source, .value = one.value, .subject = subject, .stored = .{ .sequence = 0, .negated = one.negated, .created = one.created, .cid = one.cid, .expires = one.expires } });
        }
    }

    fn liveActor(self: *const Model, did: []const u8) ?Actor {
        const actor = self.actors.get(did) orelse return null;
        return if (actor.status == .live) actor else null;
    }

    /// Apply one change. Returns how many actors' content it altered.
    pub fn apply(self: *Model, one: Change) usize {
        switch (one) {
            .observe => |c| {
                const known = self.actors.get(c.did);
                var actor = known orelse self.fresh(c.did);
                if (actor.status != .live) return 0;
                self.stamp(&actor);
                if (known == null and c.enqueue_identity) self.discover(c.did, &actor);
                return @intFromBool(self.put(c.did, actor));
            },
            .profile => |c| {
                const known = self.actors.get(c.did);
                var actor = known orelse self.fresh(c.did);
                if (actor.status != .live) return 0;
                actor.handle = orKeep(actor.handle, c.handle);
                actor.display_name = orKeep(actor.display_name, c.display_name);
                actor.avatar = orKeep(actor.avatar, c.avatar);
                actor.pds = orKeep(actor.pds, c.pds);
                actor.description = orKeep(actor.description, c.description);
                actor.banner = orKeep(actor.banner, c.banner);
                self.stamp(&actor);
                self.wantRefresh(&actor);
                if (known == null and c.enqueue_identity) self.discover(c.did, &actor);
                return @intFromBool(self.put(c.did, actor));
            },
            .identity_resolved => |c| {
                var actor = self.liveActor(c.did) orelse return 0;
                if (!self.resolved(&actor, c.observed_at, c.handle, c.pds)) return 0;
                return @intFromBool(self.put(c.did, actor));
            },
            .gone => |c| {
                var actor = self.actors.get(c.did) orelse return 0;
                actor.status = if (c.status == .deleted) .deleted else .deactivated;
                actor.deleted_at = self.now;
                return @intFromBool(self.put(c.did, actor));
            },
            .reactivated => |did| {
                const known = self.actors.get(did);
                if (known != null and known.?.status == .live) return 0;
                var actor = known orelse self.fresh(did);
                actor.status = .live;
                self.stamp(&actor);
                self.wantRefresh(&actor);
                return @intFromBool(self.put(did, actor));
            },
            .label => |c| {
                if (!std.mem.eql(u8, c.source, self.source)) return 0;
                var actor = self.liveActor(c.did) orelse return 0;
                if (self.findRow(c.did, c.source, c.value, c.subject)) |existing| {
                    if (c.sequence < existing.stored.sequence) return 0;
                }
                self.setRow(c.did, .{ .source = c.source, .value = c.value, .subject = c.subject, .stored = .{ .sequence = c.sequence, .negated = c.negated, .created = c.created, .cid = c.cid, .expires = c.expires } });
                actor.labels = self.projected(c.did, actor.labels);
                return @intFromBool(self.put(c.did, actor));
            },
            .labels_reconciled => |c| {
                var actor = self.liveActor(c.did) orelse return 0;
                self.reconcile(c.did, c.labels);
                actor.labels = self.projected(c.did, actor.labels);
                return @intFromBool(self.put(c.did, actor));
            },
            .override_set => |c| {
                const action: record.Override = if (c.action == .show) .show else .hide;
                self.overrides.put(self.mem(), self.own(c.did), .{ .action = action, .created_at = self.now, .reason = self.own(c.reason) }) catch @panic("out of memory");
                var actor = self.actors.get(c.did) orelse return 0;
                actor.override = action;
                return @intFromBool(self.put(c.did, actor));
            },
            .override_cleared => |did| {
                _ = self.overrides.orderedRemove(did);
                var actor = self.actors.get(did) orelse return 0;
                actor.override = .none;
                return @intFromBool(self.put(did, actor));
            },
            .domain_blocked => |c| {
                var buf: [keys.max_domain]u8 = undefined;
                const lower = std.ascii.lowerString(&buf, c.domain);
                self.domains.put(self.mem(), self.own(lower), .{ .created_at = self.now, .reason = self.own(c.reason) }) catch @panic("out of memory");
                return 0;
            },
            .domain_unblocked => |name| {
                var buf: [keys.max_domain]u8 = undefined;
                _ = self.domains.orderedRemove(std.ascii.lowerString(&buf, name));
                return 0;
            },
            .enrichment => |c| {
                var actor = self.liveActor(c.did) orelse return 0;
                if (self.shouldVerify(actor, c)) {
                    actor.identity_due = self.now;
                    self.queue.put(self.mem(), self.own(c.did), .{ .reason = .identity, .enqueued_at = self.now }) catch @panic("out of memory");
                }
                actor.display_name = orKeep(actor.display_name, c.display_name);
                actor.avatar = orKeep(actor.avatar, c.avatar);
                actor.description = orKeep(actor.description, c.description);
                actor.banner = orKeep(actor.banner, c.banner);
                actor.created_at = orKeep(actor.created_at, c.created_at);
                if (c.associated.len > 0 and !std.mem.eql(u8, c.associated, "{}")) actor.associated = c.associated;
                if (c.followers_count != 0) actor.followers_count = c.followers_count;
                if (c.follows_count != 0) actor.follows_count = c.follows_count;
                if (c.posts_count != 0) actor.posts_count = c.posts_count;
                if (c.quality_score != 0) actor.quality_score = c.quality_score;
                actor.profile_source = c.source;
                actor.profile_checked_at = self.now;
                if (c.lease == null or c.lease.? == actor.refresh_due) actor.refresh_due = c.next_refresh orelse 0;
                if (c.labels) |list| {
                    self.reconcile(c.did, list);
                    actor.labels = self.projected(c.did, list);
                }
                return @intFromBool(self.put(c.did, actor));
            },
            .identity_enqueue => |c| {
                const known = self.actors.get(c.did);
                if (known == null and c.reason == .plc) return 0;
                if (known != null and c.reason == .discovered) return 0;
                var actor = known orelse self.fresh(c.did);
                if (actor.status != .live) return 0;
                if (self.queue.getPtr(c.did)) |state| {
                    state.generation += 1;
                    state.attempts = 0;
                    state.reason = c.reason;
                    actor.identity_due = @min(actor.identity_due, self.now);
                } else {
                    self.queue.put(self.mem(), self.own(c.did), .{ .reason = c.reason, .enqueued_at = self.now }) catch @panic("out of memory");
                    actor.identity_due = self.now;
                }
                return @intFromBool(self.put(c.did, actor));
            },
            .identity_lease => |c| {
                var due: std.ArrayList([]const u8) = .empty;
                for (self.actors.keys(), self.actors.values()) |did, actor| {
                    if (actor.identity_due != 0 and actor.identity_due <= self.now) due.append(self.mem(), did) catch @panic("out of memory");
                }
                std.mem.sort([]const u8, due.items, self, dueBefore);
                for (due.items[0..@min(due.items.len, c.limit)]) |did| {
                    var actor = self.actors.get(did).?;
                    self.queue.getPtr(did).?.attempts += 1;
                    actor.identity_due = self.now + c.lease_seconds;
                    _ = self.put(did, actor);
                }
                return 0;
            },
            .identity_complete => |c| {
                var actor = self.liveActor(c.did) orelse return 0;
                const current = if (self.queue.get(c.did)) |state| state.generation == c.generation else false;
                switch (c.outcome) {
                    .resolved => |r| {
                        _ = self.resolved(&actor, c.observed_at, r.handle, c.pds);
                        if (current) {
                            actor.identity_due = 0;
                            _ = self.queue.remove(c.did);
                        }
                    },
                    .retry => |r| {
                        if (c.pds.len > 0) _ = self.resolved(&actor, c.observed_at, "", c.pds);
                        if (current) {
                            if (r.attempts >= 12) {
                                actor.identity_due = 0;
                                _ = self.queue.remove(c.did);
                            } else {
                                actor.identity_due = self.now + @min(86_400, @max(30, r.delay_seconds));
                                self.queue.getPtr(c.did).?.last_error = self.own(r.reason);
                            }
                        }
                    },
                }
                return @intFromBool(self.put(c.did, actor));
            },
            .refresh_enqueue => |c| {
                var actor = self.liveActor(c.did) orelse return 0;
                if (c.due == 0) return 0;
                actor.refresh_due = if (actor.refresh_due == 0) c.due else @min(actor.refresh_due, c.due);
                return @intFromBool(self.put(c.did, actor));
            },
            .refresh_complete => |c| {
                var actor = self.liveActor(c.did) orelse return 0;
                actor.profile_checked_at = self.now;
                if (c.lease == null or c.lease.? == actor.refresh_due) actor.refresh_due = c.next_due orelse 0;
                return @intFromBool(self.put(c.did, actor));
            },
            .refresh_lease => |c| {
                var due: std.ArrayList([]const u8) = .empty;
                for (self.actors.keys(), self.actors.values()) |did, actor| {
                    if (actor.refresh_due != 0 and actor.refresh_due <= self.now) due.append(self.mem(), did) catch @panic("out of memory");
                }
                std.mem.sort([]const u8, due.items, self, refreshBefore);
                for (due.items[0..@min(due.items.len, c.limit)]) |did| {
                    var actor = self.actors.get(did).?;
                    actor.refresh_due = self.now + c.lease_seconds;
                    _ = self.put(did, actor);
                }
                return 0;
            },
            .refresh_retry => |c| {
                var actor = self.liveActor(c.did) orelse return 0;
                if (c.due == 0 or actor.refresh_due != c.lease) return 0;
                actor.refresh_due = c.due;
                return @intFromBool(self.put(c.did, actor));
            },
            .refresh_walk => |c| {
                self.walk(c);
                return 0;
            },
            .hidden_refreshed => |did| {
                const actor = self.actors.get(did) orelse return 0;
                return @intFromBool(self.put(did, actor));
            },
            .tombstones_pruned => |c| {
                var doomed: std.ArrayList([]const u8) = .empty;
                for (self.actors.keys(), self.actors.values()) |did, actor| {
                    if (actor.status != .live and actor.deleted_at < c.before) doomed.append(self.mem(), did) catch @panic("out of memory");
                }
                std.mem.sort([]const u8, doomed.items, self, deletedBefore);
                const taken = doomed.items[0..@min(doomed.items.len, c.limit)];
                for (taken) |did| {
                    self.noteChanged(did);
                    _ = self.labels.orderedRemove(did);
                    _ = self.actors.orderedRemove(did);
                }
                return taken.len;
            },
        }
    }

    fn refreshBefore(self: *Model, a: []const u8, b: []const u8) bool {
        const x = self.actors.get(a).?.refresh_due;
        const y = self.actors.get(b).?.refresh_due;
        return if (x != y) x < y else std.mem.order(u8, a, b) == .lt;
    }

    /// The keys of a walk's range as the store holds them, in order.
    fn walkKeys(self: *Model, pool: change.WalkPool) []const []const u8 {
        var out: std.ArrayList([]const u8) = .empty;
        var buf: keys.Buf = undefined;
        for (self.actors.keys(), self.actors.values()) |did, actor| {
            const key = switch (pool) {
                .quality => if (actor.ranked()) keys.quality(&buf, actor.quality_score, did) catch unreachable else continue,
                .rest => keys.actor(&buf, did) catch unreachable,
            };
            out.append(self.mem(), self.own(key)) catch @panic("out of memory");
        }
        std.mem.sort([]const u8, out.items, {}, struct {
            fn before(_: void, a: []const u8, b: []const u8) bool {
                return std.mem.order(u8, a, b) == .lt;
            }
        }.before);
        return out.items;
    }

    fn walk(self: *Model, c: change.RefreshWalk) void {
        const state = self.walks.getPtr(c.pool);
        if (state.position.len == 0) state.started_at = self.now;
        const all = self.walkKeys(c.pool);
        var from: usize = 0;
        if (state.position.len != 0) {
            while (from < all.len and std.mem.order(u8, all[from], state.position) != .gt) from += 1;
        }
        const page = all[from..@min(all.len, from + c.scan)];
        var examined: usize = 0;
        var queued: usize = 0;
        for (page) |key| {
            if (queued == c.limit) break;
            examined += 1;
            state.position = key;
            const did = switch (c.pool) {
                .quality => (keys.parseQuality(key) catch unreachable).did,
                .rest => keys.parseActor(key) catch unreachable,
            };
            var actor = self.actors.get(did).?;
            if (actor.status != .live or actor.handle.len == 0 or actor.refresh_due != 0) continue;
            if (c.pool == .rest and actor.ranked()) continue;
            actor.refresh_due = self.now;
            _ = self.put(did, actor);
            queued += 1;
        }
        if (examined == page.len and page.len < c.scan) {
            state.passes += 1;
            state.last_pass_seconds = self.now - state.started_at;
            state.position = "";
        }
    }

    fn dueBefore(self: *Model, a: []const u8, b: []const u8) bool {
        const x = self.actors.get(a).?.identity_due;
        const y = self.actors.get(b).?.identity_due;
        return if (x != y) x < y else std.mem.order(u8, a, b) == .lt;
    }

    fn deletedBefore(self: *Model, a: []const u8, b: []const u8) bool {
        const x = self.actors.get(a).?.deleted_at;
        const y = self.actors.get(b).?.deleted_at;
        return if (x != y) x < y else std.mem.order(u8, a, b) == .lt;
    }

    pub fn setCursor(self: *Model, update: change.CursorUpdate) void {
        switch (update) {
            .relay => |value| self.relay = value,
            .bridge => |value| self.bridge = value,
            .labeler => |value| self.labeler = @max(self.labeler orelse 0, value),
            .control_policy => |value| self.control_policy = if (self.control_policy) |stored| @max(stored, value) else value,
            .control_requests => |value| self.control_requests = if (self.control_requests) |stored| @max(stored, value) else value,
            .plc => |value| {
                if (self.plc) |stored| if (std.mem.order(u8, value, stored) != .gt) return;
                self.plc = self.own(value);
            },
        }
        if (self.labeler == 0) self.labeler = null;
    }

    /// Recompute `hidden` for the live actors whose handle sits under
    /// `domain`, in the order the handle range holds them. Each call is one
    /// commit covering the next `scan_limit` handle keys.
    pub fn sweepOrder(self: *Model, gpa: std.mem.Allocator) ![]const []const u8 {
        const Entry = struct { handle: []u8, did: []const u8 };
        var entries: std.ArrayList(Entry) = .empty;
        defer {
            for (entries.items) |e| gpa.free(e.handle);
            entries.deinit(gpa);
        }
        for (self.actors.keys(), self.actors.values()) |did, actor| {
            if (actor.status != .live or actor.handle.len == 0) continue;
            const lower = try gpa.dupe(u8, actor.handle);
            _ = std.ascii.lowerString(lower, actor.handle);
            try entries.append(gpa, .{ .handle = lower, .did = did });
        }
        std.mem.sort(Entry, entries.items, {}, struct {
            fn before(_: void, a: Entry, b: Entry) bool {
                return switch (std.mem.order(u8, a.handle, b.handle)) {
                    .lt => true,
                    .gt => false,
                    .eq => std.mem.order(u8, a.did, b.did) == .lt,
                };
            }
        }.before);
        const out = try gpa.alloc([]const u8, entries.items.len);
        for (out, entries.items) |*slot, e| slot.* = e.did;
        return out;
    }

    pub fn trim(self: *Model, below: u64) void {
        self.floor = @max(self.floor, @min(below, self.nextChange()));
    }
};
