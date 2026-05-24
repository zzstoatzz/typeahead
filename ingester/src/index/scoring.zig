//! Score function for the prefix index.
//!
//! V1: returns the actor's `quality_score` column unchanged. The whole
//! point of this indirection is that Track 2 (follow graph + pagerank)
//! can plug a different function in here without touching the indexer,
//! the overlay updater, or the serving path. The score is a single f32
//! by contract — never call sites assume more than that.

const std = @import("std");

pub const VERSION: []const u8 = "v1-quality-score";

/// Inputs the score function is allowed to consult. Add fields here as
/// the function gets smarter — but never remove them, or you break the
/// builder's assumption that any actor row can be scored.
pub const Inputs = struct {
    quality_score: f32,
};

/// Compute the indexable score for an actor. Builder uses this to order
/// candidates within a prefix bucket; overlay updater uses it when an
/// actor's source row changes; serving merges using this same score.
pub fn scoreOf(in: Inputs) f32 {
    return in.quality_score;
}

// ─── tests ───────────────────────────────────────────────────────────────

const testing = std.testing;

test "scoreOf passes quality_score through" {
    try testing.expectEqual(@as(f32, 900.5), scoreOf(.{ .quality_score = 900.5 }));
    try testing.expectEqual(@as(f32, 0), scoreOf(.{ .quality_score = 0 }));
    try testing.expectEqual(@as(f32, -1.5), scoreOf(.{ .quality_score = -1.5 }));
}

test "version constant is stable" {
    // Bump VERSION whenever scoreOf's output can change for any input.
    // The indexer records this in index_meta; the serving snapshot
    // loader will eventually validate it.
    try testing.expectEqualStrings("v1-quality-score", VERSION);
}
