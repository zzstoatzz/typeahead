//! Shared by the role's tests: an owner over a scratch store, stepped by
//! hand under a clock the test sets.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const owner_mod = @import("owner.zig");
const events = @import("events.zig");
const Owner = owner_mod.Owner;
const Submission = owner_mod.Submission;
const Change = store_mod.Change;

pub const t0: u64 = 1_794_000_000;

pub const FakeClock = struct {
    at: std.atomic.Value(u64) = .init(t0),

    fn seconds(context: ?*anyopaque, _: std.Io) u64 {
        const self: *FakeClock = @ptrCast(@alignCast(context.?));
        return self.at.load(.monotonic);
    }

    pub fn clock(self: *FakeClock) owner_mod.Clock {
        return .{ .context = self, .seconds = seconds };
    }
};

pub const Fixture = struct {
    tmp: std.testing.TmpDir,
    arena: std.heap.ArenaAllocator,
    clock: FakeClock = .{},
    owner: *Owner,

    pub fn create(options: owner_mod.Options) !*Fixture {
        const gpa = std.testing.allocator;
        const self = try gpa.create(Fixture);
        errdefer gpa.destroy(self);
        self.* = .{ .tmp = std.testing.tmpDir(.{ .iterate = true }), .arena = .init(gpa), .owner = undefined };
        errdefer self.tmp.cleanup();
        var with_clock = options;
        with_clock.clock = self.clock.clock();
        self.owner = try Owner.open(gpa, std.testing.io, self.tmp.dir, "store", with_clock);
        return self;
    }

    pub fn destroy(self: *Fixture) void {
        self.owner.close();
        self.arena.deinit();
        self.tmp.cleanup();
        std.testing.allocator.destroy(self);
    }

    /// Close the owner and open the same store again, as a restart does.
    pub fn restart(self: *Fixture) !void {
        const options = self.owner.options;
        self.owner.close();
        self.owner = try Owner.open(std.testing.allocator, std.testing.io, self.tmp.dir, "store", options);
    }

    pub fn now(self: *Fixture) u64 {
        return self.clock.at.load(.monotonic);
    }

    pub fn advance(self: *Fixture, seconds: u64) void {
        _ = self.clock.at.fetchAdd(seconds, .monotonic);
    }

    /// Queue `changes` with `cursor` and run the owner until they commit.
    pub fn commit(self: *Fixture, list: []const Change, cursor: ?store_mod.change.CursorUpdate) !*Submission {
        const submission = try Submission.create(self.arena.allocator());
        submission.changes = list;
        submission.cursor = cursor;
        submission.waited = true;
        try self.owner.post(submission, null);
        while (!submission.done.isSet()) _ = self.owner.turn(0);
        if (submission.failed) return error.OwnerStopped;
        return submission;
    }

    pub fn changes(self: *Fixture) events.Changes {
        return .{ .arena = self.arena.allocator() };
    }

    pub fn actor(self: *Fixture, did: []const u8) !?store_mod.Actor {
        const store = try self.owner.acquire();
        defer self.owner.release();
        return store.actor(self.arena.allocator(), did);
    }

    pub fn live(self: *Fixture, did: []const u8) !store_mod.Actor {
        const found = try self.actor(did) orelse return error.TestExpectedActor;
        try std.testing.expect(found.live());
        return found;
    }

    pub fn cursors(self: *Fixture) !store_mod.read.Cursors {
        const store = try self.owner.acquire();
        defer self.owner.release();
        return store.cursors(self.arena.allocator());
    }

    /// The identity queue entry of `did`, when it has one.
    pub fn queued(self: *Fixture, did: []const u8) !?store_mod.record.QueueState {
        const found = try self.actor(did) orelse return null;
        if (found.identity_due == 0) return null;
        const store = try self.owner.acquire();
        defer self.owner.release();
        return store_mod.read.queueState(store.db(), self.arena.allocator(), .{ .at = found.identity_due, .did = did });
    }
};

/// The owner on its own thread, for tests whose producers wait on commits.
/// Stop it before reading the store from the test, and before the fixture
/// goes away on any path.
pub const Running = struct {
    f: *Fixture,
    thread: ?std.Thread,

    pub fn start(f: *Fixture) !Running {
        return .{ .f = f, .thread = try std.Thread.spawn(.{}, Owner.run, .{f.owner}) };
    }

    pub fn stop(self: *Running) void {
        const thread = self.thread orelse return;
        self.f.owner.stop();
        thread.join();
        self.thread = null;
    }
};
