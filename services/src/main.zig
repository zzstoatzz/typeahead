const std = @import("std");
const mem = std.mem;
const logfire = @import("logfire");
const log_route = @import("log_route.zig");
const fatal = @import("fatal.zig");
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

pub fn main() !void {
    const allocator = std.heap.smp_allocator;
    app_threaded_io = std.Io.Threaded.init(allocator, .{
        .stack_size = 8 * 1024 * 1024, // 8MB (default 16MB is wasteful on 256MB VM)
        .async_limit = std.Io.Limit.limited(8),
    });

    // Explicit role, no default: a machine that was not given one must not
    // quietly open a store and follow the relay.
    const mode = cGetenv("MODE") orelse {
        log.err("MODE not set: must be store or store-bootstrap", .{});
        return error.RoleRequired;
    };
    if (mem.eql(u8, mode, "store-bootstrap")) {
        store_bootstrap.run(allocator, io) catch |err| {
            log.err("store bootstrap failed: {s}", .{@errorName(err)});
            return err;
        };
        return;
    }
    if (!mem.eql(u8, mode, "store")) {
        log.err("MODE='{s}' is not a role of this binary: must be store or store-bootstrap", .{mode});
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

    return store_role.serve(allocator, io, &srv);
}
