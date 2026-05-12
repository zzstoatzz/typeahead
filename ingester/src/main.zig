const std = @import("std");
const mem = std.mem;
const Allocator = mem.Allocator;
const zat = @import("zat");
const logfire = @import("logfire");
const HttpTransport = zat.HttpTransport;
const LocalDb = @import("db/LocalDb.zig");
const sync = @import("db/sync.zig");
const server = @import("server.zig");
const ingest = @import("ingest.zig");
const identity = @import("identity.zig");

// phase 2 build-time check: ensure the zug adapter compiles. removed in
// phase 3 when zug.sqlite.run actually consumes it.
comptime {
    _ = @import("db/zug_conn.zig");
}

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

    // configure logfire (reads LOGFIRE_WRITE_TOKEN from env)
    _ = logfire.configure(.{
        .service_name = "typeahead-ingester",
        .service_version = "0.1.0",
        .environment = cGetenv("FLY_APP_NAME") orelse "development",
    }) catch |err| {
        log.err("logfire configure failed: {}", .{err});
    };

    const config = ingest.getConfig();
    log.info("typeahead ingester → {s}", .{config.worker_url});

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

    // task group: .concurrent for long-lived background tasks,
    // .async (bounded pool, inline fallback) for HTTP handlers
    var tasks: std.Io.Group = .init;

    // start sync (background — turso → local SQLite)
    if (local_db_ptr) |db| {
        tasks.concurrent(io, sync.syncLoop, .{ allocator, db }) catch |err| {
            log.err("sync task failed: {}", .{err});
        };
    }

    // start jetstream (background — ingestion)
    var transport = HttpTransport.init(io, allocator);
    transport.keep_alive = false;
    defer transport.deinit();

    // identity resolution pool: jetstream #identity events omit the handle in
    // practice, so we slingshot-resolve each one and POST back to the worker.
    var resolver = identity.Resolver.init(allocator, io, config.worker_url, config.secret);
    defer resolver.deinit();

    var identity_pool: identity.Pool = undefined;
    identity_pool.init(allocator, .{
        .queue_size = 4096,
        .ctx = &resolver,
        .process = identity.Resolver.process,
    }) catch |err| {
        log.err("identity pool init failed: {}", .{err});
        return err;
    };
    defer identity_pool.deinit();

    identity_pool.start(io, 4) catch |err| {
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
            tasks.async(io, server.handleConnection, .{ stream, db, &identity_pool });
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
    log.info("draining identity resolver…", .{});
    identity_pool.shutdown(io);
    log.info("shutdown complete", .{});
}
