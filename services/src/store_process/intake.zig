//! The relay consumer of the store role: firehose events in, batches of
//! store changes out to the owner.
//!
//! The read loop never waits for a commit. A batch is handed to the owner's
//! queue when it holds `max_changes` or has been open `max_age_seconds`, and
//! the next event is read at once; the owner commits on its own thread. The
//! loop blocks only when the queue is full, which is how a slow or stalled
//! store slows the relay down. A batch holds whole events and carries the
//! seq of its last one, and batches commit in the order they were handed
//! over, so the stored relay cursor never passes an event that is not
//! stored with it.

const std = @import("std");
const zat = @import("zat");
const events = @import("events.zig");
const owner_mod = @import("owner.zig");
const Owner = owner_mod.Owner;
const Submission = owner_mod.Submission;

const log = std.log.scoped(.store_intake);

pub const Signals = struct {
    events: std.atomic.Value(u64) = .init(0),
    batches: std.atomic.Value(u64) = .init(0),
    /// Unix seconds an event last arrived, or intake last resumed.
    last_event_at: std.atomic.Value(u64) = .init(0),
    /// Unix seconds the relay stamped on the newest event.
    source_event_at: std.atomic.Value(i64) = .init(0),
    /// The relay seq of the last batch handed to the owner.
    handed_cursor: std.atomic.Value(i64) = .init(0),
    /// True while the read loop waits on a full queue.
    paused: std.atomic.Value(bool) = .init(false),
};

pub const Options = struct {
    max_changes: usize = 400,
    max_age_seconds: u64 = 1,
    /// Called when an event cannot be mapped or handed over. Continuing
    /// would move the cursor past it, so production exits and replays.
    on_fatal: *const fn () void = noFatal,

    fn noFatal() void {}
};

pub const Intake = struct {
    gpa: std.mem.Allocator,
    owner: *Owner,
    options: Options = .{},
    signals: Signals = .{},
    open: ?*Submission = null,
    changes: events.Changes = undefined,
    opened_at: u64 = 0,
    /// The seq of the newest event heard, and of the last one handed over.
    heard: i64 = 0,
    handed: i64 = 0,
    dead: bool = false,

    pub fn deinit(self: *Intake) void {
        if (self.open) |submission| submission.destroy();
        self.open = null;
    }

    /// Start after `cursor`, the relay seq the store holds.
    pub fn resumeFrom(self: *Intake, cursor: i64) void {
        self.heard = cursor;
        self.handed = cursor;
        self.signals.handed_cursor.store(cursor, .monotonic);
        self.signals.last_event_at.store(self.owner.now(), .monotonic);
    }

    pub fn onEvent(self: *Intake, event: zat.FirehoseEvent) void {
        if (self.dead) return;
        self.hear(event) catch |err| {
            log.warn("relay event at seq {d} could not be queued: {s}", .{ self.heard, @errorName(err) });
            self.dead = true;
            self.options.on_fatal();
        };
    }

    pub fn onError(_: *Intake, err: anyerror) void {
        log.err("firehose: {s}", .{@errorName(err)});
    }

    pub fn onConnect(_: *Intake, host: []const u8) void {
        log.info("connected to {s}", .{host});
    }

    pub fn shouldStop(self: *Intake) bool {
        return self.dead;
    }

    fn hear(self: *Intake, event: zat.FirehoseEvent) !void {
        const now = self.owner.now();
        if (self.open == null) {
            const submission = try Submission.create(self.gpa);
            self.open = submission;
            self.changes = .{ .arena = submission.arena.allocator() };
            self.opened_at = now;
        }
        try self.changes.firehose(event);
        if (event.seq()) |seq| self.heard = seq;

        const source_time: ?[]const u8 = switch (event) {
            .commit => |c| c.time,
            .identity => |id| id.time,
            .account => |acct| acct.time,
            .sync => |sync_event| sync_event.time,
            .info => null,
        };
        if (source_time) |text| {
            if (zat.Datetime.parse(text)) |parsed| self.signals.source_event_at.store(@divFloor(parsed.micros, 1_000_000), .monotonic);
        }
        _ = self.signals.events.fetchAdd(1, .monotonic);
        self.signals.last_event_at.store(now, .monotonic);

        if (self.changes.items().len >= self.options.max_changes or now - self.opened_at >= self.options.max_age_seconds) try self.flush();
    }

    /// Hand the open batch to the owner. A batch with no changes still
    /// carries the cursor forward, so a quiet stretch is not replayed.
    pub fn flush(self: *Intake) !void {
        const submission = self.open orelse return;
        if (self.changes.items().len == 0 and self.heard == self.handed) return;
        submission.changes = self.changes.items();
        submission.cursor = .{ .relay = @intCast(self.heard) };
        defer if (self.signals.paused.load(.monotonic)) {
            // The relay went unread on purpose, so the pause is not an
            // upstream stall.
            self.signals.last_event_at.store(self.owner.now(), .monotonic);
            self.signals.paused.store(false, .monotonic);
        };
        try self.owner.post(submission, &self.signals.paused);
        self.open = null;
        self.handed = self.heard;
        self.signals.handed_cursor.store(self.handed, .monotonic);
        _ = self.signals.batches.fetchAdd(1, .monotonic);
    }
};

pub const stall_seconds: u64 = 10 * 60;

pub const Wedge = enum { relay_silent, owner_stuck };

/// Whether the process is wedged and should exit for a restart: no relay
/// event for `stall_seconds` while intake was not paused on the store, or
/// an owner that has not finished a turn for as long. A long stall is
/// neither: the owner keeps turning and intake is paused.
pub fn wedged(now: u64, last_event_at: u64, intake_paused: bool, last_owner_turn_at: u64) ?Wedge {
    if (now -| last_owner_turn_at >= stall_seconds) return .owner_stuck;
    if (!intake_paused and now -| last_event_at >= stall_seconds) return .relay_silent;
    return null;
}
