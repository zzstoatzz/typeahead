//! Build manifest. Written last in a build, after index.db is final and
//! checksummed. This is the one durable artifact whose shape is part of the
//! indexer↔serving contract — App B reads it (from R2 via `latest.json`),
//! applies the promotion gate, and only then downloads/promotes the .db.
//! `manifest_version` guards breaking changes.
//!
//! Object layout in the `typeahead-index` R2 bucket (no redundant bucket
//! prefix in keys):
//!   builds/<build_id>/index.db
//!   builds/<build_id>/manifest.json
//!   latest.json            (stable pointer App B polls; written last)
const std = @import("std");
const Allocator = std.mem.Allocator;

const io = std.Options.debug_io;
const normalize = @import("normalize.zig");
const scoring = @import("scoring.zig");

pub const MANIFEST_VERSION: u32 = 1;

pub const Inputs = struct {
    build_id: []const u8,
    db_path: []const u8, // absolute path to the final index.db
    build_dir: []const u8, // dir to write manifest.json into
    promoted_at: i64, // unix seconds
    source_watermark: i64,
    actor_count: usize,
    prefix_count: usize,
    top_n_short: usize,
    top_n_medium: usize,
    top_n_long: usize,
    overlay_top_n: usize,
    hydrate_cap: usize,
    builder_version: []const u8,
};

pub const Result = struct {
    byte_size: u64,
    sha256_hex: [64]u8,
};

/// SHA-256 of a file, streamed in 256 KB chunks (never buffers the whole
/// file — index.db is multi-GB). Returns the byte count too, so the caller
/// gets byte_size for free in the same pass.
pub fn sha256File(path: []const u8, out_hex: *[64]u8) !u64 {
    const f = try std.Io.Dir.openFileAbsolute(io, path, .{});
    defer f.close(io);
    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
    var buf: [256 * 1024]u8 = undefined;
    var total: u64 = 0;
    while (true) {
        const n = f.readStreaming(io, &.{&buf}) catch |err| switch (err) {
            error.EndOfStream => break,
            else => return err,
        };
        hasher.update(buf[0..n]);
        total += n;
    }
    var digest: [32]u8 = undefined;
    hasher.final(&digest);
    const hex = "0123456789abcdef";
    for (digest, 0..) |b, i| {
        out_hex[i * 2] = hex[b >> 4];
        out_hex[i * 2 + 1] = hex[b & 0xF];
    }
    return total;
}

/// Write `<build_dir>/manifest.json` atomically (.tmp → rename). Streams the
/// sha256 of index.db as part of the write. normalize/scoring versions are
/// stamped from the binary's own constants so App B can reject a snapshot
/// built with incompatible normalization. previous_build is null here; the
/// active/previous pointer pair lives in R2 `latest.json` (set at upload).
pub fn write(gpa: Allocator, in: Inputs) !Result {
    var sha_hex: [64]u8 = undefined;
    const byte_size = try sha256File(in.db_path, &sha_hex);

    const json = try std.fmt.allocPrint(gpa,
        \\{{
        \\  "manifest_version": {d},
        \\  "active_build": "{s}",
        \\  "previous_build": null,
        \\  "promoted_at": {d},
        \\  "snapshot_url": "builds/{s}/index.db",
        \\  "byte_size": {d},
        \\  "sha256": "{s}",
        \\  "source_watermark": {d},
        \\  "actor_count": {d},
        \\  "prefix_count": {d},
        \\  "normalize_version": "{s}",
        \\  "scoring_version": "{s}",
        \\  "top_n_short": {d},
        \\  "top_n_medium": {d},
        \\  "top_n_long": {d},
        \\  "overlay_top_n": {d},
        \\  "hydrate_cap": {d},
        \\  "builder_version": "{s}"
        \\}}
        \\
    , .{
        MANIFEST_VERSION,   in.build_id,       in.promoted_at,      in.build_id,
        byte_size,          sha_hex[0..],      in.source_watermark, in.actor_count,
        in.prefix_count,    normalize.VERSION, scoring.VERSION,     in.top_n_short,
        in.top_n_medium,    in.top_n_long,     in.overlay_top_n,    in.hydrate_cap,
        in.builder_version,
    });
    defer gpa.free(json);

    var tmp_buf: [320]u8 = undefined;
    const tmp_path = try std.fmt.bufPrint(&tmp_buf, "{s}/manifest.json.tmp", .{in.build_dir});
    var final_buf: [320]u8 = undefined;
    const final_path = try std.fmt.bufPrint(&final_buf, "{s}/manifest.json", .{in.build_dir});

    const file = try std.Io.Dir.createFileAbsolute(io, tmp_path, .{ .truncate = true });
    {
        var wbuf: [4096]u8 = undefined;
        var fw = std.Io.File.Writer.init(file, io, &wbuf);
        try fw.interface.writeAll(json);
        try fw.interface.flush();
    }
    file.close(io);
    try std.Io.Dir.renameAbsolute(tmp_path, final_path, io);

    var out: Result = .{ .byte_size = byte_size, .sha256_hex = undefined };
    @memcpy(&out.sha256_hex, &sha_hex);
    return out;
}

const testing = std.testing;

test "sha256File matches known digest" {
    // sha256("hello\n") = 5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03
    var tmp = testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(.{ .sub_path = "x", .data = "hello\n" });
    var path_buf: [std.fs.max_path_bytes]u8 = undefined;
    const abs = try tmp.dir.realpath("x", &path_buf);
    var hex: [64]u8 = undefined;
    const n = try sha256File(abs, &hex);
    try testing.expectEqual(@as(u64, 6), n);
    try testing.expectEqualStrings("5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03", &hex);
}
