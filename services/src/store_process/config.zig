//! The store role's configuration. All of it comes from the environment,
//! and this is the whole list:
//!
//! | variable                 | required | meaning                                                        |
//! |--------------------------|----------|----------------------------------------------------------------|
//! | `MODE`                   | yes      | `store` selects this role                                      |
//! | `STORE_DIR`              | yes      | directory of the zkv store, on the machine's volume            |
//! | `STORE_READ_SECRET`      | no       | bearer secret for `/store/*`; unset, those endpoints are off   |
//! | `PORT`                   | no       | HTTP port, default 8080                                        |
//! | `LOGFIRE_TOKEN`          | no       | read by logfire; unset, nothing is exported                    |
//! | `STORE_OFFLINE`          | no       | `1` opens the store and serves HTTP without any upstream       |
//! | `ADMIN_SECRET`           | no       | bearer secret of the co-located index's admin listener; set, the index bridge runs |
//! | `INDEX_ADMIN_URL`        | no       | that listener, a loopback URL; default `http://127.0.0.1:8081` |
//! | `INDEX_SOURCE_ID`        | no       | names this store to the index; default `$FLY_APP_NAME-store`, change it when the store is rebuilt |
//! | `INDEX_ADOPT_CURSOR`     | no       | `1` takes over an index whose cursor another store wrote (a volume restored from the other slot's snapshot), by a full resync |
//! | `INDEX_PUSH_INTERVAL_MS` | no       | rest between pushes to the index while the bridge keeps up, 100 to 30000, default 1000; each push is an index commit, and a change waits at most this long to be read |
//! | `INDEX_RESYNC_AFTER`     | no       | Unix seconds. The bridge rewrites every document in the index once, in a full resync round, unless the last completed round started at or after this time. Set it when an image changes what a document holds; a restart neither repeats the round nor loses it |
//! | `ENRICH`                 | no       | `0` turns profile enrichment off; it is on unless `STORE_OFFLINE` |
//! | `ENRICH_REQUESTS_PER_SECOND` | no   | ceiling on enrichment requests from this process, default 0.5  |
//! | `ENRICH_JITTER`          | no       | each gap between requests is stretched by up to this fraction, default 0.2 |
//! | `ENRICH_WALK_PER_HOUR`   | no       | actors the quality walk queues an hour, default 20000          |
//! | `ENRICH_SWEEP_PER_HOUR`  | no       | actors the rest walk queues an hour while the queue keeps up, default 10000 |
//! | `ENRICH_LEASE_SECONDS`   | no       | how long a leased refresh stays out of the queue, default 300  |
//! | `ENRICH_BACKOFF_SECONDS` | no       | first pause after a failed appview request, doubling, default 30 |
//! | `ENRICH_BACKOFF_MAX_SECONDS` | no   | longest such pause, default 900                                |
//! | `ENRICH_HANDLE_SETTLE_SECONDS` | no | a reported handle that differs from the stored one is ignored for this long after the actor's identity was resolved, default 86400 |
//! | `ENRICH_HANDLE_BACKLOG`  | no       | such a handle queues no identity work while this many identity entries are due, default 100; `0` queues none |
//! | `ENRICH_APPVIEW_URL`     | no       | the appview asked for profiles, default `https://public.api.bsky.app` |
//! | `ENRICH_BLACKSKY_URL`    | no       | the appview asked about accounts Bluesky took down, default `https://api.blacksky.community` |
//! | `ENRICH_FIRST_CHECK_PER_HOUR` | no  | actors the first-check pass queues an hour at most, default 15000; `0` turns the pass off |
//! | `ENRICH_FIRST_CHECK_STAMP` | no     | the check time the pass looks for, Unix seconds, default 0: actors a bundle load left unchecked. On a store whose load stamped every actor, the load time (`profile_checked_at` of any actor not refreshed since) |
//! | `CORPUS_SCAN_SECONDS`    | no       | seconds between corpus counts, default 3600; `0` turns them off. The first is taken 300 s after the start |
//! | `CONTROL_URL`            | no       | the Worker's origin, `https://…`; with `SLOT_CONTROL_SECRET` it turns the control loop on: this slot pulls moderation policy and indexing requests from `/slot/control/*` there (`control.zig`). Unset, or `STORE_OFFLINE`, the loop is off and policy is whatever the store holds |
//! | `SLOT_CONTROL_SECRET`    | no       | bearer secret for those endpoints, at least 32 bytes, the same value the Worker holds. It reads and can change nothing; never the Worker's admin secret |
//! | `CONTROL_POLL_SECONDS`   | no       | seconds between polls, 5 to 3600, default 30; each rest is stretched by up to 20 % |
//! | `CONTROL_REQUESTS_PER_POLL` | no    | indexing requests taken in one poll at most, 1 to 500, default 20: 2,400 an hour at the default interval |
//! | `CONTROL_REQUEST_BACKLOG` | no      | no indexing request is taken while this many identity queue entries are due, default 100; `0` takes none. The requests wait at the Worker |
//!
//! The control loop is the setting whose mistake does not stop the role: one
//! of its two required variables without the other, a URL that is not
//! `https://`, a short secret or a poll interval out of range leaves the loop
//! off, logged at the start and reported as `control.state` in `/health`. A
//! slot that will not start stops feeding its index, which takes search down;
//! a slot without the loop serves with the policy it has.

const std = @import("std");
const ingest = @import("../ingest.zig");
const enrich = @import("enrich.zig");
const control = @import("control.zig");

pub const Config = struct {
    store_dir: []const u8,
    read_secret: ?[]const u8 = null,
    offline: bool = false,
    index: ?Index = null,
    /// Null when enrichment is off.
    enrich: ?enrich.Options = null,
    corpus_scan_seconds: u64 = 3600,
    /// Actors the first-check pass queues an hour; 0 when it is off, as it
    /// is whenever enrichment is. See `owner.Options.first_check_per_hour`.
    first_check_per_hour: u64 = 0,
    first_check_stamp: u64 = 0,
    control: Control = .off,
};

pub const Control = union(enum) {
    off,
    /// The loop was asked for and cannot run; the text says why.
    misconfigured: []const u8,
    on: control.Options,
};

pub const first_check_per_hour = 15_000;

pub const Index = struct {
    admin_url: []const u8,
    admin_secret: []const u8,
    source_id: []const u8,
    adopt_cursor: bool = false,
    /// `bridge.Config.poll_interval_ms`. The ceiling is the bridge's
    /// heartbeat: the index stops serving at 120 s without a watermark.
    push_interval_ms: u64 = 1000,
    /// `bridge.Config.resync_after`.
    resync_after: u64 = 0,
};

pub const default_admin_url = "http://127.0.0.1:8081";
var source_id_buf: [128]u8 = undefined;

pub const Error = error{ StoreDirRequired, EmptyReadSecret, IndexNotLoopback, InvalidIndexPushInterval, InvalidIndexResyncAfter, InvalidEnrichment, InvalidCorpusScan };

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
    if (getenv("STORE_OFFLINE")) |text| config.offline = std.mem.eql(u8, text, "1");
    if (getenv("ADMIN_SECRET")) |secret| if (secret.len > 0) {
        const url = getenv("INDEX_ADMIN_URL") orelse default_admin_url;
        if (!loopback(url)) return error.IndexNotLoopback;
        config.index = .{
            .admin_url = std.mem.trimEnd(u8, url, "/"),
            .admin_secret = secret,
            .source_id = getenv("INDEX_SOURCE_ID") orelse (std.fmt.bufPrint(&source_id_buf, "{s}-store", .{getenv("FLY_APP_NAME") orelse "local"}) catch "local-store"),
            .adopt_cursor = if (getenv("INDEX_ADOPT_CURSOR")) |text| std.mem.eql(u8, text, "1") else false,
        };
        if (getenv("INDEX_PUSH_INTERVAL_MS")) |text| {
            const interval = std.fmt.parseInt(u64, text, 10) catch return error.InvalidIndexPushInterval;
            if (interval < 100 or interval > 30_000) return error.InvalidIndexPushInterval;
            config.index.?.push_interval_ms = interval;
        }
        if (getenv("INDEX_RESYNC_AFTER")) |text| {
            config.index.?.resync_after = std.fmt.parseInt(u64, text, 10) catch return error.InvalidIndexResyncAfter;
        }
    };
    const enrich_off = if (getenv("ENRICH")) |text| std.mem.eql(u8, text, "0") else false;
    if (!config.offline and !enrich_off) {
        config.enrich = try enrichment(getenv);
        config.first_check_per_hour = try whole(getenv, "ENRICH_FIRST_CHECK_PER_HOUR", first_check_per_hour);
        config.first_check_stamp = try whole(getenv, "ENRICH_FIRST_CHECK_STAMP", 0);
    }
    if (getenv("CORPUS_SCAN_SECONDS")) |text| config.corpus_scan_seconds = std.fmt.parseInt(u64, text, 10) catch return error.InvalidCorpusScan;
    if (!config.offline) config.control = controlLoop(getenv);
    return config;
}

fn controlLoop(getenv: Getenv) Control {
    const url = getenv("CONTROL_URL") orelse "";
    const secret = getenv("SLOT_CONTROL_SECRET") orelse "";
    if (url.len == 0 and secret.len == 0) return .off;
    if (url.len == 0) return .{ .misconfigured = "SLOT_CONTROL_SECRET is set without CONTROL_URL" };
    if (secret.len == 0) return .{ .misconfigured = "CONTROL_URL is set without SLOT_CONTROL_SECRET" };
    const trimmed = std.mem.trimEnd(u8, url, "/");
    if (!(std.mem.startsWith(u8, trimmed, "https://") and trimmed.len > "https://".len) and !loopback(trimmed)) {
        return .{ .misconfigured = "CONTROL_URL is neither https:// nor a loopback address" };
    }
    if (secret.len < control.min_secret_bytes) return .{ .misconfigured = "SLOT_CONTROL_SECRET is shorter than 32 bytes" };
    var options: control.Options = .{ .url = trimmed, .secret = secret };
    if (getenv("CONTROL_POLL_SECONDS")) |text| {
        const seconds = std.fmt.parseInt(u64, text, 10) catch 0;
        if (seconds < 5 or seconds > 3600) return .{ .misconfigured = "CONTROL_POLL_SECONDS is not a number from 5 to 3600" };
        options.poll_seconds = seconds;
    }
    if (getenv("CONTROL_REQUESTS_PER_POLL")) |text| {
        const count = std.fmt.parseInt(usize, text, 10) catch 0;
        if (count < 1 or count > 500) return .{ .misconfigured = "CONTROL_REQUESTS_PER_POLL is not a number from 1 to 500" };
        options.requests_per_poll = count;
    }
    if (getenv("CONTROL_REQUEST_BACKLOG")) |text| {
        options.request_backlog = std.fmt.parseInt(usize, text, 10) catch return .{ .misconfigured = "CONTROL_REQUEST_BACKLOG is not a number" };
    }
    return .{ .on = options };
}

fn whole(getenv: Getenv, name: [*:0]const u8, default: u64) Error!u64 {
    return std.fmt.parseInt(u64, getenv(name) orelse return default, 10) catch error.InvalidEnrichment;
}

fn fraction(getenv: Getenv, name: [*:0]const u8, default: f64) Error!f64 {
    const value = std.fmt.parseFloat(f64, getenv(name) orelse return default) catch return error.InvalidEnrichment;
    return if (std.math.isFinite(value) and value >= 0) value else error.InvalidEnrichment;
}

fn origin(getenv: Getenv, name: [*:0]const u8, default: []const u8) Error![]const u8 {
    const url = std.mem.trimEnd(u8, getenv(name) orelse return default, "/");
    return if (std.mem.startsWith(u8, url, "https://") or loopback(url)) url else error.InvalidEnrichment;
}

fn enrichment(getenv: Getenv) Error!enrich.Options {
    const defaults: enrich.Options = .{};
    const options: enrich.Options = .{
        .appview_url = try origin(getenv, "ENRICH_APPVIEW_URL", defaults.appview_url),
        .blacksky_url = try origin(getenv, "ENRICH_BLACKSKY_URL", defaults.blacksky_url),
        .requests_per_second = try fraction(getenv, "ENRICH_REQUESTS_PER_SECOND", defaults.requests_per_second),
        .jitter = try fraction(getenv, "ENRICH_JITTER", defaults.jitter),
        .walk_per_hour = try whole(getenv, "ENRICH_WALK_PER_HOUR", defaults.walk_per_hour),
        .sweep_per_hour = try whole(getenv, "ENRICH_SWEEP_PER_HOUR", defaults.sweep_per_hour),
        .lease_seconds = try whole(getenv, "ENRICH_LEASE_SECONDS", defaults.lease_seconds),
        .backoff_seconds = try whole(getenv, "ENRICH_BACKOFF_SECONDS", defaults.backoff_seconds),
        .backoff_max_seconds = try whole(getenv, "ENRICH_BACKOFF_MAX_SECONDS", defaults.backoff_max_seconds),
        .handle_check = .{
            .settle_seconds = try whole(getenv, "ENRICH_HANDLE_SETTLE_SECONDS", defaults.handle_check.settle_seconds),
            .backlog_limit = try whole(getenv, "ENRICH_HANDLE_BACKLOG", defaults.handle_check.backlog_limit),
        },
    };
    if (options.requests_per_second <= 0 or options.requests_per_second > 10 or options.jitter > 1) return error.InvalidEnrichment;
    if (options.lease_seconds < 30 or options.backoff_seconds == 0 or options.backoff_max_seconds < options.backoff_seconds) return error.InvalidEnrichment;
    return options;
}

/// Where the relay subscription starts: the stored cursor. Null means the
/// role must not start: a store without a cursor would follow the relay from
/// now and skip everything before.
pub fn resumeCursor(stored: ?u64) ?i64 {
    const cursor = stored orelse return null;
    return ingest.plausibleRelayCursor(std.math.cast(i64, cursor));
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
    try testing.expect(bare.read_secret == null and !bare.offline);

    const full = try load(fakeEnv(&.{ .{ "STORE_DIR", "/data/store" }, .{ "STORE_READ_SECRET", "s3cret" }, .{ "STORE_OFFLINE", "1" } }));
    try testing.expectEqualStrings("s3cret", full.read_secret.?);
    try testing.expect(full.offline);
}

test "an empty read secret is refused" {
    try testing.expectError(error.EmptyReadSecret, load(fakeEnv(&.{ .{ "STORE_DIR", "/d" }, .{ "STORE_READ_SECRET", "" } })));
}

test "the role starts from the stored cursor, and not at all without a usable one" {
    try testing.expectEqual(@as(?i64, 900), resumeCursor(900));
    try testing.expectEqual(@as(?i64, null), resumeCursor(null));
    try testing.expectEqual(@as(?i64, null), resumeCursor(1_785_217_869_995_374));
}

test "the index bridge runs when the admin secret is set, against a loopback index" {
    const base = [2][]const u8{ "STORE_DIR", "/d" };
    try testing.expect((try load(fakeEnv(&.{base}))).index == null);
    try testing.expect((try load(fakeEnv(&.{ base, .{ "ADMIN_SECRET", "" } }))).index == null);
    try testing.expect((try load(fakeEnv(&.{ base, .{ "INDEX_ADMIN_URL", "http://127.0.0.1:9" } }))).index == null);
    const defaults = (try load(fakeEnv(&.{ base, .{ "ADMIN_SECRET", "s" }, .{ "FLY_APP_NAME", "typeahead-shadow-a" } }))).index.?;
    try testing.expectEqualStrings("http://127.0.0.1:8081", defaults.admin_url);
    try testing.expectEqualStrings("typeahead-shadow-a-store", defaults.source_id);
    try testing.expect(!defaults.adopt_cursor);
    try testing.expectError(error.IndexNotLoopback, load(fakeEnv(&.{ base, .{ "ADMIN_SECRET", "s" }, .{ "INDEX_ADMIN_URL", "https://index.example:8081" } })));
    try testing.expectError(error.IndexNotLoopback, load(fakeEnv(&.{ base, .{ "ADMIN_SECRET", "s" }, .{ "INDEX_ADMIN_URL", "http://127.0.0.1.evil.example:80" } })));
    const index = (try load(fakeEnv(&.{ base, .{ "ADMIN_SECRET", "s" }, .{ "INDEX_ADMIN_URL", "http://127.0.0.1:8085/" }, .{ "INDEX_SOURCE_ID", "slot-a-2" }, .{ "INDEX_ADOPT_CURSOR", "1" } }))).index.?;
    try testing.expectEqualStrings("http://127.0.0.1:8085", index.admin_url);
    try testing.expectEqualStrings("slot-a-2", index.source_id);
    try testing.expect(index.adopt_cursor);
    try testing.expectEqual(@as(u64, 1000), index.push_interval_ms);
    const paced = [2][]const u8{ "ADMIN_SECRET", "s" };
    try testing.expectEqual(@as(u64, 5000), (try load(fakeEnv(&.{ base, paced, .{ "INDEX_PUSH_INTERVAL_MS", "5000" } }))).index.?.push_interval_ms);
    try testing.expectEqual(@as(u64, 0), index.resync_after);
    try testing.expectEqual(@as(u64, 1_794_500_000), (try load(fakeEnv(&.{ base, paced, .{ "INDEX_RESYNC_AFTER", "1794500000" } }))).index.?.resync_after);
    try testing.expectError(error.InvalidIndexResyncAfter, load(fakeEnv(&.{ base, paced, .{ "INDEX_RESYNC_AFTER", "tomorrow" } })));
    inline for ([_][]const u8{ "99", "30001", "5s", "" }) |text| {
        try testing.expectError(error.InvalidIndexPushInterval, load(fakeEnv(&.{ base, paced, .{ "INDEX_PUSH_INTERVAL_MS", text } })));
    }
}

test "enrichment is on with its defaults unless the role is offline or told otherwise" {
    const base = [2][]const u8{ "STORE_DIR", "/d" };
    const defaults = (try load(fakeEnv(&.{base}))).enrich.?;
    try testing.expectEqual(@as(f64, 0.5), defaults.requests_per_second);
    try testing.expectEqual(@as(f64, 0.2), defaults.jitter);
    try testing.expectEqual(@as(u64, 20_000), defaults.walk_per_hour);
    try testing.expectEqual(@as(u64, 10_000), defaults.sweep_per_hour);
    try testing.expectEqual(@as(u64, 300), defaults.lease_seconds);
    try testing.expectEqual(@as(u64, 30), defaults.backoff_seconds);
    try testing.expectEqual(@as(u64, 900), defaults.backoff_max_seconds);
    try testing.expectEqual(@as(u64, 86_400), defaults.handle_check.settle_seconds);
    try testing.expectEqual(@as(usize, 100), defaults.handle_check.backlog_limit);
    try testing.expectEqualStrings("https://public.api.bsky.app", defaults.appview_url);
    try testing.expectEqualStrings("https://api.blacksky.community", defaults.blacksky_url);
    try testing.expectEqual(@as(u64, 3600), (try load(fakeEnv(&.{base}))).corpus_scan_seconds);

    try testing.expect((try load(fakeEnv(&.{ base, .{ "STORE_OFFLINE", "1" } }))).enrich == null);
    try testing.expect((try load(fakeEnv(&.{ base, .{ "ENRICH", "0" } }))).enrich == null);

    try testing.expectEqual(@as(u64, 15_000), (try load(fakeEnv(&.{base}))).first_check_per_hour);
    try testing.expectEqual(@as(u64, 0), (try load(fakeEnv(&.{base}))).first_check_stamp);
    try testing.expectEqual(@as(u64, 0), (try load(fakeEnv(&.{ base, .{ "ENRICH_FIRST_CHECK_PER_HOUR", "0" } }))).first_check_per_hour);
    try testing.expectEqual(@as(u64, 0), (try load(fakeEnv(&.{ base, .{ "STORE_OFFLINE", "1" } }))).first_check_per_hour);
    try testing.expectEqual(@as(u64, 0), (try load(fakeEnv(&.{ base, .{ "ENRICH", "0" }, .{ "ENRICH_FIRST_CHECK_STAMP", "1794000000" } }))).first_check_per_hour);
    const stamped = try load(fakeEnv(&.{ base, .{ "ENRICH_FIRST_CHECK_PER_HOUR", "40000" }, .{ "ENRICH_FIRST_CHECK_STAMP", "1794000000" } }));
    try testing.expectEqual(@as(u64, 40_000), stamped.first_check_per_hour);
    try testing.expectEqual(@as(u64, 1_794_000_000), stamped.first_check_stamp);
    const set = try load(fakeEnv(&.{
        base,                                                   .{ "ENRICH_REQUESTS_PER_SECOND", "0.25" }, .{ "ENRICH_JITTER", "0.5" },
        .{ "ENRICH_WALK_PER_HOUR", "800" },                     .{ "ENRICH_SWEEP_PER_HOUR", "0" },         .{ "ENRICH_LEASE_SECONDS", "600" },
        .{ "ENRICH_BACKOFF_SECONDS", "10" },                    .{ "ENRICH_BACKOFF_MAX_SECONDS", "60" },   .{ "ENRICH_APPVIEW_URL", "http://127.0.0.1:2584/" },
        .{ "ENRICH_BLACKSKY_URL", "https://blacksky.example" }, .{ "CORPUS_SCAN_SECONDS", "0" },           .{ "ENRICH_HANDLE_SETTLE_SECONDS", "3600" },
        .{ "ENRICH_HANDLE_BACKLOG", "25" },
    }));
    const options = set.enrich.?;
    try testing.expectEqual(@as(f64, 0.25), options.requests_per_second);
    try testing.expectEqual(@as(f64, 0.5), options.jitter);
    try testing.expectEqual(@as(u64, 800), options.walk_per_hour);
    try testing.expectEqual(@as(u64, 0), options.sweep_per_hour);
    try testing.expectEqual(@as(u64, 600), options.lease_seconds);
    try testing.expectEqual(@as(u64, 10), options.backoff_seconds);
    try testing.expectEqual(@as(u64, 60), options.backoff_max_seconds);
    try testing.expectEqual(@as(u64, 3600), options.handle_check.settle_seconds);
    try testing.expectEqual(@as(usize, 25), options.handle_check.backlog_limit);
    try testing.expectEqualStrings("http://127.0.0.1:2584", options.appview_url);
    try testing.expectEqualStrings("https://blacksky.example", options.blacksky_url);
    try testing.expectEqual(@as(u64, 0), set.corpus_scan_seconds);
}

test "the control loop runs when the Worker's origin and the slot secret are both set" {
    const base = [2][]const u8{ "STORE_DIR", "/d" };
    const secret = [2][]const u8{ "SLOT_CONTROL_SECRET", "0123456789abcdef0123456789abcdef" };
    const url = [2][]const u8{ "CONTROL_URL", "https://typeahead.waow.tech/" };
    try testing.expect((try load(fakeEnv(&.{base}))).control == .off);
    try testing.expect((try load(fakeEnv(&.{ base, .{ "CONTROL_URL", "" }, .{ "SLOT_CONTROL_SECRET", "" } }))).control == .off);
    try testing.expect((try load(fakeEnv(&.{ base, url, secret, .{ "STORE_OFFLINE", "1" } }))).control == .off);

    const on = (try load(fakeEnv(&.{ base, url, secret }))).control.on;
    try testing.expectEqualStrings("https://typeahead.waow.tech", on.url);
    try testing.expectEqualStrings("0123456789abcdef0123456789abcdef", on.secret);
    try testing.expectEqual(@as(u64, 30), on.poll_seconds);
    try testing.expectEqual(@as(f64, 0.2), on.jitter);
    try testing.expectEqual(@as(usize, 20), on.requests_per_poll);
    try testing.expectEqual(@as(usize, 100), on.request_backlog);
    const bounded = (try load(fakeEnv(&.{ base, url, secret, .{ "CONTROL_REQUESTS_PER_POLL", "5" }, .{ "CONTROL_REQUEST_BACKLOG", "0" } }))).control.on;
    try testing.expectEqual(@as(usize, 5), bounded.requests_per_poll);
    try testing.expectEqual(@as(usize, 0), bounded.request_backlog);
    try testing.expectEqual(@as(u64, 120), (try load(fakeEnv(&.{ base, url, secret, .{ "CONTROL_POLL_SECONDS", "120" } }))).control.on.poll_seconds);
    try testing.expectEqualStrings("http://127.0.0.1:8787", (try load(fakeEnv(&.{ base, secret, .{ "CONTROL_URL", "http://127.0.0.1:8787" } }))).control.on.url);
}

test "a control loop that cannot run is left off and says why, and the role still loads" {
    const base = [2][]const u8{ "STORE_DIR", "/d" };
    const secret = [2][]const u8{ "SLOT_CONTROL_SECRET", "0123456789abcdef0123456789abcdef" };
    const url = [2][]const u8{ "CONTROL_URL", "https://typeahead.waow.tech" };
    try testing.expectEqualStrings("CONTROL_URL is set without SLOT_CONTROL_SECRET", (try load(fakeEnv(&.{ base, url }))).control.misconfigured);
    try testing.expectEqualStrings("SLOT_CONTROL_SECRET is set without CONTROL_URL", (try load(fakeEnv(&.{ base, secret }))).control.misconfigured);
    try testing.expectEqualStrings("SLOT_CONTROL_SECRET is shorter than 32 bytes", (try load(fakeEnv(&.{ base, url, .{ "SLOT_CONTROL_SECRET", "short" } }))).control.misconfigured);
    inline for ([_][]const u8{ "http://typeahead.waow.tech", "https://", "typeahead.waow.tech", "http://127.0.0.1.evil.example:80" }) |bad| {
        try testing.expectEqualStrings("CONTROL_URL is neither https:// nor a loopback address", (try load(fakeEnv(&.{ base, secret, .{ "CONTROL_URL", bad } }))).control.misconfigured);
    }
    inline for ([_][]const u8{ "0", "501", "many", "" }) |bad| {
        try testing.expectEqualStrings("CONTROL_REQUESTS_PER_POLL is not a number from 1 to 500", (try load(fakeEnv(&.{ base, url, secret, .{ "CONTROL_REQUESTS_PER_POLL", bad } }))).control.misconfigured);
    }
    try testing.expectEqualStrings("CONTROL_REQUEST_BACKLOG is not a number", (try load(fakeEnv(&.{ base, url, secret, .{ "CONTROL_REQUEST_BACKLOG", "-1" } }))).control.misconfigured);
    inline for ([_][]const u8{ "4", "3601", "30s", "", "-1" }) |bad| {
        try testing.expectEqualStrings("CONTROL_POLL_SECONDS is not a number from 5 to 3600", (try load(fakeEnv(&.{ base, url, secret, .{ "CONTROL_POLL_SECONDS", bad } }))).control.misconfigured);
    }
}

test "enrichment settings that could not pace a loop are refused" {
    const base = [2][]const u8{ "STORE_DIR", "/d" };
    for ([_][2][]const u8{
        .{ "ENRICH_REQUESTS_PER_SECOND", "0" },              .{ "ENRICH_REQUESTS_PER_SECOND", "-1" },       .{ "ENRICH_REQUESTS_PER_SECOND", "fast" },
        .{ "ENRICH_REQUESTS_PER_SECOND", "50" },             .{ "ENRICH_JITTER", "2" },                     .{ "ENRICH_WALK_PER_HOUR", "-5" },
        .{ "ENRICH_LEASE_SECONDS", "5" },                    .{ "ENRICH_BACKOFF_SECONDS", "0" },            .{ "ENRICH_BACKOFF_MAX_SECONDS", "10" },
        .{ "ENRICH_APPVIEW_URL", "http://appview.example" }, .{ "ENRICH_HANDLE_BACKLOG", "many" },          .{ "ENRICH_HANDLE_SETTLE_SECONDS", "-1" },
        .{ "ENRICH_FIRST_CHECK_PER_HOUR", "fast" },          .{ "ENRICH_FIRST_CHECK_STAMP", "2026-11-11" },
    }) |pair| {
        const env = struct {
            var name: []const u8 = "";
            var value: []const u8 = "";
            fn get(asked: [*:0]const u8) ?[]const u8 {
                if (std.mem.eql(u8, std.mem.span(asked), "STORE_DIR")) return base[1];
                if (std.mem.eql(u8, std.mem.span(asked), name)) return value;
                return null;
            }
        };
        env.name = pair[0];
        env.value = pair[1];
        try testing.expectError(error.InvalidEnrichment, load(env.get));
    }
    try testing.expectError(error.InvalidCorpusScan, load(fakeEnv(&.{ base, .{ "CORPUS_SCAN_SECONDS", "hourly" } })));
}
