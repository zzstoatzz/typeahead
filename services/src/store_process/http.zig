//! The store role's HTTP surface: `/health`, and authenticated reads under
//! `/store/`. Every body is one of the types below, written as JSON.
//!
//! | request                                              | answer                                  |
//! |------------------------------------------------------|-----------------------------------------|
//! | `GET /health`                                        | `HealthBody`, no auth                   |
//! | `GET /store/actor?did=`                              | `{"actor": ActorBody}` or 404           |
//! | `GET /store/handle?handle=`                          | `{"actors": [ActorBody]}`               |
//!
//! `/store/*` needs `Authorization: Bearer $STORE_READ_SECRET` and answers
//! 404 when no secret is configured.
//!
//! Every `/store/*` read goes through a read-only handle on the store,
//! refreshed per request. It takes no lock the owner takes, so a read
//! cannot hold a commit back; it sees whole commits, as of the
//! refresh. The handle keeps table metadata of its own. The role opens it
//! after the index bridge and the upstream consumers are running, and until
//! then a `/store/*` request with the right bearer is answered 503.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const ingest = @import("../ingest.zig");
const identity = @import("../identity.zig");
const plc_tail = @import("../plc_tail.zig");
const label_tail = @import("../label_tail.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const corpus_mod = @import("corpus.zig");
const enrich = @import("enrich.zig");
const control = @import("control.zig");
const Bridge = @import("../bridge/root.zig").Bridge;
const Owner = owner_mod.Owner;
const Actor = store_mod.Actor;
const Allocator = std.mem.Allocator;


/// A label, with the field names of `store/record.zig`.
pub const LabelBody = struct {
    source: []const u8,
    uri: []const u8,
    value: []const u8,
    created: []const u8,
    cid: []const u8,
    expires: []const u8,
    negated: bool,
};

/// An actor record, with the field names of `store/record.zig`.
pub const ActorBody = struct {
    did: []const u8,
    /// `live`, `deactivated` or `deleted`. A gone actor keeps its record
    /// until its tombstone is pruned.
    status: []const u8,
    hidden: bool,
    handle: []const u8,
    display_name: []const u8,
    avatar: []const u8,
    description: []const u8,
    banner: []const u8,
    pds: []const u8,
    created_at: []const u8,
    /// The appview's `associated` object as JSON text, "" when empty.
    associated: []const u8,
    followers_count: u64,
    follows_count: u64,
    posts_count: u64,
    quality_score: f32,
    labels: []const LabelBody,
    /// `none`, `show` or `hide`.
    override: []const u8,
    /// `none`, `bsky`, `blacksky` or `pds`.
    profile_source: []const u8,
    /// Unix seconds of the last content change.
    updated_at: u64,
    last_activity_at: u64,
    profile_checked_at: u64,
    identity_resolved_at: u64,
    deleted_at: u64,
    /// The actor has an entry in the identity queue.
    identity_pending: bool,
    refresh_due: u64,
};

pub const ConsumerBody = struct { name: []const u8, position: u64, seen_at: u64 };
pub const LevelBody = struct { tables: usize, bytes: u64 };

pub const HealthBody = struct {
    status: []const u8 = "ok",
    role: []const u8 = "store",
    rss_kb: u64,
    relay: struct {
        /// The relay seq stored with the data it covers.
        cursor: ?u64 = null,
        /// The seq of the last batch handed to the owner.
        handed_cursor: i64,
        events: u64,
        batches: u64,
        last_event_at: u64,
        source_event_at: i64,
        paused: bool,
    },
    cursors: struct { labeler: ?u64 = null, plc: ?[]const u8 = null, bridge: ?u64 = null },
    change_log: struct { head: u64, floor: u64, consumers: []const ConsumerBody },
    queues: struct {
        /// Changes waiting for the owner.
        writes: usize,
        identity_pending: u64,
        identity_due: u64,
        refresh_pending: u64,
        /// Refresh entries due now, and those a fetch holds a lease on.
        refresh_due: u64,
        refresh_leased: u64,
        tombstones: u64,
        /// A count above stopped at the cap, so it is a lower bound.
        capped: bool,
    },
    store: struct {
        /// When the numbers in `queues`, `store` and the stored cursors were read.
        taken_at: u64,
        tables: usize,
        table_bytes: u64,
        levels: []const LevelBody,
        memtable_bytes: usize,
        unflushed_segments: u64,
        resident_metadata_bytes: usize,
        disk_bytes: u64,
        disk_files: u64,
        flushes: u64,
        compactions: u64,
        last_compaction_at: u64,
        last_commit_at: u64,
        stalled: bool,
        stalls: u64,
        stall_ms: u64,
        longest_stall_ms: u64,
        unrelieved: u64,
        stall_turns: u64,
    },
    writes: struct {
        commits: u64,
        submissions: u64,
        changes_applied: u64,
        changes_rejected: u64,
        actors_changed: u64,
        reopens: u64,
        posts_blocked: u64,
        posts_blocked_ms: u64,
    },
    /// Since this process started. A statistics total is the last full
    /// count of live actors plus `live_delta` since that count.
    actors: struct { created: u64, gone: u64, reactivated: u64, pruned: u64, live_delta: i64 },
    housekeeping: struct { hidden_refreshed: u64, expiry_passes: u64, domain_sweeps: u64, changes_trimmed: u64 },
    first_check: FirstCheckBody,
    identity: struct { leased: u64, lease_failed: u64, verified: u64, unverified: u64, no_handle: u64, did_failed: u64, retried: u64, commit_failed: u64 },
    plc: struct { pages: u64, operations: u64, failed: u64 },
    labels: struct { frames: u64, adopted: u64, malformed: u64, ignored: u64, commits: u64, commit_failed: u64, cursor: i64, newest_cts: i64 },
    /// Absent when the index bridge is not configured.
    bridge: ?BridgeBody = null,
    /// Absent when enrichment is off.
    enrichment: ?EnrichmentBody = null,
    /// Absent when the corpus scan is off.
    corpus: ?CorpusBody = null,
    /// Absent when the control loop was not asked for.
    control: ?ControlBody = null,
};

/// The control loop (`control.zig`): the moderation policy and indexing
/// requests this slot pulls from the Worker.
pub const ControlBody = struct {
    /// `on`, or `misconfigured` when the loop was asked for and cannot run:
    /// `problem` says why, and the rest is absent.
    state: []const u8,
    problem: ?[]const u8 = null,
    /// Seconds since both endpoints last answered, or since the start
    /// before they first have. Alert when it passes a few poll intervals:
    /// until it falls, a policy change made at the Worker is not in force
    /// on this slot.
    age_seconds: ?u64 = null,
    poll_seconds: ?u64 = null,
    /// The control database the positions below were read from, and how
    /// often since the start an answer came from another one and they were
    /// dropped. A change means that database was recreated or restored.
    generation: ?u64 = null,
    generation_changes: ?u64 = null,
    policy: ?struct {
        /// The revision the store's policy equals; absent before the first.
        revision: ?u64 = null,
        checked_at: u64,
        applied_at: u64,
        snapshots_applied: u64,
        rules_changed: u64,
    } = null,
    index_requests: ?struct {
        sequence: u64,
        checked_at: u64,
        queued: u64,
        skipped: u64,
        /// Polls that left the requests at the Worker because
        /// `backlog_limit` identity entries were due. Rising with a high
        /// `queues.identity_due` means the resolvers are behind, and
        /// requests wait their turn behind the relay's signals.
        deferred: u64,
        deferred_at: u64,
        /// The bound: at most `per_poll` requests a poll, none while
        /// `backlog_limit` identity entries are due.
        per_poll: usize,
        backlog_limit: usize,
    } = null,
    polls: ?u64 = null,
    failures: ?struct {
        total: u64,
        no_response: u64,
        unauthorized: u64,
        malformed: u64,
        older_revision: u64,
        /// `none` when nothing has failed since the start.
        last: []const u8,
        last_status: u16,
        last_at: u64,
    } = null,
};

pub fn controlBody(loop: *control.Controller) ControlBody {
    const r = loop.report();
    return .{
        .state = "on",
        .age_seconds = r.age_seconds,
        .poll_seconds = r.poll_seconds,
        .generation = r.generation,
        .generation_changes = r.generation_changes,
        .policy = .{ .revision = r.policy_revision, .checked_at = r.policy_checked_at, .applied_at = r.policy_applied_at, .snapshots_applied = r.snapshots_applied, .rules_changed = r.rules_changed },
        .index_requests = .{
            .sequence = r.requests_sequence,
            .checked_at = r.requests_checked_at,
            .queued = r.requests_queued,
            .skipped = r.requests_skipped,
            .deferred = r.requests_deferred,
            .deferred_at = r.requests_deferred_at,
            .per_poll = r.requests_per_poll,
            .backlog_limit = r.request_backlog,
        },
        .polls = r.polls,
        .failures = .{
            .total = r.failures,
            .no_response = r.unreachable_worker,
            .unauthorized = r.unauthorized,
            .malformed = r.malformed,
            .older_revision = r.older_revision,
            .last = @tagName(r.last_failure),
            .last_status = r.last_failure_status,
            .last_at = r.last_failure_at,
        },
    };
}

/// The pass, made once in the life of a store, that queues a refresh for
/// every actor no profile source has answered for. It has run when
/// `finished_at` is set.
pub const FirstCheckBody = struct {
    /// False when this process does not run the pass.
    enabled: bool,
    /// Actors queued an hour at most, records one step examines, seconds
    /// between steps, and the count of due refresh entries at which a step
    /// queues nothing.
    per_hour: u64,
    scan_per_step: u64,
    step_seconds: u64,
    backlog_limit: u64,
    /// The check time this process looks for, and the one the stored
    /// progress was made for. They differ until the first step, which then
    /// starts the pass over.
    stamp: u64,
    stored_stamp: u64,
    started_at: u64,
    finished_at: u64,
    /// Actor records looked at so far, and the actors queued.
    examined: u64,
    queued: u64,
    position_did: []const u8,
};

/// One standing refresh walk. `period_seconds` is `enrich.walkPeriod` for
/// the pool the last corpus scan counted; null until a scan has run or when
/// the walk is off.
pub const WalkBody = struct {
    per_hour: u64,
    /// Actors this walk queued since the process started.
    queued: u64,
    passes: u64,
    pass_started_at: u64,
    last_pass_seconds: u64,
    /// Where the pass stands: the last DID examined, and for the quality
    /// walk its score. Empty and 0 at the top of a pass.
    position_did: []const u8,
    position_score: f32,
    pool: ?u64 = null,
    period_seconds: ?u64 = null,
};

/// The enrichment loop, from `enrich.Stats`, since this process started.
pub const EnrichmentBody = struct {
    requests_per_second: f64,
    jitter: f64,
    lease_seconds: u64,
    backoff_seconds: u64,
    backoff_max_seconds: u64,
    leased: u64,
    /// Entries settled with a profile from Bluesky, Blacksky or a PDS record.
    completed: u64,
    from_blacksky: u64,
    from_pds: u64,
    not_found: u64,
    changed: u64,
    /// Entries rescheduled after a request that taught nothing.
    retried: u64,
    requests: u64,
    failed: u64,
    rate_limited: u64,
    split: u64,
    commit_failed: u64,
    last_success_at: u64,
    last_request_at: u64,
    backoff_until: u64,
    consecutive_failures: u64,
    backed_up: bool,
    handles: HandleChecksBody,
    quality_walk: WalkBody,
    rest_walk: WalkBody,
};

/// Handles a profile source reported that differed from the stored one.
/// None is written; `disagreed` is the sum of the four outcomes.
pub const HandleChecksBody = struct {
    settle_seconds: u64,
    backlog_limit: u64,
    disagreed: u64,
    /// The actor was queued for identity resolution.
    queued: u64,
    /// The actor already had an identity queue entry.
    pending: u64,
    /// The actor's identity was resolved within `settle_seconds`.
    settled: u64,
    /// `backlog_limit` identity entries were due, so nothing was queued.
    deferred: u64,
};

/// The last corpus count. `total` is what the statistics page calls total:
/// live actors less the hidden ones.
pub const CorpusBody = struct {
    scanned_at: u64,
    duration_ms: u64,
    scans: u64,
    failed: u64,
    records: ?u64 = null,
    live: ?u64 = null,
    total: ?u64 = null,
    with_handles: ?u64 = null,
    with_avatars: ?u64 = null,
    hidden: ?u64 = null,
    ranked: ?u64 = null,
    rest: ?u64 = null,
};

fn walkBody(arena: Allocator, walk: owner_mod.Walk, per_hour: u64, queued: u64, pool: ?u64, period: ?u64) !WalkBody {
    return .{
        .per_hour = per_hour,
        .queued = queued,
        .passes = walk.passes,
        .pass_started_at = walk.started_at,
        .last_pass_seconds = walk.last_pass_seconds,
        .position_did = try arena.dupe(u8, walk.position()),
        .position_score = walk.score,
        .pool = pool,
        .period_seconds = period,
    };
}

pub fn enrichmentBody(arena: Allocator, enricher: *enrich.Enricher, health: owner_mod.Health, counts: ?corpus_mod.Counts) !EnrichmentBody {
    const s = &enricher.stats;
    const o = enricher.options;
    return .{
        .requests_per_second = o.requests_per_second,
        .jitter = o.jitter,
        .lease_seconds = o.lease_seconds,
        .backoff_seconds = o.backoff_seconds,
        .backoff_max_seconds = o.backoff_max_seconds,
        .leased = load(&s.leased),
        .completed = load(&s.enriched) + load(&s.from_blacksky) + load(&s.from_pds),
        .from_blacksky = load(&s.from_blacksky),
        .from_pds = load(&s.from_pds),
        .not_found = load(&s.not_found),
        .changed = load(&s.changed),
        .retried = load(&s.retried),
        .requests = load(&s.requests),
        .failed = load(&s.failed),
        .rate_limited = load(&s.rate_limited),
        .split = load(&s.split),
        .commit_failed = load(&s.commit_failed),
        .last_success_at = load(&s.last_success_at),
        .last_request_at = load(&s.last_request_at),
        .backoff_until = load(&s.backoff_until),
        .consecutive_failures = load(&s.consecutive_failures),
        .backed_up = load(&s.backed_up),
        .handles = .{
            .settle_seconds = o.handle_check.settle_seconds,
            .backlog_limit = o.handle_check.backlog_limit,
            .disagreed = load(&s.handle_queued) + load(&s.handle_pending) + load(&s.handle_settled) + load(&s.handle_deferred),
            .queued = load(&s.handle_queued),
            .pending = load(&s.handle_pending),
            .settled = load(&s.handle_settled),
            .deferred = load(&s.handle_deferred),
        },
        .quality_walk = try walkBody(arena, health.walks.get(.quality), o.walk_per_hour, load(&s.walk_queued), if (counts) |c| c.ranked else null, if (counts) |c| enrich.walkPeriod(c.ranked, c.ranked, o.walk_per_hour, enrich.quality_scan_factor) else null),
        .rest_walk = try walkBody(arena, health.walks.get(.rest), o.sweep_per_hour, load(&s.sweep_queued), if (counts) |c| c.rest else null, if (counts) |c| enrich.walkPeriod(c.rest, c.records, o.sweep_per_hour, enrich.rest_scan_factor) else null),
    };
}

pub fn corpusBody(report: corpus_mod.Report) CorpusBody {
    var body: CorpusBody = .{ .scanned_at = report.finished_at, .duration_ms = report.duration_ms, .scans = report.scans, .failed = report.failed };
    if (report.counts) |c| {
        body.records = c.records;
        body.live = c.live;
        body.total = c.total();
        body.with_handles = c.with_handles;
        body.with_avatars = c.with_avatars;
        body.hidden = c.hidden;
        body.ranked = c.ranked;
        body.rest = c.rest;
    }
    return body;
}

/// The index bridge, from `Bridge.health`. `lag` is how many change-log
/// positions the index is behind the store.
pub const BridgeBody = struct {
    position: ?u64 = null,
    index_position: u64,
    next_change: u64,
    lag: u64,
    last_push_at: u64,
    watermark: u64,
    pushes: u64,
    applied: u64,
    unchanged: u64,
    rejected: u64,
    conflicts: u64,
    errors: u64,
    recovered: u64,
    resyncs_started: u64,
    resync_round: u64,
    resync_phase: []const u8,
    resync_completed_at: ?u64 = null,
    last_error_at: u64,
    last_error: []const u8,
};

pub fn bridgeBody(arena: Allocator, bridge: *Bridge) !BridgeBody {
    const h = bridge.health();
    return .{
        .position = h.position,
        .index_position = h.index_position,
        .next_change = h.next_change,
        .lag = h.lag,
        .last_push_at = h.last_push_at,
        .watermark = h.watermark,
        .pushes = h.pushes,
        .applied = h.applied,
        .unchanged = h.unchanged,
        .rejected = h.rejected,
        .conflicts = h.conflicts,
        .errors = h.errors,
        .recovered = h.recovered,
        .resyncs_started = h.resyncs_started,
        .resync_round = h.resync_round,
        .resync_phase = @tagName(h.resync_phase),
        .resync_completed_at = h.resync_completed_at,
        .last_error_at = h.last_error_at,
        .last_error = try arena.dupe(u8, h.lastError()),
    };
}

pub const Context = struct {
    owner: *Owner,
    /// The handle `/store/*` reads through. Used by one thread at a time.
    /// Null until the role has opened it, which it does after the index
    /// bridge and the upstream consumers are running.
    reader: ?*store_mod.Reader,
    intake: *const intake_mod.Signals,
    labels_ignored: *const std.atomic.Value(u64),
    read_secret: ?[]const u8,
    bridge: ?*Bridge = null,
    enricher: ?*enrich.Enricher = null,
    corpus: ?*corpus_mod.Scanner = null,
    control: ?*control.Controller = null,
    /// Why the control loop is not running although it was asked for.
    control_problem: ?[]const u8 = null,
};

pub const Response = struct {
    status: std.http.Status = .ok,
    body: []const u8,
};

fn load(value: anytype) @FieldType(@TypeOf(value.*), "raw") {
    return value.load(.monotonic);
}

pub fn healthBody(ctx: *const Context, arena: Allocator) !HealthBody {
    const owner = ctx.owner;
    const c = &owner.counters;
    const health = owner.health();
    const stats = health.maintenance;
    var consumer_buf: [8]owner_mod.Consumer = undefined;
    const consumers = owner.consumerList(&consumer_buf);
    const consumer_bodies = try arena.alloc(ConsumerBody, consumers.len);
    for (consumer_bodies, consumers) |*body, one| body.* = .{ .name = one.name, .position = one.position, .seen_at = one.seen_at };
    var levels: []LevelBody = &.{};
    if (stats) |s| {
        levels = try arena.alloc(LevelBody, s.levels.len);
        for (levels, s.levels) |*body, level| body.* = .{ .tables = level.tables, .bytes = level.bytes };
    }
    const created = load(&c.actors_created);
    const gone = load(&c.actors_gone);
    const reactivated = load(&c.actors_reactivated);
    const corpus_report = if (ctx.corpus) |scanner| scanner.report() else null;
    return .{
        .rss_kb = ingest.getRssKB(),
        .relay = .{
            .cursor = health.cursors.relay,
            .handed_cursor = load(&ctx.intake.handed_cursor),
            .events = load(&ctx.intake.events),
            .batches = load(&ctx.intake.batches),
            .last_event_at = load(&ctx.intake.last_event_at),
            .source_event_at = load(&ctx.intake.source_event_at),
            .paused = load(&ctx.intake.paused),
        },
        .cursors = .{ .labeler = health.cursors.labeler, .plc = if (health.plc()) |text| try arena.dupe(u8, text) else null, .bridge = health.cursors.bridge },
        .change_log = .{ .head = health.cursors.next_change, .floor = health.cursors.change_floor, .consumers = consumer_bodies },
        .queues = .{
            .writes = load(&c.queued_changes),
            .identity_pending = health.identity_pending,
            .identity_due = health.identity_due,
            .refresh_pending = health.refresh_pending,
            .refresh_due = health.refresh_due,
            .refresh_leased = health.refresh_pending -| health.refresh_due,
            .tombstones = health.tombstones,
            .capped = health.counts_capped,
        },
        .store = .{
            .taken_at = health.taken_at,
            .tables = if (stats) |s| s.tables else 0,
            .table_bytes = if (stats) |s| s.table_bytes else 0,
            .levels = levels,
            .memtable_bytes = if (stats) |s| s.memtable_bytes else 0,
            .unflushed_segments = if (stats) |s| s.unflushed_segments else 0,
            .resident_metadata_bytes = if (stats) |s| s.resident_metadata_bytes else 0,
            .disk_bytes = health.disk.bytes,
            .disk_files = health.disk.files,
            .flushes = if (stats) |s| s.flushes.count else 0,
            .compactions = if (stats) |s| s.compactions else 0,
            .last_compaction_at = if (stats) |s| s.last_compaction_at else 0,
            .last_commit_at = load(&c.last_commit_at),
            .stalled = load(&c.stalled),
            .stalls = if (stats) |s| s.stalls.count else 0,
            .stall_ms = if (stats) |s| s.stalls.total_ns / std.time.ns_per_ms else 0,
            .longest_stall_ms = if (stats) |s| s.stalls.longest_ns / std.time.ns_per_ms else 0,
            .unrelieved = if (stats) |s| s.unrelieved else 0,
            .stall_turns = load(&c.stall_turns),
        },
        .writes = .{
            .commits = load(&c.commits),
            .submissions = load(&c.submissions),
            .changes_applied = load(&c.changes_applied),
            .changes_rejected = load(&c.changes_rejected),
            .actors_changed = load(&c.actors_changed),
            .reopens = load(&c.reopens),
            .posts_blocked = load(&c.posts_blocked),
            .posts_blocked_ms = load(&c.posts_blocked_ms),
        },
        .actors = .{
            .created = created,
            .gone = gone,
            .reactivated = reactivated,
            .pruned = load(&c.tombstones_pruned),
            .live_delta = @as(i64, @intCast(created + reactivated)) - @as(i64, @intCast(gone)),
        },
        .housekeeping = .{
            .hidden_refreshed = load(&c.hidden_refreshed),
            .expiry_passes = load(&c.expiry_passes),
            .domain_sweeps = load(&c.domain_sweeps),
            .changes_trimmed = load(&c.changes_trimmed),
        },
        .first_check = .{
            .enabled = owner.options.first_check_per_hour > 0,
            .per_hour = owner.options.first_check_per_hour,
            .scan_per_step = owner.options.first_check_scan,
            .step_seconds = owner.options.first_check_seconds,
            .backlog_limit = owner.options.first_check_backlog,
            .stamp = owner.options.first_check_stamp,
            .stored_stamp = health.first_check.stamp,
            .started_at = health.first_check.started_at,
            .finished_at = health.first_check.finished_at,
            .examined = health.first_check.examined,
            .queued = health.first_check.queued,
            .position_did = try arena.dupe(u8, health.first_check.position()),
        },
        .identity = .{
            .leased = load(&identity.stats.leased),
            .lease_failed = load(&identity.stats.lease_failed),
            .verified = load(&identity.stats.verified),
            .unverified = load(&identity.stats.unverified),
            .no_handle = load(&identity.stats.no_handle),
            .did_failed = load(&identity.stats.did_failed),
            .retried = load(&identity.stats.retried),
            .commit_failed = load(&identity.stats.post_failed),
        },
        .plc = .{ .pages = load(&plc_tail.stats.pages), .operations = load(&plc_tail.stats.operations), .failed = load(&plc_tail.stats.failed) },
        .labels = .{
            .frames = load(&label_tail.stats.frames),
            .adopted = load(&label_tail.stats.adopted),
            .malformed = load(&label_tail.stats.malformed),
            .ignored = load(ctx.labels_ignored),
            .commits = load(&label_tail.stats.posts),
            .commit_failed = load(&label_tail.stats.post_failed),
            .cursor = load(&label_tail.stats.committed),
            .newest_cts = load(&label_tail.stats.newest_cts),
        },
        .bridge = if (ctx.bridge) |bridge| try bridgeBody(arena, bridge) else null,
        .enrichment = if (ctx.enricher) |enricher| try enrichmentBody(arena, enricher, health, if (corpus_report) |report| report.counts else null) else null,
        .corpus = if (corpus_report) |report| corpusBody(report) else null,
        .control = if (ctx.control) |loop| controlBody(loop) else if (ctx.control_problem) |problem| .{ .state = "misconfigured", .problem = problem } else null,
    };
}

pub fn actorBody(arena: Allocator, did: []const u8, actor: Actor) !ActorBody {
    const labels = try arena.alloc(LabelBody, actor.labels.len);
    for (labels, actor.labels) |*body, label| body.* = .{
        .source = label.source,
        .uri = label.uri,
        .value = label.value,
        .created = label.created,
        .cid = label.cid,
        .expires = label.expires,
        .negated = label.negated,
    };
    return .{
        .did = did,
        .status = @tagName(actor.status),
        .hidden = actor.hidden,
        .handle = actor.handle,
        .display_name = actor.display_name,
        .avatar = actor.avatar,
        .description = actor.description,
        .banner = actor.banner,
        .pds = actor.pds,
        .created_at = actor.created_at,
        .associated = actor.associated,
        .followers_count = actor.followers_count,
        .follows_count = actor.follows_count,
        .posts_count = actor.posts_count,
        .quality_score = actor.quality_score,
        .labels = labels,
        .override = @tagName(actor.override),
        .profile_source = @tagName(actor.profile_source),
        .updated_at = actor.updated_at,
        .last_activity_at = actor.last_activity_at,
        .profile_checked_at = actor.profile_checked_at,
        .identity_resolved_at = actor.identity_resolved_at,
        .deleted_at = actor.deleted_at,
        .identity_pending = actor.identity_due != 0,
        .refresh_due = actor.refresh_due,
    };
}

fn json(arena: Allocator, status: std.http.Status, value: anytype) !Response {
    var out: std.Io.Writer.Allocating = .init(arena);
    var writer: std.json.Stringify = .{ .writer = &out.writer, .options = .{ .emit_null_optional_fields = false } };
    try writer.write(value);
    return .{ .status = status, .body = out.written() };
}

fn failure(arena: Allocator, status: std.http.Status, message: []const u8) !Response {
    return json(arena, status, .{ .@"error" = message });
}

/// Values of `name` in a query string, percent-decoded, in order.
pub fn queryValues(arena: Allocator, query: []const u8, name: []const u8) ![]const []const u8 {
    var out: std.ArrayList([]const u8) = .empty;
    var pairs = std.mem.splitScalar(u8, query, '&');
    while (pairs.next()) |pair| {
        const eq = std.mem.indexOfScalar(u8, pair, '=') orelse continue;
        if (!std.mem.eql(u8, pair[0..eq], name)) continue;
        const raw = try arena.dupe(u8, pair[eq + 1 ..]);
        for (raw) |*c| if (c.* == '+') {
            c.* = ' ';
        };
        try out.append(arena, std.Uri.percentDecodeInPlace(raw));
    }
    return out.items;
}

fn single(arena: Allocator, query: []const u8, name: []const u8) !?[]const u8 {
    const values = try queryValues(arena, query, name);
    return if (values.len == 1 and values[0].len > 0) values[0] else null;
}

pub fn authorized(secret: []const u8, header: ?[]const u8) bool {
    const given = header orelse return false;
    const prefix = "Bearer ";
    if (!std.mem.startsWith(u8, given, prefix)) return false;
    const token = given[prefix.len..];
    if (token.len != secret.len) return false;
    var diff: u8 = 0;
    for (token, secret) |a, b| diff |= a ^ b;
    return diff == 0;
}

/// A point read through the reader. The owner can collect a table the
/// reader's snapshot still names; a refresh finds the table that replaced it.
fn readActor(reader: *store_mod.Reader, arena: Allocator, did: []const u8) !?Actor {
    var attempts: usize = 0;
    while (true) : (attempts += 1) {
        return reader.actor(arena, did) catch |err| {
            if (err != error.Missing or attempts == 8) return err;
            try reader.refresh();
            continue;
        };
    }
}

/// Answer one request. `target` is the request target with its query.
pub fn respond(ctx: *const Context, arena: Allocator, method: std.http.Method, target: []const u8, authorization: ?[]const u8) !Response {
    const split = std.mem.indexOfScalar(u8, target, '?');
    const path = if (split) |at| target[0..at] else target;
    const query = if (split) |at| target[at + 1 ..] else "";

    if (std.mem.eql(u8, path, "/health")) return json(arena, .ok, try healthBody(ctx, arena));
    if (!std.mem.startsWith(u8, path, "/store/")) return failure(arena, .not_found, "not found");
    const secret = ctx.read_secret orelse return failure(arena, .not_found, "not found");
    if (!authorized(secret, authorization)) return failure(arena, .unauthorized, "unauthorized");

    const reader = ctx.reader orelse return failure(arena, .service_unavailable, "the store reader is not open yet");
    try reader.refresh();
    if (method != .GET) return failure(arena, .method_not_allowed, "method not allowed");

    if (std.mem.eql(u8, path, "/store/actor")) {
        const did = try single(arena, query, "did") orelse return failure(arena, .bad_request, "did is required");
        store_mod.keys.validDid(did) catch return failure(arena, .bad_request, "invalid did");
        const actor = try readActor(reader, arena, did) orelse return failure(arena, .not_found, "not found");
        return json(arena, .ok, .{ .actor = try actorBody(arena, did, actor) });
    }
    if (std.mem.eql(u8, path, "/store/handle")) {
        const handle = try single(arena, query, "handle") orelse return failure(arena, .bad_request, "handle is required");
        if (handle.len > store_mod.keys.max_handle) return failure(arena, .bad_request, "handle is too long");
        const dids = try reader.byHandle(arena, handle);
        var bodies: std.ArrayList(ActorBody) = .empty;
        for (dids) |did| {
            if (try readActor(reader, arena, did)) |actor| try bodies.append(arena, try actorBody(arena, did, actor));
        }
        return json(arena, .ok, .{ .actors = bodies.items });
    }
    return failure(arena, .not_found, "not found");
}
