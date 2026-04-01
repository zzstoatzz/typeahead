const std = @import("std");
const mem = std.mem;
const io = std.Options.debug_io;
const Allocator = mem.Allocator;
const zat = @import("zat");
const logfire = @import("logfire");
const HttpTransport = zat.HttpTransport;
const LocalDb = @import("db/LocalDb.zig");
const sync = @import("db/sync.zig");
const server = @import("server.zig");
const ingest = @import("ingest.zig");

const log = std.log.scoped(.ingester);

const SOCKET_TIMEOUT_SECS = 5;

fn cGetenv(name: [*:0]const u8) ?[]const u8 {
    if (std.c.getenv(name)) |p| return mem.span(p);
    return null;
}

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
            "sh.tangled.actor.profile",
        },
        .cursor = ingest.fetchCursor(args.transport, args.config),
    });
    defer client.deinit();

    client.subscribe(&handler);
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

    // start sync thread (background — turso → local SQLite)
    if (local_db_ptr) |db| {
        const sync_thread = try std.Thread.spawn(.{}, sync.syncLoop, .{ allocator, db });
        sync_thread.detach();
    }

    // start jetstream thread (background — ingestion)
    var transport = HttpTransport.init(io, allocator);
    transport.keep_alive = false;
    defer transport.deinit();

    const js_thread = try std.Thread.spawn(.{}, jetstreamThread, .{JetstreamArgs{
        .allocator = allocator,
        .config = config,
        .transport = &transport,
        .local_db = local_db_ptr,
    }});
    js_thread.detach();

    // main thread: HTTP accept loop
    while (true) {
        const stream = srv.accept(io) catch |err| {
            log.err("accept error: {}", .{err});
            continue;
        };

        setSocketTimeout(stream.socket.handle, SOCKET_TIMEOUT_SECS) catch |err| {
            log.warn("failed to set socket timeout: {}", .{err});
        };

        if (local_db_ptr) |db| {
            const handler_thread = std.Thread.spawn(.{}, server.handleConnection, .{ stream, db }) catch |err| {
                log.err("thread spawn error: {}", .{err});
                stream.close(io);
                continue;
            };
            handler_thread.detach();
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
}
