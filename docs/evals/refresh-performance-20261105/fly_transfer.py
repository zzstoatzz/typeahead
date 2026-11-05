"""Read at most 384 MiB from one presigned artifact URL; no write credentials."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import tempfile
import time
import urllib.request

signal.alarm(600)
url = os.environ['PROBE_OBJECT_URL']
size = 128 * 1024**2
results = []
with tempfile.TemporaryDirectory(prefix='typeahead-transfer-perf-', dir='/tmp') as folder:
    target = Path(folder) / 'prefix'
    for name, streams in [('serial', 1), ('parallel', 4), ('serial-repeat', 1)]:
        before = resource.getrusage(resource.RUSAGE_SELF)
        started = time.monotonic()
        with target.open('wb') as output:
            output.truncate(size)
        def transfer(index):
            start = index * size // streams
            end = (index + 1) * size // streams
            request = urllib.request.Request(url, headers={'Range':f'bytes={start}-{end-1}'})
            with urllib.request.urlopen(request, timeout=60) as response, target.open('r+b', buffering=0) as output:
                assert response.status == 206
                assert response.headers['Content-Range'].startswith(f'bytes {start}-{end-1}/')
                output.seek(start)
                remaining = end - start
                while remaining:
                    chunk = response.read(min(1024**2, remaining))
                    assert chunk
                    output.write(chunk)
                    remaining -= len(chunk)
                assert not response.read(1)
                os.fsync(output.fileno())
        with concurrent.futures.ThreadPoolExecutor(max_workers=streams) as executor:
            list(executor.map(transfer, range(streams)))
        elapsed = time.monotonic() - started
        after = resource.getrusage(resource.RUSAGE_SELF)
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if results:
            assert digest == results[0]['sha256']
        result = {'name':name, 'streams':streams, 'bytes':size, 'elapsed_s':elapsed,
            'mib_per_s':size / 1024**2 / elapsed, 'cpu_s':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
            'sha256':digest}
        results.append(result)
        print(json.dumps({'event':'transfer_performance_variant', **result}), flush=True)
        target.unlink()
print(json.dumps({'event':'transfer_performance_complete', 'machine':os.environ.get('FLY_MACHINE_ID'),
    'results':results, 'scratch_removed':True}), flush=True)
