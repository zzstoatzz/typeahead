//! Which `std.log` records leave the process.
//!
//! Every record reaches stderr for `fly logs`. Logfire receives all of them
//! except the two kinds `shipped` names, which were about 70% of the
//! project's records in the week to 2026-11-10 and repeated numbers the
//! once-a-minute `ingest.heartbeat` span already carries.

const std = @import("std");
const logfire = @import("logfire");

/// Scope for the line written after every flush attempt.
pub const flush_scope = .flush;

pub fn shipped(comptime level: std.log.Level, comptime scope: @EnumLiteral()) bool {
    if (scope == flush_scope) return false;
    // zat's connection progress; the ingester logs its own connects and read
    // errors, and zat reports a failed connection at err.
    if (scope == .zat and @backingInt(level) >= @backingInt(std.log.Level.info)) return false;
    return true;
}

pub fn logFn(
    comptime level: std.log.Level,
    comptime scope: @EnumLiteral(),
    comptime format: []const u8,
    args: anytype,
) void {
    if (comptime shipped(level, scope)) {
        logfire.logFn(level, scope, format, args);
    } else {
        std.log.defaultLog(level, scope, format, args);
    }
}

test "flush progress and zat connection chatter stay on stderr" {
    comptime {
        std.debug.assert(!shipped(.info, flush_scope));
        std.debug.assert(!shipped(.warn, flush_scope));
        std.debug.assert(!shipped(.info, .zat));
        std.debug.assert(!shipped(.debug, .zat));
    }
}

test "warnings, errors and the ingester's own records still ship" {
    comptime {
        std.debug.assert(shipped(.err, .zat));
        std.debug.assert(shipped(.warn, .zat));
        std.debug.assert(shipped(.info, .ingester));
        std.debug.assert(shipped(.warn, .identity));
        std.debug.assert(shipped(.err, .default));
    }
}
