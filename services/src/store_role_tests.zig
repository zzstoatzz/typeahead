//! Test root for the store role. Rooted at src/ so the role's files can
//! reach the store module and the files beside it.

test {
    _ = @import("store_process/handle_policy.zig");
    _ = @import("store_process/events.zig");
    _ = @import("store_process/owner.zig");
    _ = @import("store_process/testing.zig");
    _ = @import("store_process/owner_test.zig");
    _ = @import("store_process/events_test.zig");
    _ = @import("store_process/producers_test.zig");
    _ = @import("store_process/config.zig");
    _ = @import("store_process/http_test.zig");
    _ = @import("store_process/index_bridge_test.zig");
    _ = @import("store_process/authority_score.zig");
    _ = @import("store_process/profile_test.zig");
    _ = @import("store_process/enrich.zig");
    _ = @import("store_process/enrich_test.zig");
    _ = @import("store_process/corpus.zig");
    _ = @import("store_process/control.zig");
    _ = @import("store_process/control_test.zig");
    _ = @import("store_process/role_test.zig");
    _ = @import("identity_reuse_test.zig");
}
