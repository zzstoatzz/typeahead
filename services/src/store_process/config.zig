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
//! | `ADMIN_SECRET`           | no       | bearer secret of the co-located index's admin listener; set, the index bridge runs |
//! | `INDEX_ADMIN_URL`        | no       | that listener, a loopback URL; default `http://127.0.0.1:8081` |
//! | `INDEX_SOURCE_ID`        | no       | names this store to the index; default `$FLY_APP_NAME-store`, change it when the store is rebuilt |
//! | `INDEX_ADOPT_CURSOR`     | no       | `1` takes over an index whose cursor something else wrote (a bundle's), by a full resync |
//! | `INDEX_PUSH_INTERVAL_MS` | no       | rest between pushes to the index while the bridge keeps up, 100 to 30000, default 1000; each push is an index commit, and a change waits at most this long to be read |
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
//! | `HANDLE_REPAIR`          | no       | `0` turns the one-time re-verification of `handle.invalid` records off; it is on unless `STORE_OFFLINE` |
//! | `CORPUS_SCAN_SECONDS`    | no       | seconds between corpus counts, default 3600; `0` turns them off. The first is taken 300 s after the start |
//!
//! Enrichment pacing, with the defaults, for one replica. Two replicas run
//! this loop against the same appview, so the appview sees twice each rate.
//!
//! - Requests are at least 2 s apart (0.5 a second) and each gap is stretched
//!   by 0 to 20 %, 2.2 s on average: 1,636 requests an hour, 39,273 a day.
//!   Two replicas: at most 1 request a second, 3,273 an hour on average. The
//!   HeavyPad backfill this replaces paced itself at 2 a second.
//! - A request carries 25 actors, so a replica refreshes at most 40,909
//!   actors an hour, 981,818 a day. A DID the appview does not return costs
//!   one more request (the takedown probe) and up to three after that, which
//!   comes out of the same budget.
//! - The quality walk queues 20,000 ranked actors an hour, 480,000 a day:
//!   25 times the cron's 800. A pool of N ranked actors is swept every
//!   N / 20,000 hours: 2.1 hours for the 41,355 counted on 2026-08-16, 50
//!   hours for a million. `/health` reports the period for the pool the last
//!   corpus scan counted.
//! - The rest walk queues 10,000 an hour, 240,000 a day, and only while the
//!   queue is keeping up, so it yields to everything else. A pool of N is
//!   swept every N / 10,000 hours: 55 days if all 13.2 million actors were
//!   in it. A step examines at most 8 records for each actor it may queue,
//!   so a pass also takes at least a full read of the actor range at 80,000
//!   records an hour: 165 hours for 13.2 million.
//! - That leaves 10,909 actors an hour, 261,818 a day, for refreshes that
//!   profile and identity writes queue. New accounts arrive at about 2,900
//!   an hour.
//! - A profile whose handle differs from the stored one queues one identity
//!   resolution, at most once per actor per `ENRICH_HANDLE_SETTLE_SECONDS`
//!   and never while `ENRICH_HANDLE_BACKLOG` entries are due. So however
//!   many of the 40,909 profiles an hour disagree, enrichment stops adding
//!   to the identity queue once 100 entries are due; the rest are counted
//!   as deferred and looked at again on their walk's next pass. An entry it
//!   did add comes due again on each retry the resolver asks for.
//!
//! The role reads nothing for Turso or the Worker and opens no connection
//! to either. `MODE=store-bootstrap` loads a new store; see `bootstrap.zig`.

const std = @import("std");
const ingest = @import("../ingest.zig");
const enrich = @import("enrich.zig");

pub const Config = struct {
    store_dir: []const u8,
    read_secret: ?[]const u8 = null,
    bootstrap_cursor: ?i64 = null,
    offline: bool = false,
    index: ?Index = null,
    /// Null when enrichment is off.
    enrich: ?enrich.Options = null,
    corpus_scan_seconds: u64 = 3600,
    /// Holders of `handle.invalid` one step of the handle repair looks at;
    /// 0 when the repair is off. See `owner.Options.repair_limit`.
    handle_repair_per_step: usize = 0,
};

pub const handle_repair_per_step = 50;

pub const Index = struct {
    admin_url: []const u8,
    admin_secret: []const u8,
    source_id: []const u8,
    adopt_cursor: bool = false,
    /// `bridge.Config.poll_interval_ms`. The ceiling is the bridge's
    /// heartbeat: the index stops serving at 120 s without a watermark.
    push_interval_ms: u64 = 1000,
};

pub const default_admin_url = "http://127.0.0.1:8081";
var source_id_buf: [128]u8 = undefined;

pub const Error = error{ StoreDirRequired, InvalidBootstrapCursor, EmptyReadSecret, IndexNotLoopback, InvalidIndexPushInterval, InvalidEnrichment, InvalidCorpusScan };

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
    };
    const enrich_off = if (getenv("ENRICH")) |text| std.mem.eql(u8, text, "0") else false;
    if (!config.offline and !enrich_off) config.enrich = try enrichment(getenv);
    const repair_off = if (getenv("HANDLE_REPAIR")) |text| std.mem.eql(u8, text, "0") else false;
    if (!config.offline and !repair_off) config.handle_repair_per_step = handle_repair_per_step;
    if (getenv("CORPUS_SCAN_SECONDS")) |text| config.corpus_scan_seconds = std.fmt.parseInt(u64, text, 10) catch return error.InvalidCorpusScan;
    return config;
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

    try testing.expectEqual(@as(usize, 50), (try load(fakeEnv(&.{base}))).handle_repair_per_step);
    try testing.expectEqual(@as(usize, 0), (try load(fakeEnv(&.{ base, .{ "HANDLE_REPAIR", "0" } }))).handle_repair_per_step);
    try testing.expectEqual(@as(usize, 0), (try load(fakeEnv(&.{ base, .{ "STORE_OFFLINE", "1" } }))).handle_repair_per_step);
    try testing.expect((try load(fakeEnv(&.{ base, .{ "STORE_OFFLINE", "1" } }))).enrich == null);
    try testing.expect((try load(fakeEnv(&.{ base, .{ "ENRICH", "0" } }))).enrich == null);
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

test "enrichment settings that could not pace a loop are refused" {
    const base = [2][]const u8{ "STORE_DIR", "/d" };
    for ([_][2][]const u8{
        .{ "ENRICH_REQUESTS_PER_SECOND", "0" },              .{ "ENRICH_REQUESTS_PER_SECOND", "-1" }, .{ "ENRICH_REQUESTS_PER_SECOND", "fast" },
        .{ "ENRICH_REQUESTS_PER_SECOND", "50" },             .{ "ENRICH_JITTER", "2" },               .{ "ENRICH_WALK_PER_HOUR", "-5" },
        .{ "ENRICH_LEASE_SECONDS", "5" },                    .{ "ENRICH_BACKOFF_SECONDS", "0" },      .{ "ENRICH_BACKOFF_MAX_SECONDS", "10" },
        .{ "ENRICH_APPVIEW_URL", "http://appview.example" }, .{ "ENRICH_HANDLE_BACKLOG", "many" },    .{ "ENRICH_HANDLE_SETTLE_SECONDS", "-1" },
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
