#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Order a `dump-actors` file by DID and read it back as a corpus source.

`dump-actors` prints one stored actor per line in segment order. `sort` reduces
each line to its comparable view, writes sorted runs of at most `run_records`
views to a temporary directory, and merges them into one file with a sparse
index beside it. Memory holds one run; disk holds the runs and the merged file
at once, each about the size of the views.
"""

import argparse
import bisect
import contextlib
import gzip
import hashlib
import heapq
import json
import os
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import views
from transport import TransportError
from views import ABSENT, Doc

RUN_RECORDS = 250_000
FAN_IN = 64
INDEX_STRIDE = 4096
META_VERSION = 1
BUFFER = 1 << 20


class BundleError(TransportError):
    pass


@contextlib.contextmanager
def open_input(path):
    with sys.stdin.buffer if str(path) == "-" else open(path, "rb", buffering=BUFFER) as stream:
        if stream.peek(2)[:2] == b"\x1f\x8b":
            with gzip.open(stream, "rb") as unpacked:
                yield unpacked
        else:
            yield stream


def valid_did(did):
    return isinstance(did, str) and did.startswith("did:") and len(did) <= 2048 and not any(c <= " " or c == "|" for c in did)


def record(number, raw):
    try:
        actor = json.loads(raw)
    except ValueError:
        raise BundleError(f"line {number} is not JSON") from None
    if not isinstance(actor, dict) or not valid_did(actor.get("did")):
        raise BundleError(f"line {number} has no valid did")
    version = actor.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise BundleError(f"line {number} has no version")
    doc = views.from_actor(actor, sha=hashlib.sha256(raw).hexdigest())
    body = json.dumps([doc.sha, doc.fields], ensure_ascii=False, separators=(",", ":"))
    return actor["did"], version, number, body


def write_run(records, path):
    records.sort()
    with open(path, "w", encoding="utf-8", errors="surrogatepass", buffering=BUFFER) as out:
        for did, version, number, body in records:
            out.write(f"{did}\t{version}\t{number}\t{body}\n")


def read_run(path):
    with open(path, encoding="utf-8", errors="surrogatepass", buffering=BUFFER) as stream:
        for line in stream:
            did, version, number, body = line.rstrip("\n").split("\t", 3)
            yield did, int(version), int(number), body


def merge_runs(paths, path):
    with open(path, "w", encoding="utf-8", errors="surrogatepass", buffering=BUFFER) as out:
        for did, version, number, body in heapq.merge(*map(read_run, paths)):
            out.write(f"{did}\t{version}\t{number}\t{body}\n")
    for used in paths:
        os.unlink(used)


def meta_path(path):
    return Path(str(path) + ".meta.json")


def sort(stream, out_path, tmp_dir, *, run_records=RUN_RECORDS, fan_in=FAN_IN):
    """Write the DID-ordered view file and its sidecar; returns the sidecar without the index."""
    if run_records < 1 or fan_in < 2:
        raise BundleError("run_records must be at least 1 and fan_in at least 2")
    started = time.monotonic()
    tmp_dir, out_path = Path(tmp_dir), Path(out_path)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    runs, records, lines, input_bytes, run_bytes = [], [], 0, 0, 0

    def flush():
        nonlocal records, run_bytes
        if records:
            runs.append(tmp_dir / f"run-{len(runs):06}.tsv")
            write_run(records, runs[-1])
            run_bytes += runs[-1].stat().st_size
            records = []

    for raw in stream:
        input_bytes += len(raw)
        raw = raw.rstrip(b"\r\n")
        if not raw:
            continue
        lines += 1
        records.append(record(lines, raw))
        if len(records) >= run_records:
            flush()
    flush()
    first_runs, passes = len(runs), 0
    while len(runs) > fan_in:
        passes += 1
        merged = []
        for start in range(0, len(runs), fan_in):
            merged.append(tmp_dir / f"pass-{passes:02}-{len(merged):06}.tsv")
            merge_runs(runs[start : start + fan_in], merged[-1])
        runs = merged

    index, counts, versions = [], {"live": 0, "deleted": 0}, [None, None]
    actors = duplicates = offset = 0
    held = None

    def emit(out, did, version, body):
        nonlocal actors, offset
        sha, fields = json.loads(body)
        counts["deleted" if fields["deleted"] else "live"] += 1
        versions[0] = version if versions[0] is None else min(versions[0], version)
        versions[1] = version if versions[1] is None else max(versions[1], version)
        if actors % INDEX_STRIDE == 0:
            index.append([did, offset])
        line = (json.dumps(did, ensure_ascii=False) + "\t" + body + "\n").encode("utf-8", "surrogatepass")
        out.write(line)
        offset += len(line)
        actors += 1

    with open(out_path, "wb", buffering=BUFFER) as out:
        for did, version, _, body in heapq.merge(*map(read_run, runs)):
            if held is not None and held[0] != did:
                emit(out, *held)
            elif held is not None:
                duplicates += 1
            held = (did, version, body)
        if held is not None:
            emit(out, *held)
    for used in runs:
        os.unlink(used)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    facts = {
        "version": META_VERSION,
        "lines": lines,
        "actors": actors,
        "duplicates": duplicates,
        "live": counts["live"],
        "deleted": counts["deleted"],
        "min_version": versions[0],
        "max_version": versions[1],
        "input_bytes": input_bytes,
        "output_bytes": offset,
        "run_bytes": run_bytes,
        "runs": first_runs,
        "run_records": run_records,
        "merge_passes": passes + 1,
        "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "seconds": round(time.monotonic() - started, 3),
    }
    meta_path(out_path).write_text(json.dumps({**facts, "index_stride": INDEX_STRIDE, "index": index}, separators=(",", ":")))
    return facts


def parse(line):
    did, body = line.rstrip(b"\n").decode("utf-8", "surrogatepass").split("\t", 1)
    sha, fields = json.loads(body)
    return json.loads(did), Doc(sha, fields)


class BundleJsonl:
    """A sorted view file as a fixed snapshot of production at the bundle's watermark."""

    fixed = True
    want_fields = True

    def __init__(self, path, *, watermark=None, build_id=None, name="production bundle"):
        self.path = Path(path)
        self.name = name
        self.watermark = watermark
        self.build_id = build_id
        meta = json.loads(meta_path(self.path).read_text())
        if meta.get("version") != META_VERSION or meta["output_bytes"] != self.path.stat().st_size:
            raise BundleError(f"{self.path} does not match its sidecar; sort the dump again")
        self.index = meta.pop("index")
        self.keys = [did for did, _ in self.index]
        self.facts = meta
        self.identity = {
            "kind": "bundle-jsonl",
            "build_id": build_id,
            "watermark": watermark,
            "actors": meta["actors"],
            "bytes": meta["output_bytes"],
        }
        self.last = ("", 0)

    def marker(self):
        return {
            "build_id": self.build_id,
            "watermark": self.watermark,
            "snapshot": True,
            "actor_count": self.facts["live"],
            "tombstone_count": self.facts["deleted"],
            "duplicate_dids": self.facts["duplicates"],
            "observed_at": int(time.time()),
        }

    def seek(self, stream, after):
        """Leave the stream at the first line whose DID is greater than `after`."""
        if after == self.last[0]:
            stream.seek(self.last[1])
            return
        slot = bisect.bisect_right(self.keys, after) - 1
        stream.seek(self.index[slot][1] if slot >= 0 else 0)
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line or parse(line)[0] > after:
                stream.seek(offset)
                return

    def read(self, after, limit):
        found = []
        with open(self.path, "rb", buffering=BUFFER) as stream:
            self.seek(stream, after)
            while len(found) < limit:
                line = stream.readline()
                if not line:
                    break
                found.append(parse(line))
            if found:
                self.last = (found[-1][0], stream.tell())
        return found

    def lookup(self, dids):
        found = {}
        with open(self.path, "rb", buffering=BUFFER) as stream:
            for did in sorted(dids):
                slot = bisect.bisect_right(self.keys, did) - 1
                found[did] = ABSENT
                if slot < 0:
                    continue
                stream.seek(self.index[slot][1])
                for _ in range(self.facts["index_stride"]):
                    line = stream.readline()
                    if not line:
                        break
                    seen, doc = parse(line)
                    if seen >= did:
                        if seen == did:
                            found[did] = doc
                        break
        return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("sort", help="order a dump-actors file by DID")
    command.add_argument("--input", required=True, help="dump-actors output, plain or gzip; - reads standard input")
    command.add_argument("--out", required=True, help="the sorted view file; a .meta.json sidecar is written beside it")
    command.add_argument("--tmp", required=True, help="directory for sorted runs, on a disk with room for the views twice")
    command.add_argument("--run-records", type=int, default=RUN_RECORDS)
    args = parser.parse_args(argv)
    try:
        with open_input(args.input) as stream:
            facts = sort(stream, args.out, args.tmp, run_records=args.run_records)
    except BundleError as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps({"event": "sort", **facts}, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
