//! Read-only Turso HTTP API client for sync
//! Uses hrana v2 pipeline protocol

const std = @import("std");
const http = std.http;
const json = std.json;
const mem = std.mem;
const Allocator = mem.Allocator;

const log = std.log.scoped(.turso);

const TursoClient = @This();

const Value = struct { type: []const u8 = "text", value: []const u8 };
const Stmt = struct { sql: []const u8, args: ?[]const Value = null };
const ExecuteReq = struct { type: []const u8 = "execute", stmt: Stmt };
const CloseReq = struct { type: []const u8 = "close" };

allocator: Allocator,
url: []const u8, // host only (no protocol prefix)
token: []const u8,
http_client: http.Client,
mutex: std.Thread.Mutex = .{},

pub fn init(allocator: Allocator) !TursoClient {
    const url = std.posix.getenv("TURSO_URL") orelse {
        log.err("TURSO_URL not set", .{});
        return error.MissingEnv;
    };
    const token = std.posix.getenv("TURSO_AUTH_TOKEN") orelse {
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
        .http_client = .{ .allocator = allocator },
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

fn executeRaw(self: *TursoClient, sql: []const u8, args: []const []const u8) ![]const u8 {
    self.mutex.lock();
    defer self.mutex.unlock();

    var url_buf: [512]u8 = undefined;
    const url = std.fmt.bufPrint(&url_buf, "https://{s}/v2/pipeline", .{self.url}) catch
        return error.UrlTooLong;

    const body = try self.buildRequestBody(sql, args);
    defer self.allocator.free(body);

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

fn parseResult(allocator: Allocator, response: []const u8) !Result {
    const parsed = json.parseFromSlice(json.Value, allocator, response, .{}) catch {
        return .{ .allocator = allocator, .parsed = null, .rows = &.{} };
    };

    const json_rows = getRowsFromParsed(parsed.value) orelse {
        return .{ .allocator = allocator, .parsed = parsed, .rows = &.{} };
    };

    var rows: std.ArrayList(Row) = .{};
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

fn extractInt(val: json.Value) i64 {
    return switch (val) {
        .integer => |i| i,
        .object => |obj| {
            const v = obj.get("value") orelse return 0;
            return switch (v) {
                .integer => |i| i,
                .string => |s| std.fmt.parseInt(i64, s, 10) catch 0,
                else => 0,
            };
        },
        else => 0,
    };
}
