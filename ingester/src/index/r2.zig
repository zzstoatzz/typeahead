//! R2 object transport via rclone (shelled out). The artifact is multi-GB,
//! which exceeds R2's 5 GiB single-PUT limit → multipart is mandatory, and
//! CF's docs recommend rclone rather than hand-rolling it. rclone is trusted
//! ONLY for transport; correctness is enforced by promote.zig's sha256 +
//! validateSnapshot gate after download.
//!
//! Config is env-driven: INDEX_R2_* (the Fly secrets) are mapped to
//! RCLONE_CONFIG_R2_* in this process's environment, so a spawned rclone
//! sees a remote named "r2" with no config file needed.
const std = @import("std");
const Allocator = std.mem.Allocator;

const io = std.Options.debug_io;
const log = std.log.scoped(.r2);

// std.c omits setenv/unsetenv from 0.16's curated list (getenv is present).
extern "c" fn setenv(name: [*:0]const u8, value: [*:0]const u8, overwrite: c_int) c_int;
extern "c" fn unsetenv(name: [*:0]const u8) c_int;

const RCLONE = "/usr/local/bin/rclone";

pub const Error = error{ MissingR2Config, RcloneFailed };

/// Map INDEX_R2_* → RCLONE_CONFIG_R2_* in this process's env (rclone reads
/// them to construct remote "r2"). Returns the bucket name. The getenv
/// pointers are already NUL-terminated in libc's environ, so they pass
/// straight to setenv (which copies). Idempotent; safe to call before each op.
pub fn configure() Error![:0]const u8 {
    const endpoint = std.c.getenv("INDEX_R2_ENDPOINT") orelse return Error.MissingR2Config;
    const bucket = std.c.getenv("INDEX_R2_BUCKET") orelse return Error.MissingR2Config;
    const akid = std.c.getenv("INDEX_R2_ACCESS_KEY_ID") orelse return Error.MissingR2Config;
    const secret = std.c.getenv("INDEX_R2_SECRET_ACCESS_KEY") orelse return Error.MissingR2Config;

    _ = setenv("RCLONE_CONFIG_R2_TYPE", "s3", 1);
    _ = setenv("RCLONE_CONFIG_R2_PROVIDER", "Cloudflare", 1);
    _ = setenv("RCLONE_CONFIG_R2_ACL", "private", 1);
    // object-scoped tokens can't HEAD the bucket; skip the existence check.
    _ = setenv("RCLONE_CONFIG_R2_NO_CHECK_BUCKET", "true", 1);
    _ = setenv("RCLONE_CONFIG_R2_ENDPOINT", endpoint, 1);
    _ = setenv("RCLONE_CONFIG_R2_ACCESS_KEY_ID", akid, 1);
    _ = setenv("RCLONE_CONFIG_R2_SECRET_ACCESS_KEY", secret, 1);
    // env-only remote: don't read or create a user rclone.conf.
    _ = setenv("RCLONE_CONFIG", "/dev/null", 1);

    return std.mem.span(bucket);
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
pub fn upload(gpa: Allocator, bucket: []const u8, local_path: []const u8, object_key: []const u8) !void {
    const remote = try std.fmt.allocPrint(gpa, "r2:{s}/{s}", .{ bucket, object_key });
    defer gpa.free(remote);
    log.info("rclone upload {s} -> {s}", .{ local_path, remote });
    try run(&.{ RCLONE, "copyto", local_path, remote });
}

/// Download r2:<bucket>/<object_key> → local_path.
pub fn download(gpa: Allocator, bucket: []const u8, object_key: []const u8, local_path: []const u8) !void {
    const remote = try std.fmt.allocPrint(gpa, "r2:{s}/{s}", .{ bucket, object_key });
    defer gpa.free(remote);
    log.info("rclone download {s} -> {s}", .{ remote, local_path });
    try run(&.{ RCLONE, "copyto", remote, local_path });
}

const testing = std.testing;

test "configure maps INDEX_R2_* to RCLONE_CONFIG_R2_* and returns bucket" {
    _ = setenv("INDEX_R2_ENDPOINT", "https://acct.r2.cloudflarestorage.com", 1);
    _ = setenv("INDEX_R2_BUCKET", "typeahead-index", 1);
    _ = setenv("INDEX_R2_ACCESS_KEY_ID", "akid123", 1);
    _ = setenv("INDEX_R2_SECRET_ACCESS_KEY", "secret456", 1);

    const bucket = try configure();
    try testing.expectEqualStrings("typeahead-index", bucket);
    try testing.expectEqualStrings("s3", std.mem.span(std.c.getenv("RCLONE_CONFIG_R2_TYPE").?));
    try testing.expectEqualStrings("Cloudflare", std.mem.span(std.c.getenv("RCLONE_CONFIG_R2_PROVIDER").?));
    try testing.expectEqualStrings("https://acct.r2.cloudflarestorage.com", std.mem.span(std.c.getenv("RCLONE_CONFIG_R2_ENDPOINT").?));
    try testing.expectEqualStrings("akid123", std.mem.span(std.c.getenv("RCLONE_CONFIG_R2_ACCESS_KEY_ID").?));
}

test "configure errors when a secret is missing" {
    // unset one and confirm the error (others may linger from the prior test).
    _ = unsetenv("INDEX_R2_SECRET_ACCESS_KEY");
    try testing.expectError(Error.MissingR2Config, configure());
}
