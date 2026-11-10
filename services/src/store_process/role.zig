//! `MODE=store`: the ingester's second role. It follows the relay, the
//! labeler and the PLC export, resolves identities, and commits everything
//! to a zkv store on local disk. It opens no connection to Turso or the
//! Worker. Configuration is in `config.zig`, the concurrency model in
//! `owner.zig`, the HTTP surface in `http.zig`.

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
const bridge_mod = @import("../bridge/root.zig");
const Io = std.Io;
const Owner = owner_mod.Owner;

const log = std.log.scoped(.store_role);

const socket_timeout_seconds = 5;
const relay_host = "bsky.network";

var role: ?*Role = null;

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return std.mem.span(p);
    return null;
}

fn exitOnStoreFailure() void {
    log.err("the store would not commit after repeated reopens, exiting for restart", .{});
    fatal.exitForRestart();
}

fn exitOnIntakeFailure() void {
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
    span.setAttribute("relay_cursor", @as(i64, @intCast(health.cursors.relay orelse 0)));
    span.setAttribute("relay_handed_cursor", self.intake.signals.handed_cursor.load(.monotonic));
    span.setAttribute("relay_events", self.intake.signals.events.load(.monotonic));
    span.setAttribute("relay_paused", self.intake.signals.paused.load(.monotonic));
    span.setAttribute("source_event_at", self.intake.signals.source_event_at.load(.monotonic));
    span.setAttribute("labeler_cursor", @as(i64, @intCast(health.cursors.labeler orelse 0)));
    span.setAttribute("change_log_head", health.cursors.next_change);
    span.setAttribute("change_log_floor", health.cursors.change_floor);
    span.setAttribute("queued_changes", c.queued_changes.load(.monotonic));
    span.setAttribute("identity_pending", health.identity_pending);
    span.setAttribute("identity_due", health.identity_due);
    span.setAttribute("refresh_pending", health.refresh_pending);
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
        span.setAttribute("bridge_position", @as(i64, @intCast(b.position orelse 0)));
        span.setAttribute("bridge_index_position", b.index_position);
        span.setAttribute("bridge_lag", b.lag);
        span.setAttribute("bridge_pushes", b.pushes);
        span.setAttribute("bridge_applied", b.applied);
        span.setAttribute("bridge_errors", b.errors);
        span.setAttribute("bridge_conflicts", b.conflicts);
        span.setAttribute("bridge_resyncs_started", b.resyncs_started);
        span.setAttribute("bridge_last_push_at", b.last_push_at);
    }
    span.setAttribute("last_commit_at", c.last_commit_at.load(.monotonic));
    span.setAttribute("rss_kb", ingest.getRssKB());
}

const Role = struct {
    allocator: std.mem.Allocator,
    io: Io,
    config: config_mod.Config,
    owner: *Owner,
    reader: *store_mod.Reader,
    intake: intake_mod.Intake,
    labels: tails.LabelSink,
    plc: tails.PlcTail,
    resolver: identity.Resolver,
    work: identity_work.Work,
    bridge: ?*bridge_mod.Bridge = null,
    bridge_stop: std.atomic.Value(bool) = .init(false),

    fn relay(self: *Role) void {
        const stored = blk: {
            var arena: std.heap.ArenaAllocator = .init(self.allocator);
            defer arena.deinit();
            const store = self.owner.acquire() catch break :blk null;
            defer self.owner.release();
            break :blk (store.cursors(arena.allocator()) catch break :blk null).relay;
        };
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
        log.err("firehose subscription ended, exiting for restart", .{});
        fatal.exitForRestart();
    }

    fn watchdog(self: *Role) void {
        while (true) {
            self.io.sleep(Io.Duration.fromSeconds(60), .awake) catch return;
            const signals = &self.intake.signals;
            const wedge = intake_mod.wedged(
                self.owner.now(),
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
        .reader = self.reader,
        .intake = &self.intake.signals,
        .labels_ignored = &self.labels.ignored,
        .read_secret = self.config.read_secret,
        .bridge = self.bridge,
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
    log.info("store at {s}, reads {s}{s}", .{
        config.store_dir,
        if (config.read_secret != null) "on" else "off (STORE_READ_SECRET not set)",
        if (config.offline) ", offline: no upstream connections" else "",
    });

    const owner = Owner.open(allocator, io, Io.Dir.cwd(), config.store_dir, .{
        .on_fatal = exitOnStoreFailure,
        .on_heartbeat = heartbeat,
    }) catch |err| {
        log.err("store open failed: {s}", .{@errorName(err)});
        return err;
    };
    defer owner.close();

    const reader = store_mod.Reader.open(allocator, io, Io.Dir.cwd(), config.store_dir, owner.options.store.layout) catch |err| {
        log.err("store reader open failed: {s}", .{@errorName(err)});
        return err;
    };
    defer reader.close();

    var self: Role = .{
        .allocator = allocator,
        .io = io,
        .config = config,
        .owner = owner,
        .reader = reader,
        .intake = .{ .gpa = allocator, .owner = owner, .options = .{ .on_fatal = exitOnIntakeFailure } },
        .labels = .{ .gpa = allocator, .owner = owner },
        .plc = .{ .gpa = allocator, .io = io, .owner = owner },
        .resolver = identity.Resolver.init(allocator, io, "", ""),
        .work = undefined,
    };
    defer self.intake.deinit();
    self.work = .{ .gpa = allocator, .io = io, .owner = owner, .resolver = &self.resolver };
    role = &self;
    defer role = null;

    var tasks: Io.Group = .init;
    try tasks.concurrent(io, Owner.run, .{owner});

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
        bridge = bridge_mod.Bridge.init(allocator, io, .{ .source_id = index.source_id, .adopt_foreign_cursor = index.adopt_cursor }, bridge_source.interface(), bridge_endpoint.transport());
        self.bridge = &bridge;
        try tasks.concurrent(io, bridge_mod.Bridge.run, .{ &bridge, &self.bridge_stop });
        log.info("index bridge to {s} as source {s}", .{ index.admin_url, index.source_id });
    }
    defer if (config.index != null) {
        bridge.deinit();
        bridge_http.deinit();
        bridge_source.close();
    };

    var pool: identity_work.Pool = undefined;
    var have_pool = false;
    if (!config.offline) {
        pool = try identity_work.Pool.init(allocator, .{ .queue_capacity = 64, .workers = 8, .ctx = &self.work, .process = identity_work.Work.process });
        have_pool = true;
        try pool.start(io);
        try tasks.concurrent(io, identity_work.Work.drain, .{ &self.work, &pool });
        try tasks.concurrent(io, tails.PlcTail.run, .{&self.plc});
        try tasks.concurrent(io, tails.LabelSink.run, .{ &self.labels, io });
        try tasks.concurrent(io, Role.relay, .{&self});
        try tasks.concurrent(io, Role.watchdog, .{&self});
    }
    defer if (have_pool) pool.deinit();

    while (true) {
        const stream = listener.accept(io) catch |err| switch (err) {
            error.SocketNotListening => break,
            else => {
                log.err("accept error: {}", .{err});
                continue;
            },
        };
        serveOne(&self, stream);
    }

    log.info("SIGTERM received, stopping the store role", .{});
    self.bridge_stop.store(true, .release);
    owner.stop();
    tasks.cancel(io);
    if (have_pool) pool.shutdown(io);
    log.info("shutdown complete", .{});
}
