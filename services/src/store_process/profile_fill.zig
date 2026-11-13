//! The profile fill: a pass, made once in the life of a store, that copies
//! the descriptions and banners Turso holds into the actors here that have
//! none.
//!
//! A slot's store was loaded from a search-index bundle, which carries
//! neither field, so it holds them only for the accounts it has refreshed
//! since. The Worker serves Turso's at `/slot/control/profile-fill`, a page
//! of its `actors` table at a time in DID order (`src/handlers/slot-control.ts`),
//! behind the secret the control loop uses. This loop asks for the page
//! after its stored position and hands it to the owner, which writes the
//! rows and the new position in one commit (`Store.profileFill`): a field
//! is written only where the stored one is empty, and nothing else about an
//! actor moves. It asks Bluesky nothing and queues no refresh.
//!
//! One page a step. A step is skipped while the index bridge is more than
//! `lag_limit` entries behind the change log, because every actor filled is
//! an entry the bridge has to send to the index. A page that cannot be had
//! (no answer, a refusal, a body that is not a page) is counted and asked
//! for again after `failure_seconds`. When the Worker says its table has
//! ended the pass is finished, the store says so from then on, and the loop
//! ends; it never runs again for that store.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const control = @import("control.zig");
const enrich = @import("enrich.zig");
const owner_mod = @import("owner.zig");
const Allocator = std.mem.Allocator;
const Owner = owner_mod.Owner;
const Store = store_mod.Store;
const Submission = owner_mod.Submission;

const log = std.log.scoped(.profile_fill);

pub const path = "/slot/control/profile-fill";
/// The Worker refuses a larger page.
pub const max_page_size = 1000;

pub const Options = struct {
    /// The Worker's origin, without a trailing slash, and the slot secret:
    /// the control loop's.
    url: []const u8,
    secret: []const u8,
    /// Rows of Turso's table one page reads.
    page_size: usize = 500,
    /// Seconds between pages. With the default size, 13.2 million rows are
    /// 26,400 pages: 7 hours 20 minutes.
    step_seconds: u64 = 1,
    /// No page is asked for while the bridge is further behind than this.
    lag_limit: u64 = 50_000,
    /// The rest after a page that could not be had: the control loop's poll
    /// interval, stretched by up to `jitter`.
    failure_seconds: u64 = 30,
    jitter: f64 = 0.2,
};

pub const Failure = enum(u8) {
    none,
    /// No response: refused, timed out, redirected or over the size bound.
    unreachable_worker,
    /// 401 or 403: the secret is wrong or the Worker has none.
    unauthorized,
    /// Any other status, 410 among them: the Worker has stopped keeping
    /// Turso's actor tables.
    status,
    /// The body was not a page after the position asked from.
    malformed,
    /// The store could not be read, or the owner would not commit.
    store,
};

pub const Stats = struct {
    /// Pages applied by this process, and when the last one was.
    pages: std.atomic.Value(u64) = .init(0),
    last_page_at: std.atomic.Value(u64) = .init(0),
    /// Steps skipped because the bridge was behind, and when the last was.
    deferred: std.atomic.Value(u64) = .init(0),
    deferred_at: std.atomic.Value(u64) = .init(0),
    /// The bridge's lag at the last step.
    bridge_lag: std.atomic.Value(u64) = .init(0),
    finished: std.atomic.Value(bool) = .init(false),
    failures: std.atomic.Value(u64) = .init(0),
    unreachable_worker: std.atomic.Value(u64) = .init(0),
    unauthorized: std.atomic.Value(u64) = .init(0),
    malformed: std.atomic.Value(u64) = .init(0),
    last_failure: std.atomic.Value(Failure) = .init(.none),
    last_failure_status: std.atomic.Value(u16) = .init(0),
    last_failure_at: std.atomic.Value(u64) = .init(0),
};

/// What `/health` and the heartbeat say about the loop. Where the pass
/// stands is the store's to say (`owner.Health.profile_fill`).
pub const Report = struct {
    page_size: usize,
    step_seconds: u64,
    lag_limit: u64,
    pages: u64,
    last_page_at: u64,
    deferred: u64,
    deferred_at: u64,
    bridge_lag: u64,
    finished: bool,
    failures: u64,
    unreachable_worker: u64,
    unauthorized: u64,
    malformed: u64,
    last_failure: Failure,
    last_failure_status: u16,
    last_failure_at: u64,
};

pub const Step = enum {
    /// A page was committed.
    applied,
    /// The bridge is behind; nothing was asked for.
    deferred,
    /// The page could not be had; it has been counted.
    failed,
    /// The pass has reached the end of the Worker's table.
    finished,
};

pub const ParseError = error{ Malformed, OutOfMemory };

const Wire = struct {
    rows: []const struct { did: []const u8, description: []const u8, banner: []const u8 },
    next: ?[]const u8,
};

/// The page in `body`, asked for after `after` with `limit`. `Malformed`
/// unless its DIDs rise from above `after` and `next` is at or past the
/// last of them: the position stored is `next`, so a page out of order
/// would skip what lies between, and one that does not move would be asked
/// for again for ever.
pub fn parsePage(arena: Allocator, body: []const u8, after: []const u8, limit: usize) ParseError!Store.FillPage {
    const parsed = std.json.parseFromSliceLeaky(Wire, arena, body, .{ .ignore_unknown_fields = true }) catch |err| switch (err) {
        error.OutOfMemory => return error.OutOfMemory,
        else => return error.Malformed,
    };
    if (parsed.rows.len > limit) return error.Malformed;
    const rows = try arena.alloc(Store.FillRow, parsed.rows.len);
    var last = after;
    for (rows, parsed.rows) |*slot, one| {
        if (std.mem.order(u8, one.did, last) != .gt) return error.Malformed;
        last = one.did;
        slot.* = .{ .did = one.did, .description = one.description, .banner = one.banner };
    }
    if (parsed.next) |next| {
        if (!control.validDid(next) or std.mem.order(u8, next, last) == .lt or std.mem.order(u8, next, after) != .gt) return error.Malformed;
    }
    return .{ .after = after, .rows = rows, .next = parsed.next };
}

/// `text` as a query parameter's value.
fn queryValue(arena: Allocator, text: []const u8) ![]const u8 {
    var out: std.ArrayList(u8) = .empty;
    for (text) |c| switch (c) {
        'A'...'Z', 'a'...'z', '0'...'9', '-', '.', '_', '~', ':' => try out.append(arena, c),
        else => try out.print(arena, "%{X:0>2}", .{c}),
    };
    return out.items;
}

pub const Filler = struct {
    gpa: Allocator,
    owner: *Owner,
    transport: control.Transport,
    timer: enrich.Timer,
    options: Options,
    prng: std.Random.DefaultPrng = .init(0),
    stats: Stats = .{},
    stopping: std.atomic.Value(bool) = .init(false),
    authorization: []const u8 = "",
    /// The stored position, read at `start` and kept in step with every
    /// commit this loop makes.
    position: std.ArrayList(u8) = .empty,

    /// Read where the pass stands. Call once, before `step` or `run`.
    pub fn start(self: *Filler) !void {
        self.authorization = try std.fmt.allocPrint(self.gpa, "Bearer {s}", .{self.options.secret});
        errdefer self.deinit();
        try self.reload();
    }

    pub fn deinit(self: *Filler) void {
        self.gpa.free(self.authorization);
        self.authorization = "";
        self.position.deinit(self.gpa);
        self.position = .empty;
    }

    pub fn stop(self: *Filler) void {
        self.stopping.store(true, .release);
    }

    fn nowSeconds(self: *Filler) u64 {
        return @intCast(@max(@divFloor(self.timer.now_ms(self.timer.context), 1000), 0));
    }

    fn reload(self: *Filler) !void {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const store = try self.owner.acquire();
        defer self.owner.release();
        const state = try store_mod.read.profileFillState(store.db(), arena.allocator());
        self.position.clearRetainingCapacity();
        try self.position.appendSlice(self.gpa, state.position);
        self.stats.finished.store(state.finished_at != 0, .monotonic);
    }

    pub fn report(self: *Filler) Report {
        const s = &self.stats;
        return .{
            .page_size = self.options.page_size,
            .step_seconds = self.options.step_seconds,
            .lag_limit = self.options.lag_limit,
            .pages = s.pages.load(.monotonic),
            .last_page_at = s.last_page_at.load(.monotonic),
            .deferred = s.deferred.load(.monotonic),
            .deferred_at = s.deferred_at.load(.monotonic),
            .bridge_lag = s.bridge_lag.load(.monotonic),
            .finished = s.finished.load(.monotonic),
            .failures = s.failures.load(.monotonic),
            .unreachable_worker = s.unreachable_worker.load(.monotonic),
            .unauthorized = s.unauthorized.load(.monotonic),
            .malformed = s.malformed.load(.monotonic),
            .last_failure = s.last_failure.load(.monotonic),
            .last_failure_status = s.last_failure_status.load(.monotonic),
            .last_failure_at = s.last_failure_at.load(.monotonic),
        };
    }

    fn failed(self: *Filler, what: Failure, status: u16) void {
        const s = &self.stats;
        _ = s.failures.fetchAdd(1, .monotonic);
        switch (what) {
            .unreachable_worker => _ = s.unreachable_worker.fetchAdd(1, .monotonic),
            .unauthorized => _ = s.unauthorized.fetchAdd(1, .monotonic),
            .malformed => _ = s.malformed.fetchAdd(1, .monotonic),
            .none, .status, .store => {},
        }
        s.last_failure.store(what, .monotonic);
        s.last_failure_status.store(status, .monotonic);
        s.last_failure_at.store(self.nowSeconds(), .monotonic);
        log.warn("{s}: {t} (status {d})", .{ path, what, status });
    }

    /// How far the index bridge is behind the change log; 0 for a store no
    /// bridge has read.
    fn bridgeLag(self: *Filler) !u64 {
        const store = try self.owner.acquire();
        defer self.owner.release();
        const position = try store_mod.read.counter(store.db(), .bridge) orelse return 0;
        return store.next_change -| position;
    }

    /// One page, or the reason there was none. An error is the store's: it
    /// could not be read or would not commit, and is counted.
    pub fn step(self: *Filler) !Step {
        return self.page() catch |err| {
            self.failed(.store, 0);
            return err;
        };
    }

    fn page(self: *Filler) !Step {
        if (self.stats.finished.load(.monotonic)) return .finished;
        const lag = try self.bridgeLag();
        self.stats.bridge_lag.store(lag, .monotonic);
        if (lag > self.options.lag_limit) {
            _ = self.stats.deferred.fetchAdd(1, .monotonic);
            self.stats.deferred_at.store(self.nowSeconds(), .monotonic);
            return .deferred;
        }

        const submission = try Submission.create(self.gpa);
        defer submission.destroy();
        const a = submission.arena.allocator();
        const url = try std.fmt.allocPrint(a, "{s}" ++ path ++ "?after={s}&limit={d}", .{ self.options.url, try queryValue(a, self.position.items), self.options.page_size });
        const response = self.transport.get(self.transport.context, a, url, self.authorization);
        const refusal: Failure = switch (response.status) {
            200 => .none,
            0 => .unreachable_worker,
            401, 403 => .unauthorized,
            else => .status,
        };
        if (refusal != .none) {
            self.failed(refusal, response.status);
            return .failed;
        }
        const fill = parsePage(a, response.body, try a.dupe(u8, self.position.items), self.options.page_size) catch |err| switch (err) {
            error.OutOfMemory => return err,
            error.Malformed => {
                self.failed(.malformed, response.status);
                return .failed;
            },
        };
        submission.fill = fill;
        try self.owner.call(submission);

        // Not applied: the store is at another position than this loop
        // held, which a commit that landed and was then replayed leaves.
        // The store's word is the one that counts.
        if (!submission.filled.applied) {
            try self.reload();
            return if (self.stats.finished.load(.monotonic)) .finished else .applied;
        }
        if (fill.next) |next| {
            self.position.clearRetainingCapacity();
            try self.position.appendSlice(self.gpa, next);
        }
        _ = self.stats.pages.fetchAdd(1, .monotonic);
        self.stats.last_page_at.store(self.nowSeconds(), .monotonic);
        if (!submission.filled.finished) return .applied;
        self.stats.finished.store(true, .monotonic);
        log.info("the profile fill reached the end of the Worker's table and is finished for this store", .{});
        return .finished;
    }

    /// Milliseconds to rest after a step that ended as `outcome`.
    pub fn rest(self: *Filler, outcome: Step) u64 {
        if (outcome == .deferred) return @max(self.options.step_seconds, 1) * 1000;
        if (outcome != .failed) return self.options.step_seconds * 1000;
        const base: f64 = @floatFromInt(self.options.failure_seconds * 1000);
        return @intFromFloat(base * (1 + self.options.jitter * self.prng.random().float(f64)));
    }

    pub fn run(self: *Filler) void {
        if (self.stats.finished.load(.monotonic)) {
            log.info("the profile fill has already run for this store", .{});
            return;
        }
        log.info("filling descriptions and banners from {s}, {d} rows every {d} s, from {s}", .{
            self.options.url, self.options.page_size, self.options.step_seconds, if (self.position.items.len == 0) "the start" else self.position.items,
        });
        while (!self.stopping.load(.acquire)) {
            const outcome = self.step() catch |err| blk: {
                if (err == error.OwnerStopped) return;
                log.warn("profile fill step failed: {s}", .{@errorName(err)});
                break :blk Step.failed;
            };
            if (outcome == .finished) return;
            if (!self.timer.sleep_ms(self.timer.context, self.rest(outcome))) return;
        }
    }
};

const testing = std.testing;

test "a page is taken only when its DIDs rise from the position asked from" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const page = try parsePage(arena,
        \\{"rows":[{"did":"did:plc:b","description":"line\none","banner":""},{"did":"did:plc:c","description":"","banner":"bafkc"}],"next":"did:plc:d","extra":1}
    , "did:plc:a", 2);
    try testing.expectEqualStrings("did:plc:a", page.after);
    try testing.expectEqual(@as(usize, 2), page.rows.len);
    try testing.expectEqualStrings("line\none", page.rows[0].description);
    try testing.expectEqualStrings("bafkc", page.rows[1].banner);
    try testing.expectEqualStrings("did:plc:d", page.next.?);

    const empty = try parsePage(arena, "{\"rows\":[],\"next\":\"did:plc:b\"}", "did:plc:a", 500);
    try testing.expectEqual(@as(usize, 0), empty.rows.len);
    const end = try parsePage(arena, "{\"rows\":[{\"did\":\"did:plc:b\",\"description\":\"x\",\"banner\":\"\"}],\"next\":null}", "", 500);
    try testing.expect(end.next == null and end.rows.len == 1);
    // The last DID read may be the last row's.
    _ = try parsePage(arena, "{\"rows\":[{\"did\":\"did:plc:b\",\"description\":\"x\",\"banner\":\"\"}],\"next\":\"did:plc:b\"}", "did:plc:a", 500);

    for ([_][]const u8{
        "",
        "not json",
        "{\"rows\":[]}",
        "{\"next\":null}",
        "{\"rows\":[{\"did\":\"did:plc:b\",\"description\":\"x\"}],\"next\":null}",
        "{\"rows\":[{\"did\":\"did:plc:b\",\"description\":\"x\",\"banner\":7}],\"next\":null}",
        // at or before the position asked from
        "{\"rows\":[{\"did\":\"did:plc:a\",\"description\":\"x\",\"banner\":\"\"}],\"next\":\"did:plc:b\"}",
        "{\"rows\":[],\"next\":\"did:plc:a\"}",
        "{\"rows\":[],\"next\":\"did:plc:0\"}",
        // out of order, or repeated
        "{\"rows\":[{\"did\":\"did:plc:c\",\"description\":\"x\",\"banner\":\"\"},{\"did\":\"did:plc:b\",\"description\":\"x\",\"banner\":\"\"}],\"next\":\"did:plc:d\"}",
        "{\"rows\":[{\"did\":\"did:plc:b\",\"description\":\"x\",\"banner\":\"\"},{\"did\":\"did:plc:b\",\"description\":\"y\",\"banner\":\"\"}],\"next\":\"did:plc:d\"}",
        // a next position before the last row, or one that is no DID
        "{\"rows\":[{\"did\":\"did:plc:c\",\"description\":\"x\",\"banner\":\"\"}],\"next\":\"did:plc:b\"}",
        "{\"rows\":[],\"next\":\"\"}",
        "{\"rows\":[],\"next\":\"eve.test\"}",
        "{\"rows\":[],\"next\":\"did:plc:b c\"}",
    }) |body| try testing.expectError(error.Malformed, parsePage(arena, body, "did:plc:a", 500));
    // more rows than were asked for
    try testing.expectError(error.Malformed, parsePage(arena, "{\"rows\":[{\"did\":\"did:plc:b\",\"description\":\"x\",\"banner\":\"\"},{\"did\":\"did:plc:c\",\"description\":\"x\",\"banner\":\"\"}],\"next\":\"did:plc:c\"}", "did:plc:a", 1));
}

test "a position is sent as a query value" {
    var arena_state: std.heap.ArenaAllocator = .init(testing.allocator);
    defer arena_state.deinit();
    try testing.expectEqualStrings("did:plc:abc234", try queryValue(arena_state.allocator(), "did:plc:abc234"));
    try testing.expectEqualStrings("did:web:a.test%253A8080%26x%3D1%23f%2B", try queryValue(arena_state.allocator(), "did:web:a.test%3A8080&x=1#f+"));
}
