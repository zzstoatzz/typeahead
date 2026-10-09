import argparse
import json
from pathlib import Path
import resource
import sqlite3
import time

from build import build
from reference_build import build as reference_build


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--actors", type=int, default=100000)
    parser.add_argument("--prefixes", type=int, default=900000)
    args = parser.parse_args()
    args.output.mkdir()
    source = args.output / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("ATTACH DATABASE ? AS original", (args.source.resolve().as_uri() + "?mode=ro",))
        db.executescript("""
            CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY, blob BLOB NOT NULL) WITHOUT ROWID;
            CREATE TABLE prefix_top_base(key TEXT PRIMARY KEY, dids BLOB NOT NULL) WITHOUT ROWID;
            CREATE TABLE index_meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO index_meta SELECT * FROM original.index_meta;
        """)
        db.execute("INSERT INTO actor_compact_base SELECT * FROM original.actor_compact_base ORDER BY did LIMIT ?", (args.actors,))
        db.execute("INSERT INTO prefix_top_base SELECT * FROM original.prefix_top_base ORDER BY key LIMIT ?", (args.prefixes,))
    results = {}
    manifests = []
    for name, function in (("python", reference_build), ("zig", build)):
        wall, cpu = time.monotonic(), time.process_time()
        manifests.append(function(source, args.output / name))
        results[name] = dict(wall_seconds=time.monotonic() - wall, cpu_seconds=time.process_time() - cpu)
        print(json.dumps({name: results[name]}), flush=True)
    if manifests[0] != manifests[1]:
        raise AssertionError("complete subset artifacts differ")
    results.update(keys=manifests[0]["keys"], sha256=manifests[0]["sha256"], max_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    (args.output / "result.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
