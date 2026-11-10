//! The quality score, a port of `computeAuthorityScore` in `src/utils.ts`:
//!
//!     log10(followers + 1) * 100
//!   + log10(posts + 1) * 20
//!   + 50 when followers > 100, follows > 0 and followers / follows > 10
//!   + 100 more when that ratio is over 100
//!   + 30 when the account is over 365 days old, 30 more over 1095 days
//!   - 100 when it is under one day old, else - 50 under three days
//!
//! floored at zero and rounded to two decimals the way `Math.round` does.
//! The inputs are the three counts, the profile's `createdAt` and the time
//! of the computation. The index stores the result as an f32.
//!
//! `Math.log10` is not correctly rounded and implementations differ in the
//! last bit, so this file carries V8's own (`src/base/ieee754.cc`, the
//! fdlibm `e_log10.c` and `e_log.c`) rather than calling the platform's:
//! the Worker runs on V8, and the same bits come out on every target.

const std = @import("std");

fn words(x: f64) struct { hi: i32, lo: u32 } {
    const bits: u64 = @bitCast(x);
    return .{ .hi = @bitCast(@as(u32, @truncate(bits >> 32))), .lo = @truncate(bits) };
}

fn fromWords(hi: i32, lo: u32) f64 {
    return @bitCast(@as(u64, @as(u32, @bitCast(hi))) << 32 | lo);
}

const two54: f64 = 1.80143985094819840000e+16;

fn log(value: f64) f64 {
    const ln2_hi: f64 = 6.93147180369123816490e-01;
    const ln2_lo: f64 = 1.90821492927058770002e-10;
    const lg1: f64 = 6.666666666666735130e-01;
    const lg2: f64 = 3.999999999940941908e-01;
    const lg3: f64 = 2.857142874366239149e-01;
    const lg4: f64 = 2.222219843214978396e-01;
    const lg5: f64 = 1.818357216161805012e-01;
    const lg6: f64 = 1.531383769920937332e-01;
    const lg7: f64 = 1.479819860511658591e-01;

    var x = value;
    var w0 = words(x);
    var hx = w0.hi;
    var k: i32 = 0;
    if (hx < 0x00100000) {
        if ((hx & 0x7fffffff) | @as(i32, @bitCast(w0.lo)) == 0) return -std.math.inf(f64);
        if (hx < 0) return std.math.nan(f64);
        k -= 54;
        x *= two54;
        w0 = words(x);
        hx = w0.hi;
    }
    if (hx >= 0x7ff00000) return x + x;
    k += (hx >> 20) - 1023;
    hx &= 0x000fffff;
    var i: i32 = (hx + 0x95f64) & 0x100000;
    x = fromWords(hx | (i ^ 0x3ff00000), w0.lo);
    k += i >> 20;
    const f = x - 1.0;
    if ((0x000fffff & (2 + hx)) < 3) {
        if (f == 0) {
            if (k == 0) return 0;
            const dk: f64 = @floatFromInt(k);
            return dk * ln2_hi + dk * ln2_lo;
        }
        const r = f * f * (0.5 - 0.33333333333333333 * f);
        if (k == 0) return f - r;
        const dk: f64 = @floatFromInt(k);
        return dk * ln2_hi - ((r - dk * ln2_lo) - f);
    }
    const s = f / (2.0 + f);
    const dk: f64 = @floatFromInt(k);
    const z = s * s;
    i = hx - 0x6147a;
    const w = z * z;
    const j = 0x6b851 - hx;
    const t1 = w * (lg2 + w * (lg4 + w * lg6));
    const t2 = z * (lg1 + w * (lg3 + w * (lg5 + w * lg7)));
    i |= j;
    const r = t2 + t1;
    if (i > 0) {
        const hfsq = 0.5 * f * f;
        if (k == 0) return f - (hfsq - s * (hfsq + r));
        return dk * ln2_hi - ((hfsq - (s * (hfsq + r) + dk * ln2_lo)) - f);
    }
    if (k == 0) return f - s * (f - r);
    return dk * ln2_hi - ((s * (f - r) - dk * ln2_lo) - f);
}

pub fn log10(value: f64) f64 {
    const ivln10: f64 = 4.34294481903251816668e-01;
    const log10_2hi: f64 = 3.01029995663611771306e-01;
    const log10_2lo: f64 = 3.69423907715893078616e-13;

    var x = value;
    var w = words(x);
    var hx = w.hi;
    var k: i32 = 0;
    if (hx < 0x00100000) {
        if ((hx & 0x7fffffff) | @as(i32, @bitCast(w.lo)) == 0) return -std.math.inf(f64);
        if (hx < 0) return std.math.nan(f64);
        k -= 54;
        x *= two54;
        w = words(x);
        hx = w.hi;
    }
    if (hx >= 0x7ff00000) return x + x;
    if (hx == 0x3ff00000 and w.lo == 0) return 0;
    k += (hx >> 20) - 1023;
    const i: i32 = @intCast(@as(u32, @bitCast(k)) >> 31);
    hx = (hx & 0x000fffff) | ((0x3ff - i) << 20);
    const y: f64 = @floatFromInt(k + i);
    x = fromWords(hx, w.lo);
    const z = y * log10_2lo + ivln10 * log(x);
    return z + y * log10_2hi;
}

/// `Math.round`: the nearest integer, a tie going towards positive infinity.
pub fn jsRound(x: f64) f64 {
    if (!std.math.isFinite(x)) return x;
    const floor = @floor(x);
    return if (x - floor >= 0.5) floor + 1 else floor;
}

pub const Inputs = struct {
    followers: f64,
    follows: f64,
    posts: f64,
    /// Milliseconds since the epoch of the profile's `createdAt`; null when
    /// it is absent or does not parse.
    created_ms: ?i64 = null,
    now_ms: i64,
};

pub fn score(in: Inputs) f64 {
    const f = @max(0, in.followers);
    const fw = @max(0, in.follows);
    const pc = @max(0, in.posts);
    const followers_influence = log10(f + 1) * 100;
    const posts_influence = log10(pc + 1) * 20;
    var ratio_bonus: f64 = 0;
    var mature_bonus: f64 = 0;
    var freshness_penalty: f64 = 0;
    if (f > 100 and fw > 0) {
        const ratio = f / fw;
        if (ratio > 10) ratio_bonus += 50;
        if (ratio > 100) ratio_bonus += 100;
    }
    if (in.created_ms) |created| {
        const age_days = @as(f64, @floatFromInt(in.now_ms - created)) / 86_400_000;
        if (age_days < 1) {
            freshness_penalty = -100;
        } else if (age_days < 3) {
            freshness_penalty = -50;
        }
        if (age_days > 365) mature_bonus += 30;
        if (age_days > 365 * 3) mature_bonus += 30;
    }
    const total = @max(0, followers_influence + posts_influence + ratio_bonus + mature_bonus + freshness_penalty);
    return jsRound(total * 100) / 100;
}

/// The bits the actor record and the index hold.
pub fn stored(value: f64) f32 {
    return @floatCast(value);
}

const testing = std.testing;

test "Math.round sends a tie up" {
    try testing.expectEqual(@as(f64, 3), jsRound(2.5));
    try testing.expectEqual(@as(f64, -2), jsRound(-2.5));
    try testing.expectEqual(@as(f64, 0), jsRound(0.49999999999999994));
    try testing.expectEqual(@as(f64, 1), jsRound(0.5));
    try testing.expectEqual(@as(f64, 2), jsRound(1.5));
    try testing.expectEqual(@as(f64, -1), jsRound(-1.4));
}

test "log10 is exact at the powers of ten the formula meets" {
    try testing.expectEqual(@as(f64, 0), log10(1));
    try testing.expectEqual(@as(f64, 1), log10(10));
    try testing.expectEqual(@as(f64, 2), log10(100));
    try testing.expectEqual(@as(f64, 3), log10(1000));
    try testing.expectEqual(@as(f64, 6), log10(1_000_000));
}
