//! Test root for the store role. Rooted at src/ so the role's files can
//! reach the store module and the worker role's shared pieces.

test {
    _ = @import("store_process/handle_policy.zig");
}
