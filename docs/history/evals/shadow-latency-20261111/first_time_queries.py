"""Send queries neither backend has been sent by this session, one a second to each, and compare Server-Timing."""
import sys, time, urllib.request, urllib.parse, re, json
words = sys.argv[2].split()
out = {"production": [], "shadow": []}
hosts = {"production": "https://typeahead-tantivy-b.fly.dev", "shadow": "https://typeahead-shadow-a.fly.dev"}
for i, w in enumerate(words):
    order = ["production", "shadow"] if i % 2 == 0 else ["shadow", "production"]
    start = time.monotonic()
    for side in order:
        try:
            with urllib.request.urlopen(hosts[side] + "/search?" + urllib.parse.urlencode({"q": w, "limit": 10}), timeout=10) as r:
                r.read(); m = re.search(r"dur=([0-9.]+)", r.headers.get("server-timing", ""))
                out[side].append((float(m.group(1)), w))
        except Exception as e:
            print("error", side, w, e, file=sys.stderr)
    time.sleep(max(0, 1.1 - (time.monotonic() - start)))
json.dump(out, open(sys.argv[1], "w"))
def q(a, p): a = sorted(a); return a[min(len(a) - 1, int(len(a) * p))]
for side in out:
    a = [x for x, _ in out[side]]
    print(f"{side:10s} n={len(a)} p50={q(a,.5):.2f} p90={q(a,.9):.2f} p95={q(a,.95):.2f} p99={q(a,.99):.2f} max={max(a):.2f}")
