//! Index-backed `/search`. Serves from the ATTACHed prefix snapshot + the
//! live overlay. Response shape is the public typeahead shape
//! (search.Candidate + search.writeActor).
//!
//! Flow: normalize → queryKeys → per-key (base prefix_top_base ∪ prefix_overlay)
//! → merge (union single-token / intersect multi-token) → exact-handle
//! promotion → hydrate (overlay actor wins; tombstone/hidden suppress) →
//! response.
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

/// One `search` span per query is the whole logfire bill: 37,769 of 38,000
/// records in the 2026-09-04 burst's peak hours were this span, at p50 3 ms,
/// duplicating counts the worker's request_buckets already keep. Head-sample
/// 1 in N and stamp `sample_rate` so `count(*) * sample_rate` still gives
/// volume; slow searches log regardless, so the tail is never lost.
pub const search_span_sample_rate: u32 = 10;
pub const slow_search_ms: i64 = 100;
var search_seq = std.atomic.Value(u32).init(0);

fn sampleSearch(seq: u32) bool {
    return seq % search_span_sample_rate == 0;
}

fn sampledSearchSpan() logfire.Span {
    if (sampleSearch(search_seq.fetchAdd(1, .monotonic))) {
        return logfire.span("search", .{ .sample_rate = @as(i64, search_span_sample_rate) });
    }
    return .{ .inner = null, .parent_context = null };
}

fn milliTimestamp() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds, std.time.ns_per_ms));
}

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
    const t0 = milliTimestamp();
    const span = sampledSearchSpan();
    defer span.end();
    defer {
        const elapsed = milliTimestamp() - t0;
        if (elapsed >= slow_search_ms) log.warn("slow search: {d} ms (q_len={d} keys={d} merged={d})", .{ elapsed, norm.len, trace.keys.len, trace.merged });
    }
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
    // Counts describe rows fetched by this request, not total stored rows.
    pub const KeyStat = struct { key: []const u8, base: usize, overlay: usize, overlay_bounded: bool = false };

    keys: []KeyStat = &.{},
    mode: merge.Mode = .union_any,
    merged: usize = 0,
    exact_hits: usize = 0,
    /// hydrate outcomes
    hydrated_overlay: usize = 0,
    hydrated_base: usize = 0,
    tombstoned: usize = 0,
    overlay_hidden: usize = 0,
    blob_hidden: usize = 0,
    not_found: usize = 0,
};

/// normalize'd query → ranked candidates. The single implementation behind both
/// `/search` and `/debug/search`.
fn execute(
    arena: Allocator,
    lease: LocalDb.ReadLease,
    norm: []const u8,
    trace: ?*Trace,
) ![]searchmod.Candidate {
    return executeLookup(arena, lease, norm, trace, true);
}

// The unbounded instantiation is used only by differential tests.
fn executeLookup(arena: Allocator, lease: LocalDb.ReadLease, norm: []const u8, trace: ?*Trace, comptime bounded: bool) ![]searchmod.Candidate {
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
        const is_exact = key.len >= 2 and key[0] == 'e' and key[1] == ':';
        const ov = if (bounded and boundOverlay(mode, is_exact))
            try boundedOverlayPrefixRows(arena, lease, key, base)
        else
            try overlayPrefixRows(arena, lease, key);
        if (key.len >= 2 and key[0] == 'e' and key[1] == ':') {
            for (base) |b| try exact.put(arena, b.did, b.score);
            for (ov) |o| {
                if (o.op == 0) _ = exact.remove(o.did) else try exact.put(arena, o.did, o.score);
            }
        }
        try keysets.append(arena, .{ .base = base, .overlay = ov });
        if (trace != null) try key_stats.append(arena, .{ .key = key, .base = base.len, .overlay = ov.len, .overlay_bounded = bounded and boundOverlay(mode, is_exact) });
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

/// Offline/shadow evaluator. Caller supplies one frozen read lease; never used
/// by the public search handler. Public JSON and internal scores must agree.
pub fn evaluateFrozen(arena: Allocator, lease: LocalDb.ReadLease, raw_query: []const u8) !Evaluation {
    const norm = try normalize.query(arena, searchmod.sanitize(raw_query));
    const start = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
    var baseline_trace: Trace = .{};
    var candidate_trace: Trace = .{};
    const baseline = try executeLookup(arena, lease, norm, &baseline_trace, false);
    const middle = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
    const candidate = try executeLookup(arena, lease, norm, &candidate_trace, true);
    const end = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
    var a: std.Io.Writer.Allocating = .init(arena);
    var b: std.Io.Writer.Allocating = .init(arena);
    var ja: json.Stringify = .{ .writer = &a.writer };
    var jb: json.Stringify = .{ .writer = &b.writer };
    try ja.beginArray();
    try jb.beginArray();
    for (baseline) |*actor| try searchmod.writeActor(&ja, actor);
    for (candidate) |*actor| try searchmod.writeActor(&jb, actor);
    try ja.endArray();
    try jb.endArray();
    var equal = std.mem.eql(u8, a.written(), b.written());
    if (baseline.len != candidate.len) equal = false else {
        for (baseline, candidate) |x, y| {
            if (x.quality_score != y.quality_score) equal = false;
        }
    }
    var baseline_rows: usize = 0;
    var candidate_rows: usize = 0;
    for (baseline_trace.keys) |key| baseline_rows += key.overlay;
    for (candidate_trace.keys) |key| candidate_rows += key.overlay;
    return .{ .baseline_overlay_rows = baseline_rows, .candidate_overlay_rows = candidate_rows, .equal = equal, .baseline_us = @intCast(@divTrunc(middle - start, 1000)), .candidate_us = @intCast(@divTrunc(end - middle, 1000)), .actors = candidate.len };
}

pub const Evaluation = struct { equal: bool, baseline_us: u64, candidate_us: u64, actors: usize, baseline_overlay_rows: usize, candidate_overlay_rows: usize };

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

fn boundOverlay(mode: merge.Mode, is_exact: bool) bool {
    return mode == .union_any and !is_exact;
}

/// A union's top K cannot contain an overlay addition ranked below K within
/// its own key. Keep those K additions, plus EVERY overlay change to a base
/// candidate: a deletion or lower score must still replace the snapshot row.
/// Intersections and exact-handle promotion deliberately use the full lookup.
/// Scores are stored as widened f32 by overlay.upsertPrefix, so SQLite's
/// score/DID ordering agrees with merge.scoreDesc, including ties.
fn boundedOverlayPrefixRows(arena: Allocator, lease: LocalDb.ReadLease, key: []const u8, base: []const merge.Scored) ![]merge.OverlayEntry {
    // One statement keeps the additions and base corrections on the same
    // SQLite read snapshot, even if an overlay writer commits while stepping.
    const top_sql = std.fmt.comptimePrint(
        "SELECT did, score, op FROM (SELECT did, score, op FROM prefix_overlay WHERE key = ? AND op = 1 ORDER BY score DESC, did ASC LIMIT {d})",
        .{HYDRATE_CAP},
    );
    const sql = if (base.len == 0) top_sql else try inClause(arena, top_sql ++ " UNION ALL SELECT did, score, op FROM prefix_overlay WHERE key = ? AND did IN (", base.len);
    const args = try arena.alloc([]const u8, if (base.len == 0) 1 else base.len + 2);
    args[0] = key;
    if (base.len > 0) {
        args[1] = key;
        for (base, 2..) |entry, i| args[i] = entry.did;
    }
    var out: std.ArrayListUnmanaged(merge.OverlayEntry) = .empty;
    var rows = try lease.queryTextList(sql, args);
    defer rows.deinit();
    while (rows.next()) |row| try out.append(arena, .{
        .did = try arena.dupe(u8, row.text(0)),
        .score = @floatCast(row.float(1)),
        .op = @intCast(row.int(2)),
    });
    if (rows.err()) |err| return err;
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
/// suppress. Moderation is carried by the overlay row and the packed blob —
/// there is no second source to consult.
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

test "search spans are head-sampled 1 in N" {
    var sampled: u32 = 0;
    var seq: u32 = 0;
    while (seq < search_span_sample_rate * 3) : (seq += 1) {
        if (sampleSearch(seq)) sampled += 1;
    }
    try std.testing.expectEqual(@as(u32, 3), sampled);
    try std.testing.expect(sampleSearch(0));
    try std.testing.expect(!sampleSearch(1));
}

test "bounded union lookup preserves deletions, lowered base scores, and score ties" {
    const zqlite = @import("zqlite");
    var conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try @import("schema.zig").initOverlay(conn);
    var local = LocalDb.init(testing.allocator);
    const lease: LocalDb.ReadLease = .{ .db = &local, .slot = 0, .conn = conn };
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    const base = [_]merge.Scored{
        .{ .did = "deleted", .score = 1000 },
        .{ .did = "lowered", .score = 999 },
        .{ .did = "untouched", .score = 998 },
    };
    for (0..200) |i| {
        const did = try std.fmt.allocPrint(arena, "did:{d:0>4}", .{i});
        try conn.exec("INSERT INTO prefix_overlay(key,did,score,op,updated_at) VALUES('d:bs',?,10,1,1)", .{did});
    }
    try conn.exec("INSERT INTO prefix_overlay(key,did,score,op,updated_at) VALUES('d:bs','deleted',0,0,1),('d:bs','lowered',0,1,1)", .{});
    const full = try overlayPrefixRows(arena, lease, "d:bs");
    const bounded = try boundedOverlayPrefixRows(arena, lease, "d:bs", &base);
    try testing.expectEqual(@as(usize, 202), full.len);
    try testing.expectEqual(@as(usize, 52), bounded.len);
    const expected = try merge.combine(arena, &.{.{ .base = &base, .overlay = full }}, .union_any, HYDRATE_CAP);
    const actual = try merge.combine(arena, &.{.{ .base = &base, .overlay = bounded }}, .union_any, HYDRATE_CAP);
    try testing.expectEqual(expected.len, actual.len);
    for (expected, actual) |e, a| {
        try testing.expectEqualStrings(e.did, a.did);
        try testing.expectEqual(e.score, a.score);
    }
    try testing.expectEqualStrings("untouched", actual[0].did);
    try testing.expectEqualStrings("did:0000", actual[1].did);
    try testing.expectEqualStrings("did:0048", actual[49].did);
}

test "overlay bounds never truncate intersections or exact promotion" {
    try testing.expect(boundOverlay(.union_any, false));
    try testing.expect(!boundOverlay(.union_any, true));
    try testing.expect(!boundOverlay(.intersect_all, false));
    try testing.expect(!boundOverlay(.intersect_all, true));
}

test "bounded lookup differential: seeded multi-key SQLite fixtures match full ranking" {
    const zqlite = @import("zqlite");
    var conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try @import("schema.zig").initOverlay(conn);
    var local = LocalDb.init(testing.allocator);
    const lease: LocalDb.ReadLease = .{ .db = &local, .slot = 0, .conn = conn };
    var prng = std.Random.DefaultPrng.init(0x3850);
    const random = prng.random();
    for (0..160) |trial| {
        try conn.exec("DELETE FROM prefix_overlay", .{});
        var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
        defer arena_state.deinit();
        const arena = arena_state.allocator();
        var full_sets: std.ArrayListUnmanaged(merge.KeySet) = .empty;
        var bounded_sets: std.ArrayListUnmanaged(merge.KeySet) = .empty;
        const mode: merge.Mode = if (trial % 4 == 0) .intersect_all else .union_any;
        for ([_][]const u8{ "h:bs", "d:bs", "n:bs", "e:bs" }) |key| {
            var base: std.ArrayListUnmanaged(merge.Scored) = .empty;
            for (0..240) |i| {
                const did = try std.fmt.allocPrint(arena, "did:{d:0>4}", .{i});
                // Repeated DIDs across keys, ties, fractional scores, empty base
                // lists, snapshot candidates with both lowered and raised scores.
                if (trial % 5 != 0 and random.uintLessThan(u8, 4) == 0)
                    try base.append(arena, .{ .did = did, .score = @as(f32, @floatFromInt(random.uintLessThan(u16, 80))) / 8 });
                if (random.uintLessThan(u8, 5) != 0) {
                    const score: f32 = @as(f32, @floatFromInt(random.uintLessThan(u16, 80))) / 8;
                    const op: i64 = if (random.uintLessThan(u8, 5) == 0) 0 else 1;
                    try conn.exec("INSERT INTO prefix_overlay(key,did,score,op,updated_at) VALUES(?,?,?,?,1)", .{ key, did, @as(f64, score), op });
                }
            }
            const full = try overlayPrefixRows(arena, lease, key);
            const bounded = if (boundOverlay(mode, key[0] == 'e'))
                try boundedOverlayPrefixRows(arena, lease, key, base.items)
            else
                try overlayPrefixRows(arena, lease, key);
            try full_sets.append(arena, .{ .base = base.items, .overlay = full });
            try bounded_sets.append(arena, .{ .base = base.items, .overlay = bounded });
        }
        const expected = try merge.combine(arena, full_sets.items, mode, HYDRATE_CAP);
        const actual = try merge.combine(arena, bounded_sets.items, mode, HYDRATE_CAP);
        try testing.expectEqual(expected.len, actual.len);
        for (expected, actual) |e, a| {
            try testing.expectEqualStrings(e.did, a.did);
            try testing.expectEqual(e.score, a.score);
        }
    }
}

test "full pipeline differential preserves exact promotion and moderation across query shapes" {
    const zqlite = @import("zqlite");
    var conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try @import("schema.zig").initOverlay(conn);
    try conn.exec("ATTACH DATABASE ':memory:' AS idx", .{});
    try conn.exec("CREATE TABLE idx.prefix_top_base(key TEXT PRIMARY KEY,dids BLOB)", .{});
    try conn.exec("CREATE TABLE idx.actor_compact_base(did TEXT PRIMARY KEY,blob BLOB)", .{});
    var local = LocalDb.init(testing.allocator);
    const lease: LocalDb.ReadLease = .{ .db = &local, .slot = 0, .conn = conn };
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    for ([_][]const u8{ "bs", "pds.dad", "nate nowack", "日本", "élo", "missing" }) |query| {
        const norm = try normalize.query(arena, query);
        const keys = try prefixes.queryKeys(arena, norm);
        for (keys) |key| {
            const exact = std.mem.startsWith(u8, key, "e:");
            var base: std.ArrayListUnmanaged(pack.DidEntry) = .empty;
            for (0..120) |i| {
                const did = try std.fmt.allocPrint(arena, "did:{d:0>4}", .{i});
                const score: f32 = @floatFromInt(i);
                const blob = try pack.packActor(arena, .{ .hidden = i % 13 == 0, .quality_score = score, .handle = did, .display = query, .avatar = "", .labels = "[]", .pds = "", .created_at = "", .associated = "{}" });
                if (i % 3 == 0) {
                    try base.append(arena, .{ .did = did, .score = score });
                    try conn.exec("INSERT OR REPLACE INTO idx.actor_compact_base VALUES(?,?)", .{ did, blob });
                }
                if ((!exact or i == 1) and !std.mem.eql(u8, query, "missing")) {
                    try conn.exec("INSERT OR REPLACE INTO prefix_overlay(key,did,score,op,updated_at) VALUES(?,?,?,?,1)", .{ key, did, @as(f64, score), @as(i64, if (i % 11 == 0) 0 else 1) });
                }
                if (i % 7 == 0) {
                    try conn.exec("INSERT OR REPLACE INTO actor_overlay(did,blob,hidden,updated_at) VALUES(?,NULL,0,1)", .{did});
                } else if (i % 5 != 0) {
                    try conn.exec("INSERT OR REPLACE INTO actor_overlay(did,blob,hidden,updated_at) VALUES(?,?,?,1)", .{ did, blob, @as(i64, if (i % 17 == 0) 1 else 0) });
                }
            }
            const packed_list = try pack.packDidList(arena, if (exact or std.mem.eql(u8, query, "missing")) &.{} else base.items);
            try conn.exec("INSERT OR REPLACE INTO idx.prefix_top_base VALUES(?,?)", .{ key, packed_list });
        }
        const full = try executeLookup(arena, lease, norm, null, false);
        const bounded = try executeLookup(arena, lease, norm, null, true);
        try testing.expectEqual(full.len, bounded.len);
        for (full, bounded) |a, b| {
            try testing.expectEqualStrings(a.did, b.did);
            try testing.expectEqualStrings(a.handle, b.handle);
            try testing.expectEqualStrings(a.display_name, b.display_name);
            try testing.expectEqual(a.quality_score, b.quality_score);
        }
    }
}

// Opt-in, read-only real-data differential. Both paths use the same captured
// overlay and immutable snapshot. Never defaults to the serving database.
test "real data differential (explicit offline paths only)" {
    const overlay_env = std.c.getenv("TYPEAHEAD_EVAL_OVERLAY") orelse return error.SkipZigTest;
    const snapshot_env = std.c.getenv("TYPEAHEAD_EVAL_SNAPSHOT") orelse return error.SkipZigTest;
    const zqlite = @import("zqlite");
    var conn = try zqlite.open(std.mem.span(overlay_env), zqlite.OpenFlags.ReadOnly);
    defer conn.close();
    try conn.exec("ATTACH DATABASE ? AS idx", .{std.mem.span(snapshot_env)});
    try conn.exec("PRAGMA query_only=ON", .{});
    var local = LocalDb.init(testing.allocator);
    const lease: LocalDb.ReadLease = .{ .db = &local, .slot = 0, .conn = conn };
    for ([_][]const u8{ "bs", "nate", "nowack", "alice", "pds.dad", "zzstoatzz.io", "日本", "unlikelymissing38", "nate nowack", "bs bs", "nate nate", "ALICE" }) |query| {
        var full_times: [7]i128 = undefined;
        var bounded_times: [7]i128 = undefined;
        var count: usize = 0;
        for (0..8) |iteration| {
            var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
            defer arena_state.deinit();
            const arena = arena_state.allocator();
            const norm = try normalize.query(arena, query);
            const start = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
            const full = try executeLookup(arena, lease, norm, null, false);
            const middle = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
            const bounded = try executeLookup(arena, lease, norm, null, true);
            const end = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
            if (iteration > 0) {
                full_times[iteration - 1] = middle - start;
                bounded_times[iteration - 1] = end - middle;
            }
            try testing.expectEqual(full.len, bounded.len);
            count = full.len;
            // Compare every public actor field, in order, not just DID overlap.
            var a: std.Io.Writer.Allocating = .init(arena);
            var b: std.Io.Writer.Allocating = .init(arena);
            var ja: json.Stringify = .{ .writer = &a.writer };
            var jb: json.Stringify = .{ .writer = &b.writer };
            try ja.beginArray();
            try jb.beginArray();
            for (full) |*candidate| try searchmod.writeActor(&ja, candidate);
            for (bounded) |*candidate| try searchmod.writeActor(&jb, candidate);
            try ja.endArray();
            try jb.endArray();
            try testing.expectEqualStrings(a.written(), b.written());
            for (full, bounded) |x, y| try testing.expectEqual(x.quality_score, y.quality_score);
        }
        std.mem.sort(i128, &full_times, {}, std.sort.asc(i128));
        std.mem.sort(i128, &bounded_times, {}, std.sort.asc(i128));
        std.debug.print("REAL_EVAL q={s} actors={d} full_us={d} bounded_us={d}\n", .{ query, count, @divTrunc(full_times[3], 1000), @divTrunc(bounded_times[3], 1000) });
    }
}
