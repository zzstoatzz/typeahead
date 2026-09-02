const std = @import("std");
const mem = std.mem;
const Allocator = mem.Allocator;
const zat = @import("zat");
const logfire = @import("logfire");
const HttpTransport = zat.HttpTransport;
const LocalDb = @import("db/LocalDb.zig");
const TursoClient = @import("db/TursoClient.zig");
const sync = @import("db/sync.zig");
const turso_schema = @import("db/turso_schema.zig");
const server = @import("server.zig");
const ingest = @import("ingest.zig");
const identity = @import("identity.zig");
const index_builder = @import("index/builder.zig");
const promote_runner = @import("index/promote_runner.zig");

const log = std.log.scoped(.ingester);

// override debug_io with a real threaded implementation so Io.Mutex,
// io.sleep, and network ops work concurrently across threads.
// without this, std.Options.debug_io uses global_single_threaded which
// silently serializes all I/O.
var app_threaded_io: std.Io.Threaded = undefined;
pub const std_options_debug_threaded_io: ?*std.Io.Threaded = &app_threaded_io;
const io = std.Options.debug_io;

// every std.log.* call ships to logfire as a log record under the current
// span (and still reaches stderr for `fly logs`); a panic's message is
// flushed before the process dies. docs/operations.md "observability".
pub const std_options: std.Options = .{
    .log_level = .info,
    .logFn = logfire.logFn,
};
pub const panic = std.debug.FullPanic(logfire.panicFn);

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
    identity_pool: ?*identity.Pool,
};

fn firehoseThread(args: FirehoseArgs) void {
    var handler = ingest.IngestHandler.init(
        args.allocator,
        args.config,
        args.transport,
        args.identity_pool,
    ) catch |err| {
        log.err("handler init failed: {}, exiting for restart", .{err});
        logfire.flush();
        std.process.exit(1);
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
    var client = zat.FirehoseClient.init(io, args.allocator, .{
        .hosts = &.{"bsky.network"},
        .cursor = ingest.fetchCursor(args.transport, args.config),
    });
    defer client.deinit();

    client.subscribe(&handler) catch |err| {
        log.err("firehose subscribe failed: {}", .{err});
    };
    // subscribe reconnects internally and only returns on cancellation or a
    // terminal error — either way this role is over, so let fly restart us.
    log.err("firehose subscription ended, exiting for restart", .{});
    logfire.flush();
    std.process.exit(1);
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
        .async_limit = std.Io.Limit.limited(8), // bounded HTTP handler pool
    });

    // Process role — explicit and required, no default. A role running on the
    // wrong box is invisible until it competes for resources: the ingester ran
    // the search app's full-corpus sync for months because roles used to
    // default all-on (found 2026-06-12, mid-incident).
    //   ingester — relay's firehose → worker, owns Turso schema migrations
    //   search   — snapshot promote + Turso → overlay projection + HTTP search
    //   indexer  — offline snapshot builder, runs once and exits
    const Role = enum { ingester, search, indexer };
    const role: Role = blk: {
        const mode = cGetenv("MODE") orelse {
            log.err("MODE not set — must be one of: ingester, search, indexer", .{});
            return error.RoleRequired;
        };
        break :blk std.meta.stringToEnum(Role, mode) orelse {
            log.err("MODE='{s}' unrecognized — must be one of: ingester, search, indexer", .{mode});
            return error.RoleRequired;
        };
    };

    if (role == .indexer) {
        // Logfire stays unconfigured here — indexer is a short-lived
        // offline job, std.log to stderr is enough for now. Wire spans
        // later if cadence-wise insight matters.
        log.info("MODE=indexer — running snapshot builder", .{});
        return index_builder.run(allocator);
    }

    // service_name distinguishes app A vs app B in logfire traces.
    // Fly injects FLY_APP_NAME so the two apps tag distinctly without manual config.
    const service_name = cGetenv("FLY_APP_NAME") orelse "typeahead-dev";

    log.info("role: {s} service_name={s}", .{ @tagName(role), service_name });

    // service.version is the deployed image ref (fly sets FLY_IMAGE_REF), so
    // a regression in logfire can be tied to a deploy without a build step
    _ = logfire.configure(.{
        .service_name = service_name,
        .service_version = cGetenv("FLY_IMAGE_REF") orelse "dev",
        .environment = if (cGetenv("FLY_APP_NAME") != null) "production" else "development",
    }) catch |err| {
        log.err("logfire configure failed: {}", .{err});
    };
    // SIGTERM drain below ends with this; watchdog exits flush on their own
    defer logfire.shutdown();

    // local SQLite holds the search role's overlay + sync state (the snapshot
    // is attached to it). the ingester has no local consumer and doesn't open
    // one — its HTTP listener serves the health-only fallback.
    var local_db = LocalDb.init(allocator);
    var local_db_ptr: ?*LocalDb = null;
    defer local_db.deinit();
    if (role == .search) {
        local_db.open() catch |err| {
            log.err("local db failed to open: {} — search role requires it, aborting boot", .{err});
            return err;
        };
        if (local_db.conn == null) {
            log.err("local db unavailable — search role requires it, aborting boot", .{});
            return error.LocalDbRequired;
        }
        local_db_ptr = &local_db;
    }

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

    // Run Turso schema migrations BEFORE spawning sync/firehose — sync queries
    // turso assuming the schema exists. On baselined prod this is a few sub-
    // second SELECTs + INSERT OR IGNORE; on a fresh DB it creates everything
    // from migration 001. The search service must NOT run these.
    if (role == .ingester) {
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

    // task group: .concurrent for long-lived background tasks,
    // .async (bounded pool, inline fallback) for HTTP handlers
    var tasks: std.Io.Group = .init;

    // start sync (background — turso → overlay projection)
    if (role == .search) {
        const db = local_db_ptr.?; // guaranteed non-null by check above
        tasks.concurrent(io, sync.syncLoop, .{ allocator, db }) catch |err| {
            log.err("sync task failed: {}", .{err});
        };
        // a wedged sync loop looks identical to a healthy one from outside
        tasks.concurrent(io, sync.watchdogLoop, .{}) catch |err| {
            log.err("sync watchdog failed to start: {}", .{err});
        };
    }

    // start prefix-index promote watcher (background — R2 latest.json → ATTACH).
    // Not optional: an attached, validated snapshot is what makes the search
    // role ready, and the sync loop waits on it before projecting anything.
    if (role == .search) {
        const db = local_db_ptr.?;
        tasks.concurrent(io, promote_runner.watchLoop, .{ allocator, db }) catch |err| {
            log.err("promote watcher task failed: {}", .{err});
        };
    }

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
    defer if (have_identity_pool) identity_pool.deinit();

    if (role == .ingester) {
        // Reading TYPEAHEAD_URL/TYPEAHEAD_SECRET here (not unconditionally at
        // top of main) so the search service can come up without them.
        const config = ingest.getConfig();
        log.info("typeahead ingester → {s}", .{config.worker_url});

        // keep-alive matters here: flushes run every ≤5s and a cold TLS
        // handshake per POST (~150-400ms) blocks the firehose consumer loop —
        // at peak firehose rate that pushed catch-up below 1× real-time.
        // stale-connection failures are covered by flush retry + the watchdog.
        transport = HttpTransport.init(io, allocator);
        transport.keep_alive = true;
        have_transport = true;

        // identity resolution pool: #identity events omit the handle
        // in practice, so we slingshot-resolve each one and POST to the worker.
        resolver = identity.Resolver.init(allocator, io, config.worker_url, config.secret);
        have_resolver = true;

        identity_pool = identity.Pool.init(allocator, .{
            .queue_capacity = 4096,
            .workers = 4,
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

        tasks.concurrent(io, firehoseThread, .{FirehoseArgs{
            .allocator = allocator,
            .config = config,
            .transport = &transport,
            .identity_pool = &identity_pool,
        }}) catch |err| {
            log.err("firehose task failed: {}", .{err});
        };

        tasks.concurrent(io, ingest.watchdogLoop, .{}) catch |err| {
            log.err("watchdog task failed to start: {}", .{err});
        };
    }

    // HTTP handlers receive identity pool as optional — search-only role has none
    const id_pool_arg: ?*identity.Pool = if (have_identity_pool) &identity_pool else null;

    // main thread: HTTP accept loop
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

        if (local_db_ptr) |db| {
            // bounded pool — inline fallback under load (graceful degradation)
            tasks.async(io, server.handleConnection, .{ stream, db, id_pool_arg });
        } else {
            // No local DB (the ingester role). This branch used to answer EVERY
            // path with 503 "search unavailable" — /health included — so the
            // identity pool's accepted/dropped/processed/queued counters were
            // computed, attached to a span, and readable by nobody. A DID
            // silently dropped by the resolver had no observable signal at all;
            // that is how birdlover.selfhosted.social went missing unnoticed.
            // /health is now served here; everything else still 503s.
            var read_buffer: [1024]u8 = undefined;
            var write_buffer: [1024]u8 = undefined;
            var reader = stream.reader(io, &read_buffer);
            var writer = stream.writer(io, &write_buffer);
            var http_srv = std.http.Server.init(&reader.interface, &writer.interface);
            var request = http_srv.receiveHead() catch {
                stream.close(io);
                continue;
            };

            const target = request.head.target;
            const path = if (std.mem.indexOfScalar(u8, target, '?')) |qi| target[0..qi] else target;

            if (std.mem.eql(u8, path, "/health")) {
                var buf: [512]u8 = undefined;
                const body = if (id_pool_arg) |pool| blk: {
                    const ic = pool.counters(io);
                    break :blk std.fmt.bufPrint(&buf,
                        \\{{"status":"ok","role":"ingester","rss_kb":{d},"identity":{{"accepted":{d},"dropped":{d},"processed":{d},"queued":{d}}}}}
                    , .{ ingest.getRssKB(), ic.accepted, ic.dropped, ic.processed, ic.queued }) catch "{\"status\":\"ok\",\"role\":\"ingester\"}";
                } else std.fmt.bufPrint(&buf,
                    \\{{"status":"ok","role":"ingester","rss_kb":{d}}}
                , .{ingest.getRssKB()}) catch "{\"status\":\"ok\",\"role\":\"ingester\"}";

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
    }

    log.info("SIGTERM received, draining tasks…", .{});
    tasks.cancel(io);
    if (have_identity_pool) {
        log.info("draining identity resolver…", .{});
        identity_pool.shutdown(io);
    }
    log.info("shutdown complete", .{});
}
