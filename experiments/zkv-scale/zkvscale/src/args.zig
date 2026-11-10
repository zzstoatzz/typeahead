const std = @import("std");

pub const Flag = struct { name: []const u8, value: bool = true };

pub const Args = struct {
    names: []const []const u8,
    values: []const ?[]const u8,

    pub fn init(arena: std.mem.Allocator, argv: []const []const u8, flags: []const Flag) !Args {
        var names: std.ArrayList([]const u8) = .empty;
        var values: std.ArrayList(?[]const u8) = .empty;
        var i: usize = 0;
        while (i < argv.len) : (i += 1) {
            const flag = for (flags) |flag| {
                if (std.mem.eql(u8, flag.name, argv[i])) break flag;
            } else {
                std.debug.print("unknown argument: {s}\n", .{argv[i]});
                return error.UnknownArgument;
            };
            try names.append(arena, flag.name);
            if (!flag.value) {
                try values.append(arena, null);
                continue;
            }
            i += 1;
            if (i == argv.len) {
                std.debug.print("{s} needs a value\n", .{flag.name});
                return error.MissingValue;
            }
            try values.append(arena, argv[i]);
        }
        return .{ .names = names.items, .values = values.items };
    }

    pub fn has(self: Args, name: []const u8) bool {
        for (self.names) |known| if (std.mem.eql(u8, known, name)) return true;
        return false;
    }

    pub fn get(self: Args, name: []const u8) ?[]const u8 {
        for (self.names, self.values) |known, value| if (std.mem.eql(u8, known, name)) return value;
        return null;
    }

    pub fn required(self: Args, name: []const u8) ![]const u8 {
        return self.get(name) orelse {
            std.debug.print("{s} is required\n", .{name});
            return error.MissingArgument;
        };
    }

    pub fn int(self: Args, comptime T: type, name: []const u8, default: T) !T {
        const text = self.get(name) orelse return default;
        return std.fmt.parseInt(T, text, 10) catch {
            std.debug.print("{s} needs an integer, got {s}\n", .{ name, text });
            return error.InvalidInteger;
        };
    }
};

test "flags parse, default and reject strangers" {
    var arena: std.heap.ArenaAllocator = .init(std.testing.allocator);
    defer arena.deinit();
    const flags = [_]Flag{ .{ .name = "--store" }, .{ .name = "--page" }, .{ .name = "--reader", .value = false } };
    const args = try Args.init(arena.allocator(), &.{ "--store", "/x", "--reader" }, &flags);
    try std.testing.expectEqualStrings("/x", try args.required("--store"));
    try std.testing.expect(args.has("--reader"));
    try std.testing.expectEqual(@as(usize, 1000), try args.int(usize, "--page", 1000));
}
