//! Full-scan checks of a store against the model and against itself.

const std = @import("std");
const change = @import("../change.zig");
const engine = @import("../engine.zig");
const keys = @import("../keys.zig");
const read = @import("../read.zig");
const record = @import("../record.zig");
const scan = @import("../scan.zig");
const Model = @import("model.zig").Model;
const Actor = record.Actor;

pub const Failed = error{VerificationFailed};

fn ensure(ok: bool, comptime fmt: []const u8, args: anytype) Failed!void {
    if (ok) return;
    std.debug.print("verification failed: " ++ fmt ++ "\n", args);
    return error.VerificationFailed;
}

const Pair = struct { key: []const u8, value: []const u8 };

const Collect = struct {
    arena: std.mem.Allocator,
    pairs: std.ArrayList(Pair) = .empty,

    pub fn entry(self: *Collect, key: []const u8, value: []const u8) !void {
        try self.pairs.append(self.arena, .{ .key = try self.arena.dupe(u8, key), .value = try self.arena.dupe(u8, value) });
    }
};

fn range(arena: std.mem.Allocator, db: *engine.Db, start: []const u8, end: []const u8) ![]const Pair {
    var out: Collect = .{ .arena = arena };
    _ = try scan.all(arena, db, start, end, .{ .page = 257 }, &out);
    return out.pairs.items;
}

fn prefixRange(arena: std.mem.Allocator, db: *engine.Db, prefix: keys.Prefix) ![]const Pair {
    const bounds = prefix.bounds();
    return range(arena, db, &bounds[0], &bounds[1]);
}

fn keyBefore(_: void, a: Pair, b: Pair) bool {
    return std.mem.order(u8, a.key, b.key) == .lt;
}

fn sameKeys(name: []const u8, expected: []Pair, found: []const Pair) !void {
    std.mem.sort(Pair, expected, {}, keyBefore);
    try ensure(expected.len == found.len, "{s} range holds {d} keys, the actor records imply {d}", .{ name, found.len, expected.len });
    for (expected, found) |want, got| {
        try ensure(std.mem.eql(u8, want.key, got.key), "{s} range holds {x}, the actor records imply {x}", .{ name, got.key, want.key });
    }
}

pub const Summary = struct {
    actors: usize = 0,
    live: usize = 0,
    handles: usize = 0,
    ranked: usize = 0,
    tombstones: usize = 0,
    refresh: usize = 0,
    identity: usize = 0,
    labels: usize = 0,
    changes: usize = 0,
};

/// Scan every range and check that the derived ranges are exactly what the
/// actor records imply, that the records and everything else equal the
/// model, and that the change log is the model's, in order.
pub fn check(gpa: std.mem.Allocator, db: *engine.Db, model: *const Model) !Summary {
    var arena_state: std.heap.ArenaAllocator = .init(gpa);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var summary: Summary = .{};
    var buf: keys.Buf = undefined;

    const stored = try prefixRange(arena, db, .actor);
    try ensure(stored.len == model.actors.count(), "store holds {d} actors, model {d}", .{ stored.len, model.actors.count() });
    summary.actors = stored.len;

    var implied: std.EnumArray(keys.Prefix, std.ArrayList(Pair)) = .initFill(.empty);
    for (stored) |pair| {
        const did = try keys.parseActor(pair.key);
        const actor = try Actor.decode(arena, pair.value);
        const want = model.actors.get(did) orelse {
            try ensure(false, "store holds {s}, the model does not", .{did});
            unreachable;
        };
        if (!actor.eql(want)) {
            std.debug.print("store: {any}\nmodel: {any}\n", .{ actor, want });
            try ensure(false, "actor {s} differs from the model", .{did});
        }
        try ensure(actor.live() or (actor.identity_due == 0 and actor.refresh_due == 0), "gone actor {s} is queued", .{did});
        try ensure(actor.live() == (actor.deleted_at == 0), "actor {s} status and deletion time disagree", .{did});
        if (actor.live()) summary.live += 1;
        if (actor.live() and actor.handle.len > 0) try implied.getPtr(.handle).append(arena, .{ .key = try arena.dupe(u8, try keys.handle(&buf, actor.handle, did)), .value = "" });
        if (actor.ranked()) try implied.getPtr(.quality).append(arena, .{ .key = try arena.dupe(u8, try keys.quality(&buf, actor.quality_score, did)), .value = "" });
        if (!actor.live()) try implied.getPtr(.tombstone).append(arena, .{ .key = try arena.dupe(u8, try keys.timed(&buf, .tombstone, actor.deleted_at, did)), .value = "" });
        if (actor.refresh_due != 0) try implied.getPtr(.refresh).append(arena, .{ .key = try arena.dupe(u8, try keys.timed(&buf, .refresh, actor.refresh_due, did)), .value = "" });
        if (actor.identity_due != 0) try implied.getPtr(.identity).append(arena, .{ .key = try arena.dupe(u8, try keys.timed(&buf, .identity, actor.identity_due, did)), .value = "" });

        const policy = try read.override(db, arena, did);
        try ensure(actor.override == (if (policy) |p| p.action else .none), "actor {s} override copy disagrees with the policy range", .{did});

        var moderated: usize = 0;
        for (actor.labels) |one| moderated += @intFromBool(std.mem.eql(u8, one.source, model.source));
        var asserted: usize = 0;
        for (try read.labels(db, arena, did)) |row| {
            if (row.stored.negated) continue;
            asserted += 1;
            const uri = try change.subjectUri(arena, did, row.subject);
            const projected = for (actor.labels) |one| {
                if (std.mem.eql(u8, one.source, row.source) and std.mem.eql(u8, one.value, row.value) and std.mem.eql(u8, one.uri, uri)) break one;
            } else {
                try ensure(false, "actor {s} is missing label {s} from its label range", .{ did, row.value });
                unreachable;
            };
            try ensure(std.mem.eql(u8, projected.created, row.stored.created) and std.mem.eql(u8, projected.expires, row.stored.expires) and std.mem.eql(u8, projected.cid, row.stored.cid) and !projected.negated, "actor {s} label {s} differs from its label range", .{ did, row.value });
        }
        try ensure(moderated == asserted, "actor {s} carries {d} moderation labels, its label range asserts {d}", .{ did, moderated, asserted });
    }

    for ([_]keys.Prefix{ .handle, .quality, .tombstone, .refresh, .identity }) |prefix| {
        const found = try prefixRange(arena, db, prefix);
        try sameKeys(@tagName(prefix), implied.getPtr(prefix).items, found);
        for (found) |pair| {
            if (prefix != .identity) {
                try ensure(pair.value.len == 0, "{s} key carries a value", .{@tagName(prefix)});
                continue;
            }
            const entry = try keys.parseTimed(pair.key, .identity);
            const state = try record.QueueState.decode(pair.value);
            const want = model.queue.get(entry.did) orelse {
                try ensure(false, "identity queue holds {s}, the model does not", .{entry.did});
                unreachable;
            };
            try ensure(state.generation == want.generation and state.attempts == want.attempts and state.reason == want.reason and
                state.enqueued_at == want.enqueued_at and std.mem.eql(u8, state.last_error, want.last_error), "identity queue state of {s} differs: {any} vs {any}", .{ entry.did, state, want });
        }
    }
    summary.handles = implied.get(.handle).items.len;
    summary.ranked = implied.get(.quality).items.len;
    summary.tombstones = implied.get(.tombstone).items.len;
    summary.refresh = implied.get(.refresh).items.len;
    summary.identity = implied.get(.identity).items.len;
    try ensure(summary.identity == model.queue.count(), "identity queue holds {d}, model {d}", .{ summary.identity, model.queue.count() });

    var want_labels: std.ArrayList(Pair) = .empty;
    var encoded: std.ArrayList(u8) = .empty;
    for (model.labels.keys(), model.labels.values()) |did, rows| for (rows.items) |row| {
        try row.stored.encode(arena, &encoded);
        try want_labels.append(arena, .{ .key = try arena.dupe(u8, try keys.label(&buf, did, row.source, row.value, row.subject)), .value = try arena.dupe(u8, encoded.items) });
    };
    const found_labels = try prefixRange(arena, db, .label);
    try sameKeys("label", want_labels.items, found_labels);
    for (want_labels.items, found_labels) |want, got| try ensure(std.mem.eql(u8, want.value, got.value), "label {x} differs from the model", .{got.key});
    summary.labels = found_labels.len;

    const found_overrides = try read.overrides(db, arena);
    try ensure(found_overrides.len == model.overrides.count(), "store holds {d} overrides, model {d}", .{ found_overrides.len, model.overrides.count() });
    for (found_overrides) |policy| {
        const want = model.overrides.get(policy.subject) orelse return error.VerificationFailed;
        try ensure(policy.entry.action == want.action and policy.entry.created_at == want.created_at and std.mem.eql(u8, policy.entry.reason, want.reason), "override of {s} differs", .{policy.subject});
    }
    const found_domains = try read.blockedDomains(db, arena);
    try ensure(found_domains.len == model.domains.count(), "store holds {d} blocked domains, model {d}", .{ found_domains.len, model.domains.count() });
    for (found_domains) |policy| {
        const want = model.domains.get(policy.subject) orelse return error.VerificationFailed;
        try ensure(policy.entry.created_at == want.created_at and std.mem.eql(u8, policy.entry.reason, want.reason), "blocked domain {s} differs", .{policy.subject});
    }

    const log = try prefixRange(arena, db, .change);
    const first = @max(model.floor, 1);
    try ensure(log.len == model.nextChange() - first, "change log holds {d} entries, model {d}", .{ log.len, model.nextChange() - first });
    for (log, first..) |pair, sequence| {
        try ensure(try keys.parseChange(pair.key) == sequence, "change log has a gap at {d}", .{sequence});
        try ensure(std.mem.eql(u8, pair.value, model.log.items[sequence - 1]), "change {d} is {s}, model {s}", .{ sequence, pair.value, model.log.items[sequence - 1] });
    }
    summary.changes = log.len;

    const cursors = try read.cursors(db, arena);
    try ensure(cursors.next_change == model.nextChange(), "next change is {d}, model {d}", .{ cursors.next_change, model.nextChange() });
    try ensure(cursors.change_floor == model.floor, "change floor is {d}, model {d}", .{ cursors.change_floor, model.floor });
    try ensure(std.meta.eql(cursors.relay, model.relay), "relay cursor is {?d}, model {?d}", .{ cursors.relay, model.relay });
    try ensure(std.meta.eql(cursors.labeler, model.labeler), "labeler cursor is {?d}, model {?d}", .{ cursors.labeler, model.labeler });
    try ensure(std.meta.eql(cursors.bridge, model.bridge), "bridge cursor is {?d}, model {?d}", .{ cursors.bridge, model.bridge });
    try ensure((cursors.plc == null) == (model.plc == null) and (cursors.plc == null or std.mem.eql(u8, cursors.plc.?, model.plc.?)), "plc cursor is {?s}, model {?s}", .{ cursors.plc, model.plc });

    const counts = try read.counts(gpa, db, .{ .page = 311 });
    var total: u64 = 0;
    for (keys.prefixes) |prefix| total += counts.get(prefix);
    const everything = try range(arena, db, "", "\xff\xff");
    try ensure(everything.len == total, "store holds {d} keys, its ranges {d}", .{ everything.len, total });
    try ensure(counts.get(.actor) == summary.actors and counts.get(.handle) == summary.handles and counts.get(.quality) == summary.ranked and
        counts.get(.tombstone) == summary.tombstones and counts.get(.refresh) == summary.refresh and counts.get(.identity) == summary.identity and
        counts.get(.label) == summary.labels and counts.get(.change) == summary.changes and
        counts.get(.policy) == found_overrides.len + found_domains.len, "range counts disagree with the scans", .{});
    return summary;
}

fn didBefore(_: void, a: []const u8, b: []const u8) bool {
    return std.mem.order(u8, a, b) == .lt;
}

const Due = struct {
    at: u64,
    did: []const u8,

    fn before(_: void, a: Due, b: Due) bool {
        return if (a.at != b.at) a.at < b.at else std.mem.order(u8, a.did, b.did) == .lt;
    }
};

const Ranked = struct {
    score: f32,
    did: []const u8,

    fn before(_: void, a: Ranked, b: Ranked) bool {
        return if (a.score != b.score) a.score > b.score else std.mem.order(u8, a.did, b.did) == .lt;
    }
};

/// Check the read API of a writer or reader handle against the model.
pub fn checkReads(gpa: std.mem.Allocator, handle: anytype, model: *const Model, random: std.Random) !void {
    var arena_state: std.heap.ArenaAllocator = .init(gpa);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const dids = model.actors.keys();
    const fetched = try handle.actors(arena, dids);
    for (dids, fetched, model.actors.values()) |did, got, want| {
        try ensure(got != null and got.?.eql(want), "batch read of {s} differs from the model", .{did});
        const single = try handle.actor(arena, did) orelse return error.VerificationFailed;
        try ensure(single.eql(want), "read of {s} differs from the model", .{did});
    }
    try ensure(try handle.actor(arena, "did:plc:nobodyhasthisdidatall000") == null, "an unknown DID was found", .{});

    for (model.actors.values()) |holder| {
        if (holder.status != .live or holder.handle.len == 0) continue;
        var expected: std.ArrayList([]const u8) = .empty;
        for (dids, model.actors.values()) |did, other| {
            if (other.status == .live and std.ascii.eqlIgnoreCase(other.handle, holder.handle)) try expected.append(arena, did);
        }
        std.mem.sort([]const u8, expected.items, {}, didBefore);
        const asked = try arena.dupe(u8, holder.handle);
        for (asked) |*c| c.* = if (random.boolean()) std.ascii.toUpper(c.*) else std.ascii.toLower(c.*);
        const got = try handle.byHandle(arena, asked);
        try ensure(got.len == expected.items.len, "handle {s} resolves to {d} DIDs, model {d}", .{ asked, got.len, expected.items.len });
        for (got, expected.items) |a, b| try ensure(std.mem.eql(u8, a, b), "handle {s} resolves to {s}, model {s}", .{ asked, a, b });
    }
    try ensure((try handle.byHandle(arena, "nobody.has.this.handle")).len == 0, "an unknown handle resolved", .{});

    var ranked: std.ArrayList(Ranked) = .empty;
    for (dids, model.actors.values()) |did, actor| if (actor.ranked()) try ranked.append(arena, .{ .score = actor.quality_score, .did = did });
    std.mem.sort(Ranked, ranked.items, {}, Ranked.before);
    var walked: usize = 0;
    var after: ?read.Quality = null;
    while (true) {
        const page = try handle.quality(arena, after, 7);
        for (page) |got| {
            try ensure(walked < ranked.items.len, "the quality walk is longer than the model's", .{});
            const want = ranked.items[walked];
            try ensure(got.score == want.score and std.mem.eql(u8, got.did, want.did), "quality walk position {d} is {s} at {d}, model {s} at {d}", .{ walked, got.did, got.score, want.did, want.score });
            walked += 1;
        }
        if (page.len < 7) break;
        after = page[page.len - 1];
    }
    try ensure(walked == ranked.items.len, "the quality walk holds {d}, model {d}", .{ walked, ranked.items.len });

    for ([_]keys.Prefix{ .identity, .refresh, .tombstone }) |prefix| {
        var due: std.ArrayList(Due) = .empty;
        for (dids, model.actors.values()) |did, actor| {
            const at = switch (prefix) {
                .identity => actor.identity_due,
                .refresh => actor.refresh_due,
                else => actor.deleted_at,
            };
            if (at != 0) try due.append(arena, .{ .at = at, .did = did });
        }
        std.mem.sort(Due, due.items, {}, Due.before);
        for ([_]u64{ std.math.maxInt(u64), model.now }) |until| {
            var at: usize = 0;
            var last: ?read.Timed = null;
            while (true) {
                const page = try handle.timed(arena, prefix, last, until, 5);
                for (page) |got| {
                    try ensure(at < due.items.len and got.at == due.items[at].at and std.mem.eql(u8, got.did, due.items[at].did), "{s} walk position {d} differs from the model", .{ @tagName(prefix), at });
                    try ensure(got.at <= until, "{s} walk returned an entry past its bound", .{@tagName(prefix)});
                    at += 1;
                }
                if (page.len < 5) break;
                last = page[page.len - 1];
            }
            var expected: usize = 0;
            for (due.items) |entry| expected += @intFromBool(entry.at <= until);
            try ensure(at == expected, "{s} walk to {d} holds {d}, model {d}", .{ @tagName(prefix), until, at, expected });
        }
    }

    var position: u64 = model.floor;
    while (true) {
        const page = switch (try handle.changes(arena, position, 9)) {
            .entries => |entries| entries,
            .trimmed => return error.VerificationFailed,
        };
        for (page) |entry| {
            try ensure(entry.sequence >= position and std.mem.eql(u8, entry.did, model.log.items[entry.sequence - 1]), "change {d} differs from the model", .{entry.sequence});
            position = entry.sequence + 1;
        }
        if (page.len < 9) break;
    }
    try ensure(position == model.nextChange() or (model.nextChange() == 1 and position <= 1), "the change log ends at {d}, model {d}", .{ position, model.nextChange() });
    if (model.floor > 0) {
        const answer = try handle.changes(arena, model.floor - 1, 9);
        try ensure(answer == .trimmed and answer.trimmed == model.floor, "a trimmed position was not reported", .{});
    }

    const cursors = try handle.cursors(arena);
    try ensure(std.meta.eql(cursors.relay, model.relay) and cursors.next_change == model.nextChange() and cursors.change_floor == model.floor, "cursors differ from the model", .{});
}
