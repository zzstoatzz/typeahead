//! `MODE=store-bootstrap`: load a new store from a bundle's actors.
//!
//! Input is one JSON object a line, as `experiments/zkv-scale/dump-actors`
//! prints them from a bundle's index, and optionally a policy file:
//!
//!     {"overrides": [{"did": "...", "action": "show" | "hide", "reason": "..."}],
//!      "blocked_domains": [{"domain": "...", "reason": "..."}]}
//!
//! Configuration, all from the environment:
//!
//! | variable                   | required | meaning                                              |
//! |----------------------------|----------|------------------------------------------------------|
//! | `STORE_DIR`                | yes      | the store to load                                    |
//! | `BOOTSTRAP_ACTORS`         | yes      | path of the actor lines                              |
//! | `BOOTSTRAP_RELAY_CURSOR`   | yes      | relay seq provably older than the bundle             |
//! | `BOOTSTRAP_POLICY`         | no       | path of the policy file                              |
//! | `BOOTSTRAP_LABELER_CURSOR` | no       | label stream seq to resume from                      |
//! | `BOOTSTRAP_PLC_CURSOR`     | no       | PLC export timestamp to resume from                  |
//!
//! Policy is applied first, so every actor's `hidden` flag is computed
//! under it. Actors are committed in batches in bulk-load mode: after each
//! commit the store works level 0 down before the next, which is the
//! backpressure. The last batch's commit also carries the cursors and sets
//! the bridge position to the end of the change log, so the store role
//! (which refuses to follow the relay without a relay cursor) cannot start
//! on a half-loaded store, and the bridge pushes only what changes later.
//!
//! A load that is interrupted is run again from the start; loading an actor
//! twice leaves the same record, and no cursor exists until the last batch.
//!
//! What a bundle does not carry is not loaded: description and banner, the
//! last activity time and the last content change (both become the load
//! time), and the identity resolution time.
//!
//! Nor does it carry when a profile was last fetched. An actor whose line
//! holds something a fetch writes (a score, a count, a creation time) is
//! stamped as checked at the load, and the refresh walks reach it in their
//! own time. A line without any of that is an actor no source answered for
//! before the bundle was built: its check time stays 0, and the store role's
//! first-check pass (`Store.firstCheck`) queues it ahead of the rest walk.
//! The load itself queues nothing.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const ingest = @import("../ingest.zig");
const Io = std.Io;
const Store = store_mod.Store;
const Change = store_mod.Change;

const log = std.log.scoped(.store_bootstrap);

pub const batch_actors = 400;

pub const Cursors = struct {
    relay: u64,
    labeler: ?u64 = null,
    plc: ?[]const u8 = null,
};

pub const Summary = struct {
    lines: u64 = 0,
    loaded: u64 = 0,
    /// Lines marked deleted in the bundle, or without a DID.
    skipped: u64 = 0,
    /// Lines that were not an actor object, or that the key layout refused.
    rejected: u64 = 0,
    changed: u64 = 0,
    /// Loaded actors whose line held nothing a profile fetch writes.
    unchecked: u64 = 0,
    overrides: u64 = 0,
    blocked_domains: u64 = 0,
    commits: u64 = 0,
    bridge_position: u64 = 0,
};

const WireLabel = struct {
    src: []const u8 = "",
    uri: []const u8 = "",
    val: []const u8 = "",
    cts: []const u8 = "",
    cid: ?[]const u8 = null,
    exp: ?[]const u8 = null,
    neg: ?bool = null,
};

const Line = struct {
    did: []const u8 = "",
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar_url: []const u8 = "",
    labels: []const WireLabel = &.{},
    associated: std.json.Value = .null,
    created_at: []const u8 = "",
    pds: []const u8 = "",
    quality_score: f32 = 0,
    deleted: bool = false,
    followers_count: u64 = 0,
    follows_count: u64 = 0,
    posts_count: u64 = 0,
};

const PolicyFile = struct {
    overrides: []const struct { did: []const u8, action: enum { show, hide }, reason: []const u8 = "" } = &.{},
    blocked_domains: []const struct { domain: []const u8, reason: []const u8 = "" } = &.{},
};

/// The two changes that load one bundle line, or null for a line to skip.
fn changesOf(arena: std.mem.Allocator, text: []const u8) !?[2]Change {
    // `text` is the reader's buffer, which the next line overwrites: every
    // string must be copied to outlive it until the batch commits.
    const line = try std.json.parseFromSliceLeaky(Line, arena, text, .{ .ignore_unknown_fields = true, .allocate = .alloc_always });
    if (line.deleted or line.did.len == 0) return null;
    const labels = try arena.alloc(store_mod.record.Label, line.labels.len);
    for (labels, line.labels) |*slot, label| slot.* = .{
        .source = label.src,
        .uri = label.uri,
        .value = label.val,
        .created = label.cts,
        .cid = label.cid orelse "",
        .expires = label.exp orelse "",
        .negated = label.neg orelse false,
    };
    const score: f32 = if (std.math.isFinite(line.quality_score) and line.quality_score > 0) line.quality_score else 0;
    const fetched = score > 0 or line.followers_count > 0 or line.follows_count > 0 or line.posts_count > 0 or line.created_at.len > 0;
    const associated: []const u8 = switch (line.associated) {
        .object => |object| if (object.count() == 0) "" else try std.json.Stringify.valueAlloc(arena, line.associated, .{}),
        else => "",
    };
    return .{
        .{ .profile = .{ .did = line.did, .handle = line.handle, .display_name = line.display_name, .avatar = line.avatar_url, .pds = line.pds } },
        .{ .enrichment = .{
            .did = line.did,
            .source = .bsky,
            .created_at = line.created_at,
            .associated = associated,
            .followers_count = line.followers_count,
            .follows_count = line.follows_count,
            .posts_count = line.posts_count,
            .quality_score = score,
            .labels = labels,
            .checked = fetched,
        } },
    };
}

pub fn applyPolicy(store: *Store, arena: std.mem.Allocator, now: u64, text: []const u8, summary: *Summary) !void {
    const policy = try std.json.parseFromSliceLeaky(PolicyFile, arena, text, .{ .ignore_unknown_fields = true });
    const batch = try store.begin(now);
    errdefer batch.abort();
    for (policy.blocked_domains) |entry| {
        _ = try batch.apply(.{ .domain_blocked = .{ .domain = entry.domain, .reason = entry.reason } });
        summary.blocked_domains += 1;
    }
    for (policy.overrides) |entry| {
        _ = try batch.apply(.{ .override_set = .{ .did = entry.did, .action = if (entry.action == .show) .show else .hide, .reason = entry.reason } });
        summary.overrides += 1;
    }
    try batch.commit();
}

const Loader = struct {
    store: *Store,
    now: u64,
    summary: *Summary,
    arena: std.heap.ArenaAllocator,
    pending: std.ArrayList(Change) = .empty,
    held: usize = 0,
    held_unchecked: usize = 0,

    fn add(self: *Loader, text: []const u8) !void {
        self.summary.lines += 1;
        const pair = changesOf(self.arena.allocator(), text) catch |err| switch (err) {
            error.OutOfMemory => return err,
            else => {
                self.summary.rejected += 1;
                return;
            },
        } orelse {
            self.summary.skipped += 1;
            return;
        };
        try self.pending.appendSlice(self.arena.allocator(), &pair);
        self.held += 1;
        self.held_unchecked += @intFromBool(!pair[1].enrichment.checked);
    }

    /// Commit what is held. With `final`, the commit also carries the
    /// cursors and the bridge position after it.
    fn commit(self: *Loader, final: ?Cursors) !void {
        const store = self.store;
        const batch = try store.begin(self.now);
        errdefer batch.abort();
        var distinct: std.StringHashMapUnmanaged(void) = .empty;
        const a = self.arena.allocator();
        for (self.pending.items) |one| {
            const outcome = batch.apply(one) catch |err| switch (err) {
                error.InvalidChange => {
                    self.summary.rejected += 1;
                    continue;
                },
                else => return err,
            };
            if (outcome.changed == 0) continue;
            const did = switch (one) {
                .profile => |c| c.did,
                .enrichment => |c| c.did,
                else => unreachable,
            };
            try distinct.put(a, did, {});
        }
        if (final) |cursors| {
            self.summary.bridge_position = store.next_change + distinct.count();
            try batch.setCursor(.{ .relay = cursors.relay });
            if (cursors.labeler) |seq| try batch.setCursor(.{ .labeler = seq });
            if (cursors.plc) |text| try batch.setCursor(.{ .plc = text });
            try batch.setCursor(.{ .bridge = self.summary.bridge_position });
        }
        try batch.commit();
        self.summary.loaded += self.held;
        self.summary.unchecked += self.held_unchecked;
        self.summary.changed += distinct.count();
        self.summary.commits += 1;
        self.held = 0;
        self.held_unchecked = 0;
        self.pending = .empty;
        _ = self.arena.reset(.retain_capacity);
        _ = try store.maintainStep();
    }
};

pub const Progress = struct {
    context: ?*anyopaque = null,
    report: *const fn (context: ?*anyopaque, summary: Summary, stats: store_mod.maintain.Stats) void,
    every_commits: u64 = 250,
};

/// Load every line of `actors` into `store` and finish with `cursors`.
pub fn load(gpa: std.mem.Allocator, store: *Store, now: u64, actors: *Io.Reader, cursors: Cursors, progress: ?Progress, summary: *Summary) !void {
    var loader: Loader = .{ .store = store, .now = now, .summary = summary, .arena = .init(gpa) };
    defer loader.arena.deinit();
    while (try actors.takeDelimiter('\n')) |text| {
        if (text.len == 0) continue;
        try loader.add(text);
        if (loader.held < batch_actors) continue;
        try loader.commit(null);
        if (progress) |p| if (summary.commits % p.every_commits == 0) p.report(p.context, summary.*, store.stats());
    }
    try loader.commit(cursors);
    if (store.next_change != summary.bridge_position) return error.BridgePositionMismatch;
    try store.maintainer.flush();
    if (progress) |p| p.report(p.context, summary.*, store.stats());
}

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return std.mem.span(p);
    return null;
}

const Clock = struct {
    io: Io,
    started: i96,

    fn report(context: ?*anyopaque, summary: Summary, stats: store_mod.maintain.Stats) void {
        const self: *Clock = @ptrCast(@alignCast(context.?));
        const elapsed_ms: u64 = @intCast(@divFloor(Io.Timestamp.now(self.io, .awake).nanoseconds - self.started, std.time.ns_per_ms));
        log.info("loaded {d} actors ({d} changed, {d} skipped, {d} rejected) in {d} s, {d} actors/s; {d} stalls for {d} s, longest {d} ms; {d} tables, {d} MiB, level 0 holds {d}", .{
            summary.loaded,     summary.changed,                           summary.skipped,
            summary.rejected,   elapsed_ms / 1000,                         summary.loaded * 1000 / @max(elapsed_ms, 1),
            stats.stalls.count, stats.stalls.total_ns / std.time.ns_per_s, stats.stalls.longest_ns / std.time.ns_per_ms,
            stats.tables,       stats.table_bytes >> 20,                   stats.levels[0].tables,
        });
    }
};

/// The subcommand: configuration from the environment, progress on the log.
pub fn run(gpa: std.mem.Allocator, io: Io) !void {
    const store_dir = cGetenv("STORE_DIR") orelse return error.StoreDirRequired;
    const actors_path = cGetenv("BOOTSTRAP_ACTORS") orelse return error.BootstrapActorsRequired;
    const relay = ingest.plausibleRelayCursor(std.fmt.parseInt(i64, cGetenv("BOOTSTRAP_RELAY_CURSOR") orelse return error.BootstrapRelayCursorRequired, 10) catch return error.InvalidBootstrapCursor) orelse return error.InvalidBootstrapCursor;
    var cursors: Cursors = .{ .relay = @intCast(relay) };
    if (cGetenv("BOOTSTRAP_LABELER_CURSOR")) |text| cursors.labeler = std.fmt.parseInt(u64, text, 10) catch return error.InvalidBootstrapCursor;
    if (cGetenv("BOOTSTRAP_PLC_CURSOR")) |text| {
        if (store_mod.time.parseMs(text) == null) return error.InvalidBootstrapCursor;
        cursors.plc = text;
    }

    const cwd = Io.Dir.cwd();
    const store = try Store.open(gpa, io, cwd, store_dir, .{ .maintenance = .bulk_load });
    defer store.close();
    const now = ingest.timestamp();
    var summary: Summary = .{};
    if (cGetenv("BOOTSTRAP_POLICY")) |path| {
        var arena: std.heap.ArenaAllocator = .init(gpa);
        defer arena.deinit();
        const text = try cwd.readFileAlloc(io, path, arena.allocator(), .limited(16 << 20));
        try applyPolicy(store, arena.allocator(), @intCast(now), text, &summary);
        log.info("policy: {d} blocked domains, {d} overrides", .{ summary.blocked_domains, summary.overrides });
    }

    const file = try cwd.openFile(io, actors_path, .{});
    defer file.close(io);
    const buffer = try gpa.alloc(u8, 8 << 20);
    defer gpa.free(buffer);
    var reader = file.reader(io, buffer);
    var clock: Clock = .{ .io = io, .started = Io.Timestamp.now(io, .awake).nanoseconds };
    log.info("loading {s} into {s}", .{ actors_path, store_dir });
    try load(gpa, store, @intCast(now), &reader.interface, cursors, .{ .context = &clock, .report = Clock.report }, &summary);
    log.info("bootstrap complete: {d} actors in {d} commits, {d} of them never fetched and left for the first-check pass; relay cursor {d}, bridge position {d}", .{ summary.loaded, summary.commits, summary.unchecked, cursors.relay, summary.bridge_position });
}
