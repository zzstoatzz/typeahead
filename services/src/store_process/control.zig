//! The control loop: what an operator or a user asked the Worker for, pulled
//! by this slot and applied to its own store.
//!
//! A slot takes no inbound control. The Worker keeps a durable control record
//! (`src/control-store.ts`, the `typeahead-control` D1 database) and serves
//! it read-only at `/slot/control/*`; every slot asks for it on its own
//! schedule. So a change reaches any number of slots, a slot that was down
//! when it was made, and a slot bootstrapped afterwards.
//!
//! Two things come this way, and nothing else is accepted:
//!
//! - **Moderation policy.** The whole of it, as one snapshot under a
//!   revision. The loop makes the store's overrides and blocked domains
//!   equal to the snapshot, as `override_set`, `override_cleared`,
//!   `domain_blocked` and `domain_unblocked` changes through the owner, and
//!   stores the revision in the same commit. A rule the snapshot lacks is
//!   removed, so a slot cannot keep one that was withdrawn however many
//!   changes it missed, and applying a snapshot twice changes nothing. A
//!   store that has applied no revision asks for the snapshot
//!   unconditionally, which is how a bootstrapped store drops whatever its
//!   policy file held that the Worker no longer does.
//! - **Indexing requests.** DIDs, in sequence order. Each is treated as an
//!   `#identity` event for that DID (`events.Changes.indexRequest`): the
//!   actor is created if unknown and queued for identity resolution, whose
//!   result requests a profile refresh. The Worker's word is only that the
//!   DID is worth a look; the handle and PDS are what this slot's own
//!   resolver finds. The last sequence is stored in the same commit.
//!
//! Every answer names the control database it came from, its generation. A
//! stored revision or sequence means something only against that database:
//! one that is recreated starts both again, and one restored to an earlier
//! time moves both back. So when an answer carries a generation other than
//! the stored one, the loop drops both positions in the commit that takes
//! the answer. The snapshot is then applied whatever its revision, and
//! requests are taken from the start of the log, which the Worker serves to
//! a slot of another generation. Requests the slot had already taken queue
//! again, which is harmless: the identity queue holds one entry an actor.
//! Starting from the newest sequence instead would lose whatever was asked
//! between the restore and this slot's next poll.
//!
//! A snapshot or a page that is malformed in any part is refused whole. A
//! snapshot older than the applied revision of the same generation is
//! refused: there the revision only rises, so a lower one is a stale or
//! replayed answer. While
//! the Worker cannot be reached the slot serves with the policy it last
//! applied, and `/health` and the heartbeat report how long that has been.

const std = @import("std");
const zat = @import("zat");
const plc_tail = @import("../plc_tail.zig");
const store_mod = @import("../store/root.zig");
const enrich = @import("enrich.zig");
const events = @import("events.zig");
const owner_mod = @import("owner.zig");
const Allocator = std.mem.Allocator;
const Change = store_mod.Change;
const Owner = owner_mod.Owner;
const Submission = owner_mod.Submission;

const log = std.log.scoped(.control);

pub const policy_path = "/slot/control/policy";
pub const requests_path = "/slot/control/index-requests";
/// The Worker refuses a slot secret shorter than this, so the role does too.
pub const min_secret_bytes = 32;
/// A response larger than this is not read. The policy is a few rows; at
/// 80 bytes an override this is room for about 50,000.
pub const max_response_bytes = 4 << 20;
pub const max_rules = 100_000;
/// A revision or sequence above this is refused. The Worker's are JavaScript
/// numbers, exact only to here, and what this role stores it also reports
/// as a signed 64-bit span attribute.
pub const max_position = 1 << 53;

/// `value` as a span attribute, which is a signed 64-bit integer: one too
/// large to be one is reported as the largest, instead of stopping the
/// process on the conversion.
pub fn attribute(value: u64) i64 {
    return std.math.cast(i64, value) orelse std.math.maxInt(i64);
}

pub const Options = struct {
    /// The Worker's origin, without a trailing slash.
    url: []const u8,
    secret: []const u8,
    poll_seconds: u64 = 30,
    /// Each rest between polls is stretched by up to this fraction, so two
    /// slots started together drift apart.
    jitter: f64 = 0.2,
    /// Indexing requests asked for in one page; the Worker allows 500.
    request_page: usize = 100,
    /// Pages taken in one poll while the Worker says more remain.
    request_pages: usize = 10,
};

pub const Response = struct {
    /// 0 when no response arrived.
    status: u16 = 0,
    body: []const u8 = "",
};

pub const Transport = struct {
    context: *anyopaque,
    get: *const fn (context: *anyopaque, arena: Allocator, url: []const u8, authorization: []const u8) Response,
};

pub const Failure = enum(u8) {
    none,
    /// No response: refused, timed out, redirected or over the size bound.
    unreachable_worker,
    /// 401 or 403: the secret is wrong or the Worker has none.
    unauthorized,
    /// Any other status the endpoint should not give.
    status,
    /// The body was not the record the endpoint serves.
    malformed,
    /// A policy revision below the one already applied.
    older_revision,
    /// The owner would not commit.
    commit,
};

pub const Stats = struct {
    started_at: std.atomic.Value(u64) = .init(0),
    /// Unix seconds of the last answer accepted from each endpoint, applied
    /// or already held. 0 before the first.
    policy_checked_at: std.atomic.Value(u64) = .init(0),
    requests_checked_at: std.atomic.Value(u64) = .init(0),
    policy_applied_at: std.atomic.Value(u64) = .init(0),
    /// The control database the positions belong to; 0 before the first.
    generation: std.atomic.Value(u64) = .init(0),
    /// Times an answer came from another control database than the stored
    /// positions did, and they were dropped.
    generation_changes: std.atomic.Value(u64) = .init(0),
    /// 0 with `policy_known` false: no revision has been applied.
    policy_revision: std.atomic.Value(u64) = .init(0),
    policy_known: std.atomic.Value(bool) = .init(false),
    requests_sequence: std.atomic.Value(u64) = .init(0),
    snapshots_applied: std.atomic.Value(u64) = .init(0),
    /// Store changes the snapshots came to.
    rules_changed: std.atomic.Value(u64) = .init(0),
    requests_queued: std.atomic.Value(u64) = .init(0),
    /// Requests whose DID this role does not index.
    requests_skipped: std.atomic.Value(u64) = .init(0),
    polls: std.atomic.Value(u64) = .init(0),
    failures: std.atomic.Value(u64) = .init(0),
    unreachable_worker: std.atomic.Value(u64) = .init(0),
    unauthorized: std.atomic.Value(u64) = .init(0),
    malformed: std.atomic.Value(u64) = .init(0),
    older_revision: std.atomic.Value(u64) = .init(0),
    last_failure: std.atomic.Value(Failure) = .init(.none),
    last_failure_status: std.atomic.Value(u16) = .init(0),
    last_failure_at: std.atomic.Value(u64) = .init(0),
};

/// What `/health` and the heartbeat say about the loop.
pub const Report = struct {
    generation: ?u64,
    generation_changes: u64,
    policy_revision: ?u64,
    policy_checked_at: u64,
    policy_applied_at: u64,
    requests_sequence: u64,
    requests_checked_at: u64,
    /// Seconds since both endpoints last answered; since the start, before
    /// they first have. This is the number to alert on.
    age_seconds: u64,
    snapshots_applied: u64,
    rules_changed: u64,
    requests_queued: u64,
    requests_skipped: u64,
    polls: u64,
    failures: u64,
    unreachable_worker: u64,
    unauthorized: u64,
    malformed: u64,
    older_revision: u64,
    last_failure: Failure,
    last_failure_status: u16,
    last_failure_at: u64,
    poll_seconds: u64,
};

pub const Action = enum { show, hide };
pub const Rule = struct { did: []const u8, action: Action };

/// A snapshot that passed every check.
pub const Policy = struct {
    generation: u64,
    revision: u64,
    overrides: []const Rule,
    /// Lowercase, without repeats.
    domains: []const []const u8,
};

pub const Request = struct { seq: u64, did: []const u8 };
pub const Page = struct { generation: u64, requests: []const Request, more: bool };

pub const ParseError = error{ Malformed, OutOfMemory };

const PolicyWire = struct {
    generation: u64,
    revision: u64,
    overrides: []const struct { did: []const u8, action: []const u8 },
    blocked_domains: []const []const u8,
};

const PageWire = struct {
    generation: u64,
    requests: []const struct { seq: u64, did: []const u8 },
    more: bool,
};

fn wire(comptime T: type, arena: Allocator, body: []const u8) ParseError!T {
    return std.json.parseFromSliceLeaky(T, arena, body, .{ .ignore_unknown_fields = true }) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => error.Malformed,
    };
}

/// Printable ASCII with no space.
fn visibleAscii(text: []const u8) bool {
    for (text) |c| if (c < 0x21 or c > 0x7e) return false;
    return true;
}

/// The subject of an override. The Worker applies the same rule before it
/// writes one (`src/control-rules.ts`), because a snapshot is refused whole
/// over a single subject that fails it; `src/control-rule-vectors.json`
/// holds values both must judge alike.
pub fn validDid(did: []const u8) bool {
    return std.mem.startsWith(u8, did, "did:") and did.len <= store_mod.keys.max_did and visibleAscii(did);
}

/// A blocked domain, under the same arrangement as `validDid`.
pub fn validDomain(name: []const u8) bool {
    if (name.len == 0 or name.len > store_mod.keys.max_domain) return false;
    return std.mem.indexOfScalar(u8, name, '.') != null and visibleAscii(name);
}

/// The snapshot in `body`, or `Malformed` when any part of it is not a rule
/// the store can hold. Nothing is taken from a snapshot that fails: a
/// partial one would read as the removal of every rule it lacks.
pub fn parsePolicy(arena: Allocator, body: []const u8) ParseError!Policy {
    const parsed = try wire(PolicyWire, arena, body);
    if (parsed.revision > max_position or parsed.generation > max_position) return error.Malformed;
    if (parsed.overrides.len > max_rules or parsed.blocked_domains.len > max_rules) return error.Malformed;

    const overrides = try arena.alloc(Rule, parsed.overrides.len);
    var seen: std.StringHashMapUnmanaged(void) = .empty;
    for (overrides, parsed.overrides) |*slot, one| {
        const action = std.meta.stringToEnum(Action, one.action) orelse return error.Malformed;
        if (!validDid(one.did)) return error.Malformed;
        if ((try seen.getOrPut(arena, one.did)).found_existing) return error.Malformed;
        slot.* = .{ .did = one.did, .action = action };
    }

    const domains = try arena.alloc([]const u8, parsed.blocked_domains.len);
    seen = .empty;
    for (domains, parsed.blocked_domains) |*slot, one| {
        if (!validDomain(one)) return error.Malformed;
        const lower = try std.ascii.allocLowerString(arena, one);
        if ((try seen.getOrPut(arena, lower)).found_existing) return error.Malformed;
        slot.* = lower;
    }
    return .{ .generation = parsed.generation, .revision = parsed.revision, .overrides = overrides, .domains = domains };
}

/// The page in `body`, asked for by a slot holding sequence `after` of
/// generation `held`. `Malformed` unless its sequences rise, from above
/// `after` when the page is of that generation and from the start of the
/// log when it is of another: the position stored is the last one, so a
/// page out of order would skip what lies between.
pub fn parsePage(arena: Allocator, body: []const u8, held: ?u64, after: u64, limit: usize) ParseError!Page {
    const parsed = try wire(PageWire, arena, body);
    if (parsed.requests.len > limit or parsed.generation > max_position) return error.Malformed;
    const requests = try arena.alloc(Request, parsed.requests.len);
    var last: u64 = if (held != null and held.? == parsed.generation) after else 0;
    for (requests, parsed.requests) |*slot, one| {
        if (one.seq <= last or one.seq > max_position) return error.Malformed;
        last = one.seq;
        slot.* = .{ .seq = one.seq, .did = one.did };
    }
    if (parsed.more and requests.len == 0) return error.Malformed;
    return .{ .generation = parsed.generation, .requests = requests, .more = parsed.more };
}

/// The changes that make a store holding `overrides` and `domains` hold
/// `policy` instead. Strings are borrowed from the arguments.
pub fn difference(
    arena: Allocator,
    overrides: []const store_mod.read.Policy,
    domains: []const store_mod.read.Policy,
    policy: Policy,
) ![]const Change {
    var changes: std.ArrayList(Change) = .empty;

    var held: std.StringHashMapUnmanaged(store_mod.record.Override) = .empty;
    for (overrides) |one| try held.put(arena, one.subject, one.entry.action);
    var wanted: std.StringHashMapUnmanaged(void) = .empty;
    for (policy.overrides) |rule| {
        try wanted.put(arena, rule.did, {});
        const stored = held.get(rule.did) orelse .none;
        switch (rule.action) {
            .show => if (stored != .show) try changes.append(arena, .{ .override_set = .{ .did = rule.did, .action = .show } }),
            .hide => if (stored != .hide) try changes.append(arena, .{ .override_set = .{ .did = rule.did, .action = .hide } }),
        }
    }
    for (overrides) |one| {
        if (!wanted.contains(one.subject)) try changes.append(arena, .{ .override_cleared = one.subject });
    }

    var blocked: std.StringHashMapUnmanaged(void) = .empty;
    for (domains) |one| try blocked.put(arena, one.subject, {});
    wanted = .empty;
    for (policy.domains) |name| {
        try wanted.put(arena, name, {});
        if (!blocked.contains(name)) try changes.append(arena, .{ .domain_blocked = .{ .domain = name } });
    }
    for (domains) |one| {
        if (!wanted.contains(one.subject)) try changes.append(arena, .{ .domain_unblocked = one.subject });
    }
    return changes.items;
}

pub const Controller = struct {
    gpa: Allocator,
    owner: *Owner,
    transport: Transport,
    timer: enrich.Timer,
    options: Options,
    prng: std.Random.DefaultPrng = .init(0),
    stats: Stats = .{},
    stopping: std.atomic.Value(bool) = .init(false),
    authorization: []const u8 = "",
    /// What the store holds, read at `start` and kept in step with every
    /// commit this loop makes.
    generation: ?u64 = null,
    policy_revision: ?u64 = null,
    requests_sequence: u64 = 0,

    /// Read the stored positions. Call once, before `step` or `run`.
    pub fn start(self: *Controller) !void {
        self.authorization = try std.fmt.allocPrint(self.gpa, "Bearer {s}", .{self.options.secret});
        errdefer self.deinit();
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const store = try self.owner.acquire();
        defer self.owner.release();
        const cursors = try store.cursors(arena.allocator());
        self.generation = cursors.control_generation;
        self.policy_revision = cursors.control_policy;
        self.requests_sequence = cursors.control_requests orelse 0;
        self.stats.generation.store(cursors.control_generation orelse 0, .monotonic);
        self.stats.started_at.store(self.nowSeconds(), .monotonic);
        self.stats.policy_revision.store(cursors.control_policy orelse 0, .monotonic);
        self.stats.policy_known.store(cursors.control_policy != null, .monotonic);
        self.stats.requests_sequence.store(self.requests_sequence, .monotonic);
    }

    pub fn deinit(self: *Controller) void {
        self.gpa.free(self.authorization);
        self.authorization = "";
    }

    pub fn stop(self: *Controller) void {
        self.stopping.store(true, .release);
    }

    fn nowSeconds(self: *Controller) u64 {
        return @intCast(@max(@divFloor(self.timer.now_ms(self.timer.context), 1000), 0));
    }

    pub fn report(self: *Controller) Report {
        const s = &self.stats;
        const policy_at = s.policy_checked_at.load(.monotonic);
        const requests_at = s.requests_checked_at.load(.monotonic);
        const answered = @min(policy_at, requests_at);
        const since = if (answered == 0) s.started_at.load(.monotonic) else answered;
        const generation = s.generation.load(.monotonic);
        return .{
            .generation = if (generation == 0) null else generation,
            .generation_changes = s.generation_changes.load(.monotonic),
            .policy_revision = if (s.policy_known.load(.monotonic)) s.policy_revision.load(.monotonic) else null,
            .policy_checked_at = policy_at,
            .policy_applied_at = s.policy_applied_at.load(.monotonic),
            .requests_sequence = s.requests_sequence.load(.monotonic),
            .requests_checked_at = requests_at,
            .age_seconds = self.nowSeconds() -| since,
            .snapshots_applied = s.snapshots_applied.load(.monotonic),
            .rules_changed = s.rules_changed.load(.monotonic),
            .requests_queued = s.requests_queued.load(.monotonic),
            .requests_skipped = s.requests_skipped.load(.monotonic),
            .polls = s.polls.load(.monotonic),
            .failures = s.failures.load(.monotonic),
            .unreachable_worker = s.unreachable_worker.load(.monotonic),
            .unauthorized = s.unauthorized.load(.monotonic),
            .malformed = s.malformed.load(.monotonic),
            .older_revision = s.older_revision.load(.monotonic),
            .last_failure = s.last_failure.load(.monotonic),
            .last_failure_status = s.last_failure_status.load(.monotonic),
            .last_failure_at = s.last_failure_at.load(.monotonic),
            .poll_seconds = self.options.poll_seconds,
        };
    }

    fn failed(self: *Controller, what: Failure, status: u16, endpoint: []const u8) void {
        const s = &self.stats;
        _ = s.failures.fetchAdd(1, .monotonic);
        switch (what) {
            .unreachable_worker => _ = s.unreachable_worker.fetchAdd(1, .monotonic),
            .unauthorized => _ = s.unauthorized.fetchAdd(1, .monotonic),
            .malformed => _ = s.malformed.fetchAdd(1, .monotonic),
            .older_revision => _ = s.older_revision.fetchAdd(1, .monotonic),
            .none, .status, .commit => {},
        }
        s.last_failure.store(what, .monotonic);
        s.last_failure_status.store(status, .monotonic);
        s.last_failure_at.store(self.nowSeconds(), .monotonic);
        log.warn("{s}: {t} (status {d})", .{ endpoint, what, status });
    }

    /// False when `response` is one to read; otherwise it has been counted.
    fn refused(self: *Controller, response: Response, endpoint: []const u8) bool {
        const what: Failure = switch (response.status) {
            200 => return false,
            0 => .unreachable_worker,
            401, 403 => .unauthorized,
            else => .status,
        };
        self.failed(what, response.status, endpoint);
        return true;
    }

    fn commit(self: *Controller, submission: *Submission, endpoint: []const u8) !void {
        self.owner.call(submission) catch |err| {
            self.failed(.commit, 0, endpoint);
            return err;
        };
        if (submission.summary.rejected > 0) log.warn("{s}: the store refused {d} changes", .{ endpoint, submission.summary.rejected });
    }

    const Held = struct { generation: u64, revision: u64 };

    /// After a commit that carried a position of `generation`: when that is
    /// not the generation held, the store dropped both positions in that
    /// commit, and so does the loop.
    fn adopt(self: *Controller, generation: u64) void {
        if (self.generation != null and self.generation.? == generation) return;
        if (self.generation) |old| {
            _ = self.stats.generation_changes.fetchAdd(1, .monotonic);
            log.warn("the control database's generation changed from {d} to {d}: the stored policy revision and request sequence are dropped", .{ old, generation });
        }
        self.generation = generation;
        self.policy_revision = null;
        self.requests_sequence = 0;
        self.stats.generation.store(generation, .monotonic);
        self.stats.policy_known.store(false, .monotonic);
        self.stats.policy_revision.store(0, .monotonic);
        self.stats.requests_sequence.store(0, .monotonic);
    }

    /// Ask for the policy and make the store equal to it. True when the
    /// Worker answered and the store now holds its revision.
    pub fn pollPolicy(self: *Controller) !bool {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const a = arena.allocator();
        const held: ?Held = if (self.generation != null and self.policy_revision != null) .{ .generation = self.generation.?, .revision = self.policy_revision.? } else null;
        const url = if (held) |position|
            try std.fmt.allocPrint(a, "{s}" ++ policy_path ++ "?generation={d}&after={d}", .{ self.options.url, position.generation, position.revision })
        else
            try std.fmt.allocPrint(a, "{s}" ++ policy_path, .{self.options.url});
        const response = self.transport.get(self.transport.context, a, url, self.authorization);
        if (response.status == 304 and held != null) {
            self.stats.policy_checked_at.store(self.nowSeconds(), .monotonic);
            return true;
        }
        if (self.refused(response, policy_path)) return false;
        const policy = parsePolicy(a, response.body) catch |err| switch (err) {
            error.OutOfMemory => return err,
            error.Malformed => {
                self.failed(.malformed, response.status, policy_path);
                return false;
            },
        };
        if (held) |position| if (position.generation == policy.generation) {
            if (policy.revision < position.revision) {
                self.failed(.older_revision, response.status, policy_path);
                return false;
            }
            if (policy.revision == position.revision) {
                self.stats.policy_checked_at.store(self.nowSeconds(), .monotonic);
                return true;
            }
        };

        const submission = try Submission.create(self.gpa);
        defer submission.destroy();
        const owned = submission.arena.allocator();
        {
            const store = try self.owner.acquire();
            defer self.owner.release();
            const overrides = try store_mod.read.overrides(store.db(), owned);
            const domains = try store_mod.read.blockedDomains(store.db(), owned);
            submission.changes = try difference(owned, overrides, domains, policy);
        }
        submission.cursor = .{ .control_policy = .{ .generation = policy.generation, .at = policy.revision } };
        try self.commit(submission, policy_path);

        self.adopt(policy.generation);
        self.policy_revision = policy.revision;
        const now = self.nowSeconds();
        const s = &self.stats;
        s.policy_revision.store(policy.revision, .monotonic);
        s.policy_known.store(true, .monotonic);
        s.policy_checked_at.store(now, .monotonic);
        s.policy_applied_at.store(now, .monotonic);
        _ = s.snapshots_applied.fetchAdd(1, .monotonic);
        _ = s.rules_changed.fetchAdd(submission.changes.len, .monotonic);
        log.info("policy revision {d} applied: {d} overrides, {d} blocked domains, {d} changes", .{ policy.revision, policy.overrides.len, policy.domains.len, submission.changes.len });
        return true;
    }

    /// Take the indexing requests after the stored sequence, a page at a
    /// time. True when the Worker answered every page asked for.
    pub fn pollRequests(self: *Controller) !bool {
        for (0..self.options.request_pages) |_| switch (try self.requestPage()) {
            .refused => return false,
            .more => {},
            .end => break,
        };
        self.stats.requests_checked_at.store(self.nowSeconds(), .monotonic);
        return true;
    }

    fn requestPage(self: *Controller) !enum { refused, more, end } {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const a = arena.allocator();
        const url = if (self.generation) |generation|
            try std.fmt.allocPrint(a, "{s}" ++ requests_path ++ "?generation={d}&after={d}&limit={d}", .{ self.options.url, generation, self.requests_sequence, self.options.request_page })
        else
            try std.fmt.allocPrint(a, "{s}" ++ requests_path ++ "?after=0&limit={d}", .{ self.options.url, self.options.request_page });
        const response = self.transport.get(self.transport.context, a, url, self.authorization);
        if (self.refused(response, requests_path)) return .refused;
        const page = parsePage(a, response.body, self.generation, self.requests_sequence, self.options.request_page) catch |err| switch (err) {
            error.OutOfMemory => return err,
            error.Malformed => {
                self.failed(.malformed, response.status, requests_path);
                return .refused;
            },
        };
        if (page.requests.len == 0) return .end;

        const submission = try Submission.create(self.gpa);
        defer submission.destroy();
        var changes: events.Changes = .{ .arena = submission.arena.allocator() };
        var skipped: u64 = 0;
        for (page.requests) |request| {
            if (!events.validLabelDid(request.did)) {
                skipped += 1;
                continue;
            }
            try changes.indexRequest(try submission.arena.allocator().dupe(u8, request.did));
        }
        const last = page.requests[page.requests.len - 1].seq;
        submission.changes = changes.items();
        submission.cursor = .{ .control_requests = .{ .generation = page.generation, .at = last } };
        try self.commit(submission, requests_path);

        self.adopt(page.generation);
        self.requests_sequence = last;
        const s = &self.stats;
        s.requests_sequence.store(last, .monotonic);
        _ = s.requests_queued.fetchAdd(page.requests.len - skipped, .monotonic);
        _ = s.requests_skipped.fetchAdd(skipped, .monotonic);
        log.info("indexing requests through {d}: {d} queued, {d} skipped", .{ last, page.requests.len - skipped, skipped });
        return if (page.more) .more else .end;
    }

    /// One poll of both endpoints. Neither waits on the other: a Worker
    /// that serves requests but not policy still gets its requests taken.
    pub fn step(self: *Controller) !void {
        _ = self.stats.polls.fetchAdd(1, .monotonic);
        _ = try self.pollPolicy();
        _ = try self.pollRequests();
    }

    /// Milliseconds to rest before the next poll.
    pub fn rest(self: *Controller) u64 {
        const base: f64 = @floatFromInt(self.options.poll_seconds * 1000);
        return @intFromFloat(base * (1 + self.options.jitter * self.prng.random().float(f64)));
    }

    pub fn run(self: *Controller) void {
        log.info("pulling the control record from {s} every {d} s", .{ self.options.url, self.options.poll_seconds });
        while (!self.stopping.load(.acquire)) {
            self.step() catch |err| {
                if (err == error.OwnerStopped) return;
                log.warn("control step failed: {s}", .{@errorName(err)});
            };
            if (!self.timer.sleep_ms(self.timer.context, self.rest())) return;
        }
    }
};

/// The transport the role runs with: every request bounded in time and
/// size, the bearer secret sent to the configured origin only, and a
/// redirect refused rather than followed with it.
pub const Http = struct {
    client: zat.HttpTransport,

    const deadline_ns = 15 * std.time.ns_per_s;

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

    fn get(context: *anyopaque, arena: Allocator, url: []const u8, authorization: []const u8) Response {
        const self: *Http = @ptrCast(@alignCast(context));
        var result = self.client.fetch(.{
            .url = url,
            .authorization = authorization,
            .accept = "application/json",
            .deadline_ns = deadline_ns,
            .max_response_size = max_response_bytes,
            .on_redirect = .refuse,
        }) catch |err| {
            log.debug("request failed: {s}", .{@errorName(err)});
            return .{};
        };
        defer result.deinit(self.client.allocator);
        return .{ .status = @backingInt(result.status), .body = arena.dupe(u8, result.body) catch return .{} };
    }
};
