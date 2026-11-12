//! Key layout of the actor store. Every range has a one-byte prefix, so a
//! range scan is `[prefix, prefix + 1)` and ranges never interleave.
//!
//! | prefix | key after the prefix                              | value                |
//! |--------|---------------------------------------------------|----------------------|
//! | `a`    | DID                                               | actor record         |
//! | `c`    | change sequence, u64 big-endian                   | DID                  |
//! | `h`    | ASCII-lowercased handle, 0x00, DID                | empty                |
//! | `i`    | due time, u64 big-endian, DID                     | identity queue state |
//! | `l`    | DID, 0x00, label source, 0x00, value, 0x00, subject | label fields       |
//! | `p`    | `d` + domain, `o` + DID, or `s` + domain          | policy entry; empty for `s` |
//! | `q`    | inverted score bits, u32 big-endian, DID          | empty                |
//! | `r`    | due time, u64 big-endian, DID                     | empty                |
//! | `t`    | deletion time, u64 big-endian, DID                | empty                |
//! | `z`    | cursor name                                       | position             |
//!
//! Times are unix seconds. The actor record, queue state, label and policy
//! encodings are in `record.zig`. `h`, `q`, `r`, `t` and `i` are derived from
//! the actor record and exist exactly when the record implies them:
//!
//! - `h` for a live actor with a handle;
//! - `q` for a live, visible actor with a handle and a positive score. The
//!   score's IEEE-754 bits are inverted, so higher scores sort first and ties
//!   sort by DID;
//! - `r` and `i` for a live actor whose record carries that due time;
//! - `t` for an actor that is deleted or deactivated.
//!
//! A `p` `s` key marks a domain whose block changed and whose sweep of the
//! actors already stored has not finished; see `Store.sweepDomain`.
//!
//! The label subject is one byte, `a` for the account and `p` for its profile
//! record: the labeler keys a label by subject URI, and the same value can sit
//! on both. DIDs, handles, label sources, label values and domains cannot
//! contain 0x00, which is what makes the separators unambiguous.

const std = @import("std");

pub const Prefix = enum(u8) {
    actor = 'a',
    change = 'c',
    handle = 'h',
    identity = 'i',
    label = 'l',
    policy = 'p',
    quality = 'q',
    refresh = 'r',
    tombstone = 't',
    cursor = 'z',

    pub fn byte(self: Prefix) u8 {
        return @backingInt(self);
    }

    /// The half-open byte range holding every key of this prefix.
    pub fn bounds(self: Prefix) [2][1]u8 {
        return .{ .{self.byte()}, .{self.byte() + 1} };
    }
};

pub const prefixes = std.enums.values(Prefix);

pub const max_did = 2048;
pub const max_handle = 253;
pub const max_domain = 253;
pub const max_label_source = 2048;
pub const max_label_value = 128;

pub const max_key = 1 + max_did + 1 + max_label_source + 1 + max_label_value + 2;
pub const Buf = [max_key]u8;

pub const Error = error{InvalidKeyPart};

pub const Subject = enum(u8) { account = 'a', profile = 'p' };
pub const Cursor = enum { relay, labeler, plc, bridge, next_change, change_floor, format, walk_quality, walk_rest, handle_repair, control_policy, control_requests, control_generation };

fn checked(part: []const u8, max: usize) Error![]const u8 {
    if (part.len == 0 or part.len > max or std.mem.indexOfScalar(u8, part, 0) != null) return error.InvalidKeyPart;
    return part;
}

pub fn validDid(did: []const u8) Error!void {
    _ = try checked(did, max_did);
}

const Writer = struct {
    buf: *Buf,
    len: usize = 0,

    fn byte(self: *Writer, b: u8) void {
        self.buf[self.len] = b;
        self.len += 1;
    }

    fn bytes(self: *Writer, part: []const u8) void {
        @memcpy(self.buf[self.len..][0..part.len], part);
        self.len += part.len;
    }

    fn lower(self: *Writer, part: []const u8) void {
        for (part, self.buf[self.len..][0..part.len]) |c, *out| out.* = std.ascii.toLower(c);
        self.len += part.len;
    }

    fn int(self: *Writer, comptime T: type, value: T) void {
        std.mem.writeInt(T, self.buf[self.len..][0..@sizeOf(T)], value, .big);
        self.len += @sizeOf(T);
    }

    fn done(self: *const Writer) []const u8 {
        return self.buf[0..self.len];
    }
};

fn start(buf: *Buf, prefix: Prefix) Writer {
    var w: Writer = .{ .buf = buf };
    w.byte(prefix.byte());
    return w;
}

pub fn actor(buf: *Buf, did: []const u8) Error![]const u8 {
    var w = start(buf, .actor);
    w.bytes(try checked(did, max_did));
    return w.done();
}

pub fn change(buf: *Buf, seq: u64) []const u8 {
    var w = start(buf, .change);
    w.int(u64, seq);
    return w.done();
}

/// The prefix shared by every DID holding `handle`, compared without case.
pub fn handlePrefix(buf: *Buf, name: []const u8) Error![]const u8 {
    var w = start(buf, .handle);
    w.lower(try checked(name, max_handle));
    w.byte(0);
    return w.done();
}

pub fn handle(buf: *Buf, name: []const u8, did: []const u8) Error![]const u8 {
    var w = start(buf, .handle);
    w.lower(try checked(name, max_handle));
    w.byte(0);
    w.bytes(try checked(did, max_did));
    return w.done();
}

/// Positive finite scores only: their bit patterns order as the values do.
pub fn scoreBits(score: f32) u32 {
    std.debug.assert(score > 0 and std.math.isFinite(score));
    return ~@as(u32, @bitCast(score));
}

pub fn quality(buf: *Buf, score: f32, did: []const u8) Error![]const u8 {
    var w = start(buf, .quality);
    w.int(u32, scoreBits(score));
    w.bytes(try checked(did, max_did));
    return w.done();
}

pub fn timed(buf: *Buf, prefix: Prefix, at: u64, did: []const u8) Error![]const u8 {
    std.debug.assert(prefix == .identity or prefix == .refresh or prefix == .tombstone);
    var w = start(buf, prefix);
    w.int(u64, at);
    w.bytes(try checked(did, max_did));
    return w.done();
}

/// The first key of a timed range at or after `at`; with `at + 1` it is the
/// exclusive end of everything due by `at`.
pub fn timedBound(buf: *Buf, prefix: Prefix, at: u64) []const u8 {
    var w = start(buf, prefix);
    w.int(u64, at);
    return w.done();
}

pub fn labelPrefix(buf: *Buf, did: []const u8) Error![]const u8 {
    var w = start(buf, .label);
    w.bytes(try checked(did, max_did));
    w.byte(0);
    return w.done();
}

pub fn label(buf: *Buf, did: []const u8, source: []const u8, value: []const u8, subject: Subject) Error![]const u8 {
    var w = start(buf, .label);
    w.bytes(try checked(did, max_did));
    w.byte(0);
    w.bytes(try checked(source, max_label_source));
    w.byte(0);
    w.bytes(try checked(value, max_label_value));
    w.byte(0);
    w.byte(@backingInt(subject));
    return w.done();
}

pub fn override(buf: *Buf, did: []const u8) Error![]const u8 {
    var w = start(buf, .policy);
    w.byte('o');
    w.bytes(try checked(did, max_did));
    return w.done();
}

pub fn domain(buf: *Buf, name: []const u8) Error![]const u8 {
    var w = start(buf, .policy);
    w.byte('d');
    w.lower(try checked(name, max_domain));
    return w.done();
}

pub fn owedSweep(buf: *Buf, name: []const u8) Error![]const u8 {
    var w = start(buf, .policy);
    w.byte('s');
    w.lower(try checked(name, max_domain));
    return w.done();
}

pub const domain_bounds = [2][2]u8{ .{ Prefix.policy.byte(), 'd' }, .{ Prefix.policy.byte(), 'd' + 1 } };
pub const override_bounds = [2][2]u8{ .{ Prefix.policy.byte(), 'o' }, .{ Prefix.policy.byte(), 'o' + 1 } };
pub const owed_sweep_bounds = [2][2]u8{ .{ Prefix.policy.byte(), 's' }, .{ Prefix.policy.byte(), 's' + 1 } };

pub fn cursor(buf: *Buf, which: Cursor) []const u8 {
    var w = start(buf, .cursor);
    w.bytes(@tagName(which));
    return w.done();
}

pub const Timed = struct { at: u64, did: []const u8 };
pub const Quality = struct { score: f32, did: []const u8 };
pub const Handle = struct { handle: []const u8, did: []const u8 };
pub const Label = struct { did: []const u8, source: []const u8, value: []const u8, subject: Subject };

pub const ParseError = error{CorruptKey};

fn body(key: []const u8, prefix: Prefix, min: usize) ParseError![]const u8 {
    if (key.len < 1 + min or key[0] != prefix.byte()) return error.CorruptKey;
    return key[1..];
}

pub fn parseActor(key: []const u8) ParseError![]const u8 {
    return body(key, .actor, 1);
}

pub fn parseChange(key: []const u8) ParseError!u64 {
    const rest = try body(key, .change, 8);
    if (rest.len != 8) return error.CorruptKey;
    return std.mem.readInt(u64, rest[0..8], .big);
}

pub fn parseHandle(key: []const u8) ParseError!Handle {
    const rest = try body(key, .handle, 3);
    const at = std.mem.indexOfScalar(u8, rest, 0) orelse return error.CorruptKey;
    if (at == 0 or at + 1 == rest.len) return error.CorruptKey;
    return .{ .handle = rest[0..at], .did = rest[at + 1 ..] };
}

pub fn parseQuality(key: []const u8) ParseError!Quality {
    const rest = try body(key, .quality, 5);
    return .{ .score = @bitCast(~std.mem.readInt(u32, rest[0..4], .big)), .did = rest[4..] };
}

pub fn parseTimed(key: []const u8, prefix: Prefix) ParseError!Timed {
    const rest = try body(key, prefix, 9);
    return .{ .at = std.mem.readInt(u64, rest[0..8], .big), .did = rest[8..] };
}

pub fn parseLabel(key: []const u8) ParseError!Label {
    const rest = try body(key, .label, 7);
    var parts = std.mem.splitScalar(u8, rest, 0);
    const did = parts.next() orelse return error.CorruptKey;
    const source = parts.next() orelse return error.CorruptKey;
    const value = parts.next() orelse return error.CorruptKey;
    const subject = parts.next() orelse return error.CorruptKey;
    if (parts.next() != null or did.len == 0 or source.len == 0 or value.len == 0 or subject.len != 1) return error.CorruptKey;
    return .{
        .did = did,
        .source = source,
        .value = value,
        .subject = std.enums.fromInt(Subject, subject[0]) orelse return error.CorruptKey,
    };
}

pub fn parseDomain(key: []const u8) ParseError![]const u8 {
    if (key.len < 3 or !std.mem.startsWith(u8, key, &domain_bounds[0])) return error.CorruptKey;
    return key[2..];
}

pub fn parseOverride(key: []const u8) ParseError![]const u8 {
    if (key.len < 3 or !std.mem.startsWith(u8, key, &override_bounds[0])) return error.CorruptKey;
    return key[2..];
}

pub fn parseOwedSweep(key: []const u8) ParseError![]const u8 {
    if (key.len < 3 or !std.mem.startsWith(u8, key, &owed_sweep_bounds[0])) return error.CorruptKey;
    return key[2..];
}

const testing = std.testing;

fn less(a: []const u8, b: []const u8) bool {
    return std.mem.order(u8, a, b) == .lt;
}

test "every range sits inside its own one-byte prefix" {
    var buf: Buf = undefined;
    const did = "did:plc:abcdefghijklmnopqrstuvwx";
    const samples = [_]struct { Prefix, []const u8 }{
        .{ .actor, try testing.allocator.dupe(u8, try actor(&buf, did)) },
        .{ .change, try testing.allocator.dupe(u8, change(&buf, std.math.maxInt(u64))) },
        .{ .handle, try testing.allocator.dupe(u8, try handle(&buf, "\xff\xff.test", did)) },
        .{ .identity, try testing.allocator.dupe(u8, try timed(&buf, .identity, std.math.maxInt(u64), did)) },
        .{ .label, try testing.allocator.dupe(u8, try label(&buf, did, "did:plc:mod", "!hide", .profile)) },
        .{ .policy, try testing.allocator.dupe(u8, try override(&buf, did)) },
        .{ .policy, try testing.allocator.dupe(u8, try domain(&buf, "\xff.test")) },
        .{ .quality, try testing.allocator.dupe(u8, try quality(&buf, std.math.floatMin(f32), did)) },
        .{ .refresh, try testing.allocator.dupe(u8, try timed(&buf, .refresh, std.math.maxInt(u64), did)) },
        .{ .tombstone, try testing.allocator.dupe(u8, try timed(&buf, .tombstone, std.math.maxInt(u64), did)) },
        .{ .cursor, try testing.allocator.dupe(u8, cursor(&buf, .relay)) },
    };
    defer for (samples) |sample| testing.allocator.free(sample[1]);
    for (samples) |sample| {
        const bounds = sample[0].bounds();
        try testing.expect(!less(sample[1], &bounds[0]));
        try testing.expect(less(sample[1], &bounds[1]));
    }
    var seen: [256]bool = @splat(false);
    for (prefixes) |prefix| {
        try testing.expect(!seen[prefix.byte()]);
        seen[prefix.byte()] = true;
    }
}

test "actor keys round trip and order by DID" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    try testing.expectEqualStrings("did:web:example.com", try parseActor(try actor(&a, "did:web:example.com")));
    try testing.expect(less(try actor(&a, "did:plc:aaaa"), try actor(&b, "did:plc:aaab")));
    try testing.expectError(error.InvalidKeyPart, actor(&a, ""));
    try testing.expectError(error.InvalidKeyPart, actor(&a, "did:plc:a\x00b"));
    try testing.expectError(error.CorruptKey, parseActor("a"));
    try testing.expectError(error.CorruptKey, parseActor("hdid:plc:aaaa"));
}

test "change keys round trip and order by sequence" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    const seqs = [_]u64{ 0, 1, 255, 256, 65535, 65536, 1 << 32, (1 << 56) - 1, 1 << 56, std.math.maxInt(u64) };
    for (seqs) |seq| try testing.expectEqual(seq, try parseChange(change(&a, seq)));
    for (seqs[0 .. seqs.len - 1], seqs[1..]) |low, high| try testing.expect(less(change(&a, low), change(&b, high)));
    try testing.expectError(error.CorruptKey, parseChange("c1234567"));
}

test "handle keys compare without case and keep one handle's DIDs together" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    try testing.expectEqualStrings(try handle(&a, "Alice.Example.COM", "did:plc:one"), try handle(&b, "alice.example.com", "did:plc:one"));
    const parsed = try parseHandle(try handle(&a, "Alice.Test", "did:plc:one"));
    try testing.expectEqualStrings("alice.test", parsed.handle);
    try testing.expectEqualStrings("did:plc:one", parsed.did);

    const prefix = try testing.allocator.dupe(u8, try handlePrefix(&a, "ALICE.test"));
    defer testing.allocator.free(prefix);
    try testing.expect(std.mem.startsWith(u8, try handle(&b, "alice.TEST", "did:plc:one"), prefix));
    try testing.expect(std.mem.startsWith(u8, try handle(&b, "alice.test", "did:plc:two"), prefix));
    try testing.expect(!std.mem.startsWith(u8, try handle(&b, "alice.tests", "did:plc:one"), prefix));
    try testing.expect(!std.mem.startsWith(u8, try handle(&b, "alice.tes", "did:plc:one"), prefix));
    try testing.expect(less(try handle(&a, "alice.test", "did:plc:one"), try handle(&b, "ALICE.test", "did:plc:two")));
    try testing.expect(less(try handle(&a, "alice.test", "did:plc:zzz"), try handle(&b, "alice.test.example", "did:plc:aaa")));
    try testing.expectError(error.InvalidKeyPart, handle(&a, "", "did:plc:one"));
    try testing.expectError(error.CorruptKey, parseHandle("halice.test"));
}

test "quality keys sort higher scores first, then by DID" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    const scores = [_]f32{ std.math.floatMax(f32), 98765.5, 1000.01, 1000.0, 100.25, 1.5, 1.0, 0.001, 1e-30, std.math.floatMin(f32), std.math.floatTrueMin(f32) };
    for (scores[0 .. scores.len - 1], scores[1..]) |high, low| {
        try testing.expect(less(try quality(&a, high, "did:plc:zzzz"), try quality(&b, low, "did:plc:aaaa")));
    }
    try testing.expect(less(try quality(&a, 7.0, "did:plc:aaaa"), try quality(&b, 7.0, "did:plc:bbbb")));
    for (scores) |score| {
        const parsed = try parseQuality(try quality(&a, score, "did:plc:abc"));
        try testing.expectEqual(score, parsed.score);
        try testing.expectEqualStrings("did:plc:abc", parsed.did);
    }
}

test "timed keys round trip and order by time, then DID" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    const times = [_]u64{ 0, 1, 255, 256, 1_793_901_791, 1_793_901_792, 1 << 40, std.math.maxInt(u64) };
    for ([_]Prefix{ .identity, .refresh, .tombstone }) |prefix| {
        for (times[0 .. times.len - 1], times[1..]) |low, high| {
            try testing.expect(less(try timed(&a, prefix, low, "did:plc:zzzz"), try timed(&b, prefix, high, "did:plc:aaaa")));
            try testing.expect(less(try timed(&a, prefix, low, "did:plc:zzzz"), timedBound(&b, prefix, low + 1)));
            try testing.expect(!less(try timed(&a, prefix, low, "did:plc:aaaa"), timedBound(&b, prefix, low)));
        }
        try testing.expect(less(try timed(&a, prefix, 9, "did:plc:aaaa"), try timed(&b, prefix, 9, "did:plc:bbbb")));
        for (times) |at| {
            const parsed = try parseTimed(try timed(&a, prefix, at, "did:plc:abc"), prefix);
            try testing.expectEqual(at, parsed.at);
            try testing.expectEqualStrings("did:plc:abc", parsed.did);
        }
    }
    try testing.expectError(error.CorruptKey, parseTimed(try timed(&a, .refresh, 1, "did:plc:abc"), .identity));
}

test "label keys round trip and group by DID, source, value, subject" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    const parsed = try parseLabel(try label(&a, "did:plc:abc", "did:plc:mod", "!takedown", .profile));
    try testing.expectEqualStrings("did:plc:abc", parsed.did);
    try testing.expectEqualStrings("did:plc:mod", parsed.source);
    try testing.expectEqualStrings("!takedown", parsed.value);
    try testing.expectEqual(Subject.profile, parsed.subject);

    const prefix = try testing.allocator.dupe(u8, try labelPrefix(&a, "did:plc:abc"));
    defer testing.allocator.free(prefix);
    try testing.expect(std.mem.startsWith(u8, try label(&b, "did:plc:abc", "did:plc:mod", "spam", .account), prefix));
    try testing.expect(!std.mem.startsWith(u8, try label(&b, "did:plc:abcd", "did:plc:mod", "spam", .account), prefix));
    try testing.expect(less(try label(&a, "did:plc:abc", "did:plc:mod", "spam", .account), try label(&b, "did:plc:abc", "did:plc:mod", "spam", .profile)));
    try testing.expect(less(try label(&a, "did:plc:abc", "did:plc:mod", "spam", .profile), try label(&b, "did:plc:abc", "did:plc:mod", "spammy", .account)));
    try testing.expect(less(try label(&a, "did:plc:abc", "did:plc:mod", "z", .profile), try label(&b, "did:plc:abc", "did:plc:mode", "a", .account)));
    try testing.expectError(error.InvalidKeyPart, label(&a, "did:plc:abc", "did:plc:mod", "sp\x00am", .account));
    try testing.expectError(error.CorruptKey, parseLabel("ldid:plc:abc\x00src\x00val"));
}

test "policy keys split into domains and overrides" {
    var a: Buf = undefined;
    var b: Buf = undefined;
    try testing.expectEqualStrings("spam.example", try parseDomain(try domain(&a, "Spam.Example")));
    try testing.expectEqualStrings("did:plc:abc", try parseOverride(try override(&a, "did:plc:abc")));
    try testing.expect(less(try domain(&a, "zzz.example"), try override(&b, "did:plc:aaa")));
    try testing.expect(less(try domain(&a, "a.example"), try domain(&b, "b.example")));
    try testing.expect(less(try domain(&a, "\xff"), &domain_bounds[1]));
    try testing.expect(less(try override(&a, "did:plc:\xff"), &override_bounds[1]));
    try testing.expectError(error.CorruptKey, parseDomain(try override(&a, "did:plc:abc")));
    try testing.expectEqualStrings("spam.example", try parseOwedSweep(try owedSweep(&a, "Spam.Example")));
    try testing.expect(less(try override(&a, "did:plc:\xff"), try owedSweep(&b, "a.example")));
    try testing.expect(less(try owedSweep(&a, "\xff"), &owed_sweep_bounds[1]));
    try testing.expectError(error.CorruptKey, parseOwedSweep(try domain(&a, "spam.example")));
    try testing.expectError(error.CorruptKey, parseDomain(try owedSweep(&a, "spam.example")));
}

test "cursor keys are distinct" {
    var bufs: [std.enums.values(Cursor).len]Buf = undefined;
    for (std.enums.values(Cursor), &bufs, 0..) |which, *buf, i| {
        const key = cursor(buf, which);
        for (std.enums.values(Cursor)[0..i], bufs[0..i]) |other, *other_buf| {
            try testing.expect(!std.mem.eql(u8, key, cursor(other_buf, other)));
        }
    }
}
