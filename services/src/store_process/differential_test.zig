//! Differential test: one seeded sequence of synthetic input goes through
//! the store role (relay intake, identity work, PLC tail, label sink, the
//! owner on its own thread) and through `sql_model.zig`, and every account's
//! visible state is compared: whether it is live, its handle, PDS, profile
//! fields, labels, hidden flag, and whether identity work is pending.
//!
//! With `Divergence.store` the two must agree exactly. The directed tests at
//! the end run the model as production (`Divergence.production`) and show
//! what each flag changes.
//!
//! A failure prints its seed and step; `STORE_DIFF_SEED=<n>` runs one seed.

const std = @import("std");
const zat = @import("zat");
const store_mod = @import("../store/root.zig");
const store_harness = @import("../store/testing/harness.zig");
const identity = @import("../identity.zig");
const label_tail = @import("../label_tail.zig");
const identity_work = @import("identity_work.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const sql = @import("sql_model.zig");
const tails = @import("tails.zig");
const harness = @import("testing.zig");
const Fixture = harness.Fixture;
const testing = std.testing;

const universe = 36;
const cids = [_][]const u8{ "bafkreiegehiwbazkkpp5nucenajtdjwinxrrnz7ukawpqnudbr47f44zhq", "bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku" };
const names = [_][]const u8{ "", "Alice", "bob", "a longer display name" };
const texts = [_][]const u8{ "", "first description", "second description" };
const values = [_][]const u8{ "!hide", "!takedown", "spam", "porn", "!no-unauthenticated" };
const hosts = [_][]const u8{ "https://pds.example", "https://morel.us-east.host.bsky.network", "https://blacksky.app" };

const Identity = struct {
    handle: ?[]const u8 = null,
    pds: ?[]const u8 = null,
    verified: bool = true,
    available: bool = true,
};

const Relayed = struct { seq: i64, event: sql.Event };

const Sim = struct {
    f: *Fixture,
    running: harness.Running,
    arena: std.mem.Allocator,
    random: std.Random,
    model: sql.Model,
    intake: intake_mod.Intake,
    work: identity_work.Work,
    sink: tails.LabelSink,
    plc: tails.PlcTail,
    world: [universe]Identity = @splat(.{}),
    relayed: std.ArrayList(Relayed) = .empty,
    blobs: [cids.len][]const u8 = undefined,
    label_seq: u64 = 100,
    label_cursor: u64 = 0,
    plc_cursor: ?[]const u8 = null,
    step: usize = 0,
    restarts: u64 = 0,
    pruned: u64 = 0,

    fn create(flags: sql.Divergence, seed: u64, prng: *std.Random.DefaultPrng) !*Sim {
        const f = try Fixture.create(.{
            .store = .{ .layout = store_harness.small_layout, .maintenance = store_harness.busy_policy },
            .idle_wait_ms = 1,
            .expiry_seconds = 0,
            .prune_seconds = 0,
        });
        errdefer f.destroy();
        prng.* = .init(seed);
        const arena = f.arena.allocator();
        const self = try arena.create(Sim);
        self.* = .{
            .f = f,
            .running = undefined,
            .arena = arena,
            .random = prng.random(),
            .model = .{ .arena = arena, .flags = flags, .now = f.now(), .parse_ms = store_mod.time.parseMs },
            .intake = .{ .gpa = testing.allocator, .owner = f.owner, .options = .{ .max_changes = 6 } },
            .work = .{ .gpa = testing.allocator, .io = testing.io, .owner = f.owner, .resolver = undefined },
            .sink = .{ .gpa = testing.allocator, .owner = f.owner },
            .plc = .{ .gpa = testing.allocator, .io = testing.io, .owner = f.owner },
        };
        for (&self.blobs, cids) |*raw, text| raw.* = try zat.multibase.decode(arena, text);
        self.running = try harness.Running.start(f);
        return self;
    }

    fn destroy(self: *Sim) void {
        self.running.stop();
        self.intake.deinit();
        self.f.destroy();
    }

    fn did(self: *Sim, index: usize) []const u8 {
        const text = self.arena.dupe(u8, "did:plc:2222aaaaaaaaaaaaaaaaaaaa") catch @panic("out of memory");
        var left = index;
        for (0..4) |place| {
            text["did:plc:".len + 3 - place] = '2' + @as(u8, @intCast(left % 6));
            left /= 6;
        }
        return text;
    }

    /// The inverse of `did`: label subjects must be base32, so the index is
    /// written in the digits 2 to 7.
    fn indexOf(subject: []const u8) usize {
        var index: usize = 0;
        for (subject["did:plc:".len..][0..4]) |c| index = index * 6 + (c - '2');
        return index;
    }

    fn anyDid(self: *Sim) []const u8 {
        return self.did(self.random.uintLessThan(usize, universe));
    }

    fn pick(self: *Sim, pool: []const []const u8) []const u8 {
        return pool[self.random.uintLessThan(usize, pool.len)];
    }

    fn chance(self: *Sim, percent: u8) bool {
        return self.random.uintLessThan(u8, 100) < percent;
    }

    fn blob(self: *Sim, cid: []const u8) zat.cbor.Value {
        const raw = for (cids, self.blobs) |text, bytes| {
            if (std.mem.eql(u8, text, cid)) break bytes;
        } else unreachable;
        const fields = self.arena.dupe(zat.cbor.Value.MapEntry, &.{.{ .key = "ref", .value = .{ .cid = .{ .raw = raw } } }}) catch @panic("out of memory");
        return .{ .map = fields };
    }

    /// The relay event for a model event, as the firehose would carry it.
    fn wire(self: *Sim, seq: i64, event: sql.Event) zat.FirehoseEvent {
        const stamp = "2026-11-01T00:00:00.000Z";
        switch (event) {
            .sighting => |subject| return .{ .commit = .{ .seq = seq, .repo = subject, .rev = "", .time = stamp, .ops = &.{
                .{ .action = .create, .collection = "app.bsky.feed.like", .rkey = "3k" },
                .{ .action = .create, .collection = "app.bsky.feed.repost", .rkey = "3k" },
            } } },
            .profile => |p| {
                var fields: std.ArrayList(zat.cbor.Value.MapEntry) = .empty;
                if (p.display_name.len > 0) fields.append(self.arena, .{ .key = "displayName", .value = .{ .text = p.display_name } }) catch @panic("out of memory");
                if (p.description.len > 0) fields.append(self.arena, .{ .key = "description", .value = .{ .text = p.description } }) catch @panic("out of memory");
                if (p.avatar.len > 0) fields.append(self.arena, .{ .key = "avatar", .value = self.blob(p.avatar) }) catch @panic("out of memory");
                if (p.banner.len > 0) fields.append(self.arena, .{ .key = "banner", .value = self.blob(p.banner) }) catch @panic("out of memory");
                const ops = self.arena.dupe(zat.firehose.RepoOp, &.{
                    .{ .action = .update, .collection = "app.bsky.actor.profile", .rkey = "self", .record = .{ .map = fields.items } },
                }) catch @panic("out of memory");
                return .{ .commit = .{ .seq = seq, .repo = p.did, .rev = "", .time = stamp, .ops = ops } };
            },
            .identity => |subject| return .{ .identity = .{ .seq = seq, .did = subject, .time = stamp, .handle = if (self.chance(30)) "claimed.example" else null } },
            .active => |subject| return .{ .account = .{ .seq = seq, .did = subject, .time = stamp, .active = true } },
            .inactive => |i| return .{ .account = .{ .seq = seq, .did = i.did, .time = stamp, .active = false, .status = if (i.deleted) .deleted else if (self.chance(50)) .deactivated else .takendown } },
        }
    }

    fn relay(self: *Sim, event: sql.Event) !void {
        const seq: i64 = @intCast(self.relayed.items.len + 1);
        try self.relayed.append(self.arena, .{ .seq = seq, .event = event });
        self.intake.onEvent(self.wire(seq, event));
        self.model.ingest(event);
    }

    fn barrier(self: *Sim) !void {
        const submission = try owner_mod.Submission.create(testing.allocator);
        defer submission.destroy();
        try self.f.owner.call(submission);
    }

    /// Everything handed over is committed and a housekeeping pass has run
    /// after it; the model does the same housekeeping.
    fn settle(self: *Sim) !void {
        try self.intake.flush();
        try self.barrier();
        try self.barrier();
        try self.barrier();
        self.model.expiryPass();
        self.model.prune();
    }

    fn advance(self: *Sim, seconds: u64) !void {
        try self.settle();
        self.f.advance(seconds);
        self.model.now = self.f.now();
        try self.settle();
    }

    fn drain(self: *Sim) !void {
        try self.settle();
        var jobs: [identity.LEASE_BATCH]identity.Job = undefined;
        const leased = try self.work.lease(&jobs);
        const expected = self.model.lease(identity.LEASE_BATCH);
        try testing.expectEqual(expected.len, leased);
        for (jobs[0..leased], expected) |job, want| {
            try testing.expectEqualStrings(want.did, job.did.slice());
            try testing.expectEqual(want.attempts, @as(u64, job.attempts));
            if (self.chance(15)) continue;
            const index = indexOf(want.did);
            const known = self.world[index];
            const lookup: ?identity.Lookup = if (known.available) .{ .handle = known.handle, .pds = known.pds, .verified = known.verified } else null;
            const outcome = identity.decide(lookup, job.attempts);
            const observed_at = self.f.now() - self.random.uintLessThan(u64, 3);
            try self.work.report(job, .{ .observed_at = @intCast(observed_at), .outcome = outcome });
            self.model.settle(.{
                .did = want.did,
                .gen = want.gen,
                .observed_at = observed_at,
                .pds = switch (outcome) {
                    .resolved => |r| r.pds orelse "",
                    .retry => |r| r.pds orelse "",
                },
                .outcome = switch (outcome) {
                    .resolved => |r| .{ .resolved = .{ .handle = r.handle orelse "", .verified = r.handle_verified } },
                    .retry => |r| .{ .retry = .{ .attempts = job.attempts, .delay_seconds = @intCast(r.delay_seconds), .reason = r.@"error" } },
                },
            });
        }
    }

    fn stampOf(self: *Sim, at: u64) []const u8 {
        var buf: [24]u8 = undefined;
        return self.arena.dupe(u8, tails.isoSeconds(&buf, at)) catch @panic("out of memory");
    }

    fn labelFrame(self: *Sim) !void {
        try self.settle();
        var wires: std.ArrayList(label_tail.Wire) = .empty;
        var modelled: std.ArrayList(sql.WireLabel) = .empty;
        for (0..1 + self.random.uintLessThan(usize, 4)) |_| {
            const subject = self.anyDid();
            const uri = if (self.chance(30)) try std.fmt.allocPrint(self.arena, "at://{s}/app.bsky.actor.profile/self", .{subject}) else subject;
            self.label_seq += 1;
            const seq = if (self.chance(20)) 100 + self.random.uintLessThan(u64, self.label_seq - 99) else self.label_seq;
            const now = self.f.now();
            const exp: ?[]const u8 = switch (self.random.uintLessThan(u8, 10)) {
                0...5 => null,
                6 => self.stampOf(now - 1 - self.random.uintLessThan(u64, 5000)),
                7, 8 => self.stampOf(now + 1 + self.random.uintLessThan(u64, 7200)),
                else => self.stampOf(now + 30 * 86_400),
            };
            const one: label_tail.Wire = .{
                .src = label_tail.ADOPTED_SOURCE,
                .uri = uri,
                .val = self.pick(&values),
                .cts = self.stampOf(now - self.random.uintLessThan(u64, 60)),
                .seq = @intCast(seq),
                .cid = if (self.chance(20)) cids[0] else null,
                .neg = if (self.chance(25)) true else null,
                .exp = exp,
            };
            try wires.append(self.arena, one);
            try modelled.append(self.arena, .{ .did = subject, .uri = uri, .val = one.val, .cts = one.cts, .cid = one.cid orelse "", .exp = one.exp orelse "", .neg = one.neg orelse false, .seq = seq });
            if (self.chance(10)) {
                var junk = one;
                junk.cts = "yesterday";
                try wires.append(self.arena, junk);
            }
        }
        self.label_cursor = @max(self.label_cursor, self.label_seq);
        try self.sink.commit(wires.items, @intCast(self.label_seq));
        self.model.labels(modelled.items, self.label_seq);
    }

    fn plcPage(self: *Sim) !void {
        try self.settle();
        const first = self.anyDid();
        const dids = [_][]const u8{ first, self.anyDid(), first, "did:plc:zzzzzzzzzzzzzzzzzzzzzzzz" };
        const cursor = self.stampOf(self.f.now() - self.random.uintLessThan(u64, 3));
        try self.plc.apply(.{ .dids = &dids, .operations = dids.len, .next = cursor });
        self.model.plc(&dids, cursor);
        if (self.plc_cursor == null or std.mem.order(u8, cursor, self.plc_cursor.?) == .gt) self.plc_cursor = cursor;
    }

    fn changeWorld(self: *Sim) !void {
        const index = self.random.uintLessThan(usize, universe);
        const known = &self.world[index];
        switch (self.random.uintLessThan(u8, 6)) {
            0, 1 => known.handle = try std.fmt.allocPrint(self.arena, "user{d}-{d}.example", .{ index, self.random.uintLessThan(u16, 4) }),
            2 => known.handle = if (self.chance(50)) "donald.trump.com" else null,
            3 => known.pds = self.pick(&hosts),
            4 => known.verified = !known.verified,
            else => known.available = !known.available,
        }
        if (self.chance(60)) try self.relay(.{ .identity = self.did(index) });
    }

    /// Stop the owner as a crash would, reopen, and replay the relay from
    /// the stored cursor, or from a little before it as a bootstrap does.
    fn restart(self: *Sim) !void {
        self.running.stop();
        self.restarts += 1;
        self.pruned += self.f.owner.counters.tombstones_pruned.load(.monotonic);
        self.intake.deinit();
        try self.f.restart();
        const stored: i64 = @intCast((try self.f.cursors()).relay orelse 0);
        const from = if (self.chance(40)) @max(0, stored - @as(i64, self.random.uintLessThan(u8, 12))) else stored;
        self.intake = .{ .gpa = testing.allocator, .owner = self.f.owner, .options = .{ .max_changes = 6 } };
        self.intake.resumeFrom(from);
        self.work.owner = self.f.owner;
        self.sink.owner = self.f.owner;
        self.plc.owner = self.f.owner;
        self.running = try harness.Running.start(self.f);
        for (self.relayed.items) |past| {
            if (past.seq <= from) continue;
            self.intake.onEvent(self.wire(past.seq, past.event));
            self.model.ingest(past.event);
        }
    }

    fn next(self: *Sim) !void {
        const subject = self.anyDid();
        switch (self.random.uintLessThan(u8, 100)) {
            0...24 => try self.relay(.{ .sighting = subject }),
            25...39 => try self.relay(.{ .profile = .{
                .did = subject,
                .display_name = self.pick(&names),
                .description = self.pick(&texts),
                .avatar = if (self.chance(40)) self.pick(&cids) else "",
                .banner = if (self.chance(20)) self.pick(&cids) else "",
            } }),
            40...46 => try self.relay(.{ .identity = subject }),
            47...52 => try self.relay(.{ .inactive = .{ .did = subject, .deleted = self.chance(40) } }),
            53...58 => try self.relay(.{ .active = subject }),
            59...68 => try self.labelFrame(),
            69...72 => try self.plcPage(),
            73...82 => try self.drain(),
            83...89 => try self.changeWorld(),
            90...96 => try self.advance(switch (self.random.uintLessThan(u8, 6)) {
                0 => 1,
                1 => 45,
                2 => 301,
                3 => 1000,
                4 => 3700,
                else => 16 * 60,
            }),
            97 => try self.restart(),
            98 => try self.advance(8 * 86_400),
            else => try self.advance(86_400),
        }
    }

    fn fail(self: *Sim, subject: []const u8, comptime what: []const u8, args: anytype) error{Diverged} {
        std.debug.print("step {d}: {s} differs: " ++ what ++ "\n", .{ self.step, subject } ++ args);
        return error.Diverged;
    }

    fn sameText(self: *Sim, subject: []const u8, comptime field: []const u8, stored: []const u8, modelled: []const u8) !void {
        if (!std.mem.eql(u8, stored, modelled)) return self.fail(subject, field ++ " is '{s}' in the store, '{s}' in the model", .{ stored, modelled });
    }

    /// Compare one account. Returns whether the two agree on it being live.
    fn compareOne(self: *Sim, subject: []const u8) !void {
        const stored = try self.f.actor(subject);
        const row = self.model.actors.getPtr(subject);
        const store_live = if (stored) |actor| actor.live() else false;
        const model_live = if (row) |r| r.status == .live else false;
        if (store_live != model_live) return self.fail(subject, "live is {} in the store, {} in the model", .{ store_live, model_live });
        const store_pending = if (stored) |actor| actor.identity_due != 0 else false;
        if (store_pending != self.model.pending(subject)) return self.fail(subject, "identity work pending is {} in the store, {} in the model", .{ store_pending, self.model.pending(subject) });
        if (!store_live) {
            if (self.model.flags.gone_keeps_record and (stored != null) != (row != null)) return self.fail(subject, "a tombstone record is kept in one only", .{});
            return;
        }
        const actor = stored.?;
        const want = row.?;
        try self.sameText(subject, "handle", actor.handle, want.handle);
        try self.sameText(subject, "pds", actor.pds, want.pds);
        try self.sameText(subject, "display name", actor.display_name, want.display_name);
        try self.sameText(subject, "avatar", actor.avatar, want.avatar);
        try self.sameText(subject, "description", actor.description, want.description);
        try self.sameText(subject, "banner", actor.banner, want.banner);
        if (actor.hidden != want.hidden) return self.fail(subject, "hidden is {} in the store, {} in the model", .{ actor.hidden, want.hidden });
        if (actor.identity_resolved_at != want.identity_resolved_at) return self.fail(subject, "resolved at {d} in the store, {d} in the model", .{ actor.identity_resolved_at, want.identity_resolved_at });
        if (actor.last_activity_at != want.last_activity_at) return self.fail(subject, "active at {d} in the store, {d} in the model", .{ actor.last_activity_at, want.last_activity_at });
        if (actor.labels.len != want.labels.len) return self.fail(subject, "{d} labels in the store, {d} in the model", .{ actor.labels.len, want.labels.len });
        for (want.labels) |label| {
            const found = for (actor.labels) |held| {
                if (std.mem.eql(u8, held.uri, label.uri) and std.mem.eql(u8, held.value, label.val) and std.mem.eql(u8, held.created, label.cts) and
                    std.mem.eql(u8, held.cid, label.cid) and std.mem.eql(u8, held.expires, label.exp) and std.mem.eql(u8, held.source, sql.mod_source)) break true;
            } else false;
            if (!found) return self.fail(subject, "label {s} on {s} is in the model only", .{ label.val, label.uri });
        }
    }

    fn compare(self: *Sim) !void {
        try self.settle();
        for (0..universe) |index| try self.compareOne(self.did(index));
        const cursors = try self.f.cursors();
        try testing.expectEqual(@as(u64, self.relayed.items.len), cursors.relay orelse 0);
        try testing.expectEqual(self.model.label_cursor, cursors.labeler);
        try testing.expectEqual(if (self.label_cursor == 0) null else @as(?u64, self.label_cursor), cursors.labeler);
        try testing.expectEqual(self.plc_cursor == null, cursors.plc == null);
        if (self.plc_cursor) |cursor| {
            try testing.expectEqualStrings(cursor, cursors.plc.?);
            try testing.expectEqualStrings(cursor, self.model.plc_cursor.?);
        }
    }
};

const Tally = struct { relayed: usize = 0, restarts: u64 = 0, live: usize = 0, gone: usize = 0, labelled: usize = 0, hidden: usize = 0, handles: usize = 0, pruned: u64 = 0, flushes: u64 = 0 };

fn runSeed(seed: u64, steps: usize) !Tally {
    var prng: std.Random.DefaultPrng = undefined;
    const sim = try Sim.create(.store, seed, &prng);
    defer sim.destroy();
    errdefer std.debug.print("differential test failed: seed {d}, step {d} of {d}\n", .{ seed, sim.step, steps });
    var tally: Tally = .{};
    while (sim.step < steps) : (sim.step += 1) {
        try sim.next();
        if (sim.step % 25 == 24) try sim.compare();
    }
    try sim.compare();
    sim.running.stop();
    tally.relayed = sim.relayed.items.len;
    tally.pruned = sim.pruned + sim.f.owner.counters.tombstones_pruned.load(.monotonic);
    tally.restarts = sim.restarts;
    tally.flushes = sim.f.owner.health().maintenance.?.flushes.count;
    for (0..universe) |index| {
        const actor = try sim.f.actor(sim.did(index)) orelse continue;
        if (!actor.live()) {
            tally.gone += 1;
            continue;
        }
        tally.live += 1;
        tally.labelled += @intFromBool(actor.labels.len > 0);
        tally.hidden += @intFromBool(actor.hidden);
        tally.handles += @intFromBool(actor.handle.len > 0);
    }
    return tally;
}

test "the store role and the SQL model agree on every account, across restarts and replays" {
    if (std.c.getenv("STORE_DIFF_SEED")) |text| {
        _ = try runSeed(try std.fmt.parseInt(u64, std.mem.span(text), 10), 1500);
        return;
    }
    var total: Tally = .{};
    for ([_]u64{ 1, 20261110, 0xfeed }) |seed| {
        const tally = try runSeed(seed, 700);
        inline for (comptime std.meta.fieldNames(Tally)) |name| @field(total, name) += @field(tally, name);
    }
    // The sequences must have exercised what the comparison is about.
    errdefer std.debug.print("differential coverage: {any}\n", .{total});
    try testing.expect(total.relayed > 800);
    try testing.expect(total.live > 20 and total.gone > 0 and total.handles > 5);
    try testing.expect(total.labelled > 3 and total.hidden > 0);
    try testing.expect(total.pruned > 0 and total.restarts > 3);
    try testing.expect(total.flushes > 0);
}

// -- what each divergence changes, against production's semantics --

const Pair = struct {
    sim: *Sim,
    production: sql.Model,
    prng: std.Random.DefaultPrng = undefined,

    fn create() !*Pair {
        const self = try testing.allocator.create(Pair);
        errdefer testing.allocator.destroy(self);
        self.sim = try Sim.create(.store, 7, &self.prng);
        self.production = .{ .arena = self.sim.arena, .flags = .production, .now = self.sim.f.now(), .parse_ms = store_mod.time.parseMs };
        return self;
    }

    fn destroy(self: *Pair) void {
        self.sim.destroy();
        testing.allocator.destroy(self);
    }

    fn relay(self: *Pair, event: sql.Event) !void {
        try self.sim.relay(event);
        self.production.ingest(event);
    }

    fn advance(self: *Pair, seconds: u64) !void {
        try self.sim.advance(seconds);
        self.production.now = self.sim.f.now();
        self.production.prune();
    }

    fn label(self: *Pair, subject: []const u8, val: []const u8, seq: u64, exp: []const u8) !void {
        try self.sim.settle();
        const cts = self.sim.stampOf(self.sim.f.now());
        try self.sim.sink.commit(&.{.{ .src = label_tail.ADOPTED_SOURCE, .uri = subject, .val = val, .cts = cts, .seq = @intCast(seq), .exp = if (exp.len > 0) exp else null }}, @intCast(seq));
        const wire: sql.WireLabel = .{ .did = subject, .uri = subject, .val = val, .cts = cts, .exp = exp, .seq = seq };
        self.sim.model.labels(&.{wire}, seq);
        self.production.labels(&.{wire}, seq);
    }

    /// Resolve everything due, on all three, to `handle`.
    fn resolve(self: *Pair, handle: []const u8) !void {
        try self.sim.settle();
        var jobs: [identity.LEASE_BATCH]identity.Job = undefined;
        const leased = try self.sim.work.lease(&jobs);
        const outcome: identity.Outcome = .{ .resolved = .{ .handle = handle, .handle_verified = true, .pds = hosts[0] } };
        const now = self.sim.f.now();
        for (jobs[0..leased]) |job| try self.sim.work.report(job, .{ .observed_at = @intCast(now), .outcome = outcome });
        for ([_]*sql.Model{ &self.sim.model, &self.production }) |model| {
            for (model.lease(identity.LEASE_BATCH)) |job| model.settle(.{ .did = job.did, .gen = job.gen, .observed_at = now, .pds = hosts[0], .outcome = .{ .resolved = .{ .handle = handle, .verified = true } } });
        }
    }

    /// The store agrees with the model that carries the store's semantics.
    fn agree(self: *Pair, subject: []const u8) !store_mod.Actor {
        try self.sim.settle();
        try self.sim.compareOne(subject);
        return (try self.sim.f.actor(subject)).?;
    }
};

const eve = "did:plc:2223aaaaaaaaaaaaaaaaaaaa";

test "divergence gone_keeps_record: a stray event does not bring a gone account back, and reactivation restores it whole" {
    const pair = try Pair.create();
    defer pair.destroy();
    try pair.relay(.{ .sighting = eve });
    try pair.resolve("eve.example");
    try pair.relay(.{ .inactive = .{ .did = eve, .deleted = false } });
    try pair.relay(.{ .sighting = eve });

    try testing.expect(!(try pair.agree(eve)).live());
    const recreated = pair.production.actors.get(eve).?;
    try testing.expectEqualStrings("", recreated.handle);
    try testing.expect(pair.production.pending(eve));

    try pair.relay(.{ .active = eve });
    try testing.expectEqualStrings("eve.example", (try pair.agree(eve)).handle);
    try testing.expectEqualStrings("", pair.production.actors.get(eve).?.handle);
    try pair.resolve("eve.example");
    try testing.expectEqualStrings("eve.example", (try pair.agree(eve)).handle);
    try testing.expectEqualStrings("eve.example", pair.production.actors.get(eve).?.handle);
}

test "divergence gone_drops_identity_work: a gone account's queue entry goes with it" {
    const pair = try Pair.create();
    defer pair.destroy();
    try pair.relay(.{ .identity = eve });
    try pair.relay(.{ .inactive = .{ .did = eve, .deleted = true } });
    const gone = try pair.agree(eve);
    try testing.expectEqual(@as(u64, 0), gone.identity_due);
    try testing.expect(pair.production.pending(eve));
    try testing.expect(pair.production.actors.get(eve) == null);
}

test "divergence prune_forgets_labels: a label does not outlive the pruned account it was on" {
    const pair = try Pair.create();
    defer pair.destroy();
    try pair.relay(.{ .sighting = eve });
    try pair.label(eve, "spam", 5, "");
    try testing.expect((try pair.agree(eve)).hidden);
    try pair.relay(.{ .inactive = .{ .did = eve, .deleted = true } });
    try pair.advance(8 * 86_400);
    try testing.expect(try pair.sim.f.actor(eve) == null);

    try pair.relay(.{ .sighting = eve });
    try pair.label(eve, "porn", 6, "");
    const back = try pair.agree(eve);
    try testing.expect(!back.hidden);
    try testing.expectEqual(@as(usize, 1), back.labels.len);
    const row = pair.production.actors.get(eve).?;
    try testing.expectEqual(@as(usize, 2), row.labels.len);
    try testing.expect(row.hidden);
}

test "divergence hidden flags: the stored flag follows a lapsed label here, and only at query time in production" {
    const pair = try Pair.create();
    defer pair.destroy();
    try pair.relay(.{ .sighting = eve });
    try pair.label(eve, "!hide", 5, pair.sim.stampOf(pair.sim.f.now() + 3600));
    try testing.expect((try pair.agree(eve)).hidden);
    try testing.expect(pair.production.actors.get(eve).?.hidden);

    try pair.advance(3601);
    try testing.expect(!(try pair.agree(eve)).hidden);
    const row = pair.production.actors.getPtr(eve).?;
    try testing.expect(row.hidden);
    try testing.expect(!pair.production.resolveHidden(row));

    try pair.relay(.{ .profile = .{ .did = eve, .display_name = "Eve" } });
    try testing.expect(!(try pair.agree(eve)).hidden);
    try testing.expect(pair.production.actors.get(eve).?.hidden);
}

fn slur(handle: []const u8) bool {
    return std.mem.indexOf(u8, handle, "slur") != null;
}

test "divergence slur_rule_left_to_index: a slur handle is not hidden in the stored flag" {
    const pair = try Pair.create();
    defer pair.destroy();
    pair.production.flagged = slur;
    pair.sim.model.flagged = slur;
    try pair.relay(.{ .sighting = eve });
    try pair.resolve("slur.example");
    try pair.label(eve, "porn", 5, "");
    try testing.expect(!(try pair.agree(eve)).hidden);
    try testing.expect(pair.production.actors.get(eve).?.hidden);
}

test "every difference on the store's review list is a flag or an explained omission" {
    try testing.expectEqual(@as(usize, 6), std.meta.fieldNames(sql.Divergence).len);
    try testing.expectEqual(@as(usize, 11), sql.unmodelled.len);
    inline for (comptime std.meta.fieldNames(sql.Divergence)) |name| {
        try testing.expect(@field(sql.Divergence.store, name));
        try testing.expect(!@field(sql.Divergence.production, name));
    }
}
