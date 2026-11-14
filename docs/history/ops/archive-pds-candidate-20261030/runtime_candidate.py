"""Bounded candidate runtime inside a 1-GiB cloud container; no routing changes."""
import json
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

children = []

def finish(*_):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, finish)
signal.signal(signal.SIGINT, finish)
signal.signal(signal.SIGALRM, finish)
signal.alarm(900)
try:
    manifest = json.loads(Path('/candidate/manifest.json').read_bytes())
    import sys
    sys.path.insert(0, '/app')
    import tantivy_install
    tantivy_install.validate_fallback_file('/data', manifest)
    server = subprocess.Popen(['/app/typeahead-tantivy', 'serve', '--data', '/data',
        '--listen', '0.0.0.0:8080', '--admin-listen', '127.0.0.1:8081',
        '--retrieval', 'global-merge', '--max-sync-lag-s', '120'])
    children.append(server)
    deadline = time.monotonic() + 60
    while True:
        if server.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError('Candidate reader failed to start')
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(.2)
    fallback = manifest['pds_fallback']
    sync = subprocess.Popen(['python3', '/app/input/sync.py',
        '--source-mode','observed','--policy-mode','snapshot',
        '--status-url','http://127.0.0.1:8080/index/status',
        '--pds-fallback','/data/pds-fallback.sqlite','--pds-fallback-sha256',fallback['sha256'],
        '--budget-file','/data/source-budget.json','--budget-build-id',manifest['active_build'],
        '--status-file','/data/source-status.json','--max-run-seconds','840',
        '--max-source-bytes','536870912','--poll-reserve-bytes','134217728',
        '--anti-entropy-pages','64','--max-sweep-seconds','20','--max-cycle-seconds','30',
        '--interval','40'])
    children.append(sync)
    while all(child.poll() is None for child in children):
        time.sleep(1)
    raise RuntimeError('Candidate runtime child exited')
finally:
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
