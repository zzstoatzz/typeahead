import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def request(port, path, timeout=10):
    started = time.monotonic()
    try:
        response = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        return {
            "status": response.status,
            "ms": round((time.monotonic() - started) * 1000, 3),
            "sha256": hashlib.sha256(body).hexdigest(),
            "body": json.loads(body),
        }


def evict(paths):
    os.sync()
    for path in paths:
        with path.open("rb") as source:
            os.posix_fadvise(source.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)


def process_metrics(pid):
    root = Path(f"/proc/{pid}")
    result = {}
    for line in (root / "io").read_text().splitlines():
        key, value = line.split(":", 1)
        result[key] = int(value)
    for line in (root / "status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, value = line.split(":", 1)
            result[key] = int(value.split()[0]) * 1024
    return result


def measure(args, variant, number, frozen, queries):
    overlay = args.output.parent / "trial-overlay.db"
    for path in (overlay, Path(str(overlay) + "-wal"), Path(str(overlay) + "-shm")):
        path.unlink(missing_ok=True)
    shutil.copyfile(frozen, overlay)
    evict((args.snapshot, Path(str(args.snapshot) + ".prefix"), frozen, overlay))
    env = dict(os.environ, TYPEAHEAD_EVAL_SNAPSHOT=str(args.snapshot),
               LOCAL_DB_PATH=str(overlay), PORT=str(args.port), READ_POOL_SIZE="8")
    result = {"variant": variant, "round": number, "queries": []}
    started = time.monotonic()
    with (args.output.parent / f"{number}-{variant}.log").open("w") as log:
        process = subprocess.Popen([str(args.bin_dir / variant)], env=env, stdout=log, stderr=log)
        try:
            last_report = 0
            while time.monotonic() - started < args.startup_timeout:
                if process.poll() is not None:
                    raise RuntimeError(f"{variant} exited: {process.returncode}")
                try:
                    health = request(args.port, "/health", timeout=0.5)
                    if health["status"] == 200 and health["body"].get("ready"):
                        result["startup_seconds"] = round(time.monotonic() - started, 3)
                        result["startup_process"] = process_metrics(process.pid)
                        result["index"] = request(args.port, "/index/status")["body"]
                        print("ready", variant, result["startup_seconds"], flush=True)
                        break
                except (OSError, ValueError):
                    pass
                if time.monotonic() - last_report >= 15:
                    print("loading", variant, round(time.monotonic() - started), process_metrics(process.pid), flush=True)
                    last_report = time.monotonic()
                time.sleep(0.005)
            else:
                raise TimeoutError(f"{variant} startup")
            for phase in ("first_pass", "warm"):
                for query in queries:
                    params = {"q": query, "limit": 50}
                    path = "/search?" + urllib.parse.urlencode(params)
                    row = {"phase": phase, "q": query}
                    try:
                        response = request(args.port, path)
                        row.update({key: response[key] for key in ("status", "ms", "sha256")})
                        row["actors"] = len(response["body"].get("actors", []))
                    except (OSError, ValueError) as error:
                        row["error"] = str(error)
                    result["queries"].append(row)
                timings = [r["ms"] for r in result["queries"] if r["phase"] == phase and "ms" in r]
                print(variant, phase, "median_ms", statistics.median(timings), "max_ms", max(timings), flush=True)
            for pds in ("selfhosted.social", "https://selfhosted.social"):
                result.setdefault("scoped", []).append(request(args.port, "/search?" + urllib.parse.urlencode({"q": "a", "pds": pds, "limit": 50})))
            result["final_process"] = process_metrics(process.pid)
            result["health"] = request(args.port, "/health")["body"]
            return result
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, default=Path("/app"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--startup-timeout", type=int, default=1200)
    parser.add_argument("--variants", nargs="+", default=["baseline", "candidate", "candidate", "baseline"], choices=["baseline", "candidate"])
    args = parser.parse_args()
    if not os.environ.get("FLY_APP_NAME", "").startswith("typeahead-startup-"):
        parser.error("requires an isolated typeahead-startup-* Fly application")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frozen = args.output.parent / "frozen-overlay.db"
    with sqlite3.connect(args.overlay.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(frozen) as target:
            source.backup(target)
    queries = json.loads(args.queries.read_text())
    result = {"app": os.environ["FLY_APP_NAME"], "snapshot": str(args.snapshot),
              "queries": queries, "runs": [], "cache_policy": "per-file POSIX_FADV_DONTNEED before each process; copied private overlay"}
    for number, variant in enumerate(args.variants):
        result["runs"].append(measure(args, variant, number, frozen, queries))
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
