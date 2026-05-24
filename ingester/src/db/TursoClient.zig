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

/// POST a fully-built hrana v2 pipeline JSON body. Shared between the
/// string-only `executeRaw` and the typed `execRuntime`/`queryRuntime`
/// paths so the HTTP / mutex / error handling stays one place.
fn doRequest(self: *TursoClient, body: []const u8) ![]const u8 {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "https://{s}/v2/pipeline", .{self.url}) catch
        return error.UrlTooLong;

    var auth_buf: [512]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.token}) catch
        return error.AuthTooLong;

    var response_body: std.Io.Writer.Allocating = .init(self.allocator);
    errdefer response_body.deinit();

    const res = self.http_client.fetch(.{
        .location = .{ .url = url },
        .method = .POST,
        .headers = .{
            .content_type = .{ .override = "application/json" },
            .authorization = .{ .override = auth },
        },
        .payload = body,
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
    const parsed = json.parseFromSlice(json.Value, allocator, response, .{}) catch {
        return .{ .allocator = allocator, .parsed = null, .rows = &.{} };
    };

    const json_rows = getRowsFromParsed(parsed.value) orelse {
        return .{ .allocator = allocator, .parsed = parsed, .rows = &.{} };
    };

    var rows: std.ArrayList(Row) = .empty;
    errdefer rows.deinit(allocator);

    for (json_rows.items) |item| {
        if (item == .array) {
            try rows.append(allocator, .{ .columns = item.array.items });
        }
    }

    return .{
        .allocator = allocator,
        .parsed = parsed,
        .rows = try rows.toOwnedSlice(allocator),
    };
}

fn getRowsFromResult(item: json.Value) ?json.Array {
    if (item != .object) return null;
    const resp = item.object.get("response") orelse return null;
    if (resp != .object) return null;
    const res = resp.object.get("result") orelse return null;
    if (res != .object) return null;
    const rows = res.object.get("rows") orelse return null;
    if (rows != .array) return null;
    return rows.array;
}

fn getRowsFromParsed(value: json.Value) ?json.Array {
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

// ─── hrana v3 cursor (POST /v3/cursor) ───────────────────────────────────
//
// One HTTP request, one streaming NDJSON response:
//
//   POST /v3/cursor                  body: { baton: null, batch: {steps: [...]} }
//   ← line 1 (CursorRespBody):       {"baton": "...", "base_url": null}
//   ← lines 2..N (CursorEntry):      {"type": "step_begin"|"row"|"step_end"|...}
//
// Cursor verbs (open_cursor/fetch_cursor/close_cursor) are WebSocket-only
// per the spec — over HTTP the entire cursor lifecycle is this single
// endpoint with newline-framed response chunks.
//
// We MUST NOT buffer the entire response (could be GBs). The custom
// NdjsonCursorWriter below implements std.Io.Writer with a small
// per-line buffer that dispatches each parsed CursorEntry through a
// user-provided callback as bytes arrive off the wire.

/// One statement in a cursor batch. Each becomes its own step in the
/// CursorEntry stream (step_begin → rows → step_end).
pub const CursorStmt = struct {
    sql: []const u8,
    args: []const RuntimeValue = &.{},
};

/// One item in the streamed cursor response. Borrowed from the per-line
/// JSON parse — the callback MUST consume any strings/rows before
/// returning, or dupe them into its own arena.
pub const CursorEntry = union(enum) {
    step_begin: struct { step: u32 },
    step_end: struct { affected_row_count: u32 },
    row: Row,
    step_error: struct { step: u32, message: []const u8 },
    err: struct { message: []const u8 },
};

pub const CursorError = error{
    LineTooLong,
    NdjsonParseFailed,
    MalformedCursorEntry,
    MissingHeader,
    CallbackError,
};

/// Drive a /v3/cursor stream. The callback fires for every CursorEntry
/// as soon as it parses off the wire — never holds more than one entry
/// in memory at a time. Callback errors abort the stream and propagate.
///
/// `ctx` must be a pointer type so we can type-erase across the
/// std.Io.Writer drain boundary. `onEntry` is comptime-bound; the
/// concrete callback is preserved through the wrapper.
pub fn streamCursor(
    self: *TursoClient,
    allocator: Allocator,
    stmts: []const CursorStmt,
    ctx: anytype,
    comptime onEntry: fn (@TypeOf(ctx), CursorEntry) anyerror!void,
) !void {
    const CtxType = @TypeOf(ctx);
    comptime {
        const info = @typeInfo(CtxType);
        if (info != .pointer) {
            @compileError("streamCursor: ctx must be a pointer type, got " ++ @typeName(CtxType));
        }
    }

    const Wrapper = struct {
        fn call(opaque_ctx: *anyopaque, entry: CursorEntry) anyerror!void {
            const typed_ctx: CtxType = @ptrCast(@alignCast(opaque_ctx));
            return onEntry(typed_ctx, entry);
        }
    };

    var parser = NdjsonCursorWriter.init(
        allocator,
        @ptrCast(ctx),
        Wrapper.call,
    );
    defer parser.deinit();

    const body = try self.buildCursorReqBody(allocator, stmts);
    defer allocator.free(body);

    try self.doRequestStreaming("/v3/cursor", body, &parser.writer);

    if (parser.deferred_error) |err| return err;
    if (!parser.have_header) return error.MissingHeader;
}

/// Build a CursorReqBody JSON document.
fn buildCursorReqBody(
    self: *TursoClient,
    allocator: Allocator,
    stmts: []const CursorStmt,
) ![]const u8 {
    _ = self; // for symmetry with other buildXxxRequestBody methods

    var body: std.Io.Writer.Allocating = .init(allocator);
    errdefer body.deinit();
    var jw: json.Stringify = .{
        .writer = &body.writer,
        .options = .{ .emit_null_optional_fields = false },
    };

    try jw.beginObject();

    try jw.objectField("baton");
    try jw.write(null);

    try jw.objectField("batch");
    try jw.beginObject();
    try jw.objectField("steps");
    try jw.beginArray();
    for (stmts) |stmt| {
        try jw.beginObject();
        try jw.objectField("stmt");
        try jw.beginObject();
        try jw.objectField("sql");
        try jw.write(stmt.sql);
        if (stmt.args.len > 0) {
            try jw.objectField("args");
            try jw.beginArray();
            var int_buf: [24]u8 = undefined;
            for (stmt.args) |arg| {
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
        try jw.endObject(); // stmt
        try jw.endObject(); // step
    }
    try jw.endArray(); // steps
    try jw.endObject(); // batch

    try jw.endObject(); // root

    return try body.toOwnedSlice();
}

/// POST `body` to `path` on the Turso host, stream the response body
/// through `response_writer` chunk-by-chunk. Same auth + mutex shape
/// as the unary `doRequest`, but we don't buffer the response.
fn doRequestStreaming(
    self: *TursoClient,
    path: []const u8,
    body: []const u8,
    response_writer: *std.Io.Writer,
) !void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "https://{s}{s}", .{ self.url, path }) catch
        return error.UrlTooLong;

    var auth_buf: [512]u8 = undefined;
    const auth = std.fmt.bufPrint(&auth_buf, "Bearer {s}", .{self.token}) catch
        return error.AuthTooLong;

    const res = self.http_client.fetch(.{
        .location = .{ .url = url },
        .method = .POST,
        .headers = .{
            .content_type = .{ .override = "application/json" },
            .authorization = .{ .override = auth },
        },
        .payload = body,
        .response_writer = response_writer,
        .keep_alive = false,
    }) catch |err| {
        log.err("v3/cursor http failed: {s}", .{@errorName(err)});
        return error.HttpError;
    };

    if (res.status != .ok) {
        log.err("v3/cursor status: {}", .{res.status});
        return error.TursoError;
    }
}

/// Custom std.Io.Writer that splits the incoming response stream on
/// '\n' and dispatches each parsed CursorEntry through a callback.
///
/// `buffer` is set to empty so every drain delivers fresh bytes (no
/// writer-level coalescing). The line accumulator lives on
/// `line_buf` and is bounded by `line_max` (default 1 MiB) — pathological
/// lines beyond that fail the stream rather than buffer unboundedly.
const NdjsonCursorWriter = struct {
    allocator: Allocator,
    writer: std.Io.Writer,

    line_buf: std.ArrayListUnmanaged(u8) = .empty,
    line_max: usize = 1 * 1024 * 1024,

    /// First NDJSON line is CursorRespBody (header). Subsequent lines
    /// are CursorEntry. The first line we see flips this true.
    have_header: bool = false,
    /// Captured from the header for debug logging.
    baton: ?[]const u8 = null,
    base_url: ?[]const u8 = null,

    entry_cb: *const fn (ctx: *anyopaque, entry: CursorEntry) anyerror!void,
    entry_ctx: *anyopaque,

    /// Set when a callback or parse fails. Stream is then drained-to-
    /// completion without further dispatch; streamCursor surfaces this
    /// error to the caller.
    deferred_error: ?anyerror = null,

    fn init(
        allocator: Allocator,
        entry_ctx: *anyopaque,
        entry_cb: *const fn (ctx: *anyopaque, entry: CursorEntry) anyerror!void,
    ) NdjsonCursorWriter {
        return .{
            .allocator = allocator,
            .writer = .{
                .vtable = &vtable,
                .buffer = &.{},
            },
            .entry_cb = entry_cb,
            .entry_ctx = entry_ctx,
        };
    }

    fn deinit(self: *NdjsonCursorWriter) void {
        self.line_buf.deinit(self.allocator);
    }

    const vtable: std.Io.Writer.VTable = .{
        .drain = drainFn,
        // Default flush, never-rebase: our buffer is always empty so
        // there's nothing to flush or rebase.
        .flush = std.Io.Writer.defaultFlush,
        .rebase = std.Io.Writer.failingRebase,
    };

    fn drainFn(
        w: *std.Io.Writer,
        data: []const []const u8,
        splat: usize,
    ) std.Io.Writer.Error!usize {
        const self: *NdjsonCursorWriter = @fieldParentPtr("writer", w);

        // If we previously latched an error, keep accepting bytes so
        // the HTTP layer can complete its read, but stop dispatching.
        if (self.deferred_error != null) {
            var total: usize = 0;
            for (data, 0..) |slice, i| {
                const reps = if (i + 1 == data.len) splat else 1;
                total += slice.len * reps;
            }
            return total;
        }

        var total: usize = 0;
        for (data, 0..) |slice, i| {
            const reps = if (i + 1 == data.len) splat else 1;
            for (0..reps) |_| {
                self.feed(slice) catch |err| {
                    self.deferred_error = err;
                    // Keep returning consumed bytes so the writer protocol
                    // is happy; streamCursor checks deferred_error after fetch.
                    return total + slice.len;
                };
            }
            total += slice.len * reps;
        }
        return total;
    }

    /// Walk `bytes`, dispatching every complete line ending in '\n'.
    /// Trailing partial line accumulates onto `line_buf` for the next call.
    fn feed(self: *NdjsonCursorWriter, bytes: []const u8) !void {
        var start: usize = 0;
        var i: usize = 0;
        while (i < bytes.len) : (i += 1) {
            if (bytes[i] != '\n') continue;

            const chunk = bytes[start..i];
            if (self.line_buf.items.len == 0) {
                // Whole line is in this slice — dispatch without copying.
                if (chunk.len > 0) try self.dispatchLine(chunk);
            } else {
                if (self.line_buf.items.len + chunk.len > self.line_max) return error.LineTooLong;
                try self.line_buf.appendSlice(self.allocator, chunk);
                try self.dispatchLine(self.line_buf.items);
                self.line_buf.clearRetainingCapacity();
            }
            start = i + 1;
        }
        // tail: incomplete line
        const tail = bytes[start..];
        if (tail.len > 0) {
            if (self.line_buf.items.len + tail.len > self.line_max) return error.LineTooLong;
            try self.line_buf.appendSlice(self.allocator, tail);
        }
    }

    fn dispatchLine(self: *NdjsonCursorWriter, line: []const u8) !void {
        if (line.len == 0) return;

        const parsed = json.parseFromSlice(json.Value, self.allocator, line, .{}) catch {
            log.err("ndjson parse failed: {s}", .{line[0..@min(line.len, 200)]});
            return error.NdjsonParseFailed;
        };
        defer parsed.deinit();

        if (!self.have_header) {
            // First line is CursorRespBody { baton, base_url }
            if (parsed.value == .object) {
                if (parsed.value.object.get("baton")) |v| if (v == .string) {
                    self.baton = v.string;
                };
                if (parsed.value.object.get("base_url")) |v| if (v == .string) {
                    self.base_url = v.string;
                    log.info("v3/cursor base_url: {s}", .{v.string});
                };
            }
            self.have_header = true;
            return;
        }

        // Subsequent lines are CursorEntry
        const entry = parseCursorEntry(parsed.value) catch |err| {
            log.err("malformed cursor entry: {s}", .{line[0..@min(line.len, 200)]});
            return err;
        };

        self.entry_cb(self.entry_ctx, entry) catch return error.CallbackError;
    }
};

fn parseCursorEntry(value: json.Value) !CursorEntry {
    if (value != .object) return error.MalformedCursorEntry;
    const obj = value.object;

    const type_v = obj.get("type") orelse return error.MalformedCursorEntry;
    if (type_v != .string) return error.MalformedCursorEntry;
    const t = type_v.string;

    if (mem.eql(u8, t, "step_begin")) {
        const step = obj.get("step") orelse return error.MalformedCursorEntry;
        const step_u: u32 = switch (step) {
            .integer => |n| if (n >= 0) @intCast(n) else return error.MalformedCursorEntry,
            else => return error.MalformedCursorEntry,
        };
        return .{ .step_begin = .{ .step = step_u } };
    }
    if (mem.eql(u8, t, "step_end")) {
        const affected = obj.get("affected_row_count") orelse json.Value{ .integer = 0 };
        const affected_u: u32 = switch (affected) {
            .integer => |n| if (n >= 0) @intCast(n) else 0,
            else => 0,
        };
        return .{ .step_end = .{ .affected_row_count = affected_u } };
    }
    if (mem.eql(u8, t, "row")) {
        const row_v = obj.get("row") orelse return error.MalformedCursorEntry;
        if (row_v != .array) return error.MalformedCursorEntry;
        return .{ .row = .{ .columns = row_v.array.items } };
    }
    if (mem.eql(u8, t, "step_error")) {
        const step = obj.get("step") orelse return error.MalformedCursorEntry;
        const step_u: u32 = switch (step) {
            .integer => |n| if (n >= 0) @intCast(n) else 0,
            else => 0,
        };
        const err_v = obj.get("error") orelse return error.MalformedCursorEntry;
        if (err_v != .object) return error.MalformedCursorEntry;
        const msg_v = err_v.object.get("message") orelse return error.MalformedCursorEntry;
        if (msg_v != .string) return error.MalformedCursorEntry;
        return .{ .step_error = .{ .step = step_u, .message = msg_v.string } };
    }
    if (mem.eql(u8, t, "error")) {
        const err_v = obj.get("error") orelse return error.MalformedCursorEntry;
        if (err_v != .object) return error.MalformedCursorEntry;
        const msg_v = err_v.object.get("message") orelse return error.MalformedCursorEntry;
        if (msg_v != .string) return error.MalformedCursorEntry;
        return .{ .err = .{ .message = msg_v.string } };
    }
    return error.MalformedCursorEntry;
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
