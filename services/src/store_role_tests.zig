//! Test root for the store role. Rooted at src/ so the role's files can
//! reach the store module and the worker role's shared pieces.

test {
    _ = @import("store_process/handle_policy.zig");
    _ = @import("store_process/events.zig");
    _ = @import("store_process/owner.zig");
    _ = @import("store_process/testing.zig");
    _ = @import("store_process/owner_test.zig");
    _ = @import("store_process/events_test.zig");
    _ = @import("store_process/producers_test.zig");
    _ = @import("store_process/http_test.zig");
}
