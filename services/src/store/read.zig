//! Reads shared by the writer and reader handles. Results are copied into
//! the caller's arena, so they outlive the next call on the handle.

const std = @import("std");
const engine = @import("engine.zig");
const keys = @import("keys.zig");
const record = @import("record.zig");
const scan = @import("scan.zig");
const Db = engine.Db;

pub fn actor(db: *Db, arena: std.mem.Allocator, did: []const u8) !?record.Actor {
    var buf: keys.Buf = undefined;
    const borrowed = try db.get(try keys.actor(&buf, did)) orelse return null;
    return try record.Actor.decode(arena, try arena.dupe(u8, borrowed));
}

pub fn actors(db: *Db, arena: std.mem.Allocator, dids: []const []const u8) ![]?record.Actor {
    const out = try arena.alloc(?record.Actor, dids.len);
    for (out, dids) |*slot, did| slot.* = try actor(db, arena, did);
    return out;
}

/// A page of the actor range: the DIDs that have a record, of any status,
/// strictly after `after_did` in ascending byte order. Pass "" to start,
/// and the last DID of a page to continue; fewer than `limit` ends the walk.
pub fn walk(db: *Db, arena: std.mem.Allocator, after_did: []const u8, limit: usize) ![]const []const u8 {
    var start_buf: keys.Buf = undefined;
    var key_buf: keys.Buf = undefined;
    const bounds = keys.Prefix.actor.bounds();
    const start: []const u8 = if (after_did.len == 0) &bounds[0] else successor(&start_buf, try keys.actor(&key_buf, after_did));
    const found = try collect(db, arena, start, &bounds[1], limit);
    const dids = try arena.alloc([]const u8, found.keys.items.len);
    for (dids, found.keys.items) |*did, key| did.* = try keys.parseActor(key);
    return dids;
}

const Collect = struct {
    arena: std.mem.Allocator,
    keys: std.ArrayList([]const u8) = .empty,
    values: std.ArrayList([]const u8) = .empty,

    pub fn entry(self: *Collect, key: []const u8, value: []const u8) !void {
        try self.keys.append(self.arena, try self.arena.dupe(u8, key));
        try self.values.append(self.arena, try self.arena.dupe(u8, value));
    }
};

fn collect(db: *Db, arena: std.mem.Allocator, start: []const u8, end: []const u8, limit: usize) !Collect {
    var out: Collect = .{ .arena = arena };
    _ = try scan.all(arena, db, start, end, .{ .limit = limit }, &out);
    return out;
}

fn successor(buf: *keys.Buf, key: []const u8) []const u8 {
    @memcpy(buf[0..key.len], key);
    buf[key.len] = 0;
    return buf[0 .. key.len + 1];
}

/// Every live DID holding `handle`, compared without case, in ascending DID
/// order. More than one account can claim the same handle value.
pub fn byHandle(db: *Db, arena: std.mem.Allocator, handle: []const u8) ![]const []const u8 {
    var start_buf: keys.Buf = undefined;
    var end_buf: keys.Buf = undefined;
    const start = try keys.handlePrefix(&start_buf, handle);
    @memcpy(end_buf[0..start.len], start);
    end_buf[start.len - 1] = 1;
    const found = try collect(db, arena, start, end_buf[0..start.len], std.math.maxInt(usize));
    const dids = try arena.alloc([]const u8, found.keys.items.len);
    for (dids, found.keys.items) |*did, key| did.* = (try keys.parseHandle(key)).did;
    return dids;
}

pub const Timed = keys.Timed;

/// A page of a queue (`.identity`, `.refresh`) or of the tombstones, in
/// ascending time then DID, holding entries at or before `until`. Pass the
/// last entry of a page as `after` to continue.
pub fn timed(db: *Db, arena: std.mem.Allocator, prefix: keys.Prefix, after: ?Timed, until: u64, limit: usize) ![]const Timed {
    var start_buf: keys.Buf = undefined;
    var end_buf: keys.Buf = undefined;
    const start = if (after) |last| successor(&start_buf, try keys.timed(&end_buf, prefix, last.at, last.did)) else keys.timedBound(&start_buf, prefix, 0);
    return timedRange(db, arena, prefix, start, until, limit);
}

/// `timed` for a caller that knows no entry lies before `from`: the scan
/// starts there instead of at time 0, so it does not read the keys that
/// earlier entries left behind and no compaction has dropped yet.
pub fn timedFrom(db: *Db, arena: std.mem.Allocator, prefix: keys.Prefix, from: u64, until: u64, limit: usize) ![]const Timed {
    var start_buf: keys.Buf = undefined;
    return timedRange(db, arena, prefix, keys.timedBound(&start_buf, prefix, from), until, limit);
}

fn timedRange(db: *Db, arena: std.mem.Allocator, prefix: keys.Prefix, start: []const u8, until: u64, limit: usize) ![]const Timed {
    var end_buf: keys.Buf = undefined;
    const bounds = prefix.bounds();
    const end: []const u8 = if (until == std.math.maxInt(u64)) &bounds[1] else keys.timedBound(&end_buf, prefix, until + 1);
    const found = try collect(db, arena, start, end, limit);
    const out = try arena.alloc(Timed, found.keys.items.len);
    for (out, found.keys.items) |*slot, key| slot.* = try keys.parseTimed(key, prefix);
    return out;
}

pub fn queueState(db: *Db, arena: std.mem.Allocator, entry: Timed) !?record.QueueState {
    var buf: keys.Buf = undefined;
    const borrowed = try db.get(try keys.timed(&buf, .identity, entry.at, entry.did)) orelse return null;
    return try record.QueueState.decode(try arena.dupe(u8, borrowed));
}

pub const Quality = keys.Quality;

/// A page of the quality walk: live, visible actors with a handle and a
/// positive score, highest score first, ties by DID.
pub fn quality(db: *Db, arena: std.mem.Allocator, after: ?Quality, limit: usize) ![]const Quality {
    var start_buf: keys.Buf = undefined;
    var scratch: keys.Buf = undefined;
    const bounds = keys.Prefix.quality.bounds();
    const start: []const u8 = if (after) |last| successor(&start_buf, try keys.quality(&scratch, last.score, last.did)) else &bounds[0];
    const found = try collect(db, arena, start, &bounds[1], limit);
    const out = try arena.alloc(Quality, found.keys.items.len);
    for (out, found.keys.items) |*slot, key| slot.* = try keys.parseQuality(key);
    return out;
}

pub const ChangeEntry = struct { sequence: u64, did: []const u8 };

pub const Changes = union(enum) {
    /// Entries at or after the requested position, in commit order. The
    /// consumer's next position is the last sequence plus one.
    entries: []const ChangeEntry,
    /// The requested position is below the log's floor: entries the consumer
    /// has not seen are gone. It must rebuild from a scan of the actors and
    /// continue from the floor given here.
    trimmed: u64,
};

pub fn changes(db: *Db, arena: std.mem.Allocator, from: u64, limit: usize) !Changes {
    const floor = try counter(db, .change_floor) orelse 0;
    if (from < floor) return .{ .trimmed = floor };
    var start_buf: keys.Buf = undefined;
    const bounds = keys.Prefix.change.bounds();
    const found = try collect(db, arena, keys.change(&start_buf, from), &bounds[1], limit);
    const out = try arena.alloc(ChangeEntry, found.keys.items.len);
    for (out, found.keys.items, found.values.items) |*slot, key, value| slot.* = .{ .sequence = try keys.parseChange(key), .did = value };
    return .{ .entries = out };
}

pub const Cursors = struct {
    relay: ?u64 = null,
    labeler: ?u64 = null,
    plc: ?[]const u8 = null,
    bridge: ?u64 = null,
    /// The control database the two positions below were read from.
    control_generation: ?u64 = null,
    /// The policy revision last applied from the Worker's control record;
    /// null before the first.
    control_policy: ?u64 = null,
    /// The last indexing request taken from the control record.
    control_requests: ?u64 = null,
    /// The sequence the next change-log entry will take.
    next_change: u64 = 1,
    /// The lowest position a change-log consumer may still ask for.
    change_floor: u64 = 0,
};

pub fn counter(db: *Db, which: keys.Cursor) !?u64 {
    var buf: keys.Buf = undefined;
    const borrowed = try db.get(keys.cursor(&buf, which)) orelse return null;
    return try record.decodeU64(borrowed);
}

pub fn cursors(db: *Db, arena: std.mem.Allocator) !Cursors {
    var buf: keys.Buf = undefined;
    var out: Cursors = .{
        .relay = try counter(db, .relay),
        .labeler = try counter(db, .labeler),
        .bridge = try counter(db, .bridge),
        .control_generation = try counter(db, .control_generation),
        .control_policy = try counter(db, .control_policy),
        .control_requests = try counter(db, .control_requests),
        .next_change = try counter(db, .next_change) orelse 1,
        .change_floor = try counter(db, .change_floor) orelse 0,
    };
    if (try db.get(keys.cursor(&buf, .plc))) |borrowed| out.plc = try arena.dupe(u8, borrowed);
    return out;
}

/// Where a refresh walk stands; the default before its first step.
pub fn walkState(db: *Db, arena: std.mem.Allocator, which: keys.Cursor) !record.WalkState {
    var buf: keys.Buf = undefined;
    const borrowed = try db.get(keys.cursor(&buf, which)) orelse return .{};
    return record.WalkState.decode(try arena.dupe(u8, borrowed));
}

/// Where the handle repair stands; the default before its first step.
pub fn repairState(db: *Db, arena: std.mem.Allocator) !record.RepairState {
    var buf: keys.Buf = undefined;
    const borrowed = try db.get(keys.cursor(&buf, .handle_repair)) orelse return .{};
    return record.RepairState.decode(try arena.dupe(u8, borrowed));
}

/// Where the first-check pass stands; the default before its first step.
pub fn firstCheckState(db: *Db, arena: std.mem.Allocator) !record.FirstCheckState {
    var buf: keys.Buf = undefined;
    const borrowed = try db.get(keys.cursor(&buf, .first_check)) orelse return .{};
    return record.FirstCheckState.decode(try arena.dupe(u8, borrowed));
}

pub const LabelRow = struct {
    source: []const u8,
    value: []const u8,
    subject: keys.Subject,
    stored: record.StoredLabel,
};

/// The label range of one account, negations included, in key order.
pub fn labels(db: *Db, arena: std.mem.Allocator, did: []const u8) ![]const LabelRow {
    var start_buf: keys.Buf = undefined;
    var end_buf: keys.Buf = undefined;
    const start = try keys.labelPrefix(&start_buf, did);
    @memcpy(end_buf[0..start.len], start);
    end_buf[start.len - 1] = 1;
    const found = try collect(db, arena, start, end_buf[0..start.len], std.math.maxInt(usize));
    const out = try arena.alloc(LabelRow, found.keys.items.len);
    for (out, found.keys.items, found.values.items) |*slot, key, value| {
        const parsed = try keys.parseLabel(key);
        slot.* = .{ .source = parsed.source, .value = parsed.value, .subject = parsed.subject, .stored = try record.StoredLabel.decode(value) };
    }
    return out;
}

pub fn override(db: *Db, arena: std.mem.Allocator, did: []const u8) !?record.PolicyEntry {
    var buf: keys.Buf = undefined;
    const borrowed = try db.get(try keys.override(&buf, did)) orelse return null;
    return try record.PolicyEntry.decode(try arena.dupe(u8, borrowed));
}

pub const Policy = struct { subject: []const u8, entry: record.PolicyEntry };

pub fn blockedDomains(db: *Db, arena: std.mem.Allocator) ![]const Policy {
    const found = try collect(db, arena, &keys.domain_bounds[0], &keys.domain_bounds[1], std.math.maxInt(usize));
    const out = try arena.alloc(Policy, found.keys.items.len);
    for (out, found.keys.items, found.values.items) |*slot, key, value| slot.* = .{ .subject = try keys.parseDomain(key), .entry = try record.PolicyEntry.decode(value) };
    return out;
}

pub fn overrides(db: *Db, arena: std.mem.Allocator) ![]const Policy {
    const found = try collect(db, arena, &keys.override_bounds[0], &keys.override_bounds[1], std.math.maxInt(usize));
    const out = try arena.alloc(Policy, found.keys.items.len);
    for (out, found.keys.items, found.values.items) |*slot, key, value| slot.* = .{ .subject = try keys.parseOverride(key), .entry = try record.PolicyEntry.decode(value) };
    return out;
}

/// Domains whose sweep of the handle range is owed, in key order.
pub fn owedSweeps(db: *Db, arena: std.mem.Allocator) ![]const []const u8 {
    const found = try collect(db, arena, &keys.owed_sweep_bounds[0], &keys.owed_sweep_bounds[1], std.math.maxInt(usize));
    const out = try arena.alloc([]const u8, found.keys.items.len);
    for (out, found.keys.items) |*slot, key| slot.* = try keys.parseOwedSweep(key);
    return out;
}

pub const Counts = std.EnumArray(keys.Prefix, u64);

/// Entries in every range. This scans the whole store: 19 s for 45 million
/// entries in the 2026-11-10 measurement. Run it from a reader, on a schedule.
pub fn counts(gpa: std.mem.Allocator, db: *Db, options: scan.Options) !Counts {
    var out: Counts = .initFill(0);
    for (keys.prefixes) |prefix| {
        const bounds = prefix.bounds();
        var count: scan.Count = .{};
        _ = try scan.all(gpa, db, &bounds[0], &bounds[1], options, &count);
        out.set(prefix, count.entries);
    }
    return out;
}
