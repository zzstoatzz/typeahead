import concurrent.futures
import hashlib
import json
import re
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parent
origins = {"a": "https://typeahead-tantivy-a.fly.dev", "b": "https://typeahead-tantivy-b.fly.dev"}
queries = [dict(q=q, limit=10) for q in [
    "zzstoatzz.io", "jola.dev", "pfrazee.com", "dholms.xyz", "alice", "nate",
    "john", "rebecca", "東京", "atproto", "jay", "dan"]]
queries += [dict(q="zzstoatzz", limit=10, pds="https://pds.zzstoatzz.io")]

def request(item):
    name, query = item
    started = time.monotonic()
    url = origins[name] + "/search?" + urllib.parse.urlencode(query)
    with urllib.request.urlopen(urllib.request.Request(url,
            headers={"User-Agent": "typeahead-readiness-check/1.0"}), timeout=10) as response:
        body = response.read(1024 * 1024)
        assert response.status == 200
        assert response.headers["X-Typeahead-Binary-Sha256"] == "ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d"
        assert response.headers["X-Typeahead-Build"] == "b1793815407-cp-1f545dd4aa"
        assert response.headers["X-Typeahead-Retrieval-Strategy"] == "global-merge"
        if "pds" in query:
            assert response.headers["X-Typeahead-Pds"] == query["pds"]
        return dict(target=name, query=query, status=200,
                    engine_ms=float(re.search(r"search;dur=([0-9.]+)", response.headers["Server-Timing"])[1]),
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    body_sha256=hashlib.sha256(body).hexdigest(),
                    actor_count=len(json.loads(body)["actors"]))

rows, pairs = [], []
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    for repeat in range(3):
        for query in queries:
            pair = list(pool.map(request, [(name, query) for name in origins]))
            for row in pair:
                row["warmup"] = repeat == 0
                rows.append(row)
            pairs.append(dict(query=query, warmup=repeat == 0,
                              identical=pair[0]["body_sha256"] == pair[1]["body_sha256"]))

def metrics(name):
    times = sorted(row["engine_ms"] for row in rows if row["target"] == name and not row["warmup"])
    return dict(samples=len(times), median_ms=statistics.median(times),
                sample_p95_ms=times[int((len(times) - 1) * .95)], max_ms=max(times))

a, b = metrics("a"), metrics("b")
passed = (all(pair["identical"] for pair in pairs) and
          b["median_ms"] <= a["median_ms"] + 2 and
          b["sample_p95_ms"] <= a["sample_p95_ms"] + 5 and
          b["sample_p95_ms"] <= 100)
result = dict(passed=passed, a=a, b=b, pairs=pairs, samples=rows,
              acceptance="All bodies identical; candidate median <= baseline +2ms, sample p95 <= baseline +5ms and <=100ms.",
              limitations="Small paired warm-engine regression, not a production percentile or sustained-load SLO.")
(root / "matched-performance.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({k: result[k] for k in ["passed", "a", "b"]}))
raise SystemExit(0 if passed else 1)
