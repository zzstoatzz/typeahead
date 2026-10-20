"""Bounded checkpoint download with admission checks; no publication or retries."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.request
from pds_bundle_download import Budget


class HealthNotReady(RuntimeError):
    pass


def wait_for_admission(check, *, timeout=120, interval=10):
    if not 0 < interval <= timeout <= 120:
        raise ValueError('Admission pause must be bounded by 120 seconds')
    deadline = time.monotonic() + timeout
    paused = False
    healthy = 0
    while True:
        try:
            check()
            healthy += 1
            if not paused or healthy >= 2:
                return
        except HealthNotReady:
            paused = True
            healthy = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HealthNotReady('Admission did not recover within pause budget')
        time.sleep(min(interval, remaining))
        if time.monotonic() >= deadline:
            raise HealthNotReady('Admission did not recover within pause budget')


def download(base_url, token, output, *, max_bytes, bytes_per_second, admit):
    output = Path(output)
    partial = Path(str(output) + '.part')
    attempt = Path(str(output) + '.attempt.json')
    if output.exists() or partial.exists() or attempt.exists():
        raise ValueError('Existing checkpoint attempt requires reconciliation')
    if max_bytes < 1 or bytes_per_second < 1 or not callable(admit):
        raise ValueError('Transfer limits and admission required')
    admit()
    budget = Budget(Path(str(output) + '.budget.db'), max_bytes + 65536)
    budget.reserve(max_bytes + 65536)
    headers = {'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}
    with urllib.request.urlopen(urllib.request.Request(base_url.rstrip('/') + '/info', headers=headers), timeout=10) as response:
        payload = response.read(65537)
        if response.status != 200 or len(payload) > 65536:
            raise ValueError('Invalid checkpoint metadata response')
    generation = json.loads(payload)['current_generation']
    if type(generation) is not int or generation < 0:
        raise ValueError('Invalid checkpoint generation')
    metadata = {'generation': generation, 'max_bytes': max_bytes,
                'bytes_per_second': bytes_per_second, 'started_at': time.time(),
                'published': False}
    with attempt.open('x') as receipt:
        json.dump(metadata, receipt, indent=2)
        receipt.flush()
        os.fsync(receipt.fileno())
    admit()
    started = last_check = time.monotonic()
    total = 0
    digest = hashlib.sha256()
    request = urllib.request.Request(base_url.rstrip('/') + '/export/' + str(generation), headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response, partial.open('xb') as stream:
        if response.status != 200:
            raise ValueError('Checkpoint download failed')
        length = response.headers.get('Content-Length')
        if length is not None and int(length) > max_bytes:
            raise ValueError('Checkpoint exceeds transfer budget')
        while True:
            if time.monotonic() - last_check >= 10:
                admit()
                last_check = time.monotonic()
            chunk_started = time.monotonic()
            chunk = response.read(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError('Checkpoint exceeds transfer budget')
            stream.write(chunk)
            digest.update(chunk)
            delay = len(chunk) / bytes_per_second - (time.monotonic() - chunk_started)
            if delay > 0:
                time.sleep(delay)
        stream.flush()
        os.fsync(stream.fileno())
    admit()
    with sqlite3.connect(partial.resolve().as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('Checkpoint integrity check failed')
        columns = {r[1] for r in db.execute('PRAGMA table_info(actors)')}
        if not {'did','pds','updated_at','handle','quality_score'} <= columns:
            raise ValueError('Checkpoint lacks source actors')
    admit()
    partial.rename(output)
    result = {'generation': generation, 'bytes': total, 'sha256': digest.hexdigest(),
              'seconds': time.monotonic() - started, 'published': False}
    Path(str(output) + '.json').write_text(json.dumps(result, indent=2))
    return result
