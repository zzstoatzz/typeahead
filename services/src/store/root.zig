//! The actor store: the typeahead corpus in a zkv directory store. Layout in
//! `keys.zig`, encodings in `record.zig`.

pub const change = @import("change.zig");
pub const keys = @import("keys.zig");
pub const moderation = @import("moderation.zig");
pub const record = @import("record.zig");
pub const time = @import("time.zig");

pub const Actor = record.Actor;
pub const Change = change.Change;

test {
    _ = change;
    _ = keys;
    _ = moderation;
    _ = record;
    _ = time;
}
