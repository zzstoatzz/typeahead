//! Profile enrichment against the store's refresh queue. One loop on its own
//! thread; it never touches the store, and hands every lease and result to
//! the owner as a submission.
//!
//! One step:
//!
//! 1. Feed and lease, in one commit: a step of the quality walk and, when
//!    the queue is not backed up, of the rest walk, then a lease of up to 25
//!    due entries. Profile and identity writes queue their actor themselves,
//!    which is how new and re-touched actors arrive.
//! 2. Ask the appview's `getProfiles` for the leased DIDs.
//! 3. A profile that came back becomes an `enrichment` from Bluesky, with
//!    the response's labels as the labeler's answer.
//! 4. A DID that did not come back is handled as `refreshModeration` in
//!    `src/cron.ts` handles it. With a PDS on record and no `hide` override,
//!    the appview's `getProfile` is asked why; `AccountTakedown`, or a `show`
//!    override without asking, sends the DID to Blacksky's appview and then
//!    to the profile record on its PDS (`app.bsky.actor.profile`, then
//!    `sh.tangled.actor.profile`). Either becomes an `enrichment` from that
//!    source. Anything else (deactivated,
//!    suspended, never indexed) changes nothing and is stamped as checked.
//!    Nothing is deleted here: account removal arrives on the relay.
//! 5. The results are one submission. A request that taught nothing (429,
//!    5xx, no connection, a body without `profiles`) reschedules its entries
//!    and backs the whole loop off.
//!
//! No source's handle is ever written. The cron copied the appview's handle
//! over the stored one, and the appview is often wrong about handles: it
//! answers `handle.invalid` for handles that resolve, and an old handle
//! after a PLC handle change. Here a handle from Bluesky or Blacksky that
//! differs from the stored one sends the actor back through this role's own
//! identity resolution, which alone writes handles. `Options.handle_check`
//! bounds that: an actor resolved in the last day is left alone, and nothing
//! is queued while a hundred identity entries are due. A PDS record carries
//! no handle.
//!
//! Three more things differ from the cron on purpose. A PDS record carries
//! no labels, so that source keeps the stored ones; the cron wrote an empty
//! list and so unhid a taken-down account. A failed takedown probe retries
//! the DID; the cron read it as "not taken down". And a 400 for a batch is
//! split until the DID the appview refuses is alone, so one bad DID cannot
//! hold twenty-four others back.
//!
//! Pacing is one clock for every request this loop makes: no two closer
//! than `1 / requests_per_second`, each gap stretched by up to `jitter`.
//! Failures double a pause from `backoff_seconds` to `backoff_max_seconds`
//! and honour `Retry-After`. The arithmetic is in `config.zig`.

const std = @import("std");
const zat = @import("zat");
const plc_tail = @import("../plc_tail.zig");
const store_mod = @import("../store/root.zig");
const authority = @import("authority_score.zig");
const owner_mod = @import("owner.zig");
const profile = @import("profile.zig");
const Allocator = std.mem.Allocator;
const Change = store_mod.Change;
const Owner = owner_mod.Owner;
const Submission = owner_mod.Submission;
const RefreshJob = store_mod.change.RefreshJob;
const Value = std.json.Value;

const log = std.log.scoped(.enrich);

pub const get_profiles_max = 25;
/// Keys a walk step examines for each actor it may queue.
pub const quality_scan_factor = 4;
pub const rest_scan_factor = 8;

/// Seconds one pass of a walk takes: its pool at the hourly rate, or every
/// key of its range at the rate a step examines them, whichever is longer.
pub fn walkPeriod(pool: u64, range: u64, per_hour: u64, scan_factor: u64) ?u64 {
    if (per_hour == 0) return null;
    return @max(pool * 3600 / per_hour, range * 3600 / (per_hour * scan_factor));
}

pub const Options = struct {
    appview_url: []const u8 = "https://public.api.bsky.app",
    blacksky_url: []const u8 = "https://api.blacksky.community",
    /// The ceiling on requests from this process, to any host.
    requests_per_second: f64 = 0.5,
    /// Each gap between requests is stretched by up to this fraction.
    jitter: f64 = 0.2,
    lease_seconds: u64 = 300,
    /// Actors the quality walk queues an hour.
    walk_per_hour: u64 = 20_000,
    /// Actors the rest walk queues an hour, while the queue keeps up.
    sweep_per_hour: u64 = 10_000,
    backoff_seconds: u64 = 30,
    backoff_max_seconds: u64 = 900,
    /// How long the loop waits when nothing is due.
    idle_seconds: u64 = 5,
    /// When a reported handle that differs from the stored one sends its
    /// actor to identity resolution.
    handle_check: store_mod.change.HandleCheck = .{},
};

pub const Response = struct {
    /// 0 when no response arrived.
    status: u16 = 0,
    body: []const u8 = "",
    retry_after_seconds: ?u64 = null,
};

pub const Transport = struct {
    context: *anyopaque,
    get: *const fn (context: *anyopaque, arena: Allocator, url: []const u8) Response,
};

/// Wall time in milliseconds, and a sleep. Tests supply a clock they move.
pub const Timer = struct {
    context: *anyopaque,
    now_ms: *const fn (context: *anyopaque) i64,
    /// False when the sleep was cut short because the process is stopping.
    sleep_ms: *const fn (context: *anyopaque, ms: u64) bool,
};

pub const Stats = struct {
    leased: std.atomic.Value(u64) = .init(0),
    /// Entries settled with a profile, by where it came from.
    enriched: std.atomic.Value(u64) = .init(0),
    from_blacksky: std.atomic.Value(u64) = .init(0),
    from_pds: std.atomic.Value(u64) = .init(0),
    /// Entries settled with no profile anywhere.
    not_found: std.atomic.Value(u64) = .init(0),
    /// Entries whose stored content the result changed.
    changed: std.atomic.Value(u64) = .init(0),
    /// Entries rescheduled because a request taught nothing.
    retried: std.atomic.Value(u64) = .init(0),
    requests: std.atomic.Value(u64) = .init(0),
    /// Requests that got no usable answer, 429s included.
    failed: std.atomic.Value(u64) = .init(0),
    rate_limited: std.atomic.Value(u64) = .init(0),
    /// Batches split after a 400.
    split: std.atomic.Value(u64) = .init(0),
    commit_failed: std.atomic.Value(u64) = .init(0),
    walk_queued: std.atomic.Value(u64) = .init(0),
    sweep_queued: std.atomic.Value(u64) = .init(0),
    /// Unix seconds.
    last_success_at: std.atomic.Value(u64) = .init(0),
    last_request_at: std.atomic.Value(u64) = .init(0),
    backoff_until: std.atomic.Value(u64) = .init(0),
    consecutive_failures: std.atomic.Value(u64) = .init(0),
    /// The last lease came back full: more is due than one step takes.
    backed_up: std.atomic.Value(bool) = .init(false),
};

const hex = "0123456789ABCDEF";

/// `encodeURIComponent`.
fn encoded(arena: Allocator, text: []const u8) ![]const u8 {
    var out: std.ArrayList(u8) = .empty;
    for (text) |c| {
        if (std.ascii.isAlphanumeric(c) or std.mem.indexOfScalar(u8, "-_.!~*'()", c) != null) {
            try out.append(arena, c);
        } else try out.appendSlice(arena, &.{ '%', hex[c >> 4], hex[c & 15] });
    }
    return out.items;
}

/// `pdsOrigin` in `src/utils.ts`: the https origin of an actor-controlled
/// PDS value, or null when it carries credentials, a path, a query, a
/// fragment or a single-label host.
pub fn pdsOrigin(arena: Allocator, pds: []const u8) !?[]const u8 {
    const uri = std.Uri.parse(pds) catch return null;
    if (!std.ascii.eqlIgnoreCase(uri.scheme, "https")) return null;
    if (uri.user != null or uri.password != null or uri.query != null or uri.fragment != null) return null;
    const path = try uri.path.toRawMaybeAlloc(arena);
    if (path.len != 0 and !std.mem.eql(u8, path, "/")) return null;
    const host = try (uri.host orelse return null).toRawMaybeAlloc(arena);
    if (std.mem.indexOfScalar(u8, host, '.') == null) return null;
    const lower = try std.ascii.allocLowerString(arena, host);
    if (uri.port) |port| if (port != 443) return try std.fmt.allocPrint(arena, "https://{s}:{d}", .{ lower, port });
    return try std.fmt.allocPrint(arena, "https://{s}", .{lower});
}

fn member(value: Value, name: []const u8) ?Value {
    return switch (value) {
        .object => |object| object.get(name),
        else => null,
    };
}

fn string(value: ?Value) []const u8 {
    return switch (value orelse return "") {
        .string => |s| s,
        else => "",
    };
}

fn parsed(arena: Allocator, body: []const u8) ?Value {
    return std.json.parseFromSliceLeaky(Value, arena, body, .{}) catch null;
}

const Found = struct { fields: profile.Fields, source: store_mod.record.ProfileSource };

const Verdict = union(enum) {
    found: Found,
    /// No profile anywhere: settle the entry as checked.
    absent,
    /// The appview could not be asked: leave the entry for a later attempt.
    unknown,
};

pub const Step = enum { idle, worked };

pub const Enricher = struct {
    gpa: Allocator,
    owner: *Owner,
    transport: Transport,
    timer: Timer,
    options: Options = .{},
    stats: Stats = .{},
    prng: std.Random.DefaultPrng = .init(0x7e11_0c1a),
    stopping: std.atomic.Value(bool) = .init(false),
    next_request_ms: i64 = 0,
    blocked_until_ms: i64 = 0,
    failures: u32 = 0,
    fed_at_ms: ?i64 = null,
    walk_tokens: f64 = 0,
    sweep_tokens: f64 = 0,

    fn now(self: *Enricher) i64 {
        return self.timer.now_ms(self.timer.context);
    }

    fn stretch(self: *Enricher, ms: f64) i64 {
        return @intFromFloat(@ceil(ms * (1 + self.options.jitter * self.prng.random().float(f64))));
    }

    /// One request, no sooner than the pace and any pause allow.
    fn request(self: *Enricher, arena: Allocator, url: []const u8) Response {
        const due = @max(self.next_request_ms, self.blocked_until_ms);
        const at = self.now();
        if (at < due and !self.timer.sleep_ms(self.timer.context, @intCast(due - at))) return .{};
        const started = self.now();
        self.next_request_ms = started + self.stretch(1000 / self.options.requests_per_second);
        _ = self.stats.requests.fetchAdd(1, .monotonic);
        self.stats.last_request_at.store(@intCast(@divFloor(started, 1000)), .monotonic);
        return self.transport.get(self.transport.context, arena, url);
    }

    fn taughtNothing(status: u16) bool {
        return status == 0 or status == 429 or status >= 500;
    }

    fn failed(self: *Enricher, response: Response) void {
        _ = self.stats.failed.fetchAdd(1, .monotonic);
        if (response.status == 429) _ = self.stats.rate_limited.fetchAdd(1, .monotonic);
        self.failures +|= 1;
        const doubled = std.math.shl(u64, self.options.backoff_seconds, self.failures - 1);
        var seconds = @min(self.options.backoff_max_seconds, @max(self.options.backoff_seconds, doubled));
        if (response.retry_after_seconds) |asked| seconds = @min(self.options.backoff_max_seconds, @max(seconds, asked));
        self.blocked_until_ms = self.now() + self.stretch(@floatFromInt(seconds * 1000));
        self.stats.backoff_until.store(@intCast(@divFloor(self.blocked_until_ms, 1000)), .monotonic);
        self.stats.consecutive_failures.store(self.failures, .monotonic);
        log.warn("appview request failed (HTTP {d}), pausing {d} s (failure {d})", .{ response.status, seconds, self.failures });
    }

    fn succeeded(self: *Enricher) void {
        self.failures = 0;
        self.stats.consecutive_failures.store(0, .monotonic);
        self.stats.last_success_at.store(@intCast(@divFloor(self.now(), 1000)), .monotonic);
    }

    /// The appview's profiles for `dids`, into `out`. False when a request
    /// taught nothing, so nothing may be concluded about a DID not in `out`.
    fn profiles(self: *Enricher, arena: Allocator, dids: []const []const u8, out: *std.StringHashMapUnmanaged(Value)) !bool {
        var url: std.ArrayList(u8) = .empty;
        try url.print(arena, "{s}/xrpc/app.bsky.actor.getProfiles?", .{self.options.appview_url});
        for (dids, 0..) |did, index| try url.print(arena, "{s}actors={s}", .{ if (index == 0) "" else "&", try encoded(arena, did) });
        const response = self.request(arena, url.items);
        if (taughtNothing(response.status)) {
            self.failed(response);
            return false;
        }
        if (response.status != 200) {
            self.succeeded();
            if (dids.len == 1) return true;
            _ = self.stats.split.fetchAdd(1, .monotonic);
            const half = dids.len / 2;
            return try self.profiles(arena, dids[0..half], out) and try self.profiles(arena, dids[half..], out);
        }
        const list = switch (member(parsed(arena, response.body) orelse .null, "profiles") orelse .null) {
            .array => |array| array.items,
            else => {
                self.failed(.{ .status = 0 });
                return false;
            },
        };
        self.succeeded();
        for (list) |one| {
            const did = string(member(one, "did"));
            if (did.len > 0) try out.put(arena, did, one);
        }
        return true;
    }

    fn blacksky(self: *Enricher, arena: Allocator, did: []const u8) !?Value {
        const url = try std.fmt.allocPrint(arena, "{s}/xrpc/app.bsky.actor.getProfile?actor={s}", .{ self.options.blacksky_url, try encoded(arena, did) });
        const response = self.request(arena, url);
        if (response.status != 200) return null;
        const body = parsed(arena, response.body) orelse return null;
        return if (std.mem.eql(u8, string(member(body, "did")), did)) body else null;
    }

    /// `fetchProfileFromPds`: the object the Worker builds from a profile
    /// record, the Bluesky collection first.
    fn record(self: *Enricher, arena: Allocator, did: []const u8, pds: []const u8) !?Value {
        const origin = try pdsOrigin(arena, pds) orelse return null;
        for ([_][]const u8{ "app.bsky.actor.profile", "sh.tangled.actor.profile" }) |collection| {
            const url = try std.fmt.allocPrint(arena, "{s}/xrpc/com.atproto.repo.getRecord?repo={s}&collection={s}&rkey=self", .{ origin, try encoded(arena, did), collection });
            const response = self.request(arena, url);
            if (response.status != 200) continue;
            const value = member(parsed(arena, response.body) orelse continue, "value") orelse continue;
            if (value != .object) continue;
            var object: std.json.ObjectMap = .empty;
            try object.put(arena, "did", .{ .string = did });
            try object.put(arena, "handle", .{ .string = "" });
            try object.put(arena, "displayName", .{ .string = string(member(value, "displayName")) });
            try object.put(arena, "avatar", .{ .string = string(member(member(member(value, "avatar") orelse .null, "ref") orelse .null, "$link")) });
            try object.put(arena, "createdAt", .{ .string = string(member(value, "createdAt")) });
            return .{ .object = object };
        }
        return null;
    }

    /// What to do about a DID `getProfiles` left out.
    fn fallback(self: *Enricher, arena: Allocator, job: RefreshJob, now_ms: i64) !Verdict {
        if (job.pds.len == 0 or job.override == .hide) return .absent;
        if (job.override != .show) {
            const url = try std.fmt.allocPrint(arena, "{s}/xrpc/app.bsky.actor.getProfile?actor={s}", .{ self.options.appview_url, try encoded(arena, job.did) });
            const response = self.request(arena, url);
            if (taughtNothing(response.status)) {
                self.failed(response);
                return .unknown;
            }
            self.succeeded();
            if (response.status == 200) return .absent;
            const reason = string(member(parsed(arena, response.body) orelse .null, "error"));
            if (!std.mem.eql(u8, reason, "AccountTakedown")) return .absent;
        }
        if (try self.blacksky(arena, job.did)) |found| return .{ .found = .{ .fields = try profile.extract(arena, found, now_ms), .source = .blacksky } };
        if (try self.record(arena, job.did, job.pds)) |found| return .{ .found = .{ .fields = try profile.extract(arena, found, now_ms), .source = .pds } };
        return .absent;
    }

    fn enrichment(self: *const Enricher, job: RefreshJob, found: Found) Change {
        const f = found.fields;
        const handle = if (f.handle.len <= store_mod.keys.max_handle and std.mem.indexOfScalar(u8, f.handle, 0) == null) f.handle else "";
        return .{ .enrichment = .{
            .did = job.did,
            .source = found.source,
            .handle = handle,
            .handle_check = self.options.handle_check,
            .display_name = f.display_name,
            .avatar = f.avatar,
            .description = f.description,
            .banner = f.banner,
            .created_at = f.created_at,
            .associated = f.associated,
            .followers_count = f.followers_count,
            .follows_count = f.follows_count,
            .posts_count = f.posts_count,
            .quality_score = authority.stored(f.quality_score),
            .labels = if (found.source == .pds) null else f.labels,
            .lease = job.lease,
        } };
    }

    /// The walk steps this step owes, by the time since the last one.
    fn feeds(self: *Enricher, arena: Allocator, list: *std.ArrayList(Change)) !void {
        const at = self.now();
        const elapsed_hours = @as(f64, @floatFromInt(@max(0, at - (self.fed_at_ms orelse at)))) / 3_600_000;
        self.fed_at_ms = at;
        const most: f64 = 4 * get_profiles_max;
        self.walk_tokens = @min(most, self.walk_tokens + elapsed_hours * @as(f64, @floatFromInt(self.options.walk_per_hour)));
        self.sweep_tokens = @min(most, self.sweep_tokens + elapsed_hours * @as(f64, @floatFromInt(self.options.sweep_per_hour)));
        if (self.walk_tokens >= get_profiles_max) {
            const limit: usize = @intFromFloat(self.walk_tokens);
            self.walk_tokens -= @floatFromInt(limit);
            try list.append(arena, .{ .refresh_walk = .{ .pool = .quality, .limit = limit, .scan = limit * quality_scan_factor } });
        }
        if (self.sweep_tokens >= get_profiles_max and !self.stats.backed_up.load(.monotonic)) {
            const limit: usize = @intFromFloat(self.sweep_tokens);
            self.sweep_tokens -= @floatFromInt(limit);
            try list.append(arena, .{ .refresh_walk = .{ .pool = .rest, .limit = limit, .scan = limit * rest_scan_factor } });
        }
    }

    /// Feed, lease, fetch and report once.
    pub fn step(self: *Enricher) !Step {
        const leasing = try Submission.create(self.gpa);
        defer leasing.destroy();
        const arena = leasing.arena.allocator();
        var asks: std.ArrayList(Change) = .empty;
        try self.feeds(arena, &asks);
        try asks.append(arena, .{ .refresh_lease = .{ .limit = get_profiles_max, .lease_seconds = self.options.lease_seconds } });
        leasing.changes = asks.items;
        try self.owner.call(leasing);
        _ = self.stats.walk_queued.fetchAdd(leasing.walked.get(.quality).queued, .monotonic);
        _ = self.stats.sweep_queued.fetchAdd(leasing.walked.get(.rest).queued, .monotonic);
        const jobs = leasing.refresh_jobs;
        self.stats.backed_up.store(jobs.len == get_profiles_max, .monotonic);
        if (jobs.len == 0) return .idle;
        _ = self.stats.leased.fetchAdd(jobs.len, .monotonic);

        const dids = try arena.alloc([]const u8, jobs.len);
        for (dids, jobs) |*did, job| did.* = job.did;
        var returned: std.StringHashMapUnmanaged(Value) = .empty;
        const answered = try self.profiles(arena, dids, &returned);
        const now_ms = self.now();

        const report = try Submission.create(self.gpa);
        defer report.destroy();
        const out = report.arena.allocator();
        const changes = try out.alloc(Change, jobs.len);
        var tally: struct { enriched: u64 = 0, blacksky: u64 = 0, pds: u64 = 0, absent: u64 = 0, retried: u64 = 0 } = .{};
        for (changes, jobs) |*slot, job| {
            const verdict: Verdict = if (returned.get(job.did)) |found|
                .{ .found = .{ .fields = try profile.extract(arena, found, now_ms), .source = .bsky } }
            else if (!answered)
                .unknown
            else
                try self.fallback(arena, job, now_ms);
            slot.* = switch (verdict) {
                .found => |found| blk: {
                    switch (found.source) {
                        .blacksky => tally.blacksky += 1,
                        .pds => tally.pds += 1,
                        else => tally.enriched += 1,
                    }
                    break :blk self.enrichment(job, found);
                },
                .absent => blk: {
                    tally.absent += 1;
                    break :blk .{ .refresh_complete = .{ .did = job.did, .lease = job.lease } };
                },
                .unknown => blk: {
                    tally.retried += 1;
                    const wait_ms = @max(self.blocked_until_ms - now_ms, @as(i64, @intCast(self.options.backoff_seconds * 1000)));
                    break :blk .{ .refresh_retry = .{ .did = job.did, .lease = job.lease, .due = @intCast(@divFloor(now_ms + wait_ms, 1000) + 1) } };
                },
            };
        }
        report.changes = changes;
        self.owner.call(report) catch |err| {
            _ = self.stats.commit_failed.fetchAdd(1, .monotonic);
            return err;
        };
        _ = self.stats.enriched.fetchAdd(tally.enriched, .monotonic);
        _ = self.stats.from_blacksky.fetchAdd(tally.blacksky, .monotonic);
        _ = self.stats.from_pds.fetchAdd(tally.pds, .monotonic);
        _ = self.stats.not_found.fetchAdd(tally.absent, .monotonic);
        _ = self.stats.retried.fetchAdd(tally.retried, .monotonic);
        _ = self.stats.changed.fetchAdd(report.summary.changed, .monotonic);
        return .worked;
    }

    pub fn stop(self: *Enricher) void {
        self.stopping.store(true, .release);
    }

    pub fn run(self: *Enricher) void {
        log.info("enriching at up to {d} requests/s; quality walk {d}/h, rest walk {d}/h", .{ self.options.requests_per_second, self.options.walk_per_hour, self.options.sweep_per_hour });
        while (!self.stopping.load(.acquire)) {
            const outcome = self.step() catch |err| blk: {
                if (err == error.OwnerStopped) return;
                log.warn("enrichment step failed: {s}", .{@errorName(err)});
                break :blk .idle;
            };
            if (outcome == .idle and !self.timer.sleep_ms(self.timer.context, self.options.idle_seconds * 1000)) return;
        }
    }
};

/// The transport the role runs with: one keep-alive client, every request
/// bounded in time and size.
pub const Http = struct {
    client: zat.HttpTransport,

    const deadline_ns = 15 * std.time.ns_per_s;
    const max_body = 4 << 20;

    pub fn init(io: std.Io, gpa: Allocator) Http {
        var client = zat.HttpTransport.init(io, gpa);
        client.keep_alive = true;
        client.user_agent = plc_tail.USER_AGENT;
        return .{ .client = client };
    }

    pub fn deinit(self: *Http) void {
        self.client.deinit();
    }

    pub fn transport(self: *Http) Transport {
        return .{ .context = self, .get = get };
    }

    fn get(context: *anyopaque, arena: Allocator, url: []const u8) Response {
        const self: *Http = @ptrCast(@alignCast(context));
        var result = self.client.fetch(.{ .url = url, .deadline_ns = deadline_ns, .max_response_size = max_body, .capture_headers = &.{"retry-after"} }) catch |err| {
            log.debug("request failed: {s}", .{@errorName(err)});
            return .{};
        };
        defer result.deinit(self.client.allocator);
        var response: Response = .{ .status = @backingInt(result.status), .body = arena.dupe(u8, result.body) catch return .{} };
        for (result.headers.entries) |header| response.retry_after_seconds = std.fmt.parseInt(u64, std.mem.trim(u8, header.value, " "), 10) catch null;
        return response;
    }
};

pub const WallTimer = struct {
    io: std.Io,

    pub fn timer(self: *WallTimer) Timer {
        return .{ .context = self, .now_ms = nowMs, .sleep_ms = sleepMs };
    }

    fn nowMs(context: *anyopaque) i64 {
        const self: *WallTimer = @ptrCast(@alignCast(context));
        return @intCast(@divFloor(std.Io.Timestamp.now(self.io, .real).nanoseconds, std.time.ns_per_ms));
    }

    fn sleepMs(context: *anyopaque, ms: u64) bool {
        const self: *WallTimer = @ptrCast(@alignCast(context));
        self.io.sleep(std.Io.Duration.fromMilliseconds(@intCast(ms)), .awake) catch return false;
        return true;
    }
};

test "a walk's period is its pool at the rate, or its range at the scan rate" {
    try std.testing.expectEqual(@as(?u64, 7443), walkPeriod(41_355, 41_355, 20_000, quality_scan_factor));
    try std.testing.expectEqual(@as(?u64, 4_752_000), walkPeriod(13_200_000, 13_200_000, 10_000, rest_scan_factor));
    try std.testing.expectEqual(@as(?u64, 594_000), walkPeriod(500_000, 13_200_000, 10_000, rest_scan_factor));
    try std.testing.expectEqual(@as(?u64, null), walkPeriod(5, 5, 0, rest_scan_factor));
}
