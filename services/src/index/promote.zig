//! Search-service side of the index promote flow: parse the manifest, verify a
//! downloaded snapshot (size + sha256), and gate it before it's allowed to
//! become servable. The actual transport (R2 signed GET) and the ATTACH-swap
//! into LocalDb's read pool are wired separately; this module is the
//! transport-agnostic, unit-testable core — "never accept a snapshot you
//! can't fully validate."
//!
//! Gate order (cheapest reject first): manifest_version → size → sha256 →
//! tables present → meta present → version match → sanity floors.
const std = @import("std");
const zqlite = @import("zqlite");
const Allocator = std.mem.Allocator;

const schema = @import("schema.zig");
const normalize = @import("normalize.zig");
const scoring = @import("scoring.zig");
const manifest = @import("manifest.zig");

const log = std.log.scoped(.promote);
const io = std.Options.debug_io;

/// Mirrors the manifest contract (docs/typeahead-index-design.md). Unknown
/// fields are ignored on parse so the indexer can add fields without breaking
/// the search service; breaking changes bump `manifest_version` (checked in the gate).
pub const Artifact = struct {
    object_key: []const u8,
    byte_size: u64,
    sha256: []const u8,
};

pub const LegacyTwin = struct {
    manifest: Artifact,
    build_id: []const u8,
    source_watermark: i64,
};

pub const Manifest = struct {
    format_version: u32 = 0,
    prefix_map: ?Artifact = null,
    legacy_twin: ?LegacyTwin = null,
    manifest_version: u32,
    active_build: []const u8,
    previous_build: ?[]const u8 = null,
    promoted_at: i64,
    snapshot_url: []const u8,
    byte_size: u64,
    sha256: []const u8,
    source_watermark: i64,
    actor_count: u64,
    prefix_count: u64,
    normalize_version: []const u8,
    scoring_version: []const u8,
    top_n_short: u32,
    top_n_medium: u32,
    top_n_long: u32,
    overlay_top_n: u32,
    hydrate_cap: u32,
    builder_version: []const u8,
    // v2+ : count of CJK `n:` keys / actors. Default 0 so v1 manifests
    // (which lack these) still parse. Floored by the gate only when the
    // snapshot's normalize_version is a CJK version.
    cjk_key_count: u64 = 0,
    cjk_actor_count: u64 = 0,
};

/// Minimum CJK keys a v2-cjk build must carry. Phase 0 estimated ~1.7M for
/// ~434k CJK actors; this floor (~34× under that) exists only to reject a
/// CJK-blind build (normalizer/tokenizer regression → ~0 CJK keys), not to
/// track the true count. Cheap structural guard, no live-account dependency.
pub const CJK_KEY_FLOOR: u64 = 50_000;

pub const GateError = error{
    UnsupportedManifestVersion,
    InvalidArtifactManifest,
    SizeMismatch,
    ChecksumMismatch,
    MissingTable,
    MissingMeta,
    NormalizeVersionMismatch,
    ScoringVersionMismatch,
    ManifestMetaMismatch,
    ActorCountFloor,
    PrefixCountFloor,
    CjkKeyFloor,
};

/// Parse manifest JSON. Caller owns the returned Parsed and must deinit it
/// (the Manifest's string slices point into its arena).
pub fn parse(gpa: Allocator, json: []const u8) !std.json.Parsed(Manifest) {
    return std.json.parseFromSlice(Manifest, gpa, json, .{ .ignore_unknown_fields = true });
}

pub fn validBuildId(value: []const u8) bool {
    if (value.len == 0 or value.len > 128) return false;
    for (value) |ch| if (!std.ascii.isAlphanumeric(ch) and ch != '-' and ch != '_') return false;
    return true;
}

fn validHash(value: []const u8) bool {
    if (value.len != 64) return false;
    for (value) |ch| if (!std.ascii.isDigit(ch) and (ch < 'a' or ch > 'f')) return false;
    return true;
}

pub fn validObjectKey(value: []const u8) bool {
    if (value.len == 0 or value.len > 1024 or value[0] == '/') return false;
    var parts = std.mem.splitScalar(u8, value, '/');
    while (parts.next()) |part| {
        if (part.len == 0 or std.mem.eql(u8, part, ".") or std.mem.eql(u8, part, "..")) return false;
        for (part) |ch| if (!std.ascii.isAlphanumeric(ch) and ch != '-' and ch != '_' and ch != '.') return false;
    }
    return true;
}

pub fn validateManifest(m: Manifest) !void {
    inline for (.{ "actor_count", "prefix_count", "cjk_key_count", "cjk_actor_count" }) |field| {
        if (@field(m, field) > std.math.maxInt(i64)) return GateError.InvalidArtifactManifest;
    }
    if (m.manifest_version == 1) {
        if (m.format_version != 0 or m.prefix_map != null or m.legacy_twin != null) return GateError.InvalidArtifactManifest;
        return;
    }
    if (m.manifest_version != 2) return GateError.UnsupportedManifestVersion;
    const map = m.prefix_map orelse return GateError.InvalidArtifactManifest;
    const twin = m.legacy_twin orelse return GateError.InvalidArtifactManifest;
    if (m.format_version != 5 or !validBuildId(m.active_build) or !validBuildId(twin.build_id) or
        std.mem.eql(u8, m.active_build, twin.build_id) or twin.source_watermark != m.source_watermark or
        !validObjectKey(m.snapshot_url) or !validObjectKey(map.object_key) or !validObjectKey(twin.manifest.object_key) or
        m.byte_size == 0 or map.byte_size == 0 or twin.manifest.byte_size == 0 or
        !validHash(m.sha256) or !validHash(map.sha256) or !validHash(twin.manifest.sha256)) return GateError.InvalidArtifactManifest;
    if (!std.mem.endsWith(u8, m.snapshot_url, "/index.db") or
        map.object_key.len != m.snapshot_url.len + ".prefix".len or
        !std.mem.startsWith(u8, map.object_key, m.snapshot_url) or
        !std.mem.endsWith(u8, map.object_key, ".prefix")) return GateError.InvalidArtifactManifest;
    const parent = m.snapshot_url[0 .. m.snapshot_url.len - "/index.db".len];
    const last = std.mem.lastIndexOfScalar(u8, parent, '/') orelse return GateError.InvalidArtifactManifest;
    if (!std.mem.eql(u8, parent[last + 1 ..], m.active_build)) return GateError.InvalidArtifactManifest;
    if (!std.mem.endsWith(u8, twin.manifest.object_key, "/manifest.json")) return GateError.InvalidArtifactManifest;
    const twin_parent = twin.manifest.object_key[0 .. twin.manifest.object_key.len - "/manifest.json".len];
    const twin_last = std.mem.lastIndexOfScalar(u8, twin_parent, '/') orelse return GateError.InvalidArtifactManifest;
    if (!std.mem.eql(u8, twin_parent[twin_last + 1 ..], twin.build_id)) return GateError.InvalidArtifactManifest;
}

/// Open a downloaded snapshot file read-only for validation (and later
/// querying). query_only is belt-and-suspenders — the snapshot is immutable.
pub fn openSnapshotReadOnly(path: [*:0]const u8) !zqlite.Conn {
    const conn = try zqlite.open(path, zqlite.OpenFlags.ReadOnly);
    _ = conn.exec("PRAGMA query_only=ON", .{}) catch {};
    return conn;
}

/// Verify a downloaded file against the manifest's size + sha256. Streams the
/// hash (never buffers the multi-GB file). Reject = delete the .part + retry.
pub fn verifyFile(path: []const u8, expected_size: u64, expected_sha_hex: []const u8) !void {
    var hex: [64]u8 = undefined;
    const size = try manifest.sha256File(path, &hex);
    if (size != expected_size) {
        log.warn("size mismatch: got {d} want {d}", .{ size, expected_size });
        return GateError.SizeMismatch;
    }
    if (expected_sha_hex.len != 64 or !std.mem.eql(u8, &hex, expected_sha_hex)) {
        log.warn("sha256 mismatch", .{});
        return GateError.ChecksumMismatch;
    }
}

/// Validate an opened snapshot against the manifest and this binary's own
/// versions. Rejects (NEVER promote) on any failure. `prev_*` are the
/// currently-attached build's counts; pass 0 on first ship to skip floors.
pub fn validateSnapshot(
    gpa: Allocator,
    snap: zqlite.Conn,
    m: Manifest,
    prev_actor_count: u64,
    prev_prefix_count: u64,
) !void {
    try validateManifest(m);

    inline for (.{ schema.table.prefix_top_base, schema.table.actor_compact_base, schema.table.index_meta }) |t| {
        const kind = if (m.manifest_version == 2 and !std.mem.eql(u8, t, schema.table.index_meta)) "view" else "table";
        const row = try snap.row("SELECT 1 FROM sqlite_master WHERE type=? AND name=?", .{ kind, t });
        if (row) |r| r.deinit() else return GateError.MissingTable;
    }

    const nv = (try schema.getMeta(snap, gpa, schema.meta.normalize_version)) orelse return GateError.MissingMeta;
    defer gpa.free(nv);
    if (!std.mem.eql(u8, nv, normalize.VERSION)) return GateError.NormalizeVersionMismatch;

    const sv = (try schema.getMeta(snap, gpa, schema.meta.scoring_version)) orelse return GateError.MissingMeta;
    defer gpa.free(sv);
    if (!std.mem.eql(u8, sv, scoring.VERSION)) return GateError.ScoringVersionMismatch;

    // Defense in depth: the manifest's claimed versions must match what the
    // downloaded file actually carries — catches a manifest/file mismatch.
    if (!std.mem.eql(u8, m.normalize_version, nv)) return GateError.ManifestMetaMismatch;
    if (!std.mem.eql(u8, m.scoring_version, sv)) return GateError.ManifestMetaMismatch;

    {
        const id = (try schema.getMeta(snap, gpa, schema.meta.build_id)) orelse return GateError.MissingMeta;
        defer gpa.free(id);
        if (!std.mem.eql(u8, id, m.active_build)) return GateError.ManifestMetaMismatch;
        if (m.manifest_version == 2) {
            const format = (try schema.getMeta(snap, gpa, "flat_prefix_version")) orelse return GateError.MissingMeta;
            defer gpa.free(format);
            const digest = (try schema.getMeta(snap, gpa, "flat_prefix_sha256")) orelse return GateError.MissingMeta;
            defer gpa.free(digest);
            if (!std.mem.eql(u8, id, m.active_build) or !std.mem.eql(u8, format, "5") or
                !std.mem.eql(u8, digest, m.prefix_map.?.sha256)) return GateError.ManifestMetaMismatch;
        }
        inline for (.{ "source_watermark", "actor_count", "prefix_count", "top_n_short", "top_n_medium", "top_n_long", "overlay_top_n", "hydrate_cap", "cjk_key_count", "cjk_actor_count" }) |field| {
            const value = (try schema.getMeta(snap, gpa, field)) orelse return GateError.MissingMeta;
            defer gpa.free(value);
            const parsed = std.fmt.parseInt(i64, value, 10) catch return GateError.ManifestMetaMismatch;
            if (parsed != @as(i64, @intCast(@field(m, field)))) return GateError.ManifestMetaMismatch;
        }
    }

    // Sanity floors: a build with <half the prior actors/keys is suspect.
    if (prev_actor_count > 0 and m.actor_count < prev_actor_count / 2) return GateError.ActorCountFloor;
    if (prev_prefix_count > 0 and m.prefix_count < prev_prefix_count / 2) return GateError.PrefixCountFloor;

    // CJK floor: a v2-cjk snapshot that emits ~no CJK keys means the
    // normalizer/tokenizer regressed (Japanese silently unsearchable). Reject
    // rather than ship a CJK-blind index. Structural, not account-dependent.
    if (std.mem.startsWith(u8, nv, "v2-cjk") and m.cjk_key_count < CJK_KEY_FLOOR) {
        log.warn("cjk_key_count {d} below floor {d} for {s}", .{ m.cjk_key_count, CJK_KEY_FLOOR, nv });
        return GateError.CjkKeyFloor;
    }
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

const sample_json =
    \\{
    \\  "manifest_version": 1,
    \\  "active_build": "b1779638898",
    \\  "previous_build": null,
    \\  "promoted_at": 1779638902,
    \\  "snapshot_url": "builds/b1779638898/index.db",
    \\  "byte_size": 6442450944,
    \\  "sha256": "3a7be9c1",
    \\  "source_watermark": 1779638800,
    \\  "actor_count": 5894868,
    \\  "prefix_count": 4123550,
    \\  "normalize_version": "v1-ascii-latin1",
    \\  "scoring_version": "v1-quality-score",
    \\  "top_n_short": 300,
    \\  "top_n_medium": 200,
    \\  "top_n_long": 100,
    \\  "overlay_top_n": 100,
    \\  "hydrate_cap": 50,
    \\  "builder_version": "deadbeef",
    \\  "some_future_field": "ignored"
    \\}
;

test "parse manifest (ignores unknown fields)" {
    const p = try parse(testing.allocator, sample_json);
    defer p.deinit();
    const m = p.value;
    try testing.expectEqual(@as(u32, 1), m.manifest_version);
    try testing.expectEqualStrings("b1779638898", m.active_build);
    try testing.expectEqual(@as(?[]const u8, null), m.previous_build);
    try testing.expectEqual(@as(u64, 6442450944), m.byte_size);
    try testing.expectEqualStrings("v1-ascii-latin1", m.normalize_version);
    try testing.expectEqual(@as(u32, 300), m.top_n_short);
    // v1 manifest lacks CJK fields → they default to 0 (no parse error).
    try testing.expectEqual(@as(u64, 0), m.cjk_key_count);
    try testing.expectEqual(@as(u64, 0), m.cjk_actor_count);
}

test "verifyFile size + checksum gate" {
    const path = "/tmp/typeahead-promote-verify-test.bin";
    {
        const f = try std.Io.Dir.createFileAbsolute(io, path, .{ .truncate = true });
        var wbuf: [64]u8 = undefined;
        var fw = std.Io.File.Writer.init(f, io, &wbuf);
        try fw.interface.writeAll("hello\n");
        try fw.interface.flush();
        f.close(io);
    }
    defer std.Io.Dir.deleteFile(.cwd(), io, path) catch {};
    const real_sha = "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03";

    try verifyFile(path, 6, real_sha); // ok
    try testing.expectError(GateError.SizeMismatch, verifyFile(path, 7, real_sha));
    try testing.expectError(GateError.ChecksumMismatch, verifyFile(path, 6, "0000000000000000000000000000000000000000000000000000000000000000"));
}

fn goodSnapshot() !zqlite.Conn {
    const conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    try schema.initSnapshot(conn);
    try schema.setMeta(conn, schema.meta.normalize_version, normalize.VERSION);
    try schema.setMeta(conn, schema.meta.scoring_version, scoring.VERSION);
    const m = goodManifest();
    try schema.setMeta(conn, "build_id", m.active_build);
    inline for (.{ "source_watermark", "actor_count", "prefix_count", "top_n_short", "top_n_medium", "top_n_long", "overlay_top_n", "hydrate_cap", "cjk_key_count", "cjk_actor_count" }) |field| {
        var buf: [32]u8 = undefined;
        try schema.setMeta(conn, field, try std.fmt.bufPrint(&buf, "{d}", .{@field(m, field)}));
    }
    return conn;
}

fn goodManifest() Manifest {
    return .{
        .manifest_version = manifest.MANIFEST_VERSION,
        .active_build = "b1",
        .promoted_at = 0,
        .snapshot_url = "builds/b1/index.db",
        .byte_size = 1,
        .sha256 = "x",
        .source_watermark = 0,
        .actor_count = 1000,
        .prefix_count = 5000,
        .normalize_version = normalize.VERSION,
        .scoring_version = scoring.VERSION,
        .top_n_short = 300,
        .top_n_medium = 200,
        .top_n_long = 100,
        .overlay_top_n = 100,
        .hydrate_cap = 50,
        .builder_version = "dev",
        .cjk_key_count = 100_000, // above CJK_KEY_FLOOR (normalize.VERSION is v2-cjk)
        .cjk_actor_count = 20_000,
    };
}

test "validateSnapshot accepts a good snapshot" {
    var conn = try goodSnapshot();
    defer conn.close();
    try validateSnapshot(testing.allocator, conn, goodManifest(), 0, 0); // first ship
    try validateSnapshot(testing.allocator, conn, goodManifest(), 1500, 8000); // within floor
}

test "validateSnapshot CJK floor: v2 build with ~no CJK keys is rejected" {
    var conn = try goodSnapshot(); // normalize_version = v2-cjk-bigram
    defer conn.close();
    var m = goodManifest();
    try schema.setMeta(conn, "cjk_key_count", "0");
    m.cjk_key_count = 0; // CJK-blind build (regressed tokenizer)
    try testing.expectError(GateError.CjkKeyFloor, validateSnapshot(testing.allocator, conn, m, 0, 0));
    try schema.setMeta(conn, "cjk_key_count", "50000");
    m.cjk_key_count = CJK_KEY_FLOOR; // exactly at floor → accepted
    try validateSnapshot(testing.allocator, conn, m, 0, 0);
}

test "validateSnapshot rejects bad manifest_version" {
    var conn = try goodSnapshot();
    defer conn.close();
    var m = goodManifest();
    m.manifest_version = 99;
    try testing.expectError(GateError.UnsupportedManifestVersion, validateSnapshot(testing.allocator, conn, m, 0, 0));
}

test "validateSnapshot rejects normalize_version mismatch" {
    const conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    try schema.initSnapshot(conn);
    try schema.setMeta(conn, schema.meta.normalize_version, "v0-WRONG");
    try schema.setMeta(conn, schema.meta.scoring_version, scoring.VERSION);
    try testing.expectError(GateError.NormalizeVersionMismatch, validateSnapshot(testing.allocator, conn, goodManifest(), 0, 0));
}

test "validateSnapshot rejects missing table" {
    const conn = try zqlite.open(":memory:", zqlite.OpenFlags.Create | zqlite.OpenFlags.ReadWrite);
    defer conn.close();
    // only index_meta, no prefix_top_base / actor_compact_base
    try conn.exec("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)", .{});
    try testing.expectError(GateError.MissingTable, validateSnapshot(testing.allocator, conn, goodManifest(), 0, 0));
}

test "validateSnapshot enforces actor_count floor" {
    var conn = try goodSnapshot();
    defer conn.close();
    var m = goodManifest();
    try schema.setMeta(conn, "actor_count", "100");
    m.actor_count = 100; // prev was 1000 → below half
    try testing.expectError(GateError.ActorCountFloor, validateSnapshot(testing.allocator, conn, m, 1000, 0));
}

fn compactManifest() Manifest {
    var m = goodManifest();
    m.manifest_version = 2;
    m.format_version = 5;
    m.sha256 = "0000000000000000000000000000000000000000000000000000000000000000";
    m.prefix_map = .{ .object_key = "builds/b1/index.db.prefix", .byte_size = 4096, .sha256 = "1111111111111111111111111111111111111111111111111111111111111111" };
    m.legacy_twin = .{ .manifest = .{ .object_key = "builds/b0/manifest.json", .byte_size = 500, .sha256 = "2222222222222222222222222222222222222222222222222222222222222222" }, .build_id = "b0", .source_watermark = m.source_watermark };
    return m;
}

test "compact manifest binds pair identity and rollback watermark" {
    const m = compactManifest();
    try validateManifest(m);
    var bad = m;
    bad.prefix_map = null;
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(bad));
    bad = m;
    bad.prefix_map.?.object_key = "builds/b2/index.db.prefix";
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(bad));
    bad = m;
    bad.legacy_twin.?.source_watermark = 1;
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(bad));
    bad = m;
    bad.snapshot_url = "builds/../b1/index.db";
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(bad));
    bad = m;
    bad.active_build = "../b1";
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(bad));
    bad = m;
    bad.manifest_version = 1;
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(bad));
}

test "compact metadata must agree with every manifest serving field" {
    const conn = try goodSnapshot();
    defer conn.close();
    try conn.exec("DROP TABLE prefix_top_base", .{});
    try conn.exec("CREATE VIEW prefix_top_base AS SELECT NULL AS key,NULL AS dids WHERE 0", .{});
    try conn.exec("DROP TABLE actor_compact_base", .{});
    try conn.exec("CREATE VIEW actor_compact_base AS SELECT NULL AS did,NULL AS blob WHERE 0", .{});
    const m = compactManifest();
    try schema.setMeta(conn, "build_id", m.active_build);
    try schema.setMeta(conn, "flat_prefix_version", "5");
    try schema.setMeta(conn, "flat_prefix_sha256", m.prefix_map.?.sha256);
    inline for (.{ "source_watermark", "actor_count", "prefix_count", "top_n_short", "top_n_medium", "top_n_long", "overlay_top_n", "hydrate_cap", "cjk_key_count", "cjk_actor_count" }) |field| {
        var buf: [32]u8 = undefined;
        try schema.setMeta(conn, field, try std.fmt.bufPrint(&buf, "{d}", .{@field(m, field)}));
    }
    try validateSnapshot(testing.allocator, conn, m, 0, 0);
    try schema.setMeta(conn, "source_watermark", "1");
    try testing.expectError(GateError.ManifestMetaMismatch, validateSnapshot(testing.allocator, conn, m, 0, 0));
}

test "legacy rollback metadata cannot claim a different watermark or identity" {
    const conn = try goodSnapshot();
    defer conn.close();
    const m = goodManifest();
    try schema.setMeta(conn, "source_watermark", "1");
    try testing.expectError(GateError.ManifestMetaMismatch, validateSnapshot(testing.allocator, conn, m, 0, 0));
    try schema.setMeta(conn, "source_watermark", "0");
    try schema.setMeta(conn, "build_id", "other");
    try testing.expectError(GateError.ManifestMetaMismatch, validateSnapshot(testing.allocator, conn, m, 0, 0));
}

test "manifest rejects numeric overflow and incorrectly identified twin" {
    var m = compactManifest();
    m.actor_count = std.math.maxInt(u64);
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(m));
    m = compactManifest();
    m.legacy_twin.?.manifest.object_key = "builds/unrelated/manifest.json";
    try testing.expectError(GateError.InvalidArtifactManifest, validateManifest(m));
}
