//! The store role's configuration. All of it comes from the environment,
//! and this is the whole list:
//!
//! | variable                 | required | meaning                                                        |
//! |--------------------------|----------|----------------------------------------------------------------|
//! | `MODE`                   | yes      | `store` selects this role                                      |
//! | `STORE_DIR`              | yes      | directory of the zkv store, on the machine's volume            |
//! | `STORE_READ_SECRET`      | no       | bearer secret for `/store/*`; unset, those endpoints are off   |
//! | `RELAY_BOOTSTRAP_CURSOR` | no       | relay seq to start from when the store holds no relay cursor   |
//! | `PORT`                   | no       | HTTP port, default 8080                                        |
//! | `LOGFIRE_TOKEN`          | no       | read by logfire; unset, nothing is exported                    |
//! | `STORE_OFFLINE`          | no       | `1` opens the store and serves HTTP without any upstream       |
//! | `INDEX_ADMIN_URL`        | no       | the co-located index's admin listener, a loopback URL          |
//! | `INDEX_ADMIN_SECRET`     | no       | that listener's bearer secret                                  |
//! | `INDEX_SOURCE_ID`        | no       | names this store to the index; change it when the store is rebuilt |
//!
//! The index bridge runs when all three `INDEX_*` variables are set, and
//! setting only some is an error. The role reads nothing for Turso or the
//! Worker and opens no connection to either.

const std = @import("std");
const ingest = @import("../ingest.zig");

pub const Config = struct {
    store_dir: []const u8,
    read_secret: ?[]const u8 = null,
    bootstrap_cursor: ?i64 = null,
    offline: bool = false,
    index: ?Index = null,
};

pub const Index = struct {
    admin_url: []const u8,
    admin_secret: []const u8,
    source_id: []const u8,
};

pub const Error = error{ StoreDirRequired, InvalidBootstrapCursor, EmptyReadSecret, IncompleteIndexConfig, IndexNotLoopback };

/// The bridge sends the admin secret, so the index must be on this machine.
pub fn loopback(url: []const u8) bool {
    for ([_][]const u8{ "http://127.0.0.1:", "http://localhost:", "http://[::1]:" }) |prefix| {
        if (std.mem.startsWith(u8, url, prefix) and url.len > prefix.len) return true;
    }
    return false;
}

pub const Getenv = *const fn (name: [*:0]const u8) ?[]const u8;

pub fn load(getenv: Getenv) Error!Config {
    var config: Config = .{ .store_dir = getenv("STORE_DIR") orelse return error.StoreDirRequired };
    if (config.store_dir.len == 0) return error.StoreDirRequired;
    if (getenv("STORE_READ_SECRET")) |secret| {
        if (secret.len == 0) return error.EmptyReadSecret;
        config.read_secret = secret;
    }
    if (getenv("RELAY_BOOTSTRAP_CURSOR")) |text| {
        const parsed = std.fmt.parseInt(i64, text, 10) catch return error.InvalidBootstrapCursor;
        config.bootstrap_cursor = ingest.plausibleRelayCursor(parsed) orelse return error.InvalidBootstrapCursor;
    }
    if (getenv("STORE_OFFLINE")) |text| config.offline = std.mem.eql(u8, text, "1");
    const url = getenv("INDEX_ADMIN_URL");
    const secret = getenv("INDEX_ADMIN_SECRET");
    const id = getenv("INDEX_SOURCE_ID");
    if (url != null or secret != null or id != null) {
        if (url == null or secret == null or id == null or secret.?.len == 0 or id.?.len == 0) return error.IncompleteIndexConfig;
        if (!loopback(url.?)) return error.IndexNotLoopback;
        config.index = .{ .admin_url = std.mem.trimEnd(u8, url.?, "/"), .admin_secret = secret.?, .source_id = id.? };
    }
    return config;
}

/// Where the relay subscription starts: the stored cursor when there is
/// one, else the configured bootstrap. Null means the role must not start
/// live, because it would skip whatever it never replayed.
pub fn resumeCursor(stored: ?u64, bootstrap: ?i64) ?i64 {
    if (stored) |cursor| return ingest.plausibleRelayCursor(std.math.cast(i64, cursor));
    return bootstrap;
}

const testing = std.testing;

fn fakeEnv(comptime pairs: []const [2][]const u8) Getenv {
    return struct {
        fn get(name: [*:0]const u8) ?[]const u8 {
            inline for (pairs) |pair| if (std.mem.eql(u8, std.mem.span(name), pair[0])) return pair[1];
            return null;
        }
    }.get;
}

test "the store directory is required and the rest is optional" {
    try testing.expectError(error.StoreDirRequired, load(fakeEnv(&.{})));
    try testing.expectError(error.StoreDirRequired, load(fakeEnv(&.{.{ "STORE_DIR", "" }})));
    const bare = try load(fakeEnv(&.{.{ "STORE_DIR", "/data/store" }}));
    try testing.expectEqualStrings("/data/store", bare.store_dir);
    try testing.expect(bare.read_secret == null and bare.bootstrap_cursor == null and !bare.offline);

    const full = try load(fakeEnv(&.{ .{ "STORE_DIR", "/data/store" }, .{ "STORE_READ_SECRET", "s3cret" }, .{ "RELAY_BOOTSTRAP_CURSOR", "32234719514" }, .{ "STORE_OFFLINE", "1" } }));
    try testing.expectEqualStrings("s3cret", full.read_secret.?);
    try testing.expectEqual(@as(?i64, 32_234_719_514), full.bootstrap_cursor);
    try testing.expect(full.offline);
}

test "a bootstrap cursor that is not a relay seq is refused" {
    for ([_][]const u8{ "", "abc", "0", "-5", "1785217869995374" }) |text| {
        const env = struct {
            var value: []const u8 = "";
            fn get(name: [*:0]const u8) ?[]const u8 {
                if (std.mem.eql(u8, std.mem.span(name), "STORE_DIR")) return "/data/store";
                if (std.mem.eql(u8, std.mem.span(name), "RELAY_BOOTSTRAP_CURSOR")) return value;
                return null;
            }
        };
        env.value = text;
        try testing.expectError(error.InvalidBootstrapCursor, load(env.get));
    }
    try testing.expectError(error.EmptyReadSecret, load(fakeEnv(&.{ .{ "STORE_DIR", "/d" }, .{ "STORE_READ_SECRET", "" } })));
}

test "the stored cursor wins, and with neither the role does not start live" {
    try testing.expectEqual(@as(?i64, 900), resumeCursor(900, 5));
    try testing.expectEqual(@as(?i64, 5), resumeCursor(null, 5));
    try testing.expectEqual(@as(?i64, null), resumeCursor(null, null));
    try testing.expectEqual(@as(?i64, null), resumeCursor(1_785_217_869_995_374, 5));
}

test "the index bridge needs its whole configuration and a loopback index" {
    const base = [2][]const u8{ "STORE_DIR", "/d" };
    try testing.expect((try load(fakeEnv(&.{base}))).index == null);
    try testing.expectError(error.IncompleteIndexConfig, load(fakeEnv(&.{ base, .{ "INDEX_ADMIN_URL", "http://127.0.0.1:8081" } })));
    try testing.expectError(error.IncompleteIndexConfig, load(fakeEnv(&.{ base, .{ "INDEX_ADMIN_URL", "http://127.0.0.1:8081" }, .{ "INDEX_ADMIN_SECRET", "" }, .{ "INDEX_SOURCE_ID", "a" } })));
    try testing.expectError(error.IndexNotLoopback, load(fakeEnv(&.{ base, .{ "INDEX_ADMIN_URL", "https://index.example:8081" }, .{ "INDEX_ADMIN_SECRET", "s" }, .{ "INDEX_SOURCE_ID", "a" } })));
    try testing.expectError(error.IndexNotLoopback, load(fakeEnv(&.{ base, .{ "INDEX_ADMIN_URL", "http://127.0.0.1.evil.example:80" }, .{ "INDEX_ADMIN_SECRET", "s" }, .{ "INDEX_SOURCE_ID", "a" } })));
    const index = (try load(fakeEnv(&.{ base, .{ "INDEX_ADMIN_URL", "http://127.0.0.1:8081/" }, .{ "INDEX_ADMIN_SECRET", "s" }, .{ "INDEX_SOURCE_ID", "slot-a-1" } }))).index.?;
    try testing.expectEqualStrings("http://127.0.0.1:8081", index.admin_url);
    try testing.expectEqualStrings("slot-a-1", index.source_id);
}
