const std = @import("std");
const native = @import("native");
const io = std.Options.debug_io;
const alloc = std.heap.page_allocator;
const Actor = struct {
    did: []const u8,
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar_url: []const u8 = "",
    labels: []const u8 = "[]",
    associated: []const u8 = "{}",
    pds: []const u8 = "",
    created_at: []const u8 = "",
    quality_score: f32 = 0,
    hidden: bool = false,
    deleted: bool = false,
    version: i64,
};
const Batch = struct { feed_sha256: []const u8, cursor: u64, expected_cursor: u64, actors: []Actor };
const Stored = struct { feed_sha256: []const u8, cursor: u64, request_sha256: []const u8 };
const Receipt = struct { accepted: bool = true, cursor: u64, applied: u64 = 0, replayed: u64 = 0, stale: u64 = 0, transactions: u64 = 0, sqlite_changes: i64 = 0 };

fn env(comptime key: [:0]const u8) ![]const u8 {
    return std.mem.span(std.c.getenv(key) orelse return error.MissingQualificationInput);
}
fn hash(arena: std.mem.Allocator, bytes: []const u8) ![]const u8 {
    var out: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(bytes, &out, .{});
    return std.fmt.allocPrint(arena, "{s}", .{std.fmt.bytesToHex(out, .lower)});
}
fn inside(root: []const u8, path: []const u8) bool {
    if (path.len <= root.len or !std.mem.startsWith(u8, path, root) or path[root.len] != '/') return false;
    var it = std.mem.splitScalar(u8, path, '/');
    while (it.next()) |part| if (std.mem.eql(u8, part, "..") or std.mem.eql(u8, part, ".")) return false;
    return true;
}
fn checkedPath(root: []const u8, path: []const u8) !void {
    if (!inside(root, path)) return error.InvalidQualificationPath;
    const parent = std.fs.path.dirname(path) orelse return error.InvalidQualificationPath;
    const resolved_parent = try std.Io.Dir.realPathFileAbsoluteAlloc(io, parent, alloc);
    defer alloc.free(resolved_parent);
    if (!std.mem.eql(u8, root, resolved_parent) and !inside(root, resolved_parent)) return error.InvalidQualificationPath;
    const resolved = std.Io.Dir.realPathFileAbsoluteAlloc(io, path, alloc) catch |err| {
        if (err == error.FileNotFound) return;
        return err;
    };
    defer alloc.free(resolved);
    if (!inside(root, resolved)) return error.InvalidQualificationPath;
}
fn apply(local: *native.LocalDb, arena: std.mem.Allocator, bytes: []const u8, feed: []const u8) !Receipt {
    const parsed = try std.json.parseFromSlice(Batch, arena, bytes, .{});
    const batch = parsed.value;
    if (!std.mem.eql(u8, feed, batch.feed_sha256) or batch.actors.len > 1000 or batch.expected_cursor == std.math.maxInt(u64) or batch.cursor != batch.expected_cursor + 1) return error.InvalidBatch;
    var seen: std.StringHashMapUnmanaged(void) = .empty;
    for (batch.actors) |actor| {
        if (actor.did.len == 0 or actor.did.len > 2048 or actor.version <= 0 or !std.math.isFinite(actor.quality_score) or (!actor.deleted and actor.handle.len == 0)) return error.InvalidActor;
        if ((try seen.getOrPut(arena, actor.did)).found_existing) return error.DuplicateActor;
    }
    const request_hash = try hash(arena, bytes);
    local.lock();
    defer local.unlock();
    const conn = local.getConn() orelse return error.NoConn;
    try conn.exec("BEGIN IMMEDIATE", .{});
    errdefer conn.exec("ROLLBACK", .{}) catch {};
    const stored_row = try conn.row("SELECT value FROM sync_meta WHERE key='qualification_cursor'", .{});
    var current: u64 = 0;
    if (stored_row) |row| {
        defer row.deinit();
        const stored = (try std.json.parseFromSlice(Stored, arena, row.text(0), .{})).value;
        if (!std.mem.eql(u8, stored.feed_sha256, feed)) return error.FeedConflict;
        current = stored.cursor;
        if (batch.cursor == current and std.mem.eql(u8, stored.request_sha256, request_hash)) {
            try conn.exec("ROLLBACK", .{});
            return .{ .cursor = current, .replayed = batch.actors.len };
        }
    }
    if (batch.expected_cursor != current) return error.CursorConflict;
    var receipt: Receipt = .{ .cursor = batch.cursor };
    const changes_before = try conn.row("SELECT total_changes()", .{});
    const initial_changes = changes_before.?.int(0);
    changes_before.?.deinit();
    const floor: i64 = @intCast(local.attachedMetaInt(native.schema.meta.source_watermark));
    for (batch.actors) |actor| {
        const encoded = try std.json.Stringify.valueAlloc(arena, actor, .{});
        const actor_hash = try hash(arena, encoded);
        const previous = try conn.row("SELECT version,hash FROM qualification_actor_state WHERE did=?", .{actor.did});
        if (previous) |row| {
            defer row.deinit();
            if (actor.version < row.int(0)) {
                receipt.stale += 1;
                continue;
            }
            if (actor.version == row.int(0)) {
                if (!std.mem.eql(u8, row.text(1), actor_hash)) return error.SourceVersionConflict;
                receipt.replayed += 1;
                continue;
            }
        } else if (actor.version <= floor) {
            receipt.stale += 1;
            continue;
        }
        if (actor.deleted) {
            try native.sync.projectRemoval(conn, actor.did, "qualification", .{ .arena = arena, .updated_at = actor.version, .local = local });
        } else {
            var row: native.sync.InlineRow = .{ .vals = @splat(""), .qs = actor.quality_score };
            row.vals[native.sync.Col.did] = actor.did;
            row.vals[native.sync.Col.handle] = actor.handle;
            row.vals[native.sync.Col.display_name] = actor.display_name;
            row.vals[native.sync.Col.avatar_url] = actor.avatar_url;
            row.vals[native.sync.Col.labels] = actor.labels;
            row.vals[native.sync.Col.associated] = actor.associated;
            row.vals[native.sync.Col.pds] = actor.pds;
            row.vals[native.sync.Col.created_at] = actor.created_at;
            row.ints[0] = @intFromBool(actor.hidden);
            try native.sync.projectRow(conn, &row, .{ .arena = arena, .updated_at = actor.version, .local = local });
        }
        try conn.exec("INSERT INTO qualification_actor_state(did,version,hash) VALUES(?,?,?) ON CONFLICT(did) DO UPDATE SET version=excluded.version,hash=excluded.hash", .{ actor.did, actor.version, actor_hash });
        receipt.applied += 1;
    }
    const stored: Stored = .{ .feed_sha256 = feed, .cursor = batch.cursor, .request_sha256 = request_hash };
    try conn.exec("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('qualification_cursor',?)", .{try std.json.Stringify.valueAlloc(arena, stored, .{})});
    const after = (try conn.row("SELECT total_changes()", .{})).?;
    receipt.sqlite_changes = after.int(0) - initial_changes;
    after.deinit();
    try conn.exec("COMMIT", .{});
    receipt.transactions = 1;
    return receipt;
}
pub fn main() !void {
    const root = try env("NATIVE_QUAL_ROOT");
    if (root.len < 2 or root[0] != '/' or root[root.len - 1] == '/') return error.InvalidQualificationRoot;
    const resolved_root = try std.Io.Dir.realPathFileAbsoluteAlloc(io, root, alloc);
    defer alloc.free(resolved_root);
    if (!std.mem.eql(u8, root, resolved_root)) return error.InvalidQualificationRoot;
    const marker = try std.fmt.allocPrint(alloc, "{s}/owner", .{root});
    const owner = try std.Io.Dir.cwd().readFileAlloc(io, marker, alloc, .limited(100));
    if (!std.mem.eql(u8, owner, "native-qualification-v1")) return error.InvalidOwner;
    if (std.c.getenv("FLY_APP_NAME")) |name| if (!std.mem.startsWith(u8, std.mem.span(name), "typeahead-qual-")) return error.InvalidQualificationApp;
    const snapshot = try env("TYPEAHEAD_EVAL_SNAPSHOT");
    const overlay = try env("LOCAL_DB_PATH");
    const input = try env("NATIVE_QUAL_REQUEST");
    const output = try env("NATIVE_QUAL_RESPONSE");
    for ([_][]const u8{ snapshot, overlay, input, output, try env("SQLITE_TMPDIR") }) |path| try checkedPath(root, path);
    try checkedPath(root, try std.fmt.allocPrint(alloc, "{s}.prefix", .{snapshot}));
    try checkedPath(root, try std.fmt.allocPrint(alloc, "{s}.part", .{output}));
    const feed = try env("NATIVE_QUAL_FEED_SHA256");
    if (feed.len != 64) return error.InvalidFeedHash;
    for (feed) |c| if (!std.ascii.isDigit(c) and !(c >= 'a' and c <= 'f')) return error.InvalidFeedHash;
    var local = native.LocalDb.init(alloc);
    defer local.deinit();
    try local.open();
    try local.swapSnapshot(snapshot);
    const conn = local.getConn() orelse return error.NoConn;
    try conn.exec("PRAGMA foreign_keys=ON", .{});
    try conn.exec("CREATE TABLE IF NOT EXISTS qualification_actor_state(did TEXT PRIMARY KEY, version INTEGER NOT NULL, hash TEXT NOT NULL) WITHOUT ROWID", .{});
    const max_seconds = try std.fmt.parseInt(u64, if (std.c.getenv("NATIVE_QUAL_SECONDS")) |v| std.mem.span(v) else "900", 10);
    if (max_seconds == 0 or max_seconds > 900) return error.InvalidTimeBudget;
    const start = std.Io.Timestamp.now(io, .awake).nanoseconds;
    while (std.Io.Timestamp.now(io, .awake).nanoseconds - start < @as(i96, @intCast(max_seconds)) * std.time.ns_per_s) {
        var arena_state = std.heap.ArenaAllocator.init(alloc);
        defer arena_state.deinit();
        const arena = arena_state.allocator();
        const bytes = std.Io.Dir.cwd().readFileAlloc(io, input, arena, .limited(4 * 1024 * 1024)) catch |err| {
            if (err != error.FileNotFound) return err;
            io.sleep(std.Io.Duration.fromMilliseconds(10), .awake) catch {};
            continue;
        };
        const result = apply(&local, arena, bytes, feed);
        const encoded = if (result) |receipt| try std.json.Stringify.valueAlloc(arena, receipt, .{}) else |err| try std.json.Stringify.valueAlloc(arena, .{ .accepted = false, .err = @errorName(err) }, .{});
        const temporary = try std.fmt.allocPrint(arena, "{s}.part", .{output});
        try native.state.writeFile(temporary, encoded);
        try std.Io.Dir.renameAbsolute(temporary, output, io);
        try std.Io.Dir.deleteFile(.cwd(), io, input);
        if (std.c.getenv("NATIVE_QUAL_ONESHOT") != null) return;
    }
}
