const std = @import("std");
const mem = std.mem;
const json = std.json;
const Allocator = mem.Allocator;
const zat = @import("zat");
const HttpTransport = zat.HttpTransport;

const log = std.log.scoped(.ingester);

const MAX_BATCH: usize = 100;
const BLOOM_BITS: usize = 10_000_000; // ~1.2MB fixed
const BLOOM_HASHES: usize = 7;

const BloomFilter = struct {
    bits: std.DynamicBitSetUnmanaged,
    num_bits: usize,
    num_hashes: usize,
    count: usize = 0,

    fn init(allocator: Allocator, num_bits: usize, num_hashes: usize) !BloomFilter {
        const bits = try std.DynamicBitSetUnmanaged.initEmpty(allocator, num_bits);
        return .{
            .bits = bits,
            .num_bits = num_bits,
            .num_hashes = num_hashes,
        };
    }

    fn deinit(self: *BloomFilter, allocator: Allocator) void {
        self.bits.deinit(allocator);
    }

    fn hashIndices(self: *const BloomFilter, key: []const u8) [BLOOM_HASHES]usize {
        const h1 = std.hash.Wyhash.hash(0, key);
        const h2 = std.hash.Wyhash.hash(1, key);
        var indices: [BLOOM_HASHES]usize = undefined;
        for (0..self.num_hashes) |i| {
            indices[i] = @intCast((h1 +% i *% h2) % self.num_bits);
        }
        return indices;
    }

    fn insert(self: *BloomFilter, key: []const u8) void {
        const indices = self.hashIndices(key);
        for (indices) |idx| {
            self.bits.set(idx);
        }
        self.count += 1;
    }

    fn contains(self: *const BloomFilter, key: []const u8) bool {
        const indices = self.hashIndices(key);
        for (indices) |idx| {
            if (!self.bits.isSet(idx)) return false;
        }
        return true;
    }

    fn reset(self: *BloomFilter) void {
        self.bits.unsetAll();
        self.count = 0;
    }
};

const Config = struct {
    worker_url: []const u8,
    secret: []const u8,
};

fn getConfig() Config {
    return .{
        .worker_url = std.posix.getenv("TYPEAHEAD_URL") orelse
            @panic("TYPEAHEAD_URL not set"),
        .secret = std.posix.getenv("TYPEAHEAD_SECRET") orelse
            @panic("TYPEAHEAD_SECRET not set"),
    };
}

/// an actor event to POST to the worker
const ActorEvent = struct {
    did: []const u8,
    handle: ?[]const u8 = null,
    display_name: ?[]const u8 = null,
    avatar_cid: ?[]const u8 = null,
};

const IngestHandler = struct {
    allocator: Allocator,
    config: Config,
    transport: *HttpTransport,
    buffer: std.ArrayList(ActorEvent),
    delete_buffer: std.ArrayList([]const u8),
    /// arena owns all string data in buffer/delete_buffer
    arena: std.heap.ArenaAllocator,
    /// bloom filter: fixed-size dedup for non-profile DIDs (~1.2MB)
    bloom: BloomFilter,
    pending_cursor: i64 = 0,
    flushed_cursor: i64 = 0,
    total_ingested: u64 = 0,
    total_deleted: u64 = 0,
    last_flush: i64 = 0,
    retry_after: i64 = 0, // timestamp before which we skip flush attempts

    fn init(allocator: Allocator, config: Config, transport: *HttpTransport) !IngestHandler {
        return .{
            .allocator = allocator,
            .config = config,
            .transport = transport,
            .buffer = .{},
            .delete_buffer = .{},
            .arena = std.heap.ArenaAllocator.init(allocator),
            .bloom = try BloomFilter.init(allocator, BLOOM_BITS, BLOOM_HASHES),
            .last_flush = std.time.timestamp(),
        };
    }

    fn deinit(self: *IngestHandler) void {
        self.bloom.deinit(self.allocator);
        self.arena.deinit();
        self.buffer.deinit(self.allocator);
        self.delete_buffer.deinit(self.allocator);
    }

    fn dupe(self: *IngestHandler, s: []const u8) ?[]const u8 {
        return self.arena.allocator().dupe(u8, s) catch null;
    }

    fn resetBloom(self: *IngestHandler) void {
        log.info("resetting bloom filter ({d} insertions)", .{self.bloom.count});
        self.bloom.reset();
    }

    pub fn onEvent(self: *IngestHandler, event: zat.JetstreamEvent) void {
        switch (event) {
            .commit => |c| self.handleCommit(c),
            .identity => |id| self.handleIdentity(id),
            .account => |acct| self.handleAccount(acct),
        }

        self.pending_cursor = event.timeUs();

        const now = std.time.timestamp();
        // drop incoming events if buffers are backed up (persistent failure)
        const MAX_BACKLOG = MAX_BATCH * 10;
        if (self.buffer.items.len >= MAX_BACKLOG or self.delete_buffer.items.len >= MAX_BACKLOG) {
            log.err("backlog overflow ({d} ingest, {d} delete), dropping oldest events", .{
                self.buffer.items.len, self.delete_buffer.items.len,
            });
            self.buffer.clearRetainingCapacity();
            self.delete_buffer.clearRetainingCapacity();
            _ = self.arena.reset(.retain_capacity);
            self.resetBloom();
            self.flushed_cursor = self.pending_cursor;
            self.retry_after = 0;
        }

        // respect backoff cooldown after failures
        if (now < self.retry_after) return;

        const should_flush = self.buffer.items.len >= MAX_BATCH or
            self.delete_buffer.items.len >= MAX_BATCH or
            (now - self.last_flush >= 5 and (self.buffer.items.len > 0 or self.delete_buffer.items.len > 0));

        if (should_flush) {
            self.flush();
        }
    }

    pub fn onError(_: *IngestHandler, err: anyerror) void {
        log.err("jetstream: {s}", .{@errorName(err)});
    }

    pub fn onConnect(_: *IngestHandler, host: []const u8) void {
        log.info("connected to {s}", .{host});
    }

    fn handleCommit(self: *IngestHandler, c: zat.jetstream.CommitEvent) void {
        if (mem.eql(u8, c.collection, "app.bsky.actor.profile")) {
            // rich extraction from profile records
            if (c.operation != .create and c.operation != .update) return;

            const record = c.record orelse return;

            const did = self.dupe(c.did) orelse return;
            var event = ActorEvent{ .did = did };

            if (zat.json.getString(record, "displayName")) |name| {
                event.display_name = self.dupe(name);
            }

            if (zat.json.getPath(record, "avatar")) |avatar| {
                if (zat.json.getString(avatar, "ref.$link")) |cid| {
                    event.avatar_cid = self.dupe(cid);
                }
            }

            // ingester only sees self-labels from profile records — it can never
            // correctly determine hidden. let refreshModeration cron handle it.

            self.buffer.append(self.allocator, event) catch return;
        } else {
            // non-profile collections: just discover the DID
            if (c.operation == .delete) return;

            // dedup: skip if bloom filter says we've seen this DID recently
            if (self.bloom.contains(c.did)) return;
            self.bloom.insert(c.did);

            const did = self.dupe(c.did) orelse return;
            self.buffer.append(self.allocator, .{ .did = did }) catch return;
        }
    }

    fn handleIdentity(self: *IngestHandler, id: zat.jetstream.IdentityEvent) void {
        const handle = id.handle orelse return;
        self.buffer.append(self.allocator, .{
            .did = self.dupe(id.did) orelse return,
            .handle = self.dupe(handle),
        }) catch return;
    }

    fn handleAccount(self: *IngestHandler, acct: zat.jetstream.AccountEvent) void {
        if (!acct.active) {
            self.delete_buffer.append(self.allocator, self.dupe(acct.did) orelse return) catch return;
        }
    }

    fn flush(self: *IngestHandler) void {
        self.last_flush = std.time.timestamp();
        var any_failure = false;

        if (self.buffer.items.len > 0) {
            // send at most MAX_BATCH at a time to avoid overwhelming D1
            const batch_end = @min(self.buffer.items.len, MAX_BATCH);
            const batch = self.buffer.items[0..batch_end];
            const ok = postBatch(
                self.transport,
                self.config,
                batch,
                self.flushed_cursor,
            );
            if (ok) {
                self.total_ingested += batch_end;
                const rss = getRssKB();
                log.info("+{d} actors (total: {d}, pending: {d}) cursor={d} rss={d}KB", .{
                    batch_end, self.total_ingested,
                    self.buffer.items.len - batch_end, self.pending_cursor, rss,
                });
                // shift remaining items forward
                if (batch_end < self.buffer.items.len) {
                    std.mem.copyForwards(
                        ActorEvent,
                        self.buffer.items[0 .. self.buffer.items.len - batch_end],
                        self.buffer.items[batch_end..],
                    );
                }
                self.buffer.shrinkRetainingCapacity(self.buffer.items.len - batch_end);
            } else {
                log.err("ingest batch failed ({d} events, {d} pending), will retry in 5s", .{
                    batch_end, self.buffer.items.len,
                });
                any_failure = true;
            }
        }

        if (self.delete_buffer.items.len > 0) {
            const batch_end = @min(self.delete_buffer.items.len, MAX_BATCH);
            const batch = self.delete_buffer.items[0..batch_end];
            const ok = deleteActors(
                self.transport,
                self.config,
                batch,
            );
            if (ok) {
                self.total_deleted += batch_end;
                log.info("-{d} moderated (total: {d})", .{
                    batch_end, self.total_deleted,
                });
                if (batch_end < self.delete_buffer.items.len) {
                    const remaining = self.delete_buffer.items.len - batch_end;
                    // shift items - need to copy as slices
                    var i: usize = 0;
                    while (i < remaining) : (i += 1) {
                        self.delete_buffer.items[i] = self.delete_buffer.items[batch_end + i];
                    }
                    self.delete_buffer.shrinkRetainingCapacity(remaining);
                } else {
                    self.delete_buffer.clearRetainingCapacity();
                }
            } else {
                log.err("delete batch failed ({d} dids), will retry in 5s", .{batch_end});
                any_failure = true;
            }
        }

        if (any_failure) {
            self.retry_after = std.time.timestamp() + 5;
        } else if (self.buffer.items.len == 0 and self.delete_buffer.items.len == 0) {
            // only advance cursor and free arena when all work is drained
            self.flushed_cursor = self.pending_cursor;
            _ = self.arena.reset(.retain_capacity);
        }
    }
};

fn getRssKB() u64 {
    const f = std.fs.openFileAbsolute("/proc/self/statm", .{}) catch return 0;
    defer f.close();
    var buf: [128]u8 = undefined;
    const n = f.read(&buf) catch return 0;
    // statm: size resident shared text lib data dt (in pages)
    var it = std.mem.splitScalar(u8, buf[0..n], ' ');
    _ = it.next(); // size
    const rss_pages = std.fmt.parseInt(u64, it.next() orelse return 0, 10) catch return 0;
    return rss_pages * 4; // pages are 4KB on Linux
}

fn postBatch(transport: *HttpTransport, config: Config, events: []const ActorEvent, cursor: i64) bool {
    var output: std.Io.Writer.Allocating = .init(transport.allocator);
    defer output.deinit();

    var jw: json.Stringify = .{
        .writer = &output.writer,
        .options = .{ .emit_null_optional_fields = false },
    };
    jw.write(.{ .events = events, .cursor = cursor }) catch return false;

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/ingest", .{config.worker_url}) catch return false;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return false;

    const result = transport.fetch(.{
        .url = url,
        .method = .POST,
        .payload = output.written(),
        .authorization = auth,
    }) catch return false;
    defer transport.allocator.free(result.body);

    if (result.status != .ok) {
        log.err("ingest HTTP {d}: {s}", .{ @intFromEnum(result.status), result.body });
    }

    return result.status == .ok;
}

fn deleteActors(transport: *HttpTransport, config: Config, dids: []const []const u8) bool {
    var output: std.Io.Writer.Allocating = .init(transport.allocator);
    defer output.deinit();

    var jw: json.Stringify = .{ .writer = &output.writer };
    jw.write(.{ .dids = dids }) catch return false;

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/delete", .{config.worker_url}) catch return false;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return false;

    const result = transport.fetch(.{
        .url = url,
        .method = .POST,
        .payload = output.written(),
        .authorization = auth,
    }) catch return false;
    defer transport.allocator.free(result.body);

    return result.status == .ok;
}

fn fetchCursorOnce(transport: *HttpTransport, config: Config) ?i64 {
    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/cursor", .{config.worker_url}) catch return null;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return null;

    const result = transport.fetch(.{
        .url = url,
        .authorization = auth,
    }) catch return null;
    defer transport.allocator.free(result.body);

    if (result.status != .ok) return null;

    const parsed = json.parseFromSlice(json.Value, transport.allocator, result.body, .{}) catch return null;
    defer parsed.deinit();

    const cursor_val = parsed.value.object.get("cursor") orelse return null;
    return switch (cursor_val) {
        .integer => |n| n,
        .float => |f| @intFromFloat(f),
        else => null,
    };
}

fn fetchCursor(transport: *HttpTransport, config: Config) ?i64 {
    const backoff = [_]u64{ 1, 3, 10 };
    for (backoff, 0..) |delay, attempt| {
        if (fetchCursorOnce(transport, config)) |cursor| return cursor;
        log.warn("cursor fetch failed (attempt {d}/3), retrying in {d}s", .{ attempt + 1, delay });
        std.Thread.sleep(delay * std.time.ns_per_s);
    }
    return null;
}

const HealthState = struct {
    handler: *IngestHandler,
};

fn healthThread(state: *HealthState) void {
    const addr = std.net.Address.parseIp4("0.0.0.0", 8080) catch return;
    var srv = addr.listen(.{ .reuse_address = true }) catch |err| {
        log.err("health listener failed: {s}", .{@errorName(err)});
        return;
    };
    log.info("health endpoint listening on :8080", .{});

    while (true) {
        const conn = srv.accept() catch continue;
        handleHealthConn(conn.stream, state.handler) catch {};
        conn.stream.close();
    }
}

fn handleHealthConn(stream: std.net.Stream, handler: *IngestHandler) !void {
    var buf: [1024]u8 = undefined;
    _ = stream.read(&buf) catch return;

    const rss = getRssKB();
    var body_buf: [256]u8 = undefined;
    const body = std.fmt.bufPrint(&body_buf, "{{\"status\":\"ok\",\"ingested\":{d},\"deleted\":{d},\"bloom\":{d},\"rss_kb\":{d}}}", .{
        handler.total_ingested, handler.total_deleted, handler.bloom.count, rss,
    }) catch return;

    var resp_buf: [512]u8 = undefined;
    const resp = std.fmt.bufPrint(&resp_buf, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {d}\r\nConnection: close\r\n\r\n{s}", .{
        body.len, body,
    }) catch return;

    _ = stream.write(resp) catch {};
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    const config = getConfig();
    log.info("typeahead ingester → {s}", .{config.worker_url});

    var transport = HttpTransport.init(allocator);
    transport.keep_alive = false;
    defer transport.deinit();

    const cursor = fetchCursor(&transport, config);
    if (cursor) |c| {
        log.info("resuming from cursor {d}", .{c});
    } else {
        log.info("no cursor found, starting from live", .{});
    }

    var handler = try IngestHandler.init(allocator, config, &transport);
    defer handler.deinit();

    // health endpoint in background thread
    var health_state = HealthState{ .handler = &handler };
    const health_thread = try std.Thread.spawn(.{}, healthThread, .{&health_state});
    health_thread.detach();

    var client = zat.JetstreamClient.init(allocator, .{
        .wanted_collections = &.{
            "app.bsky.actor.profile",
            "app.bsky.feed.post",
            "app.bsky.feed.like",
            "app.bsky.graph.follow",
        },
        .cursor = cursor,
    });
    defer client.deinit();

    client.subscribe(&handler);
}
