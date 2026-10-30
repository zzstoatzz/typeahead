"""Checkpointed cloud discovery; each invocation reads a bounded source rowid range."""
import argparse
import fcntl
import json
import os
import shutil
import time
from pathlib import Path

from pds_freshness_queue import Queue
from pds_repair_canary import Turso

PAGE_SQL = 'SELECT rowid,did,pds FROM actors WHERE rowid>:cursor ORDER BY rowid LIMIT :limit'


def discover(execute, queue, pages=10, page_size=1000):
    if not 1 <= pages <= 100 or not 1 <= page_size <= 1000:
        raise ValueError('Discovery bounds exceeded')
    result = {'rows': 0, 'inserted': 0, 'excluded': 0, 'caught_up': False}
    for _ in range(pages):
        rows = execute(PAGE_SQL, {'cursor': queue.cursor(), 'limit': page_size})
        if not rows:
            result['caught_up'] = True
            break
        counts = queue.discover_records(rows, rows[-1][0])
        result['rows'] += len(rows)
        for key in ('inserted', 'excluded'):
            result[key] += counts[key]
    return {**result, 'cursor': queue.cursor()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--pages', type=int, default=10)
    args = parser.parse_args()
    if os.environ.get('FLY_APP_NAME') != 'typeahead-pds-repair' or not os.environ.get('FLY_MACHINE_ID'):
        raise ValueError('Dedicated Fly repair app required')
    directory = Path(args.directory)
    directory.mkdir(exist_ok=True, parents=True)
    lock = (directory / 'writer.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    transfer = {'remaining': 16 * 1024 * 1024}
    client = Turso({'url': os.environ['TURSO_URL'], 'token': os.environ['TURSO_AUTH_TOKEN']}, byte_budget=transfer)
    started = time.monotonic()

    def execute(sql, params):
        if time.monotonic() - started >= 120 or shutil.disk_usage(directory).free < 128 * 1024 * 1024:
            raise ValueError('Discovery time/disk budget exhausted')
        return client.execute(sql, params)

    result = discover(execute, Queue(directory / 'queue.db'), pages=args.pages)
    result.update(at=time.time(), seconds=time.monotonic()-started, response_bytes=16 * 1024 * 1024-transfer['remaining'])
    with (directory / 'discovery.jsonl').open('a') as log:
        log.write(json.dumps(result)+'\n')
        log.flush()
        os.fsync(log.fileno())
    print(json.dumps(result))
