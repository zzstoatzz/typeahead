import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.parse
from pathlib import Path

BUILD = Path(
    "/var/lib/typeahead-indexer/work/tantivy-builds/checkpoint-ef4ecac6b82f4d2aae0efea7cda2ad19"
)
SCRATCH = Path("/var/lib/typeahead-indexer/experiments/import-tuning-20261105")
BINARY = "ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    published = json.loads((BUILD / "published.json").read_bytes())
    if published.get("remote_readback_passed") is not True:
        raise ValueError("Normal publication must finish before this pilot")
    seed = BUILD / "sample/seed.jsonl"
    if not 0 < seed.stat().st_size <= 128 * 1024**2:
        raise ValueError("Existing sample is outside its size bound")
    with seed.open("rb") as source:
        header = json.loads(source.readline(16 * 1024**2))
        if (
            header.get("partial") is not True
            or not 1 <= header["actor_count"] <= 100000
        ):
            raise ValueError("Only the existing unqualified sample is permitted")
        handle = scope = None
        for _ in range(10000):
            actor = json.loads(source.readline())
            if actor.get("type") == "end":
                break
            if actor.get("handle") and not handle:
                handle = actor["handle"]
            if actor.get("pds") and not scope:
                scope = actor["pds"]
            if handle and scope:
                break
    if digest(BUILD / "code/typeahead-tantivy") != BINARY:
        raise ValueError("Frozen reader changed")
    request = json.loads((BUILD / "build/stream-request.json").read_bytes())
    helper = Path(request["repository"]) / "services/tantivy/tests/smoke.py"
    SCRATCH.parent.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(mode=0o700)
    result = {
        "test_only": True,
        "scope": "Rust import of the same real 100k sample; not full-corpus qualification",
        "binary_sha256": BINARY,
        "input_sha256": digest(seed),
        "input_documents": header["actor_count"],
        "variants": [],
    }
    before = shutil.disk_usage(SCRATCH).free
    started = time.monotonic()
    maximum = 0

    def expired(*_):
        raise TimeoutError("Import pilot budget expired")

    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)
    signal.signal(signal.SIGHUP, expired)
    signal.alarm(300)
    try:
        shutil.copy2(seed, SCRATCH / "seed.jsonl")
        shutil.copy2(BUILD / "code/typeahead-tantivy", SCRATCH / "reader")
        shutil.copy2(helper, SCRATCH / "smoke.py")
        spec = importlib.util.spec_from_file_location(
            "pilot_smoke", SCRATCH / "smoke.py"
        )
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        smoke.BIN = SCRATCH / "reader"
        os.environ["TA_RETRIEVAL_STRATEGY"] = "global-merge"
        queries = [
            {"q": q, "limit": 10}
            for q in ["a", "alice", "john", "nate", "東京", "atproto"]
        ]
        if handle:
            queries.append({"q": handle, "limit": 10})
        if scope:
            queries.append({"q": "a", "limit": 10, "pds": scope})
        reference = None
        for name, memory, batch in [
            ("baseline-first", 128, 50000),
            ("memory", 512, 50000),
            ("batch-memory", 512, 100000),
            ("large-memory", 1024, 100000),
            ("baseline-last", 128, 50000),
        ]:
            case = SCRATCH / name
            case.mkdir()
            data = case / "data"
            began = time.monotonic()
            with (
                (case / "import.out").open("wb") as output,
                (case / "import.log").open("wb") as log,
            ):
                child = subprocess.Popen(
                    [
                        str(smoke.BIN),
                        "import",
                        "--data",
                        str(data),
                        "--input",
                        str(SCRATCH / "seed.jsonl"),
                        "--allow-partial",
                        "true",
                        "--writer-memory-mb",
                        str(memory),
                        "--batch-size",
                        str(batch),
                    ],
                    stdout=output,
                    stderr=log,
                    start_new_session=True,
                )
                while True:
                    pid, status, usage = os.wait4(child.pid, os.WNOHANG)
                    used = sum(
                        p.stat().st_size for p in SCRATCH.rglob("*") if p.is_file()
                    )
                    maximum = max(maximum, used)
                    if used > 1024**3 or time.monotonic() - began > 90:
                        raise TimeoutError("Import variant exceeded resource bounds")
                    if pid:
                        child.returncode = os.waitstatus_to_exitcode(status)
                        break
                    time.sleep(0.1)
            if child.returncode:
                raise RuntimeError("Import variant failed")
            elapsed = time.monotonic() - began
            metadata = json.loads((case / "import.out").read_bytes())
            if (
                metadata["full_source"] is not False
                or metadata["input_actors"] != header["actor_count"]
            ):
                raise ValueError("Sample import identity differs")
            identity = {
                key: metadata[key]
                for key in [
                    "format",
                    "build_id",
                    "source_manifest",
                    "input_actors",
                    "actor_count",
                    "tombstone_count",
                    "indexed_memberships",
                    "expiry_coverage",
                ]
            }
            with (case / "server.log").open("wb") as log:
                server = None
                try:
                    server, base, _ = smoke.start(
                        data, log, "offline-import-pilot-not-production"
                    )
                    replies = []
                    for query in queries:
                        code, _, body = smoke.request(
                            base, "/search?" + urllib.parse.urlencode(query)
                        )
                        if code != 200:
                            raise ValueError("Sample reader did not answer")
                        replies.append(
                            hashlib.sha256(
                                json.dumps(
                                    body, sort_keys=True, separators=(",", ":")
                                ).encode()
                            ).hexdigest()
                        )
                finally:
                    if server:
                        smoke.stop(server)
            fingerprint = hashlib.sha256(
                json.dumps(
                    [identity, replies], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if reference is None:
                reference = fingerprint
            if fingerprint != reference:
                raise ValueError("Import semantics or results changed")
            merge = []
            for line in (case / "import.log").read_bytes().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "import_merge":
                    merge.append(event)
            result["variants"].append(
                {
                    "name": name,
                    "writer_memory_mb": memory,
                    "batch_size": batch,
                    "import_seconds": elapsed,
                    "cpu_seconds": usage.ru_utime + usage.ru_stime,
                    "peak_rss_kb": usage.ru_maxrss,
                    "fingerprint": fingerprint,
                    "queries": len(queries),
                    "merge": merge,
                }
            )
            shutil.rmtree(case)
        result["passed"] = True
    except (OSError, ValueError, RuntimeError, AssertionError) as error:
        result.update(passed=False, error_type=type(error).__name__)
    finally:
        signal.alarm(0)
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes().split(b"\0")
                if command[0] != str(SCRATCH / "reader").encode():
                    continue
                os.killpg(int(entry.name), signal.SIGKILL)
                try:
                    os.waitpid(int(entry.name), 0)
                except ChildProcessError:
                    pass
            except (OSError, ValueError):
                continue
        shutil.rmtree(SCRATCH)
        result.update(
            elapsed_seconds=time.monotonic() - started,
            max_scratch_bytes=maximum,
            scratch_removed=not SCRATCH.exists(),
            free_bytes_before=before,
            free_bytes_after=shutil.disk_usage(SCRATCH.parent).free,
        )
    print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
