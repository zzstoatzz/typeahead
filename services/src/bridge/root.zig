//! Index bridge: pushes changed actors from the embedded store into the
//! co-located Tantivy server. See docs/index-bridge.md.

pub const source = @import("source.zig");
pub const wire = @import("wire.zig");

test {
    _ = source;
    _ = wire;
}
