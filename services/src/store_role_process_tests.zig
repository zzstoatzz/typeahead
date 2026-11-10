//! Restart test for the store role, with real SIGKILLs. Run with no
//! arguments this is the parent: it spawns itself as the role (the owner on
//! its own thread, the relay intake, the label sink, the PLC tail and the
//! identity work, all fed from fakes), kills it between and during commits,
//! and lets each new child resume from the cursors in the store. `zig build
//! test` runs it.
//!
//! The input is a pure function of a round number, the clock is set per
//! round, and every producer resumes from its own stored cursor, so a store
//! that was killed many times must end equal to one that never was: the
//! parent runs the same rounds once more, uninterrupted, into a second
//! store and compares every account. A lost effect, a duplicated one, or a
//! cursor that passed uncommitted data shows up as a difference. Identity
//! resolution is the one thing whose timing a kill changes (a lease taken
//! before a kill lapses instead of completing), so both stores drain their
//! queues at the end and must then agree on handles too.

const std = @import("std");
const zat = @import("zat");
const store_mod = @import("store/root.zig");
const store_harness = @import("store/testing/harness.zig");
const identity = @import("identity.zig");
const label_tail = @import("label_tail.zig");
const identity_work = @import("store_process/identity_work.zig");
const intake_mod = @import("store_process/intake.zig");
const owner_mod = @import("store_process/owner.zig");
const tails = @import("store_process/tails.zig");
const harness = @import("store_process/testing.zig");
const Io = std.Io;
const Owner = owner_mod.Owner;

const universe = 40;
const events_per_round = 30;
const seq_stride = 100;
const round_seconds = 40;
const stamp = "2026-11-01T00:00:00.000Z";

pub fn main(init: std.process.Init) !void {
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    if (args.len >= 4 and std.mem.eql(u8, args[1], "child")) {
        return child(init, args[2], try std.fmt.parseInt(u64, args[3], 10), if (args.len > 4) try std.fmt.parseInt(u64, args[4], 10) else null);
    }
    const seed = if (init.environ_map.get("STORE_TEST_SEED")) |text| try std.fmt.parseInt(u64, text, 10) else 20261110;
    var random_bytes: [8]u8 = undefined;
    init.io.random(&random_bytes);
    var name_buf: [64]u8 = undefined;
    const scratch = try std.fmt.bufPrint(&name_buf, ".zig-cache/tmp/store-role-{x}", .{std.mem.readInt(u64, &random_bytes, .little)});
    const cwd = Io.Dir.cwd();
    try cwd.createDirPath(init.io, scratch);
    defer cwd.deleteTree(init.io, scratch) catch {};
    const parent: Parent = .{
        .gpa = init.gpa,
        .io = init.io,
        .scratch = scratch,
        .self_path = try std.process.executablePathAlloc(init.io, init.arena.allocator()),
        .seed = seed,
    };
    try parent.run(std.mem.readInt(u64, &random_bytes, .little));
}

fn mark(io: Io, comptime fmt: []const u8, args: anytype) void {
    var buf: [48]u8 = undefined;
    const line = std.fmt.bufPrint(&buf, fmt ++ "\n", args) catch unreachable;
    Io.File.stdout().writeStreamingAll(io, line) catch std.process.exit(3);
}

const Tracer = struct {
    io: Io,

    fn commit(context: ?*anyopaque, stage: owner_mod.Stage, edge: store_mod.maintain.Edge) void {
        const self: *Tracer = @ptrCast(@alignCast(context.?));
        if (stage == .commit) mark(self.io, "{c}", .{@as(u8, if (edge == .begin) 'C' else 'A')});
    }

    fn maintenance(context: ?*anyopaque, phase: store_mod.maintain.Phase, edge: store_mod.maintain.Edge) void {
        const self: *Tracer = @ptrCast(@alignCast(context.?));
        if (phase == .flush or phase == .compact) mark(self.io, "{c}", .{@as(u8, if (edge == .begin) 'M' else 'm')});
    }
};

fn didOf(arena: std.mem.Allocator, index: usize) ![]const u8 {
    const text = try arena.dupe(u8, "did:plc:2222aaaaaaaaaaaaaaaaaaaa");
    var left = index;
    for (0..4) |place| {
        text["did:plc:".len + 3 - place] = '2' + @as(u8, @intCast(left % 6));
        left /= 6;
    }
    return text;
}

fn handleOf(arena: std.mem.Allocator, did: []const u8) ![]const u8 {
    return std.fmt.allocPrint(arena, "user-{s}.example", .{did["did:plc:".len..][0..4]});
}

/// The role, fed from fakes. The same code is the killed child, the child
/// that finishes, and the uninterrupted reference.
const Role = struct {
    gpa: std.mem.Allocator,
    io: Io,
    seed: u64,
    clock: harness.FakeClock = .{},
    owner: *Owner,
    thread: std.Thread,
    intake: intake_mod.Intake,
    sink: tails.LabelSink,
    plc: tails.PlcTail,
    work: identity_work.Work,

    fn start(self: *Role, gpa: std.mem.Allocator, io: Io, path: []const u8, seed: u64, tracer: ?*Tracer) !void {
        self.* = .{ .gpa = gpa, .io = io, .seed = seed, .owner = undefined, .thread = undefined, .intake = undefined, .sink = undefined, .plc = undefined, .work = undefined };
        self.owner = try Owner.open(gpa, io, Io.Dir.cwd(), path, .{
            .store = .{
                .layout = store_harness.small_layout,
                .maintenance = store_harness.busy_policy,
                .trace = if (tracer) |t| .{ .context = t, .call = Tracer.maintenance } else null,
            },
            .clock = self.clock.clock(),
            .trace = if (tracer) |t| .{ .context = t, .call = Tracer.commit } else null,
            .idle_wait_ms = 1,
        });
        self.intake = .{ .gpa = gpa, .owner = self.owner, .options = .{ .max_changes = 7, .max_age_seconds = 1_000_000 } };
        self.sink = .{ .gpa = gpa, .owner = self.owner };
        self.plc = .{ .gpa = gpa, .io = io, .owner = self.owner };
        self.work = .{ .gpa = gpa, .io = io, .owner = self.owner, .resolver = undefined };
        self.thread = try std.Thread.spawn(.{}, Owner.run, .{self.owner});
    }

    fn stop(self: *Role) void {
        self.owner.stop();
        self.thread.join();
        self.intake.deinit();
        self.owner.close();
    }

    fn setRound(self: *Role, n: u64) void {
        self.clock.at.store(harness.t0 + n * round_seconds, .monotonic);
    }

    fn barrier(self: *Role) !void {
        const submission = try owner_mod.Submission.create(self.gpa);
        defer submission.destroy();
        try self.owner.call(submission);
    }

    const Position = struct { relay: u64, labeler: u64, plc: u64 };

    /// How far each producer got, in rounds and relay seqs, from the store.
    fn position(self: *Role, arena: std.mem.Allocator) !Position {
        const store = try self.owner.acquire();
        defer self.owner.release();
        const cursors = try store.cursors(arena);
        const plc_at: u64 = if (cursors.plc) |text| @intCast(@divFloor(store_mod.time.parseMs(text).?, 1000)) else harness.t0;
        return .{ .relay = cursors.relay orelse seq_stride - 1, .labeler = cursors.labeler orelse 0, .plc = plc_at - harness.t0 };
    }

    fn relayEvent(arena: std.mem.Allocator, random: std.Random, seq: i64) !zat.FirehoseEvent {
        const did = try didOf(arena, random.uintLessThan(usize, universe));
        return switch (random.uintLessThan(u8, 20)) {
            0...8 => .{ .commit = .{ .seq = seq, .repo = did, .rev = "", .time = stamp, .ops = &.{
                .{ .action = .create, .collection = "app.bsky.feed.post", .rkey = "3k" },
            } } },
            9...13 => blk: {
                const fields = try arena.dupe(zat.cbor.Value.MapEntry, &.{
                    .{ .key = "displayName", .value = .{ .text = try std.fmt.allocPrint(arena, "name {d}", .{random.uintLessThan(u16, 50)}) } },
                    .{ .key = "description", .value = .{ .text = if (random.boolean()) "about" else "" } },
                });
                const ops = try arena.dupe(zat.firehose.RepoOp, &.{.{ .action = .update, .collection = "app.bsky.actor.profile", .rkey = "self", .record = .{ .map = fields } }});
                break :blk .{ .commit = .{ .seq = seq, .repo = did, .rev = "", .time = stamp, .ops = ops } };
            },
            14, 15 => .{ .identity = .{ .seq = seq, .did = did, .time = stamp } },
            16, 17 => .{ .account = .{ .seq = seq, .did = did, .time = stamp, .active = false, .status = if (random.boolean()) .deleted else .deactivated } },
            else => .{ .account = .{ .seq = seq, .did = did, .time = stamp, .active = true } },
        };
    }

    /// One round, skipping whatever the store already holds of it.
    fn round(self: *Role, n: u64) !void {
        var arena_state: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena_state.deinit();
        const arena = arena_state.allocator();
        const at = try self.position(arena);
        self.setRound(n);

        var prng: std.Random.DefaultPrng = .init(std.hash.Wyhash.hash(self.seed, std.mem.asBytes(&n)));
        const random = prng.random();
        self.intake.resumeFrom(@intCast(at.relay));
        for (0..events_per_round) |index| {
            const seq = n * seq_stride + index;
            const event = try relayEvent(arena, random, @intCast(seq));
            if (seq > at.relay) self.intake.onEvent(event);
        }
        try self.intake.flush();
        try self.barrier();

        var wires: [3]label_tail.Wire = undefined;
        for (&wires) |*wire| wire.* = .{
            .src = label_tail.ADOPTED_SOURCE,
            .uri = try didOf(arena, random.uintLessThan(usize, universe)),
            .val = if (random.boolean()) "spam" else "porn",
            .cts = stamp,
            .seq = @intCast(n),
            .neg = if (random.uintLessThan(u8, 4) == 0) true else null,
        };
        if (at.labeler < n) try self.sink.commit(&wires, @intCast(n));

        const dids = [_][]const u8{ try didOf(arena, random.uintLessThan(usize, universe)), try didOf(arena, random.uintLessThan(usize, universe)) };
        var buf: [24]u8 = undefined;
        if (at.plc < n) try self.plc.apply(.{ .dids = &dids, .operations = dids.len, .next = tails.isoSeconds(&buf, harness.t0 + n) });

        _ = try self.resolveDue(arena);
    }

    /// Lease what is due and resolve it: every account's handle is a
    /// function of its DID, verified on the first attempt.
    fn resolveDue(self: *Role, arena: std.mem.Allocator) !usize {
        var jobs: [identity.LEASE_BATCH]identity.Job = undefined;
        const leased = try self.work.lease(&jobs);
        for (jobs[0..leased]) |job| {
            const lookup: identity.Lookup = .{ .handle = try handleOf(arena, job.did.slice()), .pds = "https://pds.example", .verified = true };
            try self.work.report(job, .{ .observed_at = @intCast(self.owner.now()), .outcome = identity.decide(lookup, job.attempts) });
        }
        return leased;
    }

    /// The round the store has completed for every producer.
    fn completed(self: *Role) !u64 {
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        const at = try self.position(arena.allocator());
        const relay_round = (at.relay + 1) / seq_stride - @intFromBool((at.relay + 1) % seq_stride < events_per_round);
        return @min(relay_round, @min(at.labeler, at.plc));
    }

    /// Run rounds up to `last`, then resolve until the queue stays empty.
    fn finish(self: *Role, last: u64) !void {
        var n = try self.completed() + 1;
        while (n <= last) : (n += 1) try self.round(n);
        var arena: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena.deinit();
        var quiet: usize = 0;
        var extra: u64 = 1;
        while (quiet < 3) : (extra += 1) {
            self.clock.at.store(harness.t0 + last * round_seconds + extra * 400, .monotonic);
            quiet = if (try self.resolveDue(arena.allocator()) == 0) quiet + 1 else 0;
        }
        try self.barrier();
    }
};

/// `last` null: run rounds forever, until killed. Otherwise finish at it.
fn child(init: std.process.Init, path: []const u8, seed: u64, last: ?u64) !void {
    var tracer: Tracer = .{ .io = init.io };
    var role: Role = undefined;
    try role.start(init.gpa, init.io, path, seed, if (last == null) &tracer else null);
    defer role.stop();
    if (last) |round| return role.finish(round);
    var n = try role.completed() + 1;
    while (true) : (n += 1) {
        mark(init.io, "R {d}", .{n});
        try role.round(n);
    }
}

const Landed = enum { commit, between, maintenance };

const Parent = struct {
    gpa: std.mem.Allocator,
    io: Io,
    scratch: []const u8,
    self_path: []const u8,
    seed: u64,

    fn fail(comptime fmt: []const u8, args: anytype) error{RoleProcessTestFailed} {
        std.debug.print("store role process test failed: " ++ fmt ++ "\n", args);
        return error.RoleProcessTestFailed;
    }

    const Kill = struct { landed: Landed, round: u64 };

    /// Start the role, wait for its `occurrence`-th commit (or the end of
    /// one), wait `delay_us` more, and SIGKILL it.
    fn killOnce(self: Parent, path: []const u8, during: bool, occurrence: u32, delay_us: u32) !Kill {
        var seed_buf: [24]u8 = undefined;
        var process = try std.process.spawn(self.io, .{
            .argv = &.{ self.self_path, "child", path, try std.fmt.bufPrint(&seed_buf, "{d}", .{self.seed}) },
            .stdout = .pipe,
        });
        const pipe = process.stdout.?;
        var result: Kill = .{ .landed = .between, .round = 0 };
        var buf: [4096]u8 = undefined;
        var held: usize = 0;
        var seen: u32 = 0;
        var lines: u32 = 0;
        var killed = false;
        while (true) {
            var slices = [_][]u8{buf[held..]};
            const got = pipe.readStreaming(self.io, &slices) catch |err| switch (err) {
                error.EndOfStream => 0,
                else => return err,
            };
            if (got == 0) break;
            held += got;
            var start: usize = 0;
            while (std.mem.indexOfScalarPos(u8, buf[0..held], start, '\n')) |end| : (start = end + 1) {
                const line = buf[start..end];
                lines += 1;
                if (killed) continue;
                switch (line[0]) {
                    'R' => result.round = try std.fmt.parseInt(u64, line[2..], 10),
                    'C' => {
                        result.landed = .commit;
                        seen += @intFromBool(during);
                    },
                    'A' => {
                        result.landed = .between;
                        seen += @intFromBool(!during);
                    },
                    'M' => result.landed = .maintenance,
                    'm' => result.landed = .between,
                    else => return fail("the child wrote {s}", .{line}),
                }
                if (seen >= occurrence or lines > 6000) {
                    if (delay_us > 0) try self.io.sleep(.fromMicroseconds(delay_us), .awake);
                    try std.posix.kill(process.id.?, .KILL);
                    killed = true;
                }
            }
            std.mem.copyForwards(u8, &buf, buf[start..held]);
            held -= start;
        }
        const term = try process.wait(self.io);
        if (!killed or term != .signal or term.signal != .KILL) return fail("the child ended on its own: {f}", .{term});
        return result;
    }

    fn finishChild(self: Parent, path: []const u8, last: u64) !void {
        var seed_buf: [24]u8 = undefined;
        var last_buf: [24]u8 = undefined;
        var process = try std.process.spawn(self.io, .{ .argv = &.{
            self.self_path, "child", path, try std.fmt.bufPrint(&seed_buf, "{d}", .{self.seed}), try std.fmt.bufPrint(&last_buf, "{d}", .{last}),
        } });
        const term = try process.wait(self.io);
        if (term != .exited or term.exited != 0) return fail("finishing {s} to round {d} ended with {f}", .{ path, last, term });
    }

    fn run(self: Parent, entropy: u64) !void {
        var killed_buf: [128]u8 = undefined;
        var reference_buf: [128]u8 = undefined;
        const killed = try std.fmt.bufPrint(&killed_buf, "{s}/killed", .{self.scratch});
        const reference = try std.fmt.bufPrint(&reference_buf, "{s}/reference", .{self.scratch});
        var prng: std.Random.DefaultPrng = .init(entropy);
        const random = prng.random();

        var landed: std.EnumArray(Landed, u32) = .initFill(0);
        var kills: u32 = 0;
        var last_round: u64 = 0;
        while (kills < 120) : (kills += 1) {
            if (kills >= 30 and landed.get(.commit) >= 8 and landed.get(.between) >= 8) break;
            const during = kills % 2 == 0;
            const kill = try self.killOnce(killed, during, 1 + random.uintLessThan(u32, 25), if (during) random.uintLessThan(u32, 300) else random.uintLessThan(u32, 3000));
            landed.getPtr(kill.landed).* += 1;
            last_round = @max(last_round, kill.round);
        }
        if (landed.get(.commit) < 8 or landed.get(.between) < 8) return fail("kills landed in commit {d} times and between commits {d} times; 8 of each are needed", .{ landed.get(.commit), landed.get(.between) });

        const last = last_round + 3;
        try self.finishChild(killed, last);
        try self.finishChild(reference, last);
        const compared = try self.compare(killed, reference);
        std.debug.print("store role kill test: seed {d}, {d} kills (in commit {d}, between commits {d}, in maintenance {d}), {d} rounds, {d} relay events; {d} accounts equal to an uninterrupted run ({d} live, {d} gone, {d} labelled), {d} change-log entries, identity queue empty\n", .{
            self.seed, kills, landed.get(.commit), landed.get(.between), landed.get(.maintenance), last, last * events_per_round, compared.actors, compared.live, compared.gone, compared.labelled, compared.changes,
        });
    }

    const Compared = struct { actors: usize = 0, live: usize = 0, gone: usize = 0, labelled: usize = 0, changes: u64 = 0 };

    fn compare(self: Parent, killed_path: []const u8, reference_path: []const u8) !Compared {
        const options: store_mod.Options = .{ .layout = store_harness.small_layout, .maintenance = store_harness.busy_policy };
        const killed = try store_mod.Store.open(self.gpa, self.io, Io.Dir.cwd(), killed_path, options);
        defer killed.close();
        const reference = try store_mod.Store.open(self.gpa, self.io, Io.Dir.cwd(), reference_path, options);
        defer reference.close();
        var arena_state: std.heap.ArenaAllocator = .init(self.gpa);
        defer arena_state.deinit();
        const arena = arena_state.allocator();
        var out: Compared = .{};

        const a = try killed.cursors(arena);
        const b = try reference.cursors(arena);
        if (!std.meta.eql(a.relay, b.relay) or !std.meta.eql(a.labeler, b.labeler) or !std.mem.eql(u8, a.plc.?, b.plc.?)) {
            return fail("cursors differ: relay {?d}/{?d}, labeler {?d}/{?d}, plc {s}/{s}", .{ a.relay, b.relay, a.labeler, b.labeler, a.plc.?, b.plc.? });
        }

        const dids = try reference.walk(arena, "", universe + 1);
        const held = try killed.walk(arena, "", universe + 1);
        if (dids.len != held.len) return fail("the killed store holds {d} accounts, the reference {d}", .{ held.len, dids.len });
        var with_handle: u64 = 0;
        var queued: u64 = 0;
        for (dids, held) |did, other| {
            if (!std.mem.eql(u8, did, other)) return fail("the stores hold different accounts: {s} and {s}", .{ other, did });
            const got = (try killed.actor(arena, did)).?;
            const want = (try reference.actor(arena, did)).?;
            out.actors += 1;
            if (got.status != want.status or got.deleted_at != want.deleted_at) return fail("{s}: status {t} at {d}, reference {t} at {d}", .{ did, got.status, got.deleted_at, want.status, want.deleted_at });
            inline for (.{ "display_name", "avatar", "description", "banner" }) |name| {
                if (!std.mem.eql(u8, @field(got, name), @field(want, name))) return fail("{s}: " ++ name ++ " is '{s}', reference '{s}'", .{ did, @field(got, name), @field(want, name) });
            }
            // What a gone account resolved to before it went depends on when
            // a kill let its lease lapse; a live one has been resolved since.
            if (got.live() and (!std.mem.eql(u8, got.handle, want.handle) or !std.mem.eql(u8, got.pds, want.pds))) {
                return fail("{s}: handle '{s}' on {s}, reference '{s}' on {s}", .{ did, got.handle, got.pds, want.handle, want.pds });
            }
            if (got.hidden != want.hidden) return fail("{s}: hidden {}, reference {}", .{ did, got.hidden, want.hidden });
            if (got.last_activity_at != want.last_activity_at) return fail("{s}: active at {d}, reference {d}", .{ did, got.last_activity_at, want.last_activity_at });
            if (got.labels.len != want.labels.len) return fail("{s}: {d} labels, reference {d}", .{ did, got.labels.len, want.labels.len });
            for (got.labels, want.labels) |x, y| if (!x.eql(y)) return fail("{s}: label {s} differs from the reference", .{ did, x.value });
            if (got.identity_due != 0) return fail("{s}: identity work is still queued", .{did});
            if (got.live()) {
                out.live += 1;
                if (!std.mem.eql(u8, got.handle, try handleOf(arena, did))) return fail("{s}: resolved to '{s}'", .{ did, got.handle });
                with_handle += 1;
            } else out.gone += 1;
            out.labelled += @intFromBool(got.labels.len > 0);
            queued += @intFromBool(got.identity_due != 0);
        }

        // The derived ranges of the killed store agree with its records.
        const counts = try killed.counts();
        if (counts.get(.actor) != out.actors or counts.get(.handle) != with_handle or counts.get(.tombstone) != out.gone or counts.get(.identity) != queued) {
            return fail("ranges disagree with the records: {d} actors, {d} handles, {d} tombstones, {d} queued", .{ counts.get(.actor), counts.get(.handle), counts.get(.tombstone), counts.get(.identity) });
        }
        var from = a.change_floor;
        while (true) {
            const page = (try killed.changes(arena, from, 500)).entries;
            for (page) |entry| {
                if (entry.sequence != @max(from, 1)) return fail("the change log has a gap at {d}", .{from});
                from = entry.sequence + 1;
                out.changes += 1;
            }
            if (page.len < 500) break;
        }
        if (@max(from, 1) != a.next_change) return fail("the change log ends at {d}, the next change is {d}", .{ from, a.next_change });
        return out;
    }
};
