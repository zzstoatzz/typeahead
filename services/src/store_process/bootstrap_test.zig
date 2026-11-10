const std = @import("std");
const store_mod = @import("../store/root.zig");
const store_harness = @import("../store/testing/harness.zig");
const bootstrap = @import("bootstrap.zig");
const config = @import("config.zig");
const Store = store_mod.Store;
const testing = std.testing;

const now = 1_794_000_000;
const mod = store_mod.moderation.bsky_moderation;

const policy_file =
    \\{"overrides":[{"did":"did:plc:shown","action":"show","reason":"false positive"},{"did":"did:plc:absent","action":"hide"}],
    \\ "blocked_domains":[{"domain":"Spam.Example","reason":"farm"}]}
;

const actor_lines =
    \\{"did":"did:plc:alice","handle":"Alice.Example","display_name":"Alice \"A\" 名前","avatar_url":"bafkreiaaa","labels":[{"src":"did:plc:alice","uri":"at://did:plc:alice/app.bsky.actor.profile/self","val":"!no-unauthenticated","cts":"2026-03-01T00:00:00.000Z"}],"associated":{"lists":3},"created_at":"2023-10-28T05:49:54.323Z","pds":"https://pds.example","pds_host":"pds.example","quality_score":16.5,"version":1767596894,"deleted":false,"hidden":false,"followers_count":7,"follows_count":170,"posts_count":10434,"last_activity_at":1774564108}
    \\{"did":"did:plc:spam","handle":"a.spam.example","display_name":"","avatar_url":"","labels":[],"associated":{},"created_at":"","pds":"","pds_host":"","quality_score":2.5,"version":1,"deleted":false,"hidden":true,"followers_count":0,"follows_count":0,"posts_count":0,"last_activity_at":0}
    \\{"did":"did:plc:shown","handle":"b.spam.example","display_name":"","avatar_url":"","labels":[],"associated":{},"created_at":"","pds":"","pds_host":"","quality_score":3.5,"version":1,"deleted":false,"hidden":false,"followers_count":0,"follows_count":0,"posts_count":0,"last_activity_at":0}
    \\{"did":"did:plc:labelled","handle":"l.example","display_name":"","avatar_url":"","labels":[{"src":"did:plc:ar7c4by46qjdydhdevvrndac","uri":"did:plc:labelled","val":"!takedown","cts":"2026-03-01T00:00:00.000Z"}],"associated":{},"created_at":"","pds":"","pds_host":"","quality_score":1,"version":1,"deleted":false,"hidden":true,"followers_count":0,"follows_count":0,"posts_count":0,"last_activity_at":0}
    \\{"did":"did:plc:gone","handle":"","display_name":"","avatar_url":"","labels":[],"associated":{},"created_at":"","pds":"","pds_host":"","quality_score":0,"version":9,"deleted":true,"hidden":false,"followers_count":0,"follows_count":0,"posts_count":0,"last_activity_at":0}
    \\
    \\this is not json
    \\{"did":"did:plc:last","handle":"last.example","quality_score":4}
;

fn open(tmp: *testing.TmpDir) !*Store {
    var options = store_harness.options(null);
    options.handle_flagged = null;
    options.maintenance.mode = .bulk_load;
    return Store.open(testing.allocator, testing.io, tmp.dir, "store", options);
}

fn loadAll(store: *Store, lines: []const u8, cursors: bootstrap.Cursors) !bootstrap.Summary {
    var arena: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena.deinit();
    var summary: bootstrap.Summary = .{};
    try bootstrap.applyPolicy(store, arena.allocator(), now, policy_file, &summary);
    var reader: std.Io.Reader = .fixed(lines);
    try bootstrap.load(testing.allocator, store, now, &reader, cursors, null, &summary);
    return summary;
}

test "a bundle's actors and the policy load into a store that is ready to follow" {
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    const store = try open(&tmp);
    defer store.close();
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const summary = try loadAll(store, actor_lines, .{ .relay = 32_234_719_514, .labeler = 77, .plc = "2026-11-06T21:16:00.003Z" });
    try testing.expectEqual(@as(u64, 7), summary.lines);
    try testing.expectEqual(@as(u64, 5), summary.loaded);
    try testing.expectEqual(@as(u64, 1), summary.skipped);
    try testing.expectEqual(@as(u64, 1), summary.rejected);
    try testing.expectEqual(@as(u64, 2), summary.overrides);
    try testing.expectEqual(@as(u64, 1), summary.blocked_domains);

    const alice = (try store.actor(arena, "did:plc:alice")).?;
    try testing.expectEqualStrings("Alice.Example", alice.handle);
    try testing.expectEqualStrings("Alice \"A\" 名前", alice.display_name);
    try testing.expectEqualStrings("bafkreiaaa", alice.avatar);
    try testing.expectEqualStrings("https://pds.example", alice.pds);
    try testing.expectEqualStrings("2023-10-28T05:49:54.323Z", alice.created_at);
    try testing.expectEqualStrings("{\"lists\":3}", alice.associated);
    try testing.expectEqual(@as(u64, 7), alice.followers_count);
    try testing.expectEqual(@as(u64, 10434), alice.posts_count);
    try testing.expectEqual(@as(f32, 16.5), alice.quality_score);
    try testing.expectEqual(@as(usize, 1), alice.labels.len);
    try testing.expect(!alice.hidden and alice.identity_due == 0 and alice.refresh_due == 0);
    try testing.expectEqualStrings("did:plc:alice", (try store.byHandle(arena, "alice.example"))[0]);

    try testing.expect((try store.actor(arena, "did:plc:spam")).?.hidden);
    try testing.expect(!(try store.actor(arena, "did:plc:shown")).?.hidden);
    const labelled = (try store.actor(arena, "did:plc:labelled")).?;
    try testing.expect(labelled.hidden);
    try testing.expectEqualStrings(mod, labelled.labels[0].source);
    try testing.expectEqual(@as(usize, 1), (try store_mod.read.labels(store.db(), arena, "did:plc:labelled")).len);
    try testing.expect(try store.actor(arena, "did:plc:gone") == null);
    try testing.expect(try store.actor(arena, "did:plc:last") != null);
    try testing.expect(try store.actor(arena, "did:plc:absent") == null);
    try testing.expectEqual(@as(usize, 2), (try store_mod.read.overrides(store.db(), arena)).len);

    // The cursors and the bridge position landed with the last batch, and
    // the bridge has nothing to push.
    const cursors = try store.cursors(arena);
    try testing.expectEqual(@as(?u64, 32_234_719_514), cursors.relay);
    try testing.expectEqual(@as(?u64, 77), cursors.labeler);
    try testing.expectEqualStrings("2026-11-06T21:16:00.003Z", cursors.plc.?);
    try testing.expectEqual(@as(?u64, cursors.next_change), cursors.bridge);
    try testing.expectEqual(@as(u64, 6), cursors.next_change);
    try testing.expectEqual(@as(?i64, 32_234_719_514), config.resumeCursor(cursors.relay, null));
    try testing.expectEqual(@as(usize, 0), (try store.changes(arena, cursors.bridge.?, 10)).entries.len);
}

test "an interrupted load leaves no cursor, and running it again completes it" {
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var lines: std.ArrayList(u8) = .empty;
    for (0..1000) |n| try lines.print(arena, "{{\"did\":\"did:plc:n{d:0>5}\",\"handle\":\"user{d}.example\",\"quality_score\":{d}.5}}\n", .{ n, n, n % 50 });

    {
        const store = try open(&tmp);
        defer store.close();
        // The process dies after two batches' worth of actors, before the
        // commit that carries the cursors.
        var partial: std.Io.Reader = .fixed(lines.items[0 .. std.mem.indexOfScalarPos(u8, lines.items, lines.items.len / 2, '\n').? + 1]);
        var count: usize = 0;
        const batch = try store.begin(now);
        while (try partial.takeDelimiter('\n')) |text| : (count += 1) {
            const parsed = try std.json.parseFromSliceLeaky(struct { did: []const u8, handle: []const u8 }, arena, text, .{ .ignore_unknown_fields = true });
            _ = try batch.apply(.{ .profile = .{ .did = parsed.did, .handle = parsed.handle } });
        }
        try batch.commit();
        try testing.expect(count > 400 and count < 1000);
        try testing.expectEqual(@as(?u64, null), (try store.cursors(arena)).relay);
        try testing.expectEqual(@as(?i64, null), config.resumeCursor((try store.cursors(arena)).relay, null));
    }

    const store = try open(&tmp);
    defer store.close();
    const summary = try loadAll(store, lines.items, .{ .relay = 900 });
    try testing.expectEqual(@as(u64, 1000), summary.loaded);
    try testing.expectEqual(@as(u64, 3), summary.commits);
    const cursors = try store.cursors(arena);
    try testing.expectEqual(@as(?u64, 900), cursors.relay);
    try testing.expectEqual(@as(?u64, cursors.next_change), cursors.bridge);
    try testing.expectEqual(@as(u64, 1000), (try store.counts()).get(.actor));
    try testing.expectEqual(@as(u64, 1000), (try store.counts()).get(.handle));
    try testing.expectEqual(@as(f32, 49.5), (try store.actor(arena, "did:plc:n00999")).?.quality_score);

    const again = try loadAll(store, lines.items, .{ .relay = 900 });
    try testing.expectEqual(@as(u64, 0), again.changed);
    try testing.expectEqual(cursors.next_change, (try store.cursors(arena)).next_change);
}

test "lines read through a small file buffer keep their own strings until the commit" {
    // Regression (2026-11-10): the parsed strings pointed into the reader's
    // buffer, so by the time a batch committed they held later lines' bytes.
    // The first run on a real file stored DIDs like `\n{"did":"did:plc:...`.
    var tmp = testing.tmpDir(.{ .iterate = true });
    defer tmp.cleanup();
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var lines: std.ArrayList(u8) = .empty;
    for (0..300) |n| try lines.print(arena, "{{\"did\":\"did:plc:n{d:0>5}\",\"handle\":\"user{d}.example\",\"display_name\":\"name {d}\",\"quality_score\":1.5}}\n", .{ n, n, n });
    try tmp.dir.writeFile(testing.io, .{ .sub_path = "actors.jsonl", .data = lines.items });

    const store = try open(&tmp);
    defer store.close();
    const file = try tmp.dir.openFile(testing.io, "actors.jsonl", .{});
    defer file.close(testing.io);
    var buffer: [256]u8 = undefined;
    var reader = file.reader(testing.io, &buffer);
    var summary: bootstrap.Summary = .{};
    try bootstrap.load(testing.allocator, store, now, &reader.interface, .{ .relay = 5 }, null, &summary);
    try testing.expectEqual(@as(u64, 300), summary.loaded);
    for ([_]usize{ 0, 1, 150, 299 }) |n| {
        const did = try std.fmt.allocPrint(arena, "did:plc:n{d:0>5}", .{n});
        const actor = (try store.actor(arena, did)).?;
        try testing.expectEqualStrings(try std.fmt.allocPrint(arena, "user{d}.example", .{n}), actor.handle);
        try testing.expectEqualStrings(try std.fmt.allocPrint(arena, "name {d}", .{n}), actor.display_name);
    }
    try testing.expectEqual(@as(u64, 300), (try store.counts()).get(.actor));
}
