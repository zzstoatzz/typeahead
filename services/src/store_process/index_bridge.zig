//! The index bridge's view of the store (`bridge/source.zig`), and the
//! bridge's place in the store role.
//!
//! The bridge runs on its own thread: a step blocks on loopback HTTP for as
//! long as the index takes to commit, so it must not be the thread that
//! owns the writer. It reads through a read-only handle of its own,
//! refreshed at the start of `cursors` and `changes`, so its reads never
//! wait on a commit and never go backward within a step. `setPosition` is
//! handed to the owner, which folds it into its next commit; the bridge
//! repeats it after a restart if it did not land. It never waits for room
//! in the owner's queue: the index's watermark moves only when the bridge
//! pushes, so a bridge held up behind a full queue would let the index go
//! stale for as long as the owner was stalled or catching up. A position
//! the queue had no room for is kept and offered again at the next step,
//! and the trim sees it at once either way. `policy` is always a
//! snapshot of the store's overrides and blocked domains: an index built
//! from a bundle has policy enabled and stops serving without a fresh one.

const std = @import("std");
const bridge = @import("../bridge/root.zig");
const store_mod = @import("../store/root.zig");
const owner_mod = @import("owner.zig");
const corpus_mod = @import("corpus.zig");
const Allocator = std.mem.Allocator;
const Owner = owner_mod.Owner;
const source = bridge.source;

pub const consumer_name = "bridge";

const log = std.log.scoped(.index_bridge);

/// Hands the corpus scanner's last count to the index, which serves it on
/// its public listener beside its own counts: the store's port has no
/// public service, and the count is what the Worker's statistics page shows.
/// The index keeps it in memory only, so it is sent every
/// `interval_seconds` whether or not it changed, and an index restarted
/// since the last scan has it back within one interval.
pub const CorpusReporter = struct {
    gpa: Allocator,
    io: std.Io,
    scanner: *corpus_mod.Scanner,
    client: bridge.wire.Client,
    interval_seconds: u64 = 60,
    sent: std.atomic.Value(u64) = .init(0),
    failed: std.atomic.Value(u64) = .init(0),

    /// Whether there was a count to send.
    pub fn report(self: *CorpusReporter) !bool {
        const latest = self.scanner.report();
        const counts = latest.counts orelse return false;
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        try self.client.corpus(arena.allocator(), .{
            .live = counts.live,
            .total = counts.total(),
            .with_handles = counts.with_handles,
            .with_avatars = counts.with_avatars,
            .hidden = counts.hidden,
            .scanned_at = latest.finished_at,
        });
        _ = self.sent.fetchAdd(1, .monotonic);
        return true;
    }

    pub fn run(self: *CorpusReporter, stop: *const std.atomic.Value(bool)) void {
        var failing = false;
        while (!stop.load(.acquire)) {
            if (self.report()) |_| {
                if (failing) log.info("the index took the corpus count again", .{});
                failing = false;
            } else |err| {
                _ = self.failed.fetchAdd(1, .monotonic);
                if (!failing) log.warn("the index did not take the corpus count: {s}", .{@errorName(err)});
                failing = true;
            }
            self.io.sleep(.fromSeconds(@intCast(self.interval_seconds)), .awake) catch return;
        }
    }
};

pub const StoreSource = struct {
    gpa: Allocator,
    owner: *Owner,
    reader: *store_mod.Reader,
    /// A position the owner's queue has not taken yet.
    unstored: ?u64 = null,

    pub fn open(gpa: Allocator, owner: *Owner) !StoreSource {
        return .{
            .gpa = gpa,
            .owner = owner,
            .reader = try store_mod.Reader.open(gpa, owner.io, owner.root, owner.path, owner.options.store.layout),
        };
    }

    pub fn close(self: *StoreSource) void {
        self.reader.close();
    }

    pub fn interface(self: *StoreSource) source.Source {
        return .{ .ptr = self, .vtable = &.{
            .changes = changes,
            .cursors = cursors,
            .actor = actor,
            .walk = walk,
            .policy = policy,
            .setPosition = setPosition,
        } };
    }

    fn changes(ptr: *anyopaque, arena: Allocator, from: u64, limit: usize) anyerror!source.Changes {
        const self: *StoreSource = @ptrCast(@alignCast(ptr));
        try self.reader.refresh();
        switch (try self.reader.changes(arena, from, limit)) {
            .trimmed => |floor| return .{ .trimmed = floor },
            .entries => |entries| {
                const out = try arena.alloc(source.Change, entries.len);
                for (out, entries) |*slot, entry| slot.* = .{ .sequence = entry.sequence, .did = entry.did };
                return .{ .entries = out };
            },
        }
    }

    fn cursors(ptr: *anyopaque) anyerror!source.Cursors {
        const self: *StoreSource = @ptrCast(@alignCast(ptr));
        try self.offerPosition();
        try self.reader.refresh();
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const stored = try self.reader.cursors(arena.allocator());
        return .{ .bridge = stored.bridge, .next_change = stored.next_change };
    }

    /// After a failed read: refresh when the owner collected a table the
    /// handle's snapshot still names, which only moves the handle forward.
    fn again(self: *StoreSource, err: anyerror, attempts: *usize) !void {
        if (err != error.Missing or attempts.* == 8) return err;
        attempts.* += 1;
        try self.reader.refresh();
    }

    fn record(self: *StoreSource, arena: Allocator, did: []const u8) !?store_mod.Actor {
        var attempts: usize = 0;
        while (true) {
            return self.reader.actor(arena, did) catch |err| {
                try self.again(err, &attempts);
                continue;
            };
        }
    }

    fn actor(ptr: *anyopaque, arena: Allocator, did: []const u8) anyerror!?source.Actor {
        const self: *StoreSource = @ptrCast(@alignCast(ptr));
        const found = try self.record(arena, did) orelse return null;
        const labels = try arena.alloc(source.Label, found.labels.len);
        for (labels, found.labels) |*slot, label| slot.* = .{
            .source = label.source,
            .uri = label.uri,
            .value = label.value,
            .created = label.created,
            .cid = label.cid,
            .expires = label.expires,
            .negated = label.negated,
        };
        return .{
            .status = switch (found.status) {
                .live => .live,
                .deactivated => .deactivated,
                .deleted => .deleted,
            },
            .hidden = found.hidden,
            .handle = found.handle,
            .display_name = found.display_name,
            .avatar = found.avatar,
            .description = found.description,
            .banner = found.banner,
            .pds = found.pds,
            .created_at = found.created_at,
            .associated = found.associated,
            .followers_count = found.followers_count,
            .follows_count = found.follows_count,
            .posts_count = found.posts_count,
            .quality_score = found.quality_score,
            .updated_at = found.updated_at,
            .profile_checked = found.profile_checked_at > 0,
            .labels = labels,
        };
    }

    fn walk(ptr: *anyopaque, arena: Allocator, after_did: []const u8, limit: usize) anyerror![]const []const u8 {
        const self: *StoreSource = @ptrCast(@alignCast(ptr));
        var attempts: usize = 0;
        while (true) {
            return self.reader.walk(arena, after_did, limit) catch |err| {
                try self.again(err, &attempts);
                continue;
            };
        }
    }

    /// The store's overrides and blocked domains. With a snapshot in force
    /// the index decides visibility itself, at query time, from labels,
    /// its slur rules, these lists and the label expiries, and ignores the
    /// stored `hidden` flag; it stops serving once the snapshot is 300 s
    /// old, and the bridge sends one with every push and heartbeat.
    fn policy(ptr: *anyopaque, arena: Allocator) anyerror!?source.Policy {
        const self: *StoreSource = @ptrCast(@alignCast(ptr));
        var attempts: usize = 0;
        while (true) {
            return self.snapshot(arena) catch |err| {
                try self.again(err, &attempts);
                continue;
            };
        }
    }

    fn snapshot(self: *StoreSource, arena: Allocator) !source.Policy {
        const stored = try store_mod.read.overrides(self.reader.db(), arena);
        const overrides = try arena.alloc(source.Override, stored.len);
        var kept: usize = 0;
        for (stored) |entry| {
            overrides[kept] = .{ .did = entry.subject, .action = switch (entry.entry.action) {
                .show => .show,
                .hide => .hide,
                .none => continue,
            } };
            kept += 1;
        }
        const blocked = try store_mod.read.blockedDomains(self.reader.db(), arena);
        const domains = try arena.alloc([]const u8, blocked.len);
        for (domains, blocked) |*slot, entry| slot.* = entry.subject;
        return .{ .overrides = overrides[0..kept], .blocked_domains = domains };
    }

    fn setPosition(ptr: *anyopaque, position: u64) anyerror!void {
        const self: *StoreSource = @ptrCast(@alignCast(ptr));
        self.owner.noteConsumer(consumer_name, position);
        self.unstored = position;
        try self.offerPosition();
    }

    fn offerPosition(self: *StoreSource) !void {
        const position = self.unstored orelse return;
        const submission = try owner_mod.Submission.create(self.gpa);
        submission.cursor = .{ .bridge = position };
        const taken = self.owner.offer(submission) catch |err| {
            submission.destroy();
            return err;
        };
        if (taken) self.unstored = null else submission.destroy();
    }
};
