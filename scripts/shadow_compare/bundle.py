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

A line of the view file is the DID as JSON, the view's comparison key and the
view as `[sha, fields]`, tab-separated. A walk reads the key and leaves the
view unparsed unless the other side's key differs.

`pack` turns a sorted view file into three objects another machine can fetch:
the views gzipped, the sidecar, and a manifest naming the bundle, its
watermark, the counts and each object's size and SHA-256. `unpack` is the
other end: it checks a fetched stream against the manifest while writing it.
"""

import argparse
import bisect
import contextlib
import gzip
import hashlib
import heapq
import json
import os
import re
import resource
import shutil
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import views
from transport import TransportError
from views import ABSENT, TOMBSTONE_KEY, Doc, Row

RUN_RECORDS = 250_000
FAN_IN = 64
INDEX_STRIDE = 4096
META_VERSION = 2
BUFFER = 1 << 20
MANIFEST_VERSION = 2
SCRATCH_PREFIX = "scratch/shadow-compare"
MANIFEST_NAME = "manifest.json"
VIEWS_NAME = "views.jsonl.gz"
SIDECAR_NAME = "views.jsonl.meta.json"
MANIFEST_MAX_BYTES = 1 << 20
SIDECAR_MAX_BYTES = 256 << 20
GZIP = 31
SHA256 = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
COUNTS = ("lines", "actors", "live", "deleted", "duplicates")


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
    body = views.key_of(doc.fields) + "\t" + json.dumps([doc.sha, doc.fields], ensure_ascii=False, separators=(",", ":"))
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
        counts["deleted" if body.startswith(TOMBSTONE_KEY + "\t") else "live"] += 1
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


def file_sha256(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pack(views_path, out_dir, *, run_id, build_id, watermark):
    """Write the gzipped views, the sidecar and the manifest into `out_dir`; returns the manifest and its SHA-256."""
    views_path, out_dir = Path(views_path), Path(out_dir)
    if not RUN_ID.fullmatch(str(run_id)):
        raise BundleError("run id must be 1 to 64 letters, digits, dashes or underscores")
    meta = json.loads(meta_path(views_path).read_text())
    if meta.get("version") != META_VERSION or meta["output_bytes"] != views_path.stat().st_size:
        raise BundleError(f"{views_path} does not match its sidecar; sort the dump again")
    started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    plain, packed, packed_bytes = hashlib.sha256(), hashlib.sha256(), 0
    squeeze = zlib.compressobj(6, zlib.DEFLATED, GZIP)
    with open(views_path, "rb", buffering=BUFFER) as source, open(out_dir / VIEWS_NAME, "wb", buffering=BUFFER) as out:
        while chunk := source.read(BUFFER):
            plain.update(chunk)
            piece = squeeze.compress(chunk)
            packed.update(piece)
            packed_bytes += len(piece)
            out.write(piece)
        piece = squeeze.flush()
        packed.update(piece)
        packed_bytes += len(piece)
        out.write(piece)
    shutil.copyfile(meta_path(views_path), out_dir / SIDECAR_NAME)
    manifest = {
        "version": MANIFEST_VERSION,
        "run_id": str(run_id),
        "build_id": build_id,
        "source_watermark": watermark,
        **{name: meta[name] for name in COUNTS},
        "views": {
            "name": VIEWS_NAME,
            "bytes": packed_bytes,
            "sha256": packed.hexdigest(),
            "unpacked_bytes": meta["output_bytes"],
            "unpacked_sha256": plain.hexdigest(),
        },
        "sidecar": {"name": SIDECAR_NAME, "bytes": (out_dir / SIDECAR_NAME).stat().st_size, "sha256": file_sha256(out_dir / SIDECAR_NAME)},
        "packed_at": int(time.time()),
    }
    raw = json.dumps(check_manifest(manifest), separators=(",", ":"), ensure_ascii=False).encode()
    (out_dir / MANIFEST_NAME).write_bytes(raw)
    return manifest, hashlib.sha256(raw).hexdigest(), round(time.monotonic() - started, 3)


def check_manifest(manifest):
    def count(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def described(entry, name, sizes):
        return (
            isinstance(entry, dict) and entry.get("name") == name and SHA256.fullmatch(str(entry.get("sha256")))
            and all(count(entry.get(size)) for size in sizes)
        )

    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise BundleError("manifest has an unknown version")
    if not RUN_ID.fullmatch(str(manifest.get("run_id"))) or not isinstance(manifest.get("build_id"), str) or not manifest["build_id"]:
        raise BundleError("manifest does not name its run and bundle")
    if not count(manifest.get("source_watermark")) or not all(count(manifest.get(name)) for name in COUNTS):
        raise BundleError("manifest has no watermark or counts")
    views = manifest.get("views")
    if not described(views, VIEWS_NAME, ("bytes", "unpacked_bytes")) or not SHA256.fullmatch(str(views.get("unpacked_sha256"))):
        raise BundleError("manifest does not describe the views")
    if not described(manifest.get("sidecar"), SIDECAR_NAME, ("bytes",)) or manifest["sidecar"]["bytes"] > SIDECAR_MAX_BYTES:
        raise BundleError("manifest does not describe the sidecar")
    return manifest


def read_manifest(raw, sha256):
    """The manifest in `raw`, once its bytes hash to `sha256`."""
    if len(raw) > MANIFEST_MAX_BYTES or hashlib.sha256(raw).hexdigest() != sha256:
        raise BundleError("manifest differs from the expected SHA-256")
    try:
        return check_manifest(json.loads(raw))
    except ValueError:
        raise BundleError("manifest is not JSON") from None


def unpack(stream, manifest, out_path):
    """Write the gunzipped views to `out_path`, or nothing if the stream is not what the manifest describes."""
    views, out_path = manifest["views"], Path(out_path)
    partial = out_path.with_name(out_path.name + ".partial")
    plain, packed = hashlib.sha256(), hashlib.sha256()
    packed_bytes = plain_bytes = 0
    expand = zlib.decompressobj(GZIP)
    try:
        with open(partial, "wb", buffering=BUFFER) as out:
            while chunk := stream.read(BUFFER):
                packed.update(chunk)
                packed_bytes += len(chunk)
                if packed_bytes > views["bytes"] or expand.eof:
                    raise BundleError("views object is larger than its manifest says")
                while True:
                    piece = expand.decompress(chunk, BUFFER)
                    chunk = expand.unconsumed_tail
                    plain.update(piece)
                    plain_bytes += len(piece)
                    if plain_bytes > views["unpacked_bytes"]:
                        raise BundleError("views unpack to more than their manifest says")
                    out.write(piece)
                    if not chunk and len(piece) < BUFFER:
                        break
            if not expand.eof or expand.unused_data:
                raise BundleError("views object is not one complete gzip stream")
        found = (packed_bytes, packed.hexdigest(), plain_bytes, plain.hexdigest())
        if found != (views["bytes"], views["sha256"], views["unpacked_bytes"], views["unpacked_sha256"]):
            raise BundleError("views differ from their manifest")
        os.replace(partial, out_path)
    except zlib.error:
        raise BundleError("views object is not gzip") from None
    finally:
        partial.unlink(missing_ok=True)


def view_of(body):
    sha, fields = json.loads(body.decode("utf-8", "surrogatepass"))
    return Doc(sha, fields)


def split(line):
    """A line's DID, comparison key and unparsed view."""
    did, key, body = line.rstrip(b"\n").split(b"\t", 2)
    did = json.loads(did) if b"\\" in did else did[1:-1].decode("utf-8", "surrogatepass")
    return did, key.decode(), body


def parse(line):
    did, _, body = split(line)
    return did, view_of(body)


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
            if not line or split(line)[0] > after:
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
                did, key, body = split(line)
                found.append((did, Row(key, key != TOMBSTONE_KEY, view_of, body)))
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
                    seen, _, body = split(line)
                    if seen >= did:
                        if seen == did:
                            found[did] = view_of(body)
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
    command = commands.add_parser("pack", help="gzip a sorted view file and describe it in a manifest")
    command.add_argument("--views", required=True, help="a view file written by sort, with its sidecar beside it")
    command.add_argument("--out-dir", required=True, help=f"receives {VIEWS_NAME}, {SIDECAR_NAME} and {MANIFEST_NAME}")
    command.add_argument("--run-id", required=True)
    command.add_argument("--build-id", required=True)
    command.add_argument("--watermark", required=True, type=int, help="the bundle's source watermark in unix seconds")
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            manifest, sha256, seconds = pack(args.views, args.out_dir, run_id=args.run_id, build_id=args.build_id, watermark=args.watermark)
            record = {"event": "pack", "manifest_sha256": sha256, "seconds": seconds, **manifest}
        else:
            with open_input(args.input) as stream:
                record = {"event": "sort", **sort(stream, args.out, args.tmp, run_records=args.run_records)}
    except (BundleError, OSError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    print(json.dumps(record, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
