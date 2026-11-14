import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parent
targets = {
    "a": "https://typeahead-tantivy-a.fly.dev/health",
    "b": "https://typeahead-tantivy-b.fly.dev/health",
}

def probe(item):
    name, url = item
    row = {"at": time.time(), "target": name}
    try:
        try:
            response = urllib.request.urlopen(urllib.request.Request(url,
                headers={"User-Agent": "typeahead-readiness-check/1.0"}), timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            row["status"] = response.status
            row["headers"] = {k: v for k, v in response.headers.items()
                              if k.lower().startswith("x-typeahead-")}
            data = json.loads(response.read(65537))
            row["data"] = {k: v for k, v in data.items() if k in (
                "ok", "ready", "sync_lag_s", "build_id", "stale", "probe_ok",
                "replica_lag_s", "coverage_deadline", "reasons")}
            if isinstance(data.get("actors"), list):
                row["actor_count"] = len(data["actors"])
            if "error" in data:
                row["api_error"] = data["error"]
    except (OSError, ValueError) as error:
        row["error"] = type(error).__name__
    return row

until = time.monotonic() + float(sys.argv[1])
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    with (root / "observations.jsonl").open("a", buffering=1) as log:
        tick = 0
        witnessed = False
        while time.monotonic() < until:
            started = time.monotonic()
            batch = list(targets.items())
            if tick % 4 == 0:
                batch.append(("public", "https://typeahead.waow.tech/health/freshness"))
            results = list(pool.map(probe, batch))
            for row in results:
                log.write(json.dumps(row, separators=(",", ":")) + "\n")
            if not witnessed and any(row["target"] == "a" and
                    row.get("data", {}).get("ready") is False for row in results):
                witnessed = True
                base = "https://typeahead.waow.tech/xrpc/"
                query = "?q=ZzStOaTzZ.iO&limit=7"
                requests = [("fallback-canonical", base + "tech.waow.typeahead.searchActors" + query),
                    ("fallback-alias", base + "app.bsky.actor.searchActorsTypeahead" + query),
                    ("fallback-scope", base + "tech.waow.typeahead.searchActors" + query + "&pds=https%3A%2F%2Fpds.zzstoatzz.io")]
                for row in pool.map(probe, requests):
                    log.write(json.dumps(row, separators=(",", ":")) + "\n")
            tick += 1
            time.sleep(max(0, min(15 - (time.monotonic() - started), until - time.monotonic())))
