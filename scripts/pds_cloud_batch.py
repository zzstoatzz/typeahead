"""Bounded cloud PDS discovery and repair, with durable per-identity receipts."""
import argparse
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from pds_cloud_pilot import API, read_json, prepare, apply_prepared, source
from pds_freshness_queue import Queue
from pds_repair_canary import Turso
from pds_repair_ledger import Ledger

PAGE_SQL = 'SELECT rowid,did,handle,pds,updated_at,hidden FROM actors WHERE rowid>:cursor ORDER BY rowid LIMIT :limit'


def discover(execute, queue, limit, max_pages=10):
    selected = []
    scanned = 0
    cursor = queue.cursor()
    for _ in range(max_pages):
        rows = execute(PAGE_SQL, {'cursor': cursor, 'limit': 1000})
        if not rows:
            break
        for rowid, did, handle, pds, updated, hidden in rows:
            cursor = rowid
            scanned += 1
            if pds == '' and handle and handle != 'handle.invalid' and not hidden and re.fullmatch(r'did:plc:[a-z2-7]{24}', did):
                selected.append({'did': did, 'handle': handle})
            if len(selected) >= limit:
                break
        queue.discover([item['did'] for item in selected], cursor)
        if len(selected) >= limit:
            break
    return selected, scanned


def run(output, backend, limit=100, apply=False, wall_seconds=1200):
    if os.environ.get('FLY_APP_NAME') != 'typeahead-pds-repair' or not os.environ.get('FLY_MACHINE_ID'):
        raise ValueError('Dedicated Fly repair app required')
    if backend not in ('https://typeahead-tantivy-a.fly.dev', 'https://typeahead-tantivy-b.fly.dev'):
        raise ValueError('Explicit managed backend required')
    if limit not in (100, 1000) or not 60 <= wall_seconds <= 3600:
        raise ValueError('Expected bounded 100/1000 stage')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    client = Turso({'url': os.environ['TURSO_URL'], 'token': os.environ['TURSO_AUTH_TOKEN']})
    queue = Queue(output / 'queue.db')
    ledger = Ledger(output / 'ledger.db')
    started = time.monotonic()
    next_public = 0
    counts = {'source_statements': 0, 'source_writes': 0, 'public_requests': 0, 'authority_requests': 0}
    with (output / 'events.jsonl').open('x') as log:
        def event(kind, **values):
            log.write(json.dumps({'event': kind, 'at': time.time(), **values}) + '\n')
            log.flush()
            os.fsync(log.fileno())

        def budget():
            if time.monotonic() - started >= wall_seconds:
                raise TimeoutError('Stage time budget exhausted')

        def execute(sql, params):
            budget()
            counts['source_statements'] += 1
            if sql.startswith('UPDATE'):
                counts['source_writes'] += 1
            return client.execute(sql, params)

        def health():
            budget()
            value = read_json(backend + '/health')
            if not value.get('ready') or value.get('sync_lag_s', 9999) > 120:
                raise ValueError('Serving health gate failed')
            return value

        def present(item, pds=None):
            nonlocal next_public
            budget()
            time.sleep(max(0, next_public - time.monotonic()))
            next_public = time.monotonic() + 1.1
            params = {'q': item['handle'], 'limit': 10}
            if pds:
                params['pds'] = pds
            counts['public_requests'] += 1
            value = read_json(API + '?' + urllib.parse.urlencode(params))
            return any(actor['did'] == item['did'] for actor in value['actors'])

        event('start', apply=apply, limit=limit, health=health())
        items, scanned = discover(execute, queue, limit)
        event('discovery', selected=len(items), scanned=scanned, source_cursor=queue.cursor())
        by_did = {item['did']: item for item in items}
        confirmed = []
        claims = []
        for offset in range(0, len(items), 100):
            claims.extend(queue.claim(limit=min(100, len(items)-offset), lease_seconds=wall_seconds))
        for claim in claims:
            item = by_did[claim['did']]
            did, token = item['did'], claim['token']
            if not present(item):
                queue.failed(did, token, 'global baseline absent')
                event('unresolved', did=did, reason='global baseline absent')
                continue
            health()
            counts['authority_requests'] += 1
            try:
                proposal = prepare(execute, did, item['handle'], read_json('https://plc.directory/' + did))
            except (ValueError, OSError) as error:
                queue.failed(did, token, type(error).__name__)
                event('unresolved', did=did, reason=type(error).__name__)
                continue
            event('would_write', **proposal)
            if not apply:
                continue
            queue.begin_write(did, token)
            try:
                receipt = apply_prepared(ledger, execute, proposal)
            except Exception as error:
                queue.failed(did, token, type(error).__name__)
                event('write_failed', did=did, reason=type(error).__name__)
                raise
            current = source(execute, did)
            if current['pds'] != proposal['proposed_pds'] or current['handle'] != item['handle']:
                queue.failed(did, token, 'source readback differs')
                raise ValueError('Source verification failed')
            queue.verified(did, token, proposal['proposed_pds'], int(proposal['authority_checked_at']))
            confirmed.append(proposal)
            event('write', receipt=receipt)
        if apply and confirmed:
            budget()
            time.sleep(min(75, max(0, wall_seconds - (time.monotonic()-started))))
            for item in confirmed:
                health()
                host = urllib.parse.urlsplit(item['proposed_pds']).hostname
                direct = read_json(backend + '/search?' + urllib.parse.urlencode({'q': item['handle'], 'pds': host, 'limit': 10}))
                if not any(a['did'] == item['did'] for a in direct['actors']):
                    raise ValueError('Direct backend scope verification failed')
                good = present(item, host) and present(item) and not present(item, 'unrelated-pilot.invalid')
                event('verification', did=item['did'], passed=good)
                if not good:
                    raise ValueError('Public verification failed; stop expansion')
        event('complete', confirmed=len(confirmed), seconds=time.monotonic()-started, counts=counts, queue=queue.metrics(), health=health())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--backend', required=True)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--wall-seconds', type=int, default=1200)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    run(args.output, args.backend, args.limit, args.apply, args.wall_seconds)
