//! Unit test hub for sources outside the ingest test root. Rooted at src/ so
//! index/ files stay inside the module tree.

comptime {
    _ = @import("index/normalize.zig");
    _ = @import("index/pds.zig");
    _ = @import("search.zig");
    _ = @import("db/TursoClient.zig");
    _ = @import("http_guard.zig");
}
