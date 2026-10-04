//! Query / handle / display-name normalization for the prefix index.
//!
//! THIS MODULE IS LOAD-BEARING: the indexer (offline) and the serving
//! path (online) MUST call the same fns and produce identical output for
//! identical input. The index_meta table records `normalize_version`;
//! the serving snapshot loader refuses snapshots whose recorded version
//! disagrees with the running binary's `VERSION` constant.
//!
//! Bump `VERSION` whenever any byte of output can change for any input.
//!
//! Three entry points, different rules:
//!
//!   query(arena, raw)
//!     For incoming /search?q=… input. Permissive: keeps `_` because
//!     someone may type a handle with `_` (rare but harmless), and used
//!     against both `h:` and `n:` keys.
//!
//!   handle(arena, raw)
//!     For actor.handle fields at index time. Strict: drops `_`, since
//!     atproto handles are spec'd as `[a-z0-9.-]` (RFC-3986-ish subset).
//!     A handle containing `_` is malformed; we don't index those.
//!
//!   display(arena, raw)
//!     For actor.display_name fields at index time. Permissive: keeps
//!     `_` because display names commonly have it ("alice_b") and we
//!     tokenize on whitespace later anyway.
//!
//! Scope (v2): ASCII letters/digits + a hand-rolled fold table for
//! Latin-1 Supplement (U+0080..U+00FF) and Latin Extended-A
//! (U+0100..U+017F), PLUS Japanese/CJK: Han, Hiragana, and Katakana
//! (folded to Hiragana), with NFKC-lite width folding (full-width ASCII →
//! ASCII, ideographic/middle-dot → space). CJK codepoints are PRESERVED
//! inline in the output; the prefix tokenizer (prefixes.zig) segments
//! script runs and bigrams CJK runs. See docs/i18n-cjk-search.md.
//!
//! STILL DROPPED: other non-Latin scripts (Arabic, Hebrew, Hangul, Thai…),
//! emoji, symbols, combining marks. Known gap: half-width katakana is kept
//! as-is (not cross-form folded to full-width). These are later versions.
//!
//! LOAD-BEARING: bumping the supported set or any fold changes `VERSION`,
//! which forces a full re-index (the snapshot loader rejects a snapshot
//! whose recorded normalize_version disagrees with the running binary).
//!
//! Output character sets:
//!   query():   a-z 0-9 .  -  _   plus single ' ' between tokens
//!   handle():  a-z 0-9 .  -          (no `_`, no whitespace allowed)
//!   display(): a-z 0-9 .  -  _   plus single ' ' between tokens

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const VERSION: []const u8 = "v2-cjk-bigram";

/// Hard cap on normalized output length. Atproto handles are ≤253 chars;
/// display names are ≤64 codepoints (≤256 bytes UTF-8). Queries arrive
/// over HTTP and are already URL-bounded. 256 is generous.
pub const MAX_OUTPUT: usize = 256;

const CharClass = enum {
    letter_or_digit, // a-z 0-9 — token-internal
    handle_internal, // . - — part of handle, doesn't break display tokens
    underscore, // _ — kept by query()/display(), dropped by handle()
    separator, // whitespace or non-handle punctuation — collapses to single space
    drop, // controls, unknown bytes — skip
};

fn classifyAscii(c: u8) CharClass {
    return switch (c) {
        'a'...'z', '0'...'9' => .letter_or_digit,
        '.', '-' => .handle_internal,
        '_' => .underscore,
        ' ', '\t', '\n', '\r' => .separator,
        // Most ASCII punctuation acts as a token separator. This is how
        // "Fig (aka:[phil])" → "fig aka phil" — parens, colons, brackets
        // disappear into single spaces.
        '(', ')', '[', ']', '{', '}', ',', ';', ':', '/', '\\', '|', '!', '?', '"', '\'', '`', '@', '#', '$', '%', '^', '&', '*', '+', '=', '<', '>', '~' => .separator,
        else => .drop,
    };
}

/// What policy a given normalization entry point applies to the
/// underscore character.
const UnderscorePolicy = enum {
    keep, // query()/display() — token-internal
    drop, // handle() — atproto handles are [a-z0-9.-]
};

const Folded = struct {
    bytes: [4]u8 = .{ 0, 0, 0, 0 }, // up to 4 ASCII bytes (Æ→ae, ß→ss, etc.)
    len: u8 = 0,
};

/// Fold a single codepoint to up to 4 lowercase ASCII bytes. Empty
/// return (len=0) means "drop this codepoint."
fn foldCodepoint(cp: u21) Folded {
    var f: Folded = .{};

    // ASCII fast path
    if (cp < 0x80) {
        const c: u8 = @intCast(cp);
        f.bytes[0] = if (c >= 'A' and c <= 'Z') c + 32 else c;
        f.len = 1;
        return f;
    }

    // Combining marks: drop. Covers Unicode blocks Combining Diacritical
    // Marks (U+0300..U+036F), Extended (U+1AB0..U+1AFF), Supplement
    // (U+1DC0..U+1DFF), for Symbols (U+20D0..U+20FF), Half Marks (U+FE20..U+FE2F).
    if (cp >= 0x0300 and cp <= 0x036F) return f;
    if (cp >= 0x1AB0 and cp <= 0x1AFF) return f;
    if (cp >= 0x1DC0 and cp <= 0x1DFF) return f;
    if (cp >= 0x20D0 and cp <= 0x20FF) return f;
    if (cp >= 0xFE20 and cp <= 0xFE2F) return f;

    // Latin-1 Supplement + Latin Extended-A folds.
    return switch (cp) {
        // Latin-1 Supplement
        0x00C0...0x00C5, 0x00E0...0x00E5 => one('a'),
        0x00C6, 0x00E6 => two('a', 'e'), // Æ æ
        0x00C7, 0x00E7 => one('c'), // Ç ç
        0x00C8...0x00CB, 0x00E8...0x00EB => one('e'),
        0x00CC...0x00CF, 0x00EC...0x00EF => one('i'),
        0x00D0, 0x00F0 => one('d'), // Ð ð (Eth)
        0x00D1, 0x00F1 => one('n'), // Ñ ñ
        0x00D2...0x00D6, 0x00F2...0x00F6, 0x00D8, 0x00F8 => one('o'),
        0x00D9...0x00DC, 0x00F9...0x00FC => one('u'),
        0x00DD, 0x00FD, 0x00FF => one('y'),
        0x00DF => two('s', 's'), // ß
        0x00DE, 0x00FE => two('t', 'h'), // Þ þ (Thorn)
        // Latin Extended-A (covers Polish, Czech, Hungarian, Turkish, etc.)
        0x0100...0x0105 => one('a'),
        0x0106...0x010D => one('c'),
        0x010E...0x0111 => one('d'),
        0x0112...0x011B => one('e'),
        0x011C...0x0123 => one('g'),
        0x0124, 0x0125, 0x0126, 0x0127 => one('h'),
        0x0128...0x0131 => one('i'),
        0x0132, 0x0133 => two('i', 'j'), // Ĳ ĳ
        0x0134, 0x0135 => one('j'),
        0x0136, 0x0137, 0x0138 => one('k'),
        0x0139...0x0142 => one('l'),
        0x0143...0x014B => one('n'),
        0x014C...0x0151 => one('o'),
        0x0152, 0x0153 => two('o', 'e'), // Œ œ
        0x0154...0x0159 => one('r'),
        0x015A...0x0161 => one('s'),
        0x0162...0x0167 => one('t'),
        0x0168...0x0173 => one('u'),
        0x0174, 0x0175 => one('w'),
        0x0176...0x0178 => one('y'),
        0x0179...0x017E => one('z'),
        // Everything else (CJK, Arabic, emoji, symbols, ...): drop.
        else => f,
    };
}

fn one(c: u8) Folded {
    return .{ .bytes = .{ c, 0, 0, 0 }, .len = 1 };
}
fn two(a: u8, b: u8) Folded {
    return .{ .bytes = .{ a, b, 0, 0 }, .len = 2 };
}

/// Normalize for incoming search queries. Permissive: keeps `_` for
/// queries like "alice_b". Whitespace is preserved as a single space
/// (the serving path uses this to detect multi-token queries).
pub fn query(arena: Allocator, raw: []const u8) ![]const u8 {
    return normalizeWith(arena, raw, .keep);
}

/// Normalize for actor.display_name at index time. Same rules as query():
/// keeps `_`, collapses internal whitespace, drops non-Latin scripts.
pub fn display(arena: Allocator, raw: []const u8) ![]const u8 {
    return normalizeWith(arena, raw, .keep);
}

/// Normalize for actor.handle at index time. Strict: drops `_` so the
/// output matches the atproto handle character set `[a-z0-9.-]`. A
/// malformed handle containing `_` will have it stripped here — the
/// resulting prefix keys still represent the actor as best we can.
pub fn handle(arena: Allocator, raw: []const u8) ![]const u8 {
    return normalizeWith(arena, raw, .drop);
}

// ─── v2: CJK (Japanese) support ────────────────────────────────────────────
// Checked BEFORE the Latin fold path in normalizeWith. The Latin fold table
// above is unchanged; CJK/width handling lives here so the two stay decoupled.

/// CJK codepoints we index. Han is preserved; katakana is folded to hiragana
/// (so サトウ and さとう collide); hiragana preserved. Returns the canonical
/// codepoint to emit, or null if `cp` is not CJK. NOTE: half-width katakana is
/// preserved as-is (not cross-form folded) — a known v2 gap, rare in names.
fn cjkFold(cp: u21) ?u21 {
    if (cp >= 0x3041 and cp <= 0x3096) return cp; // hiragana
    if (cp >= 0x30A1 and cp <= 0x30F6) return cp - 0x60; // katakana → hiragana
    if (cp == 0x30FC) return cp; // ー prolonged sound mark (shared by kana)
    if (cp >= 0xFF66 and cp <= 0xFF9D) return cp; // half-width katakana (as-is)
    if (cp >= 0x3400 and cp <= 0x4DBF) return cp; // CJK Ext A
    if (cp >= 0x4E00 and cp <= 0x9FFF) return cp; // CJK Unified Ideographs
    if (cp >= 0xF900 and cp <= 0xFAFF) return cp; // CJK Compatibility Ideographs
    return null;
}

/// Full-width / ideographic forms that fold to a single ASCII byte (handled
/// before CJK + Latin). Full-width `Ａ-Ｚ０-９！…` → ASCII; ideographic and
/// katakana middle-dot spaces → ' ' (a separator). Returns the raw ASCII byte
/// (not yet lowercased) or null.
fn widthFoldAscii(cp: u21) ?u8 {
    if (cp >= 0xFF01 and cp <= 0xFF5E) return @intCast(cp - 0xFEE0); // full-width ASCII
    if (cp == 0x3000) return ' '; // ideographic space
    if (cp == 0x30FB or cp == 0xFF65) return ' '; // ・ middle dot → token separator
    return null;
}

/// Classify + append one already-lowercased ASCII byte through the shared
/// separator/letter logic. Returns false when MAX_OUTPUT is reached (stop).
/// Used by both the width-fold path and the Latin fold loop — one code path,
/// no "almost-same" duplication.
fn emitAscii(
    out: *std.ArrayListUnmanaged(u8),
    c: u8,
    underscore: UnderscorePolicy,
    pending_space: *bool,
    has_non_space: *bool,
) bool {
    if (out.items.len >= MAX_OUTPUT) return false;
    const cls = classifyAscii(c);
    const effective: CharClass = switch (cls) {
        .underscore => switch (underscore) {
            .keep => .letter_or_digit,
            .drop => .drop,
        },
        else => cls,
    };
    switch (effective) {
        .drop => {},
        .separator => {
            if (has_non_space.*) pending_space.* = true;
        },
        .letter_or_digit, .handle_internal => {
            if (pending_space.*) {
                out.appendAssumeCapacity(' ');
                pending_space.* = false;
            }
            out.appendAssumeCapacity(c);
            has_non_space.* = true;
        },
        .underscore => unreachable, // resolved above
    }
    return true;
}

fn normalizeWith(arena: Allocator, raw: []const u8, underscore: UnderscorePolicy) ![]const u8 {
    var out: std.ArrayListUnmanaged(u8) = .empty;
    // +8 slack so a multi-byte CJK codepoint appended at the MAX_OUTPUT
    // boundary still fits before the next-iteration break.
    try out.ensureTotalCapacityPrecise(arena, @min(raw.len + 4, MAX_OUTPUT + 8));

    var pending_space = false; // becomes true when a separator runs; emitted lazily
    var has_non_space = false; // leading separators get dropped

    var i: usize = 0;
    while (i < raw.len) {
        if (out.items.len >= MAX_OUTPUT) break;

        const cp_len = std.unicode.utf8ByteSequenceLength(raw[i]) catch {
            i += 1;
            continue;
        };
        if (i + cp_len > raw.len) break;
        const cp = std.unicode.utf8Decode(raw[i .. i + cp_len]) catch {
            i += cp_len;
            continue;
        };
        i += cp_len;

        // 1. full-width / ideographic → ASCII, then shared classify path.
        if (widthFoldAscii(cp)) |raw_c| {
            const c = if (raw_c >= 'A' and raw_c <= 'Z') raw_c + 32 else raw_c;
            if (!emitAscii(&out, c, underscore, &pending_space, &has_non_space)) break;
            continue;
        }

        // 2. CJK: preserve (katakana already folded to hiragana). Emitted as
        //    its UTF-8 bytes — letter-class, lazy leading space like ASCII.
        if (cjkFold(cp)) |kept| {
            var buf: [4]u8 = undefined;
            const n = std.unicode.utf8Encode(kept, &buf) catch continue;
            if (pending_space) {
                out.appendAssumeCapacity(' ');
                pending_space = false;
            }
            for (buf[0..n]) |b| out.appendAssumeCapacity(b);
            has_non_space = true;
            continue;
        }

        // 3. Latin / ASCII fold path (table above, unchanged).
        const folded = foldCodepoint(cp);
        for (folded.bytes[0..folded.len]) |c| {
            if (!emitAscii(&out, c, underscore, &pending_space, &has_non_space)) break;
        }
    }

    return out.items;
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

fn expectNorm(input: []const u8, expected: []const u8) !void {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const got = try query(arena.allocator(), input);
    try testing.expectEqualStrings(expected, got);
}

fn expectHandle(input: []const u8, expected: []const u8) !void {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const got = try handle(arena.allocator(), input);
    try testing.expectEqualStrings(expected, got);
}

test "ascii lowercase" {
    try expectNorm("Nate", "nate");
    try expectNorm("HELLO", "hello");
    try expectNorm("MixedCase", "mixedcase");
}

test "handle internal chars preserved" {
    try expectNorm("nate.fm", "nate.fm");
    try expectNorm("jay.bsky.team", "jay.bsky.team");
    try expectNorm("my-handle_v2", "my-handle_v2");
}

test "latin-1 fold" {
    try expectNorm("ÉRIC", "eric");
    try expectNorm("éric", "eric");
    try expectNorm("naïve", "naive");
    try expectNorm("Ñoño", "nono");
    try expectNorm("Müller", "muller");
    try expectNorm("François", "francois");
}

test "ligatures expand" {
    try expectNorm("Æneid", "aeneid");
    try expectNorm("Œuvre", "oeuvre");
    try expectNorm("Straße", "strasse");
}

test "punctuation becomes space separator" {
    try expectNorm("Fig (aka:[phil])", "fig aka phil");
    try expectNorm("hello, world!", "hello world");
    try expectNorm("a/b/c", "a b c");
}

test "leading and trailing separators stripped" {
    try expectNorm("   hello   ", "hello");
    try expectNorm("((nate))", "nate");
    try expectNorm("!!!hi!!!", "hi");
}

test "internal runs of separators collapse to single space" {
    try expectNorm("foo   bar", "foo bar");
    try expectNorm("foo,,,bar", "foo bar");
    try expectNorm("foo , , , bar", "foo bar");
}

test "emoji dropped, CJK preserved (v2)" {
    try expectNorm("nate 👋", "nate"); // emoji still dropped
    try expectNorm("🚀rocket🚀", "rocket");
    try expectNorm("hello 你好", "hello 你好"); // CJK now preserved
}

test "v2: japanese kana + han preserved" {
    try expectNorm("日本", "日本"); // Han preserved
    try expectNorm("さとう", "さとう"); // hiragana preserved
    try expectNorm("佐藤太郎", "佐藤太郎"); // kanji run preserved
}

test "v2: katakana folds to hiragana" {
    try expectNorm("サトウ", "さとう"); // katakana → hiragana (collides with さとう)
    try expectNorm("トーキョー", "とーきょー"); // ー prolonged mark preserved
}

test "v2: full-width ascii folds to ascii" {
    try expectNorm("Ｎａｔｅ", "nate"); // full-width latin → ascii lowercase
    try expectNorm("ｈｅｌｌｏ１２３", "hello123");
}

test "v2: ideographic + middle-dot spaces separate tokens" {
    try expectNorm("ジョン・スミス", "じょん すみす"); // ・ → space; katakana → hiragana
    try expectNorm("東京　タワー", "東京 たわー"); // ideographic space → space
}

test "v2: mixed latin + cjk preserved inline (segmented later)" {
    try expectNorm("Tokyo東京", "tokyo東京"); // no space inserted; tokenizer segments runs
    try expectNorm("東京2020", "東京2020");
}

test "combining marks dropped (pre-composed already folded above)" {
    // "é" can be either U+00E9 (precomposed, folded above) or
    // "e" + U+0301 (combining acute). Both must produce "e".
    try expectNorm("e\u{0301}ric", "eric"); // decomposed é
    try expectNorm("n\u{0303}o", "no"); // decomposed ñ
}

test "empty and whitespace-only inputs" {
    try expectNorm("", "");
    try expectNorm("   ", "");
    try expectNorm("()()", "");
}

test "invalid utf8 bytes skipped" {
    try expectNorm("na\xFFte", "nate");
    try expectNorm("\xC0\xC0hello", "hello");
}

test "output capped at MAX_OUTPUT" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var long_input: [1024]u8 = undefined;
    @memset(&long_input, 'a');
    const got = try query(arena.allocator(), &long_input);
    try testing.expect(got.len <= MAX_OUTPUT);
    try testing.expectEqualSlices(u8, got, &@as([MAX_OUTPUT]u8, @splat('a')));
}

test "idempotence: query(query(x)) == query(x)" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();

    const inputs = [_][]const u8{
        "Nate",
        "ÉRIC",
        "Fig (aka:[phil])",
        "  hello,, world  ",
        "Straße",
        "🚀rocket🚀",
    };
    for (inputs) |input| {
        const first = try query(arena.allocator(), input);
        const second = try query(arena.allocator(), first);
        try testing.expectEqualStrings(first, second);
    }
}

test "handle() drops underscore (atproto spec is [a-z0-9.-])" {
    try expectHandle("alice_bob", "alicebob");
    try expectHandle("a_b_c", "abc");
    try expectHandle("nate.fm", "nate.fm");
    try expectHandle("alex-griss.tech", "alex-griss.tech");
}

test "handle() also lowercase/fold/strip emoji like query()" {
    try expectHandle("Alice.BSKY.social", "alice.bsky.social");
    try expectHandle("éric.fm", "eric.fm");
    try expectHandle("alice🚀.bsky.social", "alice.bsky.social");
}

test "display() keeps underscore (display names commonly have it)" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const got = try display(arena.allocator(), "alice_b");
    try testing.expectEqualStrings("alice_b", got);
}

test "query() keeps underscore (user may type a username with _)" {
    try expectNorm("alice_b", "alice_b");
}

test "version constant is stable" {
    // Don't change this without bumping VERSION and rebuilding the index.
    try testing.expectEqualStrings("v2-cjk-bigram", VERSION);
}
