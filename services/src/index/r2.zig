//! R2 object transport via rclone (shelled out). The artifact is multi-GB,
//! which exceeds R2's 5 GiB single-PUT limit → multipart is mandatory, and
//! CF's docs recommend rclone rather than hand-rolling it. rclone is trusted
//! ONLY for transport; correctness is enforced by promote.zig's sha256 +
//! validateSnapshot gate after download.
//!
//! Config: we write a minimal rclone.conf defining remote `r2` from the
//! INDEX_R2_* secrets and pass it via `--config` on every invocation. We do
//! NOT use rclone's RCLONE_CONFIG_* env-var remotes: Zig's process spawn does
//! not propagate runtime setenv() to the child (it uses the startup env
//! snapshot), so env-defined remotes silently don't reach rclone. A --config
//! arg always propagates, and keeps secrets out of argv/logs.
const std = @import("std");
const Allocator = std.mem.Allocator;

const io = std.Options.debug_io;
const log = std.log.scoped(.r2);

// std.c omits setenv/unsetenv from 0.16's curated list. Only the tests use
// these (to populate INDEX_R2_* before calling configure); prod reads the
// vars that Fly injects at boot.
extern "c" fn setenv(name: [*:0]const u8, value: [*:0]const u8, overwrite: c_int) c_int;
extern "c" fn unsetenv(name: [*:0]const u8) c_int;

const CONFIG_PATH = "/tmp/rclone-r2.conf";

/// rclone binary path, resolved at runtime so the host decides where rclone
/// lives — `INDEX_RCLONE_BIN` if set, else a bare "rclone" (PATH-searched).
/// The Fly image has it at /usr/local/bin; a home box may have it under
/// ~/.local/bin. Baking an absolute path here broke the home-box build, so
/// don't: let the env (or PATH) say where it is.
fn rcloneBin() []const u8 {
    if (std.c.getenv("INDEX_RCLONE_BIN")) |p| return std.mem.span(p);
    return "rclone";
}

pub const Error = error{ MissingR2Config, RcloneFailed };

/// rclone remote handle: the bucket name + the path to the written config.
/// Both fields are process-lifetime stable (bucket points into libc environ,
/// config_path is a static literal), so callers don't free them.
pub const Config = struct {
    bucket: []const u8,
    config_path: []const u8,
};

/// Write `/tmp/rclone-r2.conf` defining remote `r2` from the INDEX_R2_*
/// secrets. Idempotent — safe to call before each op. Returns the bucket +
/// config path for the remote. (Lives in /tmp on a single-tenant VM; tighten
/// perms to 0600 later — std.Io.CreateFileOptions has no mode field in 0.16.)
pub fn configure(gpa: Allocator) Error!Config {
    const endpoint = std.c.getenv("INDEX_R2_ENDPOINT") orelse return Error.MissingR2Config;
    const bucket = std.c.getenv("INDEX_R2_BUCKET") orelse return Error.MissingR2Config;
    const akid = std.c.getenv("INDEX_R2_ACCESS_KEY_ID") orelse return Error.MissingR2Config;
    const secret = std.c.getenv("INDEX_R2_SECRET_ACCESS_KEY") orelse return Error.MissingR2Config;

    const content = std.fmt.allocPrint(gpa,
        \\[r2]
        \\type = s3
        \\provider = Cloudflare
        \\acl = private
        \\no_check_bucket = true
        \\region = auto
        \\endpoint = {s}
        \\access_key_id = {s}
        \\secret_access_key = {s}
        \\
    , .{ std.mem.span(endpoint), std.mem.span(akid), std.mem.span(secret) }) catch return Error.MissingR2Config;
    defer gpa.free(content);

    writeConfig(content) catch |err| {
        log.err("failed to write rclone config: {s}", .{@errorName(err)});
        return Error.RcloneFailed;
    };

    return .{ .bucket = std.mem.span(bucket), .config_path = CONFIG_PATH };
}

fn writeConfig(content: []const u8) !void {
    const file = try std.Io.Dir.createFileAbsolute(io, CONFIG_PATH, .{ .truncate = true });
    defer file.close(io);
    var wbuf: [256]u8 = undefined;
    var fw = std.Io.File.Writer.init(file, io, &wbuf);
    try fw.interface.writeAll(content);
    try fw.interface.flush();
}

fn run(argv: []const []const u8) Error!void {
    var child = std.process.spawn(io, .{
        .argv = argv,
        .stdout = .inherit,
        .stderr = .inherit,
    }) catch |err| {
        log.err("rclone spawn failed: {s}", .{@errorName(err)});
        return Error.RcloneFailed;
    };
    const term = child.wait(io) catch |err| {
        log.err("rclone wait failed: {s}", .{@errorName(err)});
        return Error.RcloneFailed;
    };
    switch (term) {
        .exited => |code| if (code != 0) {
            log.err("rclone exited with code {d}", .{code});
            return Error.RcloneFailed;
        },
        else => return Error.RcloneFailed,
    }
}

/// Upload local_path → r2:<bucket>/<object_key> (rclone picks multipart for
/// large files automatically).
pub fn upload(gpa: Allocator, cfg: Config, local_path: []const u8, object_key: []const u8) !void {
    const remote = try std.fmt.allocPrint(gpa, "r2:{s}/{s}", .{ cfg.bucket, object_key });
    defer gpa.free(remote);
    log.info("rclone upload {s} -> {s}", .{ local_path, remote });
    try run(&.{ rcloneBin(), "--config", cfg.config_path, "copyto", local_path, remote });
}

/// Download r2:<bucket>/<object_key> → local_path.
pub fn download(gpa: Allocator, cfg: Config, object_key: []const u8, local_path: []const u8) !void {
    const remote = try std.fmt.allocPrint(gpa, "r2:{s}/{s}", .{ cfg.bucket, object_key });
    defer gpa.free(remote);
    log.info("rclone download {s} -> {s}", .{ remote, local_path });
    try run(&.{ rcloneBin(), "--config", cfg.config_path, "copyto", remote, local_path });
}

const testing = std.testing;

test "configure writes an r2 remote config and returns the bucket" {
    _ = setenv("INDEX_R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com", 1);
    _ = setenv("INDEX_R2_BUCKET", "typeahead-index", 1);
    _ = setenv("INDEX_R2_ACCESS_KEY_ID", "akid123", 1);
    _ = setenv("INDEX_R2_SECRET_ACCESS_KEY", "secret456", 1);

    const cfg = try configure(testing.allocator);
    try testing.expectEqualStrings("typeahead-index", cfg.bucket);
    try testing.expectEqualStrings(CONFIG_PATH, cfg.config_path);

    // the written config must define remote [r2] as a Cloudflare s3 backend
    const f = try std.Io.Dir.openFileAbsolute(io, CONFIG_PATH, .{});
    defer f.close(io);
    var buf: [512]u8 = undefined;
    const n = try f.readStreaming(io, &.{&buf});
    const got = buf[0..n];
    try testing.expect(std.mem.indexOf(u8, got, "[r2]") != null);
    try testing.expect(std.mem.indexOf(u8, got, "provider = Cloudflare") != null);
    try testing.expect(std.mem.indexOf(u8, got, "https://acct.r2.cloudflarestorage.com") != null);
}

test "configure errors when a secret is missing" {
    _ = unsetenv("INDEX_R2_SECRET_ACCESS_KEY");
    try testing.expectError(Error.MissingR2Config, configure(testing.allocator));
}
