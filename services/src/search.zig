//! The public typeahead response shape and query sanitising, shared by the
//! index engine (`index/index_search.zig`) and `/debug/search`.
//!
//! Ranking lives in the snapshot builder (`index/scoring.zig`): candidates
//! arrive here already ordered by the precomputed quality score, and this
//! module only decides how an actor is emitted. Bluesky's typeahead is the
//! benchmark oracle in scripts/bench.py (`just bench-ranking`), not a serving
//! dependency.

const std = @import("std");
const mem = std.mem;

pub const Source = enum { exact, prefix };

pub const Candidate = struct {
    did: []const u8,
    handle: []const u8,
    display_name: []const u8,
    avatar_url: []const u8,
    labels: []const u8,
    created_at: []const u8,
    associated: []const u8,
    pds: []const u8,
    followers_count: i64,
    follows_count: i64,
    posts_count: i64,
    last_activity_at: i64,
    quality_score: f64,
    source: Source,
};

fn avatarCid(stored: []const u8) []const u8 {
    if (stored.len == 0) return "";
    if (mem.indexOf(u8, stored, "/") == null) return stored; // bare CID
    if (mem.indexOf(u8, stored, "cid=")) |i| {
        const start = i + 4;
        const end = mem.indexOfScalarPos(u8, stored, start, '&') orelse stored.len;
        return stored[start..end];
    }
    // CDN-style: last path segment, minus @format suffix
    const last_slash = mem.lastIndexOfScalar(u8, stored, '/') orelse return "";
    var seg = stored[last_slash + 1 ..];
    if (mem.indexOfScalar(u8, seg, '@')) |at| seg = seg[0..at];
    if (seg.len == 0 or mem.indexOfScalar(u8, seg, '?') != null) return "";
    return seg;
}

pub fn writeActor(jw: anytype, c: *const Candidate) !void {
    try jw.beginObject();

    try jw.objectField("did");
    try jw.write(c.did);
    try jw.objectField("handle");
    try jw.write(c.handle);

    if (c.display_name.len > 0) {
        try jw.objectField("displayName");
        try jw.write(c.display_name);
    }
    if (c.avatar_url.len > 0) {
        // CDN-first for everyone (mirrors worker utils.ts avatarUrl): the CDN
        // serves any appview-ingested actor, stays up, and matches bsky's own
        // typeahead. The old PDS-first form broke whenever a self-hosted box
        // was down and leaked viewer IPs to it (evil.brookie.blog via
        // pegasus.juli.ee, 2026-08-04). Stored values are bare CIDs or full
        // URLs (older enrichment stored complete getBlob URLs) — extract the
        // CID from either shape. Non-CDN sources ship as avatarFallback;
        // pages swap to it on img error (covers actors the appview has
        // never ingested).
        const cid = avatarCid(c.avatar_url);
        try jw.objectField("avatar");
        if (cid.len > 0) {
            // Build the URL into a stack buffer, then let Stringify quote +
            // escape it. Manual `\"...\"` printing would skip JSON escaping
            // and produce invalid output if a value ever contains " or \.
            var url_buf: [512]u8 = undefined;
            const url = std.fmt.bufPrint(&url_buf, "https://cdn.bsky.app/img/avatar/plain/{s}/{s}", .{ c.did, cid }) catch null;
            if (url) |u| try jw.write(u) else try jw.write(null);

            var fb_buf: [512]u8 = undefined;
            const fallback: ?[]const u8 = if (mem.startsWith(u8, c.avatar_url, "https://") and mem.indexOf(u8, c.avatar_url, "cdn.bsky.app") == null)
                c.avatar_url
            else if (!mem.startsWith(u8, c.avatar_url, "https://") and c.pds.len > 0 and !mem.endsWith(u8, c.pds, ".bsky.network"))
                std.fmt.bufPrint(&fb_buf, "{s}/xrpc/com.atproto.sync.getBlob?did={s}&cid={s}", .{ c.pds, c.did, cid }) catch null
            else
                null;
            if (fallback) |fb| {
                try jw.objectField("avatarFallback");
                try jw.write(fb);
            }
        } else if (mem.startsWith(u8, c.avatar_url, "https://")) {
            try jw.write(c.avatar_url);
        } else {
            try jw.write(null);
        }
    }
    // `associated` and `labels` are stored as JSON strings — embed via the
    // explicit raw-passthrough API rather than `print("{s}", ...)` so the
    // intent is obvious and Stringify's internal safety asserts apply.
    if (!mem.eql(u8, c.associated, "{}") and c.associated.len > 2) {
        try jw.objectField("associated");
        try jw.beginWriteRaw();
        try jw.writer.writeAll(c.associated);
        jw.endWriteRaw();
    }
    try jw.objectField("labels");
    try jw.beginWriteRaw();
    try jw.writer.writeAll(c.labels);
    jw.endWriteRaw();

    if (c.created_at.len > 0) {
        try jw.objectField("createdAt");
        try jw.write(c.created_at);
    }

    try jw.endObject();
}

/// Strip only what would break our SQL — LIKE wildcards (%, _), LIKE escape (\),
/// FTS5 phrase-quote ("), and control chars. Everything else (parens, brackets,
/// colons, commas, quotes, emoji, unicode) passes through. Real display names
/// contain these characters; stripping them silently turns "fig (aka:[phil])"
/// into "fig akaphil" and makes the query useless.
/// Mirrors the worker's sanitize() in src/utils.ts.
pub fn sanitize(input: []const u8, sanitize_buf: *[512]u8) []const u8 {
    var out: usize = 0;
    for (input) |c| {
        if (out >= sanitize_buf.len) break;
        if (c == '\\' or c == '%' or c == '_' or c == '"') continue;
        if (c < 0x20 or c == 0x7f) continue;
        sanitize_buf[out] = c;
        out += 1;
    }
    return mem.trim(u8, sanitize_buf[0..out], " \t\n\r");
}

test "avatarCid: bare CID, getBlob URL, CDN URL, garbage" {
    const t = std.testing;
    try t.expectEqualStrings("bafkabc", avatarCid("bafkabc"));
    try t.expectEqualStrings("bafkxyz", avatarCid("https://pegasus.juli.ee/xrpc/com.atproto.sync.getBlob?did=did:plc:x&cid=bafkxyz"));
    try t.expectEqualStrings("bafkcdn", avatarCid("https://cdn.bsky.app/img/avatar/plain/did:plc:x/bafkcdn@jpeg"));
    try t.expectEqualStrings("", avatarCid(""));
    try t.expectEqualStrings("", avatarCid("https://example.com/"));
}
