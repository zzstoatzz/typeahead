//! Turso schema initialization via zug.
//!
//! Boot path:
//!   1. Open ephemeral `TursoClient`
//!   2. If `actors` table exists → `zug.sqlite.baseline` seeds and verifies
//!      the first `BOOTSTRAP_BASELINE_COUNT` migrations as already-applied.
//!      No SQL runs against turso for these — just the bookkeeping
//!      INSERT-OR-IGNORE + checksum verification.
//!   3. `zug.sqlite.run` walks the full migration list. On baselined DBs
//!      it sees zero work. On fresh DBs (no `actors` table) it runs every
//!      migration from `001_initial_schema` forward.
//!
//! Designed to be called from `main.zig` startup, before the sync /
//! jetstream threads spin up.

const std = @import("std");
const Allocator = std.mem.Allocator;
const zug = @import("zug");
const TursoClient = @import("TursoClient.zig");
const MigrationConn = @import("zug_conn.zig").MigrationConn;

const migrations_mod = @import("migrations.zig");
const migrations = migrations_mod.migrations;
const BOOTSTRAP_BASELINE_COUNT = migrations_mod.BOOTSTRAP_BASELINE_COUNT;

const log = std.log.scoped(.turso_schema);

/// Apply Turso schema migrations via zug. Returns once the schema is current
/// or returns an error (refuses to boot rather than running on a half-applied
/// schema — better to fail loud than to silently let zug re-run history).
pub fn init(allocator: Allocator, client: *TursoClient) !void {
    var conn = MigrationConn.init(client);
    var diag: zug.Diagnostics = .{};

    if (try existingAppSchema(client)) {
        log.info("existing turso schema detected — baselining {d} migration(s)", .{BOOTSTRAP_BASELINE_COUNT});
        zug.sqlite.baseline(allocator, &conn, migrations[0..BOOTSTRAP_BASELINE_COUNT], .{
            .diagnostics = &diag,
        }) catch |err| {
            log.err("baseline failed: {s} | id={s} | {s}", .{
                @errorName(err),
                diag.migration_id,
                diag.message,
            });
            return err;
        };
    } else {
        log.info("fresh turso DB — zug will create schema from migration 001", .{});
    }

    zug.sqlite.run(allocator, &conn, &migrations, .{ .diagnostics = &diag }) catch |err| {
        log.err("migration failed: {s} | id={s} stmt#{d} preview={s} | {s}", .{
            @errorName(err),
            diag.migration_id,
            diag.statement_index,
            diag.statement_preview,
            diag.message,
        });
        return err;
    };

    log.info("turso schema ready", .{});
}

/// App-specific predicate: does this turso DB already have typeahead's schema?
/// Used to distinguish "fresh DB — let zug create everything" from "existing
/// pre-zug DB — record baseline migrations as already-applied so zug doesn't
/// re-run them." `actors` is the load-bearing table; if it exists the schema
/// is in place.
///
/// Per zug's contract, `zug.sqlite.baseline` doesn't decide fresh-vs-existing —
/// it always tries to seed + verify. We gate the call on this predicate so a
/// fresh DB doesn't get a phantom "applied" migration row without the actual
/// schema underneath.
fn existingAppSchema(client: *TursoClient) !bool {
    var res = try client.queryRuntime(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        &[_]TursoClient.RuntimeValue{.{ .text = "actors" }},
    );
    defer res.deinit();
    return res.rows.len > 0;
}
