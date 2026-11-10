//! The index bridge's view of the store (`bridge/source.zig`), and the
//! bridge's place in the store role.
//!
//! The bridge runs on its own thread: a step blocks on loopback HTTP for as
//! long as the index takes to commit, so it must not be the thread that
//! owns the writer. It reads through a read-only handle of its own,
//! refreshed at the start of `cursors` and `changes`, so its reads never
//! wait on a commit and never go backward within a step. `setPosition` is
//! handed to the owner, which folds it into its next commit; the bridge
//! repeats it after a restart if it did not land.

const std = @import("std");
const bridge = @import("../bridge/root.zig");
const store_mod = @import("../store/root.zig");
const owner_mod = @import("owner.zig");
const Allocator = std.mem.Allocator;
const Owner = owner_mod.Owner;
const source = bridge.source;

pub const consumer_name = "bridge";

pub const StoreSource = struct {
    gpa: Allocator,
    owner: *Owner,
    reader: *store_mod.Reader,

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
            .pds = found.pds,
            .created_at = found.created_at,
            .associated = found.associated,
            .followers_count = found.followers_count,
            .follows_count = found.follows_count,
            .posts_count = found.posts_count,
            .quality_score = found.quality_score,
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

    /// Null: the stored `hidden` flag is the whole moderation decision, and
    /// the index filters on it.
    fn policy(_: *anyopaque, _: Allocator) anyerror!?source.Policy {
        return null;
    }

    fn setPosition(ptr: *anyopaque, position: u64) anyerror!void {
        const self: *StoreSource = @ptrCast(@alignCast(ptr));
        const submission = try owner_mod.Submission.create(self.gpa);
        errdefer submission.destroy();
        submission.cursor = .{ .bridge = position };
        try self.owner.post(submission, null);
        self.owner.noteConsumer(consumer_name, position);
    }
};
