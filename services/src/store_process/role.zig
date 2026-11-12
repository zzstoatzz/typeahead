//! `MODE=store`: the ingester's second role. It follows the relay, the
//! labeler and the PLC export, resolves identities, refreshes profiles from
//! the appview, and commits everything to a zkv store on local disk. It
//! opens no connection to Turso, and one to the Worker: the control loop
//! pulls moderation policy and indexing requests from it (`control.zig`).
//! Configuration is in `config.zig`, the concurrency model in `owner.zig`,
//! the HTTP surface in `http.zig`, enrichment in `enrich.zig`, the corpus
//! count in `corpus.zig`.
//! `Stage` is the order it starts in.

const std = @import("std");
const zat = @import("zat");
const logfire = @import("logfire");
const fatal = @import("../fatal.zig");
const http_guard = @import("../http_guard.zig");
const identity = @import("../identity.zig");
const ingest = @import("../ingest.zig");
const store_mod = @import("../store/root.zig");
const config_mod = @import("config.zig");
const http = @import("http.zig");
const identity_work = @import("identity_work.zig");
const intake_mod = @import("intake.zig");
const owner_mod = @import("owner.zig");
const tails = @import("tails.zig");
const index_bridge = @import("index_bridge.zig");
const corpus_mod = @import("corpus.zig");
const enrich = @import("enrich.zig");
const control = @import("control.zig");
const bridge_mod = @import("../bridge/root.zig");
const Io = std.Io;
const Owner = owner_mod.Owner;

const log = std.log.scoped(.store_role);

const socket_timeout_seconds = 5;
const relay_host = "bsky.network";

/// The points of a start, in the order the role reaches them. The index
/// stops answering searches 120 s after its last watermark, and only the
/// bridge moves the watermark, so the bridge and then the upstream
/// consumers come before everything that reads the store for its own sake.
pub const Stage = enum {
    /// The writer is open and the relay cursor is read. Nothing has been
    /// counted or scanned.
    store_opened,
    /// The owner is turning and, when an index is configured, the bridge
    /// is pushing.
    bridge_started,
    /// The relay, the labeler, the PLC export and the identity work are
    /// following. Offline, nothing is started here.
    intake_started,
    /// `/store/*` has its handle.
    reader_opened,
    /// The corpus scanner is waiting for its first count.
    corpus_started,
};

pub const State = struct {
    owner: *Owner,
    reader_open: bool,
    corpus_scans: u64,
};

pub const Options = struct {
    /// How long after the start the process does only what feeds the
    /// index: the owner leaves its scans (`owner.Options.startup_seconds`)
    /// and the `/store/*` handle is not opened.
    startup_seconds: u64 = 30,
    /// How long after the start the first corpus count begins.
    corpus_delay_seconds: u64 = 300,
    context: ?*anyopaque = null,
    /// Called as each stage is reached, on the thread that reached it.
    on_stage: ?*const fn (context: ?*anyopaque, stage: Stage, state: State) void = null,
    /// Called after a stop was asked for and the owner has finished the
    /// commit it was in. `serve` exits the process here: see `run`'s end.
    on_stopped: ?*const fn (context: ?*anyopaque) void = null,
    /// Ends `run` the way SIGTERM does, for a caller that cannot shut the
    /// listening socket down under an accept (macOS): set it, then connect
    /// to the listener once.
    stop: ?*const std.atomic.Value(bool) = null,
};

var role: ?*Role = null;

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return std.mem.span(p);
    return null;
}

/// True once a stop was asked for. The owner refuses submissions from then
/// on, which every producer sees as a failure; none of them is one.
fn stopping() bool {
    const self = role orelse return false;
    return self.stopping.load(.acquire);
}

fn exitOnStoreFailure() void {
    if (stopping()) return;
    log.err("the store would not commit after repeated reopens, exiting for restart", .{});
    fatal.exitForRestart();
}

fn exitOnIntakeFailure() void {
    if (stopping()) return;
    log.err("a relay event could not be queued, exiting for restart to replay it", .{});
    fatal.exitForRestart();
}

/// One span a minute with the store's numbers, after the owner refreshed
/// them, so a gap means the owner is not turning. docs/operations.md
/// "Telemetry".
fn heartbeat(owner: *Owner) void {
    const self = role orelse return;
    const health = owner.health();
    const c = &owner.counters;
    const span = logfire.span("store.heartbeat", .{});
    defer span.end();
    span.setAttribute("relay_cursor", control.attribute(health.cursors.relay orelse 0));
    span.setAttribute("relay_handed_cursor", self.intake.signals.handed_cursor.load(.monotonic));
    span.setAttribute("relay_events", self.intake.signals.events.load(.monotonic));
    span.setAttribute("relay_paused", self.intake.signals.paused.load(.monotonic));
    span.setAttribute("source_event_at", self.intake.signals.source_event_at.load(.monotonic));
    span.setAttribute("labeler_cursor", control.attribute(health.cursors.labeler orelse 0));
    span.setAttribute("change_log_head", health.cursors.next_change);
    span.setAttribute("change_log_floor", health.cursors.change_floor);
    span.setAttribute("queued_changes", c.queued_changes.load(.monotonic));
    span.setAttribute("identity_pending", health.identity_pending);
    span.setAttribute("identity_due", health.identity_due);
    span.setAttribute("refresh_pending", health.refresh_pending);
    span.setAttribute("refresh_due", health.refresh_due);
    span.setAttribute("refresh_leased", health.refresh_pending -| health.refresh_due);
    if (self.enricher) |enricher| {
        const s = &enricher.stats;
        span.setAttribute("enrich_leased", s.leased.load(.monotonic));
        span.setAttribute("enrich_completed", s.enriched.load(.monotonic) + s.from_blacksky.load(.monotonic) + s.from_pds.load(.monotonic));
        span.setAttribute("enrich_from_blacksky", s.from_blacksky.load(.monotonic));
        span.setAttribute("enrich_from_pds", s.from_pds.load(.monotonic));
        span.setAttribute("enrich_not_found", s.not_found.load(.monotonic));
        span.setAttribute("enrich_changed", s.changed.load(.monotonic));
        span.setAttribute("enrich_handle_queued", s.handle_queued.load(.monotonic));
        span.setAttribute("enrich_handle_pending", s.handle_pending.load(.monotonic));
        span.setAttribute("enrich_handle_settled", s.handle_settled.load(.monotonic));
        span.setAttribute("enrich_handle_deferred", s.handle_deferred.load(.monotonic));
        span.setAttribute("enrich_retried", s.retried.load(.monotonic));
        span.setAttribute("enrich_requests", s.requests.load(.monotonic));
        span.setAttribute("enrich_failed", s.failed.load(.monotonic));
        span.setAttribute("enrich_rate_limited", s.rate_limited.load(.monotonic));
        span.setAttribute("enrich_commit_failed", s.commit_failed.load(.monotonic));
        span.setAttribute("enrich_last_success_at", s.last_success_at.load(.monotonic));
        span.setAttribute("enrich_backoff_until", s.backoff_until.load(.monotonic));
        span.setAttribute("enrich_backed_up", s.backed_up.load(.monotonic));
        span.setAttribute("enrich_walk_queued", s.walk_queued.load(.monotonic));
        span.setAttribute("enrich_sweep_queued", s.sweep_queued.load(.monotonic));
        span.setAttribute("enrich_walk_per_hour", enricher.options.walk_per_hour);
        span.setAttribute("enrich_sweep_per_hour", enricher.options.sweep_per_hour);
        span.setAttribute("enrich_requests_per_second", enricher.options.requests_per_second);
    }
    const quality_walk = health.walks.get(.quality);
    const rest_walk = health.walks.get(.rest);
    span.setAttribute("walk_quality_passes", quality_walk.passes);
    span.setAttribute("walk_quality_score", @as(f64, quality_walk.score));
    span.setAttribute("walk_quality_last_pass_seconds", quality_walk.last_pass_seconds);
    span.setAttribute("walk_rest_passes", rest_walk.passes);
    span.setAttribute("walk_rest_position", rest_walk.position());
    span.setAttribute("walk_rest_last_pass_seconds", rest_walk.last_pass_seconds);
    if (self.corpus) |scanner| {
        const report = scanner.report();
        span.setAttribute("corpus_scans", report.scans);
        span.setAttribute("corpus_scan_failed", report.failed);
        span.setAttribute("corpus_scanned_at", report.finished_at);
        span.setAttribute("corpus_scan_ms", report.duration_ms);
        if (report.counts) |counts| {
            span.setAttribute("corpus_live", counts.live);
            span.setAttribute("corpus_total", counts.total());
            span.setAttribute("corpus_with_handles", counts.with_handles);
            span.setAttribute("corpus_with_avatars", counts.with_avatars);
            span.setAttribute("corpus_hidden", counts.hidden);
            span.setAttribute("corpus_ranked", counts.ranked);
            span.setAttribute("corpus_rest", counts.rest);
            if (self.enricher) |enricher| {
                if (enrich.walkPeriod(counts.ranked, counts.ranked, enricher.options.walk_per_hour, enrich.quality_scan_factor)) |period| span.setAttribute("walk_quality_period_seconds", period);
                if (enrich.walkPeriod(counts.rest, counts.records, enricher.options.sweep_per_hour, enrich.rest_scan_factor)) |period| span.setAttribute("walk_rest_period_seconds", period);
            }
        }
    }
    span.setAttribute("handle_repair_examined", health.repair.examined);
    span.setAttribute("handle_repair_queued", health.repair.queued);
    span.setAttribute("handle_repair_remaining", health.repair.remaining);
    span.setAttribute("handle_repair_finished_at", health.repair.finished_at);
    span.setAttribute("tombstones", health.tombstones);
    span.setAttribute("commits", c.commits.load(.monotonic));
    span.setAttribute("changes_applied", c.changes_applied.load(.monotonic));
    span.setAttribute("changes_rejected", c.changes_rejected.load(.monotonic));
    span.setAttribute("actors_created", c.actors_created.load(.monotonic));
    span.setAttribute("actors_gone", c.actors_gone.load(.monotonic));
    span.setAttribute("actors_reactivated", c.actors_reactivated.load(.monotonic));
    span.setAttribute("reopens", c.reopens.load(.monotonic));
    span.setAttribute("posts_blocked", c.posts_blocked.load(.monotonic));
    span.setAttribute("stall_turns", c.stall_turns.load(.monotonic));
    span.setAttribute("disk_bytes", health.disk.bytes);
    if (health.maintenance) |stats| {
        span.setAttribute("tables", stats.tables);
        span.setAttribute("level0_tables", stats.levels[0].tables);
        span.setAttribute("table_bytes", stats.table_bytes);
        span.setAttribute("resident_metadata_bytes", stats.resident_metadata_bytes);
        span.setAttribute("stalls", stats.stalls.count);
        span.setAttribute("stall_ms", stats.stalls.total_ns / std.time.ns_per_ms);
        span.setAttribute("compactions", stats.compactions);
        span.setAttribute("last_compaction_at", stats.last_compaction_at);
    }
    if (self.bridge) |bridge| {
        const b = bridge.health();
        span.setAttribute("bridge_position", control.attribute(b.position orelse 0));
        span.setAttribute("bridge_index_position", b.index_position);
        span.setAttribute("bridge_lag", b.lag);
        span.setAttribute("bridge_pushes", b.pushes);
        span.setAttribute("bridge_applied", b.applied);
        span.setAttribute("bridge_errors", b.errors);
        span.setAttribute("bridge_conflicts", b.conflicts);
        span.setAttribute("bridge_resyncs_started", b.resyncs_started);
        span.setAttribute("bridge_last_push_at", b.last_push_at);
    }
    if (self.controller) |loop| {
        const r = loop.report();
        span.setAttribute("control_age_seconds", control.attribute(r.age_seconds));
        span.setAttribute("control_policy_revision", control.attribute(r.policy_revision orelse 0));
        span.setAttribute("control_policy_checked_at", control.attribute(r.policy_checked_at));
        span.setAttribute("control_policy_applied_at", control.attribute(r.policy_applied_at));
        span.setAttribute("control_rules_changed", control.attribute(r.rules_changed));
        span.setAttribute("control_requests_sequence", control.attribute(r.requests_sequence));
        span.setAttribute("control_requests_checked_at", control.attribute(r.requests_checked_at));
        span.setAttribute("control_requests_queued", control.attribute(r.requests_queued));
        span.setAttribute("control_requests_skipped", control.attribute(r.requests_skipped));
        span.setAttribute("control_failures", control.attribute(r.failures));
        span.setAttribute("control_unauthorized", control.attribute(r.unauthorized));
        span.setAttribute("control_malformed", control.attribute(r.malformed));
        span.setAttribute("control_older_revision", control.attribute(r.older_revision));
    }
    span.setAttribute("control_misconfigured", self.config.control == .misconfigured);
    span.setAttribute("last_commit_at", c.last_commit_at.load(.monotonic));
    span.setAttribute("rss_kb", ingest.getRssKB());
}

const Role = struct {
    allocator: std.mem.Allocator,
    io: Io,
    config: config_mod.Config,
    options: Options,
    started: Io.Timestamp,
    owner: *Owner,
    /// The handle `/store/*` reads through, once `late` has opened it.
    reader: std.atomic.Value(?*store_mod.Reader) = .init(null),
    /// The relay cursor the store held when it was opened, read before
    /// the owner's first turn so the relay consumer never waits on one.
    stored_relay: ?u64,
    intake: intake_mod.Intake,
    labels: tails.LabelSink,
    plc: tails.PlcTail,
    resolver: identity.Resolver,
    work: identity_work.Work,
    bridge: ?*bridge_mod.Bridge = null,
    enricher: ?*enrich.Enricher = null,
    corpus: ?*corpus_mod.Scanner = null,
    controller: ?*control.Controller = null,
    bridge_stop: std.atomic.Value(bool) = .init(false),
    stopping: std.atomic.Value(bool) = .init(false),

    fn reached(self: *Role, stage: Stage) void {
        const elapsed = self.started.untilNow(self.io, .awake);
        log.info("start: {t} after {d} ms", .{ stage, elapsed.toMilliseconds() });
        const report = self.options.on_stage orelse return;
        report(self.options.context, stage, .{
            .owner = self.owner,
            .reader_open = self.reader.load(.acquire) != null,
            .corpus_scans = if (self.corpus) |scanner| scanner.report().scans else 0,
        });
    }

    /// What waits until the index is fed: the `/store/*` handle, whose
    /// open reads every table's metadata, and then the corpus scanner.
    fn late(self: *Role) void {
        self.io.sleep(Io.Duration.fromSeconds(@intCast(self.options.startup_seconds)), .awake) catch return;
        const reader = store_mod.Reader.open(self.allocator, self.io, Io.Dir.cwd(), self.config.store_dir, self.owner.options.store.layout) catch |err| {
            if (stopping()) return;
            log.err("store reader open failed: {s}, exiting for restart", .{@errorName(err)});
            fatal.exitForRestart();
        };
        self.reader.store(reader, .release);
        self.reached(.reader_opened);
        const scanner = self.corpus orelse return;
        self.reached(.corpus_started);
        scanner.run();
    }

    fn relay(self: *Role) void {
        const stored = self.stored_relay;
        const cursor = config_mod.resumeCursor(stored, self.config.bootstrap_cursor) orelse {
            log.err("the store holds no usable relay cursor and RELAY_BOOTSTRAP_CURSOR is not set; refusing to start live and skip replay", .{});
            fatal.exitForRestart();
        };
        log.info("following {s} after seq {d} ({s})", .{ relay_host, cursor, if (stored != null) "stored" else "bootstrap" });
        self.intake.resumeFrom(cursor);

        // One host, deliberately: a relay seq means nothing to another relay.
        // See the worker role's firehoseThread in main.zig.
        var client = zat.FirehoseClient.init(self.io, self.allocator, .{ .hosts = &.{relay_host}, .cursor = cursor });
        defer client.deinit();
        client.subscribe(&self.intake) catch |err| log.err("firehose subscribe failed: {}", .{err});
        if (stopping()) return;
        log.err("firehose subscription ended, exiting for restart", .{});
        fatal.exitForRestart();
    }

    fn watchdog(self: *Role) void {
        const started_at = self.owner.now();
        while (true) {
            self.io.sleep(Io.Duration.fromSeconds(60), .awake) catch return;
            const signals = &self.intake.signals;
            const wedge = intake_mod.wedged(
                self.owner.now(),
                started_at,
                signals.last_event_at.load(.monotonic),
                signals.paused.load(.monotonic),
                self.owner.counters.last_turn_at.load(.monotonic),
            ) orelse continue;
            log.err("watchdog: {t} (last event at {d}, last owner turn at {d}), exiting for restart", .{
                wedge, signals.last_event_at.load(.monotonic), self.owner.counters.last_turn_at.load(.monotonic),
            });
            fatal.exitForRestart();
        }
    }
};

fn setSocketTimeout(fd: std.posix.fd_t, seconds: u32) !void {
    const timeout = std.mem.toBytes(std.posix.timeval{ .sec = @intCast(seconds), .usec = 0 });
    try std.posix.setsockopt(fd, std.posix.SOL.SOCKET, std.posix.SO.RCVTIMEO, &timeout);
    try std.posix.setsockopt(fd, std.posix.SOL.SOCKET, std.posix.SO.SNDTIMEO, &timeout);
}

fn serveOne(self: *Role, stream: Io.net.Stream) void {
    defer stream.close(self.io);
    setSocketTimeout(stream.socket.handle, socket_timeout_seconds) catch |err| log.warn("failed to set socket timeout: {}", .{err});
    var read_buffer: [16 * 1024]u8 = undefined;
    var write_buffer: [4096]u8 = undefined;
    var reader = stream.reader(self.io, &read_buffer);
    var writer = stream.writer(self.io, &write_buffer);
    var server = std.http.Server.init(&reader.interface, &writer.interface);
    var request = server.receiveHead() catch return;
    if (http_guard.rejectUnframedBody(&request)) return;

    var arena: std.heap.ArenaAllocator = .init(self.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    var authorization: ?[]const u8 = null;
    var headers = request.iterateHeaders();
    while (headers.next()) |header| {
        if (std.ascii.eqlIgnoreCase(header.name, "authorization")) authorization = a.dupe(u8, header.value) catch return;
    }
    const method = request.head.method;
    const target = a.dupe(u8, request.head.target) catch return;
    var body: []const u8 = "";
    if (method.requestHasBody()) {
        var body_buffer: [4096]u8 = undefined;
        const body_reader = request.readerExpectContinue(&body_buffer) catch return;
        body = body_reader.allocRemaining(a, .limited(http.max_body_bytes)) catch {
            request.respond("{\"error\":\"body too large\"}", .{ .status = .payload_too_large, .keep_alive = false }) catch {};
            return;
        };
    }

    const context: http.Context = .{
        .owner = self.owner,
        .reader = self.reader.load(.acquire),
        .intake = &self.intake.signals,
        .labels_ignored = &self.labels.ignored,
        .read_secret = self.config.read_secret,
        .bridge = self.bridge,
        .enricher = self.enricher,
        .corpus = self.corpus,
        .control = self.controller,
        .control_problem = switch (self.config.control) {
            .misconfigured => |problem| problem,
            .off, .on => null,
        },
    };
    const response = http.respond(&context, a, method, target, authorization, body) catch |err| blk: {
        log.warn("request failed: {s}", .{@errorName(err)});
        break :blk http.Response{ .status = .internal_server_error, .body = "{\"error\":\"internal error\"}" };
    };
    request.respond(response.body, .{
        .status = response.status,
        .extra_headers = &.{.{ .name = "content-type", .value = "application/json" }},
    }) catch {};
}

/// Run the store role on a listening socket until SIGTERM closes it.
pub fn serve(allocator: std.mem.Allocator, io: Io, listener: *Io.net.Server) !void {
    const config = config_mod.load(cGetenv) catch |err| {
        log.err("store role configuration: {s}", .{@errorName(err)});
        return err;
    };
    return run(allocator, io, listener, config, .{ .on_stopped = exitClean });
}

fn exitClean(_: ?*anyopaque) void {
    fatal.exitClean();
}

/// The role under a configuration the caller built, until the listening
/// socket is shut down.
pub fn run(allocator: std.mem.Allocator, io: Io, listener: *Io.net.Server, config: config_mod.Config, options: Options) !void {
    const started = Io.Timestamp.now(io, .awake);
    log.info("store at {s}, reads {s}{s}", .{
        config.store_dir,
        if (config.read_secret != null) "on" else "off (STORE_READ_SECRET not set)",
        if (config.offline) ", offline: no upstream connections" else "",
    });

    const owner = Owner.open(allocator, io, Io.Dir.cwd(), config.store_dir, .{
        .on_fatal = exitOnStoreFailure,
        .on_heartbeat = heartbeat,
        .repair_limit = config.handle_repair_per_step,
        .startup_seconds = options.startup_seconds,
    }) catch |err| {
        log.err("store open failed: {s}", .{@errorName(err)});
        return err;
    };
    defer owner.close();

    const stored_relay = blk: {
        var arena: std.heap.ArenaAllocator = .init(allocator);
        defer arena.deinit();
        const store = try owner.acquire();
        defer owner.release();
        break :blk (try store.cursors(arena.allocator())).relay;
    };

    var self: Role = .{
        .allocator = allocator,
        .io = io,
        .config = config,
        .options = options,
        .started = started,
        .owner = owner,
        .stored_relay = stored_relay,
        .intake = .{ .gpa = allocator, .owner = owner, .options = .{ .on_fatal = exitOnIntakeFailure } },
        .labels = .{ .gpa = allocator, .owner = owner },
        .plc = .{ .gpa = allocator, .io = io, .owner = owner },
        .resolver = identity.Resolver.init(allocator, io, "", ""),
        .work = undefined,
    };
    defer self.intake.deinit();
    defer if (self.reader.load(.acquire)) |reader| reader.close();
    self.work = .{ .gpa = allocator, .io = io, .owner = owner, .resolver = &self.resolver };
    role = &self;
    defer role = null;
    self.reached(.store_opened);

    // The index bridge: its own thread, its own reader, its own HTTP client.
    var bridge_source: index_bridge.StoreSource = undefined;
    var bridge_http: zat.HttpTransport = undefined;
    var bridge_endpoint: bridge_mod.wire.Http = undefined;
    var bridge: bridge_mod.Bridge = undefined;
    var authorization_buf: [512]u8 = undefined;
    if (config.index) |index| {
        bridge_source = index_bridge.StoreSource.open(allocator, owner) catch |err| {
            log.err("bridge reader open failed: {s}", .{@errorName(err)});
            return err;
        };
        bridge_http = zat.HttpTransport.init(io, allocator);
        bridge_http.keep_alive = true;
        bridge_endpoint = .{
            .http = &bridge_http,
            .base_url = index.admin_url,
            .authorization = std.fmt.bufPrint(&authorization_buf, "Bearer {s}", .{index.admin_secret}) catch return error.IndexSecretTooLong,
        };
        bridge = bridge_mod.Bridge.init(allocator, io, .{ .source_id = index.source_id, .adopt_foreign_cursor = index.adopt_cursor, .poll_interval_ms = index.push_interval_ms }, bridge_source.interface(), bridge_endpoint.transport());
        self.bridge = &bridge;
    }
    defer if (config.index != null) {
        bridge.deinit();
        bridge_http.deinit();
        bridge_source.close();
    };

    // Enrichment: its own thread and its own HTTP client; see enrich.zig.
    var enrich_http: enrich.Http = undefined;
    var enrich_timer: enrich.WallTimer = .{ .io = io };
    var enricher: enrich.Enricher = undefined;
    if (config.enrich) |enrich_options| {
        enrich_http = enrich.Http.init(io, allocator);
        enricher = .{ .gpa = allocator, .owner = owner, .transport = enrich_http.transport(), .timer = enrich_timer.timer(), .options = enrich_options };
        enricher.prng = .init(@truncate(@as(u96, @bitCast(Io.Timestamp.now(io, .real).nanoseconds))));
        self.enricher = &enricher;
    }
    defer if (config.enrich != null) enrich_http.deinit();

    // The control loop: its own thread and its own HTTP client; see control.zig.
    var control_http: control.Http = undefined;
    var control_timer: enrich.WallTimer = .{ .io = io };
    var controller: control.Controller = undefined;
    switch (config.control) {
        .off => {},
        .misconfigured => |problem| log.err("the control loop is off: {s}. Moderation policy changes and indexing requests made at the Worker will not reach this slot", .{problem}),
        .on => |control_options| {
            control_http = control.Http.init(io, allocator);
            controller = .{ .gpa = allocator, .owner = owner, .transport = control_http.transport(), .timer = control_timer.timer(), .options = control_options };
            controller.prng = .init(@truncate(@as(u96, @bitCast(Io.Timestamp.now(io, .real).nanoseconds))));
            controller.start() catch |err| {
                log.err("control loop start failed: {s}", .{@errorName(err)});
                control_http.deinit();
                return err;
            };
            self.controller = &controller;
        },
    }
    defer if (self.controller != null) {
        controller.deinit();
        control_http.deinit();
    };

    var scanner: corpus_mod.Scanner = .{
        .gpa = allocator,
        .io = io,
        .root = Io.Dir.cwd(),
        .path = config.store_dir,
        .layout = owner.options.store.layout,
        .interval_seconds = config.corpus_scan_seconds,
        .delay_seconds = options.corpus_delay_seconds -| options.startup_seconds,
    };
    if (config.corpus_scan_seconds > 0) self.corpus = &scanner;

    // Everything the heartbeat reads through `self` is in place, so the
    // threads can start, in the order `Stage` gives.
    var owner_task = try io.concurrent(Owner.run, .{owner});
    defer {
        owner.stop();
        owner_task.await(io);
    }
    var tasks: Io.Group = .init;
    if (config.index) |index| {
        try tasks.concurrent(io, bridge_mod.Bridge.run, .{ &bridge, &self.bridge_stop });
        log.info("index bridge to {s} as source {s}, resting {d} ms between pushes", .{ index.admin_url, index.source_id, index.push_interval_ms });
    }
    self.reached(.bridge_started);

    var pool: identity_work.Pool = undefined;
    var have_pool = false;
    if (!config.offline) {
        try tasks.concurrent(io, Role.relay, .{&self});
        pool = try identity_work.Pool.init(allocator, .{ .queue_capacity = 64, .workers = 8, .ctx = &self.work, .process = identity_work.Work.process });
        have_pool = true;
        try pool.start(io);
        try tasks.concurrent(io, identity_work.Work.drain, .{ &self.work, &pool });
        try tasks.concurrent(io, tails.PlcTail.run, .{&self.plc});
        try tasks.concurrent(io, tails.LabelSink.run, .{ &self.labels, io });
        try tasks.concurrent(io, Role.watchdog, .{&self});
    }
    defer if (have_pool) pool.deinit();
    self.reached(.intake_started);

    if (self.enricher) |starting| try tasks.concurrent(io, enrich.Enricher.run, .{starting});
    if (self.controller) |starting| try tasks.concurrent(io, control.Controller.run, .{starting});
    try tasks.concurrent(io, Role.late, .{&self});

    while (true) {
        const stream = listener.accept(io) catch |err| switch (err) {
            error.SocketNotListening => break,
            else => {
                log.err("accept error: {}", .{err});
                continue;
            },
        };
        if (options.stop) |stop| if (stop.load(.acquire)) {
            stream.close(io);
            break;
        };
        serveOne(&self, stream);
    }

    log.info("SIGTERM received, stopping the store role", .{});
    self.stopping.store(true, .release);
    self.bridge_stop.store(true, .release);
    if (self.enricher) |running| running.stop();
    if (self.controller) |running| running.stop();
    scanner.stop();
    owner.stop();
    // Awaited, not canceled: a cancellation lands inside a commit as a
    // failed write, and the owner answers that by reopening the store.
    owner_task.await(io);
    log.info("the owner stopped after its last commit; whatever was still queued is replayed from the stored cursors", .{});
    // Nothing can write now. The upstream clients take a cancellation for
    // a dropped connection and reconnect, so canceling `tasks` in
    // production would wait on them for ever: `serve` exits here instead.
    if (options.on_stopped) |stopped| stopped(options.context);
    tasks.cancel(io);
    if (have_pool) pool.shutdown(io);
    log.info("shutdown complete", .{});
}
