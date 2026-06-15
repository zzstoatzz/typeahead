//! Index-backed `/search` (Milestone B). Serves from the ATTACHed prefix
//! snapshot + the live overlay, behind USE_PREFIX_INDEX. Response shape is
//! byte-identical to the FTS path (reuses search.Candidate + search.writeActor).
//!
//! Flow: normalize → queryKeys → per-key (base prefix_top_base ∪ prefix_overlay)
//! → merge (union single-token / intersect multi-token) → exact-handle
//! promotion → hydrate (overlay actor wins; tombstone/hidden suppress; live
//! `actors` safety belt) → response.
//!
//! Errors propagate to the caller, which maps them to 500/503 so the worker
//! falls back to its bsky backstop — never silently serve a wrong/empty page.
const std = @import("std");
const json = std.json;
const Allocator = std.mem.Allocator;

const LocalDb = @import("../db/LocalDb.zig");
const searchmod = @import("../search.zig");
const merge = @import("merge.zig");
const pack = @import("pack.zig");
const prefixes = @import("prefixes.zig");
const normalize = @import("normalize.zig");

const log = std.log.scoped(.index_search);

/// Max candidates carried from merge into hydrate. The final `limit` (≤100)
/// trims after moderation filtering drops tombstoned/hidden rows.
const HYDRATE_CAP = 50;

pub fn run(local: *LocalDb, allocator: Allocator, raw_query: []const u8, limit: usize, writer: anytype) !void {
    const term = searchmod.sanitize(raw_query);
    if (term.len == 0) {
        try writer.writeAll("{\"actors\":[]}");
        return;
    }

    var arena_state = std.heap.ArenaAllocator.init(allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const norm = try normalize.query(arena, term);
    if (norm.len == 0) {
        try writer.writeAll("{\"actors\":[]}");
        return;
    }

    const keys = try prefixes.queryKeys(arena, norm);
    const mode = merge.modeForQuery(norm);

    const lease = try local.checkoutRead();
    defer lease.release();

    // Per-key base ∪ overlay sets; capture the exact-handle (e:) hits WITH their
    // score so we can hard-promote them — and re-inject them if the score cap
    // dropped them. A brand-new exact match (e.g. handle `pds.dad`, quality
    // score 0) otherwise loses to high-score token matches under the same query
    // (e.g. the ~300 accounts in `n:dad`) and never survives combine()'s cap.
    var exact: std.StringHashMapUnmanaged(f32) = .empty;
    var keysets: std.ArrayListUnmanaged(merge.KeySet) = .empty;
    for (keys) |key| {
        const base = try basePrefixDids(arena, lease, key);
        const ov = try overlayPrefixRows(arena, lease, key);
        if (key.len >= 2 and key[0] == 'e' and key[1] == ':') {
            for (base) |b| try exact.put(arena, b.did, b.score);
            for (ov) |o| {
                if (o.op == 0) _ = exact.remove(o.did) else try exact.put(arena, o.did, o.score);
            }
        }
        try keysets.append(arena, .{ .base = base, .overlay = ov });
    }

    var merged = try merge.combine(arena, keysets.items, mode, HYDRATE_CAP);
    if (exact.count() > 0) merged = try promoteExact(arena, merged, exact);

    const candidates = try hydrate(arena, lease, merged);

    var jw: json.Stringify = .{ .writer = writer };
    try jw.beginObject();
    try jw.objectField("actors");
    try jw.beginArray();
    const n = @min(limit, candidates.len);
    for (candidates[0..n]) |*c| try searchmod.writeActor(&jw, c);
    try jw.endArray();
    try jw.endObject();
}

/// Decode prefix_top_base.dids for one key into scored DIDs. DIDs are duped
/// into the arena (the blob is freed when the row iterator closes).
fn basePrefixDids(arena: Allocator, lease: LocalDb.ReadLease, key: []const u8) ![]merge.Scored {
    var rows = try lease.query("SELECT dids FROM idx.prefix_top_base WHERE key = ?", .{key});
    defer rows.deinit();
    if (rows.next()) |row| {
        var it = pack.DidListIter.init(row.blob(0)) catch return &.{};
        var out: std.ArrayListUnmanaged(merge.Scored) = .empty;
        while (it.next()) |e| try out.append(arena, .{ .did = try arena.dupe(u8, e.did), .score = e.score });
        return out.toOwnedSlice(arena);
    }
    return &.{};
}

// Bounded overlay read. The merge only needs the suppressions (op=0, which
// remove base DIDs — at most ~base-size relevant) plus the top additions (op=1
// by score; the final combine caps at HYDRATE_CAP anyway). `ORDER BY op ASC,
// score DESC` (backed by idx_prefix_overlay_key_op_score) keeps every
// suppression ahead of any addition, so the cap never drops an op=0 before an
// op=1. Moderation safety does NOT depend on this being complete — hydrate
// suppresses hidden/tombstoned via actor_overlay independently. Worst case from
// the cap is minor rename-staleness on a hot key, healed by the next snapshot.
// Without it, an uncapped read on a hot key (h:bsky, stale snapshot) returned
// millions of rows → 210s (2026-06-15).
const OVERLAY_READ_CAP = 500;

fn overlayPrefixRows(arena: Allocator, lease: LocalDb.ReadLease, key: []const u8) ![]merge.OverlayEntry {
    var rows = try lease.query(
        "SELECT did, score, op FROM prefix_overlay WHERE key = ? ORDER BY op ASC, score DESC LIMIT ?",
        .{ key, OVERLAY_READ_CAP },
    );
    defer rows.deinit();
    var out: std.ArrayListUnmanaged(merge.OverlayEntry) = .empty;
    while (rows.next()) |row| {
        try out.append(arena, .{
            .did = try arena.dupe(u8, row.text(0)),
            .score = @floatCast(row.float(1)),
            .op = @intCast(row.int(2)),
        });
    }
    return out.toOwnedSlice(arena);
}

/// Exact-handle = hard win (Codex): exact hits rank first, AND survive the
/// score cap. combine() caps by score, so a low-score exact match (a brand-new
/// handle, quality score 0) can be evicted before promotion — re-inject any
/// exact hit the cap dropped, then stable-partition exact to the front (each
/// tier keeps its score order). `exact` maps did → its e:-key score.
fn promoteExact(arena: Allocator, merged: []merge.Scored, exact: std.StringHashMapUnmanaged(f32)) ![]merge.Scored {
    var ex: std.ArrayListUnmanaged(merge.Scored) = .empty;
    var rest: std.ArrayListUnmanaged(merge.Scored) = .empty;
    var seen: std.StringHashMapUnmanaged(void) = .empty;
    for (merged) |m| {
        if (exact.contains(m.did)) {
            try ex.append(arena, m);
            try seen.put(arena, m.did, {});
        } else try rest.append(arena, m);
    }
    // exact hits the cap evicted (not seen in merged) — add them back up front.
    var it = exact.iterator();
    while (it.next()) |e| {
        if (!seen.contains(e.key_ptr.*)) try ex.append(arena, .{ .did = e.key_ptr.*, .score = e.value_ptr.* });
    }
    try ex.appendSlice(arena, rest.items);
    return ex.toOwnedSlice(arena);
}

/// Hydrate merged DIDs into presentable Candidates. Overlay actor wins over
/// base; NULL overlay blob = global tombstone (suppress); overlay/base hidden
/// suppress; and the live `actors` table is a safety belt (skip DIDs that are
/// missing or hidden there) while overlay correctness is still being earned.
fn hydrate(arena: Allocator, lease: LocalDb.ReadLease, dids: []const merge.Scored) ![]searchmod.Candidate {
    if (dids.len == 0) return &.{};

    var did_list = try arena.alloc([]const u8, dids.len);
    for (dids, 0..) |d, i| did_list[i] = d.did;

    // 1. actor_overlay: did → {blob?, hidden}. NULL blob = tombstone.
    const OverlayActor = struct { blob: ?[]const u8, hidden: bool };
    var ov_map: std.StringHashMapUnmanaged(OverlayActor) = .empty;
    {
        const sql = try inClause(arena, "SELECT did, blob, hidden FROM actor_overlay WHERE did IN (", did_list.len);
        var rows = try lease.queryTextList(sql, did_list);
        defer rows.deinit();
        while (rows.next()) |row| {
            const blob = if (row.nullableBlob(1)) |b| try arena.dupe(u8, b) else null;
            try ov_map.put(arena, try arena.dupe(u8, row.text(0)), .{ .blob = blob, .hidden = row.int(2) != 0 });
        }
    }

    // 2. base actor_compact_base for DIDs the overlay doesn't cover.
    var base_map: std.StringHashMapUnmanaged([]const u8) = .empty;
    {
        var need: std.ArrayListUnmanaged([]const u8) = .empty;
        for (did_list) |d| if (!ov_map.contains(d)) try need.append(arena, d);
        if (need.items.len > 0) {
            const sql = try inClause(arena, "SELECT did, blob FROM idx.actor_compact_base WHERE did IN (", need.items.len);
            var rows = try lease.queryTextList(sql, need.items);
            defer rows.deinit();
            while (rows.next()) |row| {
                try base_map.put(arena, try arena.dupe(u8, row.text(0)), try arena.dupe(u8, row.blob(1)));
            }
        }
    }

    // 3. safety belt: live actors existence + hidden.
    var live_hidden: std.StringHashMapUnmanaged(bool) = .empty;
    {
        const sql = try inClause(arena, "SELECT did, hidden FROM actors WHERE did IN (", did_list.len);
        var rows = try lease.queryTextList(sql, did_list);
        defer rows.deinit();
        while (rows.next()) |row| {
            try live_hidden.put(arena, try arena.dupe(u8, row.text(0)), row.int(1) != 0);
        }
    }

    var out: std.ArrayListUnmanaged(searchmod.Candidate) = .empty;
    for (dids) |d| {
        var blob: ?[]const u8 = null;
        if (ov_map.get(d.did)) |oa| {
            if (oa.blob == null) continue; // tombstone → global suppress
            if (oa.hidden) continue; // overlay-hidden → suppress
            blob = oa.blob;
        } else if (base_map.get(d.did)) |b| {
            blob = b;
        }
        if (blob == null) continue; // not found in overlay or base

        // safety belt: must exist and not be hidden in the live actors table
        const lh = live_hidden.get(d.did) orelse continue;
        if (lh) continue;

        const ac = pack.unpackActor(blob.?) catch continue;
        if (ac.hidden) continue; // moderation baked into the blob

        try out.append(arena, .{
            .did = d.did,
            .handle = ac.handle,
            .display_name = ac.display,
            .avatar_url = ac.avatar,
            .labels = ac.labels,
            .created_at = ac.created_at,
            .associated = ac.associated,
            .pds = ac.pds,
            .followers_count = 0,
            .follows_count = 0,
            .posts_count = 0,
            .last_activity_at = 0,
            .quality_score = d.score,
            .source = .prefix,
        });
    }
    return out.toOwnedSlice(arena);
}

/// Build "<prefix_sql> ?,?,…)" for an N-element `WHERE col IN (…)`.
fn inClause(arena: Allocator, prefix_sql: []const u8, n: usize) ![]const u8 {
    var sql: std.ArrayListUnmanaged(u8) = .empty;
    try sql.appendSlice(arena, prefix_sql);
    var i: usize = 0;
    while (i < n) : (i += 1) {
        if (i > 0) try sql.append(arena, ',');
        try sql.append(arena, '?');
    }
    try sql.append(arena, ')');
    return sql.toOwnedSlice(arena);
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

test "promoteExact lifts exact hits to the front, preserving score order" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    // merged already score-sorted: B(.9) A(.8) C(.7) D(.6). Exact = {C, A}.
    const merged = [_]merge.Scored{
        .{ .did = "B", .score = 0.9 },
        .{ .did = "A", .score = 0.8 },
        .{ .did = "C", .score = 0.7 },
        .{ .did = "D", .score = 0.6 },
    };
    var exact: std.StringHashMapUnmanaged(f32) = .empty;
    try exact.put(a, "C", 0.7);
    try exact.put(a, "A", 0.8);

    const out = try promoteExact(a, try a.dupe(merge.Scored, &merged), exact);
    // exact tier first (A before C — A had higher score), then the rest.
    try testing.expectEqualStrings("A", out[0].did);
    try testing.expectEqualStrings("C", out[1].did);
    try testing.expectEqualStrings("B", out[2].did);
    try testing.expectEqualStrings("D", out[3].did);
}

test "promoteExact re-injects an exact hit the score cap evicted" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();

    // merged is the post-cap survivors: high-score token matches, NONE of which
    // is the exact hit (regression for `pds.dad` = Bailey, quality_score 0,
    // capped out by ~300 high-score `n:dad` matches). The exact did is absent
    // from merged but present in `exact`.
    const merged = [_]merge.Scored{
        .{ .did = "tok1", .score = 500 },
        .{ .did = "tok2", .score = 400 },
    };
    var exact: std.StringHashMapUnmanaged(f32) = .empty;
    try exact.put(a, "bailey", 0); // exact handle hit, score 0, evicted by the cap

    const out = try promoteExact(a, try a.dupe(merge.Scored, &merged), exact);
    try testing.expectEqual(@as(usize, 3), out.len);
    try testing.expectEqualStrings("bailey", out[0].did); // hard win, despite score 0
    try testing.expectEqualStrings("tok1", out[1].did);
    try testing.expectEqualStrings("tok2", out[2].did);
}

test "inClause builds the right placeholder count" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const sql = try inClause(arena.allocator(), "SELECT did FROM t WHERE did IN (", 3);
    try testing.expectEqualStrings("SELECT did FROM t WHERE did IN (?,?,?)", sql);
}
