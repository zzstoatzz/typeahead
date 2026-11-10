const std = @import("std");

/// Milliseconds since the unix epoch for an RFC 3339 timestamp, or null when
/// the text is not one. Fractions past the millisecond are dropped.
pub fn parseMs(text: []const u8) ?i64 {
    if (text.len < 20 or text[4] != '-' or text[7] != '-' or (text[10] != 'T' and text[10] != 't') or text[13] != ':' or text[16] != ':') return null;
    const year = digits(text[0..4]) orelse return null;
    const month = digits(text[5..7]) orelse return null;
    const day = digits(text[8..10]) orelse return null;
    const hour = digits(text[11..13]) orelse return null;
    const minute = digits(text[14..16]) orelse return null;
    const second = digits(text[17..19]) orelse return null;
    if (month < 1 or month > 12 or day < 1 or day > daysIn(year, month) or hour > 23 or minute > 59 or second > 60) return null;

    var at: usize = 19;
    var millis: i64 = 0;
    if (text[at] == '.') {
        at += 1;
        const start = at;
        while (at < text.len and std.ascii.isDigit(text[at])) : (at += 1) {
            if (at - start < 3) millis = millis * 10 + (text[at] - '0');
        }
        if (at == start) return null;
        var given = at - start;
        while (given < 3) : (given += 1) millis *= 10;
    }
    if (at >= text.len) return null;
    var offset_minutes: i64 = 0;
    switch (text[at]) {
        'Z', 'z' => if (at + 1 != text.len) return null,
        '+', '-' => {
            if (text.len != at + 6 or text[at + 3] != ':') return null;
            const hours = digits(text[at + 1 .. at + 3]) orelse return null;
            const minutes = digits(text[at + 4 .. at + 6]) orelse return null;
            if (hours > 23 or minutes > 59) return null;
            offset_minutes = hours * 60 + minutes;
            if (text[at] == '-') offset_minutes = -offset_minutes;
        },
        else => return null,
    }
    const seconds = daysFromCivil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second - offset_minutes * 60;
    return seconds * 1000 + millis;
}

fn digits(text: []const u8) ?i64 {
    var value: i64 = 0;
    for (text) |c| {
        if (!std.ascii.isDigit(c)) return null;
        value = value * 10 + (c - '0');
    }
    return value;
}

fn leap(year: i64) bool {
    return @mod(year, 4) == 0 and (@mod(year, 100) != 0 or @mod(year, 400) == 0);
}

fn daysIn(year: i64, month: i64) i64 {
    return switch (month) {
        2 => if (leap(year)) 29 else 28,
        4, 6, 9, 11 => 30,
        else => 31,
    };
}

fn daysFromCivil(year: i64, month: i64, day: i64) i64 {
    const y = if (month <= 2) year - 1 else year;
    const era = @divFloor(y, 400);
    const year_of_era = y - era * 400;
    const day_of_year = @divFloor(153 * (if (month > 2) month - 3 else month + 9) + 2, 5) + day - 1;
    const day_of_era = year_of_era * 365 + @divFloor(year_of_era, 4) - @divFloor(year_of_era, 100) + day_of_year;
    return era * 146_097 + day_of_era - 719_468;
}

test "RFC 3339 timestamps parse to unix milliseconds" {
    const t = std.testing;
    try t.expectEqual(@as(?i64, 0), parseMs("1970-01-01T00:00:00Z"));
    try t.expectEqual(@as(?i64, 0), parseMs("1970-01-01T00:00:00.000Z"));
    try t.expectEqual(@as(?i64, 1_794_355_200_000), parseMs("2026-11-11T00:00:00Z"));
    try t.expectEqual(@as(?i64, 1_709_210_096_789), parseMs("2024-02-29T12:34:56.789Z"));
    try t.expectEqual(@as(?i64, 1_709_210_096_780), parseMs("2024-02-29T12:34:56.78Z"));
    try t.expectEqual(@as(?i64, 1_709_210_096_789), parseMs("2024-02-29T12:34:56.789123Z"));
    try t.expectEqual(@as(?i64, 1_709_210_096_000), parseMs("2024-02-29T14:34:56+02:00"));
    try t.expectEqual(@as(?i64, 1_709_210_096_000), parseMs("2024-02-29T07:04:56-05:30"));
    try t.expectEqual(@as(?i64, -1000), parseMs("1969-12-31T23:59:59Z"));
    try t.expectEqual(@as(?i64, 951_782_400_000), parseMs("2000-02-29T00:00:00Z"));
    try t.expectEqual(@as(?i64, 4_102_444_800_000), parseMs("2100-01-01T00:00:00Z"));
}

test "text that is not a timestamp is refused" {
    const t = std.testing;
    for ([_][]const u8{
        "",                         "2026-11-11",                "2026-11-11T00:00:00",
        "2026-11-11 00:00:00Z",     "2026-13-11T00:00:00Z",      "2026-02-29T00:00:00Z",
        "2100-02-29T00:00:00Z",     "2026-11-31T00:00:00Z",      "2026-11-11T24:00:00Z",
        "2026-11-11T00:60:00Z",     "2026-11-11T00:00:00.Z",     "2026-11-11T00:00:00+0200",
        "2026-11-11T00:00:00Zjunk", "2026-11-11T00:00:00+24:00", "20x6-11-11T00:00:00Z",
    }) |text| try t.expectEqual(@as(?i64, null), parseMs(text));
}
