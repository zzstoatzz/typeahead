//! Downloads and validates immutable generations, persists activation, then
//! exchanges the read pool after outstanding leases drain. The activation
//! record retains a same-watermark legacy twin for compact-format recovery.
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
const state = @import("snapshot_state.zig");

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

fn isTransientArtifact(name: []const u8) bool {
    return std.mem.endsWith(u8, name, ".part") or std.mem.indexOf(u8, name, ".partial") != null;
}

fn isStagingDirectory(name: []const u8) bool {
    const prefix = ".staging-";
    return std.mem.startsWith(u8, name, prefix) and promote.validBuildId(name[prefix.len..]);
}

/// Delete stale partial files and generation staging directories. A promote download
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
        if ((entry.kind == .file and isTransientArtifact(entry.name)) or
            (entry.kind == .directory and isStagingDirectory(entry.name)))
        {
            const dup = arena.dupe(u8, entry.name) catch continue;
            stale.append(arena, dup) catch continue;
        }
    }
    for (stale.items) |name| {
        dir.deleteTree(io, name) catch |err| {
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
            if (err == error.Canceled) return;
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
    const root = try std.fmt.allocPrint(arena, "{s}/index", .{indexRoot()});
    try std.Io.Dir.createDirPath(.cwd(), io, root);
    sweepStalePartials(arena, root);
    try recoverLocal(arena, db, root);
    const cfg = try r2.configure(arena);
    const latest_key = cGetenv("INDEX_LATEST_KEY") orelse LATEST_KEY;
    if (!promote.validObjectKey(latest_key)) return error.InvalidPointerKey;
    const downloaded = try std.fmt.allocPrint(arena, "{s}/latest.download.part", .{root});
    try r2.download(arena, cfg, latest_key, downloaded);
    const parsed = try promote.parse(arena, try state.readSmall(arena, downloaded));
    defer parsed.deinit();
    const m = parsed.value;
    try promote.validateManifest(m);
    if (!promote.validBuildId(m.active_build)) return error.InvalidBuildIdentity;
    if (try db.attachedBuildId(arena)) |current| {
        if (std.mem.eql(u8, current, m.active_build)) return;
        if (m.source_watermark < @as(i64, @intCast(db.attachedMetaInt(schema.meta.source_watermark)))) return error.RollbackRequiresReplay;
    }
    const entry: state.Entry = if (m.manifest_version == 1)
        (try findLegacySlot(arena, db, root, m)) orelse .{ .manifest = m }
    else
        .{ .manifest = m };
    var prior = try state.load(arena, root);
    defer if (prior) |*value| value.deinit();
    const previous: ?state.Entry = if (m.legacy_twin) |twin|
        try ensureTwin(arena, db, cfg, root, m, twin, if (prior) |value| value.value else null)
    else if (prior) |value| value.value.current else null;
    var prepared = if (entry.location == .generation)
        try ensureGeneration(arena, db, cfg, root, m)
    else
        try prepareEntry(arena, db, entry, root, false);
    defer prepared.deinit();
    try state.save(arena, root, .{ .current = entry, .previous = previous });
    db.activateSnapshot(&prepared);
    _ = db.compactOverlay(m.source_watermark, null, null);
    cleanupGenerations(arena, root, m.active_build, if (previous) |value| value.manifest.active_build else null) catch |err| {
        log.warn("generation cleanup failed: {s}", .{@errorName(err)});
    };
}

fn prepareEntry(arena: Allocator, db: *LocalDb, entry: state.Entry, root: []const u8, verify: bool) !LocalDb.PreparedSnapshot {
    const path = try entry.path(arena, root);
    if (verify or entry.manifest.manifest_version == 2) try promote.verifyFile(path, entry.manifest.byte_size, entry.manifest.sha256);
    const path_z = try arena.dupeSentinel(u8, path, 0);
    const snap = try promote.openSnapshotReadOnly(path_z);
    defer snap.close();
    try promote.validateSnapshot(arena, snap, entry.manifest, db.attachedMetaInt(schema.meta.actor_count), db.attachedMetaInt(schema.meta.prefix_count));
    var prepared = if (verify) try db.prepareSnapshot(path) else try db.prepareAdmittedSnapshot(path);
    errdefer prepared.deinit();
    if (entry.manifest.prefix_map) |map| {
        if (prepared.map == null or prepared.map.?.file_size != map.byte_size) return error.SizeMismatch;
    }
    if (verify or entry.manifest.manifest_version == 1) try sentinelPrepared(arena, prepared, &COMMON_PROBES);
    return prepared;
}

fn recoverLocal(arena: Allocator, db: *LocalDb, root: []const u8) !void {
    if ((try db.snapshotPath(arena)) != null) return;
    const parsed = (try state.load(arena, root)) orelse return;
    defer parsed.deinit();
    var selected = parsed.value.current;
    var prepared = prepareEntry(arena, db, selected, root, false) catch |err| blk: {
        const previous = parsed.value.previous orelse return err;
        if (!state.rollbackAllowed(selected.manifest, previous.manifest)) return error.RollbackRequiresReplay;
        const fallback = try prepareEntry(arena, db, previous, root, previous.manifest.manifest_version == 1);
        selected = previous;
        break :blk fallback;
    };
    defer prepared.deinit();
    if (!std.mem.eql(u8, selected.manifest.active_build, parsed.value.current.manifest.active_build)) try state.save(arena, root, .{ .current = selected });
    db.activateSnapshot(&prepared);
    _ = db.compactOverlay(selected.manifest.source_watermark, null, null);
}

fn ensureGeneration(arena: Allocator, db: *LocalDb, cfg: r2.Config, root: []const u8, m: promote.Manifest) !LocalDb.PreparedSnapshot {
    const entry: state.Entry = .{ .manifest = m };
    const target = try entry.path(arena, root);
    const directory = std.fs.path.dirname(target).?;
    const canonical = try std.json.Stringify.valueAlloc(arena, m, .{});
    const manifest_path = try std.fmt.allocPrint(arena, "{s}/manifest.json", .{directory});
    if (state.readSmall(arena, manifest_path)) |existing| {
        if (!std.mem.eql(u8, existing, canonical)) return error.GenerationIdentityCollision;
        return prepareEntry(arena, db, entry, root, true);
    } else |err| if (err != error.FileNotFound) return err;
    const parent = try std.fmt.allocPrint(arena, "{s}/generations", .{root});
    try std.Io.Dir.createDirPath(.cwd(), io, parent);
    const staging = try std.fmt.allocPrint(arena, "{s}/.staging-{s}", .{ root, m.active_build });
    std.Io.Dir.deleteTree(.cwd(), io, staging) catch |err| if (err != error.FileNotFound) return err;
    try std.Io.Dir.createDirPath(.cwd(), io, staging);
    defer std.Io.Dir.deleteTree(.cwd(), io, staging) catch {};
    const file_path = try std.fmt.allocPrint(arena, "{s}/index.db", .{staging});
    try r2.download(arena, cfg, m.snapshot_url, file_path);
    try promote.verifyFile(file_path, m.byte_size, m.sha256);
    if (m.prefix_map) |map| {
        const side = try std.fmt.allocPrint(arena, "{s}.prefix", .{file_path});
        try r2.download(arena, cfg, map.object_key, side);
    }
    const path_z = try arena.dupeSentinel(u8, file_path, 0);
    const snap = try promote.openSnapshotReadOnly(path_z);
    defer snap.close();
    try promote.validateSnapshot(arena, snap, m, db.attachedMetaInt(schema.meta.actor_count), db.attachedMetaInt(schema.meta.prefix_count));
    var prepared = try db.prepareSnapshot(file_path);
    errdefer prepared.deinit();
    if (m.prefix_map) |map| if (prepared.map == null or prepared.map.?.file_size != map.byte_size) return error.SizeMismatch;
    try sentinelPrepared(arena, prepared, &COMMON_PROBES);
    const final_path = try db.allocator.dupe(u8, target);
    errdefer db.allocator.free(final_path);
    for ([_][]const u8{ "index.db", "index.db.prefix" }) |name| {
        if (m.prefix_map == null and std.mem.endsWith(u8, name, ".prefix")) continue;
        const file = try std.Io.Dir.openFileAbsolute(io, try std.fmt.allocPrint(arena, "{s}/{s}", .{ staging, name }), .{});
        defer file.close(io);
        try file.sync(io);
    }
    try state.writeFile(try std.fmt.allocPrint(arena, "{s}/manifest.json", .{staging}), canonical);
    try state.syncDirectory(staging);
    try std.Io.Dir.renameAbsolute(staging, directory, io);
    try state.syncDirectory(parent);
    try state.syncDirectory(root);
    db.allocator.free(prepared.path);
    prepared.path = final_path;
    return prepared;
}

fn ensureTwin(arena: Allocator, db: *LocalDb, cfg: r2.Config, root: []const u8, m: promote.Manifest, twin: promote.LegacyTwin, prior: ?state.State) !state.Entry {
    const downloaded = try std.fmt.allocPrint(arena, "{s}/twin.manifest.part", .{root});
    try r2.download(arena, cfg, twin.manifest.object_key, downloaded);
    try promote.verifyFile(downloaded, twin.manifest.byte_size, twin.manifest.sha256);
    const legacy = try std.json.parseFromSliceLeaky(promote.Manifest, arena, try state.readSmall(arena, downloaded), .{ .ignore_unknown_fields = true });
    try promote.validateManifest(legacy);
    if (legacy.manifest_version != 1 or !std.mem.eql(u8, legacy.active_build, twin.build_id) or legacy.source_watermark != m.source_watermark or
        legacy.actor_count != m.actor_count or legacy.prefix_count != m.prefix_count or
        !std.mem.eql(u8, legacy.normalize_version, m.normalize_version) or !std.mem.eql(u8, legacy.scoring_version, m.scoring_version)) return error.InvalidLegacyTwin;
    inline for (.{ "top_n_short", "top_n_medium", "top_n_long", "overlay_top_n", "hydrate_cap", "cjk_key_count", "cjk_actor_count" }) |field| {
        if (@field(legacy, field) != @field(m, field)) return error.InvalidLegacyTwin;
    }
    if (prior) |record| {
        if (record.current.manifest.manifest_version == 1 and std.mem.eql(u8, record.current.manifest.active_build, legacy.active_build) and
            std.mem.eql(u8, record.current.manifest.sha256, legacy.sha256) and record.current.manifest.byte_size == legacy.byte_size)
        {
            var admitted = try prepareEntry(arena, db, .{ .manifest = legacy, .location = record.current.location }, root, true);
            defer admitted.deinit();
            return .{ .manifest = legacy, .location = record.current.location };
        }
    }
    var prepared = try ensureGeneration(arena, db, cfg, root, legacy);
    defer prepared.deinit();
    return .{ .manifest = legacy };
}

fn sentinelPrepared(arena: Allocator, prepared: LocalDb.PreparedSnapshot, probes: []const []const u8) !void {
    var resolved: usize = 0;
    var hydrated = false;
    for (probes) |query| {
        const norm = try normalize.query(arena, query);
        for (try prefixes.queryKeys(arena, norm)) |key| {
            const blob = (try prepared.basePrefix(arena, key)) orelse continue;
            var it = pack.DidListIter.init(blob) catch continue;
            const entry = it.next() orelse continue;
            resolved += 1;
            if (try prepared.baseActor(arena, entry.did)) |actor| {
                _ = pack.unpackActor(actor) catch return Error.SentinelHydrateFailed;
                hydrated = true;
            }
            break;
        }
    }
    if (resolved * 2 < probes.len) return Error.SentinelNoPrefixHits;
    if (!hydrated) return Error.SentinelHydrateFailed;
}

fn cleanupGenerations(arena: Allocator, root: []const u8, current: []const u8, previous: ?[]const u8) !void {
    const parent = try std.fmt.allocPrint(arena, "{s}/generations", .{root});
    var dir = std.Io.Dir.openDirAbsolute(io, parent, .{ .iterate = true }) catch |err| switch (err) {
        error.FileNotFound => return,
        else => return err,
    };
    defer dir.close(io);
    var names: std.ArrayListUnmanaged([]const u8) = .empty;
    var iterator = dir.iterate();
    while (try iterator.next(io)) |item| {
        if (item.kind != .directory or !promote.validBuildId(item.name) or std.mem.eql(u8, item.name, current)) continue;
        if (previous) |p| if (std.mem.eql(u8, p, item.name)) continue;
        try names.append(arena, try arena.dupe(u8, item.name));
    }
    for (names.items) |name| try std.Io.Dir.deleteTree(.cwd(), io, try std.fmt.allocPrint(arena, "{s}/{s}", .{ parent, name }));
}

fn findLegacySlot(arena: Allocator, db: *LocalDb, root: []const u8, m: promote.Manifest) !?state.Entry {
    for ([_][]const u8{ SLOT_A, SLOT_B }) |slot| {
        const path = try std.fmt.allocPrintSentinel(arena, "{s}/{s}", .{ root, slot }, 0);
        const snap = promote.openSnapshotReadOnly(path) catch continue;
        defer snap.close();
        const bid = (try schema.getMeta(snap, arena, schema.meta.build_id)) orelse continue;
        if (!std.mem.eql(u8, bid, m.active_build)) continue;
        promote.validateSnapshot(arena, snap, m, db.attachedMetaInt(schema.meta.actor_count), db.attachedMetaInt(schema.meta.prefix_count)) catch continue;
        sentinelCheck(arena, snap, &COMMON_PROBES) catch continue;
        return .{ .manifest = m, .location = if (std.mem.eql(u8, slot, SLOT_A)) .slot_a else .slot_b };
    }
    return null;
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
    try jw.write((try db.snapshotPath(arena)) != null);

    try jw.objectField("snapshot_path");
    if ((try db.snapshotPath(arena))) |p| try jw.write(p) else try jw.write(null);

    try jw.objectField("build_id");
    if (try db.attachedBuildId(arena)) |bid| try jw.write(bid) else try jw.write(null);

    try jw.objectField("snapshot_format");
    try jw.write(if (db.attachedMetaInt("flat_prefix_version") == 5) "FLATPX05" else "sqlite");
    try jw.objectField("source_watermark");
    try jw.write(db.attachedMetaInt(schema.meta.source_watermark));

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

    if ((try db.snapshotPath(arena)) == null) {
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
            const blob = if (lease.prefix_map) |map|
                (try map.findTagged(arena, .prefix, k)) orelse continue
            else blk: {
                var rows = try lease.query("SELECT dids FROM idx.prefix_top_base WHERE key = ?", .{k});
                defer rows.deinit();
                const row = rows.next() orelse continue;
                break :blk try arena.dupe(u8, row.blob(0));
            };
            const count = try pack.count(blob);
            if (count == 0) continue;
            top_key = k;
            n_dids = count;
            var it = try pack.DidListIter.init(blob);
            if (it.next()) |entry| sample_did = try arena.dupe(u8, entry.did);
            break;
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

const testing = std.testing;

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

test "interrupted generation cleanup preserves serving artifacts and unrelated directories" {
    var directory = "/tmp/typeahead-partials-XXXXXX".*;
    _ = mkdtemp(&directory) orelse return error.TestDirectoryFailed;
    defer std.Io.Dir.deleteTree(.cwd(), io, &directory) catch {};
    var storage = std.heap.ArenaAllocator.init(testing.allocator);
    defer storage.deinit();
    const arena = storage.allocator();
    const root: []const u8 = &directory;
    const removed = [_][]const u8{ ".staging-interrupted/index.db", ".staging-interrupted/index.db.prefix.partial", "active.json.part" };
    const retained = [_][]const u8{ "index.A.db", "index.B.db", "active.json", "generations/current/index.db", "generations/previous/index.db", ".staging-/keep", "unrelated.partial/keep" };
    for (removed ++ retained) |name| {
        const path = try std.fmt.allocPrint(arena, "{s}/{s}", .{ root, name });
        try std.Io.Dir.createDirPath(.cwd(), io, std.fs.path.dirname(path).?);
        try state.writeFile(path, "retained bytes");
    }
    sweepStalePartials(arena, root);
    sweepStalePartials(arena, root);
    for (removed) |name| try testing.expectError(error.FileNotFound, state.readSmall(arena, try std.fmt.allocPrint(arena, "{s}/{s}", .{ root, name })));
    for (retained) |name| try testing.expectEqualStrings("retained bytes", try state.readSmall(arena, try std.fmt.allocPrint(arena, "{s}/{s}", .{ root, name })));
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

extern "c" fn mkdtemp(template: [*:0]u8) ?[*:0]u8;
extern "c" fn setenv(name: [*:0]const u8, value: [*:0]const u8, overwrite: c_int) c_int;
extern "c" fn unsetenv(name: [*:0]const u8) c_int;

fn recoveryFixture(arena: Allocator, root: []const u8, id: []const u8, watermark: i64) !promote.Manifest {
    const directory = try std.fmt.allocPrint(arena, "{s}/generations/{s}", .{ root, id });
    try std.Io.Dir.createDirPath(.cwd(), io, directory);
    const path = try std.fmt.allocPrintSentinel(arena, "{s}/index.db", .{directory}, 0);
    var m: promote.Manifest = .{
        .manifest_version = 1,
        .active_build = id,
        .promoted_at = 0,
        .snapshot_url = try std.fmt.allocPrint(arena, "builds/{s}/index.db", .{id}),
        .byte_size = 0,
        .sha256 = "",
        .source_watermark = watermark,
        .actor_count = 1,
        .prefix_count = 5,
        .normalize_version = normalize.VERSION,
        .scoring_version = @import("scoring.zig").VERSION,
        .top_n_short = 300,
        .top_n_medium = 200,
        .top_n_long = 100,
        .overlay_top_n = 100,
        .hydrate_cap = 50,
        .builder_version = "test",
        .cjk_key_count = promote.CJK_KEY_FLOOR,
        .cjk_actor_count = 1,
    };
    {
        const conn = try zqlite.open(path, zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
        defer conn.close();
        try schema.initSnapshot(conn);
        try schema.setMeta(conn, "build_id", id);
        try schema.setMeta(conn, "normalize_version", m.normalize_version);
        try schema.setMeta(conn, "scoring_version", m.scoring_version);
        inline for (.{ "source_watermark", "actor_count", "prefix_count", "top_n_short", "top_n_medium", "top_n_long", "overlay_top_n", "hydrate_cap", "cjk_key_count", "cjk_actor_count" }) |field| {
            try schema.setMeta(conn, field, try std.fmt.allocPrint(arena, "{d}", .{@field(m, field)}));
        }
        const did = "did:plc:recovery";
        const actor = try pack.packActor(arena, .{ .hidden = false, .quality_score = 1, .handle = id, .display = "", .avatar = "", .labels = "[]", .pds = "", .created_at = "", .associated = "{}" });
        try conn.exec("INSERT INTO actor_compact_base VALUES(?,?)", .{ did, zqlite.blob(actor) });
        const posting = try pack.packDidList(arena, &.{.{ .did = did, .score = 1 }});
        for (COMMON_PROBES) |q| {
            const keys = try prefixes.queryKeys(arena, try normalize.query(arena, q));
            try conn.exec("INSERT INTO prefix_top_base VALUES(?,?)", .{ keys[0], zqlite.blob(posting) });
        }
    }
    var hash: [64]u8 = undefined;
    m.byte_size = try @import("manifest.zig").sha256File(path, &hash);
    m.sha256 = try arena.dupe(u8, &hash);
    try state.writeFile(try std.fmt.allocPrint(arena, "{s}/manifest.json", .{directory}), try std.json.Stringify.valueAlloc(arena, m, .{}));
    return m;
}

test "recovery ignores partial activation and refuses rollback behind pruned watermark" {
    var directory = "/tmp/typeahead-recovery-XXXXXX".*;
    _ = mkdtemp(&directory) orelse return error.TestDirectoryFailed;
    defer std.Io.Dir.deleteTree(.cwd(), io, &directory) catch {};
    var storage = std.heap.ArenaAllocator.init(testing.allocator);
    defer storage.deinit();
    const arena = storage.allocator();
    const root: []const u8 = &directory;
    const previous_env = if (cGetenv("LOCAL_DB_PATH")) |v| try arena.dupeSentinel(u8, v, 0) else null;
    const previous_tmp = if (cGetenv("SQLITE_TMPDIR")) |v| try arena.dupeSentinel(u8, v, 0) else null;
    defer {
        if (previous_env) |v| {
            _ = setenv("LOCAL_DB_PATH", v, 1);
        } else {
            _ = unsetenv("LOCAL_DB_PATH");
        }
        if (previous_tmp) |v| {
            _ = setenv("SQLITE_TMPDIR", v, 1);
        } else {
            _ = unsetenv("SQLITE_TMPDIR");
        }
    }
    _ = setenv("LOCAL_DB_PATH", try std.fmt.allocPrintSentinel(arena, "{s}/overlay.db", .{root}, 0), 1);
    _ = setenv("SQLITE_TMPDIR", &directory, 1);
    const old = try recoveryFixture(arena, root, "legacy", 100);
    const current = try recoveryFixture(arena, root, "current", 100);
    _ = try recoveryFixture(arena, root, "orphan", 100);
    try state.save(arena, root, .{ .current = .{ .manifest = current }, .previous = .{ .manifest = old } });
    try state.writeFile(try std.fmt.allocPrint(arena, "{s}/active.json.part", .{root}), "{unfinished");
    {
        var local = LocalDb.init(testing.allocator);
        defer local.deinit();
        try local.open();
        try local.conn.?.exec("INSERT INTO actor_overlay(did,blob,hidden,updated_at,source) VALUES('newer',X'00',0,101,0),('covered',X'00',0,99,0)", .{});
        const slot_path = try std.fmt.allocPrint(arena, "{s}/{s}", .{ root, SLOT_A });
        try state.writeFile(slot_path, try state.readSmall(arena, try std.fmt.allocPrint(arena, "{s}/generations/legacy/index.db", .{root})));
        const candidate = (try findLegacySlot(arena, &local, root, old)).?;
        try testing.expect(candidate.location == .slot_a);
        try testing.expect((try local.snapshotPath(arena)) == null);
        try testing.expectEqual(@as(i64, 2), local.overlayActorRows());
        try recoverLocal(arena, &local, root);
        try testing.expectEqualStrings("current", (try local.attachedBuildId(arena)).?);
        try testing.expectEqual(@as(i64, 1), local.overlayActorRows());
        try cleanupGenerations(arena, root, "current", "legacy");
        try testing.expectError(error.FileNotFound, state.readSmall(arena, try std.fmt.allocPrint(arena, "{s}/generations/orphan/manifest.json", .{root})));
    }
    try state.writeFile(try std.fmt.allocPrint(arena, "{s}/generations/current/index.db", .{root}), "broken");
    {
        var local = LocalDb.init(testing.allocator);
        defer local.deinit();
        try local.open();
        try recoverLocal(arena, &local, root);
        try testing.expectEqualStrings("legacy", (try local.attachedBuildId(arena)).?);
        try testing.expectEqual(@as(i64, 1), local.overlayActorRows());
        var saved = (try state.load(arena, root)).?;
        defer saved.deinit();
        try testing.expectEqualStrings("legacy", saved.value.current.manifest.active_build);
    }
    var future = current;
    future.source_watermark = 101;
    try state.save(arena, root, .{ .current = .{ .manifest = future }, .previous = .{ .manifest = old } });
    {
        var local = LocalDb.init(testing.allocator);
        defer local.deinit();
        try local.open();
        try testing.expectError(error.RollbackRequiresReplay, recoverLocal(arena, &local, root));
        try testing.expect((try local.snapshotPath(arena)) == null);
        try testing.expectEqual(@as(i64, 1), local.overlayActorRows());
    }
}

test "real rclone transport admits immutable generation and rejects corrupt replacement" {
    if (cGetenv("TYPEAHEAD_TEST_RCLONE") == null) return error.SkipZigTest;
    var directory = "/tmp/typeahead-transport-XXXXXX".*;
    _ = mkdtemp(&directory) orelse return error.TestDirectoryFailed;
    defer std.Io.Dir.deleteTree(.cwd(), io, &directory) catch {};
    var storage = std.heap.ArenaAllocator.init(testing.allocator);
    defer storage.deinit();
    const arena = storage.allocator();
    const root = try std.fmt.allocPrint(arena, "{s}/local", .{directory});
    const remote = try std.fmt.allocPrint(arena, "{s}/remote", .{directory});
    try std.Io.Dir.createDirPath(.cwd(), io, root);
    var m = try recoveryFixture(arena, remote, "test-build", 100);
    m.snapshot_url = "generations/test-build/index.db";
    const config = try std.fmt.allocPrint(arena, "{s}/rclone.conf", .{directory});
    try state.writeFile(config, "[r2]\ntype = local\n");
    var local = LocalDb.init(testing.allocator);
    defer local.deinit();
    const overlay = try std.fmt.allocPrintSentinel(arena, "{s}/overlay.db", .{directory}, 0);
    const conn = try zqlite.open(overlay, zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    conn.close();
    local.path = overlay;
    const cfg: r2.Config = .{ .bucket = remote, .config_path = config };
    {
        var admitted = try ensureGeneration(arena, &local, cfg, root, m);
        defer admitted.deinit();
        try testing.expectEqualStrings("test-build", (try pack.unpackActor((try admitted.baseActor(arena, "did:plc:recovery")).?)).handle);
        try testing.expect(std.mem.indexOf(u8, admitted.path, "/generations/test-build/") != null);
    }
    {
        var admitted = try ensureGeneration(arena, &local, cfg, root, m);
        defer admitted.deinit();
        try sentinelPrepared(arena, admitted, &COMMON_PROBES);
    }
    var collision = m;
    collision.source_watermark += 1;
    try testing.expectError(error.GenerationIdentityCollision, ensureGeneration(arena, &local, cfg, root, collision));
    var bad = try recoveryFixture(arena, remote, "corrupt", 100);
    bad.snapshot_url = "generations/corrupt/index.db";
    try state.writeFile(try std.fmt.allocPrint(arena, "{s}/{s}", .{ remote, bad.snapshot_url }), "truncated");
    try testing.expectError(error.SizeMismatch, ensureGeneration(arena, &local, cfg, root, bad));
    try testing.expectError(error.FileNotFound, state.readSmall(arena, try std.fmt.allocPrint(arena, "{s}/generations/corrupt/manifest.json", .{root})));
    try testing.expectError(error.FileNotFound, state.readSmall(arena, try std.fmt.allocPrint(arena, "{s}/.staging-corrupt/index.db", .{root})));
}

test "compact pair transport rejects missing and mismatched maps and recovers through verified twin" {
    const remote = cGetenv("TYPEAHEAD_TEST_PAIR_ROOT") orelse return error.SkipZigTest;
    var directory = "/tmp/typeahead-pair-XXXXXX".*;
    _ = mkdtemp(&directory) orelse return error.TestDirectoryFailed;
    defer std.Io.Dir.deleteTree(.cwd(), io, &directory) catch {};
    var storage = std.heap.ArenaAllocator.init(testing.allocator);
    defer storage.deinit();
    const arena = storage.allocator();
    const root: []const u8 = &directory;
    const previous_env = if (cGetenv("LOCAL_DB_PATH")) |v| try arena.dupeSentinel(u8, v, 0) else null;
    const previous_tmp = if (cGetenv("SQLITE_TMPDIR")) |v| try arena.dupeSentinel(u8, v, 0) else null;
    defer {
        if (previous_env) |v| {
            _ = setenv("LOCAL_DB_PATH", v, 1);
        } else {
            _ = unsetenv("LOCAL_DB_PATH");
        }
        if (previous_tmp) |v| {
            _ = setenv("SQLITE_TMPDIR", v, 1);
        } else {
            _ = unsetenv("SQLITE_TMPDIR");
        }
    }
    _ = setenv("LOCAL_DB_PATH", try std.fmt.allocPrintSentinel(arena, "{s}/overlay.db", .{root}, 0), 1);
    _ = setenv("SQLITE_TMPDIR", &directory, 1);
    const config = try std.fmt.allocPrint(arena, "{s}/rclone.conf", .{root});
    try state.writeFile(config, "[r2]\ntype = local\n");
    const cfg: r2.Config = .{ .bucket = remote, .config_path = config };
    const parsed = try promote.parse(arena, try state.readSmall(arena, try std.fmt.allocPrint(arena, "{s}/builds/compact/manifest.json", .{remote})));
    defer parsed.deinit();
    const m = parsed.value;
    try promote.validateManifest(m);
    var twin: state.Entry = undefined;
    {
        var local = LocalDb.init(testing.allocator);
        defer local.deinit();
        try local.open();
        twin = try ensureTwin(arena, &local, cfg, root, m, m.legacy_twin.?, null);
        var prepared = try ensureGeneration(arena, &local, cfg, root, m);
        defer prepared.deinit();
        try state.save(arena, root, .{ .current = .{ .manifest = m }, .previous = twin });
        local.activateSnapshot(&prepared);
        const lease = try local.checkoutRead();
        defer lease.release();
        try testing.expectEqualStrings("recovery.test", (try pack.unpackActor((try lease.baseActor(arena, "did:plc:recovery")).?)).handle);
    }
    const map_path = try std.fmt.allocPrint(arena, "{s}/generations/compact/index.db.prefix", .{root});
    const map_bytes = try state.readSmall(arena, map_path);
    try std.Io.Dir.deleteFileAbsolute(io, map_path);
    {
        var local = LocalDb.init(testing.allocator);
        defer local.deinit();
        try local.open();
        try testing.expectError(error.FlatOpenFailed, prepareEntry(arena, &local, .{ .manifest = m }, root, true));
        try recoverLocal(arena, &local, root);
        try testing.expectEqualStrings("legacy", (try local.attachedBuildId(arena)).?);
    }
    map_bytes[map_bytes.len - 1] ^= 1;
    try state.writeFile(map_path, map_bytes);
    try state.save(arena, root, .{ .current = .{ .manifest = m }, .previous = twin });
    {
        var local = LocalDb.init(testing.allocator);
        defer local.deinit();
        try local.open();
        try testing.expectError(error.FlatDigestMismatch, prepareEntry(arena, &local, .{ .manifest = m }, root, true));
        try recoverLocal(arena, &local, root);
        try testing.expectEqualStrings("legacy", (try local.attachedBuildId(arena)).?);
    }
}
