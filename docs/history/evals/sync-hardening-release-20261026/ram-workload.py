import hashlib
import json
import math
import os
import itertools
import random
import string
import sqlite3
import signal
import time
import urllib.parse
import urllib.request
from pathlib import Path

def interrupted(_signum, _frame):
    raise SystemExit('workload interrupted')

for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, interrupted)

bigrams = 'th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io le ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur na jo mi sa el da ca bo mo lo ki pa ru ta wa'.split()
queries = [''.join(pair) for pair in itertools.product(string.ascii_lowercase, repeat=2)]
queries += [prefix + vowel for prefix in bigrams for vowel in 'aeiou']
queries += ['a', 'nate', '日本', '東京', '中国', 'zzstoatzz.io', 'bsky.app']
queries = list(dict.fromkeys(queries))
random.Random(1729).shuffle(queries)
assert os.cpu_count() == 2
processes = [p.parent for p in Path('/proc').glob('[0-9]*/comm') if p.read_text().strip().startswith('typeahead-ing')]
assert len(processes) == 1
process = processes[0]

def stats():
    io = {k: int(v) for k, v in (line.split(':') for line in (process / 'io').read_text().splitlines())}
    s = (process / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'disk_read_bytes': io['read_bytes'], 'major_faults': int(s[9])}

def memory():
    return {k: int(v.split()[0]) * 1024 for k, v in (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines()) if k in ('MemTotal', 'MemAvailable', 'Cached', 'SwapTotal')}

def marker():
    c = sqlite3.connect('file:/data/refresh/overlay.db?mode=ro', uri=True)
    rows = c.execute("SELECT key,value FROM sync_meta WHERE key IN ('last_sync','overlay_compact_at') ORDER BY key").fetchall()
    c.close()
    return rows

def percentiles(samples):
    values = sorted(s['ms'] for s in samples)
    return {f'p{p}': values[min(len(values) - 1, math.ceil(len(values) * p / 100) - 1)] for p in (50, 95, 99)} if values else {}

start = time.monotonic()
records = []
result = {'queries': queries, 'cpu_count': os.cpu_count(), 'memory_before': memory(), 'samples': records, 'marker_before': marker()}
try:
    for round_id in (1, 2):
        for query in queries:
            if time.monotonic() - start > 120:
                raise TimeoutError('workload exceeded 120-second budget')
            before = stats()
            t0 = time.monotonic()
            url = 'http://127.0.0.1:8080/search?' + urllib.parse.urlencode({'q': query, 'limit': 8})
            with urllib.request.urlopen(url, timeout=5) as response:
                raw = response.read()
                status = response.status
            after = stats()
            records.append({'round': round_id, 'query': query, 'status': status, 'actors': len(json.loads(raw).get('actors', [])), 'sha256': hashlib.sha256(raw).hexdigest(), 'ms': round((time.monotonic() - t0) * 1000, 3), 'disk_read_bytes': after['disk_read_bytes'] - before['disk_read_bytes'], 'major_faults': after['major_faults'] - before['major_faults']})
    result['complete'] = True
except Exception as exc:
    result['complete'] = False
    result['error'] = type(exc).__name__
finally:
    result['elapsed_s'] = round(time.monotonic() - start, 3)
    result['memory_after'] = memory()
    result['marker_after'] = marker()
    result['sync_quiet'] = result['marker_before'] == result['marker_after']
    result['summary'] = {str(r): percentiles([s for s in records if s['round'] == r]) for r in (1, 2)}
    print(json.dumps(result))
