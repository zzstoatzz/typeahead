//! Read-only Turso HTTP API client for sync
//! Uses hrana v2 pipeline protocol

const std = @import("std");
const io = std.Options.debug_io;
const http = std.http;
const json = std.json;
const mem = std.mem;
const Allocator = mem.Allocator;

const log = std.log.scoped(.turso);

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return mem.span(p);
    return null;
}

const TursoClient = @This();

const Value = struct { type: []const u8 = "text", value: []const u8 };
const Stmt = struct { sql: []const u8, args: ?[]const Value = null };
const ExecuteReq = struct { type: []const u8 = "execute", stmt: Stmt };
const CloseReq = struct { type: []const u8 = "close" };

allocator: Allocator,
url: []const u8, // host only (no protocol prefix)
token: []const u8,
http_client: http.Client,
mutex: std.Io.Mutex = std.Io.Mutex.init,

pub fn init(allocator: Allocator) !TursoClient {
    const url = cGetenv("TURSO_URL") orelse {
        log.err("TURSO_URL not set", .{});
        return error.MissingEnv;
    };
    const token = cGetenv("TURSO_AUTH_TOKEN") orelse {
        log.err("TURSO_AUTH_TOKEN not set", .{});
        return error.MissingEnv;
    };

    const libsql_prefix = "libsql://";
    const host = if (mem.startsWith(u8, url, libsql_prefix))
        url[libsql_prefix.len..]
    else
        url;

    log.info("turso client → {s}", .{host});

    return .{
        .allocator = allocator,
        .url = host,
        .token = token,
        .http_client = .{ .allocator = allocator, .io = io },
    };
}

pub fn deinit(self: *TursoClient) void {
    self.http_client.deinit();
}

pub const Row = struct {
    columns: []const json.Value,

    pub fn text(self: Row, index: usize) []const u8 {
        if (index >= self.columns.len) return "";
        return extractText(self.columns[index]);
    }

    pub fn int(self: Row, index: usize) i64 {
        if (index >= self.columns.len) return 0;
        return extractInt(self.columns[index]);
    }

    pub fn float(self: Row, index: usize) f64 {
        if (index >= self.columns.len) return 0;
        return extractFloat(self.columns[index]);
    }
};

pub const Result = struct {
    allocator: Allocator,
    parsed: ?json.Parsed(json.Value),
    rows: []const Row,

    pub fn deinit(self: *Result) void {
        self.allocator.free(self.rows);
        if (self.parsed) |*p| p.deinit();
    }
};

pub fn query(self: *TursoClient, sql: []const u8, args: []const []const u8) !Result {
    const response = try self.executeRaw(sql, args);
    defer self.allocator.free(response);
    return parseResult(self.allocator, response);
}

/// Typed bind value for the runtime-typed exec/query path. Hrana wants
/// per-arg type tags; emitting the right one matters for INTEGER columns
/// where text-encoded numbers would be coerced unpredictably.
///
/// `null` is included for future-proofing — zug binds only `text` and
/// `int` today.
pub const RuntimeValue = union(enum) {
    text: []const u8,
    int: i64,
    null,
};

/// Execute a statement that returns no rows (INSERT/UPDATE/CREATE/etc.)
/// using runtime-typed bind values. Required for callers that can't
/// express their args as `[]const []const u8` (e.g. zug binds an i64 for
/// the dirty flag).
pub fn execRuntime(self: *TursoClient, sql: []const u8, args: []const RuntimeValue) !void {
    const body = try self.buildTypedRequestBody(sql, args);
    defer self.allocator.free(body);
    const response = try self.doRequest(body);
    self.allocator.free(response);
}

/// Same as `query` but takes runtime-typed bind values.
pub fn queryRuntime(self: *TursoClient, sql: []const u8, args: []const RuntimeValue) !Result {
    const body = try self.buildTypedRequestBody(sql, args);
    defer self.allocator.free(body);
    const response = try self.doRequest(body);
    defer self.allocator.free(response);
    return parseResult(self.allocator, response);
}

fn executeRaw(self: *TursoClient, sql: []const u8, args: []const []const u8) ![]const u8 {
    const body = try self.buildRequestBody(sql, args);
    defer self.allocator.free(body);
    return self.doRequest(body);
}

/// Read deadline for one turso round trip. 2026-08-04 kernel-stack capture
/// proved a fetch can block in tcp_recvmsg indefinitely when the flow stalls
/// mid-response (stdlib http has no timeouts); the sync loop then wedges for
/// a full watchdog cycle. Generous vs the observed p99 (~15s cold) so it only
/// fires on genuinely dead flows.
///
/// The concurrent-task + deadline + `Future.cancel` shape below is worth
/// copying, not just reading: any zig service using stdlib http against a
/// remote has this exposure. pub-search's `backend/src/db/Client.zig` and its
/// builder still do (its 2026-08-03 `verify_remote` hang was this class,
/// bounded today only by watchdogs). If a third consumer shows up, this
/// belongs in a shared zat helper.
const FETCH_DEADLINE_MS: u64 = 60_000;

const FetchOut = struct {
    done: std.atomic.Value(bool) = .init(false),
    result: anyerror![]const u8 = error.HttpError,
};

fn fetchTask(self: *TursoClient, url: []const u8, auth: []const u8, payload: []const u8, out: *FetchOut) void {
    out.result = self.rawFetch(url, auth, payload);
    out.done.store(true, .release);
}

/// POST a fully-built hrana v2 pipeline JSON body. Shared between the
/// string-only `executeRaw` and the typed `execRuntime`/`queryRuntime`
/// paths so the HTTP / mutex / error handling stays one place.
///
/// The fetch runs in a concurrent task so this caller can enforce
/// FETCH_DEADLINE_MS: on expiry, Future.cancel interrupts the task at its
/// next cancelation point — including a recv blocked on a stalled flow —
/// and the caller returns error.FetchTimeout (retryable; the next attempt
/// opens a fresh connection since keep_alive=false).
fn doRequest(self: *TursoClient, body: []const u8) ![]const u8 {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "https://{s}/v2/pipeline", .{self.url}) catch
        return error.UrlTooLong;

    var auth_buf: [512]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.token}) catch
        return error.AuthTooLong;

    var out: FetchOut = .{};
    var future = std.Io.concurrent(io, fetchTask, .{ self, url, auth, body, &out }) catch |err| {
        // pool exhaustion is OUR resource problem — fail the query (callers
        // retry with backoff) rather than quietly degrading to an undeadlined
        // fetch that can block forever, which is the exact hang this deadline
        // exists to prevent.
        log.err("turso fetch: no concurrency available ({s}); failing for retry", .{@errorName(err)});
        return error.ConcurrencyUnavailable;
    };

    var waited_ms: u64 = 0;
    while (!out.done.load(.acquire) and waited_ms < FETCH_DEADLINE_MS) {
        io.sleep(.{ .nanoseconds = 100 * std.time.ns_per_ms }, .real) catch |err| {
            future.cancel(io);
            if (out.result) |bytes| self.allocator.free(bytes) else |_| {}
            return err;
        };
        waited_ms += 100;
    }

    if (!out.done.load(.acquire)) {
        log.err("turso fetch exceeded {d}ms deadline; canceling stalled request", .{FETCH_DEADLINE_MS});
        future.cancel(io);
        if (!out.done.load(.acquire)) return error.FetchTimeout;
        return out.result catch error.FetchTimeout;
    }

    future.await(io);
    return out.result;
}

fn fetch(self: *TursoClient, options: http.Client.FetchOptions) http.Client.FetchError!http.Client.FetchResult {
    if (self.http_client.now != null) self.http_client.now = std.Io.Clock.real.now(io);
    return self.http_client.fetch(options);
}

fn rawFetch(self: *TursoClient, url: []const u8, auth: []const u8, payload: []const u8) ![]const u8 {
    var response_body: std.Io.Writer.Allocating = .init(self.allocator);
    errdefer response_body.deinit();

    const res = self.fetch(.{
        .location = .{ .url = url },
        .method = .POST,
        .headers = .{
            .content_type = .{ .override = "application/json" },
            .authorization = .{ .override = auth },
        },
        .payload = payload,
        .response_writer = &response_body.writer,
        .keep_alive = false,
    }) catch |err| {
        log.err("http failed: {s}", .{@errorName(err)});
        return error.HttpError;
    };

    if (res.status != .ok) {
        const resp_text = response_body.toOwnedSlice() catch "";
        defer if (resp_text.len > 0) self.allocator.free(resp_text);
        const preview = if (resp_text.len > 200) resp_text[0..200] else resp_text;
        log.err("turso error: {} | {s}", .{ res.status, preview });
        return error.TursoError;
    }

    return try response_body.toOwnedSlice();
}

test "Turso HTTPS uses current time after a certificate rotates" {
    const url = cGetenv("TYPEAHEAD_TLS_TEST_URL") orelse return error.SkipZigTest;
    var client: TursoClient = .{
        .allocator = std.testing.allocator,
        .url = "",
        .token = "",
        .http_client = .{ .allocator = std.testing.allocator, .io = io },
    };
    defer client.deinit();
    const options: http.Client.FetchOptions = .{ .location = .{ .url = url }, .keep_alive = false };
    _ = try client.http_client.fetch(options);
    client.http_client.now = .{ .nanoseconds = 0 };
    try std.testing.expectError(error.TlsInitializationFailed, client.http_client.fetch(options));
    _ = try client.fetch(options);
    try std.testing.expect(client.http_client.now.?.nanoseconds > 0);
}

fn buildRequestBody(self: *TursoClient, sql: []const u8, args: []const []const u8) ![]const u8 {
    var body: std.Io.Writer.Allocating = .init(self.allocator);
    errdefer body.deinit();
    var jw: json.Stringify = .{ .writer = &body.writer, .options = .{ .emit_null_optional_fields = false } };

    var values: []const Value = &.{};
    defer if (values.len > 0) self.allocator.free(values);

    if (args.len > 0) {
        const v = try self.allocator.alloc(Value, args.len);
        for (args, 0..) |arg, i| {
            v[i] = .{ .value = arg };
        }
        values = v;
    }

    try jw.beginObject();
    try jw.objectField("requests");
    try jw.beginArray();
    try jw.write(ExecuteReq{
        .stmt = .{ .sql = sql, .args = if (values.len > 0) values else null },
    });
    try jw.write(CloseReq{});
    try jw.endArray();
    try jw.endObject();

    return try body.toOwnedSlice();
}

/// Hrana v2 pipeline body for a single execute with per-arg typed bind
/// values. Written manually (rather than via a serde-style struct) because
/// the hrana value shape varies per type — `text` and `integer` carry a
/// `value`, `null` doesn't — and integers serialize as a JSON string
/// (per hrana, to preserve i64 precision through JSON).
fn buildTypedRequestBody(self: *TursoClient, sql: []const u8, args: []const RuntimeValue) ![]const u8 {
    var body: std.Io.Writer.Allocating = .init(self.allocator);
    errdefer body.deinit();
    var jw: json.Stringify = .{ .writer = &body.writer, .options = .{ .emit_null_optional_fields = false } };

    try jw.beginObject();
    try jw.objectField("requests");
    try jw.beginArray();

    // execute request
    try jw.beginObject();
    try jw.objectField("type");
    try jw.write("execute");
    try jw.objectField("stmt");
    try jw.beginObject();
    try jw.objectField("sql");
    try jw.write(sql);
    if (args.len > 0) {
        try jw.objectField("args");
        try jw.beginArray();
        var int_buf: [24]u8 = undefined; // i64 max width is 20 chars (incl. sign), 24 is plenty
        for (args) |arg| {
            try jw.beginObject();
            switch (arg) {
                .text => |s| {
                    try jw.objectField("type");
                    try jw.write("text");
                    try jw.objectField("value");
                    try jw.write(s);
                },
                .int => |n| {
                    try jw.objectField("type");
                    try jw.write("integer");
                    try jw.objectField("value");
                    const s = std.fmt.bufPrint(&int_buf, "{d}", .{n}) catch return error.IntFormat;
                    try jw.write(s);
                },
                .null => {
                    try jw.objectField("type");
                    try jw.write("null");
                },
            }
            try jw.endObject();
        }
        try jw.endArray();
    }
    try jw.endObject();
    try jw.endObject();

    // close request
    try jw.write(CloseReq{});

    try jw.endArray();
    try jw.endObject();

    return try body.toOwnedSlice();
}

fn parseResult(allocator: Allocator, response: []const u8) !Result {
    const parsed = json.parseFromSlice(json.Value, allocator, response, .{}) catch return error.InvalidTursoResponse;
    errdefer parsed.deinit();
    const json_rows = getRowsFromParsed(parsed.value) orelse return error.InvalidTursoResponse;

    var rows: std.ArrayList(Row) = .empty;
    errdefer rows.deinit(allocator);

    for (json_rows.items) |item| {
        if (item != .array) return error.InvalidTursoResponse;
        try rows.append(allocator, .{ .columns = item.array.items });
    }

    return .{
        .allocator = allocator,
        .parsed = parsed,
        .rows = try rows.toOwnedSlice(allocator),
    };
}

fn getRowsFromResult(item: json.Value) ?json.Array {
    if (item != .object) return null;
    const kind = item.object.get("type") orelse return null;
    if (kind != .string or !mem.eql(u8, kind.string, "ok")) return null;
    const resp = item.object.get("response") orelse return null;
    if (resp != .object) return null;
    const res = resp.object.get("result") orelse return null;
    if (res != .object) return null;
    const rows = res.object.get("rows") orelse return null;
    if (rows != .array) return null;
    return rows.array;
}

fn getRowsFromParsed(value: json.Value) ?json.Array {
    if (value != .object) return null;
    const results = value.object.get("results") orelse return null;
    if (results != .array or results.array.items.len == 0) return null;
    return getRowsFromResult(results.array.items[0]);
}

fn extractText(val: json.Value) []const u8 {
    return switch (val) {
        .string => |s| s,
        .object => |obj| {
            const v = obj.get("value") orelse return "";
            return if (v == .string) v.string else "";
        },
        else => "",
    };
}

// Recursive, exhaustive-by-construction. Hrana wraps INTEGER columns as
// `{"type":"integer","value":"<digits>"}` (value is a quoted string to
// preserve i64 precision). The original implementation matched that exact
// shape but used a non-recursive inner switch with an `else => 0`, which
// silently dropped values that arrived in any other shape (e.g. number
// type without a value field, or future server-side encoding changes).
// When that happened to the rowid/updated_at trailing column, the keyset
// cursor in sync.zig never advanced and the loop re-fetched the same
// 2000-row batch forever, hammering Turso. Recursing on the inner Value
// makes this resilient to any of hrana's documented value shapes plus
// any reasonable bare JSON forms.
fn extractInt(val: json.Value) i64 {
    return switch (val) {
        .integer => |i| i,
        .float => |f| @as(i64, @intFromFloat(f)),
        .number_string => |s| std.fmt.parseInt(i64, s, 10) catch 0,
        .string => |s| std.fmt.parseInt(i64, s, 10) catch 0,
        .object => |obj| blk: {
            const v = obj.get("value") orelse {
                // log once with the keys we saw so we can diagnose future drift
                logExtractIntMiss(obj);
                break :blk 0;
            };
            break :blk extractInt(v);
        },
        .bool => |b| if (b) 1 else 0,
        else => 0,
    };
}

// Same shape-tolerance as extractInt — hrana wraps REAL columns as
// `{"type":"float","value":<number>}` (bare JSON number, no string quoting).
// Falls through to integer/string forms so we don't silently lose data
// if the server-side encoding ever shifts.
fn extractFloat(val: json.Value) f64 {
    return switch (val) {
        .float => |f| f,
        .integer => |i| @floatFromInt(i),
        .number_string => |s| std.fmt.parseFloat(f64, s) catch 0,
        .string => |s| std.fmt.parseFloat(f64, s) catch 0,
        .object => |obj| blk: {
            const v = obj.get("value") orelse break :blk 0;
            break :blk extractFloat(v);
        },
        .bool => |b| if (b) 1 else 0,
        else => 0,
    };
}

// ─── database export (GET /info + GET /export/{generation}) ──────────────
//
// The fast bulk-read path. `turso db export` does exactly this under the
// hood: GET /info for the current generation, then GET /export/{gen} which
// streams a standalone, internally-consistent SQLite checkpoint file.
//
// We deliberately SKIP the CLI's /sync WAL-frame reassembly. The /export
// body is a complete committed database (a real checkpoint boundary, never
// torn); the only thing the WAL frames add is the handful of writes
// committed after that checkpoint. Our index tolerates minutes-staleness
// (rebuild cadence is hours; the overlay layer covers recent changes), so
// the main DB file alone is the right snapshot. source_watermark comes
// from MAX(updated_at) in the exported file — the watermark of THAT
// snapshot, exactly.
//
// Measured ~60× faster wire throughput than /v3/cursor for bulk reads
// (export: 234s for 5.9M rows; cursor: ~4h).

/// GET /info → current_generation. Small JSON response, buffered.
fn fetchCurrentGeneration(self: *TursoClient) !i64 {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "https://{s}/info", .{self.url}) catch return error.UrlTooLong;
    var auth_buf: [512]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.token}) catch return error.AuthTooLong;

    var response_body: std.Io.Writer.Allocating = .init(self.allocator);
    defer response_body.deinit();

    const res = self.fetch(.{
        .location = .{ .url = url },
        .method = .GET,
        .headers = .{ .authorization = .{ .override = auth } },
        .response_writer = &response_body.writer,
        .keep_alive = false,
    }) catch |err| {
        log.err("/info http failed: {s}", .{@errorName(err)});
        return error.HttpError;
    };
    if (res.status != .ok) {
        log.err("/info status: {}", .{res.status});
        return error.TursoError;
    }

    const parsed = json.parseFromSlice(json.Value, self.allocator, response_body.written(), .{}) catch
        return error.TursoError;
    defer parsed.deinit();
    if (parsed.value != .object) return error.TursoError;
    const gen_v = parsed.value.object.get("current_generation") orelse return error.TursoError;
    return switch (gen_v) {
        .integer => |n| n,
        else => error.TursoError,
    };
}

/// Export the database to a local SQLite file. Two GETs: /info for the
/// generation, then /export/{gen} streamed straight to disk. Writes to
/// `<output_path>.part` first, then renames into place so a partial
/// download is never mistaken for a complete file. `output_path` must
/// be absolute.
pub fn exportToFile(self: *TursoClient, output_path: []const u8) !void {
    const gen = try self.fetchCurrentGeneration();
    log.info("turso export: current_generation={d}", .{gen});

    var part_buf: [320]u8 = undefined;
    const part_path = std.fmt.bufPrint(&part_buf, "{s}.part", .{output_path}) catch return error.PathTooLong;

    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "https://{s}/export/{d}", .{ self.url, gen }) catch return error.UrlTooLong;
    var auth_buf: [512]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.token}) catch return error.AuthTooLong;

    const file = std.Io.Dir.createFileAbsolute(io, part_path, .{ .truncate = true }) catch |err| {
        log.err("export create {s} failed: {s}", .{ part_path, @errorName(err) });
        return error.ExportFileCreateFailed;
    };
    // 64KB write buffer so the file writer batches fd writes; the HTTP
    // body streams through here, never buffered whole in memory.
    var write_buf: [64 * 1024]u8 = undefined;
    var fw = std.Io.File.Writer.init(file, io, &write_buf);

    const res = self.fetch(.{
        .location = .{ .url = url },
        .method = .GET,
        .headers = .{ .authorization = .{ .override = auth } },
        .response_writer = &fw.interface,
        .keep_alive = false,
    }) catch |err| {
        log.err("/export http failed: {s}", .{@errorName(err)});
        file.close(io);
        return error.HttpError;
    };

    fw.interface.flush() catch |err| {
        log.err("export flush failed: {s}", .{@errorName(err)});
        file.close(io);
        return error.ExportFlushFailed;
    };
    file.close(io);

    if (res.status != .ok) {
        log.err("/export status: {}", .{res.status});
        return error.TursoError;
    }

    std.Io.Dir.renameAbsolute(part_path, output_path, io) catch |err| {
        log.err("export rename {s} → {s} failed: {s}", .{ part_path, output_path, @errorName(err) });
        return error.ExportRenameFailed;
    };
}

var logged_miss: bool = false;
fn logExtractIntMiss(obj: anytype) void {
    if (logged_miss) return;
    logged_miss = true;
    var it = obj.iterator();
    var buf: [256]u8 = undefined;
    const w = std.fmt.bufPrint(&buf, "extractInt missed: keys=", .{}) catch return;
    var used = w.len;
    while (it.next()) |entry| {
        const k = entry.key_ptr.*;
        const remain = buf.len - used;
        if (remain < k.len + 2) break;
        const part = std.fmt.bufPrint(buf[used..], "{s},", .{k}) catch break;
        used += part.len;
    }
    log.warn("{s}", .{buf[0..used]});
}

test "query responses distinguish empty rows from failed or malformed statements" {
    const allocator = std.testing.allocator;
    var empty = try parseResult(allocator, "{\"results\":[{\"type\":\"ok\",\"response\":{\"type\":\"execute\",\"result\":{\"rows\":[]}}}]}");
    defer empty.deinit();
    try std.testing.expectEqual(@as(usize, 0), empty.rows.len);
    var one = try parseResult(allocator, "{\"results\":[{\"type\":\"ok\",\"response\":{\"type\":\"execute\",\"result\":{\"rows\":[[{\"type\":\"text\",\"value\":\"did:plc:example\"},{\"type\":\"integer\",\"value\":\"123\"}]]}}}]}");
    defer one.deinit();
    try std.testing.expectEqual(@as(usize, 1), one.rows.len);
    try std.testing.expectEqualStrings("did:plc:example", one.rows[0].text(0));
    try std.testing.expectEqual(@as(i64, 123), one.rows[0].int(1));
    const invalid = [_][]const u8{
        "not json",                                                                      "null",                                                                                "[]",                                                                            "{}", "{\"results\":[]}",
        "{\"results\":[{\"type\":\"error\",\"error\":{\"code\":\"SQL_INPUT_ERROR\"}}]}", "{\"results\":[{\"type\":\"ok\",\"response\":{\"type\":\"execute\",\"result\":{}}}]}", "{\"results\":[{\"type\":\"ok\",\"response\":{\"result\":{\"rows\":[null]}}}]}",
    };
    for (invalid) |body| try std.testing.expectError(error.InvalidTursoResponse, parseResult(allocator, body));
}
