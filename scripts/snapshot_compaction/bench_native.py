import argparse
import json
from pathlib import Path
import resource
import time

from build import digest_file
from native import pack, verify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    args.output.mkdir()
    side = args.output / "index.db.prefix"
    times = {}
    start = time.monotonic(), time.process_time()
    previous = start

    def phase(name):
        nonlocal previous
        now = time.monotonic(), time.process_time()
        times[name] = dict(wall_seconds=now[0] - previous[0], cpu_seconds=now[1] - previous[1])
        print(json.dumps({"phase": name, **times[name]}), flush=True)
        previous = now

    stats = pack(args.source, side, args.output / "pages")
    (args.output / "pages").unlink()
    phase("pack")
    digest = digest_file(side)
    if digest != args.expected_sha256:
        raise AssertionError(f"reference mismatch: {digest}")
    phase("reference_checksum")
    verify(args.source, side, digest, stats)
    phase("verify")
    result = dict(
        source=str(args.source), keys=stats.keys, verified_keys=stats.verified_keys,
        pages=stats.pages, oversized_pages=stats.oversized_pages,
        sha256=digest, bytes=side.stat().st_size, phases=times,
        wall_seconds=time.monotonic() - start[0], cpu_seconds=time.process_time() - start[1],
        max_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    )
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
