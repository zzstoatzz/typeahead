//! Index bridge: pushes changed actors from the embedded store into the
//! co-located Tantivy server. See docs/index-bridge.md.

pub const Bridge = @import("Bridge.zig");
pub const source = @import("source.zig");
pub const wire = @import("wire.zig");
pub const testing = @import("testing.zig");

test {
    _ = Bridge;
    _ = source;
    _ = wire;
    _ = testing;
    _ = @import("tests.zig");
}
