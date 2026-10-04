//! Requests std.http.Server cannot safely answer.
//!
//! A POST/PUT/PATCH with neither Content-Length nor Transfer-Encoding trips an
//! assert inside std.http.Server when a keep-alive response is sent before the
//! body has been read, which takes the whole process down. Every accept loop
//! calls `rejectUnframedBody` right after `receiveHead`, before any handler
//! can respond.

const std = @import("std");
const http = std.http;

/// Answers 411 and returns true when the request declares no body framing;
/// the caller must then drop the connection.
pub fn rejectUnframedBody(request: *http.Server.Request) bool {
    const head = &request.head;
    if (!head.method.requestHasBody()) return false;
    if (head.transfer_encoding != .none or head.content_length != null) return false;
    request.respond("", .{ .status = .length_required, .keep_alive = false }) catch {};
    return true;
}

const testing = std.testing;

test "bodiless POST gets 411 and a closed connection" {
    var out: [512]u8 = undefined;
    var in: std.Io.Reader = .fixed("POST /search HTTP/1.1\r\nHost: x\r\n\r\n");
    var w: std.Io.Writer = .fixed(&out);
    var server = http.Server.init(&in, &w);
    var request = try server.receiveHead();
    try testing.expect(rejectUnframedBody(&request));
    try testing.expect(std.mem.startsWith(u8, w.buffered(), "HTTP/1.1 411 "));
    try testing.expect(std.mem.indexOf(u8, w.buffered(), "connection: close") != null);
}

test "framed and bodiless-by-method requests pass through untouched" {
    var out: [512]u8 = undefined;
    inline for (.{
        "GET /search HTTP/1.1\r\nHost: x\r\n\r\n",
        "POST /admin/refresh HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
        "POST /admin/refresh HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n",
    }) |raw| {
        var in: std.Io.Reader = .fixed(raw);
        var w: std.Io.Writer = .fixed(&out);
        var server = http.Server.init(&in, &w);
        var request = try server.receiveHead();
        try testing.expect(!rejectUnframedBody(&request));
        try testing.expectEqual(0, w.buffered().len);
    }
}
