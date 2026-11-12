//! Test doubles: an in-memory store that implements `source.Source`, a model
//! of the Tantivy server's source endpoints, and a transport wrapper that
//! kills a request before or after it takes effect.

const std = @import("std");
const Allocator = std.mem.Allocator;
const json = std.json;
const Sha256 = std.crypto.hash.sha2.Sha256;
const source = @import("source.zig");
const wire = @import("wire.zig");

/// Behaves like the store module where the bridge can tell: sequences start
/// at 1, a deleted actor keeps its record with a status until it is pruned,
/// and a write that changes nothing appends no log entry.
pub const MemoryStore = struct {
    gpa: Allocator,
    strings: std.heap.ArenaAllocator,
    actors: std.StringArrayHashMapUnmanaged(source.Actor) = .empty,
    log: std.ArrayList(source.Change) = .empty,
    next_change: u64 = 1,
    floor: u64 = 0,
    bridge: ?u64 = null,
    policy_value: ?source.Policy = null,
    fail_next_set_position: bool = false,
    /// DIDs `walk` returns beyond the limit it was given.
    walk_excess: usize = 0,

    pub fn init(gpa: Allocator) MemoryStore {
        return .{ .gpa = gpa, .strings = .init(gpa) };
    }

    pub fn deinit(self: *MemoryStore) void {
        self.actors.deinit(self.gpa);
        self.log.deinit(self.gpa);
        self.strings.deinit();
    }

    fn append(self: *MemoryStore, did: []const u8) !void {
        try self.log.append(self.gpa, .{ .sequence = self.next_change, .did = did });
        self.next_change += 1;
    }

    fn same(a: source.Actor, b: source.Actor) bool {
        if (a.status != b.status or a.hidden != b.hidden or a.quality_score != b.quality_score or a.labels.len != b.labels.len) return false;
        if (a.updated_at != b.updated_at or a.profile_checked != b.profile_checked) return false;
        inline for (.{ "handle", "display_name", "avatar", "description", "banner", "pds", "created_at", "associated" }) |name| {
            if (!std.mem.eql(u8, @field(a, name), @field(b, name))) return false;
        }
        for (a.labels, b.labels) |x, y| {
            if (x.negated != y.negated or !std.mem.eql(u8, x.value, y.value) or !std.mem.eql(u8, x.expires, y.expires)) return false;
        }
        return a.followers_count == b.followers_count and a.follows_count == b.follows_count and a.posts_count == b.posts_count;
    }

    pub fn put(self: *MemoryStore, did: []const u8, actor: source.Actor) !void {
        if (self.actors.get(did)) |old| if (same(old, actor)) return;
        const a = self.strings.allocator();
        var copy = actor;
        inline for (.{ "handle", "display_name", "avatar", "description", "banner", "pds", "created_at", "associated" }) |name| {
            @field(copy, name) = try a.dupe(u8, @field(actor, name));
        }
        const labels = try a.alloc(source.Label, actor.labels.len);
        for (labels, actor.labels) |*out, label| {
            out.* = label;
            inline for (.{ "source", "uri", "value", "created", "cid", "expires" }) |name| {
                @field(out, name) = try a.dupe(u8, @field(label, name));
            }
        }
        copy.labels = labels;
        const entry = try self.actors.getOrPut(self.gpa, did);
        if (!entry.found_existing) entry.key_ptr.* = try a.dupe(u8, did);
        entry.value_ptr.* = copy;
        try self.append(entry.key_ptr.*);
    }

    /// Mark the actor deleted, keeping its record as the store does.
    pub fn delete(self: *MemoryStore, did: []const u8) !void {
        var actor = self.actors.get(did) orelse return;
        actor.status = .deleted;
        try self.put(did, actor);
    }

    /// Remove a record with no log entry, as tombstone pruning does.
    pub fn prune(self: *MemoryStore, did: []const u8) void {
        _ = self.actors.swapRemove(did);
    }

    /// Append a log entry without touching any record.
    pub fn touch(self: *MemoryStore, did: []const u8) !void {
        try self.append(try self.strings.allocator().dupe(u8, did));
    }

    pub fn get(self: *const MemoryStore, did: []const u8) ?source.Actor {
        return self.actors.get(did);
    }

    /// Drop log entries below `below`, as `Store.trimChanges` does.
    pub fn trim(self: *MemoryStore, below: u64) void {
        var kept: usize = 0;
        for (self.log.items) |change| {
            if (change.sequence >= below) {
                self.log.items[kept] = change;
                kept += 1;
            }
        }
        self.log.shrinkRetainingCapacity(kept);
        self.floor = @max(self.floor, @min(below, self.next_change));
    }

    pub fn interface(self: *MemoryStore) source.Source {
        return .{ .ptr = self, .vtable = &.{
            .changes = changes,
            .cursors = cursors,
            .actor = actorOf,
            .walk = walk,
            .policy = policyOf,
            .setPosition = setPosition,
        } };
    }

    fn changes(ptr: *anyopaque, arena: Allocator, from: u64, limit: usize) anyerror!source.Changes {
        const self: *MemoryStore = @ptrCast(@alignCast(ptr));
        if (from < self.floor) return .{ .trimmed = self.floor };
        var entries: std.ArrayList(source.Change) = .empty;
        for (self.log.items) |change| {
            if (entries.items.len == limit) break;
            if (change.sequence >= from) try entries.append(arena, change);
        }
        return .{ .entries = entries.items };
    }

    fn cursors(ptr: *anyopaque) anyerror!source.Cursors {
        const self: *MemoryStore = @ptrCast(@alignCast(ptr));
        return .{ .bridge = self.bridge, .next_change = self.next_change };
    }

    fn actorOf(ptr: *anyopaque, _: Allocator, did: []const u8) anyerror!?source.Actor {
        const self: *MemoryStore = @ptrCast(@alignCast(ptr));
        return self.actors.get(did);
    }

    fn walk(ptr: *anyopaque, arena: Allocator, after_did: []const u8, limit: usize) anyerror![]const []const u8 {
        const self: *MemoryStore = @ptrCast(@alignCast(ptr));
        var dids: std.ArrayList([]const u8) = .empty;
        for (self.actors.keys()) |did| {
            if (std.mem.order(u8, did, after_did) == .gt) try dids.append(arena, did);
        }
        std.mem.sort([]const u8, dids.items, {}, struct {
            fn lessThan(_: void, a: []const u8, b: []const u8) bool {
                return std.mem.lessThan(u8, a, b);
            }
        }.lessThan);
        return dids.items[0..@min(limit + self.walk_excess, dids.items.len)];
    }

    fn policyOf(ptr: *anyopaque, _: Allocator) anyerror!?source.Policy {
        const self: *MemoryStore = @ptrCast(@alignCast(ptr));
        return self.policy_value;
    }

    fn setPosition(ptr: *anyopaque, value: u64) anyerror!void {
        const self: *MemoryStore = @ptrCast(@alignCast(ptr));
        if (self.fail_next_set_position) {
            self.fail_next_set_position = false;
            return error.StoreWriteFailed;
        }
        self.bridge = value;
    }
};

/// Follows services/tantivy/src/engine/source.rs and `apply_batch`: receipt
/// replay by batch ID, then the revision, document-hash and cursor guards,
/// then an atomic apply in which a document equal to the stored one apart
/// from `version` counts as replayed and is left alone.
pub const ModelServer = struct {
    pub const Document = struct {
        projection: []const u8,
        version: u64,
        deleted: bool,
        hash: [64]u8,
    };

    pub const Fault = enum { none, drop_before, drop_after, conflict, invalid };

    gpa: Allocator,
    strings: std.heap.ArenaAllocator,
    documents: std.StringArrayHashMapUnmanaged(Document) = .empty,
    cursor: []const u8 = "null",
    revision: u64 = 0,
    watermark: u64 = 100,
    import_complete: bool = true,
    policy_observed_at: ?u64 = null,
    policy_snapshots: u64 = 0,
    last_batch_id: []const u8 = "",
    last_body_sha: [Sha256.digest_length]u8 = undefined,
    last_receipt: []const u8 = "",
    last_reconcile: []const u8 = "",
    reconciles: u64 = 0,
    applied: u64 = 0,
    replayed: u64 = 0,
    watermarks: u64 = 0,
    max_actors_seen: usize = 0,
    fault: Fault = .none,

    pub fn init(gpa: Allocator) ModelServer {
        return .{ .gpa = gpa, .strings = .init(gpa) };
    }

    pub fn deinit(self: *ModelServer) void {
        self.documents.deinit(self.gpa);
        self.strings.deinit();
    }

    pub fn transport(self: *ModelServer) wire.Transport {
        return .{ .ptr = self, .send = send };
    }

    pub fn field(self: *const ModelServer, arena: Allocator, did: []const u8, name: []const u8) !?json.Value {
        const document = self.documents.get(did) orelse return null;
        const value = try json.parseFromSliceLeaky(json.Value, arena, document.projection, .{});
        return value.object.get(name);
    }

    pub fn committedCursor(self: *const ModelServer, arena: Allocator) ?wire.Cursor {
        return wire.parseCursor(arena, self.cursor);
    }

    fn reply(arena: Allocator, status: u16, value: anytype) !wire.Reply {
        return .{ .status = status, .body = try json.Stringify.valueAlloc(arena, value, .{}) };
    }

    fn send(ptr: *anyopaque, arena: Allocator, request: wire.Request) anyerror!wire.Reply {
        const self: *ModelServer = @ptrCast(@alignCast(ptr));
        if (std.mem.eql(u8, request.path, "/index/status")) {
            return reply(arena, 200, .{
                .build_id = "model",
                .snapshot_watermark = @as(u64, 100),
                .watermark = self.watermark,
                .import_complete = self.import_complete,
                .ready = true,
                .source_observation_revision = self.revision,
                .moderation = .{ .source_observed_at = self.policy_observed_at },
            });
        }
        const body = try json.parseFromSliceLeaky(json.Value, arena, request.body.?, .{});
        if (std.mem.eql(u8, request.path, "/admin/source-state")) return self.state(arena, body);
        if (std.mem.eql(u8, request.path, "/admin/source-page")) return self.page(arena, body);
        if (std.mem.eql(u8, request.path, "/admin/source-reconcile")) {
            const fault = self.fault;
            self.fault = .none;
            switch (fault) {
                .none => {},
                .drop_before => return error.ConnectionResetByPeer,
                .drop_after => {
                    _ = try self.reconcile(arena, request.body.?, body);
                    return error.ConnectionResetByPeer;
                },
                .conflict => return reply(arena, 409, .{ .@"error" = "SourceVersionConflict" }),
                .invalid => return reply(arena, 400, .{ .@"error" = "injected refusal" }),
            }
            return self.reconcile(arena, request.body.?, body);
        }
        return reply(arena, 404, .{ .@"error" = "not found" });
    }

    fn rawCursor(self: *const ModelServer, arena: Allocator) !json.Value {
        return json.parseFromSliceLeaky(json.Value, arena, self.cursor, .{});
    }

    fn state(self: *ModelServer, arena: Allocator, body: json.Value) !wire.Reply {
        const dids = body.object.get("dids").?.array.items;
        if (dids.len > 500) return reply(arena, 400, .{ .@"error" = "source state requires at most 500 unique DIDs" });
        var seen: std.StringArrayHashMapUnmanaged(void) = .empty;
        var actors: std.ArrayList(wire.ExpectedDocument) = .empty;
        for (dids) |did| {
            if (!wire.validDid(did.string) or (try seen.getOrPut(arena, did.string)).found_existing) {
                return reply(arena, 400, .{ .@"error" = "source state requires at most 500 unique DIDs" });
            }
            const document = self.documents.getPtr(did.string);
            try actors.append(arena, .{ .did = did.string, .document_sha256 = if (document) |d| &d.hash else null });
        }
        return reply(arena, 200, .{
            .source_cursor = try self.rawCursor(arena),
            .source_observation_revision = self.revision,
            .actors = actors.items,
        });
    }

    fn page(self: *ModelServer, arena: Allocator, body: json.Value) !wire.Reply {
        const after = body.object.get("after_did").?.string;
        const limit: usize = @intCast(body.object.get("limit").?.integer);
        if (limit < 1 or limit > 500) return reply(arena, 400, .{ .@"error" = "source page requires limit 1..500 and bounded cursor" });
        var dids: std.ArrayList([]const u8) = .empty;
        for (self.documents.keys()) |did| {
            if (std.mem.order(u8, did, after) == .gt) try dids.append(arena, did);
        }
        std.mem.sort([]const u8, dids.items, {}, struct {
            fn lessThan(_: void, a: []const u8, b: []const u8) bool {
                return std.mem.lessThan(u8, a, b);
            }
        }.lessThan);
        return reply(arena, 200, .{
            .dids = dids.items[0..@min(limit, dids.items.len)],
            .source_cursor = try self.rawCursor(arena),
            .source_observation_revision = self.revision,
        });
    }

    fn hashOf(projection: []const u8, version: u64) [64]u8 {
        var hasher = Sha256.init(.{});
        hasher.update(projection);
        hasher.update(std.mem.asBytes(&version));
        var digest: [Sha256.digest_length]u8 = undefined;
        hasher.final(&digest);
        return std.fmt.bytesToHex(digest, .lower);
    }

    fn reconcile(self: *ModelServer, arena: Allocator, raw: []const u8, body: json.Value) !wire.Reply {
        const keep = self.strings.allocator();
        const root = body.object;
        const actors = root.get("actors").?.array.items;
        const expected = root.get("expected_documents").?.array.items;
        const batch_id = root.get("batch_id").?.string;
        if (actors.len > 500 or batch_id.len != 64) return reply(arena, 400, .{ .@"error" = "invalid source batch size or ID" });
        if (actors.len != expected.len or root.get("source_cursor").? != .object) {
            return reply(arena, 400, .{ .@"error" = "source expectations must cover each unique actor" });
        }
        var body_sha: [Sha256.digest_length]u8 = undefined;
        Sha256.hash(raw, &body_sha, .{});
        if (std.mem.eql(u8, batch_id, self.last_batch_id)) {
            if (!std.mem.eql(u8, &body_sha, &self.last_body_sha)) return reply(arena, 409, .{ .@"error" = "SourceVersionConflict" });
            return .{ .status = 200, .body = self.last_receipt };
        }
        if (root.get("expected_observation_revision").?.integer != self.revision) {
            return reply(arena, 409, .{ .@"error" = "SourceVersionConflict" });
        }
        for (expected, actors) |item, actor| {
            const did = item.object.get("did").?.string;
            if (!std.mem.eql(u8, did, actor.object.get("did").?.string)) {
                return reply(arena, 400, .{ .@"error" = "source expectations must cover each unique actor" });
            }
            const claimed = item.object.get("document_sha256").?;
            const actual = self.documents.getPtr(did);
            const matches = if (actual) |document| claimed == .string and std.mem.eql(u8, claimed.string, &document.hash) else claimed == .null;
            if (!matches) return reply(arena, 409, .{ .@"error" = "SourceVersionConflict" });
        }
        const expected_cursor = try json.Stringify.valueAlloc(arena, root.get("expected_source_cursor").?, .{});
        if (!std.mem.eql(u8, expected_cursor, self.cursor)) return reply(arena, 409, .{ .@"error" = "SourceVersionConflict" });
        const watermark: ?u64 = if (root.get("watermark")) |value| @intCast(value.integer) else null;
        if (watermark) |value| if (value < self.watermark) return reply(arena, 400, .{ .@"error" = "watermark cannot move backward" });
        var policy_clock: ?u64 = null;
        if (root.get("policy_snapshot")) |snapshot| {
            policy_clock = @intCast(snapshot.object.get("source_observed_at").?.integer);
            if (snapshot.object.get("complete").?.bool != true or (self.policy_observed_at != null and policy_clock.? < self.policy_observed_at.?)) {
                return reply(arena, 400, .{ .@"error" = "moderation source clock cannot move backward or into the future" });
            }
        }
        for (actors) |actor| {
            const fields = actor.object;
            const live = !fields.get("deleted").?.bool;
            if (!wire.validDid(fields.get("did").?.string) or (live and fields.get("handle").?.string.len == 0) or
                fields.get("labels").? != .array or fields.get("associated").? != .object)
            {
                return reply(arena, 400, .{ .@"error" = "invalid actor" });
            }
        }

        var applied: u64 = 0;
        var replayed: u64 = 0;
        for (actors) |actor| {
            var fields = actor.object;
            const version: u64 = @intCast(fields.get("version").?.integer);
            const deleted = fields.get("deleted").?.bool;
            const did = fields.get("did").?.string;
            _ = fields.orderedRemove("version");
            const projection = try json.Stringify.valueAlloc(arena, json.Value{ .object = fields }, .{});
            if (self.documents.get(did)) |old| {
                if (std.mem.eql(u8, old.projection, projection)) {
                    replayed += 1;
                    continue;
                }
            }
            const entry = try self.documents.getOrPut(self.gpa, did);
            if (!entry.found_existing) entry.key_ptr.* = try keep.dupe(u8, did);
            entry.value_ptr.* = .{
                .projection = try keep.dupe(u8, projection),
                .version = version,
                .deleted = deleted,
                .hash = hashOf(projection, version),
            };
            applied += 1;
        }
        self.cursor = try json.Stringify.valueAlloc(keep, root.get("source_cursor").?, .{});
        self.revision += 1;
        if (watermark) |value| {
            self.watermark = value;
            self.watermarks += 1;
        }
        if (policy_clock) |value| {
            self.policy_observed_at = value;
            self.policy_snapshots += 1;
        }
        self.reconciles += 1;
        self.applied += applied;
        self.replayed += replayed;
        self.max_actors_seen = @max(self.max_actors_seen, actors.len);
        self.last_batch_id = try keep.dupe(u8, batch_id);
        self.last_body_sha = body_sha;
        self.last_reconcile = try keep.dupe(u8, raw);
        self.last_receipt = try json.Stringify.valueAlloc(keep, .{
            .applied = applied,
            .replayed = replayed,
            .stale = @as(u64, 0),
            .watermark = self.watermark,
            .opstamp = self.revision,
            .source_cursor = root.get("source_cursor").?,
            .source_observation_revision = self.revision,
        }, .{});
        return .{ .status = 200, .body = self.last_receipt };
    }
};

/// Fails the Nth request from now, either before it reaches the server or
/// after the server has acted on it, as a process kill at that point would.
pub const Chaos = struct {
    inner: wire.Transport,
    countdown: ?usize = null,
    after_effect: bool = false,
    fired: bool = false,

    pub fn transport(self: *Chaos) wire.Transport {
        return .{ .ptr = self, .send = send };
    }

    fn send(ptr: *anyopaque, arena: Allocator, request: wire.Request) anyerror!wire.Reply {
        const self: *Chaos = @ptrCast(@alignCast(ptr));
        if (self.countdown) |remaining| {
            if (remaining == 0) {
                self.countdown = null;
                self.fired = true;
                if (self.after_effect) _ = self.inner.send(self.inner.ptr, arena, request) catch {};
                return error.Killed;
            }
            self.countdown = remaining - 1;
        }
        return self.inner.send(self.inner.ptr, arena, request);
    }
};

pub const FakeClock = struct {
    seconds: u64 = 1_000,

    pub fn now(ptr: *anyopaque) u64 {
        const self: *FakeClock = @ptrCast(@alignCast(ptr));
        return self.seconds;
    }
};
