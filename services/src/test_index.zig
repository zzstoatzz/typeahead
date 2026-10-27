//! Test runner hub. Rooted at src/ so transitive imports like
//! builder.zig → ../db/TursoClient.zig stay inside the module tree.
//!
//! Add new module files (whether under index/ or elsewhere) to the
//! comptime block. `zig build test` picks them up automatically.

comptime {
    _ = @import("index/normalize.zig");
    _ = @import("index/prefixes.zig");
    _ = @import("index/pack.zig");
    _ = @import("index/merge.zig");
    _ = @import("index/index_search.zig");
    _ = @import("index/overlay.zig");
    _ = @import("index/scoring.zig");
    _ = @import("index/schema.zig");
    _ = @import("index/manifest.zig");
    _ = @import("index/promote.zig");
    _ = @import("index/snapshot_state.zig");
    _ = @import("index/promote_runner.zig");
    _ = @import("index/r2.zig");
    _ = @import("index/builder.zig");
    _ = @import("db/sync.zig");
    _ = @import("db/TursoClient.zig");
    _ = @import("http_guard.zig");
}
