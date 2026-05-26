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
pub const Manifest = struct {
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
};

pub const GateError = error{
    UnsupportedManifestVersion,
    SizeMismatch,
    ChecksumMismatch,
    MissingTable,
    MissingMeta,
    NormalizeVersionMismatch,
    ScoringVersionMismatch,
    ManifestMetaMismatch,
    ActorCountFloor,
    PrefixCountFloor,
};

/// Parse manifest JSON. Caller owns the returned Parsed and must deinit it
/// (the Manifest's string slices point into its arena).
pub fn parse(gpa: Allocator, json: []const u8) !std.json.Parsed(Manifest) {
    return std.json.parseFromSlice(Manifest, gpa, json, .{ .ignore_unknown_fields = true });
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
    if (m.manifest_version != manifest.MANIFEST_VERSION) return GateError.UnsupportedManifestVersion;

    inline for (.{ schema.table.prefix_top_base, schema.table.actor_compact_base, schema.table.index_meta }) |t| {
        const row = try snap.row("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", .{t});
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

    // Sanity floors: a build with <half the prior actors/keys is suspect.
    if (prev_actor_count > 0 and m.actor_count < prev_actor_count / 2) return GateError.ActorCountFloor;
    if (prev_prefix_count > 0 and m.prefix_count < prev_prefix_count / 2) return GateError.PrefixCountFloor;
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
    };
}

test "validateSnapshot accepts a good snapshot" {
    var conn = try goodSnapshot();
    defer conn.close();
    try validateSnapshot(testing.allocator, conn, goodManifest(), 0, 0); // first ship
    try validateSnapshot(testing.allocator, conn, goodManifest(), 1500, 8000); // within floor
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
    m.actor_count = 100; // prev was 1000 → below half
    try testing.expectError(GateError.ActorCountFloor, validateSnapshot(testing.allocator, conn, m, 1000, 0));
}
