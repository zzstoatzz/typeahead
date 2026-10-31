"""Read-only scoped-search checks against the full cloud candidate."""
import json
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

root = Path('/tmp/typeahead-pds-join-full')
base = sys.argv[2]
if base != 'https://typeahead-tantivy-b.fly.dev':
    raise ValueError('Explicit inactive candidate required')

def request(path):
    req = urllib.request.Request(base + path, headers={'X-Client':'smoke-testing'})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)

def query(handle, pds):
    return request('/search?' + urllib.parse.urlencode({'q':handle,'pds':pds,'limit':100}))['actors']

expected_build = json.loads((root/'candidate-result.json').read_bytes())['build_id']
if request('/index/status').get('build_id') != expected_build:
    raise ValueError('Candidate build differs')
with sqlite3.connect((root/'source.db').as_uri()+'?mode=ro&immutable=1', uri=True) as source, \
     sqlite3.connect((root/'pds-fallback.sqlite').as_uri()+'?mode=ro&immutable=1', uri=True) as mapping:
    samples = []
    scanned = 0
    for did, handle, pds in source.execute("SELECT did,handle,pds FROM actors WHERE hidden=0 AND handle<>'' AND COALESCE(labels,'[]')='[]' ORDER BY did LIMIT 20000"):
        scanned += 1
        if pds:
            continue
        found = mapping.execute('SELECT pds FROM pds_fallback WHERE did=?', (did,)).fetchone()
        if found:
            samples.append((did,handle,found[0]))
        if len(samples) == 100:
            break
    if len(samples) < 100:
        raise ValueError('Insufficient joined verification accounts')

def check():
    latencies = []
    misses = []
    for did, handle, pds in samples:
        started = time.monotonic()
        actors = query(handle, pds)
        latencies.append((time.monotonic()-started)*1000)
        if did not in {row['did'] for row in actors}:
            misses.append(did)
        wrong = query(handle, 'wrong.invalid')
        if did in {row['did'] for row in wrong}:
            raise ValueError('Wrong-scope match')
    return {'accounts':len(samples),'misses':len(misses),'hosts':len({row[2] for row in samples}),
            'bsky_hosts':len({row[2] for row in samples if row[2].endswith('.host.bsky.network')}),
            'p50_ms':statistics.median(latencies),'p95_ms':sorted(latencies)[94],
            'max_ms':max(latencies)}

result = {'phase':sys.argv[1], 'sample_rows_scanned':scanned, 'scope':check()}
status = request('/index/status')
result['index'] = {key:status.get(key) for key in (
    'build_id','actor_count','pds_missing_actor_count','pds_invalid_actor_count',
    'ready','source_observation_revision','watermark')}
result['passed'] = result['scope']['misses'] == 0
(root/('verification-'+sys.argv[1]+'.json')).write_text(json.dumps(result,indent=2))
print(json.dumps(result))
if not result['passed']:
    raise SystemExit(1)
