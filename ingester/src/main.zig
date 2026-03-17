const std = @import("std");
const mem = std.mem;
const json = std.json;
const http = std.http;
const Allocator = mem.Allocator;
const zat = @import("zat");

const log = std.log.scoped(.ingester);

const MAX_BATCH: usize = 500;

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
    buffer: std.ArrayList(ActorEvent),
    delete_buffer: std.ArrayList([]const u8),
    /// arena owns all string data in buffer/delete_buffer
    arena: std.heap.ArenaAllocator,
    pending_cursor: i64 = 0,
    flushed_cursor: i64 = 0,
    total_ingested: u64 = 0,
    total_deleted: u64 = 0,
    last_flush: i64 = 0,

    fn init(allocator: Allocator, config: Config) IngestHandler {
        return .{
            .allocator = allocator,
            .config = config,
            .buffer = .{},
            .delete_buffer = .{},
            .arena = std.heap.ArenaAllocator.init(allocator),
            .last_flush = std.time.timestamp(),
        };
    }

    fn deinit(self: *IngestHandler) void {
        self.arena.deinit();
        self.buffer.deinit(self.allocator);
        self.delete_buffer.deinit(self.allocator);
    }

    fn dupe(self: *IngestHandler, s: []const u8) ?[]const u8 {
        return self.arena.allocator().dupe(u8, s) catch null;
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
            self.flushed_cursor = self.pending_cursor;
        }

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
        if (!mem.eql(u8, c.collection, "app.bsky.actor.profile")) return;
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

        if (event.display_name != null or event.avatar_cid != null) {
            self.buffer.append(self.allocator, event) catch return;
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
        var ingest_ok = true;
        var delete_ok = true;

        if (self.buffer.items.len > 0) {
            const count = self.buffer.items.len;
            ingest_ok = postBatch(
                self.allocator,
                self.config,
                self.buffer.items,
                self.flushed_cursor,
            );
            if (ingest_ok) {
                self.total_ingested += count;
                log.info("+{d} actors (total: {d}) cursor={d}", .{
                    count, self.total_ingested, self.pending_cursor,
                });
                self.buffer.clearRetainingCapacity();
            } else {
                log.err("ingest batch failed ({d} events), will retry", .{count});
            }
        }

        if (self.delete_buffer.items.len > 0) {
            const count = self.delete_buffer.items.len;
            delete_ok = deleteActors(
                self.allocator,
                self.config,
                self.delete_buffer.items,
            );
            if (delete_ok) {
                self.total_deleted += count;
                log.info("-{d} moderated (total: {d})", .{
                    count, self.total_deleted,
                });
                self.delete_buffer.clearRetainingCapacity();
            } else {
                log.err("delete batch failed ({d} dids), will retry", .{count});
            }
        }

        // only advance cursor when all pending work succeeded
        if (ingest_ok and delete_ok) {
            self.flushed_cursor = self.pending_cursor;
            // free all duped strings at once — safe because buffers are empty
            _ = self.arena.reset(.retain_capacity);
        }
    }
};

fn writeJsonEscaped(w: anytype, s: []const u8) !void {
    for (s) |c| {
        switch (c) {
            '"' => try w.writeAll("\\\""),
            '\\' => try w.writeAll("\\\\"),
            '\n' => try w.writeAll("\\n"),
            '\r' => try w.writeAll("\\r"),
            '\t' => try w.writeAll("\\t"),
            else => {
                if (c < 0x20) {
                    try w.print("\\u{x:0>4}", .{c});
                } else {
                    try w.writeByte(c);
                }
            },
        }
    }
}

fn postBatch(allocator: Allocator, config: Config, events: []const ActorEvent, cursor: i64) bool {
    var client = http.Client{ .allocator = allocator };
    defer client.deinit();

    var body: std.ArrayList(u8) = .{};
    defer body.deinit(allocator);

    var w = body.writer(allocator);
    w.writeAll("{\"events\":[") catch return false;

    for (events, 0..) |event, i| {
        if (i > 0) w.writeByte(',') catch return false;
        w.writeAll("{\"did\":\"") catch return false;
        w.writeAll(event.did) catch return false;
        w.writeByte('"') catch return false;
        if (event.handle) |h| {
            w.writeAll(",\"handle\":\"") catch return false;
            writeJsonEscaped(&w, h) catch return false;
            w.writeByte('"') catch return false;
        }
        if (event.display_name) |n| {
            w.writeAll(",\"display_name\":\"") catch return false;
            writeJsonEscaped(&w, n) catch return false;
            w.writeByte('"') catch return false;
        }
        if (event.avatar_cid) |c| {
            w.writeAll(",\"avatar_cid\":\"") catch return false;
            w.writeAll(c) catch return false;
            w.writeByte('"') catch return false;
        }
        w.writeByte('}') catch return false;
    }

    w.print("],\"cursor\":{d}}}", .{cursor}) catch return false;

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/ingest", .{config.worker_url}) catch return false;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return false;

    var aw: std.Io.Writer.Allocating = .init(allocator);
    defer aw.deinit();

    const result = client.fetch(.{
        .location = .{ .url = url },
        .method = .POST,
        .headers = .{
            .content_type = .{ .override = "application/json" },
            .authorization = .{ .override = auth },
        },
        .payload = body.items,
        .response_writer = &aw.writer,
    }) catch return false;

    if (result.status != .ok) {
        const resp = aw.toArrayList();
        log.err("ingest HTTP {d}: {s}", .{ @intFromEnum(result.status), resp.items });
    }

    return result.status == .ok;
}

fn deleteActors(allocator: Allocator, config: Config, dids: []const []const u8) bool {
    var client = http.Client{ .allocator = allocator };
    defer client.deinit();

    var body: std.ArrayList(u8) = .{};
    defer body.deinit(allocator);

    var w = body.writer(allocator);
    w.writeAll("{\"dids\":[") catch return false;
    for (dids, 0..) |did, i| {
        if (i > 0) w.writeByte(',') catch return false;
        w.writeByte('"') catch return false;
        w.writeAll(did) catch return false;
        w.writeByte('"') catch return false;
    }
    w.writeAll("]}") catch return false;

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/delete", .{config.worker_url}) catch return false;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return false;

    const result = client.fetch(.{
        .location = .{ .url = url },
        .method = .POST,
        .headers = .{
            .content_type = .{ .override = "application/json" },
            .authorization = .{ .override = auth },
        },
        .payload = body.items,
    }) catch return false;

    return result.status == .ok;
}

fn fetchCursorOnce(allocator: Allocator, config: Config) ?i64 {
    var client = http.Client{ .allocator = allocator };
    defer client.deinit();

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/cursor", .{config.worker_url}) catch return null;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return null;

    var aw: std.Io.Writer.Allocating = .init(allocator);
    defer aw.deinit();

    const result = client.fetch(.{
        .location = .{ .url = url },
        .method = .GET,
        .headers = .{
            .authorization = .{ .override = auth },
        },
        .response_writer = &aw.writer,
    }) catch return null;

    if (result.status != .ok) return null;

    var resp = aw.toArrayList();
    defer resp.deinit(allocator);

    const parsed = json.parseFromSlice(json.Value, allocator, resp.items, .{}) catch return null;
    defer parsed.deinit();

    const cursor_val = parsed.value.object.get("cursor") orelse return null;
    return switch (cursor_val) {
        .integer => |n| n,
        .float => |f| @intFromFloat(f),
        else => null,
    };
}

fn fetchCursor(allocator: Allocator, config: Config) ?i64 {
    const backoff = [_]u64{ 1, 3, 10 };
    for (backoff, 0..) |delay, attempt| {
        if (fetchCursorOnce(allocator, config)) |cursor| return cursor;
        log.warn("cursor fetch failed (attempt {d}/3), retrying in {d}s", .{ attempt + 1, delay });
        std.Thread.sleep(delay * std.time.ns_per_s);
    }
    return null;
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    const config = getConfig();
    log.info("typeahead ingester → {s}", .{config.worker_url});

    const cursor = fetchCursor(allocator, config);
    if (cursor) |c| {
        log.info("resuming from cursor {d}", .{c});
    } else {
        log.info("no cursor found, starting from live", .{});
    }

    var handler = IngestHandler.init(allocator, config);
    defer handler.deinit();

    var client = zat.JetstreamClient.init(allocator, .{
        .wanted_collections = &.{"app.bsky.actor.profile"},
        .cursor = cursor,
    });
    defer client.deinit();

    client.subscribe(&handler);
}
