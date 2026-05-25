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
//!   - prefix diff writes only added (op=1) / removed (op=0) keys; unchanged
//!     keys stay served by the base (score drift waits for the next build).
const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;

const normalize = @import("normalize.zig");
const prefixes = @import("prefixes.zig");
const scoring = @import("scoring.zig");
const pack = @import("pack.zig");

const log = std.log.scoped(.overlay);

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
    created_at: []const u8,
    hidden: bool,
    quality_score: f32,
};

/// Project an updated/created actor. `old_handle`/`old_display` are the
/// pre-update values (the local row being replaced); pass "" for a brand-new
/// actor. `updated_at` is the SOURCE timestamp (Turso updated_at), used for
/// watermark-based compaction later.
pub fn projectUpdate(
    arena: Allocator,
    conn: zqlite.Conn,
    a: Actor,
    old_handle: []const u8,
    old_display: []const u8,
    updated_at: i64,
) !void {
    const nh = try normalize.handle(arena, a.handle);
    const nd = try normalize.display(arena, a.display_name);
    const new_keys = try prefixes.deriveAllUnique(arena, nh, nd);

    const oh = try normalize.handle(arena, old_handle);
    const od = try normalize.display(arena, old_display);
    const old_keys = try prefixes.deriveAllUnique(arena, oh, od);

    const score = scoring.scoreOf(.{ .quality_score = a.quality_score });

    const blob = try pack.packActor(arena, .{
        .hidden = a.hidden,
        .quality_score = a.quality_score,
        .handle = nh,
        .display = nd,
        .avatar = a.avatar_url,
        .labels = a.labels,
        .pds = a.pds,
        .created_at = a.created_at,
        .associated = a.associated,
    });
    try conn.exec(
        "INSERT OR REPLACE INTO actor_overlay (did, blob, hidden, updated_at, source) VALUES (?, ?, ?, ?, 0)",
        .{ a.did, zqlite.blob(blob), @as(i64, @intFromBool(a.hidden)), updated_at },
    );

    try diffPrefixes(conn, a.did, old_keys, new_keys, score, updated_at);
}

/// Project a deletion: write a global tombstone (NULL blob) and suppress the
/// actor's known prefixes so it stops appearing as a candidate. The tombstone
/// alone is sufficient for correctness (hydrate skips it); the prefix
/// suppressions just keep candidate sets tidy.
pub fn projectDelete(
    arena: Allocator,
    conn: zqlite.Conn,
    did: []const u8,
    old_handle: []const u8,
    old_display: []const u8,
    updated_at: i64,
) !void {
    try conn.exec(
        "INSERT OR REPLACE INTO actor_overlay (did, blob, hidden, updated_at, source) VALUES (?, NULL, 0, ?, 0)",
        .{ did, updated_at },
    );

    const oh = try normalize.handle(arena, old_handle);
    const od = try normalize.display(arena, old_display);
    for (try prefixes.deriveAllUnique(arena, oh, od)) |k| {
        try upsertPrefix(conn, k, did, 0, 0, updated_at);
    }
}

/// Merge-walk two sorted+deduped key lists: keys only in `old` → suppress
/// (op=0); keys only in `new` → add (op=1). Common keys are left to the base
/// (membership unchanged; score drift is corrected at the next build).
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
                oi += 1;
                ni += 1;
            },
        }
    }
}

fn upsertPrefix(conn: zqlite.Conn, key: []const u8, did: []const u8, score: f32, op: i64, updated_at: i64) !void {
    try conn.exec(
        "INSERT OR REPLACE INTO prefix_overlay (key, did, score, op, updated_at, source) VALUES (?, ?, ?, ?, ?, 0)",
        .{ key, did, @as(f64, score), op, updated_at },
    );
}

/// Compaction: drop sync-derived overlay rows at or below the new snapshot
/// watermark (the base now reflects them). Operator rows (source=1) survive —
/// a manual hide/show must not vanish on promote. Called from swapSnapshot.
pub fn compact(conn: zqlite.Conn, new_watermark: i64) !void {
    try conn.exec("DELETE FROM prefix_overlay WHERE source = 0 AND updated_at <= ?", .{new_watermark});
    try conn.exec("DELETE FROM actor_overlay WHERE source = 0 AND updated_at <= ?", .{new_watermark});
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;
const schema = @import("schema.zig");

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
    try testing.expectEqual(@as(?i64, null), try prefixOp(conn, "h:al", "did:plc:y")); // common → untouched
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
