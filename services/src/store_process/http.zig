//! The store role's HTTP surface: `/health`, and authenticated reads under
//! `/store/`. Every body is one of the types below, written as JSON.
//!
//! | request                                              | answer                                  |
//! |------------------------------------------------------|-----------------------------------------|
//! | `GET /health`                                        | `HealthBody`, no auth                   |
//! | `GET /store/actor?did=`                              | `{"actor": ActorBody}` or 404           |
//! | `GET /store/handle?handle=`                          | `{"actors": [ActorBody]}`               |
//! | `POST /store/actors/batch` `{"dids": [at most 500]}` | `ActorsBody`, null where none, in request order |
//! | `POST /store/actors/page` `{"after_did", "limit"}`   | `ActorsBody`: the records after that DID in ascending byte order, any status, at most `limit` (1 to 500) |
//! | `GET /store/changes?from=&limit=&consumer=`          | `ChangesBody`                           |
//!
//! `/store/*` needs `Authorization: Bearer $STORE_READ_SECRET` and answers
//! 404 when no secret is configured. The two POSTs are the contract of
//! `scripts/shadow_compare/store.py`. `from` is the first change sequence
//! wanted; the body's `next` is the `from` of the following request. Naming
//! a `consumer` records `from` as that consumer's position, which holds the
//! change log's trimming back to it.
//!
//! Every `/store/*` read goes through a read-only handle on the store,
//! refreshed per request. It takes no lock the owner takes, so a walk of
//! every actor cannot hold a commit back; it sees whole commits, as of the
//! refresh. The handle keeps table metadata of its own.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const ingest = @import("../ingest.zig");
const identity = @import("../identity.zig");
const plc_tail = @import("../plc_tail.zig");
const label_tail = @import("../label_tail.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const Owner = owner_mod.Owner;
const Actor = store_mod.Actor;
const Allocator = std.mem.Allocator;

pub const max_batch = 500;
pub const max_page = 500;
pub const max_changes = 1000;
pub const max_body_bytes = 256 * 1024;

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

pub const ActorsBody = struct {
    actors: []const ?ActorBody,
    /// The sequence the next change-log entry will take.
    next_change: u64,
};

pub const ChangeBody = struct { seq: u64, did: []const u8 };

pub const ChangesBody = struct {
    /// Changed DIDs at or after `from`, in commit order.
    entries: []const ChangeBody = &.{},
    /// The `from` of the next request.
    next: u64,
    /// The sequence the next change will take.
    head: u64,
    /// Set when `from` was below the log's floor: entries are gone, and the
    /// consumer must rebuild from the actors and continue from here.
    trimmed: ?u64 = null,
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
    identity: struct { leased: u64, lease_failed: u64, verified: u64, unverified: u64, no_handle: u64, did_failed: u64, retried: u64, commit_failed: u64 },
    plc: struct { pages: u64, operations: u64, failed: u64 },
    labels: struct { frames: u64, adopted: u64, malformed: u64, ignored: u64, commits: u64, commit_failed: u64, cursor: i64, newest_cts: i64 },
};

pub const Context = struct {
    owner: *Owner,
    /// The handle `/store/*` reads through. Used by one thread at a time.
    reader: *store_mod.Reader,
    intake: *const intake_mod.Signals,
    labels_ignored: *const std.atomic.Value(u64),
    read_secret: ?[]const u8,
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

/// A page of the walk over every actor. A record pruned between the walk
/// and its read is passed over, and the page is filled from further on: a
/// short page must mean the walk is over.
fn page(reader: *store_mod.Reader, arena: Allocator, after_did: []const u8, limit: usize) ![]const ?ActorBody {
    var bodies: std.ArrayList(?ActorBody) = .empty;
    var after = after_did;
    while (bodies.items.len < limit) {
        const want = limit - bodies.items.len;
        const dids = try reader.walk(arena, after, want);
        for (dids) |did| {
            if (try readActor(reader, arena, did)) |actor| try bodies.append(arena, try actorBody(arena, did, actor));
        }
        if (dids.len < want) break;
        after = dids[dids.len - 1];
    }
    return bodies.items;
}

/// Answer one request. `target` is the request target with its query, and
/// `body` what a POST carried.
pub fn respond(ctx: *const Context, arena: Allocator, method: std.http.Method, target: []const u8, authorization: ?[]const u8, body: []const u8) !Response {
    const split = std.mem.indexOfScalar(u8, target, '?');
    const path = if (split) |at| target[0..at] else target;
    const query = if (split) |at| target[at + 1 ..] else "";

    if (std.mem.eql(u8, path, "/health")) return json(arena, .ok, try healthBody(ctx, arena));
    if (!std.mem.startsWith(u8, path, "/store/")) return failure(arena, .not_found, "not found");
    const secret = ctx.read_secret orelse return failure(arena, .not_found, "not found");
    if (!authorized(secret, authorization)) return failure(arena, .unauthorized, "unauthorized");

    const reader = ctx.reader;
    try reader.refresh();
    const posted = std.mem.eql(u8, path, "/store/actors/batch") or std.mem.eql(u8, path, "/store/actors/page");
    if (method != (if (posted) std.http.Method.POST else std.http.Method.GET)) return failure(arena, .method_not_allowed, "method not allowed");

    if (std.mem.eql(u8, path, "/store/actors/batch")) {
        const request = std.json.parseFromSliceLeaky(struct { dids: []const []const u8 }, arena, body, .{ .ignore_unknown_fields = true }) catch return failure(arena, .bad_request, "body must be {\"dids\": [...]}");
        if (request.dids.len > max_batch) return failure(arena, .bad_request, "at most 500 dids");
        const bodies = try arena.alloc(?ActorBody, request.dids.len);
        for (bodies, request.dids) |*slot, did| {
            store_mod.keys.validDid(did) catch return failure(arena, .bad_request, "invalid did");
            slot.* = if (try readActor(reader, arena, did)) |actor| try actorBody(arena, did, actor) else null;
        }
        return json(arena, .ok, ActorsBody{ .actors = bodies, .next_change = (try reader.cursors(arena)).next_change });
    }
    if (std.mem.eql(u8, path, "/store/actors/page")) {
        const request = std.json.parseFromSliceLeaky(struct { after_did: []const u8 = "", limit: usize }, arena, body, .{ .ignore_unknown_fields = true }) catch return failure(arena, .bad_request, "body must be {\"after_did\": \"...\", \"limit\": n}");
        if (request.limit == 0 or request.limit > max_page) return failure(arena, .bad_request, "limit must be between 1 and 500");
        if (request.after_did.len > 0) store_mod.keys.validDid(request.after_did) catch return failure(arena, .bad_request, "invalid after_did");
        return json(arena, .ok, ActorsBody{ .actors = try page(reader, arena, request.after_did, request.limit), .next_change = (try reader.cursors(arena)).next_change });
    }
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
    if (std.mem.eql(u8, path, "/store/changes")) {
        const from = std.fmt.parseInt(u64, try single(arena, query, "from") orelse "0", 10) catch return failure(arena, .bad_request, "from must be a sequence");
        const limit = std.fmt.parseInt(usize, try single(arena, query, "limit") orelse "1000", 10) catch return failure(arena, .bad_request, "limit must be a number");
        if (limit == 0 or limit > max_changes) return failure(arena, .bad_request, "limit must be between 1 and 1000");
        const head = (try reader.cursors(arena)).next_change;
        const changes: ChangesBody = switch (try reader.changes(arena, from, limit)) {
            .trimmed => |floor| .{ .next = floor, .head = head, .trimmed = floor },
            .entries => |entries| blk: {
                const bodies = try arena.alloc(ChangeBody, entries.len);
                for (bodies, entries) |*slot, entry| slot.* = .{ .seq = entry.sequence, .did = entry.did };
                break :blk .{ .entries = bodies, .next = if (entries.len > 0) entries[entries.len - 1].sequence + 1 else @max(from, 1), .head = head };
            },
        };
        if (try single(arena, query, "consumer")) |name| ctx.owner.noteConsumer(name, from);
        return json(arena, .ok, changes);
    }
    return failure(arena, .not_found, "not found");
}
