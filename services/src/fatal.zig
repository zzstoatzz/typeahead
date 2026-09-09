//! Exit paths that cannot hang on telemetry.
//!
//! Twice (2026-09-03, 2026-09-09) a process panicked on the OTLP export
//! thread and then sat alive-and-dead for hours: logfire's panic hook flushed
//! before aborting, the batch processor's forceFlush waited (uncancelably,
//! ignoring its timeout) for the export that the panicking thread would never
//! finish, and the watchdog that fired ten minutes later waited on the same
//! condvar. Fly saw a healthy process. Nothing was ingested.
//!
//! Every fatal path here arms a deadman FIRST: a raw OS thread that sleeps on
//! libc nanosleep and then terminates the process. It shares nothing with the
//! Io runtime or the telemetry locks, so it fires no matter what the flush
//! does. The flush is still attempted; it just cannot hold the process.

const std = @import("std");
const logfire = @import("logfire");

/// longer than logfire's exit_flush_timeout_ms (3 s per provider) so a
/// healthy flush finishes; a flush that needs more than this is the hang.
pub const exit_deadline_ms: u64 = 5_000;
/// the trace is already on stderr; give the message ship a short window.
pub const panic_deadline_ms: u64 = 3_000;

/// flush telemetry and exit 1 for a restart. Returns never, and does so
/// within `exit_deadline_ms` even if the flush blocks forever.
pub fn exitForRestart() noreturn {
    arm(exit_deadline_ms, exitFailure);
    logfire.flush();
    std.process.exit(1);
}

/// `pub const panic = std.debug.FullPanic(fatal.panicHook)`: logfire's hook
/// prints the message and trace to stderr, ships them, and aborts. The
/// deadman aborts on its behalf if the ship never returns.
pub fn panicHook(msg: []const u8, first_trace_addr: ?usize) noreturn {
    arm(panic_deadline_ms, abortNow);
    logfire.panicFn(msg, first_trace_addr);
}

fn exitFailure() void {
    std.process.exit(1);
}

fn abortNow() void {
    std.process.abort();
}

/// start the deadman. If a thread cannot be spawned the process terminates
/// immediately: an unarmed flush is the failure this module exists to stop.
fn arm(deadline_ms: u64, terminate: *const fn () void) void {
    const thread = std.Thread.spawn(.{ .stack_size = 64 * 1024 }, deadman, .{ deadline_ms, terminate }) catch |err| {
        std.debug.print("fatal: could not arm exit deadline ({s}); terminating now\n", .{@errorName(err)});
        terminate();
        return;
    };
    thread.detach();
}

fn deadman(deadline_ms: u64, terminate: *const fn () void) void {
    sleepMs(deadline_ms);
    std.debug.print("fatal: telemetry flush did not finish within {d} ms; terminating without it\n", .{deadline_ms});
    terminate();
}

fn sleepMs(ms: u64) void {
    var remaining: std.c.timespec = .{
        .sec = @intCast(ms / std.time.ms_per_s),
        .nsec = @intCast((ms % std.time.ms_per_s) * std.time.ns_per_ms),
    };
    while (std.c.nanosleep(&remaining, &remaining) != 0) {}
}

// ---------------------------------------------------------------------------
// tests. The terminators are flags rather than exits; the deadman is otherwise
// the production path.

var fired_late = std.atomic.Value(bool).init(false);
var fired_early = std.atomic.Value(bool).init(false);

fn setFiredLate() void {
    fired_late.store(true, .release);
}

fn setFiredEarly() void {
    fired_early.store(true, .release);
}

test "the deadman fires while a flush is still hanging" {
    arm(20, setFiredLate);
    sleepMs(200);
    try std.testing.expect(fired_late.load(.acquire));
}

test "the deadman does not fire before its deadline" {
    arm(2_000, setFiredEarly);
    sleepMs(20);
    try std.testing.expect(!fired_early.load(.acquire));
}

test "production deadlines leave room for logfire's own bounded flushes" {
    try std.testing.expect(exit_deadline_ms > logfire.exit_flush_timeout_ms);
    try std.testing.expect(panic_deadline_ms > logfire.panic_flush_timeout_ms);
}
