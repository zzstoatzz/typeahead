const std = @import("std");
const mem = std.mem;
const Allocator = mem.Allocator;
const zat = @import("zat");
const logfire = @import("logfire");
const log_route = @import("log_route.zig");
const fatal = @import("fatal.zig");
const HttpTransport = zat.HttpTransport;
const TursoClient = @import("db/TursoClient.zig");
const turso_schema = @import("db/turso_schema.zig");
const http_guard = @import("http_guard.zig");
const ingest = @import("ingest.zig");
const identity = @import("identity.zig");
const plc_tail = @import("plc_tail.zig");
const label_tail = @import("label_tail.zig");
const store_role = @import("store_process/role.zig");
const store_bootstrap = @import("store_process/bootstrap.zig");

const log = std.log.scoped(.ingester);

// override debug_io with a real threaded implementation so Io.Mutex,
// io.sleep, and network ops work concurrently across threads.
// without this, std.Options.debug_io uses global_single_threaded which
// silently serializes all I/O.
var app_threaded_io: std.Io.Threaded = undefined;
pub const std_options_debug_threaded_io: ?*std.Io.Threaded = &app_threaded_io;
const io = std.Options.debug_io;

// every std.log.* call reaches stderr for `fly logs`; log_route.zig decides
// which also ship to logfire as log records. a panic's message is flushed
// before the process dies. docs/operations.md "Telemetry".
pub const std_options: std.Options = .{
    .log_level = .info,
    .logFn = log_route.logFn,
};
pub const panic = std.debug.FullPanic(fatal.panicHook);

const SOCKET_TIMEOUT_SECS = 5;

// graceful shutdown: SIGTERM → close listening socket → accept breaks → drain tasks
var listen_fd: std.atomic.Value(std.posix.fd_t) = std.atomic.Value(std.posix.fd_t).init(-1);

fn handleSigterm(_: std.posix.SIG) callconv(.c) void {
    const fd = listen_fd.load(.acquire);
    if (fd >= 0) _ = std.posix.system.shutdown(fd, 2); // SHUT_RDWR
}

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return mem.span(p);
    return null;
}

const FirehoseArgs = struct {
    allocator: Allocator,
    config: ingest.Config,
    transport: *HttpTransport,
};

fn firehoseThread(args: FirehoseArgs) void {
    var handler = ingest.IngestHandler.init(
        args.allocator,
        args.config,
        args.transport,
    ) catch |err| {
        log.err("handler init failed: {}, exiting for restart", .{err});
        fatal.exitForRestart();
    };
    defer handler.deinit();

    // The relay's firehose, not jetstream. jetstream is a lossy projection: a 7-day
    // replay returned 0 events for all 6 pds.zat.dev repos while the relay was
    // rev-current with that PDS and re-emitted its commits live within seconds
    // (verified 2026-07-28). The loss is silent and partial — most hosts look
    // fine — so it cannot be detected by spot-checking.
    //
    // There is no wantedCollections equivalent here; the allowlist moved into
    // ingest.wantedCollection and every frame is now decoded before filtering.
    //
    // ONE host, deliberately. zat's default_hosts lists four relays, but
    // northamerica/europe/asia.firehose.network are INDEPENDENT relays with
    // independent `seq` spaces (separate operators, separate A records) — not
    // mirrors. A relay seq is only meaningful to the relay that issued it, so
    // failing over mid-stream and resuming a stored cursor against a different
    // relay lands at an arbitrary point: silent skip or enormous replay.
    //
    // This is a REGRESSION RISK INTRODUCED BY THE CUTOVER, not a pre-existing
    // one: jetstream's cursor was a µs timestamp, which is host-independent, so
    // rotating hosts there was safe. Multi-host resume would require a cursor
    // per host; pinning is the cheaper correct answer until that's worth it.
    const resume_cursor = ingest.fetchCursor(args.transport, args.config) orelse {
        log.err("no valid persisted relay cursor; refusing to start live and skip replay", .{});
        fatal.exitForRestart();
    };
    handler.flushed_cursor = resume_cursor;
    handler.pending_cursor = resume_cursor;
    var client = zat.FirehoseClient.init(io, args.allocator, .{
        .hosts = &.{"bsky.network"},
        .cursor = resume_cursor,
    });
    defer client.deinit();

    client.subscribe(&handler) catch |err| {
        log.err("firehose subscribe failed: {}", .{err});
    };
    // subscribe reconnects internally and only returns on cancellation or a
    // terminal error — either way this role is over, so let fly restart us.
    log.err("firehose subscription ended, exiting for restart", .{});
    fatal.exitForRestart();
}

fn setSocketTimeout(fd: std.posix.fd_t, secs: u32) !void {
    const timeout = mem.toBytes(std.posix.timeval{
        .sec = @intCast(secs),
        .usec = 0,
    });
    try std.posix.setsockopt(fd, std.posix.SOL.SOCKET, std.posix.SO.RCVTIMEO, &timeout);
    try std.posix.setsockopt(fd, std.posix.SOL.SOCKET, std.posix.SO.SNDTIMEO, &timeout);
}

pub fn main() !void {
    const allocator = std.heap.smp_allocator;
    app_threaded_io = std.Io.Threaded.init(allocator, .{
        .stack_size = 8 * 1024 * 1024, // 8MB (default 16MB is wasteful on 256MB VM)
        .async_limit = std.Io.Limit.limited(8),
    });

    // Explicit role, no default: a binary started without MODE=ingester (say,
    // an old search or indexer machine on a new image) must not quietly start
    // consuming the firehose and migrating Turso.
    const mode = cGetenv("MODE") orelse {
        log.err("MODE not set — must be ingester, store or store-bootstrap", .{});
        return error.RoleRequired;
    };
    if (mem.eql(u8, mode, "store-bootstrap")) {
        store_bootstrap.run(allocator, io) catch |err| {
            log.err("store bootstrap failed: {s}", .{@errorName(err)});
            return err;
        };
        return;
    }
    const store_mode = mem.eql(u8, mode, "store");
    if (!store_mode and !mem.eql(u8, mode, "ingester")) {
        log.err("MODE='{s}' unrecognized — must be ingester, store or store-bootstrap", .{mode});
        return error.RoleRequired;
    }

    // service_name distinguishes app A vs app B in logfire traces.
    // Fly injects FLY_APP_NAME so the two apps tag distinctly without manual config.
    const service_name = cGetenv("FLY_APP_NAME") orelse "typeahead-dev";

    log.info("role: {s} service_name={s}", .{ mode, service_name });

    // service.version is the deployed image ref (fly sets FLY_IMAGE_REF), so
    // a regression in logfire can be tied to a deploy without a build step
    _ = logfire.configure(.{
        .service_name = service_name,
        .pending_spans = false,
        .service_version = cGetenv("FLY_IMAGE_REF") orelse "dev",
        .environment = if (cGetenv("FLY_APP_NAME") != null) "production" else "development",
    }) catch |err| {
        log.err("logfire configure failed: {}", .{err});
    };
    // SIGTERM drain below ends with this; watchdog exits flush on their own
    defer logfire.shutdown();

    // start HTTP server FIRST so Fly proxy doesn't timeout
    const port: u16 = blk: {
        const port_str = cGetenv("PORT") orelse "8080";
        break :blk std.fmt.parseInt(u16, port_str, 10) catch 8080;
    };

    var addr = std.Io.net.IpAddress{ .ip4 = .{ .bytes = .{ 0, 0, 0, 0 }, .port = port } };
    var srv = std.Io.net.IpAddress.listen(&addr, io, .{ .reuse_address = true }) catch |err| {
        log.err("listen failed: {}", .{err});
        return err;
    };
    defer srv.deinit(io);

    log.info("listening on port {d}", .{port});

    // register SIGTERM for graceful shutdown (Fly sends SIGTERM before stopping)
    listen_fd.store(srv.socket.handle, .release);
    std.posix.sigaction(.TERM, &.{
        .handler = .{ .handler = handleSigterm },
        .mask = mem.zeroes(std.posix.sigset_t),
        .flags = 0,
    }, null);

    // The store role keeps its data on local disk: no Turso, no Worker.
    if (store_mode) return store_role.serve(allocator, io, &srv);

    // Run Turso schema migrations BEFORE spawning the firehose — ingest assumes
    // the schema exists. On baselined prod this is a few sub-second SELECTs +
    // INSERT OR IGNORE; on a fresh DB it creates everything from migration 001.
    {
        var schema_client = TursoClient.init(allocator) catch |err| {
            log.err("turso client init for schema failed: {}, aborting boot", .{err});
            return err;
        };
        defer schema_client.deinit();
        turso_schema.init(allocator, &schema_client) catch |err| {
            log.err("turso schema init failed: {}, aborting boot", .{err});
            return err;
        };
    }

    // task group for long-lived background tasks
    var tasks: std.Io.Group = .init;

    // firehose-only state (transport, resolver, identity pool) — declared at
    // outer scope so the spawned task's pointers stay valid for main()'s lifetime
    var transport: HttpTransport = undefined;
    var have_transport = false;
    defer if (have_transport) transport.deinit();

    var resolver: identity.Resolver = undefined;
    var have_resolver = false;
    defer if (have_resolver) resolver.deinit();

    var identity_pool: identity.Pool = undefined;
    var have_identity_pool = false;
    defer if (have_identity_pool) identity_pool.deinit(io);

    var tail: plc_tail.Tail = undefined;
    var labels: label_tail.Tail = undefined;

    {
        const config = ingest.getConfig();
        log.info("typeahead ingester → {s}", .{config.worker_url});

        // keep-alive matters here: flushes run every ≤5s and a cold TLS
        // handshake per POST (~150-400ms) blocks the firehose consumer loop —
        // at peak firehose rate that pushed catch-up below 1× real-time.
        // stale-connection failures are covered by flush retry + the watchdog.
        transport = HttpTransport.init(io, allocator);
        transport.keep_alive = true;
        have_transport = true;

        // identity resolution pool, fed only by the durable identity_queue drain:
        // each leased DID is re-resolved from its authority; see identity.zig.
        resolver = identity.Resolver.init(allocator, io, config.worker_url, config.secret);
        have_resolver = true;

        identity_pool = identity.Pool.init(allocator, .{
            .queue_capacity = 64,
            .workers = 8,
            .ctx = &resolver,
            .process = identity.Resolver.process,
        }) catch |err| {
            log.err("identity pool init failed: {}", .{err});
            return err;
        };
        have_identity_pool = true;

        identity_pool.start(io) catch |err| {
            log.err("identity pool start failed: {}", .{err});
            return err;
        };

        tasks.concurrent(io, identity.Resolver.drain, .{ &resolver, &identity_pool }) catch |err| {
            log.err("identity drain task failed: {}", .{err});
            return err;
        };

        // recovery feed for did:plc identity changes the relay never signalled; see plc_tail.zig
        tail = .{ .allocator = allocator, .io = io, .worker_url = config.worker_url, .worker_secret = config.secret };
        tasks.concurrent(io, plc_tail.Tail.run, .{&tail}) catch |err| {
            log.err("plc tail task failed: {}", .{err});
            return err;
        };

        // moderation labels from Bluesky's labeler stream; see label_tail.zig
        labels = .{ .allocator = allocator, .io = io, .worker_url = config.worker_url, .worker_secret = config.secret };
        tasks.concurrent(io, label_tail.Tail.run, .{&labels}) catch |err| {
            log.err("label tail task failed: {}", .{err});
            return err;
        };

        tasks.concurrent(io, firehoseThread, .{FirehoseArgs{
            .allocator = allocator,
            .config = config,
            .transport = &transport,
        }}) catch |err| {
            log.err("firehose task failed: {}", .{err});
        };

        tasks.concurrent(io, ingest.watchdogLoop, .{}) catch |err| {
            log.err("watchdog task failed to start: {}", .{err});
        };
    }

    // main thread: HTTP accept loop serving /health; everything else 503s
    while (true) {
        const stream = srv.accept(io) catch |err| switch (err) {
            error.SocketNotListening => break,
            else => {
                log.err("accept error: {}", .{err});
                continue;
            },
        };

        setSocketTimeout(stream.socket.handle, SOCKET_TIMEOUT_SECS) catch |err| {
            log.warn("failed to set socket timeout: {}", .{err});
        };

        // /health is the only reader of the resolver and tail counters: a DID
        // silently dropped by the resolver has no other observable signal
        // (that is how birdlover.selfhosted.social went missing unnoticed).
        var read_buffer: [1024]u8 = undefined;
        var write_buffer: [1024]u8 = undefined;
        var reader = stream.reader(io, &read_buffer);
        var writer = stream.writer(io, &write_buffer);
        var http_srv = std.http.Server.init(&reader.interface, &writer.interface);
        var request = http_srv.receiveHead() catch {
            stream.close(io);
            continue;
        };
        if (http_guard.rejectUnframedBody(&request)) {
            stream.close(io);
            continue;
        }

        const target = request.head.target;
        const path = if (std.mem.indexOfScalar(u8, target, '?')) |qi| target[0..qi] else target;

        if (std.mem.eql(u8, path, "/health")) {
            var buf: [2048]u8 = undefined;
            const ic = identity_pool.counters(io);
            const body = std.fmt.bufPrint(&buf,
                \\{{"status":"ok","role":"ingester","rss_kb":{d},"identity":{{"leased":{d},"lease_failed":{d},"accepted":{d},"dropped":{d},"processed":{d},"queued":{d},"verified":{d},"unverified":{d},"no_handle":{d},"did_failed":{d},"retried":{d},"post_failed":{d}}},"plc":{{"pages":{d},"operations":{d},"failed":{d}}},"labels":{{"frames":{d},"adopted":{d},"malformed":{d},"posts":{d},"post_failed":{d},"cursor":{d},"newest_cts":{d}}},"ingest":{{"queued":{d},"deletes_queued":{d},"backpressure_active":{s},"backpressure_count":{d},"last_write_at":{d},"source_event_at":{d}}}}}
            , .{ ingest.getRssKB(), identity.stats.leased.load(.monotonic), identity.stats.lease_failed.load(.monotonic), ic.accepted, ic.dropped, ic.processed, ic.queued, identity.stats.verified.load(.monotonic), identity.stats.unverified.load(.monotonic), identity.stats.no_handle.load(.monotonic), identity.stats.did_failed.load(.monotonic), identity.stats.retried.load(.monotonic), identity.stats.post_failed.load(.monotonic), plc_tail.stats.pages.load(.monotonic), plc_tail.stats.operations.load(.monotonic), plc_tail.stats.failed.load(.monotonic), label_tail.stats.frames.load(.monotonic), label_tail.stats.adopted.load(.monotonic), label_tail.stats.malformed.load(.monotonic), label_tail.stats.posts.load(.monotonic), label_tail.stats.post_failed.load(.monotonic), label_tail.stats.committed.load(.monotonic), label_tail.stats.newest_cts.load(.monotonic), ingest.queued_ingest.load(.monotonic), ingest.queued_deletes.load(.monotonic), if (ingest.backpressure_active.load(.monotonic)) "true" else "false", ingest.backpressure_count.load(.monotonic), ingest.last_flush_ok_at.load(.monotonic), ingest.source_event_at.load(.monotonic) }) catch "{\"status\":\"ok\",\"role\":\"ingester\"}";

            request.respond(body, .{
                .status = .ok,
                .extra_headers = &.{
                    .{ .name = "content-type", .value = "application/json" },
                },
            }) catch {};
        } else {
            request.respond("{\"error\":\"search unavailable\"}", .{
                .status = .service_unavailable,
                .extra_headers = &.{
                    .{ .name = "content-type", .value = "application/json" },
                },
            }) catch {};
        }
        stream.close(io);
    }

    log.info("SIGTERM received, draining tasks…", .{});
    tasks.cancel(io);
    if (have_identity_pool) {
        log.info("draining identity resolver…", .{});
        identity_pool.shutdown(io) catch |err| log.warn("identity pool drain interrupted: {}", .{err});
    }
    log.info("shutdown complete", .{});
}
