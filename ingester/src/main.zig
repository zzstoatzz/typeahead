const std = @import("std");
const mem = std.mem;
const net = std.net;
const posix = std.posix;
const Allocator = mem.Allocator;
const Thread = std.Thread;
const zat = @import("zat");
const HttpTransport = zat.HttpTransport;
const LocalDb = @import("db/LocalDb.zig");
const sync = @import("db/sync.zig");
const server = @import("server.zig");
const ingest = @import("ingest.zig");

const log = std.log.scoped(.ingester);

const MAX_HTTP_WORKERS = 8;
const SOCKET_TIMEOUT_SECS = 5;

const JetstreamArgs = struct {
    allocator: Allocator,
    config: ingest.Config,
    transport: *HttpTransport,
    local_db: ?*LocalDb,
};

fn jetstreamThread(args: JetstreamArgs) void {
    var handler = ingest.IngestHandler.init(
        args.allocator,
        args.config,
        args.transport,
        args.local_db,
    ) catch |err| {
        log.err("handler init failed: {}", .{err});
        return;
    };
    defer handler.deinit();

    var client = zat.JetstreamClient.init(args.allocator, .{
        .wanted_collections = &.{
            "app.bsky.actor.profile",
            "app.bsky.feed.post",
            "app.bsky.feed.like",
            "app.bsky.graph.follow",
        },
        .cursor = ingest.fetchCursor(args.transport, args.config),
    });
    defer client.deinit();

    client.subscribe(&handler);
}

fn setSocketTimeout(fd: posix.fd_t, secs: u32) !void {
    const timeout = mem.toBytes(posix.timeval{
        .sec = @intCast(secs),
        .usec = 0,
    });
    try posix.setsockopt(fd, posix.SOL.SOCKET, posix.SO.RCVTIMEO, &timeout);
    try posix.setsockopt(fd, posix.SOL.SOCKET, posix.SO.SNDTIMEO, &timeout);
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

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
        const port_str = posix.getenv("PORT") orelse "8080";
        break :blk std.fmt.parseInt(u16, port_str, 10) catch 8080;
    };

    const address = try net.Address.parseIp("0.0.0.0", port);
    var listener = try address.listen(.{ .reuse_address = true });
    defer listener.deinit();

    log.info("listening on port {d}", .{port});

    // init thread pool for HTTP connections
    var pool: Thread.Pool = undefined;
    try pool.init(.{
        .allocator = allocator,
        .n_jobs = MAX_HTTP_WORKERS,
    });
    defer pool.deinit();

    // start sync thread (background — turso → local SQLite)
    if (local_db_ptr) |db| {
        const sync_thread = try Thread.spawn(.{}, sync.syncLoop, .{ allocator, db });
        sync_thread.detach();
    }

    // start jetstream thread (background — ingestion)
    var transport = HttpTransport.init(allocator);
    transport.keep_alive = false;
    defer transport.deinit();

    const js_thread = try Thread.spawn(.{}, jetstreamThread, .{JetstreamArgs{
        .allocator = allocator,
        .config = config,
        .transport = &transport,
        .local_db = local_db_ptr,
    }});
    js_thread.detach();

    // main thread: HTTP accept loop
    while (true) {
        const conn = listener.accept() catch |err| {
            log.err("accept error: {}", .{err});
            continue;
        };

        setSocketTimeout(conn.stream.handle, SOCKET_TIMEOUT_SECS) catch |err| {
            log.warn("failed to set socket timeout: {}", .{err});
        };

        if (local_db_ptr) |db| {
            pool.spawn(server.handleConnection, .{ conn, db }) catch |err| {
                log.err("pool spawn error: {}", .{err});
                conn.stream.close();
            };
        } else {
            // no local DB — just serve health
            var read_buffer: [1024]u8 = undefined;
            var write_buffer: [1024]u8 = undefined;
            var reader = conn.stream.reader(&read_buffer);
            var writer = conn.stream.writer(&write_buffer);
            var srv = std.http.Server.init(reader.interface(), &writer.interface);
            var request = srv.receiveHead() catch {
                conn.stream.close();
                continue;
            };
            request.respond("{\"error\":\"search unavailable\"}", .{
                .status = .service_unavailable,
                .extra_headers = &.{
                    .{ .name = "content-type", .value = "application/json" },
                },
            }) catch {};
            conn.stream.close();
        }
    }
}
