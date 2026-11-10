//! What the index bridge needs from the store process, as calls in the same
//! process. The bridge is not built here; this is the surface it codes
//! against.
//!
//! A bridge loops: `poll` from its position, read the current record of
//! every DID in the page with `actors` (absent or not live means remove it
//! from the index), push those to the index, then `ack` the position after
//! the page. `ack` is durable, so a restarted bridge continues from
//! `position`. An entry may be delivered again after a crash; applying the
//! current record twice is harmless.
//!
//! `poll` answers `resync` when the position is below the change log's
//! floor: entries the bridge never saw are gone. It must then rebuild the
//! index from a scan of every actor (`openScan`, which reads beside the
//! writer without holding it) and continue from the position `resync`
//! carries.

const std = @import("std");
const store_mod = @import("../store/root.zig");
const owner_mod = @import("owner.zig");
const Owner = owner_mod.Owner;
const Submission = owner_mod.Submission;

pub const consumer_name = "bridge";

pub const Page = union(enum) {
    /// Changed DIDs in commit order. The next position is the last
    /// sequence plus one.
    entries: []const store_mod.read.ChangeEntry,
    resync: u64,
};

pub const Feed = struct {
    owner: *Owner,

    /// The position last acknowledged, null for a bridge that never ran.
    pub fn position(self: Feed, arena: std.mem.Allocator) !?u64 {
        const store = try self.owner.acquire();
        defer self.owner.release();
        return (try store.cursors(arena)).bridge;
    }

    pub fn poll(self: Feed, arena: std.mem.Allocator, from: u64, limit: usize) !Page {
        const store = try self.owner.acquire();
        defer self.owner.release();
        return switch (try store.changes(arena, from, limit)) {
            .entries => |entries| .{ .entries = entries },
            .trimmed => |floor| .{ .resync = floor },
        };
    }

    /// The current record of each DID, null where the store holds none.
    pub fn actors(self: Feed, arena: std.mem.Allocator, dids: []const []const u8) ![]?store_mod.Actor {
        const store = try self.owner.acquire();
        defer self.owner.release();
        return store.actors(arena, dids);
    }

    /// Record that everything below `next` is in the index. The change log
    /// is trimmed no further than the slowest consumer's position.
    pub fn ack(self: Feed, gpa: std.mem.Allocator, next: u64) !void {
        const submission = try Submission.create(gpa);
        defer submission.destroy();
        submission.cursor = .{ .bridge = next };
        try self.owner.call(submission);
        self.owner.noteConsumer(consumer_name, next);
    }

    /// A read-only handle on the same store for a full scan. Close it when
    /// the scan is done; it holds table metadata of its own.
    pub fn openScan(self: Feed, gpa: std.mem.Allocator) !*store_mod.Reader {
        return store_mod.Reader.open(gpa, self.owner.io, self.owner.root, self.owner.path, self.owner.options.store.layout);
    }
};
