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

/// Parse a boolean env flag. Defaults to true (preserves existing behavior
/// when var is unset). "0", "false", "no", "off" → false; anything else → true.
fn envFlag(name: [*:0]const u8, default: bool) bool {
    const v = cGetenv(name) orelse return default;
    if (mem.eql(u8, v, "0") or mem.eql(u8, v, "false") or
        mem.eql(u8, v, "no") or mem.eql(u8, v, "off")) return false;
    return true;
}

const JetstreamArgs = struct {
    allocator: Allocator,
    config: ingest.Config,
    transport: *HttpTransport,
    local_db: ?*LocalDb,
    identity_pool: ?*identity.Pool,
};

fn jetstreamThread(args: JetstreamArgs) void {
    var handler = ingest.IngestHandler.init(
        args.allocator,
        args.config,
        args.transport,
        args.local_db,
        args.identity_pool,
    ) catch |err| {
        log.err("handler init failed: {}", .{err});
        return;
    };
    defer handler.deinit();

    var client = zat.JetstreamClient.init(io, args.allocator, .{
        .wanted_collections = &.{
            "app.bsky.actor.profile",
            "app.bsky.feed.post",
            "app.bsky.feed.like",
            "app.bsky.graph.follow",
            "sh.tangled.actor.profile",
            "site.standard.*",
        },
        .cursor = ingest.fetchCursor(args.transport, args.config),
    });
    defer client.deinit();

    client.subscribe(&handler) catch |err| {
        log.err("jetstream subscribe failed: {}", .{err});
    };
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

    // Process-role selection. `MODE=indexer` runs the offline snapshot
    // builder against Turso and exits — used as a fly process group on
    // the typeahead-indexer machine. Unset = existing serving + sync +
    // jetstream behavior.
    if (cGetenv("MODE")) |mode| {
        if (mem.eql(u8, mode, "indexer")) {
            // Logfire stays unconfigured here — indexer is a short-lived
            // offline job, std.log to stderr is enough for now. Wire spans
            // later if cadence-wise insight matters.
            log.info("MODE=indexer — running snapshot builder", .{});
            return index_builder.run(allocator);
        }
        log.warn("MODE='{s}' unrecognized; falling through to serving role", .{mode});
    }

    // role flags — defaults preserve current all-in-one behavior
    const enable_jetstream = envFlag("ENABLE_JETSTREAM", true);
    const enable_sync = envFlag("ENABLE_SYNC", true);
    const enable_schema_migrations = envFlag("ENABLE_SCHEMA_MIGRATIONS", true);

    // service_name distinguishes app A vs app B in logfire traces.
    // Fly injects FLY_APP_NAME so the two apps tag distinctly without manual config.
    const service_name = cGetenv("FLY_APP_NAME") orelse "typeahead-ingester";

    log.info("role: jetstream={} sync={} schema={} service_name={s}", .{
        enable_jetstream, enable_sync, enable_schema_migrations, service_name,
    });

    _ = logfire.configure(.{
        .service_name = service_name,
        .service_version = "0.1.0",
        .environment = cGetenv("FLY_APP_NAME") orelse "development",
    }) catch |err| {
        log.err("logfire configure failed: {}", .{err});
    };

    // open local SQLite (optional — graceful degradation if no volume)
    var local_db = LocalDb.init(allocator);
    var local_db_ptr: ?*LocalDb = null;
    local_db.open() catch |err| {
        log.warn("local db failed to open: {}, search will be unavailable", .{err});
    };
    if (local_db.conn != null) {
        local_db_ptr = &local_db;
    }
    defer local_db.deinit();

    // A search service without a local DB is not a service. Fail boot loudly
    // rather than serving 503s indefinitely.
    if (enable_sync and local_db_ptr == null) {
        log.err("ENABLE_SYNC=1 but local DB unavailable — aborting boot", .{});
        return error.LocalDbRequired;
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

    // Run Turso schema migrations BEFORE spawning sync/jetstream — sync queries
    // turso assuming the schema exists. On baselined prod this is a few sub-
    // second SELECTs + INSERT OR IGNORE; on a fresh DB it creates everything
    // from migration 001. The search service must NOT run these.
    if (enable_schema_migrations) {
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

    // start sync (background — turso → local SQLite)
    if (enable_sync) {
        const db = local_db_ptr.?; // guaranteed non-null by check above
        tasks.concurrent(io, sync.syncLoop, .{ allocator, db }) catch |err| {
            log.err("sync task failed: {}", .{err});
        };
    }

    // start prefix-index promote watcher (background — R2 latest.json → ATTACH).
    // Off by default; the search service opts in via ENABLE_INDEX_PROMOTE=1 once R2 creds are
    // set. Does not touch the live /search path (that's gated separately by
    // USE_PREFIX_INDEX) — it only keeps a validated snapshot attached.
    if (enable_sync and envFlag("ENABLE_INDEX_PROMOTE", false)) {
        const db = local_db_ptr.?;
        tasks.concurrent(io, promote_runner.watchLoop, .{ allocator, db }) catch |err| {
            log.err("promote watcher task failed: {}", .{err});
        };
    }

    // jetstream-only state (transport, resolver, identity pool) — declared at
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

    if (enable_jetstream) {
        // Reading TYPEAHEAD_URL/TYPEAHEAD_SECRET here (not unconditionally at
        // top of main) so the search service can come up without them.
        const config = ingest.getConfig();
        log.info("typeahead ingester → {s}", .{config.worker_url});

        transport = HttpTransport.init(io, allocator);
        transport.keep_alive = false;
        have_transport = true;

        // identity resolution pool: jetstream #identity events omit the handle
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

        tasks.concurrent(io, jetstreamThread, .{JetstreamArgs{
            .allocator = allocator,
            .config = config,
            .transport = &transport,
            .local_db = local_db_ptr,
            .identity_pool = &identity_pool,
        }}) catch |err| {
            log.err("jetstream task failed: {}", .{err});
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
            // no local DB — just serve health
            var read_buffer: [1024]u8 = undefined;
            var write_buffer: [1024]u8 = undefined;
            var reader = stream.reader(io, &read_buffer);
            var writer = stream.writer(io, &write_buffer);
            var http_srv = std.http.Server.init(&reader.interface, &writer.interface);
            var request = http_srv.receiveHead() catch {
                stream.close(io);
                continue;
            };
            request.respond("{\"error\":\"search unavailable\"}", .{
                .status = .service_unavailable,
                .extra_headers = &.{
                    .{ .name = "content-type", .value = "application/json" },
                },
            }) catch {};
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
