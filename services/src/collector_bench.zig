//! CPU cost of the store role's relay path, per 1,000 relay events.
//!
//!   zig build bench-collector -Doptimize=ReleaseSafe -- [--events 400000] [--actors 1000000] ...
//!
//! A deterministic stream of `com.atproto.sync.subscribeRepos` frames, built
//! with zat's own encoders (a CAR of commit, tree and record blocks inside a
//! `#commit` frame), goes through what the role does with relay bytes: the
//! TLS record decryption the relay's cipher suite needs, zat's frame decode,
//! `Intake`, `events.Changes`, and the `Owner` committing to a zkv store
//! that already holds `--actors` synthetic actors. The owner is stepped on
//! this thread after every batch intake hands over, under a clock that
//! advances `1 / --rate` seconds per event, so a run is reproducible.
//!
//! The store is built once per (`--actors`, `--active`, `--skew`, `--seed`)
//! under `--dir` and copied for each run. Its actors carry last-activity
//! stamps in the steady state the event mix implies: an actor heard at rate
//! r is stamped once every 3600 + 1/r seconds, so each starts at a uniform
//! point of that cycle.
//!
//! Every share is a flag, in events per thousand; the defaults are the
//! production slot's own counters on 2026-11-12 (docs/evals/collector-cpu-20261112).
//!
//! CPU is `getrusage` user + system of this process. `decode` is measured in
//! a pass of its own over the same frames; `intake` is the relay pass less
//! that and less the owner's turns.

const std = @import("std");
const builtin = @import("builtin");
const zat = @import("zat");
const store_mod = @import("store/root.zig");
const owner_mod = @import("store_process/owner.zig");
const intake_mod = @import("store_process/intake.zig");
const bridge = @import("bridge/root.zig");
const Io = std.Io;
const Allocator = std.mem.Allocator;
const Sha256 = std.crypto.hash.sha2.Sha256;
const Aes256Gcm = std.crypto.aead.aes_gcm.Aes256Gcm;

pub const std_options: std.Options = .{ .log_level = .warn };

const Config = struct {
    actors: u64 = 1_000_000,
    events: u64 = 400_000,
    seed: u64 = 20261112,
    /// Relay events per simulated second.
    rate: u64 = 480,
    /// Actors that are ever heard from; the rest of the corpus is dormant.
    active: u64 = 150_000,
    /// An event's actor is `active * u^skew` for uniform u.
    skew: f64 = 2.0,
    /// Commits to collections the store does not want (reposts, blocks, ...).
    unwanted: u64 = 130,
    /// Deletes in wanted collections.
    deletes: u64 = 40,
    /// Profile record updates from known actors.
    profiles: u64 = 7,
    /// Commits from actors the store has not seen.
    unknown: u64 = 1,
    identity: u64 = 1,
    account: u64 = 1,
    /// Tree blocks beside the commit and record blocks, and their mean size.
    tree_blocks: u64 = 3,
    tree_block_bytes: u64 = 450,
    /// 1 decrypts every frame as one AES-256-GCM record, the suite
    /// bsky.network picks from what Zig's TLS client offers.
    tls: u64 = 1,
    /// Enrichment results committed per simulated second, as a stand-in for
    /// the role's other writer. 0 leaves the relay alone with the store.
    enrich: u64 = 0,
    dir: []const u8 = ".zig-cache/collector-bench",

    fn parse(args: []const []const u8) !Config {
        var config: Config = .{};
        var i: usize = 1;
        while (i + 1 < args.len) : (i += 2) {
            const name = args[i];
            if (!std.mem.startsWith(u8, name, "--")) return error.BadArgument;
            const info = @typeInfo(Config).@"struct";
            const matched = inline for (info.field_names, info.field_types) |field, Field| {
                if (std.mem.eql(u8, name[2..], field)) {
                    @field(config, field) = switch (Field) {
                        u64 => try std.fmt.parseInt(u64, args[i + 1], 10),
                        f64 => try std.fmt.parseFloat(f64, args[i + 1]),
                        []const u8 => args[i + 1],
                        else => comptime unreachable,
                    };
                    break true;
                }
            } else false;
            if (!matched) return error.BadArgument;
        }
        if (i != args.len) return error.BadArgument;
        if (config.active == 0 or config.active > config.actors) return error.BadArgument;
        return config;
    }

    /// Share of events that are a sighting of a known actor.
    fn sightings(self: Config) f64 {
        const rest = self.unwanted + self.deletes + self.unknown;
        return @as(f64, @floatFromInt(1000 - @min(rest, 1000))) / 1000.0;
    }
};

const t0: u64 = 1_794_470_400;

fn cpuSeconds() f64 {
    const usage = std.posix.getrusage(std.posix.rusage.SELF);
    const user = @as(f64, @floatFromInt(usage.utime.sec)) + @as(f64, @floatFromInt(usage.utime.usec)) / 1e6;
    const system = @as(f64, @floatFromInt(usage.stime.sec)) + @as(f64, @floatFromInt(usage.stime.usec)) / 1e6;
    return user + system;
}

fn peakRssBytes() u64 {
    const usage = std.posix.getrusage(std.posix.rusage.SELF);
    const raw: u64 = @intCast(usage.maxrss);
    return if (builtin.os.tag.isDarwin()) raw else raw * 1024;
}

const SimClock = struct {
    micros: u64 = 0,

    fn seconds(context: ?*anyopaque, _: Io) u64 {
        const self: *SimClock = @ptrCast(@alignCast(context.?));
        return t0 + self.micros / 1_000_000;
    }
};

// -- synthetic corpus --

const base32 = "abcdefghijklmnopqrstuvwxyz234567";
const words = [_][]const u8{ "quiet", "harbor", "signal", "paper", "orbit", "maple", "tidal", "ember", "lantern", "north", "field", "copper", "willow", "static", "meadow", "cinder" };

fn mix(seed: u64, index: u64, salt: u64) std.Random.SplitMix64 {
    return .init(seed ^ (index *% 0x9E3779B97F4A7C15) ^ (salt *% 0xC2B2AE3D27D4EB4F));
}

const Did = [32]u8;

fn didOf(seed: u64, index: u64) Did {
    var out: Did = undefined;
    var random = mix(seed, index, 1);
    @memcpy(out[0..8], "did:plc:");
    for ([_]u64{ random.next(), random.next() }, 0..) |bits, half| {
        for (0..12) |i| out[8 + half * 12 + i] = base32[@intCast((bits >> @intCast(5 * i)) & 31)];
    }
    return out;
}

fn cidText(buf: *[59]u8, seed: u64, index: u64, salt: u64) []const u8 {
    var random = mix(seed, index, salt);
    @memcpy(buf[0..7], "bafkrei");
    var at: usize = 7;
    while (at < buf.len) {
        const bits = random.next();
        for (0..12) |i| {
            if (at == buf.len) break;
            buf[at] = base32[@intCast((bits >> @intCast(5 * i)) & 31)];
            at += 1;
        }
    }
    return buf;
}

fn sentence(buf: []u8, random: *std.Random.SplitMix64, count: usize) []const u8 {
    var w: Io.Writer = .fixed(buf);
    for (0..count) |i| {
        if (i > 0) w.writeByte(' ') catch break;
        w.writeAll(words[@intCast(random.next() % words.len)]) catch break;
    }
    return w.buffered();
}

/// The chance one sighting names active actor `index`.
fn pickShare(config: Config, index: u64) f64 {
    const a: f64 = @floatFromInt(config.active);
    const lo = @as(f64, @floatFromInt(index)) / a;
    const hi = @as(f64, @floatFromInt(index + 1)) / a;
    return std.math.pow(f64, hi, 1.0 / config.skew) - std.math.pow(f64, lo, 1.0 / config.skew);
}

/// Seconds before the run that actor `index` was last stamped.
fn stampAge(config: Config, index: u64) f64 {
    var random = mix(config.seed, index, 2);
    const unit = struct {
        fn draw(r: *std.Random.SplitMix64) f64 {
            return (@as(f64, @floatFromInt(r.next() >> 11)) + 0.5) / @as(f64, 1 << 53);
        }
    }.draw;
    if (index >= config.active) return 86_400.0 + unit(&random) * 29.0 * 86_400.0;
    const per_second = @as(f64, @floatFromInt(config.rate)) * config.sightings() * pickShare(config, index);
    const gap = 1.0 / per_second;
    var wait = -@log(unit(&random)) * gap;
    if (unit(&random) >= 3600.0 / (3600.0 + gap)) wait += -@log(unit(&random)) * gap;
    return unit(&random) * (3600.0 + wait);
}

fn buildBase(gpa: Allocator, io: Io, root: Io.Dir, path: []const u8, config: Config) !void {
    const store = try store_mod.Store.open(gpa, io, root, path, .{ .maintenance = .bulk_load });
    defer store.close();

    const Aged = struct { index: u32, at: u64 };
    const order = try gpa.alloc(Aged, config.actors);
    defer gpa.free(order);
    for (order, 0..) |*slot, index| slot.* = .{ .index = @intCast(index), .at = t0 - @as(u64, @intFromFloat(stampAge(config, index))) };
    std.mem.sort(Aged, order, {}, struct {
        fn before(_: void, a: Aged, b: Aged) bool {
            return if (a.at != b.at) a.at < b.at else a.index < b.index;
        }
    }.before);

    var done: usize = 0;
    while (done < order.len) {
        // Small commits for the actors a run can reach, so their stamps
        // keep the spread they were drawn with.
        const size: usize = if (order[done].at + 7200 < t0) 2000 else 100;
        const chunk = order[done..@min(done + size, order.len)];
        const batch = try store.begin(chunk[chunk.len / 2].at);
        errdefer batch.abort();
        for (chunk) |aged| {
            const index: u64 = aged.index;
            const did = didOf(config.seed, index);
            var random = mix(config.seed, index, 3);
            var handle_buf: [64]u8 = undefined;
            var name_buf: [64]u8 = undefined;
            var about_buf: [160]u8 = undefined;
            var pds_buf: [64]u8 = undefined;
            var avatar_buf: [59]u8 = undefined;
            const custom = random.next() % 10 == 0;
            _ = try batch.apply(.{ .profile = .{
                .did = &did,
                .handle = try std.fmt.bufPrint(&handle_buf, "{s}{d}.{s}", .{ words[@intCast(index % words.len)], index, if (custom) "example.com" else "bsky.social" }),
                .display_name = sentence(&name_buf, &random, 2),
                .avatar = if (random.next() % 4 != 0) cidText(&avatar_buf, config.seed, index, 4) else "",
                .pds = try std.fmt.bufPrint(&pds_buf, "https://pds{d}.host.bsky.network", .{index % 40}),
                .description = if (random.next() % 10 < 6) sentence(&about_buf, &random, 12) else "",
            } });
            const bits = random.next();
            _ = try batch.apply(.{ .enrichment = .{
                .did = &did,
                .source = .bsky,
                .created_at = "2024-11-19T08:15:30.000Z",
                .followers_count = bits % 5000,
                .follows_count = (bits >> 16) % 2000,
                .posts_count = (bits >> 32) % 20_000,
                .quality_score = if (bits >> 60 < 5) 1.0 + @as(f32, @floatFromInt((bits >> 40) % 30_000)) / 100.0 else 0,
            } });
        }
        try batch.commit();
        _ = try store.maintainStep();
        done += chunk.len;
    }
    try store.maintainer.settle();
}

fn copyTree(gpa: Allocator, io: Io, from: Io.Dir, to: Io.Dir) !void {
    var walker = try from.walk(gpa);
    defer walker.deinit();
    while (try walker.next(io)) |entry| switch (entry.kind) {
        .directory => try to.createDirPath(io, entry.path),
        .file => try from.copyFile(entry.path, to, entry.path, io, .{}),
        else => {},
    };
}

// -- synthetic relay --

const Pooled = struct { cid: zat.cbor.Cid, block: zat.car.Block };

const Collection = struct { nsid: []const u8, share: u64 };
const wanted = [_]Collection{ .{ .nsid = "app.bsky.feed.like", .share = 60 }, .{ .nsid = "app.bsky.feed.post", .share = 22 }, .{ .nsid = "app.bsky.graph.follow", .share = 18 } };
const unwanted = [_]Collection{ .{ .nsid = "app.bsky.feed.repost", .share = 60 }, .{ .nsid = "app.bsky.graph.listitem", .share = 20 }, .{ .nsid = "app.bsky.graph.block", .share = 10 }, .{ .nsid = "app.bsky.feed.threadgate", .share = 10 } };
const pool_size = 32;

const Frame = struct {
    /// Ciphertext when TLS is modelled, the frame itself otherwise.
    wire: []u8,
    tag: [Aes256Gcm.tag_length]u8 = undefined,
    plain: []u8,
    micros: u64,
};

const Relay = struct {
    config: Config,
    /// Lives for the run: the block pools.
    arena: Allocator,
    random: std.Random.DefaultPrng,
    commits: [pool_size]Pooled = undefined,
    trees: [256]Pooled = undefined,
    wanted_records: [wanted.len][pool_size]Pooled = undefined,
    unwanted_records: [unwanted.len][pool_size]Pooled = undefined,
    blobs: [64]zat.cbor.Cid = undefined,
    seq: i64 = 35_000_000_000,
    micros: u64 = 0,
    strangers: u64 = 0,
    key: [Aes256Gcm.key_length]u8 = @splat(0x5a),

    fn init(arena: Allocator, config: Config) !Relay {
        var self: Relay = .{ .config = config, .arena = arena, .random = .init(config.seed) };
        const r = self.random.random();
        for (&self.commits) |*slot| {
            var signature: [64]u8 = undefined;
            r.bytes(&signature);
            slot.* = try self.block(.{ .map = &.{
                .{ .key = "did", .value = .{ .text = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa" } },
                .{ .key = "rev", .value = .{ .text = "3m5aaaaaaaa22" } },
                .{ .key = "sig", .value = .{ .bytes = &signature } },
                .{ .key = "data", .value = .{ .bytes = signature[0..36] } },
                .{ .key = "prev", .value = .null },
                .{ .key = "version", .value = .{ .unsigned = 3 } },
            } });
        }
        for (&self.trees) |*slot| {
            const size = config.tree_block_bytes / 2 + r.uintLessThan(u64, config.tree_block_bytes + 1);
            const filler = try arena.alloc(u8, size);
            r.bytes(filler);
            slot.* = try self.block(.{ .map = &.{.{ .key = "e", .value = .{ .bytes = filler } }} });
        }
        for (&self.blobs) |*slot| {
            var seed: [16]u8 = undefined;
            r.bytes(&seed);
            slot.* = try zat.cbor.Cid.create(arena, 1, zat.cbor.Codec.raw, zat.cbor.HashFn.sha2_256, &seed);
        }
        for (wanted, &self.wanted_records) |collection, *pool| for (pool) |*slot| {
            slot.* = try self.record(collection.nsid);
        };
        for (unwanted, &self.unwanted_records) |collection, *pool| for (pool) |*slot| {
            slot.* = try self.record(collection.nsid);
        };
        return self;
    }

    fn block(self: *Relay, value: zat.cbor.Value) !Pooled {
        const bytes = try zat.cbor.encodeAlloc(self.arena, value);
        const cid = try zat.cbor.Cid.forDagCbor(self.arena, bytes);
        return .{ .cid = cid, .block = .{ .cid_raw = cid.raw, .data = bytes } };
    }

    fn record(self: *Relay, nsid: []const u8) !Pooled {
        const r = self.random.random();
        var text_buf: [300]u8 = undefined;
        var seed: std.Random.SplitMix64 = .init(r.int(u64));
        if (std.mem.eql(u8, nsid, "app.bsky.feed.post")) return self.block(.{ .map = &.{
            .{ .key = "text", .value = .{ .text = sentence(&text_buf, &seed, 4 + r.uintLessThan(usize, 30)) } },
            .{ .key = "$type", .value = .{ .text = nsid } },
            .{ .key = "langs", .value = .{ .array = &.{.{ .text = "en" }} } },
            .{ .key = "createdAt", .value = .{ .text = "2026-11-12T08:00:00.000Z" } },
        } });
        const subject_did = didOf(self.config.seed, r.uintLessThan(u64, self.config.actors));
        const uri = try std.fmt.allocPrint(self.arena, "at://{s}/app.bsky.feed.post/3m5aaaaaaaa22", .{subject_did});
        var cid_buf: [59]u8 = undefined;
        const subject: zat.cbor.Value = if (std.mem.startsWith(u8, nsid, "app.bsky.feed.")) .{ .map = &.{
            .{ .key = "cid", .value = .{ .text = cidText(&cid_buf, self.config.seed, r.int(u32), 5) } },
            .{ .key = "uri", .value = .{ .text = uri } },
        } } else .{ .text = &subject_did };
        return self.block(.{ .map = &.{
            .{ .key = "$type", .value = .{ .text = nsid } },
            .{ .key = "subject", .value = subject },
            .{ .key = "createdAt", .value = .{ .text = "2026-11-12T08:00:00.000Z" } },
        } });
    }

    fn pick(r: std.Random, comptime list: []const Collection) usize {
        var roll = r.uintLessThan(u64, 100);
        for (list, 0..) |collection, index| {
            if (roll < collection.share) return index;
            roll -= collection.share;
        }
        return list.len - 1;
    }

    fn known(self: *Relay) Did {
        const u = self.random.random().float(f64);
        const index: u64 = @intFromFloat(std.math.pow(f64, u, self.config.skew) * @as(f64, @floatFromInt(self.config.active)));
        return didOf(self.config.seed, @min(index, self.config.active - 1));
    }

    fn stamp(buf: *[24]u8, micros: u64) []const u8 {
        const seconds: std.time.epoch.EpochSeconds = .{ .secs = t0 + micros / 1_000_000 };
        const day = seconds.getEpochDay().calculateYearDay();
        const month = day.calculateMonthDay();
        const clock = seconds.getDaySeconds();
        return std.fmt.bufPrint(buf, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}.{d:0>3}Z", .{
            day.year,                month.month.numeric(),      month.day_index + 1,
            clock.getHoursIntoDay(), clock.getMinutesIntoHour(), clock.getSecondsIntoMinute(),
            (micros / 1000) % 1000,
        }) catch unreachable;
    }

    fn commit(self: *Relay, a: Allocator, did: []const u8, action: zat.firehose.CommitAction, nsid: []const u8, rkey: []const u8, body: ?Pooled, time: []const u8) ![]u8 {
        const r = self.random.random();
        var blocks: std.ArrayList(zat.car.Block) = .empty;
        const head = self.commits[r.uintLessThan(usize, pool_size)];
        try blocks.append(a, head.block);
        for (0..self.config.tree_blocks) |_| try blocks.append(a, self.trees[r.uintLessThan(usize, self.trees.len)].block);
        if (body) |pooled| try blocks.append(a, pooled.block);
        const car = try zat.car.writeAlloc(a, .{ .roots = &.{head.cid}, .blocks = blocks.items });
        const rev = zat.Tid.fromTimestamp(t0 * 1_000_000 + self.micros, 0).str();
        return zat.firehose.encodeCommitEvent(a, .{
            .seq = self.seq,
            .repo_did = did,
            .commit_cid = head.cid,
            .rev = &rev,
            .prev_data = self.trees[0].cid,
            .blocks = car,
            .ops = &.{.{ .action = action, .collection = nsid, .rkey = rkey, .cid = if (body) |pooled| pooled.cid else null }},
            .time = time,
        });
    }

    fn signal(self: *Relay, a: Allocator, kind: []const u8, fields: []const zat.cbor.Value.MapEntry) ![]u8 {
        _ = self;
        var out: Io.Writer.Allocating = .init(a);
        try zat.cbor.encode(a, &out.writer, .{ .map = &.{
            .{ .key = "op", .value = .{ .unsigned = 1 } },
            .{ .key = "t", .value = .{ .text = kind } },
        } });
        try zat.cbor.encode(a, &out.writer, .{ .map = fields });
        return out.written();
    }

    /// The next frame, allocated in `a`.
    fn next(self: *Relay, a: Allocator) !Frame {
        const r = self.random.random();
        const config = self.config;
        self.seq += 1;
        self.micros += 1_000_000 / config.rate;
        var time_buf: [24]u8 = undefined;
        const time = stamp(&time_buf, self.micros);
        const rkey = zat.Tid.fromTimestamp(t0 * 1_000_000 + self.micros, 1).str();
        const seq: zat.cbor.Value = .{ .unsigned = @intCast(self.seq) };

        var roll = r.uintLessThan(u64, 1000);
        const plain: []u8 = if (roll < config.unwanted) blk: {
            const which = pick(r, &unwanted);
            break :blk try self.commit(a, &self.known(), .create, unwanted[which].nsid, &rkey, self.unwanted_records[which][r.uintLessThan(usize, pool_size)], time);
        } else if (sub(&roll, config.unwanted) < config.deletes) blk: {
            break :blk try self.commit(a, &self.known(), .delete, wanted[pick(r, &wanted)].nsid, &rkey, null, time);
        } else if (sub(&roll, config.deletes) < config.profiles) blk: {
            var name_buf: [64]u8 = undefined;
            var about_buf: [200]u8 = undefined;
            var seed: std.Random.SplitMix64 = .init(r.int(u64));
            var fields: std.ArrayList(zat.cbor.Value.MapEntry) = .empty;
            try fields.append(a, .{ .key = "$type", .value = .{ .text = "app.bsky.actor.profile" } });
            if (r.boolean()) try fields.append(a, .{ .key = "displayName", .value = .{ .text = sentence(&name_buf, &seed, 2) } });
            if (r.boolean()) try fields.append(a, .{ .key = "description", .value = .{ .text = sentence(&about_buf, &seed, 10) } });
            if (r.uintLessThan(u8, 10) < 3) try fields.append(a, .{ .key = "avatar", .value = .{ .map = &.{
                .{ .key = "$type", .value = .{ .text = "blob" } },
                .{ .key = "ref", .value = .{ .cid = self.blobs[r.uintLessThan(usize, self.blobs.len)] } },
                .{ .key = "mimeType", .value = .{ .text = "image/jpeg" } },
                .{ .key = "size", .value = .{ .unsigned = 48_213 } },
            } } });
            const bytes = try zat.cbor.encodeAlloc(a, .{ .map = fields.items });
            const cid = try zat.cbor.Cid.forDagCbor(a, bytes);
            break :blk try self.commit(a, &self.known(), .update, "app.bsky.actor.profile", "self", .{ .cid = cid, .block = .{ .cid_raw = cid.raw, .data = bytes } }, time);
        } else if (sub(&roll, config.profiles) < config.unknown) blk: {
            self.strangers += 1;
            const did = didOf(config.seed, config.actors + self.strangers);
            break :blk try self.commit(a, &did, .create, "app.bsky.feed.like", &rkey, self.wanted_records[0][0], time);
        } else if (sub(&roll, config.unknown) < config.identity) blk: {
            break :blk try self.signal(a, "#identity", &.{
                .{ .key = "did", .value = .{ .text = &self.known() } },
                .{ .key = "seq", .value = seq },
                .{ .key = "time", .value = .{ .text = time } },
            });
        } else if (sub(&roll, config.identity) < config.account) blk: {
            const fate = r.uintLessThan(u8, 20);
            break :blk try self.signal(a, "#account", &.{
                .{ .key = "did", .value = .{ .text = &self.known() } },
                .{ .key = "seq", .value = seq },
                .{ .key = "time", .value = .{ .text = time } },
                .{ .key = "active", .value = .{ .boolean = fate >= 5 } },
                .{ .key = "status", .value = if (fate >= 5) .null else .{ .text = if (fate == 0) "deleted" else if (fate < 3) "takendown" else "deactivated" } },
            });
        } else blk: {
            const which = pick(r, &wanted);
            break :blk try self.commit(a, &self.known(), .create, wanted[which].nsid, &rkey, self.wanted_records[which][r.uintLessThan(usize, pool_size)], time);
        };

        var frame: Frame = .{ .wire = plain, .plain = plain, .micros = self.micros };
        if (config.tls != 0) {
            frame.wire = try a.alloc(u8, plain.len);
            frame.plain = try a.alloc(u8, plain.len);
            Aes256Gcm.encrypt(frame.wire, &frame.tag, plain, &record_header, nonceOf(self.seq), self.key);
        }
        return frame;
    }

    fn sub(roll: *u64, share: u64) u64 {
        roll.* -= share;
        return roll.*;
    }
};

const record_header = [5]u8{ 23, 3, 3, 0, 0 };

fn nonceOf(seq: i64) [Aes256Gcm.nonce_length]u8 {
    var nonce: [Aes256Gcm.nonce_length]u8 = @splat(0);
    std.mem.writeInt(u64, nonce[4..], @intCast(seq), .big);
    return nonce;
}

// -- the measured path --

/// What zat's subscription client does with one frame: the handler's
/// `onRawFrame` when it has one, a decode in a frame arena and `onEvent`
/// otherwise.
fn deliver(gpa: Allocator, intake: *intake_mod.Intake, data: []const u8) !void {
    if (comptime @hasDecl(intake_mod.Intake, "onRawFrame")) {
        _ = zat.firehose.peekSeq(gpa, data);
        const result = intake.onRawFrame(data);
        if (comptime @TypeOf(result) != void) try result;
        return;
    }
    var arena: std.heap.ArenaAllocator = .init(gpa);
    defer arena.deinit();
    const event = try zat.firehose.decodeFrame(arena.allocator(), data);
    intake.onEvent(event);
}

/// The owner's maintenance step, with what the store wrote counted around
/// it: bytes that entered the memtable, and every table a flush or a
/// compaction produced.
const Writes = struct {
    gpa: Allocator,
    memtable_seen: usize = 0,
    committed_bytes: u64 = 0,
    tables: std.AutoHashMapUnmanaged(u64, void) = .empty,
    flushed_bytes: u64 = 0,
    compacted_bytes: u64 = 0,

    fn step(context: ?*anyopaque, store: *store_mod.Store) anyerror!store_mod.maintain.Pressure {
        const self: *Writes = @ptrCast(@alignCast(context.?));
        const before = store.db().memtableBytes();
        if (before > self.memtable_seen) self.committed_bytes += before - self.memtable_seen;
        const pressure = try store.maintainStep();
        self.memtable_seen = store.db().memtableBytes();
        try self.note(store, true);
        return pressure;
    }

    fn note(self: *Writes, store: *store_mod.Store, count: bool) !void {
        for (store.db().version.tables, 0..) |level, depth| for (level) |table| {
            const slot = try self.tables.getOrPut(self.gpa, table.id);
            if (slot.found_existing or !count) continue;
            if (depth == 0) self.flushed_bytes += table.size else self.compacted_bytes += table.size;
        };
    }
};

const Enricher = struct {
    config: Config,
    random: std.Random.DefaultPrng,

    fn post(self: *Enricher, gpa: Allocator, owner: *owner_mod.Owner) !void {
        const submission = try owner_mod.Submission.create(gpa);
        errdefer submission.destroy();
        const a = submission.arena.allocator();
        const r = self.random.random();
        const changes = try a.alloc(store_mod.Change, self.config.enrich);
        for (changes) |*slot| {
            const did = try a.dupe(u8, &didOf(self.config.seed, r.uintLessThan(u64, self.config.actors)));
            slot.* = .{ .enrichment = .{
                .did = did,
                .source = .bsky,
                .followers_count = 1 + r.uintLessThan(u64, 5000),
                .posts_count = 1 + r.uintLessThan(u64, 20_000),
                .quality_score = 1.0 + @as(f32, @floatFromInt(r.uintLessThan(u32, 30_000))) / 100.0,
            } };
        }
        submission.changes = changes;
        try owner.post(submission, null);
    }
};

// -- the digest --

const Digest = struct {
    arena: std.heap.ArenaAllocator,
    since: u64,
    ranges: std.EnumArray(store_mod.keys.Prefix, Sha256) = .initFill(.init(.{})),
    counts: std.EnumArray(store_mod.keys.Prefix, u64) = .initFill(0),
    /// Actor fields a change-log consumer reads, as `Actor.contentEql` has them.
    content: Sha256 = .init(.{}),
    /// The request bodies the index bridge would send for every actor.
    documents: Sha256 = .init(.{}),
    entries: std.ArrayList(bridge.wire.Entry) = .empty,
    stamped: u64 = 0,

    fn feed(hasher: *Sha256, bytes: []const u8) void {
        var len: [4]u8 = undefined;
        std.mem.writeInt(u32, &len, @intCast(bytes.len), .little);
        hasher.update(&len);
        hasher.update(bytes);
    }

    pub fn entry(self: *Digest, key: []const u8, value: []const u8) !void {
        const prefix = std.enums.fromInt(store_mod.keys.Prefix, key[0]) orelse return error.UnknownPrefix;
        feed(self.ranges.getPtr(prefix), key);
        feed(self.ranges.getPtr(prefix), value);
        self.counts.getPtr(prefix).* += 1;
        if (prefix != .actor) return;

        const a = self.arena.allocator();
        const did = try a.dupe(u8, try store_mod.keys.parseActor(key));
        const actor = try store_mod.Actor.decode(a, try a.dupe(u8, value));
        if (actor.last_activity_at > self.since) self.stamped += 1;

        var observable = actor;
        inline for (.{ "last_activity_at", "updated_at", "profile_checked_at", "identity_resolved_at", "deleted_at", "identity_due", "refresh_due" }) |name| @field(observable, name) = 0;
        observable.override = .none;
        var encoded: std.ArrayList(u8) = .empty;
        try observable.encode(a, &encoded);
        feed(&self.content, did);
        feed(&self.content, encoded.items);

        var document: bridge.wire.Entry = .{ .did = did, .version = 0, .record = null };
        if (actor.status == .live and actor.handle.len > 0) {
            const labels = try a.alloc(bridge.source.Label, actor.labels.len);
            for (labels, actor.labels) |*slot, label| slot.* = .{ .source = label.source, .uri = label.uri, .value = label.value, .created = label.created, .cid = label.cid, .expires = label.expires, .negated = label.negated };
            const sent: bridge.source.Actor = .{
                .status = .live,
                .hidden = actor.hidden,
                .handle = actor.handle,
                .display_name = actor.display_name,
                .avatar = actor.avatar,
                .pds = actor.pds,
                .created_at = actor.created_at,
                .associated = actor.associated,
                .followers_count = actor.followers_count,
                .follows_count = actor.follows_count,
                .posts_count = actor.posts_count,
                .quality_score = actor.quality_score,
                .labels = labels,
            };
            if (bridge.wire.rejection(a, sent) == null) document.record = sent;
        }
        try self.entries.append(a, document);
        if (self.entries.items.len == bridge.wire.max_batch) try self.push();
    }

    fn push(self: *Digest) !void {
        if (self.entries.items.len > 0) {
            const body = try bridge.wire.encodeReconcile(self.arena.allocator(), .{
                .entries = self.entries.items,
                .expected = &.{},
                .cursor = .{ .source_id = "bench", .build_id = "bench", .snapshot_watermark = 0, .position = 0, .through = 0 },
                .expected_cursor = "null",
                .expected_revision = 0,
            });
            self.documents.update(body);
        }
        self.entries = .empty;
        _ = self.arena.reset(.retain_capacity);
    }

    fn hex(hasher: *Sha256) [16]u8 {
        var out: [Sha256.digest_length]u8 = undefined;
        var copy = hasher.*;
        copy.final(&out);
        return std.fmt.bytesToHex(out[0..8], .lower);
    }
};

pub fn main(init: std.process.Init) !void {
    const io = init.io;
    const gpa = std.heap.smp_allocator;
    const config = try Config.parse(try init.minimal.args.toSlice(init.arena.allocator()));
    var out_buf: [4096]u8 = undefined;
    var stdout = Io.File.stdout().writer(io, &out_buf);
    const out = &stdout.interface;

    const cwd = Io.Dir.cwd();
    try cwd.createDirPath(io, config.dir);
    var dir = try cwd.openDir(io, config.dir, .{ .iterate = true });
    defer dir.close(io);
    const base = try std.fmt.allocPrint(init.arena.allocator(), "base-{d}-{d}-{d}-{d}", .{ config.actors, config.active, @as(u64, @intFromFloat(config.skew * 100)), config.seed });
    const marker = try std.fmt.allocPrint(init.arena.allocator(), "{s}.built", .{base});
    if (dir.access(io, marker, .{})) |_| {} else |_| {
        dir.deleteTree(io, base) catch {};
        const started = cpuSeconds();
        try buildBase(gpa, io, dir, base, config);
        try dir.writeFile(io, .{ .sub_path = marker, .data = "" });
        try out.print("built {s} in {d:.1} cpu-s\n", .{ base, cpuSeconds() - started });
        try out.flush();
    }
    dir.deleteTree(io, "run") catch {};
    try dir.createDirPath(io, "run");
    {
        var from = try dir.openDir(io, base, .{ .iterate = true });
        defer from.close(io);
        var to = try dir.openDir(io, "run", .{});
        defer to.close(io);
        try copyTree(gpa, io, from, to);
    }

    var clock: SimClock = .{};
    var writes: Writes = .{ .gpa = gpa };
    defer writes.tables.deinit(gpa);
    const owner = try owner_mod.Owner.open(gpa, io, dir, "run", .{
        .clock = .{ .context = &clock, .seconds = SimClock.seconds },
        .maintenance = .{ .context = &writes, .step = Writes.step },
    });
    defer owner.close();
    try writes.note(owner.store, false);
    writes.memtable_seen = owner.store.db().memtableBytes();

    var intake: intake_mod.Intake = .{ .gpa = gpa, .owner = owner };
    defer intake.deinit();
    intake.resumeFrom(35_000_000_000);

    var pools: std.heap.ArenaAllocator = .init(gpa);
    defer pools.deinit();
    var relay = try Relay.init(pools.allocator(), config);
    var enricher: Enricher = .{ .config = config, .random = .init(config.seed +% 1) };
    var chunk: std.heap.ArenaAllocator = .init(gpa);
    defer chunk.deinit();
    const frames = try gpa.alloc(Frame, 2048);
    defer gpa.free(frames);

    var tls_cpu: f64 = 0;
    var decode_cpu: f64 = 0;
    var relay_cpu: f64 = 0;
    var owner_cpu: f64 = 0;
    var frame_bytes: u64 = 0;
    var batches_seen: u64 = 0;
    var enriched_at: u64 = 0;
    var done: u64 = 0;
    const wall_started = Io.Clock.awake.now(io);
    while (done < config.events) {
        _ = chunk.reset(.retain_capacity);
        const count: usize = @intCast(@min(frames.len, config.events - done));
        for (frames[0..count]) |*frame| {
            frame.* = try relay.next(chunk.allocator());
            frame_bytes += frame.wire.len;
        }

        var mark = cpuSeconds();
        if (config.tls != 0) for (frames[0..count], 0..) |frame, index| {
            try Aes256Gcm.decrypt(frame.plain, frame.wire, frame.tag, &record_header, nonceOf(relay.seq - @as(i64, @intCast(count - 1 - index))), relay.key);
        };
        tls_cpu += cpuSeconds() - mark;

        mark = cpuSeconds();
        for (frames[0..count]) |frame| {
            var arena: std.heap.ArenaAllocator = .init(gpa);
            defer arena.deinit();
            std.mem.doNotOptimizeAway(try zat.firehose.decodeFrame(arena.allocator(), frame.plain));
        }
        decode_cpu += cpuSeconds() - mark;

        mark = cpuSeconds();
        for (frames[0..count]) |frame| {
            clock.micros = frame.micros;
            try deliver(gpa, &intake, frame.plain);
            if (intake.dead) return error.IntakeFailed;
            const handed = intake.signals.batches.load(.monotonic);
            if (handed == batches_seen) continue;
            batches_seen = handed;
            const turn_started = cpuSeconds();
            const second = clock.micros / 1_000_000;
            if (config.enrich > 0 and second > enriched_at) {
                enriched_at = second;
                try enricher.post(gpa, owner);
            }
            _ = owner.turn(0);
            while (owner.counters.queued_changes.load(.monotonic) > 0) _ = owner.turn(0);
            owner_cpu += cpuSeconds() - turn_started;
        }
        relay_cpu += cpuSeconds() - mark;
        done += count;
    }
    {
        const mark = cpuSeconds();
        try intake.flush();
        _ = owner.turn(0);
        while (owner.counters.queued_changes.load(.monotonic) > 0) _ = owner.turn(0);
        owner_cpu += cpuSeconds() - mark;
        relay_cpu += cpuSeconds() - mark;
    }
    const wall = @as(f64, @floatFromInt(wall_started.durationTo(Io.Clock.awake.now(io)).toNanoseconds())) / 1e9;

    const per_thousand = 1000.0 / @as(f64, @floatFromInt(config.events));
    const c = &owner.counters;
    const stats = owner.store.stats();
    try out.print(
        \\events={d} actors={d} active={d} skew={d:.2} seed={d} rate={d}/s simulated_s={d} mean_frame_bytes={d}
        \\cpu_ms_per_1000: total={d:.2} tls={d:.2} decode={d:.2} intake={d:.2} owner={d:.2}
        \\commits={d} batches={d} changes_applied={d} actors_changed={d} actors_created={d} trimmed={d}
        \\store: committed_bytes={d} flushes={d} flushed_bytes={d} compactions={d} compaction_steps={d} compacted_bytes={d} tables={d} table_bytes={d}
        \\peak_rss_mb={d} wall_s={d:.1}
        \\
    , .{
        config.events,                               config.actors,                     config.active,                      config.skew,                                                config.seed,                     config.rate,                clock.micros / 1_000_000,                frame_bytes / config.events,
        (tls_cpu + relay_cpu) * per_thousand * 1000, tls_cpu * per_thousand * 1000,     decode_cpu * per_thousand * 1000,   (relay_cpu - decode_cpu - owner_cpu) * per_thousand * 1000, owner_cpu * per_thousand * 1000, c.commits.load(.monotonic), intake.signals.batches.load(.monotonic), c.changes_applied.load(.monotonic),
        c.actors_changed.load(.monotonic),           c.actors_created.load(.monotonic), c.changes_trimmed.load(.monotonic), writes.committed_bytes,                                     stats.flushes.count,             writes.flushed_bytes,       stats.compactions,                       stats.compaction_steps.count,
        writes.compacted_bytes,                      stats.tables,                      stats.table_bytes,                  peakRssBytes() >> 20,                                       wall,
    });
    try out.flush();

    var digest: Digest = .{ .arena = .init(gpa), .since = t0 };
    defer digest.arena.deinit();
    {
        const store = try owner.acquire();
        defer owner.release();
        _ = try store_mod.scan.all(gpa, store.db(), "a", "{", .{}, &digest);
        try digest.push();
    }
    var whole: Sha256 = .init(.{});
    try out.print("stamped={d} ({d:.1} per 1000 events)\ndigest:", .{ digest.stamped, @as(f64, @floatFromInt(digest.stamped)) * per_thousand });
    for (store_mod.keys.prefixes) |prefix| {
        const text = Digest.hex(digest.ranges.getPtr(prefix));
        whole.update(&text);
        try out.print(" {t}={s}/{d}", .{ prefix, text, digest.counts.get(prefix) });
    }
    try out.print("\ndigest: content={s} documents={s}\ndigest: store={s}\n", .{ Digest.hex(&digest.content), Digest.hex(&digest.documents), Digest.hex(&whole) });
    try out.flush();
}
