//! Starts `identity_stub.py` and routes a resolver's connections to it.
//!
//! The resolver keeps its real URLs (`https://plc.directory`, the handle's own
//! host on port 443), so its TLS names, connection-pool keys and network-safety
//! checks are the ones production uses. What changes is name lookup: the `Io`
//! from `Stub.io` answers every lookup, including the address literals the
//! handle resolver dials after its DNS preflight, with the stub's loopback
//! port.

const std = @import("std");
const zat = @import("zat");
const identity = @import("../identity.zig");
const Io = std.Io;
const HostName = Io.net.HostName;

pub const script_path = @import("identity_stub_options").script_path;

pub const Counts = struct { accepts: u64, handshakes: u64, requests: u64 };

var routed_io: Io = undefined;
var routed_port: u16 = 0;
var routed_vtable: Io.VTable = undefined;

fn lookup(_: ?*anyopaque, host: HostName, resolved: *Io.Queue(HostName.LookupResult), options: HostName.LookupOptions) HostName.LookupError!void {
    defer resolved.close(routed_io);
    resolved.putOneUncancelable(routed_io, .{ .address = .{ .ip4 = .loopback(routed_port) } }) catch unreachable;
    if (options.canonical_name_buffer) |buffer| {
        @memcpy(buffer[0..host.bytes.len], host.bytes);
        resolved.putOneUncancelable(routed_io, .{ .canonical_name = .{ .bytes = buffer[0..host.bytes.len] } }) catch unreachable;
    }
}

pub const Stub = struct {
    real_io: Io,
    child: std.process.Child,
    port: u16,
    trust_path_buf: [512]u8 = undefined,
    trust_path_len: usize = 0,
    control: zat.HttpTransport,

    /// One stub per process: the lookup route is process-wide.
    pub fn start(self: *Stub, real_io: Io, gpa: std.mem.Allocator) !void {
        var child = try std.process.spawn(real_io, .{
            .argv = &.{ "python3", script_path },
            .stdin = .pipe,
            .stdout = .pipe,
        });
        errdefer child.kill(real_io);
        var line_buf: [1024]u8 = undefined;
        var reader = child.stdout.?.readerStreaming(real_io, &line_buf);
        const line = try reader.interface.takeDelimiterExclusive('\n');
        var words = std.mem.tokenizeScalar(u8, line, ' ');
        if (!std.mem.eql(u8, words.next() orelse "", "READY")) return error.StubNotReady;
        const port = try std.fmt.parseInt(u16, words.next() orelse return error.StubNotReady, 10);
        const trust_path = words.next() orelse return error.StubNotReady;

        self.* = .{ .real_io = real_io, .child = child, .port = port, .control = undefined };
        @memcpy(self.trust_path_buf[0..trust_path.len], trust_path);
        self.trust_path_len = trust_path.len;

        routed_io = real_io;
        routed_port = port;
        routed_vtable = real_io.vtable.*;
        routed_vtable.netLookup = lookup;

        self.control = zat.HttpTransport.init(self.io(), gpa);
        const now = Io.Clock.real.now(real_io);
        try self.control.http_client.ca_bundle.addCertsFromFilePathAbsolute(gpa, real_io, now, self.trustPath());
        self.control.http_client.now = now;
    }

    pub fn deinit(self: *Stub) void {
        self.control.deinit();
        self.child.stdin.?.close(self.real_io);
        self.child.stdin = null;
        _ = self.child.wait(self.real_io) catch {};
    }

    pub fn io(self: *const Stub) Io {
        return .{ .userdata = self.real_io.userdata, .vtable = &routed_vtable };
    }

    pub fn trustPath(self: *const Stub) []const u8 {
        return self.trust_path_buf[0..self.trust_path_len];
    }

    pub fn upstreams(self: *const Stub) identity.Upstreams {
        return .{ .extra_trust_path = self.trustPath() };
    }

    /// Totals since the stub started. The control connection is kept alive,
    /// so it adds one connection on the first call and none after.
    pub fn counts(self: *Stub) !Counts {
        const result = try self.control.fetch(.{ .url = "https://plc.directory/__stats" });
        defer self.control.allocator.free(result.body);
        const parsed = try std.json.parseFromSlice(Counts, self.control.allocator, result.body, .{});
        defer parsed.deinit();
        return parsed.value;
    }

    pub const CloseHow = enum { notify, abrupt };

    /// Have the stub close every connection that is idle, as a server whose
    /// keep-alive timeout ran out would.
    pub fn closeIdle(self: *Stub, how: CloseHow) !void {
        var url_buf: [96]u8 = undefined;
        const url = try std.fmt.bufPrint(&url_buf, "https://plc.directory/__close_idle?how={s}", .{@tagName(how)});
        const result = try self.control.fetch(.{ .url = url });
        self.control.allocator.free(result.body);
    }
};

/// `did:plc:` followed by the stub's four-letter behaviour and `n` in base 32.
pub fn did(kind: *const [4]u8, n: u64) identity.Did {
    const alphabet = "abcdefghijklmnopqrstuvwxyz234567";
    var text: [32]u8 = undefined;
    @memcpy(text[0..8], "did:plc:");
    @memcpy(text[8..12], kind);
    var rest = n;
    for (0..20) |i| {
        text[31 - i] = alphabet[@intCast(rest % 32)];
        rest /= 32;
    }
    return identity.Did.fromSlice(&text).?;
}
