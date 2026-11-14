//! What the store role reads out of a relay event: which collections make
//! an account known, what a profile record says about it, and whether a
//! stored relay cursor can be resumed.
//!
//! The relay is `com.atproto.sync.subscribeRepos`, not jetstream. jetstream
//! is a lossy projection of it: pds.zat.dev repos were absent from a 7-day
//! jetstream replay (0 events, 6/6 repos) while the relay was rev-current
//! with that PDS and re-emitted its commits live (verified 2026-07-28). The
//! loss is silent and partial, so it cannot be spot-checked.

const std = @import("std");
const io = std.Options.debug_io;
const mem = std.mem;
const Allocator = mem.Allocator;
const zat = @import("zat");

pub fn timestamp() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
}

/// The relay's firehose has no server-side collection filter (jetstream's
/// wantedCollections has no equivalent), so the allowlist moves client-side and
/// every frame is decoded before it can be discarded.
/// Which firehose collections cause us to discover an actor.
///
/// TODO: this list is hardcoded, so onboarding an app is a code change and a
/// deploy — and until that happens its users are discoverable ONLY via
/// `#identity`. That is not a theoretical gap: birds.place users write nothing
/// but `place.birds.*`, so before this entry existed the commit path could not
/// see them at all, and an actor whose `#identity` was missed had no second
/// chance. Worth moving to config (or dropping the allowlist for a deny-list of
/// known-spam collections) rather than growing this by hand.
/// Design sketch — published lexicons as the signal: docs/collection-discovery.md
pub fn wantedCollection(collection: []const u8) bool {
    const exact = [_][]const u8{
        "app.bsky.actor.profile",
        "app.bsky.feed.post",
        "app.bsky.feed.like",
        "app.bsky.graph.follow",
        "sh.tangled.actor.profile",
    };
    for (exact) |e| if (mem.eql(u8, collection, e)) return true;

    const prefixes = [_][]const u8{
        "site.standard.",
        "place.birds.",
    };
    for (prefixes) |pre| if (mem.startsWith(u8, collection, pre)) return true;
    return false;
}

/// Render a profile record's avatar blob ref as the base32 CID string the rest
/// of the system stores. DAG-CBOR carries the ref as a real CID (tag 42), where
/// jetstream handed us a pre-rendered `ref.$link` string — so this transform is
/// new with the firehose cutover and is the one that can silently produce wrong
/// avatars rather than failing loudly.
pub fn avatarCidOf(allocator: Allocator, record: zat.cbor.Value) ?[]const u8 {
    return blobCidOf(allocator, record, "avatar");
}

/// Same rendering for any blob-valued profile field (`avatar`, `banner`).
pub fn blobCidOf(allocator: Allocator, record: zat.cbor.Value, key: []const u8) ?[]const u8 {
    const blob = record.get(key) orelse return null;
    const cid = blob.getCid("ref") orelse return null;
    return zat.multibase.encode(allocator, .base32lower, cid.raw) catch null;
}

pub fn isProfileCollection(collection: []const u8) bool {
    return mem.eql(u8, collection, "app.bsky.actor.profile") or mem.eql(u8, collection, "sh.tangled.actor.profile");
}

pub const ProfileFields = struct {
    display_name: ?[]const u8 = null,
    avatar_cid: ?[]const u8 = null,
    description: ?[]const u8 = null,
    banner_cid: ?[]const u8 = null,
};

/// What a profile record (bsky or tangled) says about its actor, copied into
/// `arena`. Tangled profiles have no displayName.
pub fn profileFields(arena: Allocator, record: zat.cbor.Value) ProfileFields {
    return .{
        .display_name = if (record.getString("displayName")) |name| arena.dupe(u8, name) catch null else null,
        .avatar_cid = avatarCidOf(arena, record),
        .description = if (record.getString("description")) |d| arena.dupe(u8, d) catch null else null,
        .banner_cid = blobCidOf(arena, record, "banner"),
    };
}

/// A relay `seq` is a small monotonic counter (~8.5e8 as of 2026-07). Jetstream's
/// cursor was a µs timestamp (~1.78e15). During the jetstream→relay cutover a
/// jetstream-era ingester can still be writing µs values into the shared cursor
/// key, so a stale one must be rejected rather than replayed: subscribing at a
/// seq six orders of magnitude in the future yields an empty stream, and the
/// watchdog would read that as a dead upstream instead of a bad cursor.
/// Rejecting prevents subscription; the caller must recover a valid cursor
/// rather than start live and silently skip the unavailable replay interval.
pub const MAX_PLAUSIBLE_RELAY_SEQ: i64 = 1_000_000_000_000; // 1e12

pub fn plausibleRelayCursor(cursor: ?i64) ?i64 {
    const c = cursor orelse return null;
    if (c <= 0 or c >= MAX_PLAUSIBLE_RELAY_SEQ) return null;
    return c;
}

// -- utilities --

pub fn getRssKB() u64 {
    const f = std.Io.Dir.openFileAbsolute(io, "/proc/self/statm", .{}) catch return 0;
    defer f.close(io);
    var buf: [128]u8 = undefined;
    const n = f.readStreaming(io, &.{&buf}) catch return 0;
    var it = std.mem.splitScalar(u8, buf[0..n], ' ');
    _ = it.next(); // size
    const rss_pages = std.fmt.parseInt(u64, it.next() orelse return 0, 10) catch return 0;
    return rss_pages * 4;
}

const testing = std.testing;

// identity.zig, plc_tail.zig, label_tail.zig, log_route.zig and fatal.zig
// have tests of their own, and `zig build test` roots this suite here:
// without these references they are never run.
test {
    _ = @import("identity.zig");
    _ = @import("plc_tail.zig");
    _ = @import("label_tail.zig");
    _ = @import("log_route.zig");
    _ = @import("fatal.zig");
}

test "wantedCollection: allowlist survives the loss of jetstream's server-side filter" {
    try testing.expect(wantedCollection("app.bsky.actor.profile"));
    try testing.expect(wantedCollection("app.bsky.feed.post"));
    try testing.expect(wantedCollection("app.bsky.feed.like"));
    try testing.expect(wantedCollection("app.bsky.graph.follow"));
    try testing.expect(wantedCollection("sh.tangled.actor.profile"));
    // jetstream expressed this as the wildcard "site.standard.*"
    try testing.expect(wantedCollection("site.standard.profile"));
    try testing.expect(wantedCollection("site.standard.anything.else"));

    try testing.expect(!wantedCollection("site.standard"));
    try testing.expect(!wantedCollection("app.bsky.feed.repost"));
    try testing.expect(!wantedCollection("com.atproto.lexicon.schema"));
    try testing.expect(!wantedCollection(""));
}

test "wantedCollection: birds.place records discover their actor" {
    // Regression: a birds.place user writes only `place.birds.*`, so with this
    // missing the commit path never saw them and discovery rested entirely on a
    // single `#identity` event — which, when slingshot failed, was dropped.
    try std.testing.expect(wantedCollection("place.birds.sighting"));
    try std.testing.expect(wantedCollection("place.birds.profile"));
    try std.testing.expect(!wantedCollection("place.birdsomething.else"));
    try std.testing.expect(!wantedCollection("com.example.spam"));
}

test "avatarCidOf renders a DAG-CBOR blob ref to the stored base32 CID" {
    const allocator = testing.allocator;
    // zzstoatzz.io's real avatar CID as of 2026-07-28. cbor.Cid.raw holds the
    // CID bytes with the 0x00 identity-multibase prefix already stripped, so a
    // decode → wrap → encode round-trip must reproduce the string exactly. A
    // regression here yields a plausible-looking but wrong avatar URL.
    const want = "bafkreiegehiwbazkkpp5nucenajtdjwinxrrnz7ukawpqnudbr47f44zhq";
    const raw = try zat.multibase.decode(allocator, want);
    defer allocator.free(raw);

    const record = zat.cbor.Value{ .map = &.{
        .{ .key = "displayName", .value = .{ .text = "nate" } },
        .{ .key = "avatar", .value = .{ .map = &.{
            .{ .key = "$type", .value = .{ .text = "blob" } },
            .{ .key = "ref", .value = .{ .cid = .{ .raw = raw } } },
            .{ .key = "mimeType", .value = .{ .text = "image/jpeg" } },
        } } },
    } };

    const got = avatarCidOf(allocator, record) orelse return error.NoAvatar;
    defer allocator.free(@constCast(got));
    try testing.expectEqualStrings(want, got);

    try testing.expectEqualStrings("nate", record.getString("displayName").?);
}

test "blobCidOf reads the banner ref with the same rendering as the avatar" {
    const allocator = testing.allocator;
    const want = "bafkreiegehiwbazkkpp5nucenajtdjwinxrrnz7ukawpqnudbr47f44zhq";
    const raw = try zat.multibase.decode(allocator, want);
    defer allocator.free(raw);

    const record = zat.cbor.Value{ .map = &.{
        .{ .key = "description", .value = .{ .text = "hi" } },
        .{ .key = "banner", .value = .{ .map = &.{
            .{ .key = "$type", .value = .{ .text = "blob" } },
            .{ .key = "ref", .value = .{ .cid = .{ .raw = raw } } },
        } } },
    } };

    const got = blobCidOf(allocator, record, "banner") orelse return error.NoBanner;
    defer allocator.free(@constCast(got));
    try testing.expectEqualStrings(want, got);
    try testing.expect(avatarCidOf(allocator, record) == null);
    try testing.expectEqualStrings("hi", record.getString("description").?);
}

test "avatarCidOf returns null when the profile has no avatar" {
    const record = zat.cbor.Value{ .map = &.{
        .{ .key = "displayName", .value = .{ .text = "nate" } },
    } };
    try testing.expect(avatarCidOf(testing.allocator, record) == null);
}

test "plausibleRelayCursor rejects a jetstream-era µs cursor" {
    // the exact value /admin/cursor served mid-cutover on 2026-07-28, written by
    // a still-running jetstream ingester after the worker switched keys
    try testing.expect(plausibleRelayCursor(1785217869995374) == null);
    try testing.expect(plausibleRelayCursor(null) == null);
    try testing.expect(plausibleRelayCursor(0) == null);
    try testing.expect(plausibleRelayCursor(-1) == null);

    // real relay seqs (~8.5e8 in 2026-07) pass through untouched
    try testing.expectEqual(@as(?i64, 853_602_603), plausibleRelayCursor(853_602_603));
    try testing.expectEqual(@as(?i64, 1), plausibleRelayCursor(1));
}

test "relay seq spaces are per-relay: a cursor is only valid for its issuer" {
    // Measured 2026-07-28, first seq from each host in zat's default_hosts:
    //   bsky.network                    32_234_719_514
    //   northamerica.firehose.network    7_620_802_119
    //   europe.firehose.network          7_032_143_016
    //   asia.firehose.network            5_581_515_167
    // These are independent relays, not mirrors. The ingester failed over to
    // asia and wrote 5_581_374_644 into the cursor key while pinned-in-config
    // to bsky — resuming that against bsky would skip ~26.6 BILLION events.
    // All four are individually "plausible", which is exactly why the range
    // guard cannot catch this and why the client must pin to one host.
    const observed = [_]i64{ 32_234_719_514, 7_620_802_119, 7_032_143_016, 5_581_515_167 };
    for (observed) |s| {
        try testing.expect(plausibleRelayCursor(s) != null);
    }
    // the spread between relays dwarfs any tolerance a heuristic could apply
    try testing.expect(observed[0] - observed[3] > 26_000_000_000);
}
