import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def replace(text, before, after):
    if text.count(before) != 1:
        raise ValueError('experimental patch anchor is not unique: '+before[:70])
    return text.replace(before,after)


def instrument(text):
    text = replace(text,'    prefix_ms: i64 = 0,','    profile_candidates: usize = 0,\n    profile_blobs_decoded: usize = 0,\n    profile_bytes_loaded: usize = 0,\n    refill_batches: usize = 0,\n    candidate_budget_exhausted: bool = false,\n    prefix_ms: i64 = 0,')
    text = replace(text,'    const candidates = try executeScoped(arena, lease, norm, &trace, true, pds);\n\n    const n', '    const eval_started = std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds;\n    const candidates = try executeScoped(arena, lease, norm, &trace, true, pds);\n    const eval_us: u64 = @intCast(@divTrunc(std.Io.Timestamp.now(std.Options.debug_io, .awake).nanoseconds - eval_started, 1000));\n\n    const n')
    text = replace(text,'        .q = raw_query,\n        .normalized = norm,','        .engine_us = eval_us,\n        .profile_candidates = trace.profile_candidates,\n        .profile_blobs_decoded = trace.profile_blobs_decoded,\n        .profile_bytes_loaded = trace.profile_bytes_loaded,\n        .refill_batches = trace.refill_batches,\n        .candidate_budget_exhausted = trace.candidate_budget_exhausted,\n        .q = raw_query,\n        .normalized = norm,')
    text = replace(text,'    engine: []const u8 = "index",','    engine: []const u8 = "index",\n    engine_us: u64 = 0,\n    profile_candidates: usize = 0,\n    profile_blobs_decoded: usize = 0,\n    profile_bytes_loaded: usize = 0,\n    refill_batches: usize = 0,\n    candidate_budget_exhausted: bool = false,')
    text = replace(text,'    if (dids.len == 0) return &.{};','    if (dids.len == 0) return &.{};\n    if (trace) |t| t.profile_candidates += dids.len;')
    text = replace(text,'            try ov_map.put(arena, try arena.dupe(u8, row.text(0)), .{ .blob = blob, .hidden = row.int(2) != 0 });','            if (trace) |t| t.profile_bytes_loaded += if (blob) |b| b.len else 0;\n            try ov_map.put(arena, try arena.dupe(u8, row.text(0)), .{ .blob = blob, .hidden = row.int(2) != 0 });')
    text = replace(text,'                if (try lease.baseActor(arena, did)) |blob| try base_map.put(arena, did, blob);','                if (try lease.baseActor(arena, did)) |blob| {\n                    if (trace) |t| t.profile_bytes_loaded += blob.len;\n                    try base_map.put(arena, did, blob);\n                }')
    text = replace(text,'                try base_map.put(arena, try arena.dupe(u8, row.text(0)), try arena.dupe(u8, row.blob(1)));','                if (trace) |t| t.profile_bytes_loaded += row.blob(1).len;\n                try base_map.put(arena, try arena.dupe(u8, row.text(0)), try arena.dupe(u8, row.blob(1)));')
    text = replace(text,'        const ac = pack.unpackActor(blob.?) catch continue;','        if (trace) |t| t.profile_blobs_decoded += 1;\n        const ac = pack.unpackActor(blob.?) catch continue;')
    for name in ['hydrated_overlay','hydrated_base','tombstoned','overlay_hidden','blob_hidden','not_found']:
        text = replace(text,f'        t.{name} = stats.{name};',f'        t.{name} += stats.{name};')
    text = replace(text,'        t.full_query_dropped = rows.len - kept.items.len;','        t.full_query_dropped += rows.len - kept.items.len;')
    return text


def refill(text,budget):
    text = replace(text,'    var merged = try merge.combine(arena, keysets.items, mode, HYDRATE_CAP);',f'    var merged = try merge.combine(arena, keysets.items, mode, {budget});')
    text = replace(text,'        .{HYDRATE_CAP},',f'        .{{@as(usize, {budget})}},')
    begin=text.index('    var hydrated = try hydrate(arena, lease, merged, trace);')
    end=text.index('\n}\n\n/// The builder emits',begin)
    replacement='''    var result: std.ArrayListUnmanaged(searchmod.Candidate) = .empty;
    var checked: usize = 0;
    const maximum = @min(merged.len, BUDGET);
    while (checked < maximum) {
        const last = @min(checked + 50, maximum);
        var batch = try hydrate(arena, lease, merged[checked..last], trace);
        checked = last;
        if (trace) |t| t.refill_batches += 1;
        if (pds) |host| {
            var scoped: std.ArrayListUnmanaged(searchmod.Candidate) = .empty;
            for (batch) |c| if (@import("pds.zig").matches(c.pds, host)) {
                try scoped.append(arena, c);
            };
            batch = try scoped.toOwnedSlice(arena);
        }
        if (prefixes.queryExceedsPrefixMax(norm)) batch = try filterFullQuery(arena, norm, batch, trace);
        try result.appendSlice(arena, batch);
        if (result.items.len >= HYDRATE_CAP) break;
    }
    if (trace) |t| t.candidate_budget_exhausted = checked == BUDGET and merged.len >= BUDGET and result.items.len < HYDRATE_CAP;
    return result.items[0..@min(result.items.len, HYDRATE_CAP)];'''.replace('BUDGET',str(budget))
    return text[:begin]+replacement+text[end:]


def prepare(root,runner):
    root.mkdir(exist_ok=False)
    source=subprocess.check_output(['git','archive','HEAD','services/src','services/build.zig','services/build.zig.zon'],cwd=ROOT)
    report={}
    for name in ['baseline','refill200','complete']:
        directory=root/name
        directory.mkdir()
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(directory,filter='data')
        services=directory/'services'
        (services/'zig-pkg').symlink_to(ROOT/'services/zig-pkg',target_is_directory=True)
        path=services/'src/index/index_search.zig'
        text=instrument(path.read_text())
        if name!='baseline':text=refill(text,200 if name=='refill200' else 65535)
        path.write_text(text)
        if name=='complete':
            p=services/'src/index/prefixes.zig'
            p.write_text(replace(p.read_text(),'pub const PREFIX_MAX: usize = 16;','pub const PREFIX_MAX: usize = 253;'))
        install=directory/'install'
        common=['-Doptimize=ReleaseSafe','--prefix',str(install),'--cache-dir',str(root/'cache')]
        if name!='refill200':runner(['zig','build',*common],services,name+'-build.log',300)
        runner(['zig','build','eval-server',*common],services,name+'-eval-build.log',300)
        report[name]={'server':str(install/'bin/eval-server'),'builder':str(install/'bin/typeahead-ingester') if name!='refill200' else None,'reader_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'prefix_sha256':hashlib.sha256((services/'src/index/prefixes.zig').read_bytes()).hexdigest()}
    return report
