//! What the role hears, as store changes. Pure: no I/O and no clock.
//!
//! This is the Worker's write path (`src/handlers/ingest.ts`, `handleDelete`,
//! `src/identity-queue.ts`, `src/account-labels.ts`) restated as changes:
//!
//! | heard                                   | changes                                        |
//! |-----------------------------------------|------------------------------------------------|
//! | profile record with any field           | `profile`, discovery queued if new             |
//! | profile record with no field we keep    | `observe`, discovery queued if new             |
//! | any other wanted record, not a delete   | `observe`, discovery queued if new             |
//! | `#identity`                             | `observe`, `identity_enqueue` (identity)       |
//! | `#account`, inactive                    | `gone` (deleted, or deactivated for the rest)  |
//! | `#account`, active                      | `reactivated`, `observe`, `identity_enqueue` (account) |
//! | identity resolution, resolved           | `identity_complete` with the admitted handle   |
//! | identity resolution, to retry           | `identity_complete` with the retry             |
//! | PLC export page                         | `identity_enqueue` (plc) per distinct DID      |
//! | label frame                             | `label` per adopted, well-formed label         |
//!
//! `observe` is what stamps an actor's hourly activity, so it accompanies
//! every signal about an actor that may already be known.

const std = @import("std");
const zat = @import("zat");
const store = @import("../store/root.zig");
const ingest = @import("../ingest.zig");
const identity = @import("../identity.zig");
const label_tail = @import("../label_tail.zig");
const handle_policy = @import("handle_policy.zig");
const Allocator = std.mem.Allocator;
const Change = store.Change;

pub const Changes = struct {
    /// Owns every string the changes point at.
    arena: Allocator,
    list: std.ArrayList(Change) = .empty,
    /// DIDs this batch already observed. A second sight in the same commit
    /// would be a point read that changes nothing.
    observed: std.StringHashMapUnmanaged(void) = .empty,

    pub fn items(self: *const Changes) []const Change {
        return self.list.items;
    }

    fn add(self: *Changes, one: Change) !void {
        try self.list.append(self.arena, one);
    }

    fn observe(self: *Changes, did: []const u8, enqueue_identity: bool) !void {
        const seen = try self.observed.getOrPut(self.arena, did);
        if (seen.found_existing) return;
        try self.add(.{ .observe = .{ .did = did, .enqueue_identity = enqueue_identity } });
    }

    /// One relay event. All of an event's changes are added or none are, so
    /// a batch never holds part of an event.
    pub fn firehose(self: *Changes, event: zat.FirehoseEvent) !void {
        const before = self.list.items.len;
        errdefer self.list.shrinkRetainingCapacity(before);
        switch (event) {
            .commit => |c| {
                const did = try self.arena.dupe(u8, c.repo);
                for (c.ops) |one| {
                    if (ingest.wantedCollection(one.collection)) try self.record(did, one);
                }
            },
            .identity => |id| {
                const did = try self.arena.dupe(u8, id.did);
                try self.observe(did, false);
                try self.add(.{ .identity_enqueue = .{ .did = did, .reason = .identity } });
            },
            .account => |acct| try self.account(try self.arena.dupe(u8, acct.did), acct.active, acct.status),
            .sync, .info => {},
        }
    }

    fn record(self: *Changes, did: []const u8, one: zat.firehose.RepoOp) !void {
        if (!ingest.isProfileCollection(one.collection)) {
            if (one.action != .delete) try self.observe(did, true);
            return;
        }
        if (one.action != .create and one.action != .update) return;
        const fields = ingest.profileFields(self.arena, one.record orelse return);
        const profile: store.change.Profile = .{
            .did = did,
            .display_name = fields.display_name orelse "",
            .avatar = fields.avatar_cid orelse "",
            .description = fields.description orelse "",
            .banner = fields.banner_cid orelse "",
            .enqueue_identity = true,
        };
        if (profile.display_name.len == 0 and profile.avatar.len == 0 and profile.description.len == 0 and profile.banner.len == 0) {
            return self.observe(did, true);
        }
        try self.observed.put(self.arena, did, {});
        try self.add(.{ .profile = profile });
    }

    fn account(self: *Changes, did: []const u8, active: bool, status: ?zat.AccountStatus) !void {
        if (!active) {
            _ = self.observed.remove(did);
            return self.add(.{ .gone = .{ .did = did, .status = if (status == .deleted) .deleted else .deactivated } });
        }
        try self.add(.{ .reactivated = did });
        try self.observe(did, false);
        try self.add(.{ .identity_enqueue = .{ .did = did, .reason = .account } });
    }

    /// The did:plc subjects of one PLC export page. The store ignores the
    /// ones it does not hold.
    pub fn plc(self: *Changes, dids: []const []const u8) !void {
        var seen: std.StringHashMapUnmanaged(void) = .empty;
        for (dids) |did| {
            const slot = try seen.getOrPut(self.arena, did);
            if (slot.found_existing) continue;
            try self.add(.{ .identity_enqueue = .{ .did = try self.arena.dupe(u8, did), .reason = .plc } });
        }
    }

    /// Labels from the stream. Returns how many were left out: not from the
    /// adopted labeler, not about an account, or malformed, by the rules the
    /// Worker's `/admin/labels` schema applies.
    pub fn labels(self: *Changes, wires: []const label_tail.Wire) !usize {
        var ignored: usize = 0;
        for (wires) |wire| {
            const one = try self.label(wire) orelse {
                ignored += 1;
                continue;
            };
            try self.add(.{ .label = one });
        }
        return ignored;
    }

    fn label(self: *Changes, wire: label_tail.Wire) !?store.change.LabelEvent {
        const did = label_tail.adoptedSubject(.{ .src = wire.src, .uri = wire.uri, .val = wire.val, .cts = wire.cts }) orelse return null;
        if (!validLabelDid(did) or wire.seq < 1 or wire.uri.len > 512) return null;
        if (wire.val.len == 0 or wire.val.len > store.keys.max_label_value) return null;
        if (store.time.parseMs(wire.cts) == null) return null;
        if (wire.cid) |cid| if (cid.len > 128) return null;
        if (wire.exp) |exp| if (store.time.parseMs(exp) == null) return null;
        const a = self.arena;
        return .{
            .did = try a.dupe(u8, did),
            .source = try a.dupe(u8, wire.src),
            .value = try a.dupe(u8, wire.val),
            .subject = if (std.mem.eql(u8, wire.uri, did)) .account else .profile,
            .negated = wire.neg orelse false,
            .created = try a.dupe(u8, wire.cts),
            .cid = if (wire.cid) |cid| try a.dupe(u8, cid) else "",
            .expires = if (wire.exp) |exp| try a.dupe(u8, exp) else "",
            .sequence = @intCast(wire.seq),
        };
    }
};

/// The Worker's `did` schema for label subjects: a did:plc of 24 base32
/// characters, or a did:web.
pub fn validLabelDid(did: []const u8) bool {
    if (std.mem.startsWith(u8, did, "did:plc:")) {
        const id = did["did:plc:".len..];
        if (id.len != 24) return false;
        for (id) |c| if (!((c >= 'a' and c <= 'z') or (c >= '2' and c <= '7'))) return false;
        return true;
    }
    if (std.mem.startsWith(u8, did, "did:web:")) {
        const host = did["did:web:".len..];
        if (host.len == 0 or host.len > 253) return false;
        for (host) |c| if (!(std.ascii.isAlphanumeric(c) or c == '.' or c == ':' or c == '%' or c == '-')) return false;
        return true;
    }
    return false;
}

/// What one resolved job reports, with the handle the policy admits. Strings
/// are borrowed from the job and the observation.
pub fn completion(job: *const identity.Job, seen: identity.Observation) Change {
    const observed_at: u64 = @intCast(@max(seen.observed_at, 0));
    return .{ .identity_complete = switch (seen.outcome) {
        .resolved => |r| .{
            .did = job.did.slice(),
            .generation = job.gen,
            .observed_at = observed_at,
            .pds = r.pds orelse "",
            .outcome = .{ .resolved = .{ .handle = handle_policy.admitted(r.handle orelse "", r.handle_verified) } },
        },
        .retry => |r| .{
            .did = job.did.slice(),
            .generation = job.gen,
            .observed_at = observed_at,
            .pds = r.pds orelse "",
            .outcome = .{ .retry = .{ .attempts = job.attempts, .delay_seconds = @intCast(@max(r.delay_seconds, 0)), .reason = r.@"error" } },
        },
    } };
}
