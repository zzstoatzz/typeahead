//! Binary blob encoding/decoding for the prefix index storage layer.
//!
//! Two formats:
//!
//!   actor_compact blob
//!     Per-actor presentation payload. Stored in `actor_compact_base.blob`
//!     and `actor_overlay.blob`. Contains just what /search needs to emit.
//!     Layout (little-endian, length-prefixed strings — all u16 lengths,
//!     no u8 length fields anywhere; six extra bytes per actor at 5.9M
//!     scale is ~35MB and removes any single-byte edge cases):
//!         u8   version            (currently 1)
//!         u8   hidden             (0 or 1)
//!         f32  quality_score
//!         u16  handle_len      →  handle bytes
//!         u16  display_len     →  display bytes
//!         u16  avatar_len      →  avatar bytes
//!         u16  labels_len      →  labels JSON bytes
//!         u16  pds_len         →  pds bytes
//!         u16  created_at_len  →  created_at bytes
//!         u16  associated_len  →  associated JSON bytes
//!
//!   did_list blob
//!     Top-N candidate DIDs for one prefix key. Stored in
//!     `prefix_top_base.dids`. Layout:
//!         u8   version            (currently 1)
//!         u16  count
//!         repeated `count` times:
//!             f32  score
//!             u16  did_len       →  did bytes  (typically 32 ASCII chars
//!                                                for `did:plc:...`; up to
//!                                                ~150 for did:web — u16
//!                                                covers the long tail).
//!
//! `DidListIter.init` walks the whole blob once to validate every length
//! prefix points inside the buffer. After that, iteration is guaranteed
//! safe. Truncation/corruption is an error at init, not a silently-short
//! result during iteration.
//!
//! Storing DIDs as text rather than binary trades ~half disk for
//! roundtrip simplicity (no decode step, no special handling for did:web
//! variable-length addresses). Revisit if `prefix_top_base` ever crosses
//! 20 GB.

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const ACTOR_BLOB_VERSION: u8 = 1;
pub const DID_LIST_VERSION: u8 = 1;

pub const PackError = error{
    FieldTooLong, // any string field > 65535 bytes
    TooManyEntries, // did_list count > 65535
    OutOfMemory,
};

pub const UnpackError = error{
    UnknownVersion,
    TruncatedBlob,
    InvalidLength,
};

pub const ActorCompact = struct {
    hidden: bool,
    quality_score: f32,
    handle: []const u8,
    display: []const u8,
    avatar: []const u8,
    labels: []const u8, // JSON string ('[]' default)
    pds: []const u8,
    created_at: []const u8,
    associated: []const u8, // JSON string ('{}' default)
};

pub fn packActor(arena: Allocator, a: ActorCompact) PackError![]u8 {
    const fields = [_][]const u8{ a.handle, a.display, a.avatar, a.labels, a.pds, a.created_at, a.associated };
    for (fields) |f| if (f.len > 65535) return PackError.FieldTooLong;

    const total = 1 + 1 + 4 // version + hidden + score
    + 7 * 2 // seven u16 length prefixes
    + a.handle.len + a.display.len + a.avatar.len + a.labels.len + a.pds.len + a.created_at.len + a.associated.len;

    var buf = try arena.alloc(u8, total);
    var w: usize = 0;

    buf[w] = ACTOR_BLOB_VERSION;
    w += 1;
    buf[w] = @intFromBool(a.hidden);
    w += 1;
    std.mem.writeInt(u32, buf[w..][0..4], @bitCast(a.quality_score), .little);
    w += 4;

    w += writeLenU16(buf[w..], a.handle);
    w += writeLenU16(buf[w..], a.display);
    w += writeLenU16(buf[w..], a.avatar);
    w += writeLenU16(buf[w..], a.labels);
    w += writeLenU16(buf[w..], a.pds);
    w += writeLenU16(buf[w..], a.created_at);
    w += writeLenU16(buf[w..], a.associated);

    std.debug.assert(w == total);
    return buf;
}

pub fn unpackActor(buf: []const u8) UnpackError!ActorCompact {
    if (buf.len < 6) return UnpackError.TruncatedBlob;
    if (buf[0] != ACTOR_BLOB_VERSION) return UnpackError.UnknownVersion;

    var r: usize = 1;
    const hidden = buf[r] != 0;
    r += 1;
    const score: f32 = @bitCast(std.mem.readInt(u32, buf[r..][0..4], .little));
    r += 4;

    const handle = try readLenU16(buf, &r);
    const display = try readLenU16(buf, &r);
    const avatar = try readLenU16(buf, &r);
    const labels = try readLenU16(buf, &r);
    const pds = try readLenU16(buf, &r);
    const created_at = try readLenU16(buf, &r);
    const associated = try readLenU16(buf, &r);

    if (r != buf.len) return UnpackError.TruncatedBlob;

    return .{
        .hidden = hidden,
        .quality_score = score,
        .handle = handle,
        .display = display,
        .avatar = avatar,
        .labels = labels,
        .pds = pds,
        .created_at = created_at,
        .associated = associated,
    };
}

pub const DidEntry = struct {
    did: []const u8,
    score: f32,
};

pub fn packDidList(arena: Allocator, entries: []const DidEntry) PackError![]u8 {
    if (entries.len > 65535) return PackError.TooManyEntries;

    var total: usize = 1 + 2; // version + count
    for (entries) |e| {
        if (e.did.len > 65535) return PackError.FieldTooLong;
        total += 4 + 2 + e.did.len;
    }

    var buf = try arena.alloc(u8, total);
    var w: usize = 0;
    buf[w] = DID_LIST_VERSION;
    w += 1;
    std.mem.writeInt(u16, buf[w..][0..2], @intCast(entries.len), .little);
    w += 2;

    for (entries) |e| {
        std.mem.writeInt(u32, buf[w..][0..4], @bitCast(e.score), .little);
        w += 4;
        std.mem.writeInt(u16, buf[w..][0..2], @intCast(e.did.len), .little);
        w += 2;
        @memcpy(buf[w .. w + e.did.len], e.did);
        w += e.did.len;
    }

    std.debug.assert(w == total);
    return buf;
}

/// Iterator over a packed did_list blob. Borrows the buffer — does not
/// allocate. Caller owns the buffer for the iterator's lifetime.
///
/// `init` validates every length prefix against the buffer up front;
/// after a successful init, `next()` is guaranteed not to fall off the
/// end of the buffer. Truncation/corruption is rejected at init time,
/// not by silently returning a short iteration.
pub const DidListIter = struct {
    buf: []const u8,
    remaining: u16,
    cursor: usize,

    pub fn init(buf: []const u8) UnpackError!DidListIter {
        if (buf.len < 3) return UnpackError.TruncatedBlob;
        if (buf[0] != DID_LIST_VERSION) return UnpackError.UnknownVersion;
        const n = std.mem.readInt(u16, buf[1..3], .little);

        // Walk-validate: confirm every (score + did_len + did) is in-buffer.
        // Cost is O(N) but only done once, and it prevents corruption from
        // looking like a short list at serve time.
        var c: usize = 3;
        var i: usize = 0;
        while (i < n) : (i += 1) {
            if (c + 6 > buf.len) return UnpackError.TruncatedBlob;
            const did_len = std.mem.readInt(u16, buf[c + 4 ..][0..2], .little);
            c += 6 + did_len;
            if (c > buf.len) return UnpackError.TruncatedBlob;
        }
        if (c != buf.len) return UnpackError.TruncatedBlob;

        return .{ .buf = buf, .remaining = n, .cursor = 3 };
    }

    pub fn next(self: *DidListIter) ?DidEntry {
        if (self.remaining == 0) return null;
        // init() validated the whole walk; unreachable on bounds here.
        const score: f32 = @bitCast(std.mem.readInt(u32, self.buf[self.cursor..][0..4], .little));
        self.cursor += 4;
        const did_len = std.mem.readInt(u16, self.buf[self.cursor..][0..2], .little);
        self.cursor += 2;
        const did = self.buf[self.cursor .. self.cursor + did_len];
        self.cursor += did_len;
        self.remaining -= 1;
        return .{ .did = did, .score = score };
    }
};

pub fn count(buf: []const u8) UnpackError!u16 {
    if (buf.len < 3) return UnpackError.TruncatedBlob;
    if (buf[0] != DID_LIST_VERSION) return UnpackError.UnknownVersion;
    return std.mem.readInt(u16, buf[1..3], .little);
}

// ─── length-prefixed string helpers ──────────────────────────────────────

fn writeLenU16(dst: []u8, s: []const u8) usize {
    std.mem.writeInt(u16, dst[0..2], @intCast(s.len), .little);
    @memcpy(dst[2 .. 2 + s.len], s);
    return 2 + s.len;
}

fn readLenU16(buf: []const u8, r: *usize) UnpackError![]const u8 {
    if (r.* + 2 > buf.len) return UnpackError.TruncatedBlob;
    const n = std.mem.readInt(u16, buf[r.*..][0..2], .little);
    r.* += 2;
    if (r.* + n > buf.len) return UnpackError.TruncatedBlob;
    const out = buf[r.* .. r.* + n];
    r.* += n;
    return out;
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

test "pack/unpack actor roundtrip" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();

    const original: ActorCompact = .{
        .hidden = false,
        .quality_score = 137.5,
        .handle = "nate.fm",
        .display = "Nate The Great",
        .avatar = "bafkreiabc123xyz",
        .labels = "[]",
        .pds = "morel.us-east.host.bsky.network",
        .created_at = "2023-04-01T12:00:00Z",
        .associated = "{}",
    };

    const blob = try packActor(arena.allocator(), original);
    const decoded = try unpackActor(blob);

    try testing.expectEqual(original.hidden, decoded.hidden);
    try testing.expectEqual(original.quality_score, decoded.quality_score);
    try testing.expectEqualStrings(original.handle, decoded.handle);
    try testing.expectEqualStrings(original.display, decoded.display);
    try testing.expectEqualStrings(original.avatar, decoded.avatar);
    try testing.expectEqualStrings(original.labels, decoded.labels);
    try testing.expectEqualStrings(original.pds, decoded.pds);
    try testing.expectEqualStrings(original.created_at, decoded.created_at);
    try testing.expectEqualStrings(original.associated, decoded.associated);
}

test "pack actor with hidden=true and empty fields" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();

    const a: ActorCompact = .{
        .hidden = true,
        .quality_score = 0,
        .handle = "x.bsky.social",
        .display = "",
        .avatar = "",
        .labels = "[]",
        .pds = "",
        .created_at = "",
        .associated = "{}",
    };

    const blob = try packActor(arena.allocator(), a);
    const d = try unpackActor(blob);
    try testing.expect(d.hidden);
    try testing.expectEqualStrings("", d.display);
    try testing.expectEqualStrings("", d.avatar);
    try testing.expectEqualStrings("[]", d.labels);
}

test "unpackActor rejects unknown version" {
    const bad = [_]u8{ 99, 0, 0, 0, 0, 0 };
    try testing.expectError(UnpackError.UnknownVersion, unpackActor(&bad));
}

test "unpackActor rejects truncated blob" {
    const truncated = [_]u8{ ACTOR_BLOB_VERSION, 0, 0 };
    try testing.expectError(UnpackError.TruncatedBlob, unpackActor(&truncated));
}

test "packActor accepts display name at 256 bytes (atproto upper bound)" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var display: [256]u8 = undefined;
    @memset(&display, 'a');
    const a: ActorCompact = .{
        .hidden = false,
        .quality_score = 0,
        .handle = "alice.bsky.social",
        .display = &display,
        .avatar = "",
        .labels = "[]",
        .pds = "",
        .created_at = "",
        .associated = "{}",
    };
    const blob = try packActor(arena.allocator(), a);
    const d = try unpackActor(blob);
    try testing.expectEqual(@as(usize, 256), d.display.len);
}

test "packActor rejects field over 65535 bytes" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const long = try arena.allocator().alloc(u8, 70_000);
    @memset(long, 'a');
    const a: ActorCompact = .{
        .hidden = false,
        .quality_score = 0,
        .handle = "alice.bsky.social",
        .display = "",
        .avatar = long, // avatar is the variable URL — could plausibly go long
        .labels = "[]",
        .pds = "",
        .created_at = "",
        .associated = "{}",
    };
    try testing.expectError(PackError.FieldTooLong, packActor(arena.allocator(), a));
}

test "pack/unpack did_list roundtrip" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();

    const entries = [_]DidEntry{
        .{ .did = "did:plc:abcdefghijklmnopqrstuvwx", .score = 900.5 },
        .{ .did = "did:plc:zyxwvutsrqponmlkjihgfed", .score = 42.0 },
        .{ .did = "did:web:example.com", .score = 0 },
    };

    const blob = try packDidList(arena.allocator(), &entries);
    try testing.expectEqual(@as(u16, 3), try count(blob));

    var iter = try DidListIter.init(blob);
    var i: usize = 0;
    while (iter.next()) |e| : (i += 1) {
        try testing.expectEqualStrings(entries[i].did, e.did);
        try testing.expectEqual(entries[i].score, e.score);
    }
    try testing.expectEqual(@as(usize, 3), i);
}

test "did_list empty roundtrip" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const blob = try packDidList(arena.allocator(), &.{});
    try testing.expectEqual(@as(u16, 0), try count(blob));
    var iter = try DidListIter.init(blob);
    try testing.expect(iter.next() == null);
}

test "DidListIter rejects unknown version" {
    const bad = [_]u8{ 99, 0, 0 };
    try testing.expectError(UnpackError.UnknownVersion, DidListIter.init(&bad));
}

test "DidListIter rejects truncated blob (header)" {
    const t = [_]u8{ DID_LIST_VERSION, 0 };
    try testing.expectError(UnpackError.TruncatedBlob, DidListIter.init(&t));
}

test "DidListIter rejects corruption mid-blob (count says 2 but body has 1)" {
    // version=1, count=2, one entry score=0 len=3 "abc" then EOF.
    const corrupt = [_]u8{
        DID_LIST_VERSION,
        2, 0, // count=2
        0, 0, 0, 0, // score=0
        3,   0, // did_len=3
        'a', 'b',
        'c',
    };
    try testing.expectError(UnpackError.TruncatedBlob, DidListIter.init(&corrupt));
}

test "DidListIter rejects trailing garbage" {
    // valid blob followed by extra bytes — fails validation.
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const entries = [_]DidEntry{.{ .did = "did:plc:x", .score = 1.0 }};
    const blob = try packDidList(arena.allocator(), &entries);
    const padded = try arena.allocator().alloc(u8, blob.len + 4);
    @memcpy(padded[0..blob.len], blob);
    @memset(padded[blob.len..], 0);
    try testing.expectError(UnpackError.TruncatedBlob, DidListIter.init(padded));
}

test "actor blob fields are stable across versions" {
    // If pack format ever changes, ACTOR_BLOB_VERSION MUST bump in lockstep.
    // A snapshot built with v=1 must not be unpackable as v=2.
    try testing.expectEqual(@as(u8, 1), ACTOR_BLOB_VERSION);
    try testing.expectEqual(@as(u8, 1), DID_LIST_VERSION);
}
