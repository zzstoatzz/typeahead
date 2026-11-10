//! The one rule for an actor's stored `hidden` flag. Precedence, later wins:
//! a hiding label from the moderation service, a flagged handle, a blocked
//! domain, then the per-DID override.

const std = @import("std");
const record = @import("record.zig");
const time = @import("time.zig");

pub const bsky_moderation = "did:plc:ar7c4by46qjdydhdevvrndac";
pub const hiding_values = [_][]const u8{ "!hide", "!takedown", "!suspend", "spam" };

pub const Domains = std.StringHashMapUnmanaged(void);

/// The stored flag is as of `now`: a label that expires later does not unhide
/// its actor until something recomputes it.
pub fn labelsHide(labels: []const record.Label, source: []const u8, now: u64) bool {
    for (labels) |label| {
        if (label.negated or !std.mem.eql(u8, label.source, source)) continue;
        if (label.expires.len > 0) {
            if (time.parseMs(label.expires)) |at| if (at <= @as(i128, now) * 1000) continue;
        }
        for (hiding_values) |value| if (std.mem.eql(u8, label.value, value)) return true;
    }
    return false;
}

/// True when the handle is a blocked domain or sits under one. `domains`
/// holds lowercase names.
pub fn domainBlocked(handle: []const u8, domains: *const Domains) bool {
    if (handle.len == 0 or handle.len > 253 or domains.count() == 0) return false;
    var buf: [253]u8 = undefined;
    const lower = std.ascii.lowerString(&buf, handle);
    var at: usize = 0;
    while (true) {
        if (domains.contains(lower[at..])) return true;
        at = 1 + (std.mem.indexOfScalarPos(u8, lower, at, '.') orelse return false);
    }
}

pub fn matchesDomain(handle: []const u8, domain: []const u8) bool {
    if (handle.len < domain.len) return false;
    const tail = handle[handle.len - domain.len ..];
    if (!std.ascii.eqlIgnoreCase(tail, domain)) return false;
    return handle.len == domain.len or handle[handle.len - domain.len - 1] == '.';
}

pub const Inputs = struct {
    labels: []const record.Label,
    handle: []const u8,
    override: record.Override,
    handle_flagged: bool,
    domain_blocked: bool,
    source: []const u8,
    now: u64,
};

pub fn resolveHidden(in: Inputs) bool {
    return switch (in.override) {
        .show => false,
        .hide => true,
        .none => labelsHide(in.labels, in.source, in.now) or in.handle_flagged or in.domain_blocked,
    };
}

const testing = std.testing;

fn sample(value: []const u8, source: []const u8) record.Label {
    return .{ .source = source, .uri = "did:plc:abc", .value = value, .created = "2026-01-01T00:00:00Z" };
}

test "only live labels from the moderation service hide" {
    const now = 1_794_355_200;
    for (hiding_values) |value| try testing.expect(labelsHide(&.{sample(value, bsky_moderation)}, bsky_moderation, now));
    try testing.expect(!labelsHide(&.{sample("!no-unauthenticated", bsky_moderation)}, bsky_moderation, now));
    try testing.expect(!labelsHide(&.{sample("spam", "did:plc:someoneelse")}, bsky_moderation, now));
    try testing.expect(!labelsHide(&.{}, bsky_moderation, now));

    var negated = sample("spam", bsky_moderation);
    negated.negated = true;
    try testing.expect(!labelsHide(&.{negated}, bsky_moderation, now));

    var expiring = sample("!hide", bsky_moderation);
    expiring.expires = "2026-11-11T00:00:00Z";
    try testing.expect(!labelsHide(&.{expiring}, bsky_moderation, now));
    try testing.expect(labelsHide(&.{expiring}, bsky_moderation, now - 1));
    expiring.expires = "not a date";
    try testing.expect(labelsHide(&.{expiring}, bsky_moderation, now));
    try testing.expect(labelsHide(&.{ negated, sample("x", bsky_moderation), sample("!suspend", bsky_moderation) }, bsky_moderation, now));
}

test "a domain block covers the apex and everything under it" {
    var domains: Domains = .empty;
    defer domains.deinit(testing.allocator);
    try testing.expect(!domainBlocked("spam.example", &domains));
    try domains.put(testing.allocator, "spam.example", {});
    try testing.expect(domainBlocked("spam.example", &domains));
    try testing.expect(domainBlocked("a.Spam.Example", &domains));
    try testing.expect(domainBlocked("a.b.spam.example", &domains));
    try testing.expect(!domainBlocked("notspam.example", &domains));
    try testing.expect(!domainBlocked("spam.example.org", &domains));
    try testing.expect(!domainBlocked("example", &domains));
    try testing.expect(!domainBlocked("", &domains));

    try testing.expect(matchesDomain("spam.example", "spam.example"));
    try testing.expect(matchesDomain("A.SPAM.example", "spam.example"));
    try testing.expect(!matchesDomain("notspam.example", "spam.example"));
    try testing.expect(!matchesDomain("example", "spam.example"));
}

test "the override is the final word" {
    const hiding = [_]record.Label{sample("!takedown", bsky_moderation)};
    const base: Inputs = .{ .labels = &.{}, .handle = "a.test", .override = .none, .handle_flagged = false, .domain_blocked = false, .source = bsky_moderation, .now = 0 };
    try testing.expect(!resolveHidden(base));
    var in = base;
    in.labels = &hiding;
    try testing.expect(resolveHidden(in));
    in.override = .show;
    try testing.expect(!resolveHidden(in));
    in = base;
    in.handle_flagged = true;
    try testing.expect(resolveHidden(in));
    in = base;
    in.domain_blocked = true;
    try testing.expect(resolveHidden(in));
    in.override = .show;
    try testing.expect(!resolveHidden(in));
    in = base;
    in.override = .hide;
    try testing.expect(resolveHidden(in));
}
