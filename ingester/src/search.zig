//! Search handler: candidate generation + scoring.
//!
//! Independent ranking informed by — not dependent on — Bluesky. Three
//! candidate sources (exact handle, handle prefix, FTS) feed a single scoring
//! pipeline. Bluesky's typeahead serves as the benchmark oracle in
//! scripts/bench-search.py, not as a serving dependency.
//!
//! Score model (totals add):
//!
//!     + exact_handle      100000   handle equal to query (case-insensitive)
//!     + handle_prefix      25000   handle starts with query
//!     + name_prefix        18000   display name starts with query
//!     + name_tokens        12000   every query token appears in display name
//!     + handle_tokens       8000   every query token appears in handle
//!     + followers      900 * log10(followers + 1)
//!     + posts          180 * log10(posts + 1)
//!     + avatar               150   has avatar
//!     + has_display_name     100   has display name
//!     + has_created_at       100   has indexedAt-ish created_at
//!     + recent_activity       75   jetstream event within 30 days
//!     - no_avatar_no_name  -1200   neither avatar nor display name
//!     - suspicious_numbered -900   handle matches "<word>[-_]?<digits>"
//!     - repeated_tokens     -900   display name has the same token >= 3 times
//!
//! Ties break by: exact-match first → shorter handle → lexicographic handle.

const std = @import("std");
const json = std.json;
const mem = std.mem;
const Allocator = mem.Allocator;
const io = std.Options.debug_io;
const LocalDb = @import("db/LocalDb.zig");
const Col = LocalDb.Col;

const log = std.log.scoped(.search);

fn nowSeconds() i64 {
    return @intCast(@divFloor(std.Io.Timestamp.now(io, .real).nanoseconds, std.time.ns_per_s));
}

/// Per-candidate score components. `total()` sums them; storing each lets
/// `/debug/search` show why a row ranked where it did.
///
/// Token matching is **token-prefix** (each query token must be a prefix of
/// some text token), mirroring the analyzer behavior the bluesky AppView
/// skeleton appears to use. `fig` matches `fight` (prefix-of-token); `fig`
/// does not match `notfig` (mid-token substring). The raw_phrase_* fields
/// match the SANITIZED query verbatim against the display name — they're
/// what discriminates queries with whitespace/punctuation (e.g. `fig (`)
/// without inventing a special "name query mode".
pub const ScoreBreakdown = struct {
    exact_handle: f64 = 0,
    handle_prefix: f64 = 0,
    name_prefix: f64 = 0,
    raw_phrase_prefix_name: f64 = 0,
    raw_phrase_in_name: f64 = 0,
    name_tokens: f64 = 0,
    handle_tokens: f64 = 0,
    followers: f64 = 0,
    posts: f64 = 0,
    avatar: f64 = 0,
    has_display_name: f64 = 0,
    has_created_at: f64 = 0,
    recent_activity: f64 = 0,
    no_avatar_no_name: f64 = 0,
    suspicious_numbered: f64 = 0,
    repeated_tokens: f64 = 0,

    pub fn total(self: ScoreBreakdown) f64 {
        return self.exact_handle + self.handle_prefix + self.name_prefix +
            self.raw_phrase_prefix_name + self.raw_phrase_in_name +
            self.name_tokens + self.handle_tokens + self.followers +
            self.posts + self.avatar + self.has_display_name +
            self.has_created_at + self.recent_activity +
            self.no_avatar_no_name + self.suspicious_numbered +
            self.repeated_tokens;
    }
};

pub const Source = enum { exact, prefix, fts };

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
    source: Source,
    breakdown: ScoreBreakdown = .{},
};

/// Main search entry. Writes a `{"actors":[...]}` body, matching the public
/// `app.bsky.actor.searchActorsTypeahead` response shape.
pub fn search(local: *LocalDb, allocator: Allocator, raw_query: []const u8, limit: usize, writer: anytype) !void {
    const term = sanitize(raw_query);
    if (term.len == 0) {
        try writer.writeAll("{\"actors\":[]}");
        return;
    }

    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const aa = arena.allocator();

    var candidates = gatherCandidates(local, aa, term) catch |err| {
        log.err("gather candidates failed: {s}", .{@errorName(err)});
        try writer.writeAll("{\"actors\":[]}");
        return;
    };
    scoreCandidates(candidates, term);
    sortCandidates(candidates, term);

    var jw: json.Stringify = .{ .writer = writer };
    try jw.beginObject();
    try jw.objectField("actors");
    try jw.beginArray();

    const n = @min(limit, candidates.len);
    for (candidates[0..n]) |*c| {
        try writeActor(&jw, c);
    }

    try jw.endArray();
    try jw.endObject();
}

/// Debug entry. Returns `{"q":..., "candidates":[{..., score, breakdown, source}]}`
/// for inspecting why ranking turned out the way it did. App B-only — not
/// proxied through the worker.
pub fn debugSearch(local: *LocalDb, allocator: Allocator, raw_query: []const u8, limit: usize, writer: anytype) !void {
    const term = sanitize(raw_query);

    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const aa = arena.allocator();

    var jw: json.Stringify = .{ .writer = writer };
    try jw.beginObject();
    try jw.objectField("q");
    try jw.write(term);

    if (term.len == 0) {
        try jw.objectField("candidates");
        try jw.beginArray();
        try jw.endArray();
        try jw.endObject();
        return;
    }

    var candidates = gatherCandidates(local, aa, term) catch |err| {
        try jw.objectField("error");
        try jw.write(@errorName(err));
        try jw.objectField("candidates");
        try jw.beginArray();
        try jw.endArray();
        try jw.endObject();
        return;
    };
    scoreCandidates(candidates, term);
    sortCandidates(candidates, term);

    try jw.objectField("count");
    try jw.write(@as(u64, candidates.len));
    try jw.objectField("candidates");
    try jw.beginArray();

    const n = @min(limit, candidates.len);
    for (candidates[0..n]) |*c| {
        try writeCandidateDebug(&jw, c);
    }

    try jw.endArray();
    try jw.endObject();
}

// ── candidate gathering ──────────────────────────────────────────────────

const PREFIX_OVERFETCH: usize = 200;
const FTS_OVERFETCH: usize = 300;

/// Collect candidates from all three sources, dedupe by DID, then hydrate
/// each unique DID's full row via primary-key point-lookups. Source is
/// remembered (debug-only signal — scoring is source-agnostic).
fn gatherCandidates(local: *LocalDb, aa: Allocator, term: []const u8) ![]Candidate {
    var seen = std.StringHashMap(Source).init(aa);
    var ordered: std.ArrayListUnmanaged([]const u8) = .empty;

    try collectExact(local, aa, term, &seen, &ordered);
    try collectPrefix(local, aa, term, &seen, &ordered);
    if (term.len >= 2) try collectFts(local, aa, term, &seen, &ordered);

    if (ordered.items.len == 0) return &.{};

    // Batched fetch: one `WHERE did IN (?, ?, …)` instead of N point-lookups.
    // N can hit 501 for hot prefixes (1 exact + 200 prefix + 300 FTS). N
    // individual queries through zqlite's single read connection serialize
    // and push p95 past the worker's 3s fallback timeout; batching collapses
    // it to a single index range scan.
    var sql_buf: std.ArrayListUnmanaged(u8) = .empty;
    try sql_buf.appendSlice(aa, "SELECT " ++ LocalDb.actor_cols ++ " FROM actors WHERE hidden = 0 AND did IN (");
    for (ordered.items, 0..) |_, i| {
        if (i > 0) try sql_buf.append(aa, ',');
        try sql_buf.append(aa, '?');
    }
    try sql_buf.append(aa, ')');

    var rows = local.queryTextList(sql_buf.items, ordered.items) catch |err| {
        log.err("batched candidate fetch failed: {s}", .{@errorName(err)});
        return &.{};
    };
    defer rows.deinit();

    var candidates: std.ArrayListUnmanaged(Candidate) = try .initCapacity(aa, ordered.items.len);
    while (rows.next()) |row| {
        const did_text = row.text(Col.did);
        const source = seen.get(did_text) orelse .fts;
        candidates.appendAssumeCapacity(.{
            .did = try aa.dupe(u8, did_text),
            .handle = try aa.dupe(u8, row.text(Col.handle)),
            .display_name = try aa.dupe(u8, row.text(Col.display_name)),
            .avatar_url = try aa.dupe(u8, row.text(Col.avatar_url)),
            .labels = try aa.dupe(u8, row.text(Col.labels)),
            .created_at = try aa.dupe(u8, row.text(Col.created_at)),
            .associated = try aa.dupe(u8, row.text(Col.associated)),
            .pds = try aa.dupe(u8, row.text(Col.pds)),
            .followers_count = row.int(Col.followers_count),
            .follows_count = row.int(Col.follows_count),
            .posts_count = row.int(Col.posts_count),
            .last_activity_at = row.int(Col.last_activity_at),
            .source = source,
        });
    }

    return candidates.toOwnedSlice(aa);
}

fn collectExact(local: *LocalDb, aa: Allocator, term: []const u8, seen: *std.StringHashMap(Source), ordered: *std.ArrayListUnmanaged([]const u8)) !void {
    var rows = local.query(
        "SELECT did FROM actors WHERE handle = ? COLLATE NOCASE AND hidden = 0 LIMIT 1",
        .{term},
    ) catch |err| {
        log.err("exact query failed: {s}", .{@errorName(err)});
        return;
    };
    defer rows.deinit();
    if (rows.next()) |row| try addDid(aa, seen, ordered, row.text(0), .exact);
}

fn collectPrefix(local: *LocalDb, aa: Allocator, term: []const u8, seen: *std.StringHashMap(Source), ordered: *std.ArrayListUnmanaged([]const u8)) !void {
    var like_buf: [600]u8 = undefined;
    const like = std.fmt.bufPrint(&like_buf, "{s}%", .{term}) catch return;
    var rows = local.query(
        "SELECT did FROM actors WHERE handle LIKE ? COLLATE NOCASE AND hidden = 0 LIMIT ?",
        .{ like, PREFIX_OVERFETCH },
    ) catch |err| {
        log.err("prefix query failed: {s}", .{@errorName(err)});
        return;
    };
    defer rows.deinit();
    while (rows.next()) |row| try addDid(aa, seen, ordered, row.text(0), .prefix);
}

fn collectFts(local: *LocalDb, aa: Allocator, term: []const u8, seen: *std.StringHashMap(Source), ordered: *std.ArrayListUnmanaged([]const u8)) !void {
    var fts_buf: [600]u8 = undefined;
    const fts = buildFtsQuery(term, &fts_buf) orelse return;
    var rows = local.query(
        "SELECT did FROM actors_fts WHERE actors_fts MATCH ? ORDER BY rank LIMIT ?",
        .{ fts, FTS_OVERFETCH },
    ) catch |err| {
        log.err("FTS query failed for '{s}': {s}", .{ fts, @errorName(err) });
        return;
    };
    defer rows.deinit();
    while (rows.next()) |row| try addDid(aa, seen, ordered, row.text(0), .fts);
}

/// Build an FTS5 MATCH expression from the sanitized query, using standard
/// search-as-you-type analyzer behavior:
///
///   "fig"     → `fig*`        (last char is a token char → user still typing)
///   "fig "    → `fig`         (trailing space → token complete → exact match)
///   "fig ("   → `fig`         (same: trailing punctuation closes the token)
///   "fig aka" → `fig aka*`    (intermediate token exact, last token prefix)
///   "fig (a"  → `fig a*`      (paren closes "fig", "a" is the in-progress last)
///
/// Tokens are joined with spaces (implicit AND in FTS5). Each query token
/// must appear as an exact token in the doc — except the trailing prefix one
/// which matches via FTS5 `*` prefix operator. Mirrors bsky's open analyzer
/// behavior: q='fig' returns "Fight" hits, q='fig (' does not.
fn buildFtsQuery(term: []const u8, buf: []u8) ?[]const u8 {
    const tokens = splitTokens(term);
    if (tokens.count == 0) return null;

    const last_token_complete = !isTokenChar(term[term.len - 1]);

    var w: usize = 0;
    for (tokens.items[0..tokens.count], 0..) |tok, i| {
        if (i > 0) {
            if (w >= buf.len) return null;
            buf[w] = ' ';
            w += 1;
        }
        if (w + tok.len > buf.len) return null;
        @memcpy(buf[w .. w + tok.len], tok);
        w += tok.len;
    }
    if (!last_token_complete) {
        if (w >= buf.len) return null;
        buf[w] = '*';
        w += 1;
    }
    return buf[0..w];
}

fn addDid(aa: Allocator, seen: *std.StringHashMap(Source), ordered: *std.ArrayListUnmanaged([]const u8), did: []const u8, src: Source) !void {
    if (did.len == 0) return;
    const owned = try aa.dupe(u8, did);
    const gop = try seen.getOrPut(owned);
    if (!gop.found_existing) {
        gop.value_ptr.* = src;
        try ordered.append(aa, owned);
    }
}

// ── scoring ──────────────────────────────────────────────────────────────

const Tokens = struct {
    items: [16][]const u8 = undefined,
    count: usize = 0,
};

fn scoreCandidates(candidates: []Candidate, term: []const u8) void {
    const now = nowSeconds();
    const tokens = splitTokens(term);

    for (candidates) |*c| {
        var b: ScoreBreakdown = .{};

        if (asciiEqlIgnoreCase(c.handle, term)) b.exact_handle = 100000;

        if (asciiStartsWithIgnoreCase(c.handle, term)) b.handle_prefix = 25000;
        if (c.display_name.len > 0 and asciiStartsWithIgnoreCase(c.display_name, term)) b.name_prefix = 18000;

        // Raw-phrase signals: the sanitized query verbatim (with whitespace +
        // punctuation preserved) is checked against the display name. These
        // are what discriminate `fig (` from `fig` — the latter's raw phrase
        // matches many things while the former only matches display names
        // that literally contain `fig (`.
        if (c.display_name.len > 0) {
            if (asciiStartsWithIgnoreCase(c.display_name, term)) b.raw_phrase_prefix_name = 20000;
            if (asciiContainsIgnoreCase(c.display_name, term)) b.raw_phrase_in_name = 6000;
        }

        if (c.display_name.len > 0 and allTokensPrefixInText(c.display_name, tokens)) b.name_tokens = 12000;
        if (allTokensPrefixInText(c.handle, tokens)) b.handle_tokens = 8000;

        b.followers = 900 * std.math.log10(@as(f64, @floatFromInt(@max(c.followers_count, 0) + 1)));
        b.posts = 180 * std.math.log10(@as(f64, @floatFromInt(@max(c.posts_count, 0) + 1)));

        if (c.avatar_url.len > 0) b.avatar = 150;
        if (c.display_name.len > 0) b.has_display_name = 100;
        if (c.created_at.len > 0) b.has_created_at = 100;

        if (c.last_activity_at > 0 and (now - c.last_activity_at) <= 30 * 86400) {
            b.recent_activity = 75;
        }

        if (c.avatar_url.len == 0 and c.display_name.len == 0) b.no_avatar_no_name = -1200;
        if (suspiciousNumberedHandle(c.handle)) b.suspicious_numbered = -900;
        if (c.display_name.len > 0 and repeatedTokenName(c.display_name)) b.repeated_tokens = -900;

        c.breakdown = b;
    }
}

fn splitTokens(term: []const u8) Tokens {
    var t: Tokens = .{};
    var i: usize = 0;
    while (i < term.len and t.count < t.items.len) {
        while (i < term.len and !isTokenChar(term[i])) i += 1;
        if (i >= term.len) break;
        const start = i;
        while (i < term.len and isTokenChar(term[i])) i += 1;
        if (i > start) {
            t.items[t.count] = term[start..i];
            t.count += 1;
        }
    }
    return t;
}

/// Token-prefix match: every query token must be a prefix of at least one
/// token in the text. Mirrors ElasticSearch-style analyzer behavior the
/// bluesky AppView skeleton appears to use — `fig` matches `fight` (prefix of
/// the `fight` token), but does not match `notfig` (the only token in
/// `notfig` doesn't start with `fig`). Multi-token queries like `fig aka`
/// require each query token to prefix-match some text token, so
/// `fig (aka:[phil])` matches but `Fig (they/them)` doesn't (no token
/// starting with `aka`).
fn allTokensPrefixInText(text: []const u8, tokens: Tokens) bool {
    if (tokens.count == 0) return false;
    const text_tokens = splitTokens(text);
    if (text_tokens.count == 0) return false;
    for (tokens.items[0..tokens.count]) |q_tok| {
        if (q_tok.len == 0) continue;
        var found = false;
        for (text_tokens.items[0..text_tokens.count]) |t_tok| {
            if (asciiStartsWithIgnoreCase(t_tok, q_tok)) {
                found = true;
                break;
            }
        }
        if (!found) return false;
    }
    return true;
}

/// Case-insensitive substring search. Used by `raw_phrase_in_name` where we
/// match the entire sanitized query verbatim against the display name —
/// the punctuation/whitespace in the raw query carries the discrimination,
/// so substring (rather than tokenized) is the right semantic.
fn asciiContainsIgnoreCase(haystack: []const u8, needle: []const u8) bool {
    if (needle.len == 0) return true;
    if (needle.len > haystack.len) return false;
    var i: usize = 0;
    while (i + needle.len <= haystack.len) : (i += 1) {
        var match = true;
        for (needle, 0..) |nc, j| {
            if (toLower(nc) != toLower(haystack[i + j])) {
                match = false;
                break;
            }
        }
        if (match) return true;
    }
    return false;
}

// ── sorting ──────────────────────────────────────────────────────────────

const SortCtx = struct { term: []const u8 };

fn sortCandidates(candidates: []Candidate, term: []const u8) void {
    std.sort.block(Candidate, candidates, SortCtx{ .term = term }, candidateLessThan);
}

fn candidateLessThan(ctx: SortCtx, a: Candidate, b: Candidate) bool {
    const a_score = a.breakdown.total();
    const b_score = b.breakdown.total();
    if (a_score != b_score) return a_score > b_score;
    const a_exact = asciiEqlIgnoreCase(a.handle, ctx.term);
    const b_exact = asciiEqlIgnoreCase(b.handle, ctx.term);
    if (a_exact != b_exact) return a_exact;
    if (a.handle.len != b.handle.len) return a.handle.len < b.handle.len;
    return mem.order(u8, a.handle, b.handle) == .lt;
}

// ── emit ─────────────────────────────────────────────────────────────────

fn writeActor(jw: anytype, c: *const Candidate) !void {
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
        try jw.objectField("avatar");
        if (mem.startsWith(u8, c.avatar_url, "https://")) {
            try jw.write(c.avatar_url);
        } else if (c.pds.len > 0 and !mem.endsWith(u8, c.pds, ".bsky.network")) {
            try jw.print("\"{s}/xrpc/com.atproto.sync.getBlob?did={s}&cid={s}\"", .{ c.pds, c.did, c.avatar_url });
        } else {
            try jw.print("\"https://cdn.bsky.app/img/avatar/plain/{s}/{s}\"", .{ c.did, c.avatar_url });
        }
    }
    if (!mem.eql(u8, c.associated, "{}") and c.associated.len > 2) {
        try jw.objectField("associated");
        try jw.print("{s}", .{c.associated});
    }
    try jw.objectField("labels");
    try jw.print("{s}", .{c.labels});

    if (c.created_at.len > 0) {
        try jw.objectField("createdAt");
        try jw.write(c.created_at);
    }

    try jw.endObject();
}

fn writeCandidateDebug(jw: anytype, c: *const Candidate) !void {
    try jw.beginObject();
    try jw.objectField("did");
    try jw.write(c.did);
    try jw.objectField("handle");
    try jw.write(c.handle);
    try jw.objectField("displayName");
    try jw.write(c.display_name);
    try jw.objectField("source");
    try jw.write(@tagName(c.source));
    try jw.objectField("followers");
    try jw.write(c.followers_count);
    try jw.objectField("posts");
    try jw.write(c.posts_count);
    try jw.objectField("lastActivityAt");
    try jw.write(c.last_activity_at);
    try jw.objectField("score");
    try jw.write(c.breakdown.total());
    try jw.objectField("breakdown");
    try jw.beginObject();
    inline for (std.meta.fields(ScoreBreakdown)) |f| {
        const v = @field(c.breakdown, f.name);
        if (v != 0) {
            try jw.objectField(f.name);
            try jw.write(v);
        }
    }
    try jw.endObject();
    try jw.endObject();
}

// ── ascii helpers ────────────────────────────────────────────────────────

fn asciiEqlIgnoreCase(a: []const u8, b: []const u8) bool {
    if (a.len != b.len) return false;
    for (a, b) |ac, bc| if (toLower(ac) != toLower(bc)) return false;
    return true;
}

fn asciiStartsWithIgnoreCase(haystack: []const u8, prefix: []const u8) bool {
    if (prefix.len > haystack.len) return false;
    for (prefix, 0..) |pc, i| if (toLower(pc) != toLower(haystack[i])) return false;
    return true;
}

fn toLower(c: u8) u8 {
    return if (c >= 'A' and c <= 'Z') c + 32 else c;
}

fn isTokenChar(c: u8) bool {
    return (c >= '0' and c <= '9') or (c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or c == '_';
}

fn isDigit(c: u8) bool {
    return c >= '0' and c <= '9';
}

fn isAsciiLetter(c: u8) bool {
    return (c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z');
}

fn isWhitespace(c: u8) bool {
    return c == ' ' or c == '\t' or c == '\n' or c == '\r';
}

// ── spam heuristics ──────────────────────────────────────────────────────

/// Returns true if the local-part of the handle ends with digits, preceded by
/// at least one letter and an optional `-` or `_` separator. Catches the
/// spam pattern `alex-123`, `alex_1`, `alex123` while leaving alphabetic
/// handles like `anna-marie` alone.
fn suspiciousNumberedHandle(handle: []const u8) bool {
    const dot = mem.indexOfScalar(u8, handle, '.') orelse handle.len;
    const local = handle[0..dot];
    if (local.len < 2) return false;

    var end = local.len;
    var digit_count: usize = 0;
    while (end > 0 and isDigit(local[end - 1])) : (end -= 1) digit_count += 1;
    if (digit_count == 0) return false;

    var letter_end = end;
    if (letter_end > 0 and (local[letter_end - 1] == '-' or local[letter_end - 1] == '_')) {
        letter_end -= 1;
    }
    if (letter_end == 0) return false;

    var j: usize = 0;
    while (j < letter_end) : (j += 1) {
        if (isAsciiLetter(local[j])) return true;
    }
    return false;
}

/// Returns true if any whitespace-separated token in the display name appears
/// at least three times. Catches `alex alex alex alex` and similar
/// keyword-stuffing patterns.
fn repeatedTokenName(name: []const u8) bool {
    var tokens: [32][]const u8 = undefined;
    var count: usize = 0;
    var i: usize = 0;
    while (i < name.len and count < tokens.len) {
        while (i < name.len and isWhitespace(name[i])) i += 1;
        if (i >= name.len) break;
        const start = i;
        while (i < name.len and !isWhitespace(name[i])) i += 1;
        if (i > start) {
            tokens[count] = name[start..i];
            count += 1;
        }
    }
    if (count < 3) return false;
    for (tokens[0..count], 0..) |t1, k1| {
        var repeats: usize = 1;
        for (tokens[k1 + 1 .. count]) |t2| {
            if (asciiEqlIgnoreCase(t1, t2)) repeats += 1;
        }
        if (repeats >= 3) return true;
    }
    return false;
}

// ── sanitization ─────────────────────────────────────────────────────────

/// Strip only what would break our SQL — LIKE wildcards (%, _), LIKE escape (\),
/// FTS5 phrase-quote ("), and control chars. Everything else (parens, brackets,
/// colons, commas, quotes, emoji, unicode) passes through. Real display names
/// contain these characters; stripping them silently turns "fig (aka:[phil])"
/// into "fig akaphil" and makes the query useless.
/// Mirrors the worker's sanitize() in src/utils.ts.
/// Uses a static buffer — safe for single query at a time per thread.
var sanitize_buf: [512]u8 = undefined;

fn sanitize(input: []const u8) []const u8 {
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
