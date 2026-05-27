//! Prefix key derivation. Given a normalized handle and display name,
//! emit the lookup keys that map an actor into the prefix index.
//!
//! Four namespaces:
//!   `h:<prefix>` — every prefix of the normalized handle, length 1..PREFIX_MAX
//!   `n:<prefix>` — every prefix of every display-name token, length 1..PREFIX_MAX
//!   `e:<handle>` — the exact normalized handle (one key per actor)
//!   `d:<prefix>` — every prefix of every dot-boundary suffix of the handle,
//!                  so `foo.germ.network` is findable by `germ.network`,
//!                  `germ`, `network` (atproto handles are domain-structured;
//!                  `h:` only matches start-anchored, never mid-handle).
//!
//! Builder calls `deriveAll` for each actor and emits (key, did, score)
//! tuples. Serving calls `queryKeys` on the user's normalized query.
//! The two paths must agree on what a "key" looks like for any input —
//! both go through this file. See normalize.zig for upstream constraints.

const std = @import("std");
const Allocator = std.mem.Allocator;
const normalize = @import("normalize.zig");

/// Maximum prefix length emitted by the builder. Caps the long tail of
/// 13+-char prefixes that almost nobody types. Bumping this multiplies
/// index size linearly with handle/token length.
pub const PREFIX_MAX: usize = 16;

/// Cap on the number of display-name tokens we index per actor. Defense
/// against pathological display names (someone setting it to a 200-word
/// essay would otherwise emit thousands of keys for one actor).
pub const MAX_DISPLAY_TOKENS: usize = 8;

/// Single key namespace tag. Encoded as a 2-byte ASCII prefix in the
/// stored key (`h:`, `n:`, `e:`) for human-readable debugging and a
/// trivial `WHERE key IN (...)` lookup that covers all three.
pub const Namespace = enum(u8) {
    handle_prefix = 'h',
    display_token = 'n',
    exact_handle = 'e',
    domain_suffix = 'd',
};

/// Render a namespace + text into a freshly-allocated key string
/// "<ns>:<text>". Caller owns the result (arena-typical).
pub fn formatKey(arena: Allocator, ns: Namespace, text: []const u8) ![]const u8 {
    return std.fmt.allocPrint(arena, "{c}:{s}", .{ @intFromEnum(ns), text });
}

/// Derive every `h:` prefix key for a normalized handle. Returns a slice
/// of allocated keys; lengths from 1 up to min(len, PREFIX_MAX).
pub fn handlePrefixKeys(arena: Allocator, normalized_handle: []const u8) ![][]const u8 {
    const max = @min(normalized_handle.len, PREFIX_MAX);
    if (max == 0) return &.{};

    var keys = try arena.alloc([]const u8, max);
    var i: usize = 0;
    while (i < max) : (i += 1) {
        keys[i] = try formatKey(arena, .handle_prefix, normalized_handle[0 .. i + 1]);
    }
    return keys;
}

/// Tokens from a normalized display name. Splits on the single ASCII
/// space character `' '` (normalize collapses all separators to one space).
/// Empty strings between leading/trailing separators have already been
/// stripped by normalize, so consecutive spaces don't happen.
///
/// Returns up to MAX_DISPLAY_TOKENS slices into the input (NOT copies).
/// Caller must hold the input alive while using the result.
pub fn displayTokens(arena: Allocator, normalized_display: []const u8) ![][]const u8 {
    if (normalized_display.len == 0) return &.{};

    var tokens: std.ArrayListUnmanaged([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, normalized_display, ' ');
    while (it.next()) |tok| {
        if (tok.len == 0) continue;
        try tokens.append(arena, tok);
        if (tokens.items.len >= MAX_DISPLAY_TOKENS) break;
    }
    return tokens.toOwnedSlice(arena);
}

/// Max codepoints bigrammed from a single CJK run (defense against a
/// pathological all-CJK display name). Phase 0 avg is ~4 codepoints.
pub const CJK_RUN_MAX: usize = 24;

/// CJK codepoints we index: Han, Hiragana (katakana is folded to Hiragana
/// upstream in normalize.zig), and half-width katakana. Must agree with the
/// set normalize.zig preserves — anything preserved-and-non-ASCII is CJK.
fn isCjkCodepoint(cp: u21) bool {
    return (cp >= 0x3040 and cp <= 0x30FF) // hiragana + (folded-away) katakana + ー
    or (cp >= 0x3400 and cp <= 0x4DBF) // CJK Ext A
    or (cp >= 0x4E00 and cp <= 0x9FFF) // CJK Unified Ideographs
    or (cp >= 0xF900 and cp <= 0xFAFF) // CJK Compatibility Ideographs
    or (cp >= 0xFF66 and cp <= 0xFF9D); // half-width katakana
}

/// Emit `n:` keys for one contiguous CJK run (codepoint-safe — all slices
/// land on UTF-8 boundaries). Adjacent-codepoint BIGRAMS (`n:日本`, `n:本太`)
/// approximate word boundaries for a script with no spaces; the query side
/// intersects them (the existing multi-token `n:` AND). Plus the LEADING
/// UNIGRAM (`n:日`) so a 1-char in-progress query matches.
///   for_query=false (builder): bigrams + leading unigram.
///   for_query=true  (query):   bigrams; or the single unigram when the run
///                              is one codepoint (no every-char unigram).
fn appendCjkKeys(
    arena: Allocator,
    keys: *std.ArrayListUnmanaged([]const u8),
    run: []const u8,
    for_query: bool,
) !void {
    // Byte offsets of each codepoint boundary, capped at CJK_RUN_MAX cps.
    var bound: [CJK_RUN_MAX + 1]usize = undefined;
    var n: usize = 0;
    bound[0] = 0;
    const view = std.unicode.Utf8View.init(run) catch return;
    var it = view.iterator();
    while (it.nextCodepointSlice()) |cps| {
        if (n >= CJK_RUN_MAX) break;
        bound[n + 1] = bound[n] + cps.len;
        n += 1;
    }
    if (n == 0) return;
    if (n == 1) {
        try keys.append(arena, try formatKey(arena, .display_token, run[0..bound[1]]));
        return;
    }
    var i: usize = 0;
    while (i + 1 < n) : (i += 1) {
        try keys.append(arena, try formatKey(arena, .display_token, run[bound[i]..bound[i + 2]]));
    }
    if (!for_query) {
        try keys.append(arena, try formatKey(arena, .display_token, run[0..bound[1]])); // leading unigram
    }
}

/// Emit `n:` keys for one Latin (ASCII, post-normalize) run.
///   for_query=false (builder): every prefix, length 1..PREFIX_MAX.
///   for_query=true  (query):   the full run as a single key.
fn appendLatinKeys(
    arena: Allocator,
    keys: *std.ArrayListUnmanaged([]const u8),
    run: []const u8,
    for_query: bool,
) !void {
    if (run.len == 0) return;
    if (for_query) {
        try keys.append(arena, try formatKey(arena, .display_token, run));
        return;
    }
    const max = @min(run.len, PREFIX_MAX); // Latin run is ASCII → byte == codepoint
    var i: usize = 0;
    while (i < max) : (i += 1) {
        try keys.append(arena, try formatKey(arena, .display_token, run[0 .. i + 1]));
    }
}

/// The shared, script-aware `n:`-key tokenizer. Segments one whitespace-token
/// into contiguous Latin vs CJK runs (codepoint-safe) and emits keys per run.
/// BOTH the builder (`displayPrefixKeys`, for_query=false) and the serving
/// path (`queryKeys`, for_query=true) go through here — one code path, so the
/// index and the query can never disagree on what a CJK key looks like.
fn emitTokenKeys(
    arena: Allocator,
    keys: *std.ArrayListUnmanaged([]const u8),
    token: []const u8,
    for_query: bool,
) !void {
    const view = std.unicode.Utf8View.init(token) catch {
        try appendLatinKeys(arena, keys, token, for_query); // invalid utf8: treat as ascii
        return;
    };
    var it = view.iterator();
    var run_start: usize = 0;
    var pos: usize = 0;
    var cur_cjk: ?bool = null;
    while (it.nextCodepointSlice()) |cps| {
        const cp = std.unicode.utf8Decode(cps) catch {
            pos += cps.len;
            continue;
        };
        const this_cjk = isCjkCodepoint(cp);
        if (cur_cjk) |ck| {
            if (ck != this_cjk) {
                try emitRun(arena, keys, token[run_start..pos], ck, for_query);
                run_start = pos;
            }
        }
        cur_cjk = this_cjk;
        pos += cps.len;
    }
    if (cur_cjk) |ck| try emitRun(arena, keys, token[run_start..pos], ck, for_query);
}

fn emitRun(
    arena: Allocator,
    keys: *std.ArrayListUnmanaged([]const u8),
    run: []const u8,
    is_cjk: bool,
    for_query: bool,
) !void {
    if (is_cjk) try appendCjkKeys(arena, keys, run, for_query) else try appendLatinKeys(arena, keys, run, for_query);
}

/// Derive every `n:` key for a normalized display name. Latin runs emit
/// prefixes; CJK runs emit bigrams + a leading unigram (see emitTokenKeys).
/// Duplicate keys across tokens are NOT deduplicated here — the builder's
/// group-by pass naturally dedupes.
pub fn displayPrefixKeys(arena: Allocator, normalized_display: []const u8) ![][]const u8 {
    const tokens = try displayTokens(arena, normalized_display);
    if (tokens.len == 0) return &.{};

    var keys: std.ArrayListUnmanaged([]const u8) = .empty;
    for (tokens) |tok| try emitTokenKeys(arena, &keys, tok, false);
    return keys.toOwnedSlice(arena);
}

/// Derive the single `e:` exact-handle key. Returns null if the handle
/// is empty (don't emit empty-string keys — they collide trivially).
pub fn exactHandleKey(arena: Allocator, normalized_handle: []const u8) !?[]const u8 {
    if (normalized_handle.len == 0) return null;
    return try formatKey(arena, .exact_handle, normalized_handle);
}

/// Derive every `d:` key: prefixes of each dot-boundary suffix of the handle.
/// For `foo.germ.network` the suffixes are `germ.network` and `network` (the
/// full handle is excluded — `h:` already covers start-anchored matches), and
/// each emits prefixes up to PREFIX_MAX. Lets a domain search like
/// `germ.network` (or just `germ`/`network`) surface every account under it.
pub fn domainSuffixKeys(arena: Allocator, normalized_handle: []const u8) ![][]const u8 {
    var keys: std.ArrayListUnmanaged([]const u8) = .empty;
    var i: usize = 0;
    while (i < normalized_handle.len) : (i += 1) {
        if (normalized_handle[i] != '.') continue;
        const suffix = normalized_handle[i + 1 ..];
        const max = @min(suffix.len, PREFIX_MAX);
        var j: usize = 0;
        while (j < max) : (j += 1) {
            try keys.append(arena, try formatKey(arena, .domain_suffix, suffix[0 .. j + 1]));
        }
    }
    return keys.toOwnedSlice(arena);
}

/// Derive every key for an actor (h: prefixes, n: token prefixes, e: exact).
/// Called per-actor by the builder. Order doesn't matter — the builder
/// groups by key later. Output may contain DUPLICATES (e.g. display name
/// "fig fig" produces two `n:fig` keys). Builders dedupe via GROUP BY at
/// sort time, so duplicates are correct for that path.
///
/// IMPORTANT: callers MUST pass values already normalized through the
/// right entry point — `normalize.handle()` for the handle, and
/// `normalize.display()` for the display name. The split exists so the
/// underscore policy and other handle-specific rules are applied
/// upstream of prefix derivation. Mixing the two normalize fns leaks
/// invalid characters into `h:`/`e:` keys.
pub fn deriveAll(
    arena: Allocator,
    normalized_handle: []const u8,
    normalized_display: []const u8,
) ![][]const u8 {
    var all: std.ArrayListUnmanaged([]const u8) = .empty;

    if (try exactHandleKey(arena, normalized_handle)) |k| {
        try all.append(arena, k);
    }
    for (try handlePrefixKeys(arena, normalized_handle)) |k| {
        try all.append(arena, k);
    }
    for (try displayPrefixKeys(arena, normalized_display)) |k| {
        try all.append(arena, k);
    }
    for (try domainSuffixKeys(arena, normalized_handle)) |k| {
        try all.append(arena, k);
    }

    return all.toOwnedSlice(arena);
}

/// Same as `deriveAll` but returns a deduplicated, lexicographically
/// sorted slice. Use this from the OVERLAY UPDATER, where emitting
/// duplicate prefix_overlay rows would be wasted work AND would break
/// diff-against-old-prefixes set arithmetic (`added = new - old`).
///
/// The dedupe step uses a StringHashMap keyed by the full namespaced
/// key text. Cost is O(N) on the emission count, which is bounded by
/// PREFIX_MAX * (1 + MAX_DISPLAY_TOKENS) per actor — well under 200
/// entries even in the worst case.
pub fn deriveAllUnique(
    arena: Allocator,
    normalized_handle: []const u8,
    normalized_display: []const u8,
) ![][]const u8 {
    const all = try deriveAll(arena, normalized_handle, normalized_display);

    var seen: std.StringHashMap(void) = .init(arena);
    try seen.ensureTotalCapacity(@intCast(all.len));
    for (all) |k| _ = try seen.getOrPut(k);

    var unique = try arena.alloc([]const u8, seen.count());
    var i: usize = 0;
    var it = seen.keyIterator();
    while (it.next()) |k| : (i += 1) unique[i] = k.*;

    std.mem.sort([]const u8, unique, {}, lexLessThan);
    return unique;
}

fn lexLessThan(_: void, a: []const u8, b: []const u8) bool {
    return std.mem.order(u8, a, b) == .lt;
}

/// Build the set of lookup keys for an incoming search query. Single
/// query produces ≤ MAX_DISPLAY_TOKENS+2 keys (handle prefix + exact +
/// up to MAX_DISPLAY_TOKENS display-token prefixes).
///
/// Strategy for a query like "fig aka":
///   - h:fig%20aka     (handle prefix with the literal normalized query)
///   - e:fig%20aka     (exact handle match)
///   - n:fig, n:aka    (token-prefix keys, one per query token)
///
/// Note: `h:` here uses the FULL query string including internal spaces.
/// That intentionally won't match any handle (handles don't have spaces),
/// but matters for compositional clarity — the serving layer can choose
/// to skip h:/e: keys when the query has whitespace.
pub fn queryKeys(arena: Allocator, normalized_query: []const u8) ![][]const u8 {
    if (normalized_query.len == 0) return &.{};

    var keys: std.ArrayListUnmanaged([]const u8) = .empty;

    // Single-token queries: h: and e: are real lookups. Multi-token:
    // they typically miss but we emit them anyway for compositional
    // simplicity — empty lookups cost ~nothing.
    const has_space = std.mem.indexOfScalar(u8, normalized_query, ' ') != null;
    if (!has_space) {
        try keys.append(arena, try formatKey(arena, .handle_prefix, normalized_query));
        try keys.append(arena, try formatKey(arena, .exact_handle, normalized_query));
        // domain-suffix lookup: matches accounts under a domain the query names
        // (e.g. `germ.network` → `*.germ.network`, or `germ` → that label).
        try keys.append(arena, try formatKey(arena, .domain_suffix, normalized_query));
    }

    // CJK-aware: a CJK query token yields its bigram set (intersected by the
    // serving layer); a Latin token yields its single full-run key. Same
    // tokenizer the builder uses, so index + query agree.
    const tokens = try displayTokens(arena, normalized_query);
    for (tokens) |tok| try emitTokenKeys(arena, &keys, tok, true);

    return keys.toOwnedSlice(arena);
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

test "formatKey shape" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    try testing.expectEqualStrings("h:nate", try formatKey(arena.allocator(), .handle_prefix, "nate"));
    try testing.expectEqualStrings("n:fig", try formatKey(arena.allocator(), .display_token, "fig"));
    try testing.expectEqualStrings("e:nate.fm", try formatKey(arena.allocator(), .exact_handle, "nate.fm"));
}

test "handlePrefixKeys generates every prefix" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try handlePrefixKeys(arena.allocator(), "nate");
    try testing.expectEqual(@as(usize, 4), keys.len);
    try testing.expectEqualStrings("h:n", keys[0]);
    try testing.expectEqualStrings("h:na", keys[1]);
    try testing.expectEqualStrings("h:nat", keys[2]);
    try testing.expectEqualStrings("h:nate", keys[3]);
}

test "handlePrefixKeys respects PREFIX_MAX" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var long_handle: [40]u8 = undefined;
    @memset(&long_handle, 'a');
    const keys = try handlePrefixKeys(arena.allocator(), &long_handle);
    try testing.expectEqual(PREFIX_MAX, keys.len);
    try testing.expectEqual(@as(usize, 2 + PREFIX_MAX), keys[PREFIX_MAX - 1].len); // "h:" + PREFIX_MAX chars
}

test "handlePrefixKeys on empty handle returns empty" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try handlePrefixKeys(arena.allocator(), "");
    try testing.expectEqual(@as(usize, 0), keys.len);
}

test "displayTokens splits on single space" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const tokens = try displayTokens(arena.allocator(), "fig aka phil");
    try testing.expectEqual(@as(usize, 3), tokens.len);
    try testing.expectEqualStrings("fig", tokens[0]);
    try testing.expectEqualStrings("aka", tokens[1]);
    try testing.expectEqualStrings("phil", tokens[2]);
}

test "displayTokens caps at MAX_DISPLAY_TOKENS" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const tokens = try displayTokens(arena.allocator(), "a b c d e f g h i j k l");
    try testing.expectEqual(MAX_DISPLAY_TOKENS, tokens.len);
    try testing.expectEqualStrings("a", tokens[0]);
    try testing.expectEqualStrings("h", tokens[MAX_DISPLAY_TOKENS - 1]);
}

test "displayPrefixKeys covers every token's prefixes" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try displayPrefixKeys(arena.allocator(), "fig aka");
    // "fig" → 3, "aka" → 3
    try testing.expectEqual(@as(usize, 6), keys.len);
    try testing.expectEqualStrings("n:f", keys[0]);
    try testing.expectEqualStrings("n:fi", keys[1]);
    try testing.expectEqualStrings("n:fig", keys[2]);
    try testing.expectEqualStrings("n:a", keys[3]);
    try testing.expectEqualStrings("n:ak", keys[4]);
    try testing.expectEqualStrings("n:aka", keys[5]);
}

test "deriveAll combines h/n/e for a typical actor" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try deriveAll(arena.allocator(), "nate", "Nate Smith");
    // After normalize: handle="nate", display="nate smith"
    // But this fn assumes inputs ARE normalized — the builder calls
    // normalize first. For this test, we give normalized values directly.
    const keys2 = try deriveAll(arena.allocator(), "nate", "nate smith");
    try testing.expectEqual(@as(usize, 1 + 4 + 4 + 5), keys2.len); // e:nate + 4 h: + 4 n:nate-prefixes + 5 n:smith-prefixes
    _ = keys;

    // Spot check
    var found_exact = false;
    var found_h_nate = false;
    var found_n_smith = false;
    for (keys2) |k| {
        if (std.mem.eql(u8, k, "e:nate")) found_exact = true;
        if (std.mem.eql(u8, k, "h:nate")) found_h_nate = true;
        if (std.mem.eql(u8, k, "n:smith")) found_n_smith = true;
    }
    try testing.expect(found_exact);
    try testing.expect(found_h_nate);
    try testing.expect(found_n_smith);
}

test "deriveAll handles empty display name" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try deriveAll(arena.allocator(), "nate", "");
    // 1 exact + 4 handle prefixes; no display tokens.
    try testing.expectEqual(@as(usize, 5), keys.len);
}

test "deriveAll handles empty handle" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try deriveAll(arena.allocator(), "", "John Doe");
    // No e:, no h:; display tokens only ("john"=4, "doe"=3)
    // But wait — "John Doe" isn't normalized; we'd need lowercase.
    // Caller is responsible for normalizing before calling deriveAll.
    // Treating literal as normalized for this test:
    _ = keys;
    const keys2 = try deriveAll(arena.allocator(), "", "john doe");
    try testing.expectEqual(@as(usize, 4 + 3), keys2.len);
}

test "queryKeys: single token" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try queryKeys(arena.allocator(), "nate");
    try testing.expectEqual(@as(usize, 4), keys.len); // h:, e:, d:, n:
    try testing.expectEqualStrings("h:nate", keys[0]);
    try testing.expectEqualStrings("e:nate", keys[1]);
    try testing.expectEqualStrings("d:nate", keys[2]);
    try testing.expectEqualStrings("n:nate", keys[3]);
}

test "domainSuffixKeys: prefixes of each dot-boundary suffix" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try domainSuffixKeys(arena.allocator(), "foo.germ.network");
    // suffixes: "germ.network" (12 prefixes) + "network" (7 prefixes) = 19
    try testing.expectEqual(@as(usize, 19), keys.len);
    var has_germ = false;
    var has_germ_network = false;
    var has_network = false;
    var has_full = false;
    for (keys) |k| {
        if (std.mem.eql(u8, k, "d:germ")) has_germ = true;
        if (std.mem.eql(u8, k, "d:germ.network")) has_germ_network = true;
        if (std.mem.eql(u8, k, "d:network")) has_network = true;
        if (std.mem.eql(u8, k, "d:foo.germ.network")) has_full = true;
    }
    try testing.expect(has_germ); // searchable by domain label prefix
    try testing.expect(has_germ_network); // searchable by full domain suffix
    try testing.expect(has_network); // searchable by TLD-ish label
    try testing.expect(!has_full); // full handle excluded (h:/e: cover it)
}

test "domainSuffixKeys: no dots → no keys" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try domainSuffixKeys(arena.allocator(), "nodots");
    try testing.expectEqual(@as(usize, 0), keys.len);
}

test "deriveAll for a domain handle is findable by its domain suffix" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try deriveAll(arena.allocator(), "foo.germ.network", "");
    var has_d_germ_network = false;
    for (keys) |k| if (std.mem.eql(u8, k, "d:germ.network")) {
        has_d_germ_network = true;
    };
    try testing.expect(has_d_germ_network);
}

test "queryKeys: multi-token query skips h:/e: (would never match)" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try queryKeys(arena.allocator(), "fig aka");
    // Only n: keys for each token, no h:/e: since query has whitespace.
    try testing.expectEqual(@as(usize, 2), keys.len);
    try testing.expectEqualStrings("n:fig", keys[0]);
    try testing.expectEqualStrings("n:aka", keys[1]);
}

test "queryKeys: empty query returns empty" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try queryKeys(arena.allocator(), "");
    try testing.expectEqual(@as(usize, 0), keys.len);
}

test "deriveAll output is sane for unicode-normalized input" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();

    // simulate the indexer flow: caller normalizes via the per-field
    // entry point, then derives.
    const handle = try normalize.handle(arena.allocator(), "Éric.fm");
    const display = try normalize.display(arena.allocator(), "Éric Müller");

    try testing.expectEqualStrings("eric.fm", handle);
    try testing.expectEqualStrings("eric muller", display);

    const keys = try deriveAll(arena.allocator(), handle, display);

    var has_h_eric = false;
    var has_n_muller = false;
    for (keys) |k| {
        if (std.mem.eql(u8, k, "h:eric")) has_h_eric = true;
        if (std.mem.eql(u8, k, "n:muller")) has_n_muller = true;
    }
    try testing.expect(has_h_eric);
    try testing.expect(has_n_muller);
}

test "deriveAll on display 'fig fig' emits duplicate n:fig keys (by design)" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try deriveAll(arena.allocator(), "", "fig fig");
    // "fig" tokenized twice → 3 prefixes × 2 = 6 n: keys
    try testing.expectEqual(@as(usize, 6), keys.len);

    var n_fig_count: usize = 0;
    for (keys) |k| if (std.mem.eql(u8, k, "n:fig")) {
        n_fig_count += 1;
    };
    try testing.expectEqual(@as(usize, 2), n_fig_count);
}

test "deriveAllUnique returns sorted deduped slice" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try deriveAllUnique(arena.allocator(), "alice", "alice the great");
    // duplicates (e.g. n:a appears from "alice", "the", and any other a-word) collapse.

    // Verify no duplicates
    for (keys, 0..) |k, i| {
        for (keys[i + 1 ..]) |other| {
            try testing.expect(!std.mem.eql(u8, k, other));
        }
    }

    // Verify sorted
    var prev: []const u8 = "";
    for (keys) |k| {
        try testing.expect(std.mem.order(u8, prev, k) == .lt);
        prev = k;
    }

    // Sanity: expected keys are present
    var has_e_alice = false;
    var has_h_a = false;
    var has_n_great = false;
    for (keys) |k| {
        if (std.mem.eql(u8, k, "e:alice")) has_e_alice = true;
        if (std.mem.eql(u8, k, "h:a")) has_h_a = true;
        if (std.mem.eql(u8, k, "n:great")) has_n_great = true;
    }
    try testing.expect(has_e_alice);
    try testing.expect(has_h_a);
    try testing.expect(has_n_great);
}

test "deriveAllUnique enables clean set-diff for overlay rename" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();

    // Actor renamed from "alice.fm" → "bob.fm". Overlay needs to suppress
    // every "h:alic*" / "e:alice.fm" key the actor used to occupy.
    const old_keys = try deriveAllUnique(arena.allocator(), "alice.fm", "");
    const new_keys = try deriveAllUnique(arena.allocator(), "bob.fm", "");

    // Build "removed" = old - new via simple intersection scan (both sorted).
    var removed: std.ArrayListUnmanaged([]const u8) = .empty;
    var oi: usize = 0;
    var ni: usize = 0;
    while (oi < old_keys.len) {
        if (ni == new_keys.len) {
            try removed.append(arena.allocator(), old_keys[oi]);
            oi += 1;
            continue;
        }
        const cmp = std.mem.order(u8, old_keys[oi], new_keys[ni]);
        switch (cmp) {
            .lt => {
                try removed.append(arena.allocator(), old_keys[oi]);
                oi += 1;
            },
            .eq => {
                oi += 1;
                ni += 1;
            },
            .gt => ni += 1,
        }
    }

    // Removed should include all h:alic* prefixes and e:alice.fm.
    var has_h_alice = false;
    var has_e_alicefm = false;
    for (removed.items) |k| {
        if (std.mem.eql(u8, k, "h:alice")) has_h_alice = true;
        if (std.mem.eql(u8, k, "e:alice.fm")) has_e_alicefm = true;
    }
    try testing.expect(has_h_alice);
    try testing.expect(has_e_alicefm);
}

// ─── v2: CJK / Japanese ────────────────────────────────────────────────────

fn keySet(arena: Allocator, keys: [][]const u8) !std.StringHashMap(void) {
    var s = std.StringHashMap(void).init(arena);
    for (keys) |k| try s.put(k, {});
    return s;
}

test "v2: displayPrefixKeys bigrams a CJK run + leading unigram" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const keys = try displayPrefixKeys(arena.allocator(), "日本太郎");
    // bigrams 日本, 本太, 太郎 + leading unigram 日 → 4 keys
    try testing.expectEqual(@as(usize, 4), keys.len);
    var s = try keySet(arena.allocator(), keys);
    try testing.expect(s.contains("n:日本"));
    try testing.expect(s.contains("n:本太"));
    try testing.expect(s.contains("n:太郎"));
    try testing.expect(s.contains("n:日"));
}

test "v2: queryKeys bigrams a CJK query" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var s = try keySet(arena.allocator(), try queryKeys(arena.allocator(), "佐藤太郎"));
    try testing.expect(s.contains("n:佐藤"));
    try testing.expect(s.contains("n:藤太"));
    try testing.expect(s.contains("n:太郎"));
}

test "v2: single CJK char query → leading unigram only" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var s = try keySet(arena.allocator(), try queryKeys(arena.allocator(), "日"));
    try testing.expect(s.contains("n:日"));
}

test "v2: mixed latin+cjk token segments into runs" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var s = try keySet(arena.allocator(), try displayPrefixKeys(arena.allocator(), "tokyo東京"));
    try testing.expect(s.contains("n:t")); // latin prefix
    try testing.expect(s.contains("n:tokyo")); // latin full
    try testing.expect(s.contains("n:東京")); // cjk bigram
    try testing.expect(s.contains("n:東")); // cjk leading unigram
}

test "v2: query bigrams are a subset of indexed keys (builder/query agree)" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var idx = try keySet(arena.allocator(), try displayPrefixKeys(arena.allocator(), "佐藤太郎"));
    // a partial query "佐藤太" must produce only n: keys present in the index
    const q = try queryKeys(arena.allocator(), "佐藤太");
    for (q) |k| {
        if (std.mem.startsWith(u8, k, "n:")) try testing.expect(idx.contains(k));
    }
}
