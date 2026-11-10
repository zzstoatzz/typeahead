//! The actor store: the typeahead corpus in a zkv directory store. Layout in
//! `keys.zig`, encodings in `record.zig`, the writer in `store.zig`, the
//! read-only handle in `reader.zig`.

pub const change = @import("change.zig");
pub const engine = @import("engine.zig");
pub const keys = @import("keys.zig");
pub const maintain = @import("maintain.zig");
pub const moderation = @import("moderation.zig");
pub const read = @import("read.zig");
pub const record = @import("record.zig");
pub const scan = @import("scan.zig");
pub const time = @import("time.zig");

pub const Store = @import("store.zig").Store;
pub const Batch = @import("store.zig").Batch;
pub const Options = @import("store.zig").Options;
pub const Reader = @import("reader.zig").Reader;
pub const Actor = record.Actor;
pub const Change = change.Change;

test {
    _ = change;
    _ = engine;
    _ = keys;
    _ = maintain;
    _ = moderation;
    _ = read;
    _ = record;
    _ = scan;
    _ = time;
    _ = @import("store.zig");
    _ = @import("reader.zig");
    _ = @import("store_test.zig");
}
