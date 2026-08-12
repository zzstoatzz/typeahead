//! HTTP server for search and health endpoints
//! Uses std.http.Server with thread pool

const std = @import("std");
const http = std.http;
const mem = std.mem;
const json = std.json;
const io = std.Options.debug_io;
const logfire = @import("logfire");
const LocalDb = @import("db/LocalDb.zig");
const search = @import("search.zig");
const ingest = @import("ingest.zig");
const identity = @import("identity.zig");
const promote_runner = @import("index/promote_runner.zig");
const index_search = @import("index/index_search.zig");
const sync = @import("db/sync.zig");

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    const v = std.c.getenv(name) orelse return null;
    return mem.span(v);
}

const log = std.log.scoped(.server);

const HTTP_BUF_SIZE = 65536;

pub fn handleConnection(stream: std.Io.net.Stream, local_db: *LocalDb, identity_pool: ?*identity.Pool) void {
    defer stream.close(io);

    var read_buffer: [HTTP_BUF_SIZE]u8 = undefined;
    var write_buffer: [HTTP_BUF_SIZE]u8 = undefined;

    var reader = stream.reader(io, &read_buffer);
    var writer = stream.writer(io, &write_buffer);

    var server = http.Server.init(&reader.interface, &writer.interface);

    while (true) {
        var request = server.receiveHead() catch |err| {
            if (err != error.HttpConnectionClosing and err != error.EndOfStream) {
                log.debug("http receive error: {}", .{err});
            }
            return;
        };

        handleRequest(&request, local_db, identity_pool) catch |err| {
            log.err("request error: {}", .{err});
            return;
        };

        if (!request.head.keep_alive) return;
    }
}

fn handleRequest(request: *http.Server.Request, local_db: *LocalDb, identity_pool: ?*identity.Pool) !void {
    const target = request.head.target;

    if (request.head.method == .OPTIONS) {
        try sendCors(request);
        return;
    }

    const path = if (mem.indexOf(u8, target, "?")) |qi| target[0..qi] else target;

    if (mem.eql(u8, path, "/search")) {
        try handleSearch(request, target, local_db);
    } else if (mem.eql(u8, path, "/debug/search")) {
        try handleDebugSearch(request, target, local_db);
    } else if (mem.eql(u8, path, "/health")) {
        try handleHealth(request, local_db, identity_pool);
    } else if (mem.eql(u8, path, "/debug/fts")) {
        try handleDebugFts(request, target, local_db);
    } else if (mem.eql(u8, path, "/index/status")) {
        try handleIndexStatus(request, local_db);
    } else if (mem.eql(u8, path, "/debug/index")) {
        try handleDebugIndex(request, target, local_db);
    } else if (mem.eql(u8, path, "/admin/refresh")) {
        try handleAdminRefresh(request, target, local_db);
    } else {
        try sendJson(request, .not_found, "{\"error\":\"not found\"}");
    }
}

/// Apply one actor the caller already has — the fast path for an explicit
/// "index this account".
///
/// Takes the whole row in the body instead of a DID. The DID-only version made
/// this service read the row back out of Turso, so the fast path depended on
/// the slowest component: with a bulk job running, an indexed point lookup
/// measured 28s and this endpoint simply hung. The worker wrote the row a
/// moment earlier and can just send it.
///
/// Accepting values from the caller grants no new privilege: the same
/// ADMIN_SECRET already authorises /admin/ingest on the worker to write
/// arbitrary actor rows into Turso, which the sync loop then copies here.
fn handleAdminRefresh(request: *http.Server.Request, target: []const u8, local_db: *LocalDb) !void {
    _ = target;
    const secret = cGetenv("ADMIN_SECRET") orelse {
        try sendJson(request, .service_unavailable, "{\"error\":\"ADMIN_SECRET not configured\"}");
        return;
    };

    var authed = false;
    var it = request.iterateHeaders();
    while (it.next()) |h| {
        if (std.ascii.eqlIgnoreCase(h.name, "authorization")) {
            const prefix = "Bearer ";
            if (h.value.len > prefix.len and mem.eql(u8, h.value[0..prefix.len], prefix) and
                mem.eql(u8, h.value[prefix.len..], secret)) authed = true;
        }
    }
    if (!authed) {
        try sendJson(request, .unauthorized, "{\"error\":\"unauthorized\"}");
        return;
    }

    var body_buf: [16 * 1024]u8 = undefined;
    const body = readBody(request, &body_buf) catch {
        try sendJson(request, .bad_request, "{\"error\":\"could not read body\"}");
        return;
    };

    const gpa = std.heap.page_allocator;
    var parsed = json.parseFromSlice(json.Value, gpa, body, .{}) catch {
        try sendJson(request, .bad_request, "{\"error\":\"invalid json\"}");
        return;
    };
    defer parsed.deinit();
    const obj = switch (parsed.value) {
        .object => |o| o,
        else => {
            try sendJson(request, .bad_request, "{\"error\":\"expected an object\"}");
            return;
        },
    };

    const str = struct {
        fn get(o: json.ObjectMap, key: []const u8, dflt: []const u8) []const u8 {
            const v = o.get(key) orelse return dflt;
            return switch (v) { .string => |x| x, else => dflt };
        }
    }.get;
    const num = struct {
        fn get(o: json.ObjectMap, key: []const u8) i64 {
            const v = o.get(key) orelse return 0;
            return switch (v) {
                .integer => |x| x,
                .float => |x| @intFromFloat(x),
                .bool => |x| @intFromBool(x),
                else => 0,
            };
        }
    }.get;
    const flt = struct {
        fn get(o: json.ObjectMap, key: []const u8) f64 {
            const v = o.get(key) orelse return 0;
            return switch (v) {
                .float => |x| x,
                .integer => |x| @floatFromInt(x),
                else => 0,
            };
        }
    }.get;

    const did = str(obj, "did", "");
    const handle = str(obj, "handle", "");
    if (did.len == 0 or handle.len == 0) {
        // a handle-less actor is not searchable and must not enter the overlay
        try sendJson(request, .bad_request, "{\"error\":\"did and handle are required\"}");
        return;
    }

    var row = sync.InlineRow{ .vals = .{
        did, handle,
        str(obj, "display_name", ""),
        str(obj, "avatar_url", ""),
        "", // hidden — read via int()
        str(obj, "labels", "[]"),
        str(obj, "created_at", ""),
        str(obj, "associated", "{}"),
        str(obj, "pds", ""),
        "", "", "", "", "",
    } };
    row.ints = .{
        num(obj, "hidden"),          num(obj, "followers_count"),
        num(obj, "follows_count"),   num(obj, "posts_count"),
        num(obj, "last_activity_at"),
    };
    row.qs = flt(obj, "quality_score");

    const updated_at = blk: {
        const v = num(obj, "updated_at");
        break :blk if (v > 0) v else @as(i64, @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s)));
    };

    sync.applyInline(local_db, &row, updated_at) catch |err| {
        log.err("admin refresh failed for {s}: {}", .{ did, err });
        try sendJson(request, .internal_server_error, "{\"error\":\"apply failed\"}");
        return;
    };
    try sendJson(request, .ok, "{\"ok\":true,\"applied\":true}");
}

fn readBody(request: *http.Server.Request, buf: []u8) ![]const u8 {
    var transfer_buf: [4096]u8 = undefined;
    const reader = request.readerExpectNone(&transfer_buf);
    const n = try reader.readSliceShort(buf);
    return buf[0..n];
}

fn handleSearch(request: *http.Server.Request, target: []const u8, local_db: *LocalDb) !void {
    if (!local_db.isReady()) {
        try sendJson(request, .service_unavailable, "{\"error\":\"not ready\"}");
        return;
    }

    const raw_q = parseQueryParam(target, "q") orelse parseQueryParam(target, "term") orelse {
        try sendJson(request, .bad_request, "{\"error\":\"missing q parameter\"}");
        return;
    };

    var decode_buf: [512]u8 = undefined;
    const q = percentDecode(raw_q, &decode_buf) orelse raw_q;

    if (q.len == 0) {
        try sendJson(request, .bad_request, "{\"error\":\"empty query\"}");
        return;
    }

    const limit_str = parseQueryParam(target, "limit");
    const limit: usize = if (limit_str) |s|
        std.fmt.parseInt(usize, s, 10) catch 10
    else
        10;
    const clamped_limit = @min(@max(limit, 1), 100);

    var output: std.Io.Writer.Allocating = .init(std.heap.page_allocator);
    defer output.deinit();

    // USE_PREFIX_INDEX routes /search to the precomputed prefix index; unset
    // keeps the FTS path. Same response shape either way. On index-path error
    // we 500/503 so the worker falls back to its configured fallback path.
    const search_result = if (std.c.getenv("USE_PREFIX_INDEX") != null)
        index_search.run(local_db, std.heap.page_allocator, q, clamped_limit, &output.writer)
    else
        search.search(local_db, std.heap.page_allocator, q, clamped_limit, &output.writer);
    search_result catch |err| {
        if (err == error.PoolExhausted) {
            try sendJson(request, .service_unavailable, "{\"error\":\"read pool exhausted\"}");
            return;
        }
        log.err("search failed: {s}", .{@errorName(err)});
        try sendJson(request, .internal_server_error, "{\"error\":\"search failed\"}");
        return;
    };

    const body = output.written();
    try request.respond(body, .{
        .extra_headers = &.{
            .{ .name = "content-type", .value = "application/json" },
            .{ .name = "access-control-allow-origin", .value = "*" },
            .{ .name = "access-control-allow-methods", .value = "GET, OPTIONS" },
            .{ .name = "access-control-allow-headers", .value = "content-type" },
        },
    });
}

fn handleDebugSearch(request: *http.Server.Request, target: []const u8, local_db: *LocalDb) !void {
    if (!local_db.isReady()) {
        try sendJson(request, .service_unavailable, "{\"error\":\"not ready\"}");
        return;
    }

    const raw_q = parseQueryParam(target, "q") orelse parseQueryParam(target, "term") orelse {
        try sendJson(request, .bad_request, "{\"error\":\"missing q parameter\"}");
        return;
    };

    var decode_buf: [512]u8 = undefined;
    const q = percentDecode(raw_q, &decode_buf) orelse raw_q;

    const limit_str = parseQueryParam(target, "limit");
    const limit: usize = if (limit_str) |s|
        std.fmt.parseInt(usize, s, 10) catch 25
    else
        25;
    const clamped_limit = @min(@max(limit, 1), 200);

    var output: std.Io.Writer.Allocating = .init(std.heap.page_allocator);
    defer output.deinit();

    // Must follow the SAME env switch as /search. A debug view of the engine
    // that isn't answering requests is worse than none: it explains a ranking
    // nobody sees (2026-08-12 — `q='fig ('` ranked #1 here while /search, on
    // the index path, didn't return it at all).
    const debug_result = if (std.c.getenv("USE_PREFIX_INDEX") != null)
        index_search.debug(local_db, std.heap.page_allocator, q, clamped_limit, &output.writer)
    else
        search.debugSearch(local_db, std.heap.page_allocator, q, clamped_limit, &output.writer);
    debug_result catch |err| {
        if (err == error.PoolExhausted) {
            try sendJson(request, .service_unavailable, "{\"error\":\"read pool exhausted\"}");
            return;
        }
        try sendJson(request, .internal_server_error, "{\"error\":\"debug search failed\"}");
        return;
    };

    const body = output.written();
    try request.respond(body, .{
        .extra_headers = &.{
            .{ .name = "content-type", .value = "application/json" },
            .{ .name = "access-control-allow-origin", .value = "*" },
        },
    });
}

fn handleHealth(request: *http.Server.Request, local_db: *LocalDb, identity_pool: ?*identity.Pool) !void {
    const span = logfire.span("http.health", .{});
    defer span.end();

    const ready = local_db.isReady();
    const actors = local_db.countActors();
    const rss = ingest.getRssKB();
    const now_unix: i64 = @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
    const sync_lag = local_db.syncLagSeconds(now_unix);

    var buf: [640]u8 = undefined;
    const body = if (identity_pool) |pool| blk: {
        const ic = pool.counters(io);
        span.setAttribute("identity.accepted", @as(i64, @intCast(ic.accepted)));
        span.setAttribute("identity.dropped", @as(i64, @intCast(ic.dropped)));
        span.setAttribute("identity.processed", @as(i64, @intCast(ic.processed)));
        span.setAttribute("identity.queued", @as(i64, @intCast(ic.queued)));
        break :blk std.fmt.bufPrint(&buf,
            \\{{"status":"ok","ready":{s},"actors":{d},"rss_kb":{d},"sync_lag_s":{d},"identity":{{"accepted":{d},"dropped":{d},"processed":{d},"queued":{d}}}}}
        , .{
            if (ready) "true" else "false",
            actors,
            rss,
            sync_lag,
            ic.accepted,
            ic.dropped,
            ic.processed,
            ic.queued,
        }) catch "{\"status\":\"ok\"}";
    } else std.fmt.bufPrint(&buf,
        \\{{"status":"ok","ready":{s},"actors":{d},"rss_kb":{d},"sync_lag_s":{d}}}
    , .{
        if (ready) "true" else "false",
        actors,
        rss,
        sync_lag,
    }) catch "{\"status\":\"ok\"}";

    span.setAttribute("sync_lag_s", sync_lag);

    span.setAttribute("ready", if (ready) "true" else "false");
    span.setAttribute("actors", @as(i64, @intCast(actors)));
    span.setStatus(.ok, null);

    try sendJson(request, .ok, body);
}

fn sendJson(request: *http.Server.Request, status: http.Status, body: []const u8) !void {
    try request.respond(body, .{
        .status = status,
        .extra_headers = &.{
            .{ .name = "content-type", .value = "application/json" },
            .{ .name = "access-control-allow-origin", .value = "*" },
            .{ .name = "access-control-allow-methods", .value = "GET, OPTIONS" },
            .{ .name = "access-control-allow-headers", .value = "content-type" },
        },
    });
}

fn sendCors(request: *http.Server.Request) !void {
    try request.respond("", .{
        .status = .no_content,
        .extra_headers = &.{
            .{ .name = "access-control-allow-origin", .value = "*" },
            .{ .name = "access-control-allow-methods", .value = "GET, OPTIONS" },
            .{ .name = "access-control-allow-headers", .value = "content-type" },
        },
    });
}

fn parseQueryParam(target: []const u8, param: []const u8) ?[]const u8 {
    const prefixes = [_][]const u8{ "?", "&" };
    for (prefixes) |prefix| {
        var search_buf: [64]u8 = undefined;
        const search_str = std.fmt.bufPrint(&search_buf, "{s}{s}=", .{ prefix, param }) catch continue;
        if (mem.indexOf(u8, target, search_str)) |idx| {
            const rest = target[idx + search_str.len ..];
            const end = mem.indexOf(u8, rest, "&") orelse rest.len;
            const value = rest[0..end];
            if (value.len == 0) return null;
            return value;
        }
    }
    return null;
}

/// Decode percent-encoded URL query value into caller-provided buffer.
/// Handles %XX hex pairs and '+' as space. Thread-safe (no shared state).
fn percentDecode(input: []const u8, buf: []u8) ?[]const u8 {
    var out: usize = 0;
    var i: usize = 0;
    while (i < input.len) {
        if (out >= buf.len) return null;
        if (input[i] == '%' and i + 2 < input.len) {
            const hi = hexVal(input[i + 1]) orelse {
                buf[out] = input[i];
                out += 1;
                i += 1;
                continue;
            };
            const lo = hexVal(input[i + 2]) orelse {
                buf[out] = input[i];
                out += 1;
                i += 1;
                continue;
            };
            buf[out] = (@as(u8, hi) << 4) | @as(u8, lo);
            out += 1;
            i += 3;
        } else if (input[i] == '+') {
            buf[out] = ' ';
            out += 1;
            i += 1;
        } else {
            buf[out] = input[i];
            out += 1;
            i += 1;
        }
    }
    return buf[0..out];
}

fn hexVal(c: u8) ?u4 {
    return switch (c) {
        '0'...'9' => @intCast(c - '0'),
        'a'...'f' => @intCast(c - 'a' + 10),
        'A'...'F' => @intCast(c - 'A' + 10),
        else => null,
    };
}

/// Debug endpoint: compare actors vs actors_fts for a DID
/// Usage: /debug/fts?did=did:plc:xxx
fn handleDebugFts(request: *http.Server.Request, target: []const u8, local_db: *LocalDb) !void {
    const did = parseQueryParam(target, "did") orelse {
        try sendJson(request, .bad_request, "{\"error\":\"missing did parameter\"}");
        return;
    };

    var decode_buf: [512]u8 = undefined;
    const term = percentDecode(did, &decode_buf) orelse did;

    var out: std.Io.Writer.Allocating = .init(std.heap.page_allocator);
    defer out.deinit();
    var jw: json.Stringify = .{ .writer = &out.writer };

    try jw.beginObject();

    // actors table row
    try jw.objectField("actors");
    try writeDebugRow(&jw, local_db, "SELECT did, handle, display_name FROM actors WHERE did = ?", term);

    // FTS table row for same DID
    try jw.objectField("fts");
    try writeDebugRow(&jw, local_db, "SELECT did, handle, display_name FROM actors_fts WHERE did = ?", term);

    try jw.endObject();

    try sendJson(request, .ok, out.written());
}

fn handleIndexStatus(request: *http.Server.Request, local_db: *LocalDb) !void {
    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena_state.deinit();

    var out: std.Io.Writer.Allocating = .init(std.heap.page_allocator);
    defer out.deinit();
    var jw: json.Stringify = .{ .writer = &out.writer };

    promote_runner.writeStatus(arena_state.allocator(), local_db, &jw) catch {
        try sendJson(request, .internal_server_error, "{\"error\":\"index status failed\"}");
        return;
    };
    try sendJson(request, .ok, out.written());
}

fn handleDebugIndex(request: *http.Server.Request, target: []const u8, local_db: *LocalDb) !void {
    const raw_q = parseQueryParam(target, "q") orelse "a";
    var decode_buf: [512]u8 = undefined;
    const q = percentDecode(raw_q, &decode_buf) orelse raw_q;

    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena_state.deinit();

    var out: std.Io.Writer.Allocating = .init(std.heap.page_allocator);
    defer out.deinit();
    var jw: json.Stringify = .{ .writer = &out.writer };

    promote_runner.writeProbe(arena_state.allocator(), local_db, q, &jw) catch {
        try sendJson(request, .internal_server_error, "{\"error\":\"index probe failed\"}");
        return;
    };
    try sendJson(request, .ok, out.written());
}

fn writeDebugRow(jw: anytype, local_db: *LocalDb, comptime sql: []const u8, param: []const u8) !void {
    const lease = local_db.checkoutRead() catch {
        try jw.write("error");
        return;
    };
    defer lease.release();

    var rows = lease.query(sql, .{param}) catch {
        try jw.write("error");
        return;
    };
    defer rows.deinit();
    if (rows.next()) |row| {
        try jw.beginObject();
        try jw.objectField("did");
        try jw.write(row.text(0));
        try jw.objectField("handle");
        try jw.write(row.text(1));
        try jw.objectField("display_name");
        try jw.write(row.text(2));
        try jw.endObject();
    } else {
        try jw.write(null);
    }
}
