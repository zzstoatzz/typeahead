//! What the index bridge needs from the store, shaped after the store module
//! (`services/src/store/`) so an adapter is a field-by-field copy:
//!
//! | here            | store module                                           |
//! |-----------------|--------------------------------------------------------|
//! | `changes`       | `read.changes(db, arena, from, limit)`                 |
//! | `cursors`       | `read.cursors`: `.bridge` and `.next_change`           |
//! | `actor`         | `read.actor`; `Actor` mirrors `record.Actor` by name   |
//! | `walk`          | a page of the actor range's keys after a DID           |
//! | `policy`        | null (see below), or `read.overrides`/`blockedDomains` |
//! | `setPosition`   | `Store.setCursor(.{ .bridge = position })`             |
//!
//! Positions follow the store's convention: a position is the next sequence
//! to read, every entry below it has been applied, and `next_change` is the
//! position of a consumer that has read everything.
//!
//! Contract:
//!
//! - `changes(from, limit)` returns the entries at or after `from` in
//!   ascending sequence order, at most `limit`, or `.trimmed` when `from` is
//!   below the log's floor. The bridge answers `.trimmed` with a full resync.
//!   A DID may repeat; the bridge coalesces.
//! - `actor(did)` returns the record, or null when the store has none. A
//!   record whose `status` is not `.live` is a tombstone to the index, as is
//!   one with an empty handle.
//! - `walk(after_did, limit)` returns DIDs that have a record, in ascending
//!   byte order, strictly after `after_did`. Records of any status may be
//!   included; the bridge reads each one.
//! - Reads never go backward within a step: a read observes at least every
//!   commit an earlier read in the same step observed. The bridge reads the
//!   log first and records second, so what it pushes is never older than the
//!   entry that caused the push. A reader handle refreshed at the start of
//!   `cursors` and `changes` satisfies this.
//! - `policy()` returns null when the stored `hidden` flag is the whole
//!   moderation decision, which is what the store module computes. With a
//!   snapshot the index evaluates labels, overrides and blocked domains at
//!   query time and ignores `hidden`; it caps each list at 10,000 and stops
//!   being ready once the last snapshot is older than 300 seconds, so a store
//!   must not go from a snapshot back to null against the same index.
//! - `setPosition` records a position the index has durably committed. The
//!   bridge calls it after each commit that moved the position and once at
//!   start if the stored value differs. It may be asynchronous (handed to the
//!   writer and folded into its next commit): the bridge never reads its own
//!   write back within a step, and repeats the call after a restart if the
//!   value did not land. The store may trim the log below the stored value.
//!
//! Threading: every method is called from inside `Bridge.step`, on the
//! thread that called `step`, and from nowhere else. `step` blocks on
//! loopback HTTP for as long as the index takes to commit, so it should not
//! run on the thread that owns the writer handle; see docs/index-bridge.md.
//!
//! Every slice returned through an `arena` parameter is allocated from it
//! and is only read until the bridge's current step returns.

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const Change = struct {
    sequence: u64,
    did: []const u8,
};

pub const Changes = union(enum) {
    entries: []const Change,
    /// The log's floor: the lowest position a consumer may still ask for.
    trimmed: u64,
};

pub const Cursors = struct {
    /// The stored bridge position; null when it has never been set.
    bridge: ?u64,
    /// The sequence the next change-log entry will take.
    next_change: u64,
};

pub const Status = enum { live, deactivated, deleted };

pub const Label = struct {
    source: []const u8,
    uri: []const u8,
    value: []const u8,
    created: []const u8,
    cid: []const u8 = "",
    expires: []const u8 = "",
    negated: bool = false,
};

/// The fields of `record.Actor` the index holds, under the same names. These
/// are the fields whose change appends to the change log, and two that move
/// only when one of those does or once in an actor's life: `updated_at`,
/// which the store stamps when the content changes, and `profile_checked`,
/// whether `profile_checked_at` has ever been set. `last_activity_at` and
/// the raw `profile_checked_at` are left out on purpose: the store does not
/// log them, so sending them would make every resync rewrite documents no
/// log entry ever announced. `associated` is JSON object text, or empty for
/// an empty object.
pub const Actor = struct {
    status: Status = .live,
    hidden: bool = false,
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar: []const u8 = "",
    description: []const u8 = "",
    banner: []const u8 = "",
    pds: []const u8 = "",
    created_at: []const u8 = "",
    associated: []const u8 = "",
    followers_count: u64 = 0,
    follows_count: u64 = 0,
    posts_count: u64 = 0,
    quality_score: f32 = 0,
    updated_at: u64 = 0,
    profile_checked: bool = false,
    labels: []const Label = &.{},
};

pub const Override = struct {
    did: []const u8,
    action: Action,

    pub const Action = enum { show, hide };
};

pub const Policy = struct {
    overrides: []const Override = &.{},
    blocked_domains: []const []const u8 = &.{},
};

pub const Source = struct {
    ptr: *anyopaque,
    vtable: *const VTable,

    pub const VTable = struct {
        changes: *const fn (ptr: *anyopaque, arena: Allocator, from: u64, limit: usize) anyerror!Changes,
        cursors: *const fn (ptr: *anyopaque) anyerror!Cursors,
        actor: *const fn (ptr: *anyopaque, arena: Allocator, did: []const u8) anyerror!?Actor,
        walk: *const fn (ptr: *anyopaque, arena: Allocator, after_did: []const u8, limit: usize) anyerror![]const []const u8,
        policy: *const fn (ptr: *anyopaque, arena: Allocator) anyerror!?Policy,
        setPosition: *const fn (ptr: *anyopaque, position: u64) anyerror!void,
    };

    pub fn changes(self: Source, arena: Allocator, from: u64, limit: usize) !Changes {
        return self.vtable.changes(self.ptr, arena, from, limit);
    }

    pub fn cursors(self: Source) !Cursors {
        return self.vtable.cursors(self.ptr);
    }

    pub fn actor(self: Source, arena: Allocator, did: []const u8) !?Actor {
        return self.vtable.actor(self.ptr, arena, did);
    }

    pub fn walk(self: Source, arena: Allocator, after_did: []const u8, limit: usize) ![]const []const u8 {
        return self.vtable.walk(self.ptr, arena, after_did, limit);
    }

    pub fn policy(self: Source, arena: Allocator) !?Policy {
        return self.vtable.policy(self.ptr, arena);
    }

    pub fn setPosition(self: Source, position: u64) !void {
        return self.vtable.setPosition(self.ptr, position);
    }
};
