//! Search service snapshot consumer — the live side of the promote pipeline.
//!
//! Polls R2 `latest.json` (a copy of the active build's manifest). When it
//! points at a build we don't already have attached, it downloads the
//! snapshot into the inactive A/B slot, then runs the full gate before the
//! file is allowed to serve:
//!
//!   download → verifyFile (size+sha256) → validateSnapshot (schema/versions/
//!   floors) → sentinel (real prefix→DID→hydrate smoke) → atomic rename →
//!   LocalDb.swapSnapshot (ATTACH into the read pool)
//!
//! "Never attach a snapshot you can't fully validate." Any failure leaves the
//! currently-attached snapshot in place and retries on the next poll.
//!
//! Disk is bounded to two files (index.A.db / index.B.db): each promote
//! targets whichever slot is NOT currently attached, so the live file is
//! never overwritten and the prior build stays as a rollback.
const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;
const io = std.Options.debug_io;

const r2 = @import("r2.zig");
const promote = @import("promote.zig");
const schema = @import("schema.zig");
const pack = @import("pack.zig");
const prefixes = @import("prefixes.zig");
const normalize = @import("normalize.zig");
const LocalDb = @import("../db/LocalDb.zig");

const log = std.log.scoped(.promote_runner);

const LATEST_KEY = "latest.json";
const SLOT_A = "index.A.db";
const SLOT_B = "index.B.db";

/// Universally-common query strings. A healthy full-corpus snapshot must
/// resolve a majority of these to non-empty prefix rows — guards against a
/// structurally-valid but empty/corrupt build slipping through the gate.
const COMMON_PROBES = [_][]const u8{ "a", "s", "m", "j", "t" };

pub const Error = error{
    SentinelNoPrefixHits,
    SentinelHydrateFailed,
};

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return std.mem.span(p);
    return null;
}

fn indexRoot() []const u8 {
    return cGetenv("INDEX_BUILD_ROOT") orelse "/data";
}

/// A persisted snapshot file is only ever `index.{A,B}.db` (or `latest.json`).
/// Anything ending in `.part` (a download renamed-but-never-adopted) or
/// containing `.partial` (rclone's multi-thread copy temp) is transient debris
/// from an interrupted/failed promote. Pure so the matching rules — which must
/// NOT match a live `index.A.db` — are unit-tested.
fn isTransientArtifact(name: []const u8) bool {
    return std.mem.endsWith(u8, name, ".part") or std.mem.indexOf(u8, name, ".partial") != null;
}

/// Delete stale `.part`/`.partial` debris in the index dir. A promote download
/// is single-threaded within checkOnce, so at the top of any poll every such
/// file is leftover from a PRIOR failed/interrupted attempt — the prior code
/// only deleted the one slot it was about to reuse, so a download to the other
/// slot (or an rclone temp) leaked and accumulated until the volume filled.
/// Best-effort: a failure to open/iterate/delete is logged, never fatal.
fn sweepStalePartials(arena: Allocator, idx_dir: []const u8) void {
    var dir = std.Io.Dir.openDirAbsolute(io, idx_dir, .{ .iterate = true }) catch |err| {
        log.warn("sweep: open {s} failed: {s}", .{ idx_dir, @errorName(err) });
        return;
    };
    defer dir.close(io);

    // Collect first (entry.name borrows the iterator's buffer, invalidated on
    // the next next()), then delete — also avoids mutating the dir mid-iterate.
    var stale: std.ArrayListUnmanaged([]const u8) = .empty;
    var it = dir.iterate();
    while (true) {
        const entry = (it.next(io) catch |err| {
            log.warn("sweep: iterate {s} failed: {s}", .{ idx_dir, @errorName(err) });
            break;
        }) orelse break;
        if (entry.kind != .file) continue;
        if (isTransientArtifact(entry.name)) {
            const dup = arena.dupe(u8, entry.name) catch continue;
            stale.append(arena, dup) catch continue;
        }
    }
    for (stale.items) |name| {
        dir.deleteFile(io, name) catch |err| {
            log.warn("sweep: delete {s} failed: {s}", .{ name, @errorName(err) });
            continue;
        };
        log.info("sweep: removed stale partial {s}", .{name});
    }
}

/// Poll loop. Spawned as a concurrent task by main.zig for the search role.
/// Never returns; checkOnce failures are logged and retried on the next tick
/// rather than tearing down the task.
pub fn watchLoop(gpa: Allocator, db: *LocalDb) void {
    const interval_s = pollIntervalSecs();
    log.info("promote watcher started: poll every {d}s", .{interval_s});
    while (true) {
        checkOnce(gpa, db) catch |err| {
            log.err("promote check failed: {s} (will retry)", .{@errorName(err)});
        };
        io.sleep(.{ .nanoseconds = interval_s * std.time.ns_per_s }, .real) catch return;
    }
}

fn pollIntervalSecs() u64 {
    const s = cGetenv("INDEX_POLL_SECS") orelse return 300;
    return std.fmt.parseInt(u64, s, 10) catch 300;
}

/// One poll: fetch latest.json, and if it names a build we don't have
/// attached, download + gate + swap it in. Idempotent — a no-op when already
/// current.
pub fn checkOnce(gpa: Allocator, db: *LocalDb) !void {
    var arena_state = std.heap.ArenaAllocator.init(gpa);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const cfg = try r2.configure(arena);
    const dir = indexRoot();
    var idx_dir_buf: [256]u8 = undefined;
    const idx_dir = try std.fmt.bufPrint(&idx_dir_buf, "{s}/index", .{dir});
    std.Io.Dir.createDirPath(.cwd(), io, idx_dir) catch |err| switch (err) {
        error.PathAlreadyExists => {},
        else => return err,
    };

    // 0. sweep stale .part/.partial debris before we (maybe) download a new one.
    sweepStalePartials(arena, idx_dir);

    // 1. fetch latest.json (tiny; safe to re-pull every poll) and parse it.
    const manifest_local = try std.fmt.allocPrint(arena, "{s}/{s}", .{ idx_dir, LATEST_KEY });
    try r2.download(arena, cfg, LATEST_KEY, manifest_local);
    const json = try readSmallFile(arena, manifest_local);
    const parsed = try promote.parse(arena, json);
    defer parsed.deinit();
    const m = parsed.value;

    // 2. already current? compare to the attached snapshot's build_id.
    if (try db.attachedBuildId(arena)) |cur| {
        if (std.mem.eql(u8, cur, m.active_build)) {
            log.info("already on build {s}; nothing to do", .{cur});
            return;
        }
        log.info("new build available: {s} (current {s})", .{ m.active_build, cur });
    } else {
        log.info("no snapshot attached; promoting first build {s}", .{m.active_build});
    }

    // 2b. fast path: if a local A/B slot already holds active_build (e.g. after
    // a restart — snapshot_path is in-memory), adopt it without re-downloading
    // the multi-GB artifact.
    if (try adoptIfPresent(arena, db, idx_dir, m)) return;

    // 3. pick the inactive slot so we never overwrite the live file.
    const target = pickSlot(db.snapshotPath(), idx_dir, arena) catch return error.OutOfMemory;
    const part = try std.fmt.allocPrint(arena, "{s}.part", .{target});
    std.Io.Dir.deleteFile(.cwd(), io, part) catch {}; // clear any stale partial

    // 4. download the snapshot into the slot's .part file.
    try r2.download(arena, cfg, m.snapshot_url, part);

    // 5. verify bytes against the manifest before we trust a single row.
    try promote.verifyFile(part, m.byte_size, m.sha256);

    // 6. open read-only, validate schema/versions/floors, then sentinel-smoke.
    const part_z = try arena.dupeZ(u8, part);
    {
        const snap = try promote.openSnapshotReadOnly(part_z);
        defer snap.close();
        const prev_actors = db.attachedMetaInt(schema.meta.actor_count);
        const prev_prefix = db.attachedMetaInt(schema.meta.prefix_count);
        try promote.validateSnapshot(arena, snap, m, prev_actors, prev_prefix);
        try sentinelCheck(arena, snap, &COMMON_PROBES);
    }

    // 7. atomic rename .part → slot, then ATTACH-swap into the read pool.
    try std.Io.Dir.renameAbsolute(part, target, io);
    try db.swapSnapshot(target);
    // Prune sync overlay rows the new base now covers (operator rows survive).
    _ = db.compactOverlay(m.source_watermark, null, null);
    log.info("promoted build {s} → {s}", .{ m.active_build, target });
}

/// If a local A/B slot file already holds `active_build`, validate + attach it
/// (no download). Returns true if adopted. Used after a restart, where the
/// in-memory snapshot_path is null but the artifact is still on the volume.
/// Skips the multi-GB sha re-verify (the file was verified when written +
/// renamed atomically) but still runs validateSnapshot + sentinel.
fn adoptIfPresent(arena: Allocator, db: *LocalDb, idx_dir: []const u8, m: promote.Manifest) !bool {
    for ([_][]const u8{ SLOT_A, SLOT_B }) |slot| {
        const path = try std.fmt.allocPrint(arena, "{s}/{s}", .{ idx_dir, slot });
        const path_z = try arena.dupeZ(u8, path);
        const snap = promote.openSnapshotReadOnly(path_z) catch continue; // missing/unreadable → skip
        var adopt = false;
        {
            defer snap.close();
            const bid = (schema.getMeta(snap, arena, schema.meta.build_id) catch null) orelse continue;
            if (!std.mem.eql(u8, bid, m.active_build)) continue;
            promote.validateSnapshot(arena, snap, m, 0, 0) catch |err| {
                log.warn("adopt: validate failed for {s}: {s}", .{ path, @errorName(err) });
                continue;
            };
            sentinelCheck(arena, snap, &COMMON_PROBES) catch |err| {
                log.warn("adopt: sentinel failed for {s}: {s}", .{ path, @errorName(err) });
                continue;
            };
            adopt = true;
        }
        if (adopt) {
            try db.swapSnapshot(path);
            _ = db.compactOverlay(m.source_watermark, null, null);
            log.info("adopted on-disk snapshot {s} (build {s}, no download)", .{ path, m.active_build });
            return true;
        }
    }
    return false;
}

/// Return the A/B slot path NOT currently attached, so a download can never
/// clobber the live snapshot. Defaults to slot A when nothing is attached.
fn pickSlot(current: ?[]const u8, idx_dir: []const u8, arena: Allocator) ![]const u8 {
    const use_a = if (current) |c| !std.mem.endsWith(u8, c, SLOT_A) else true;
    return std.fmt.allocPrint(arena, "{s}/{s}", .{ idx_dir, if (use_a) SLOT_A else SLOT_B });
}

/// Semantic smoke check on a snapshot BEFORE it's attached. Derives prefix
/// keys for common queries using the SAME normalize+prefixes code the builder
/// used, confirms a majority resolve to non-empty prefix rows, and walks one
/// prefix DID through actor_compact_base to prove the hydrate path works.
/// This is the gap between "the file parses" and "the file answers queries".
fn sentinelCheck(arena: Allocator, snap: zqlite.Conn, probes: []const []const u8) !void {
    var resolved: usize = 0;
    var hydrated = false;

    for (probes) |q| {
        const norm = try normalize.query(arena, q);
        if (norm.len == 0) continue;
        const keys = try prefixes.queryKeys(arena, norm);
        for (keys) |key| {
            const row = snap.row("SELECT dids FROM prefix_top_base WHERE key = ?", .{key}) catch continue;
            if (row) |r| {
                defer r.deinit();
                var it = pack.DidListIter.init(r.blob(0)) catch continue;
                if (it.next()) |entry| {
                    resolved += 1;
                    if (!hydrated) hydrated = probeHydrate(snap, entry.did);
                    break;
                }
            }
        }
    }

    // majority must resolve — one cold letter shouldn't fail a healthy build.
    // warn (not err) so the test runner doesn't flag the negative-path tests;
    // checkOnce/watchLoop log .err when the gate actually rejects a build.
    if (resolved * 2 < probes.len) {
        log.warn("sentinel: only {d}/{d} probes resolved to non-empty prefix rows", .{ resolved, probes.len });
        return Error.SentinelNoPrefixHits;
    }
    if (!hydrated) {
        log.warn("sentinel: prefix DIDs did not hydrate to an actor blob", .{});
        return Error.SentinelHydrateFailed;
    }
    log.info("sentinel ok: {d}/{d} probes resolved, hydrate verified", .{ resolved, probes.len });
}

fn probeHydrate(snap: zqlite.Conn, did: []const u8) bool {
    const row = snap.row("SELECT blob FROM actor_compact_base WHERE did = ?", .{did}) catch return false;
    if (row) |r| {
        defer r.deinit();
        _ = pack.unpackActor(r.blob(0)) catch return false;
        return true;
    }
    return false;
}

/// Operational status for GET /index/status — no query, no read-pool work.
/// Reports whether promotion is enabled, what's attached, and the attached
/// build's identity + counts. Kept deliberately side-effect-free so it's a
/// safe liveness/health probe; semantic query testing lives in writeProbe.
pub fn writeStatus(arena: Allocator, db: *LocalDb, jw: anytype) !void {
    try jw.beginObject();

    // always on: the promote watcher is how the search role becomes ready
    try jw.objectField("promote_enabled");
    try jw.write(true);

    try jw.objectField("attached");
    try jw.write(db.snapshotPath() != null);

    try jw.objectField("snapshot_path");
    if (db.snapshotPath()) |p| try jw.write(p) else try jw.write(null);

    try jw.objectField("build_id");
    if (try db.attachedBuildId(arena)) |bid| try jw.write(bid) else try jw.write(null);

    try jw.objectField("actor_count");
    try jw.write(db.attachedMetaInt(schema.meta.actor_count));
    try jw.objectField("prefix_count");
    try jw.write(db.attachedMetaInt(schema.meta.prefix_count));

    // p0..p100 of quality_score in the attached build, so the worker's
    // reputation page computes a percentile without a corpus COUNT(*)
    try jw.objectField("score_quantiles");
    if (try db.attachedMetaText(arena, schema.meta.score_quantiles)) |q| {
        try jw.beginWriteRaw();
        try jw.writer.writeAll(q);
        jw.endWriteRaw();
    } else try jw.write(null);

    // overlay gauge: how much lives above the base, and whether the last
    // compaction finished. A growing actor count with complete=0 is the
    // ratchet that put the box in a restart loop for ten days (2026-08-19..29).
    try jw.objectField("overlay_actor_rows");
    try jw.write(db.overlayActorRows());
    try jw.objectField("overlay_compact");
    try jw.beginObject();
    try jw.objectField("deleted");
    try jw.write(db.syncMetaInt("overlay_compact_deleted"));
    try jw.objectField("ms");
    try jw.write(db.syncMetaInt("overlay_compact_ms"));
    try jw.objectField("complete");
    try jw.write(db.syncMetaInt("overlay_compact_complete") == 1);
    try jw.objectField("at");
    try jw.write(db.syncMetaInt("overlay_compact_at"));
    try jw.endObject();

    try jw.endObject();
}

/// Semantic probe for GET /debug/index?q=… — runs a LIVE prefix lookup for
/// `q` through the read pool (the exact Step-2 serving path: derive keys via
/// the real normalize+prefixes, hit idx.prefix_top_base, decode a DID). Lets
/// us prove on Fly that an attached snapshot actually answers queries without
/// flipping the production /search path over to it.
pub fn writeProbe(arena: Allocator, db: *LocalDb, q: []const u8, jw: anytype) !void {
    try jw.beginObject();
    try jw.objectField("query");
    try jw.write(q);

    if (db.snapshotPath() == null) {
        try jw.objectField("attached");
        try jw.write(false);
        try jw.endObject();
        return;
    }

    const norm = try normalize.query(arena, q);
    const keys = try prefixes.queryKeys(arena, norm);

    try jw.objectField("keys");
    try jw.beginArray();
    for (keys) |k| try jw.write(k);
    try jw.endArray();

    var top_key: ?[]const u8 = null;
    var n_dids: usize = 0;
    var sample_did: ?[]const u8 = null;

    const lease = db.checkoutRead() catch {
        try jw.objectField("error");
        try jw.write("pool_exhausted");
        try jw.endObject();
        return;
    };
    {
        defer lease.release();
        for (keys) |k| {
            var rows = lease.query("SELECT dids FROM idx.prefix_top_base WHERE key = ?", .{k}) catch continue;
            defer rows.deinit();
            if (rows.next()) |row| {
                const blob = row.blob(0);
                const c = pack.count(blob) catch 0;
                if (c > 0) {
                    top_key = k; // arena-owned (from queryKeys), safe past deinit
                    n_dids = c;
                    var it = pack.DidListIter.init(blob) catch break;
                    if (it.next()) |e| sample_did = try arena.dupe(u8, e.did);
                    break;
                }
            }
        }
    }

    try jw.objectField("top_key");
    if (top_key) |tk| try jw.write(tk) else try jw.write(null);
    try jw.objectField("dids_in_top_key");
    try jw.write(n_dids);
    try jw.objectField("sample_did");
    if (sample_did) |sd| try jw.write(sd) else try jw.write(null);

    try jw.endObject();
}

/// Read a small file (latest.json is <1KB) fully into an arena buffer.
fn readSmallFile(arena: Allocator, path: []const u8) ![]u8 {
    const f = try std.Io.Dir.openFileAbsolute(io, path, .{});
    defer f.close(io);
    var list: std.ArrayListUnmanaged(u8) = .empty;
    var buf: [4096]u8 = undefined;
    while (true) {
        const n = f.readStreaming(io, &.{&buf}) catch |err| switch (err) {
            error.EndOfStream => break,
            else => return err,
        };
        if (n == 0) break;
        try list.appendSlice(arena, buf[0..n]);
    }
    return list.items;
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

test "pickSlot toggles A/B and defaults to A" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    try testing.expectEqualStrings("/d/index/index.A.db", try pickSlot(null, "/d/index", a));
    try testing.expectEqualStrings("/d/index/index.B.db", try pickSlot("/d/index/index.A.db", "/d/index", a));
    try testing.expectEqualStrings("/d/index/index.A.db", try pickSlot("/d/index/index.B.db", "/d/index", a));
}

test "isTransientArtifact matches debris, never live snapshots" {
    // debris the sweep must reclaim
    try testing.expect(isTransientArtifact("index.A.db.part"));
    try testing.expect(isTransientArtifact("index.B.db.part"));
    try testing.expect(isTransientArtifact("index.A.db.part.1083f833.partial"));
    try testing.expect(isTransientArtifact("index.db.partial"));
    // live files the sweep must NEVER touch
    try testing.expect(!isTransientArtifact("index.A.db"));
    try testing.expect(!isTransientArtifact("index.B.db"));
    try testing.expect(!isTransientArtifact("latest.json"));
}

/// Build an in-memory snapshot whose prefix_top_base + actor_compact_base
/// answer the given probe end-to-end (prefix row → DID → actor blob).
fn snapshotAnswering(probe: []const u8) !zqlite.Conn {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    const conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    try schema.initSnapshot(conn);

    const norm = try normalize.query(a, probe);
    const keys = try prefixes.queryKeys(a, norm);
    try testing.expect(keys.len > 0);

    const did = "did:plc:sentineltest00000000000";
    const dids_blob = try pack.packDidList(a, &.{.{ .did = did, .score = 1.0 }});
    try conn.exec("INSERT INTO prefix_top_base (key, dids) VALUES (?, ?)", .{ keys[0], zqlite.blob(dids_blob) });

    const actor_blob = try pack.packActor(a, .{
        .hidden = false,
        .quality_score = 1.0,
        .handle = "sentinel.test",
        .display = "Sentinel",
        .avatar = "",
        .labels = "[]",
        .pds = "",
        .created_at = "",
        .associated = "{}",
    });
    try conn.exec("INSERT INTO actor_compact_base (did, blob) VALUES (?, ?)", .{ did, zqlite.blob(actor_blob) });
    return conn;
}

test "sentinelCheck passes on a snapshot that answers the probe" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    const conn = try snapshotAnswering("a");
    defer conn.close();
    try sentinelCheck(a, conn, &.{"a"});
}

test "sentinelCheck rejects an empty snapshot" {
    var arena_state = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    const conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try schema.initSnapshot(conn);
    try testing.expectError(Error.SentinelNoPrefixHits, sentinelCheck(a, conn, &.{"a"}));
}
