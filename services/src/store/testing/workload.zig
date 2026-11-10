//! Deterministic synthetic changes over a small universe of accounts. Batch
//! `n` of a seed is a pure function of both, so two processes that agree on
//! a seed and a batch number agree on the changes.

const std = @import("std");
const change = @import("../change.zig");
const keys = @import("../keys.zig");
const moderation = @import("../moderation.zig");
const record = @import("../record.zig");
const Change = change.Change;

pub const base_time = 1_794_000_000;

pub const Config = struct {
    seed: u64,
    universe: usize = 48,
    /// Follow a domain change with a recompute of every account in the same
    /// batch, for harnesses that cannot run the stepped sweep.
    inline_sweep: bool = false,
    /// Never remove an actor record, so the actor range keeps its size.
    keep_actors: bool = false,
};

pub const Step = struct {
    now: u64,
    changes: []const Change,
    cursors: []const change.CursorUpdate,
};

pub fn timeOf(seed: u64, n: u64) u64 {
    return base_time + n * 240 + std.hash.Wyhash.hash(seed, std.mem.asBytes(&n)) % 200;
}

pub fn flagged(handle: []const u8) bool {
    return std.ascii.findIgnoreCase(handle, "slur") != null;
}

pub fn didOf(arena: std.mem.Allocator, index: usize) ![]const u8 {
    return std.fmt.allocPrint(arena, "did:plc:{d:0>4}aaaaaaaaaaaaaaaaaaaa", .{index});
}

const names = [_][]const u8{ "alice", "bob", "carol", "Slur-Farm", "dave", "erin" };
const domains = [_][]const u8{ "bsky.social", "spam.example", "deep.spam.example", "Other.Test", "example", "blacksky.app" };
const blockable = [_][]const u8{ "spam.example", "SPAM.Example", "other.test", "nomatch.invalid", "example" };
const displays = [_][]const u8{ "", "Alice", "bob \u{1F98A}", "C", "a much longer display name with spaces" };
const cids = [_][]const u8{ "", "bafkreiaaa", "bafkreibbb", "bafkreiccc" };
const hosts = [_][]const u8{ "", "https://pds.example", "https://morel.us-east.host.bsky.network", "https://blacksky.app" };
const texts = [_][]const u8{ "", "first description", "second, longer description\nwith a second line" };
const stamps = [_][]const u8{ "", "2024-11-17T08:15:30.123Z", "2026-11-09T00:00:00Z", "not a date" };
const associations = [_][]const u8{ "", "{}", "{\"lists\":3}", "{\"chat\":{\"allowIncoming\":\"all\"}}" };
const scores = [_]f32{ 0, 0, 0.5, 12.25, 294.6, 294.6, 812.37, 1000 };
const label_values = [_][]const u8{ "!hide", "!takedown", "!suspend", "spam", "!no-unauthenticated", "porn" };
const reasons = [_][]const u8{ "", "manual review", "spam farm" };
const errors = [_][]const u8{ "HTTP 503", "timeout", "" };

const Gen = struct {
    arena: std.mem.Allocator,
    random: std.Random,
    config: Config,
    now: u64,

    fn pick(self: *Gen, comptime T: type, pool: []const T) T {
        return pool[self.random.uintLessThan(usize, pool.len)];
    }

    fn chance(self: *Gen, percent: u8) bool {
        return self.random.uintLessThan(u8, 100) < percent;
    }

    fn did(self: *Gen) ![]const u8 {
        return didOf(self.arena, self.random.uintLessThan(usize, self.config.universe));
    }

    fn handle(self: *Gen) ![]const u8 {
        if (self.chance(25)) return "";
        const text = try std.fmt.allocPrint(self.arena, "{s}{d}.{s}", .{ self.pick([]const u8, &names), self.random.uintLessThan(usize, self.config.universe / 3 + 1), self.pick([]const u8, &domains) });
        if (self.chance(30)) for (text) |*c| {
            if (self.chance(40)) c.* = std.ascii.toUpper(c.*);
        };
        return text;
    }

    fn stamp(self: *Gen, at: u64) ![]const u8 {
        const day: std.time.epoch.EpochDay = .{ .day = @intCast(at / 86_400) };
        const year_day = day.calculateYearDay();
        const month_day = year_day.calculateMonthDay();
        const seconds = at % 86_400;
        return std.fmt.allocPrint(self.arena, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}Z", .{
            year_day.year, month_day.month.numeric(), month_day.day_index + 1, seconds / 3600, seconds % 3600 / 60, seconds % 60,
        });
    }

    fn expiry(self: *Gen) ![]const u8 {
        if (self.chance(60)) return "";
        if (self.chance(10)) return "never";
        return self.stamp(self.now - 600 + self.random.uintLessThan(u64, 4000));
    }

    fn label(self: *Gen, subject_did: []const u8) !record.Label {
        const moderated = self.chance(70);
        const uri = switch (self.random.uintLessThan(u8, 5)) {
            0, 1 => subject_did,
            2, 3 => try change.subjectUri(self.arena, subject_did, .profile),
            else => try std.fmt.allocPrint(self.arena, "at://{s}/app.bsky.feed.post/3kabc", .{subject_did}),
        };
        return .{
            .source = if (moderated) moderation.bsky_moderation else subject_did,
            .uri = uri,
            .value = self.pick([]const u8, &label_values),
            .created = if (self.chance(15)) "garbage" else try self.stamp(self.now - self.random.uintLessThan(u64, 3)),
            .cid = self.pick([]const u8, &cids),
            .expires = try self.expiry(),
            .negated = self.chance(20),
            .version = self.random.uintLessThan(u64, 2),
        };
    }

    fn labels(self: *Gen, subject_did: []const u8) ![]const record.Label {
        const list = try self.arena.alloc(record.Label, self.random.uintLessThan(usize, 5));
        for (list) |*slot| slot.* = try self.label(subject_did);
        return list;
    }

    fn next(self: *Gen) !Change {
        const subject = try self.did();
        return switch (self.random.uintLessThan(u8, 100)) {
            0...11 => .{ .observe = .{ .did = subject, .enqueue_identity = self.chance(50) } },
            12...25 => .{ .profile = .{
                .did = subject,
                .handle = try self.handle(),
                .display_name = self.pick([]const u8, &displays),
                .avatar = self.pick([]const u8, &cids),
                .pds = self.pick([]const u8, &hosts),
                .description = self.pick([]const u8, &texts),
                .banner = self.pick([]const u8, &cids),
                .enqueue_identity = self.chance(50),
            } },
            26...31 => .{ .identity_resolved = .{
                .did = subject,
                .observed_at = self.now - self.random.uintLessThan(u64, 2000),
                .handle = try self.handle(),
                .pds = self.pick([]const u8, &hosts),
            } },
            32...35 => .{ .gone = .{ .did = subject, .status = if (self.chance(50)) .deleted else .deactivated } },
            36...39 => .{ .reactivated = subject },
            40...48 => .{ .label = .{
                .did = subject,
                .source = if (self.chance(90)) moderation.bsky_moderation else subject,
                .value = self.pick([]const u8, &label_values),
                .subject = if (self.chance(50)) .account else .profile,
                .negated = self.chance(30),
                .created = try self.stamp(self.now),
                .cid = self.pick([]const u8, &cids),
                .expires = try self.expiry(),
                .sequence = if (self.chance(20)) self.random.uintLessThan(u64, 50) else self.now - base_time + self.random.uintLessThan(u64, 100),
            } },
            49...51 => .{ .labels_reconciled = .{ .did = subject, .labels = try self.labels(subject) } },
            52...54 => .{ .override_set = .{ .did = subject, .action = if (self.chance(50)) .show else .hide, .reason = self.pick([]const u8, &reasons) } },
            55...56 => .{ .override_cleared = subject },
            57 => .{ .domain_blocked = .{ .domain = self.pick([]const u8, &blockable), .reason = self.pick([]const u8, &reasons) } },
            58 => .{ .domain_unblocked = self.pick([]const u8, &blockable) },
            59...68 => .{ .enrichment = .{
                .did = subject,
                .source = self.pick(record.ProfileSource, &.{ .bsky, .bsky, .blacksky, .pds }),
                .handle = try self.handle(),
                .display_name = self.pick([]const u8, &displays),
                .avatar = self.pick([]const u8, &cids),
                .description = self.pick([]const u8, &texts),
                .banner = self.pick([]const u8, &cids),
                .created_at = self.pick([]const u8, &stamps),
                .associated = self.pick([]const u8, &associations),
                .followers_count = if (self.chance(30)) 0 else self.random.uintLessThan(u64, 500_000),
                .follows_count = if (self.chance(30)) 0 else self.random.uintLessThan(u64, 5000),
                .posts_count = if (self.chance(30)) 0 else self.random.uintLessThan(u64, 100_000),
                .quality_score = self.pick(f32, &scores),
                .labels = if (self.chance(40)) null else try self.labels(subject),
                .next_refresh = if (self.chance(30)) null else self.now + self.random.uintLessThan(u64, 5000),
            } },
            69...74 => .{ .identity_enqueue = .{ .did = subject, .reason = self.pick(record.IdentityReason, std.enums.values(record.IdentityReason)) } },
            75...77 => .{ .identity_lease = .{ .limit = 1 + self.random.uintLessThan(usize, 6), .lease_seconds = 300 } },
            78...84 => .{ .identity_complete = .{
                .did = subject,
                .generation = 1 + self.random.uintLessThan(u64, 3),
                .observed_at = self.now - self.random.uintLessThan(u64, 2000),
                .pds = self.pick([]const u8, &hosts),
                .outcome = if (self.chance(55))
                    .{ .resolved = .{ .handle = try self.handle() } }
                else
                    .{ .retry = .{ .attempts = 1 + self.random.uintLessThan(u64, 13), .delay_seconds = self.random.uintLessThan(u64, 2000), .reason = self.pick([]const u8, &errors) } },
            } },
            85...88 => .{ .refresh_enqueue = .{ .did = subject, .due = self.now - 300 + self.random.uintLessThan(u64, 3000) } },
            89...92 => .{ .refresh_complete = .{ .did = subject, .next_due = if (self.chance(40)) null else self.now + self.random.uintLessThan(u64, 5000) } },
            93...96 => .{ .hidden_refreshed = subject },
            else => if (self.config.keep_actors)
                .{ .hidden_refreshed = subject }
            else
                .{ .tombstones_pruned = .{ .before = self.now - self.random.uintLessThan(u64, 6000), .limit = 1 + self.random.uintLessThan(usize, 4) } },
        };
    }
};

/// Batch `n` (from 1) of `size` generated changes, with the relay cursor
/// `n` and sometimes the other cursors.
pub fn batch(arena: std.mem.Allocator, config: Config, n: u64, size: usize) !Step {
    var prng: std.Random.DefaultPrng = .init(std.hash.Wyhash.hash(config.seed, std.mem.asBytes(&n)));
    var gen: Gen = .{ .arena = arena, .random = prng.random(), .config = config, .now = timeOf(config.seed, n) };
    var changes: std.ArrayList(Change) = .empty;
    for (0..size) |_| {
        const one = try gen.next();
        try changes.append(arena, one);
        if (config.inline_sweep and (one == .domain_blocked or one == .domain_unblocked)) {
            for (0..config.universe) |index| try changes.append(arena, .{ .hidden_refreshed = try didOf(arena, index) });
        }
    }
    var cursors: std.ArrayList(change.CursorUpdate) = .empty;
    try cursors.append(arena, .{ .relay = n });
    if (gen.chance(40)) try cursors.append(arena, .{ .labeler = gen.now - base_time + gen.random.uintLessThan(u64, 50) -| 25 });
    if (gen.chance(30)) try cursors.append(arena, .{ .plc = try gen.stamp(gen.now - gen.random.uintLessThan(u64, 400)) });
    return .{ .now = gen.now, .changes = changes.items, .cursors = cursors.items };
}

test "a batch is a pure function of its seed and number" {
    var arena: std.heap.ArenaAllocator = .init(std.testing.allocator);
    defer arena.deinit();
    const a = try batch(arena.allocator(), .{ .seed = 7 }, 3, 40);
    const b = try batch(arena.allocator(), .{ .seed = 7 }, 3, 40);
    try std.testing.expectEqualDeep(a, b);
    try std.testing.expectEqual(@as(usize, 40), a.changes.len);
    for (1..200) |n| try std.testing.expect(timeOf(7, n) > timeOf(7, n - 1));
}
