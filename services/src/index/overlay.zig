//! Overlay projection (Milestone C): turn a synced actor delta into
//! actor_overlay + prefix_overlay rows so index serving reflects the change
//! before the next build. Called from sync.zig INSIDE the write transaction
//! that upserts the actors row — so the actor write and its overlay are atomic.
//!
//! Only deltas newer than the snapshot watermark reach here (sync gates on it);
//! older state is already baked into the base. Scores match the builder
//! (scoring.scoreOf) so overlay and base entries rank on the same scale.
//!
//! Invariants (Codex review):
//!   - actor_overlay.blob = NULL is a GLOBAL DID tombstone (suppresses at
//!     hydrate regardless of prefix rows).
//!   - sync rows are source=0; operator overrides (future) are source=1 and
//!     survive compaction.
//!   - prefix diff writes added (op=1) / removed (op=0) keys, AND upserts the
//!     latest score for unchanged-set keys too. The original design deferred
//!     "score drift" to the next build, but enrichment (`quality_score`:
//!     0 → 850) is exactly the signal ranking needs to reflect immediately —
//!     waiting hours for a snapshot rebuild defeated the whole prioritized
//!     enrichment pipeline. compaction prunes these on the next rebuild.
const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;

const normalize = @import("normalize.zig");
const prefixes = @import("prefixes.zig");
const scoring = @import("scoring.zig");
const pack = @import("pack.zig");

const log = std.log.scoped(.overlay);

const upsert_actor_sql = "INSERT INTO actor_overlay (did, blob, hidden, updated_at, source) VALUES (?, ?, ?, ?, 0) ON CONFLICT(did) DO UPDATE SET blob=excluded.blob, hidden=excluded.hidden, updated_at=excluded.updated_at, source=0 WHERE actor_overlay.blob IS NOT excluded.blob OR actor_overlay.hidden != excluded.hidden OR actor_overlay.updated_at != excluded.updated_at OR actor_overlay.source != 0";

/// Raw (un-normalized) actor fields from a sync delta. We normalize the
/// handle/display here so prefix derivation matches the builder exactly.
pub const Actor = struct {
    did: []const u8,
    handle: []const u8,
    display_name: []const u8,
    avatar_url: []const u8,
    labels: []const u8,
    associated: []const u8,
    pds: []const u8,
    pds_indexed: bool = false,
    created_at: []const u8,
    hidden: bool,
    quality_score: f32,
};

/// Project an updated/created actor. `old_handle`/`old_display` are the
/// pre-update values (the local row being replaced); pass "" for a brand-new
/// actor. `updated_at` is the SOURCE timestamp (Turso updated_at), used for
/// watermark-based compaction later.
pub fn projectUpdateWithPds(
    arena: Allocator,
    conn: zqlite.Conn,
    a: Actor,
    old_handle: []const u8,
    old_display: []const u8,
    old_pds: []const u8,
    updated_at: i64,
) !void {
    const nh = try normalize.handle(arena, a.handle);
    const nd = try normalize.display(arena, a.display_name);
    const new_keys = try prefixes.deriveWithPds(arena, nh, nd, if (a.pds_indexed) a.pds else "");

    const oh = try normalize.handle(arena, old_handle);
    const od = try normalize.display(arena, old_display);
    const old_keys = try prefixes.deriveWithPds(arena, oh, od, old_pds);

    const score = scoring.scoreOf(.{ .quality_score = a.quality_score });

    // Pack the RAW handle/display — `nh`/`nd` are normalized for key derivation
    // only. The blob is what hydrate returns to clients, so we'd otherwise
    // serve every enriched actor's displayName lowercased. The builder packs
    // raw (builder.zig); the overlay must match.
    const blob = try pack.packActor(arena, .{
        .hidden = a.hidden,
        .quality_score = a.quality_score,
        .handle = a.handle,
        .display = a.display_name,
        .avatar = a.avatar_url,
        .labels = a.labels,
        .pds = a.pds,
        .created_at = a.created_at,
        .associated = a.associated,
    });
    try conn.exec(
        upsert_actor_sql,
        .{ a.did, zqlite.blob(blob), @as(i64, @intFromBool(a.hidden)), updated_at },
    );

    try diffPrefixes(conn, a.did, old_keys, new_keys, score, updated_at);
}

/// Project a deletion: write a global tombstone (NULL blob) and suppress the
/// actor's known prefixes so it stops appearing as a candidate. The tombstone
/// alone is sufficient for correctness (hydrate skips it); the prefix
/// suppressions just keep candidate sets tidy.
pub fn projectDeleteWithPds(
    arena: Allocator,
    conn: zqlite.Conn,
    did: []const u8,
    old_handle: []const u8,
    old_display: []const u8,
    old_pds: []const u8,
    updated_at: i64,
) !void {
    try conn.exec(
        upsert_actor_sql,
        .{ did, @as(?zqlite.Blob, null), @as(i64, 0), updated_at },
    );

    const oh = try normalize.handle(arena, old_handle);
    const od = try normalize.display(arena, old_display);
    for (try prefixes.deriveWithPds(arena, oh, od, old_pds)) |k| {
        try upsertPrefix(conn, k, did, 0, 0, updated_at);
    }
}

/// Merge-walk two sorted+deduped key lists. Writes per branch:
///   - key only in `old` (rename / delete) → suppress (op=0).
///   - key only in `new` (rename / new actor) → add (op=1, new score).
///   - key in both (unchanged set) → upsert (op=1, new score). Carries live
///     score updates (enrichment, refresh) into ranking before the next
///     snapshot rebuild. Identical replay rows leave the stored entry untouched.
fn diffPrefixes(
    conn: zqlite.Conn,
    did: []const u8,
    old_keys: []const []const u8,
    new_keys: []const []const u8,
    score: f32,
    updated_at: i64,
) !void {
    var oi: usize = 0;
    var ni: usize = 0;
    while (oi < old_keys.len or ni < new_keys.len) {
        if (ni == new_keys.len) {
            try upsertPrefix(conn, old_keys[oi], did, 0, 0, updated_at);
            oi += 1;
        } else if (oi == old_keys.len) {
            try upsertPrefix(conn, new_keys[ni], did, score, 1, updated_at);
            ni += 1;
        } else switch (std.mem.order(u8, old_keys[oi], new_keys[ni])) {
            .lt => {
                try upsertPrefix(conn, old_keys[oi], did, 0, 0, updated_at);
                oi += 1;
            },
            .gt => {
                try upsertPrefix(conn, new_keys[ni], did, score, 1, updated_at);
                ni += 1;
            },
            .eq => {
                try upsertPrefix(conn, new_keys[ni], did, score, 1, updated_at);
                oi += 1;
                ni += 1;
            },
        }
    }
}

const upsert_prefix_sql = "INSERT INTO prefix_overlay (key, did, score, op, updated_at, source) VALUES (?, ?, ?, ?, ?, 0) ON CONFLICT(key, did) DO UPDATE SET score=excluded.score, op=excluded.op, updated_at=excluded.updated_at, source=0 WHERE prefix_overlay.score IS NOT excluded.score OR prefix_overlay.op != excluded.op OR prefix_overlay.updated_at != excluded.updated_at OR prefix_overlay.source != 0";

fn upsertPrefix(conn: zqlite.Conn, key: []const u8, did: []const u8, score: f32, op: i64, updated_at: i64) !void {
    try conn.exec(
        upsert_prefix_sql,
        .{ key, did, @as(f64, score), op, updated_at },
    );
}

/// Rows deleted per compactChunk call. Bounds how long the caller holds the
/// write mutex: a full-overlay compaction after a long sync outage is tens of
/// millions of prefix rows, and holding the mutex across all of them blocked
/// the resync's next batch until the sync watchdog killed the process —
/// which restarted the compaction from scratch, forever (2026-08-28).
pub const COMPACT_CHUNK: u32 = 20_000;

/// One bounded step of compaction: drop up to COMPACT_CHUNK sync-derived
/// overlay rows at or below the new snapshot watermark (the base now
/// reflects them). Operator rows (source=1) survive — a manual hide/show
/// must not vanish on promote. Returns rows deleted; 0 means done. The
/// caller loops, releasing the write mutex between calls.
pub fn compactChunk(conn: zqlite.Conn, new_watermark: i64) !u32 {
    try conn.exec(
        \\DELETE FROM prefix_overlay WHERE (key, did) IN (
        \\  SELECT key, did FROM prefix_overlay WHERE source = 0 AND updated_at <= ? LIMIT ?)
    , .{ new_watermark, COMPACT_CHUNK });
    const prefix_deleted: u32 = @intCast(conn.changes());
    try conn.exec(
        \\DELETE FROM actor_overlay WHERE did IN (
        \\  SELECT did FROM actor_overlay WHERE source = 0 AND updated_at <= ? LIMIT ?)
    , .{ new_watermark, COMPACT_CHUNK });
    return prefix_deleted + @as(u32, @intCast(conn.changes()));
}

/// Full compaction in one call — for callers that own the connection outright
/// (tests, tooling). Production goes through LocalDb.compactOverlay so the
/// write mutex is released between chunks.
pub fn compact(conn: zqlite.Conn, new_watermark: i64) !void {
    while (try compactChunk(conn, new_watermark) > 0) {}
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;
const schema = @import("schema.zig");

test "prefix replay skips identical writes but preserves timestamp, ranking and source changes" {
    const conn = try memConn();
    defer conn.close();
    try upsertPrefix(conn, "h:a", "did:a", 1, 1, 100);
    try upsertPrefix(conn, "h:a", "did:a", 1, 1, 100);
    {
        const row = (try conn.row("SELECT total_changes()", .{})).?;
        defer row.deinit();
        try testing.expectEqual(@as(i64, 1), row.int(0));
    }
    try upsertPrefix(conn, "h:a", "did:a", 1, 1, 200);
    try compact(conn, 150);
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "h:a", "did:a"));
    try upsertPrefix(conn, "h:a", "did:a", 3, 1, 200);
    {
        const row = (try conn.row("SELECT score FROM prefix_overlay WHERE key='h:a' AND did='did:a'", .{})).?;
        defer row.deinit();
        try testing.expectEqual(@as(f64, 3), row.float(0));
    }
    try upsertPrefix(conn, "h:a", "did:a", 0, 0, 200);
    try testing.expectEqual(@as(?i64, 0), try prefixOp(conn, "h:a", "did:a"));
    try conn.exec("UPDATE prefix_overlay SET source=1 WHERE key='h:a' AND did='did:a'", .{});
    try upsertPrefix(conn, "h:a", "did:a", 0, 0, 200);
    try compact(conn, 200);
    try testing.expectEqual(@as(?i64, null), try prefixOp(conn, "h:a", "did:a"));
}

test "actor replay avoids writes and tombstones remain null-safe" {
    const conn = try memConn();
    defer conn.close();
    const blob = zqlite.blob("packed actor");
    try conn.exec(upsert_actor_sql, .{ "did:a", blob, @as(i64, 0), @as(i64, 100) });
    try conn.exec(upsert_actor_sql, .{ "did:a", blob, @as(i64, 0), @as(i64, 100) });
    try testing.expectEqual(@as(i64, 1), zqlite.c.sqlite3_total_changes64(conn.conn));
    try conn.exec(upsert_actor_sql, .{ "did:a", zqlite.blob("new actor"), @as(i64, 1), @as(i64, 100) });
    try testing.expectEqual(@as(i64, 2), zqlite.c.sqlite3_total_changes64(conn.conn));
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    try projectDelete(arena.allocator(), conn, "did:a", "", "", 200);
    try projectDelete(arena.allocator(), conn, "did:a", "", "", 200);
    try testing.expectEqual(@as(i64, 3), zqlite.c.sqlite3_total_changes64(conn.conn));
    const row = (try conn.row("SELECT blob IS NULL,hidden,updated_at FROM actor_overlay WHERE did='did:a'", .{})).?;
    defer row.deinit();
    try testing.expectEqual(@as(i64, 1), row.int(0));
    try testing.expectEqual(@as(i64, 0), row.int(1));
    try testing.expectEqual(@as(i64, 200), row.int(2));
}

fn memConn() !zqlite.Conn {
    const conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    try schema.initOverlay(conn);
    return conn;
}

fn prefixOp(conn: zqlite.Conn, key: []const u8, did: []const u8) !?i64 {
    const row = try conn.row("SELECT op FROM prefix_overlay WHERE key = ? AND did = ?", .{ key, did });
    if (row) |r| {
        defer r.deinit();
        return r.int(0);
    }
    return null;
}

test "projectUpdate writes actor blob + adds new prefixes" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const conn = try memConn();
    defer conn.close();

    try projectUpdate(arena.allocator(), conn, .{
        .did = "did:plc:x",
        .handle = "nate.fm",
        .display_name = "Nate",
        .avatar_url = "",
        .labels = "[]",
        .associated = "{}",
        .pds = "",
        .created_at = "",
        .hidden = false,
        .quality_score = 1.0,
    }, "", "", 100);

    // actor_overlay row exists, non-null blob, not hidden
    const ar = (try conn.row("SELECT hidden FROM actor_overlay WHERE did = ?", .{"did:plc:x"})).?;
    defer ar.deinit();
    try testing.expectEqual(@as(i64, 0), ar.int(0));

    // handle + exact + display prefixes added (op=1)
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "h:nate.fm", "did:plc:x"));
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "e:nate.fm", "did:plc:x"));
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "n:nate", "did:plc:x"));
}

test "projectUpdate rename suppresses old keys, adds new, leaves common" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const conn = try memConn();
    defer conn.close();

    // alice.fm → alicia.fm (common prefix "h:a"/"h:al"/"h:ali" unchanged).
    try projectUpdate(arena.allocator(), conn, .{
        .did = "did:plc:y",
        .handle = "alicia.fm",
        .display_name = "",
        .avatar_url = "",
        .labels = "[]",
        .associated = "{}",
        .pds = "",
        .created_at = "",
        .hidden = false,
        .quality_score = 1.0,
    }, "alice.fm", "", 200);

    try testing.expectEqual(@as(?i64, 0), try prefixOp(conn, "e:alice.fm", "did:plc:y")); // old exact suppressed
    try testing.expectEqual(@as(?i64, 0), try prefixOp(conn, "h:alice", "did:plc:y")); // old handle prefix suppressed
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "e:alicia.fm", "did:plc:y")); // new exact added
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "h:alici", "did:plc:y")); // new handle prefix added
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "h:al", "did:plc:y")); // common key → upserted w/ new score (live score carry)
}

test "projectUpdate packs RAW handle/display (not normalized) into the blob" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const conn = try memConn();
    defer conn.close();

    // mixed-case display name with kanji is what hydrate returns to clients;
    // it MUST round-trip exactly. The bug was packing normalize.display(…)
    // (lowercased + katakana→hiragana folded) into the blob, so every overlay
    // hit showed up lowercased + script-folded.
    try projectUpdate(arena.allocator(), conn, .{
        .did = "did:plc:q",
        .handle = "Pfrazee.com", // raw casing preserved
        .display_name = "Paul Frazee 🦀",
        .avatar_url = "",
        .labels = "[]",
        .associated = "{}",
        .pds = "",
        .created_at = "",
        .hidden = false,
        .quality_score = 850.0,
    }, "", "", 100);

    const row = (try conn.row(
        "SELECT blob FROM actor_overlay WHERE did = ?",
        .{"did:plc:q"},
    )).?;
    defer row.deinit();
    const blob = row.blob(0);
    const a = try pack.unpackActor(blob);
    try testing.expectEqualStrings("Pfrazee.com", a.handle);
    try testing.expectEqualStrings("Paul Frazee 🦀", a.display);
}

test "projectUpdate score-only change carries score into overlay (enrichment bug)" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const conn = try memConn();
    defer conn.close();

    // initial project: zero-score actor (unenriched, ~99% of corpus pre-enrichment).
    try projectUpdate(arena.allocator(), conn, .{
        .did = "did:plc:p",
        .handle = "pfrazee.com",
        .display_name = "Paul Frazee",
        .avatar_url = "",
        .labels = "[]",
        .associated = "{}",
        .pds = "",
        .created_at = "",
        .hidden = false,
        .quality_score = 0.0,
    }, "", "", 100);
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "h:pfrazee.com", "did:plc:p"));

    // same handle/display, score now 850 (enrichment). ALL keys hit `.eq` —
    // the pre-fix updater skipped these, leaving overlay scores at 0 and
    // ranking unchanged. fixed updater upserts every common key with the
    // new score so ranking reflects the enrichment within sync's next tick.
    try projectUpdate(arena.allocator(), conn, .{
        .did = "did:plc:p",
        .handle = "pfrazee.com",
        .display_name = "Paul Frazee",
        .avatar_url = "",
        .labels = "[]",
        .associated = "{}",
        .pds = "",
        .created_at = "",
        .hidden = false,
        .quality_score = 850.0,
    }, "pfrazee.com", "Paul Frazee", 200);

    const row = (try conn.row(
        "SELECT op, score FROM prefix_overlay WHERE key = ? AND did = ?",
        .{ "h:pfrazee.com", "did:plc:p" },
    )).?;
    defer row.deinit();
    try testing.expectEqual(@as(i64, 1), row.int(0));
    try testing.expect(row.float(1) > 100); // scoring.scoreOf(850) ≫ 0
}

test "projectDelete writes tombstone (NULL blob) + suppresses old keys" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const conn = try memConn();
    defer conn.close();

    try projectDelete(arena.allocator(), conn, "did:plc:z", "bob.fm", "", 300);

    const ar = (try conn.row("SELECT blob IS NULL FROM actor_overlay WHERE did = ?", .{"did:plc:z"})).?;
    defer ar.deinit();
    try testing.expectEqual(@as(i64, 1), ar.int(0)); // blob IS NULL → tombstone
    try testing.expectEqual(@as(?i64, 0), try prefixOp(conn, "e:bob.fm", "did:plc:z")); // suppressed
}

test "compact prunes sync rows ≤ watermark but keeps operator + newer rows" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const conn = try memConn();
    defer conn.close();

    // sync row @100, sync row @300, operator row @50.
    try conn.exec("INSERT INTO prefix_overlay (key,did,score,op,updated_at,source) VALUES ('h:a','d1',1,1,100,0)", .{});
    try conn.exec("INSERT INTO prefix_overlay (key,did,score,op,updated_at,source) VALUES ('h:b','d2',1,1,300,0)", .{});
    try conn.exec("INSERT INTO prefix_overlay (key,did,score,op,updated_at,source) VALUES ('h:c','d3',1,0,50,1)", .{});

    try compact(conn, 200);

    try testing.expectEqual(@as(?i64, null), try prefixOp(conn, "h:a", "d1")); // sync ≤200 pruned
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "h:b", "d2")); // sync >200 kept
    try testing.expectEqual(@as(?i64, 0), try prefixOp(conn, "h:c", "d3")); // operator kept despite ≤200
}

test "compactChunk is bounded and converges across an overlay larger than one chunk" {
    const conn = try memConn();
    defer conn.close();

    const n: u32 = COMPACT_CHUNK * 2 + 7;
    try conn.exec("BEGIN", .{});
    var i: u32 = 0;
    while (i < n) : (i += 1) {
        var key_buf: [32]u8 = undefined;
        const key = try std.fmt.bufPrint(&key_buf, "h:{d}", .{i});
        try conn.exec("INSERT INTO prefix_overlay (key,did,score,op,updated_at,source) VALUES (?, 'd', 1, 1, 100, 0)", .{key});
    }
    try conn.exec("INSERT INTO prefix_overlay (key,did,score,op,updated_at,source) VALUES ('keep','d', 1, 1, 100, 1)", .{});
    try conn.exec("INSERT INTO actor_overlay (did,blob,hidden,updated_at,source) VALUES ('a1', x'00', 0, 100, 0)", .{});
    try conn.exec("COMMIT", .{});

    const first = try compactChunk(conn, 200);
    try testing.expectEqual(COMPACT_CHUNK + 1, first); // one chunk of prefixes + the lone actor row

    var total: u32 = first;
    var rounds: u32 = 1;
    while (true) {
        const d = try compactChunk(conn, 200);
        if (d == 0) break;
        total += d;
        rounds += 1;
    }
    try testing.expectEqual(n + 1, total);
    try testing.expectEqual(@as(u32, 3), rounds);
    try testing.expectEqual(@as(?i64, 1), try prefixOp(conn, "keep", "d"));
}

// Destructive only to an explicitly supplied disposable fixture. This does
// not connect to Turso or start a server. Run separately on two identical
// copies (one indexed) to compare actual prefix projection and commit costs.
test "offline real actor fanout benchmark (disposable database only)" {
    const path = std.c.getenv("TYPEAHEAD_FANOUT_SCRATCH") orelse return error.SkipZigTest;
    const arm = std.c.getenv("TYPEAHEAD_FANOUT_ALLOW_WRITES") orelse return error.SkipZigTest;
    if (!std.mem.eql(u8, std.mem.span(arm), "disposable-copy")) return error.SkipZigTest;
    var conn = try zqlite.open(std.mem.span(path), zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try conn.exec("PRAGMA journal_mode=WAL", .{});
    try conn.exec("PRAGMA synchronous=NORMAL", .{});
    try conn.exec("PRAGMA wal_autocheckpoint=1000", .{});
    try conn.exec("PRAGMA cache_size=-20000", .{});
    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var actors: std.ArrayListUnmanaged(Actor) = .empty;
    {
        var rows = try conn.rows("SELECT did,blob FROM actor_overlay WHERE blob IS NOT NULL ORDER BY did LIMIT 2500", .{});
        defer rows.deinit();
        while (rows.next()) |row| {
            const blob = try arena.dupe(u8, row.blob(1));
            const a = try pack.unpackActor(blob);
            try actors.append(arena, .{
                .did = try arena.dupe(u8, row.text(0)),
                .handle = a.handle,
                .display_name = a.display,
                .avatar_url = a.avatar,
                .labels = a.labels,
                .associated = a.associated,
                .pds = a.pds,
                .created_at = a.created_at,
                .hidden = a.hidden,
                .quality_score = a.quality_score,
            });
        }
        if (rows.err) |err| return err;
    }
    if (actors.items.len < 2500) return error.InsufficientRealActors;
    for ([_][]const u8{ "unchanged", "rescore", "rename", "insert", "delete" }, 0..) |scenario, si| {
        const start = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
        try conn.exec("BEGIN IMMEDIATE", .{});
        errdefer conn.exec("ROLLBACK", .{}) catch {};
        for (actors.items[si * 500 .. (si + 1) * 500]) |original| {
            var a = original;
            if (si == 4) {
                try projectDelete(arena, conn, a.did, a.handle, a.display_name, 2000000000);
                continue;
            }
            if (si == 1) a.quality_score += 1;
            if (si == 2) {
                a.handle = try std.fmt.allocPrint(arena, "eval-{s}", .{a.handle});
                a.display_name = try std.fmt.allocPrint(arena, "Eval {s}", .{a.display_name});
            }
            if (si == 3) a.did = try std.fmt.allocPrint(arena, "eval:{s}", .{a.did});
            try projectUpdate(arena, conn, a, if (si == 3) "" else original.handle, if (si == 3) "" else original.display_name, 2000000000);
        }
        try conn.exec("COMMIT", .{});
        const end = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;
        std.debug.print("FANOUT scenario={s} actors=500 committed_us={d}\n", .{ scenario, @divTrunc(end - start, 1000) });
    }
}

fn projectUpdate(arena: Allocator, conn: zqlite.Conn, a: Actor, old_handle: []const u8, old_display: []const u8, updated_at: i64) !void {
    return projectUpdateWithPds(arena, conn, a, old_handle, old_display, a.pds, updated_at);
}
fn projectDelete(arena: Allocator, conn: zqlite.Conn, did: []const u8, old_handle: []const u8, old_display: []const u8, updated_at: i64) !void {
    return projectDeleteWithPds(arena, conn, did, old_handle, old_display, "", updated_at);
}
