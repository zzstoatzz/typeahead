//! Adapter: `*TursoClient` → the connection trait zug expects.
//!
//! zug's `sqlite.validateConn` requires the connection pointer's child to
//! expose:
//!
//!     pub fn exec(self, sql: []const u8, args: anytype) !void
//!     pub fn rows(self, sql: []const u8, args: anytype) !Rows
//!
//! where `Rows` has `next() ?Row` and `deinit()`, and `Row` has
//! `text(i) []const u8` / `int(i) i64`.
//!
//! `MigrationConn` here wraps a `*TursoClient` and provides exactly those
//! methods, converting the comptime-typed `args` tuple into the runtime
//! `[]const TursoClient.RuntimeValue` that the typed exec/query path
//! consumes. The tuple conversion is intentionally strict — `[]const u8`,
//! `*const [N:0]u8` (string literals + `@tagName(enum)`), and `i64` are the
//! only shapes zug binds today; anything else fails at comptime so
//! surprises surface there instead of at runtime.
//!
//! Pure adapter: does NOT import zug. The actual `zug.sqlite.run` /
//! `zug.sqlite.baseline` calls live in phase 3 with the build dep.

const std = @import("std");
const TursoClient = @import("TursoClient.zig");

pub const MigrationConn = struct {
    client: *TursoClient,

    pub fn init(client: *TursoClient) MigrationConn {
        return .{ .client = client };
    }

    pub fn exec(self: *MigrationConn, sql: []const u8, args: anytype) !void {
        const Args = @TypeOf(args);
        const names = @typeInfo(Args).@"struct".field_names;
        var values: [names.len]TursoClient.RuntimeValue = undefined;
        inline for (names, 0..) |name, i| {
            values[i] = toRuntimeValue(@field(args, name));
        }
        try self.client.execRuntime(sql, &values);
    }

    pub fn rows(self: *MigrationConn, sql: []const u8, args: anytype) !Rows {
        const Args = @TypeOf(args);
        const names = @typeInfo(Args).@"struct".field_names;
        var values: [names.len]TursoClient.RuntimeValue = undefined;
        inline for (names, 0..) |name, i| {
            values[i] = toRuntimeValue(@field(args, name));
        }
        const result = try self.client.queryRuntime(sql, &values);
        return Rows{ .result = result, .index = 0 };
    }
};

/// Iterator over `TursoClient.Result`. Wraps the eager-materialized rows
/// so they can be consumed via the `next() ?Row` shape zug expects.
pub const Rows = struct {
    result: TursoClient.Result,
    index: usize,

    pub fn next(self: *Rows) ?TursoClient.Row {
        if (self.index >= self.result.rows.len) return null;
        const row = self.result.rows[self.index];
        self.index += 1;
        return row;
    }

    pub fn deinit(self: *Rows) void {
        self.result.deinit();
    }
};

/// Compile-time conversion from a single tuple field to a typed bind value.
/// Accepts exactly the shapes zug binds today; everything else is a
/// `@compileError` so the build catches unsupported arg types instead of
/// silently sending them as text.
fn toRuntimeValue(arg: anytype) TursoClient.RuntimeValue {
    const T = @TypeOf(arg);
    const info = @typeInfo(T);

    // []const u8 (slice)
    if (info == .pointer and info.pointer.size == .slice and info.pointer.child == u8) {
        return .{ .text = arg };
    }
    // *const [N:0]u8 (string literals, @tagName(enum) results)
    if (info == .pointer and info.pointer.size == .one) {
        const child_info = @typeInfo(info.pointer.child);
        if (child_info == .array and child_info.array.child == u8) {
            return .{ .text = arg };
        }
    }
    // i64 (zug binds the dirty flag as @as(i64, ...))
    if (T == i64) {
        return .{ .int = arg };
    }
    // comptime_int — turn into runtime i64 for zug's @as(i64, ...) values that
    // might lose their cast in some call sites
    if (T == comptime_int) {
        return .{ .int = @as(i64, arg) };
    }

    @compileError("unsupported bind type for MigrationConn: " ++ @typeName(T));
}

// Force monomorphization of `exec` / `rows` with the exact arg-tuple shapes
// zug.sqlite.run binds today (lifted from zug's sqlite.zig: INSERT into
// zug_migrations is 4 strings + i64; UPDATE for dirty is i64 + string;
// SELECT applied takes no args). Without this, the generic methods stay
// uninstantiated until phase 3 wires up `zug.sqlite.run`, and any breakage
// would surface there instead of here.
//
// `@TypeOf` evaluates types without running the function — no HTTP call,
// no undefined deref. But it does force the compiler through the inline-for
// in each method, which exercises every `toRuntimeValue` conversion.
comptime {
    _ = @TypeOf(MigrationConn.exec(undefined, "", .{ "001_id", "name", "abc123", "startup", @as(i64, 0) }));
    _ = @TypeOf(MigrationConn.exec(undefined, "", .{ @as(i64, 0), "001_id" }));
    _ = @TypeOf(MigrationConn.exec(undefined, "", .{}));
    _ = @TypeOf(MigrationConn.rows(undefined, "", .{}));
}
