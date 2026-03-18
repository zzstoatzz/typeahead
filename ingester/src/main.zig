const std = @import("std");
const mem = std.mem;
const json = std.json;
const http = std.http;
const Allocator = mem.Allocator;
const zat = @import("zat");

const log = std.log.scoped(.ingester);

const MAX_BATCH: usize = 100;

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
    hidden: ?bool = null,
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
    retry_after: i64 = 0, // timestamp before which we skip flush attempts

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

        // check self-labels for !no-unauthenticated (user opts out of unauthenticated visibility)
        event.hidden = blk: {
            const values = zat.json.getArray(record, "labels.values") orelse break :blk false;
            for (values) |item| {
                const val = zat.json.getString(item, "val") orelse continue;
                if (mem.eql(u8, val, "!no-unauthenticated")) break :blk true;
            }
            break :blk false;
        };

        self.buffer.append(self.allocator, event) catch return;
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
                self.allocator,
                self.config,
                batch,
                self.flushed_cursor,
            );
            if (ok) {
                self.total_ingested += batch_end;
                log.info("+{d} actors (total: {d}, pending: {d}) cursor={d}", .{
                    batch_end, self.total_ingested,
                    self.buffer.items.len - batch_end, self.pending_cursor,
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
                self.allocator,
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

fn postBatch(allocator: Allocator, config: Config, events: []const ActorEvent, cursor: i64) bool {
    var client = http.Client{ .allocator = allocator };
    defer client.deinit();

    var output: std.Io.Writer.Allocating = .init(allocator);
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

    var resp_output: std.Io.Writer.Allocating = .init(allocator);
    defer resp_output.deinit();

    const body = output.toArrayList();

    const result = client.fetch(.{
        .location = .{ .url = url },
        .method = .POST,
        .headers = .{
            .content_type = .{ .override = "application/json" },
            .authorization = .{ .override = auth },
        },
        .payload = body.items,
        .response_writer = &resp_output.writer,
    }) catch return false;

    if (result.status != .ok) {
        const resp = resp_output.toArrayList();
        log.err("ingest HTTP {d}: {s}", .{ @intFromEnum(result.status), resp.items });
    }

    return result.status == .ok;
}

fn deleteActors(allocator: Allocator, config: Config, dids: []const []const u8) bool {
    var client = http.Client{ .allocator = allocator };
    defer client.deinit();

    var output: std.Io.Writer.Allocating = .init(allocator);
    defer output.deinit();

    var jw: json.Stringify = .{ .writer = &output.writer };
    jw.write(.{ .dids = dids }) catch return false;

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "{s}/admin/delete", .{config.worker_url}) catch return false;

    var auth_buf: [256]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{config.secret}) catch return false;

    const body = output.toArrayList();

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
