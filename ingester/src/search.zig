//! Search handler: 3-query ranking strategy
//! Replicates src/handlers/search.ts logic using local SQLite FTS5

const std = @import("std");
const json = std.json;
const mem = std.mem;
const Allocator = mem.Allocator;
const LocalDb = @import("db/LocalDb.zig");

const log = std.log.scoped(.search);

const MAX_SEEN = 201; // 1 exact + 100 prefix + 100 FTS
const MAX_DID_LEN = 64;

/// Perform the 3-query search and write JSON response into the writer.
/// Ranking: exact handle → handle prefix (LIKE) → FTS5 prefix
/// Matches the CF Worker's search.ts ranking.
///
/// Results are written inline while SQLite statements are alive to avoid
/// use-after-free on row text pointers (which are only valid until the
/// next step() or deinit() on the statement).
pub fn search(local: *LocalDb, raw_query: []const u8, limit: usize, writer: anytype) !void {
    const term = sanitize(raw_query);
    if (term.len == 0) {
        try writer.writeAll("{\"actors\":[]}");
        return;
    }

    // DID dedup: copy DIDs into stack storage so they outlive each query
    var seen_storage: [MAX_SEEN * MAX_DID_LEN]u8 = undefined;
    var seen_fba = std.heap.FixedBufferAllocator.init(&seen_storage);
    var seen_dids: [MAX_SEEN][]const u8 = undefined;
    var seen_count: usize = 0;

    var jw: json.Stringify = .{ .writer = writer };
    try jw.beginObject();
    try jw.objectField("actors");
    try jw.beginArray();

    var emitted: usize = 0;

    // 1. exact handle match
    {
        var rows = local.query(
            \\SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
            \\FROM actors WHERE handle = ? COLLATE NOCASE AND hidden = 0 LIMIT 1
        , .{term}) catch |err| blk: {
            log.err("exact query failed: {}", .{err});
            break :blk null;
        };
        if (rows) |*r| {
            defer r.deinit();
            if (r.next()) |row| {
                if (emitted < limit) {
                    try writeActorFromRow(&jw, row);
                    if (trackDid(&seen_fba, &seen_dids, &seen_count, row.text(0))) {}
                    emitted += 1;
                }
            }
        }
    }

    // build queries
    var fts_query_buf: [256]u8 = undefined;
    var like_query_buf: [256]u8 = undefined;

    const fts_query = std.fmt.bufPrint(&fts_query_buf, "\"{s}\"*", .{term}) catch {
        try jw.endArray();
        try jw.endObject();
        return;
    };
    const like_query = std.fmt.bufPrint(&like_query_buf, "{s}%", .{term}) catch {
        try jw.endArray();
        try jw.endObject();
        return;
    };

    // 2. two-phase handle prefix match
    // Phase 1: index-friendly LIKE with no ORDER BY (enables index early termination)
    // Phase 2: sort candidates by handle length in Zig, point-lookup for full rows
    // This restores the Worker's length-based ranking without the pathological full-scan sort.
    {
        const max_prefix = 100;
        const prefix_fetch = @min(limit * 5, max_prefix);

        var prefix_did_storage: [max_prefix * MAX_DID_LEN]u8 = undefined;
        var prefix_fba = std.heap.FixedBufferAllocator.init(&prefix_did_storage);
        var prefix_dids: [max_prefix][]const u8 = undefined;
        var prefix_hlens: [max_prefix]usize = undefined;
        var prefix_count: usize = 0;

        // phase 1: fast index scan (no ORDER BY — uses handle COLLATE NOCASE index)
        {
            var rows = local.query(
                \\SELECT did, handle FROM actors
                \\WHERE handle LIKE ? COLLATE NOCASE AND hidden = 0 LIMIT ?
            , .{ like_query, prefix_fetch }) catch |err| blk: {
                log.err("prefix query failed: {}", .{err});
                break :blk null;
            };
            if (rows) |*r| {
                defer r.deinit();
                while (r.next()) |row| {
                    const did = row.text(0);
                    const handle = row.text(1);
                    // skip exact match (already in tier 1)
                    if (handle.len == term.len and asciiEqlIgnoreCase(handle, term)) continue;
                    if (isDuplicate(seen_dids[0..seen_count], did)) continue;
                    const copy = prefix_fba.allocator().dupe(u8, did) catch break;
                    prefix_dids[prefix_count] = copy;
                    prefix_hlens[prefix_count] = handle.len;
                    prefix_count += 1;
                }
            }
        }

        // sort by handle length (insertion sort — at most 100 elements, <1μs)
        {
            var i: usize = 1;
            while (i < prefix_count) : (i += 1) {
                const key_did = prefix_dids[i];
                const key_hlen = prefix_hlens[i];
                var j: usize = i;
                while (j > 0 and prefix_hlens[j - 1] > key_hlen) {
                    prefix_dids[j] = prefix_dids[j - 1];
                    prefix_hlens[j] = prefix_hlens[j - 1];
                    j -= 1;
                }
                prefix_dids[j] = key_did;
                prefix_hlens[j] = key_hlen;
            }
        }

        // phase 2: point lookups in handle-length order
        for (prefix_dids[0..prefix_count]) |did| {
            if (emitted >= limit) break;
            var rows = local.query(
                \\SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
                \\FROM actors WHERE did = ?
            , .{did}) catch continue;
            defer rows.deinit();
            if (rows.next()) |row| {
                try writeActorFromRow(&jw, row);
                if (trackDid(&seen_fba, &seen_dids, &seen_count, row.text(0))) {}
                emitted += 1;
            }
        }
    }

    // 3. two-phase FTS5 prefix search
    // Phase 1: pure FTS5 ranked query (no JOIN — enables rank optimization + early termination)
    // Phase 2: point lookups on actors table by primary key
    // Skip if tiers 1+2 already filled the limit, or term too short (single-char prefix is pathological)
    if (emitted < limit and term.len >= 2) {
        const overfetch = limit * 5;
        const max_candidates = 500;
        const fetch_count = @min(overfetch, max_candidates);

        // collect candidate DIDs into stack buffer (row text pointers die on step/deinit)
        var candidate_storage: [max_candidates * MAX_DID_LEN]u8 = undefined;
        var candidate_fba = std.heap.FixedBufferAllocator.init(&candidate_storage);
        var candidate_dids: [max_candidates][]const u8 = undefined;
        var candidate_count: usize = 0;

        // phase 1: FTS5-only ranked query
        {
            var rows = local.query(
                \\SELECT did FROM actors_fts WHERE actors_fts MATCH ? ORDER BY rank LIMIT ?
            , .{ fts_query, fetch_count }) catch |err| blk: {
                log.err("FTS query failed for '{s}': {}", .{ fts_query, err });
                break :blk null;
            };
            if (rows) |*r| {
                defer r.deinit();
                while (r.next()) |row| {
                    const did = row.text(0);
                    if (isDuplicate(seen_dids[0..seen_count], did)) continue;
                    const copy = candidate_fba.allocator().dupe(u8, did) catch break;
                    candidate_dids[candidate_count] = copy;
                    candidate_count += 1;
                }
            }
        }

        // phase 2: point lookups by primary key
        for (candidate_dids[0..candidate_count]) |did| {
            if (emitted >= limit) break;
            var rows = local.query(
                \\SELECT did, handle, display_name, avatar_url, labels, created_at, associated, pds
                \\FROM actors WHERE did = ? AND handle != '' AND hidden = 0
            , .{did}) catch continue;
            defer rows.deinit();
            if (rows.next()) |row| {
                if (!isDuplicate(seen_dids[0..seen_count], row.text(0))) {
                    try writeActorFromRow(&jw, row);
                    if (trackDid(&seen_fba, &seen_dids, &seen_count, row.text(0))) {}
                    emitted += 1;
                }
            }
        }
    }

    try jw.endArray();
    try jw.endObject();
}

/// Write a single actor object from a live SQLite row.
/// Must be called while the row's statement is still alive.
/// Row columns: did, handle, display_name, avatar_url, labels, created_at, associated, pds
fn writeActorFromRow(jw: anytype, row: LocalDb.Row) !void {
    const did = row.text(0);
    const handle = row.text(1);
    const display_name = row.text(2);
    const avatar_url = row.text(3);
    const labels = row.text(4);
    const created_at = row.text(5);
    const associated = row.text(6);
    const pds = row.text(7);

    try jw.beginObject();

    try jw.objectField("did");
    try jw.write(did);
    try jw.objectField("handle");
    try jw.write(handle);

    if (display_name.len > 0) {
        try jw.objectField("displayName");
        try jw.write(display_name);
    }
    if (avatar_url.len > 0) {
        try jw.objectField("avatar");
        if (mem.startsWith(u8, avatar_url, "https://")) {
            try jw.write(avatar_url);
        } else if (pds.len > 0 and !mem.endsWith(u8, pds, ".bsky.network")) {
            // non-bsky PDS: construct blob URL from PDS + DID + CID
            try jw.print("\"{s}/xrpc/com.atproto.sync.getBlob?did={s}&cid={s}\"", .{ pds, did, avatar_url });
        } else {
            try jw.print("\"https://cdn.bsky.app/img/avatar/plain/{s}/{s}\"", .{ did, avatar_url });
        }
    }
    if (!mem.eql(u8, associated, "{}") and associated.len > 2) {
        try jw.objectField("associated");
        try jw.print("{s}", .{associated});
    }
    try jw.objectField("labels");
    try jw.print("{s}", .{labels});

    if (created_at.len > 0) {
        try jw.objectField("createdAt");
        try jw.write(created_at);
    }

    try jw.endObject();
}

/// Copy a DID into the fixed buffer for cross-query dedup.
fn trackDid(fba: *std.heap.FixedBufferAllocator, seen: *[MAX_SEEN][]const u8, count: *usize, did: []const u8) bool {
    if (count.* >= MAX_SEEN) return false;
    const copy = fba.allocator().dupe(u8, did) catch return false;
    seen[count.*] = copy;
    count.* += 1;
    return true;
}

fn asciiEqlIgnoreCase(a: []const u8, b: []const u8) bool {
    if (a.len != b.len) return false;
    for (a, b) |ac, bc| {
        if (toLower(ac) != toLower(bc)) return false;
    }
    return true;
}

fn toLower(c: u8) u8 {
    return if (c >= 'A' and c <= 'Z') c + 32 else c;
}

fn isDuplicate(seen: []const []const u8, did: []const u8) bool {
    for (seen) |s| {
        if (mem.eql(u8, s, did)) return true;
    }
    return false;
}

/// Keep only unicode letters, digits, whitespace, '.', '-'
/// Matches the worker's sanitize() in src/utils.ts
/// Uses a static buffer — safe for single query at a time per thread.
var sanitize_buf: [512]u8 = undefined;

fn sanitize(input: []const u8) []const u8 {
    var out: usize = 0;
    for (input) |c| {
        if (out >= sanitize_buf.len) break;
        if ((c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or (c >= '0' and c <= '9') or c == '.' or c == '-' or c == ' ' or c == '\t') {
            sanitize_buf[out] = c;
            out += 1;
        } else if (c >= 0x80) {
            // pass through UTF-8 bytes (unicode letters/digits)
            sanitize_buf[out] = c;
            out += 1;
        }
        // else: strip it (FTS5 special chars, punctuation, etc.)
    }
    return mem.trim(u8, sanitize_buf[0..out], " \t\n\r");
}
