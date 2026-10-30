const std = @import("std");
const native = @import("native_normalize");
const pdsmod = @import("native_pds");
const searchmod = @import("native_search");
const page = std.heap.page_allocator;
pub const PLAN_VERSION = "complete-prefix-groups-v1";
const max_raw_query = 512;
const max_raw_actor = 1024;
const max_groups = 64;
const max_actor_keys = 8192;
const version_z = native.VERSION ++ "\x00";
const plan_z = PLAN_VERSION ++ "\x00";

pub const TaResult = extern struct { data: ?[*]const u8 = null, len: usize = 0, owner: ?*anyopaque = null };
const Owner = struct { arena: std.heap.ArenaAllocator };
const Request = struct {
    op: []const u8,
    handle: []const u8 = "",
    display: []const u8 = "",
    q: []const u8 = "",
    pds: []const u8 = "",
    score: ?f64 = null,
};
const Run = struct { kind: []const u8, text: []const u8 };
const Group = struct { kind: []const u8, text: []const u8, keys: []const []const u8 };
const Reply = struct {
    status: []const u8 = "ok",
    reason: ?[]const u8 = null,
    normalize_version: []const u8 = native.VERSION,
    plan_version: []const u8 = PLAN_VERSION,
    normalized: ?[]const u8 = null,
    normalized_handle: ?[]const u8 = null,
    normalized_display: ?[]const u8 = null,
    mode: []const u8 = "empty",
    display_mode: []const u8 = "all",
    keys: []const []const u8 = &.{},
    groups: []const Group = &.{},
    display_runs: []const Run = &.{},
    exact: ?[]const u8 = null,
    handle_key: ?[]const u8 = null,
    domain_key: ?[]const u8 = null,
    needs_verification: bool = false,
    matches: ?bool = null,
    pds: ?[]const u8 = null,
    score_f32: ?f32 = null,
    score_bits: ?u32 = null,
    max_query_bytes: usize = max_raw_query,
    max_normalized_bytes: usize = native.MAX_OUTPUT,
    max_query_groups: usize = max_groups,
    max_actor_keys: usize = max_actor_keys,
};
const Kind = enum { query, handle, display };
fn normalized(a: std.mem.Allocator, raw: []const u8, kind: Kind) ![]const u8 {
    return switch (kind) {
        .query => native.query(a, raw),
        .handle => native.handle(a, raw),
        .display => native.display(a, raw),
    };
}
fn boundedNormalize(a: std.mem.Allocator, raw: []const u8, kind: Kind) ![]const u8 {
    if (!std.unicode.utf8ValidateSlice(raw)) return error.InvalidInput;
    const value = try normalized(a, raw, kind);
    if (value.len > native.MAX_OUTPUT) return error.BudgetExceeded;
    if (value.len < native.MAX_OUTPUT) return value;
    var end: usize = 1;
    while (end <= raw.len) : (end += 1) {
        if (end < raw.len and raw[end] & 0xc0 == 0x80) continue;
        const prefix = try normalized(a, raw[0..end], kind);
        if (prefix.len >= native.MAX_OUTPUT) {
            const tail = try normalized(a, raw[end..], kind);
            if (tail.len != 0) return error.BudgetExceeded;
            return value;
        }
    }
    return value;
}
fn runs(a: std.mem.Allocator, value: []const u8) ![]const Run {
    var out: std.ArrayList(Run) = .empty;
    var tokens = std.mem.splitScalar(u8, value, ' ');
    while (tokens.next()) |token| {
        if (token.len == 0) continue;
        var view = (try std.unicode.Utf8View.init(token)).iterator();
        var start: usize = 0;
        var offset: usize = 0;
        var cjk: ?bool = null;
        while (view.nextCodepointSlice()) |cp| {
            const this_cjk = cp[0] >= 0x80;
            if (cjk) |previous| {
                if (previous != this_cjk) {
                    try out.append(a, .{ .kind = if (previous) "cjk" else "latin", .text = token[start..offset] });
                    start = offset;
                }
            }
            cjk = this_cjk;
            offset += cp.len;
        }
        if (cjk) |previous| try out.append(a, .{ .kind = if (previous) "cjk" else "latin", .text = token[start..offset] });
    }
    return out.toOwnedSlice(a);
}
fn appendKey(a: std.mem.Allocator, out: *std.ArrayList([]const u8), ns: []const u8, text: []const u8) !void {
    if (out.items.len >= max_actor_keys) return error.BudgetExceeded;
    try out.append(a, try std.fmt.allocPrint(a, "{s}{s}", .{ ns, text }));
}
fn appendPrefixes(a: std.mem.Allocator, out: *std.ArrayList([]const u8), ns: []const u8, value: []const u8) !void {
    var it = (try std.unicode.Utf8View.init(value)).iterator();
    var end: usize = 0;
    while (it.nextCodepointSlice()) |cp| {
        end += cp.len;
        try appendKey(a, out, ns, value[0..end]);
    }
}
fn appendCjk(a: std.mem.Allocator, out: *std.ArrayList([]const u8), value: []const u8, actor: bool) !usize {
    var it = (try std.unicode.Utf8View.init(value)).iterator();
    var bounds: [native.MAX_OUTPUT + 1]usize = undefined;
    var n: usize = 0;
    bounds[0] = 0;
    while (it.nextCodepointSlice()) |cp| {
        n += 1;
        bounds[n] = bounds[n - 1] + cp.len;
    }
    if (n == 1 or actor) try appendKey(a, out, "n:", value[0..bounds[1]]);
    var i: usize = 0;
    while (i + 1 < n) : (i += 1) try appendKey(a, out, "n:", value[bounds[i]..bounds[i + 2]]);
    return n;
}
fn less(_: void, lhs: []const u8, rhs: []const u8) bool { return std.mem.order(u8, lhs, rhs) == .lt; }
fn unique(a: std.mem.Allocator, values: []const []const u8) ![]const []const u8 {
    var seen = std.StringHashMap(void).init(a);
    for (values) |value| try seen.put(value, {});
    const out = try a.alloc([]const u8, seen.count());
    var it = seen.keyIterator();
    var i: usize = 0;
    while (it.next()) |value| : (i += 1) out[i] = value.*;
    std.mem.sort([]const u8, out, {}, less);
    return out;
}
fn actorPlan(a: std.mem.Allocator, req: Request) !Reply {
    if (req.handle.len > max_raw_actor or req.display.len > max_raw_actor) return error.BudgetExceeded;
    const handle = try boundedNormalize(a, req.handle, .handle);
    const display = try boundedNormalize(a, req.display, .display);
    const display_runs = try runs(a, display);
    var keys: std.ArrayList([]const u8) = .empty;
    if (handle.len != 0) try appendKey(a, &keys, "e:", handle);
    try appendPrefixes(a, &keys, "h:", handle);
    for (handle, 0..) |c, i| if (c == '.') try appendPrefixes(a, &keys, "d:", handle[i + 1 ..]);
    for (display_runs) |run| {
        if (std.mem.eql(u8, run.kind, "cjk")) { _ = try appendCjk(a, &keys, run.text, true); }
        else try appendPrefixes(a, &keys, "n:", run.text);
    }
    var reply: Reply = .{ .normalized_handle = handle, .normalized_display = display,
        .keys = try unique(a, keys.items), .display_runs = display_runs };
    if (req.score) |input| {
        var bits: u32 = 0; var score: f32 = 0;
        if (ta_score_f32(input, &score, &bits) != 0) return error.InvalidScore;
        reply.score_f32 = score; reply.score_bits = bits;
    }
    return reply;
}
fn unsupportedScript(raw: []const u8) bool {
    var it = (std.unicode.Utf8View.init(raw) catch return true).iterator();
    while (it.nextCodepoint()) |cp| {
        if ((cp >= 0x0370 and cp <= 0x052f) or (cp >= 0x0590 and cp <= 0x06ff) or
            (cp >= 0x0e00 and cp <= 0x0e7f) or (cp >= 0xac00 and cp <= 0xd7af)) return true;
    }
    return false;
}
fn queryPlan(a: std.mem.Allocator, raw: []const u8) !Reply {
    if (raw.len > max_raw_query) return error.BudgetExceeded;
    if (!std.unicode.utf8ValidateSlice(raw)) return error.InvalidInput;
    if (unsupportedScript(raw)) return error.UnsupportedQuery;
    var sanitize_buf: [512]u8 = undefined;
    const sanitized = searchmod.sanitize(raw, &sanitize_buf);
    const value = try boundedNormalize(a, sanitized, .query);
    if (value.len == 0) return .{ .status = "empty", .normalized = value };
    const query_runs = try runs(a, value);
    if (query_runs.len > max_groups) return error.BudgetExceeded;
    var groups: std.ArrayList(Group) = .empty;
    var flattened: std.ArrayList([]const u8) = .empty;
    var needs_verification = false;
    for (query_runs) |run| {
        var keys: std.ArrayList([]const u8) = .empty;
        if (std.mem.eql(u8, run.kind, "cjk")) {
            const n = try appendCjk(a, &keys, run.text, false);
            needs_verification = needs_verification or n >= 3;
        } else try appendKey(a, &keys, "n:", run.text);
        const deduped = try unique(a, keys.items);
        try groups.append(a, .{ .kind = run.kind, .text = run.text, .keys = deduped });
        try flattened.appendSlice(a, deduped);
    }
    const flat = try unique(a, flattened.items);
    if (flat.len > 64) return error.BudgetExceeded;
    const single = std.mem.indexOfScalar(u8, value, ' ') == null;
    return .{ .normalized = value, .mode = if (single) "or" else "and", .keys = flat,
        .groups = try groups.toOwnedSlice(a), .needs_verification = needs_verification,
        .exact = if (single) try std.fmt.allocPrint(a, "e:{s}", .{value}) else null,
        .handle_key = if (single) try std.fmt.allocPrint(a, "h:{s}", .{value}) else null,
        .domain_key = if (single) try std.fmt.allocPrint(a, "d:{s}", .{value}) else null };
}
fn displayMatches(actor_runs: []const Run, groups: []const Group) bool {
    for (groups) |group| {
        var found = false;
        for (actor_runs) |run| {
            if (!std.mem.eql(u8, run.kind, group.kind)) continue;
            const cjk = std.mem.eql(u8, group.kind, "cjk");
            if (!cjk or std.unicode.utf8CountCodepoints(group.text) catch 0 == 1) {
                if (std.mem.startsWith(u8, run.text, group.text)) { found = true; break; }
            } else if (std.mem.indexOf(u8, run.text, group.text) != null) { found = true; break; }
        }
        if (!found) return false;
    }
    return groups.len != 0;
}
fn handleMatches(handle: []const u8, query: []const u8) bool {
    if (std.mem.startsWith(u8, handle, query)) return true;
    for (handle, 0..) |c, i| if (c == '.' and std.mem.startsWith(u8, handle[i + 1 ..], query)) return true;
    return false;
}
fn process(a: std.mem.Allocator, req: Request) !Reply {
    if (std.mem.eql(u8, req.op, "metadata")) return .{};
    if (std.mem.eql(u8, req.op, "actor")) return actorPlan(a, req);
    if (std.mem.eql(u8, req.op, "query")) return queryPlan(a, req.q);
    if (std.mem.eql(u8, req.op, "pds")) {
        const pds = try pdsmod.normalize(a, req.pds) orelse return error.InvalidPds;
        return .{ .pds = pds };
    }
    if (std.mem.eql(u8, req.op, "matches")) {
        const plan = try queryPlan(a, req.q);
        if (!std.mem.eql(u8, plan.status, "ok")) return .{ .status = plan.status, .matches = false };
        if (req.handle.len > max_raw_actor or req.display.len > max_raw_actor) return error.BudgetExceeded;
        const handle = try boundedNormalize(a, req.handle, .handle);
        const display = try boundedNormalize(a, req.display, .display);
        const display_runs = try runs(a, display);
        const match = (std.mem.eql(u8, plan.mode, "or") and handleMatches(handle, plan.normalized.?)) or displayMatches(display_runs, plan.groups);
        return .{ .matches = match, .normalized = plan.normalized };
    }
    return error.InvalidInput;
}
fn errorReply(err: anyerror) Reply {
    return .{ .status = switch (err) {
        error.BudgetExceeded => "budget_exceeded", error.UnsupportedQuery => "unsupported_query",
        else => "invalid_input",
    }, .reason = @errorName(err) };
}
export fn ta_plan(input: ?[*]const u8, len: usize, out: *TaResult) i32 {
    out.* = .{};
    if (input == null or len == 0 or len > 16384) return -1;
    const owner = page.create(Owner) catch return -2;
    owner.* = .{ .arena = std.heap.ArenaAllocator.init(page) };
    const a = owner.arena.allocator();
    const req = std.json.parseFromSliceLeaky(Request, a, input.?[0..len], .{ .allocate = .alloc_always }) catch {
        owner.arena.deinit(); page.destroy(owner); return -1;
    };
    const reply = process(a, req) catch |err| errorReply(err);
    const bytes = std.json.Stringify.valueAlloc(a, reply, .{}) catch {
        owner.arena.deinit(); page.destroy(owner); return -2;
    };
    out.* = .{ .data = bytes.ptr, .len = bytes.len, .owner = owner };
    return 0;
}
export fn ta_result_free(result: *TaResult) void {
    const owner: *Owner = @ptrCast(@alignCast(result.owner orelse return));
    owner.arena.deinit(); page.destroy(owner); result.* = .{};
}
export fn ta_normalizer_version() [*:0]const u8 { return version_z[0 .. version_z.len - 1 :0].ptr; }
export fn ta_plan_version() [*:0]const u8 { return plan_z[0 .. plan_z.len - 1 :0].ptr; }
export fn ta_score_f32(input: f64, value: *f32, bits: *u32) i32 {
    if (!std.math.isFinite(input)) return -1;
    const result: f32 = @floatCast(input);
    if (!std.math.isFinite(result)) return -1;
    value.* = result; bits.* = @bitCast(result); return 0;
}

test "reuse canonical normalization and native sanitizer" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator); defer arena.deinit();
    const a = arena.allocator();
    const plan = try queryPlan(a, "ÉRIC_\\%\" (Œuvre)");
    try std.testing.expectEqualStrings("eric oeuvre", plan.normalized.?);
    try std.testing.expectEqualStrings(native.VERSION, plan.normalize_version);
}
test "all display words and complete long Latin prefixes" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator); defer arena.deinit(); const a = arena.allocator();
    const actor = try actorPlan(a, .{ .op = "actor", .handle = "abcdefghijklmnopneedle.fixture.test", .display = "one two three four five six seven eight nine longdisplaytokenneedle" });
    const plan = try queryPlan(a, "one two three four five six seven eight nine");
    try std.testing.expectEqual(@as(usize,9), plan.groups.len);
    try std.testing.expect(displayMatches(actor.display_runs, plan.groups));
    const miss = try queryPlan(a, "one two three four five six seven eight nonexistent");
    try std.testing.expect(!displayMatches(actor.display_runs, miss.groups));
    var found = false; for (actor.keys) |key| { if (std.mem.eql(u8,key,"h:abcdefghijklmnopneed")) found = true; }
    try std.testing.expect(found);
}
test "CJK groups require contiguous runs and preserve nonleading bigram search" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator); defer arena.deinit(); const a = arena.allocator();
    const plan = try queryPlan(a, "日本太");
    const valid = try actorPlan(a, .{ .op = "actor", .display = "名前日本太郎" });
    const split = try actorPlan(a, .{ .op = "actor", .display = "日本 東京 本太" });
    try std.testing.expect(plan.needs_verification);
    try std.testing.expect(displayMatches(valid.display_runs, plan.groups));
    try std.testing.expect(!displayMatches(split.display_runs, plan.groups));
    const single = try queryPlan(a, "本"); try std.testing.expect(!displayMatches(valid.display_runs,single.groups));
    const mixed = try queryPlan(a, "Alice日本");
    const partial = try actorPlan(a,.{.op="actor",.display="Alice Other"});
    try std.testing.expect(!displayMatches(partial.display_runs,mixed.groups));
}
test "explicit normalization budget and accepted exact boundary" {
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator); defer arena.deinit(); const a = arena.allocator();
    _ = try queryPlan(a, &@as([256]u8,@splat('a')));
    try std.testing.expectError(error.BudgetExceeded,queryPlan(a,&@as([257]u8,@splat('a'))));
    try std.testing.expectError(error.UnsupportedQuery,queryPlan(a,"مرحبا"));
}
test "score cast is finite canonical f32" {
    var value:f32=0;var bits:u32=0;
    try std.testing.expectEqual(@as(i32,0),ta_score_f32(0.1,&value,&bits));
    try std.testing.expectEqual(@as(u32,0x3dcccccd),bits);
    try std.testing.expectEqual(@as(i32,-1),ta_score_f32(std.math.inf(f64),&value,&bits));
}
test { _ = native; _ = pdsmod; _ = searchmod; }
