//! Drives a store and the model through the same generated batches.

const std = @import("std");
const change = @import("../change.zig");
const engine = @import("../engine.zig");
const maintain = @import("../maintain.zig");
const record = @import("../record.zig");
const store_mod = @import("../store.zig");
const Model = @import("model.zig").Model;
const workload = @import("workload.zig");
const Store = store_mod.Store;
const Change = change.Change;

/// Tables, blocks and thresholds small enough that a few hundred commits
/// flush, compact across levels and collect many times over.
pub const small_layout: engine.Layout = .{ .fence_stride = 4, .block_bytes = 512, .cache_bytes = 64 << 10, .table_bytes = 12 << 10, .l0_tables = 2, .level_bytes = 48 << 10 };
pub const busy_policy: maintain.Policy = .{ .flush_bytes = 6 << 10, .flush_segments = 6, .compact_bytes = 3 << 10, .log_names = 4, .stall_tables = 3, .relief_steps = 2 };

pub fn options(trace: ?maintain.Trace) store_mod.Options {
    return .{ .layout = small_layout, .maintenance = busy_policy, .handle_flagged = workload.flagged, .trace = trace };
}

pub const Kind = std.meta.Tag(Change);

pub const Tally = struct {
    changes: u64 = 0,
    batches: u64 = 0,
    seen: std.EnumArray(Kind, u64) = .initFill(0),
    applied: std.EnumArray(Kind, u64) = .initFill(0),
    altered: std.EnumArray(Kind, u64) = .initFill(0),
    sweeps: u64 = 0,
    swept: u64 = 0,

    pub fn everyKindApplied(self: *const Tally) bool {
        for (std.enums.values(Kind)) |kind| if (self.applied.get(kind) == 0) return false;
        return true;
    }
};

pub const Mismatch = error{ModelMismatch};

/// Apply one generated batch to both. The model is told to commit only
/// after the store has.
pub fn applyStep(store: *Store, model: *Model, step: workload.Step, tally: *Tally) !void {
    const batch = try store.begin(step.now);
    errdefer batch.abort();
    model.begin(step.now);
    for (step.changes) |one| {
        const outcome = try batch.apply(one);
        const expected = model.apply(one);
        if (outcome.changed != expected) {
            std.debug.print("a {t} change altered {d} actors in the store, {d} in the model\n", .{ one, outcome.changed, expected });
            return error.ModelMismatch;
        }
        const kind = std.meta.activeTag(one);
        tally.seen.getPtr(kind).* += 1;
        tally.applied.getPtr(kind).* += @intFromBool(outcome.applied);
        tally.altered.getPtr(kind).* += outcome.changed;
        tally.changes += 1;
    }
    for (step.cursors) |update| {
        try batch.setCursor(update);
        model.setCursor(update);
    }
    try batch.commit();
    model.commit();
    tally.batches += 1;
}

/// Run the stepped sweep for every domain the batch blocked or unblocked.
pub fn sweepDomains(gpa: std.mem.Allocator, store: *Store, model: *Model, step: workload.Step, scan_limit: usize, tally: *Tally) !void {
    for (step.changes) |one| {
        const domain = switch (one) {
            .domain_blocked => |c| c.domain,
            .domain_unblocked => |name| name,
            else => continue,
        };
        const order = try model.sweepOrder(gpa);
        defer gpa.free(order);
        model.begin(step.now);
        var expected: u64 = 0;
        for (order) |did| {
            const handle = model.actors.get(did).?.handle;
            if (@import("../moderation.zig").matchesDomain(handle, domain)) expected += model.apply(.{ .hidden_refreshed = did });
        }
        model.commit();

        var sweep: Store.Sweep = .{};
        defer sweep.deinit(gpa);
        while (!sweep.done()) try store.sweepDomain(&sweep, domain, step.now, scan_limit);
        if (sweep.changed != expected) {
            std.debug.print("sweep of {s} altered {d} actors in the store, {d} in the model\n", .{ domain, sweep.changed, expected });
            return error.ModelMismatch;
        }
        tally.sweeps += 1;
        tally.swept += sweep.scanned;
    }
}

/// What an index fed from the change log would hold: each actor's content
/// as of the moment its change was consumed.
pub const Bridge = struct {
    arena: std.heap.ArenaAllocator,
    index: std.StringArrayHashMapUnmanaged(record.Actor) = .empty,
    position: u64 = 0,
    consumed: u64 = 0,

    pub fn init(gpa: std.mem.Allocator) Bridge {
        return .{ .arena = .init(gpa) };
    }

    pub fn deinit(self: *Bridge) void {
        self.arena.deinit();
    }

    /// Consume the log from the bridge's position to its end, reading each
    /// changed actor as it is now.
    pub fn consume(self: *Bridge, handle: anytype) !void {
        const mem = self.arena.allocator();
        while (true) {
            const page = switch (try handle.changes(mem, self.position, 16)) {
                .entries => |entries| entries,
                .trimmed => return error.ChangeLogTrimmed,
            };
            for (page) |entry| {
                if (try handle.actor(mem, entry.did)) |actor| {
                    try self.index.put(mem, entry.did, actor);
                } else _ = self.index.orderedRemove(entry.did);
                self.position = entry.sequence + 1;
                self.consumed += 1;
            }
            if (page.len < 16) return;
        }
    }

    /// The index must hold exactly the model's actors, with their content.
    pub fn check(self: *const Bridge, model: *const Model) !void {
        if (self.index.count() != model.actors.count()) {
            std.debug.print("an index fed from the change log holds {d} actors, the store {d}\n", .{ self.index.count(), model.actors.count() });
            return error.ModelMismatch;
        }
        for (model.actors.keys(), model.actors.values()) |did, want| {
            const got = self.index.get(did) orelse {
                std.debug.print("the change log never mentioned {s}\n", .{did});
                return error.ModelMismatch;
            };
            if (!got.contentEql(want)) {
                std.debug.print("the change log missed a change to {s}\nindex: {any}\nstore: {any}\n", .{ did, got, want });
                return error.ModelMismatch;
            }
        }
    }
};
