//! Which handle a write may carry into the store. A port of
//! `src/identity-policy.ts`: the quarantine list is the Worker's own file,
//! `src/identity-quarantine.json`, read at build time.

const std = @import("std");

pub const invalid_handle = "handle.invalid";

const quarantined = parseStrings(@embedFile("identity-quarantine"));

/// The JSON file is an array of plain strings. Anything else, an escape
/// included, fails the build rather than admitting a handle by accident.
fn parseStrings(comptime text: []const u8) []const []const u8 {
    @setEvalBranchQuota(100 * text.len + 1000);
    var out: []const []const u8 = &.{};
    var at: usize = 0;
    var opened = false;
    var closed = false;
    var expect_value = false;
    while (at < text.len) : (at += 1) {
        const c = text[at];
        if (std.ascii.isWhitespace(c)) continue;
        if (closed) @compileError("identity quarantine: text after the array");
        if (!opened) {
            if (c != '[') @compileError("identity quarantine: not an array");
            opened = true;
            expect_value = true;
            continue;
        }
        switch (c) {
            ']' => {
                if (expect_value and out.len > 0) @compileError("identity quarantine: trailing comma");
                closed = true;
            },
            ',' => {
                if (expect_value) @compileError("identity quarantine: empty element");
                expect_value = true;
            },
            '"' => {
                if (!expect_value) @compileError("identity quarantine: missing comma");
                const end = std.mem.indexOfScalarPos(u8, text, at + 1, '"') orelse @compileError("identity quarantine: unterminated string");
                const value = text[at + 1 .. end];
                if (std.mem.indexOfScalar(u8, value, '\\') != null) @compileError("identity quarantine: escapes are not supported");
                out = out ++ [_][]const u8{value};
                at = end;
                expect_value = false;
            },
            else => @compileError("identity quarantine: only strings are supported"),
        }
    }
    if (!closed) @compileError("identity quarantine: unterminated array");
    return out;
}

/// `isSearchExcludedHandle`: the placeholder for a handle that does not
/// resolve, or a handle on the quarantine list, compared in lower case.
pub fn searchExcluded(handle: []const u8) bool {
    if (handle.len > 253) return false;
    var buf: [253]u8 = undefined;
    const lower = std.ascii.lowerString(&buf, handle);
    if (std.mem.eql(u8, lower, invalid_handle)) return true;
    for (quarantined) |entry| if (std.mem.eql(u8, lower, entry)) return true;
    return false;
}

/// `admittedHandle`: the handle to write, or "" to keep the stored one. The
/// explicit `handle.invalid` is always written; any other handle needs proof
/// that it resolves back to the account, and must not be quarantined.
pub fn admitted(handle: []const u8, verified: bool) []const u8 {
    if (handle.len == 0) return "";
    if (std.mem.eql(u8, handle, invalid_handle)) return handle;
    return if (verified and !searchExcluded(handle)) handle else "";
}

const testing = std.testing;

test "ingest requires verification evidence and accepts explicit invalidation" {
    try testing.expectEqualStrings("", admitted("spoof.test", false));
    try testing.expectEqualStrings("verified.test", admitted("verified.test", true));
    try testing.expectEqualStrings("", admitted("donald.trump.com", true));
    try testing.expectEqualStrings("handle.invalid", admitted("handle.invalid", false));
}

test "an absent handle keeps the stored one" {
    try testing.expectEqualStrings("", admitted("", true));
    try testing.expectEqualStrings("", admitted("", false));
}

test "quarantine and the invalid placeholder are matched without case" {
    try testing.expect(searchExcluded("Donald.Trump.com"));
    try testing.expect(searchExcluded("HANDLE.INVALID"));
    try testing.expect(!searchExcluded("alice.example"));
    try testing.expectEqualStrings("", admitted("Jr.Donald.Trump.Com", true));
    try testing.expectEqualStrings("", admitted("Handle.Invalid", true));
    try testing.expectEqualStrings("handle.invalid", admitted("handle.invalid", true));
}

test "the quarantine list is the Worker's file" {
    try testing.expect(quarantined.len > 0);
    for (quarantined) |entry| try testing.expect(searchExcluded(entry));
}

test "the list parser reads an array of plain strings" {
    comptime {
        const list = parseStrings("[\n  \"a.test\",\n  \"b.test\"\n]\n");
        std.debug.assert(list.len == 2 and std.mem.eql(u8, list[1], "b.test"));
        std.debug.assert(parseStrings("[]").len == 0);
    }
}
