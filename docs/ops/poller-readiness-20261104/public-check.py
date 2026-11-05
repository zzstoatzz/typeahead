import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

expected, output = sys.argv[1:3]
bits = int.from_bytes(hashlib.sha256(str(time.time_ns()).encode()).digest(), "big")
term = "".join(c.upper() if bits & (1 << i) else c.lower()
               for i, c in enumerate("zzstoatzz.io"))
rows, bodies = [], []
for endpoint, scoped in [("tech.waow.typeahead.searchActors", False),
                         ("app.bsky.actor.searchActorsTypeahead", False),
                         ("tech.waow.typeahead.searchActors", True)]:
    params = dict(q=term, limit=7)
    if scoped:
        params["pds"] = "https://pds.zzstoatzz.io"
    url = "https://typeahead.waow.tech/xrpc/" + endpoint + "?" + urllib.parse.urlencode(params)
    started = time.monotonic()
    with urllib.request.urlopen(urllib.request.Request(url,
            headers={"User-Agent": "typeahead-readiness-check/1.0"}), timeout=10) as response:
        body = response.read(1024 * 1024)
        actors = json.loads(body)["actors"]
        assert response.status == 200
        assert response.headers["X-Typeahead-Engine"] == "tantivy"
        assert response.headers["X-Typeahead-Machine-Id"] == expected
        assert any(a["did"] == "did:plc:xbtmt2zjwlrfegqvch7fboei" for a in actors)
        assert response.headers.get("X-Typeahead-Failover") is None
        rows.append(dict(endpoint=endpoint, scoped=scoped, query=term, status=200,
                         elapsed_ms=(time.monotonic()-started)*1000,
                         headers={k:v for k,v in response.headers.items()
                                  if k.lower().startswith("x-typeahead-") or k.lower()=="server-timing"},
                         body_sha256=hashlib.sha256(body).hexdigest(), actor_count=len(actors)))
        bodies.append(body)
assert bodies[0] == bodies[1]
result = dict(passed=True, at=time.time(), expected_machine=expected,
              aliases_byte_identical=True, samples=rows)
Path(output).write_text(json.dumps(result, indent=2)+"\n")
print(json.dumps({k:v for k,v in result.items() if k!="samples"}))
