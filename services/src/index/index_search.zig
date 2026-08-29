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

const logfire = @import("logfire");
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

    const lease = try local.checkoutRead();
    defer lease.release();

    var trace: Trace = .{};
    const span = logfire.span("search", .{});
    defer span.end();
    const candidates = try execute(arena, lease, norm, &trace);
    span.setAttribute("q_len", @as(i64, @intCast(norm.len)));
    span.setAttribute("keys", @as(i64, @intCast(trace.keys.len)));
    span.setAttribute("merged", @as(i64, @intCast(trace.merged)));
    span.setAttribute("exact_hits", @as(i64, @intCast(trace.exact_hits)));
    span.setAttribute("hydrated_overlay", @as(i64, @intCast(trace.hydrated_overlay)));
    span.setAttribute("hydrated_base", @as(i64, @intCast(trace.hydrated_base)));
    span.setAttribute("tombstoned", @as(i64, @intCast(trace.tombstoned)));
    span.setAttribute("overlay_hidden", @as(i64, @intCast(trace.overlay_hidden)));
    span.setAttribute("blob_hidden", @as(i64, @intCast(trace.blob_hidden)));
    span.setAttribute("not_found", @as(i64, @intCast(trace.not_found)));
    span.setAttribute("belt_dropped", @as(i64, @intCast(trace.belt_dropped)));
    span.setAttribute("results", @as(i64, @intCast(@min(limit, candidates.len))));

    var jw: json.Stringify = .{ .writer = writer };
    try jw.beginObject();
    try jw.objectField("actors");
    try jw.beginArray();
    const n = @min(limit, candidates.len);
    for (candidates[0..n]) |*c| try searchmod.writeActor(&jw, c);
    try jw.endArray();
    try jw.endObject();
}

/// What the serving pipeline did with a query. Filled only when `/debug/search`
/// asks for it — the point is that the explanation comes from the SAME code
/// path that answered `/search`, not a parallel scorer that can drift out of
/// agreement with it.
pub const Trace = struct {
    pub const KeyStat = struct { key: []const u8, base: usize, overlay: usize };

    keys: []KeyStat = &.{},
    mode: merge.Mode = .union_any,
    merged: usize = 0,
    exact_hits: usize = 0,
    /// hydrate outcomes. `belt_dropped` counts candidates that passed every
    /// overlay/blob check and were dropped ONLY by the live `actors` belt —
    /// the number that decides whether the replica can go (docs/replica-removal-plan.md).
    hydrated_overlay: usize = 0,
    hydrated_base: usize = 0,
    tombstoned: usize = 0,
    overlay_hidden: usize = 0,
    blob_hidden: usize = 0,
    not_found: usize = 0,
    belt_dropped: usize = 0,
};

/// normalize'd query → ranked candidates. The single implementation behind both
/// `/search` and `/debug/search`.
fn execute(
    arena: Allocator,
    lease: LocalDb.ReadLease,
    norm: []const u8,
    trace: ?*Trace,
) ![]searchmod.Candidate {
    const keys = try prefixes.queryKeys(arena, norm);
    const mode = merge.modeForQuery(norm);

    var key_stats: std.ArrayListUnmanaged(Trace.KeyStat) = .empty;

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
        if (trace != null) try key_stats.append(arena, .{ .key = key, .base = base.len, .overlay = ov.len });
    }

    var merged = try merge.combine(arena, keysets.items, mode, HYDRATE_CAP);
    if (exact.count() > 0) merged = try promoteExact(arena, merged, exact);

    if (trace) |t| {
        t.keys = key_stats.items;
        t.mode = mode;
        t.merged = merged.len;
        t.exact_hits = exact.count();
    }

    return hydrate(arena, lease, merged, trace);
}

/// `/debug/search` for the index engine: the served ranking, plus the key-level
/// reachability facts that explain it (per-key list sizes, merge mode, the
/// HYDRATE_CAP survivors). A candidate absent from every key's top-N list is
/// unreachable at ANY score — that is the answer this endpoint exists to give.
pub fn debug(local: *LocalDb, allocator: Allocator, raw_query: []const u8, limit: usize, writer: anytype) !void {
    var arena_state = std.heap.ArenaAllocator.init(allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const term = searchmod.sanitize(raw_query);
    const norm = if (term.len == 0) "" else try normalize.query(arena, term);

    var jw: json.Stringify = .{ .writer = writer };
    if (norm.len == 0) {
        try jw.write(Report{ .q = raw_query, .normalized = norm });
        return;
    }

    const lease = try local.checkoutRead();
    defer lease.release();

    var trace: Trace = .{};
    const candidates = try execute(arena, lease, norm, &trace);

    const n = @min(limit, candidates.len);
    const rows = try arena.alloc(Report.Row, n);
    for (candidates[0..n], rows) |*c, *r| r.* = .{
        .did = c.did,
        .handle = c.handle,
        .display_name = c.display_name,
        .score = c.quality_score,
    };

    try jw.write(Report{
        .q = raw_query,
        .normalized = norm,
        .mode = @tagName(trace.mode),
        .exact_hits = trace.exact_hits,
        .merged = trace.merged,
        .keys = trace.keys,
        .candidates = rows,
    });
}

/// The `/debug/search` response for the index engine.
const Report = struct {
    const Row = struct { did: []const u8, handle: []const u8, display_name: []const u8, score: f64 };

    engine: []const u8 = "index",
    q: []const u8,
    normalized: []const u8,
    mode: []const u8 = "",
    hydrate_cap: usize = HYDRATE_CAP,
    exact_hits: usize = 0,
    merged: usize = 0,
    keys: []const Trace.KeyStat = &.{},
    candidates: []const Row = &.{},
};

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

fn overlayPrefixRows(arena: Allocator, lease: LocalDb.ReadLease, key: []const u8) ![]merge.OverlayEntry {
    var rows = try lease.query("SELECT did, score, op FROM prefix_overlay WHERE key = ?", .{key});
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
fn hydrate(arena: Allocator, lease: LocalDb.ReadLease, dids: []const merge.Scored, trace: ?*Trace) ![]searchmod.Candidate {
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

    var stats: Trace = .{};
    var out: std.ArrayListUnmanaged(searchmod.Candidate) = .empty;
    for (dids) |d| {
        var blob: ?[]const u8 = null;
        var from_overlay = false;
        if (ov_map.get(d.did)) |oa| {
            if (oa.blob == null) {
                stats.tombstoned += 1;
                continue; // tombstone → global suppress
            }
            if (oa.hidden) {
                stats.overlay_hidden += 1;
                continue; // overlay-hidden → suppress
            }
            blob = oa.blob;
            from_overlay = true;
        } else if (base_map.get(d.did)) |b| {
            blob = b;
        }
        if (blob == null) {
            stats.not_found += 1;
            continue; // not found in overlay or base
        }

        const ac = pack.unpackActor(blob.?) catch continue;
        if (ac.hidden) {
            stats.blob_hidden += 1;
            continue; // moderation baked into the blob
        }

        // safety belt: must exist and not be hidden in the live actors table.
        // Checked AFTER the overlay/blob checks so belt_dropped measures only
        // what the belt alone catches.
        const lh = live_hidden.get(d.did) orelse {
            stats.belt_dropped += 1;
            continue;
        };
        if (lh) {
            stats.belt_dropped += 1;
            continue;
        }
        if (from_overlay) stats.hydrated_overlay += 1 else stats.hydrated_base += 1;

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
    if (trace) |t| {
        t.hydrated_overlay = stats.hydrated_overlay;
        t.hydrated_base = stats.hydrated_base;
        t.tombstoned = stats.tombstoned;
        t.overlay_hidden = stats.overlay_hidden;
        t.blob_hidden = stats.blob_hidden;
        t.not_found = stats.not_found;
        t.belt_dropped = stats.belt_dropped;
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
