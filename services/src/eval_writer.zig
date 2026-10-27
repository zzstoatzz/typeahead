const std = @import("std");
const io = std.Options.debug_io;
const LocalDb = @import("db/LocalDb.zig");
const sync = @import("db/sync.zig");
const pack = @import("index/pack.zig");
const schema = @import("index/schema.zig");
const zqlite = @import("zqlite");

pub const Counters = struct {
    batches: std.atomic.Value(u64) = .init(0),
    rows: std.atomic.Value(u64) = .init(0),
    failures: std.atomic.Value(u64) = .init(0),
    changes: std.atomic.Value(u64) = .init(0),
    max_lag_ms: std.atomic.Value(u64) = .init(0),
    active: std.atomic.Value(bool) = .init(false),

    pub const Snapshot = struct { batches: u64, rows: u64, failures: u64, changes: u64, max_lag_ms: u64, active: bool };

    pub fn snapshot(self: *const Counters) Snapshot {
        return .{ .batches = self.batches.load(.acquire), .rows = self.rows.load(.acquire), .failures = self.failures.load(.acquire), .changes = self.changes.load(.acquire), .max_lag_ms = self.max_lag_ms.load(.acquire), .active = self.active.load(.acquire) };
    }
};

pub const Writer = struct {
    const Actor = struct { did: []const u8, blob_hex: []const u8 };
    const Entry = struct { row: sync.InlineRow, timestamp: i64 };
    const Mode = enum { replay, mixed };

    local: *LocalDb,
    entries: []Entry,
    mode: Mode,
    trigger: []const u8,
    delay_ms: u64,
    counters: Counters = .{},

    pub fn init(allocator: std.mem.Allocator, local: *LocalDb) !?Writer {
        const source = std.c.getenv("TYPEAHEAD_EVAL_FEED") orelse return null;
        const mode = std.meta.stringToEnum(Mode, std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_WRITER_MODE") orelse return error.MissingWriterMode)) orelse return error.InvalidWriterMode;
        const trigger = std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_WRITER_START") orelse return error.MissingWriterTrigger);
        const delay_ms = try std.fmt.parseInt(u64, std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_WRITER_DELAY_MS") orelse "1000"), 10);
        if (delay_ms > 30000) return error.InvalidWriterDelay;
        const max_actors = try std.fmt.parseInt(usize, std.mem.span(std.c.getenv("TYPEAHEAD_EVAL_MAX_ACTORS") orelse "5000"), 10);
        if (max_actors == 0 or max_actors > 200000) return error.InvalidFeedSize;
        const bytes = try std.Io.Dir.cwd().readFileAlloc(io, std.mem.span(source), allocator, .limited(if (max_actors > 5000) 128 * 1024 * 1024 else 32 * 1024 * 1024));
        const actors = try std.json.parseFromSlice([]Actor, allocator, bytes, .{ .allocate = .alloc_if_needed });
        if (actors.value.len == 0 or actors.value.len > max_actors) return error.InvalidFeedSize;
        const entries = try allocator.alloc(Entry, actors.value.len);
        var seen: std.StringHashMapUnmanaged(void) = .empty;
        const watermark: i64 = @intCast(local.attachedMetaInt(schema.meta.source_watermark));
        if (watermark <= 0) return error.NoSnapshot;
        for (actors.value, entries) |actor, *entry| {
            const found = try seen.getOrPut(allocator, actor.did);
            if (found.found_existing) return error.DuplicateFeedActor;
            if (actor.blob_hex.len > 32768 or actor.blob_hex.len % 2 != 0) return error.InvalidFeedBlob;
            const blob = try allocator.alloc(u8, actor.blob_hex.len / 2);
            _ = try std.fmt.hexToBytes(blob, actor.blob_hex);
            const value = try pack.unpackActor(blob);
            entry.* = .{ .row = .{ .vals = @splat(""), .qs = value.quality_score }, .timestamp = watermark + 1 };
            entry.row.vals[sync.Col.did] = actor.did;
            entry.row.vals[sync.Col.handle] = value.handle;
            entry.row.vals[sync.Col.display_name] = value.display;
            entry.row.vals[sync.Col.avatar_url] = value.avatar;
            entry.row.vals[sync.Col.labels] = value.labels;
            entry.row.vals[sync.Col.created_at] = value.created_at;
            entry.row.vals[sync.Col.associated] = value.associated;
            entry.row.vals[sync.Col.pds] = value.pds;
            entry.row.ints[0] = @intFromBool(value.hidden);
        }
        return .{ .local = local, .entries = entries, .mode = mode, .trigger = trigger, .delay_ms = delay_ms };
    }

    fn batch(self: *Writer, first: usize, end: usize, pass: i64) !void {
        var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer arena.deinit();
        self.local.lock();
        defer self.local.unlock();
        const conn = self.local.getConn() orelse return error.NoConn;
        try conn.exec("BEGIN", .{});
        errdefer conn.exec("ROLLBACK", .{}) catch {};
        const changes_before = zqlite.c.sqlite3_total_changes64(conn.conn);
        for (self.entries[first..end], first..) |*entry, index| {
            const timestamp = entry.timestamp + if (self.mode == .mixed and index % 2 != 0) pass else 0;
            try sync.projectRow(conn, &entry.row, .{ .arena = arena.allocator(), .updated_at = timestamp, .local = self.local });
            _ = arena.reset(.retain_capacity);
        }
        try conn.exec("COMMIT", .{});
        _ = self.counters.changes.fetchAdd(@intCast(zqlite.c.sqlite3_total_changes64(conn.conn) - changes_before), .monotonic);
    }

    pub fn prefill(self: *Writer) !void {
        var first: usize = 0;
        while (first < self.entries.len) : (first += 500) try self.batch(first, @min(first + 500, self.entries.len), 0);
    }

    pub fn run(self: *Writer) void {
        while (true) {
            std.Io.Dir.cwd().access(io, self.trigger, .{}) catch {
                io.sleep(std.Io.Duration.fromMilliseconds(25), .awake) catch return;
                continue;
            };
            break;
        }
        var pass: i64 = 1;
        const started = std.Io.Timestamp.now(io, .awake).nanoseconds;
        var batch_number: i96 = 0;
        while (true) : (pass += 1) {
            var first: usize = 0;
            while (first < self.entries.len) : (first += 500) {
                const scheduled = started + batch_number * self.delay_ms * std.time.ns_per_ms;
                const now = std.Io.Timestamp.now(io, .awake).nanoseconds;
                if (scheduled > now) io.sleep(.{ .nanoseconds = scheduled - now }, .awake) catch return;
                const awake = std.Io.Timestamp.now(io, .awake).nanoseconds;
                _ = self.counters.max_lag_ms.fetchMax(@intCast(@divTrunc(@max(awake - scheduled, 0), std.time.ns_per_ms)), .monotonic);
                self.counters.active.store(true, .release);
                self.batch(first, @min(first + 500, self.entries.len), pass) catch {
                    _ = self.counters.failures.fetchAdd(1, .monotonic);
                    self.counters.active.store(false, .release);
                    return;
                };
                self.counters.active.store(false, .release);
                _ = self.counters.batches.fetchAdd(1, .monotonic);
                _ = self.counters.rows.fetchAdd(@min(@as(usize, 500), self.entries.len - first), .monotonic);
                batch_number += 1;
            }
            self.local.lock();
            const conn = self.local.getConn() orelse {
                self.local.unlock();
                return;
            };
            conn.exec("PRAGMA wal_checkpoint(TRUNCATE)", .{}) catch {};
            self.local.unlock();
        }
    }
};
