//! Candidate merge for the index serving path. Pure in-memory logic — the
//! serving layer does the SQLite I/O to produce the inputs (base DID lists
//! from prefix_top_base + overlay rows from prefix_overlay), then calls
//! `combine` to fold them into a final ranked, deduped, capped DID list.
//!
//! Semantics (see docs/typeahead-index-design.md "serving path"):
//!   - single-token query → keys [h:X, e:X, n:X] → UNION (a hit under any
//!     key qualifies; score = max across keys)
//!   - multi-token query  → keys [n:tok1, n:tok2, …] → INTERSECT (a DID must
//!     appear under EVERY token; score = sum across tokens)
//!   - per key, prefix_overlay is applied first: op=0 suppresses a base DID,
//!     op=1 adds/rescores it. Overlay always wins over base for that key.
const std = @import("std");
const Allocator = std.mem.Allocator;

/// A DID with its relevance score. `did` is borrowed from the caller's
/// decoded blobs / overlay rows (arena-owned by the serving path).
pub const Scored = struct {
    did: []const u8,
    score: f32,
};

/// A prefix_overlay row for one key. op=1 → add/rescore, op=0 → suppress.
pub const OverlayEntry = struct {
    did: []const u8,
    score: f32,
    op: u8,
};

/// One query key's inputs: the base DID list (from prefix_top_base) and the
/// overlay rows (from prefix_overlay) for that key.
pub const KeySet = struct {
    base: []const Scored,
    overlay: []const OverlayEntry = &.{},
};

pub const Mode = enum { union_any, intersect_all };

/// Pick the merge mode from query shape: a whitespace query is multi-token
/// (intersect — every token must match); otherwise union the h:/e:/n: keys.
pub fn modeForQuery(normalized_query: []const u8) Mode {
    return if (std.mem.indexOfScalar(u8, normalized_query, ' ') != null)
        .intersect_all
    else
        .union_any;
}

/// Fold per-key candidate sets into a ranked, deduped, capped DID list.
/// Allocates working maps + the output slice in `arena`.
pub fn combine(arena: Allocator, keysets: []const KeySet, mode: Mode, cap: usize) ![]Scored {
    if (keysets.len == 0) return &.{};

    const Acc = struct { score: f32, count: usize };
    var acc: std.StringHashMapUnmanaged(Acc) = .empty;

    for (keysets) |ks| {
        // Resolve this key: base with overlay applied (op=0 drop, op=1 set).
        var keymap: std.StringHashMapUnmanaged(f32) = .empty;
        for (ks.base) |b| try keymap.put(arena, b.did, b.score);
        for (ks.overlay) |o| {
            if (o.op == 0) {
                _ = keymap.remove(o.did);
            } else {
                try keymap.put(arena, o.did, o.score);
            }
        }

        // Accumulate this key's contribution into the cross-key accumulator.
        var it = keymap.iterator();
        while (it.next()) |e| {
            const gop = try acc.getOrPut(arena, e.key_ptr.*);
            if (!gop.found_existing) {
                gop.value_ptr.* = .{ .score = e.value_ptr.*, .count = 1 };
            } else {
                gop.value_ptr.count += 1;
                gop.value_ptr.score = switch (mode) {
                    .union_any => @max(gop.value_ptr.score, e.value_ptr.*),
                    .intersect_all => gop.value_ptr.score + e.value_ptr.*,
                };
            }
        }
    }

    // Collect qualifying DIDs: union takes everything; intersect requires a
    // hit under every key.
    var out: std.ArrayListUnmanaged(Scored) = .empty;
    var it = acc.iterator();
    while (it.next()) |e| {
        const qualifies = switch (mode) {
            .union_any => true,
            .intersect_all => e.value_ptr.count == keysets.len,
        };
        if (qualifies) try out.append(arena, .{ .did = e.key_ptr.*, .score = e.value_ptr.score });
    }

    const slice = try out.toOwnedSlice(arena);
    std.mem.sort(Scored, slice, {}, scoreDesc);
    return if (slice.len > cap) slice[0..cap] else slice;
}

fn scoreDesc(_: void, a: Scored, b: Scored) bool {
    if (a.score != b.score) return a.score > b.score;
    return std.mem.order(u8, a.did, b.did) == .lt; // stable tiebreak
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

fn findScore(out: []const Scored, did: []const u8) ?f32 {
    for (out) |s| if (std.mem.eql(u8, s.did, did)) return s.score;
    return null;
}

test "modeForQuery" {
    try testing.expectEqual(Mode.union_any, modeForQuery("nate"));
    try testing.expectEqual(Mode.intersect_all, modeForQuery("fig aka"));
}

test "union: single-token across h/e/n keys, max score, dedup" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    // "nate" → h:nate, e:nate, n:nate. DID_A under h: (0.9) and n: (0.5);
    // DID_B only under n: (0.7).
    const keysets = [_]KeySet{
        .{ .base = &.{.{ .did = "A", .score = 0.9 }} }, // h:nate
        .{ .base = &.{} }, // e:nate (miss)
        .{ .base = &.{ .{ .did = "A", .score = 0.5 }, .{ .did = "B", .score = 0.7 } } }, // n:nate
    };
    const out = try combine(a, &keysets, .union_any, 10);
    try testing.expectEqual(@as(usize, 2), out.len);
    try testing.expectEqual(@as(f32, 0.9), findScore(out, "A").?); // max(0.9, 0.5)
    try testing.expectEqual(@as(f32, 0.7), findScore(out, "B").?);
    try testing.expectEqualStrings("A", out[0].did); // 0.9 ranks first
}

test "intersect: multi-token requires hit under every token, sums score" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    // "fig aka": n:fig → {A:0.4, B:0.6, C:0.3}; n:aka → {B:0.5, C:0.2}.
    // Only B and C appear under BOTH.
    const keysets = [_]KeySet{
        .{ .base = &.{ .{ .did = "A", .score = 0.4 }, .{ .did = "B", .score = 0.6 }, .{ .did = "C", .score = 0.3 } } },
        .{ .base = &.{ .{ .did = "B", .score = 0.5 }, .{ .did = "C", .score = 0.2 } } },
    };
    const out = try combine(a, &keysets, .intersect_all, 10);
    try testing.expectEqual(@as(usize, 2), out.len);
    try testing.expectEqual(@as(f32, 1.1), findScore(out, "B").?); // 0.6 + 0.5
    try testing.expectEqual(@as(f32, 0.5), findScore(out, "C").?); // 0.3 + 0.2
    try testing.expect(findScore(out, "A") == null); // not under n:aka
    try testing.expectEqualStrings("B", out[0].did);
}

test "overlay op=0 suppresses a base DID for that key" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    const keysets = [_]KeySet{.{
        .base = &.{ .{ .did = "A", .score = 0.9 }, .{ .did = "B", .score = 0.8 } },
        .overlay = &.{.{ .did = "A", .score = 0, .op = 0 }}, // suppress A
    }};
    const out = try combine(a, &keysets, .union_any, 10);
    try testing.expectEqual(@as(usize, 1), out.len);
    try testing.expectEqualStrings("B", out[0].did);
}

test "overlay op=1 adds a new DID and rescores an existing one" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    const keysets = [_]KeySet{.{
        .base = &.{.{ .did = "A", .score = 0.2 }},
        .overlay = &.{ .{ .did = "A", .score = 0.95, .op = 1 }, .{ .did = "NEW", .score = 0.5, .op = 1 } },
    }};
    const out = try combine(a, &keysets, .union_any, 10);
    try testing.expectEqual(@as(usize, 2), out.len);
    try testing.expectEqual(@as(f32, 0.95), findScore(out, "A").?); // overlay rescored
    try testing.expectEqual(@as(f32, 0.5), findScore(out, "NEW").?);
    try testing.expectEqualStrings("A", out[0].did);
}

test "intersect: overlay-suppressed DID drops out of the intersection" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    // B under both tokens, but suppressed (deleted/hidden) under the first.
    const keysets = [_]KeySet{
        .{ .base = &.{.{ .did = "B", .score = 0.6 }}, .overlay = &.{.{ .did = "B", .score = 0, .op = 0 }} },
        .{ .base = &.{.{ .did = "B", .score = 0.5 }} },
    };
    const out = try combine(a, &keysets, .intersect_all, 10);
    try testing.expectEqual(@as(usize, 0), out.len);
}

test "cap truncates to top-N by score" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    const keysets = [_]KeySet{.{ .base = &.{
        .{ .did = "A", .score = 0.1 },
        .{ .did = "B", .score = 0.9 },
        .{ .did = "C", .score = 0.5 },
    } }};
    const out = try combine(a, &keysets, .union_any, 2);
    try testing.expectEqual(@as(usize, 2), out.len);
    try testing.expectEqualStrings("B", out[0].did);
    try testing.expectEqualStrings("C", out[1].did);
}

test "empty keysets → empty result" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const out = try combine(arena.allocator(), &.{}, .union_any, 10);
    try testing.expectEqual(@as(usize, 0), out.len);
}
