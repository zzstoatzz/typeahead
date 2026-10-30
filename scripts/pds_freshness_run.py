"""Bounded cloud queue execution using persistent state and one writer lock."""
import argparse
import fcntl
import json
import os
import shutil
import time
from pathlib import Path

from pds_cloud_pilot import read_json
from pds_freshness_queue import Queue
from pds_freshness_worker import refresh_batch
from pds_repair_canary import Turso
from pds_repair_ledger import Ledger


def run(directory, backend, seconds=600, limit=100):
    if os.environ.get('FLY_APP_NAME') != 'typeahead-pds-repair' or not os.environ.get('FLY_MACHINE_ID'):
        raise ValueError('Dedicated Fly repair app required')
    if backend not in ('https://typeahead-tantivy-a.fly.dev', 'https://typeahead-tantivy-b.fly.dev'):
        raise ValueError('Explicit managed backend required')
    if not 60 <= seconds <= 3600 or not 1 <= limit <= 1000:
        raise ValueError('Bounded runtime and identity limit required')
    directory = Path(directory)
    if not (directory / 'queue.db').is_file():
        raise ValueError('A populated persistent queue is required')
    with (directory / 'writer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue = Queue(directory / 'queue.db')
        ledger = Ledger(directory / 'ledger.db')
        client = Turso({'url': os.environ['TURSO_URL'], 'token': os.environ['TURSO_AUTH_TOKEN']})
        started = time.monotonic()
        deadline = started + seconds
        counts = {'identities': 0, 'source_statements': 0, 'write_batches': 0, 'authority_requests': 0}
        with (directory / 'freshness.jsonl').open('a') as log:
            def event(kind, **values):
                log.write(json.dumps({'event': kind, 'at': time.time(), **values}) + '\n')
                log.flush()
                os.fsync(log.fileno())

            def execute(sql, params):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Execution budget exhausted')
                counts['source_statements'] += 1
                if sql.startswith('UPDATE'):
                    counts['write_batches'] += 1
                return client.execute(sql, params)

            def resolve(did):
                if not did.startswith('did:plc:'):
                    raise ValueError('Unsupported authority method')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Execution budget exhausted')
                counts['authority_requests'] += 1
                time.sleep(0.2)
                return read_json('https://plc.directory/' + did)

            event('start', limit=limit, seconds=seconds, queue=queue.metrics())
            while counts['identities'] < limit and time.monotonic() < deadline - 30:
                if shutil.disk_usage(directory).free < 64 * 1024 * 1024:
                    raise ValueError('Persistent state disk reserve exhausted')
                health = read_json(backend + '/health')
                if not health.get('ready') or health.get('sync_lag_s', 9999) > 120:
                    raise ValueError('Serving health gate failed')
                claims = queue.claim(limit=min(10, limit-counts['identities']), lease_seconds=300)
                if not claims:
                    break
                try:
                    results = refresh_batch(execute, ledger, queue, claims, resolve)
                except Exception as error:
                    event('stopped', reason=type(error).__name__, counts=counts, queue=queue.metrics())
                    raise
                counts['identities'] += len(claims)
                event('batch', results=results, counts=counts)
                time.sleep(1)
            event('complete', counts=counts, seconds=time.monotonic()-started, queue=queue.metrics())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--backend', required=True)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    run(args.directory, args.backend, args.seconds, args.limit)
