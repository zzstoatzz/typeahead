"""Bounded cloud PDS repair pilot using the durable conditional-write ledger."""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pds_coverage import pds_from_document
from pds_repair_canary import Turso
from pds_repair_ledger import Ledger

CANDIDATES = {
    'bsky.app': 'did:plc:z72i7hdynmk6r22z27h6tvur',
    'safety.bsky.app': 'did:plc:eon2iu7v3x2ukgxkqaf7e5np',
    'status.bsky.app': 'did:plc:4dtbz2ivhp5app3sbntcccxc',
    'jay.bsky.team': 'did:plc:oky5czdrnfjpqslsw2a5iclo',
    'emilyliu.me': 'did:plc:vjug55kidv6sye7ykr5faxxn',
}
API = 'https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors'


def read_json(url, byte_budget=None):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'typeahead-community-smoke/1.0', 'X-Client': 'smoke-testing'})
    with urllib.request.urlopen(request, timeout=10) as response:
        bound = 1024 * 1024 if byte_budget is None else min(1024 * 1024, max(0, byte_budget['remaining']))
        raw = response.read(bound + 1)
        if byte_budget is not None:
            byte_budget['remaining'] -= len(raw)
            if byte_budget['remaining'] < 0:
                raise ValueError('Response transfer budget exhausted')
        if len(raw) > 1024 * 1024:
            raise ValueError('Response exceeds pilot budget')
        return json.loads(raw)


def source(execute, did):
    rows = execute('SELECT did,pds,updated_at,handle FROM actors WHERE did=:did', {'did': did})
    if len(rows) != 1:
        raise ValueError('Pilot source actor missing')
    return dict(zip(('did', 'pds', 'updated_at', 'handle'), rows[0]))


def prepare(execute, did, handle, document):
    current = source(execute, did)
    if current['handle'] != handle:
        raise ValueError('Pilot handle changed')
    pds = pds_from_document(did, document)
    parsed = urllib.parse.urlsplit(pds)
    if parsed.scheme != 'https' or parsed.port or parsed.path not in ('', '/'):
        raise ValueError('Pilot requires a canonical HTTPS PDS origin')
    if current['pds']:
        raise ValueError('Pilot only repairs empty PDS fields')
    return {**current, 'proposed_pds': pds, 'authority_checked_at': time.time()}


def apply_prepared(ledger, execute, item):
    if time.time() - item['authority_checked_at'] > 60:
        raise ValueError('Authority observation expired')
    attempt = ledger.prepare(item['did'], item['updated_at'], item['proposed_pds'])
    try:
        outcome = ledger.send(attempt, execute)
    except Exception:
        current = source(execute, item['did'])
        ledger.reconcile(attempt, current)
        raise
    if outcome != 'confirmed':
        raise ValueError('Source changed during pilot; no overwrite performed')
    return ledger.get(attempt)


def search_present(handle, did, pds=None):
    params = {'q': handle, 'limit': 10}
    if pds:
        params['pds'] = pds
    result = read_json(API + '?' + urllib.parse.urlencode(params))
    return any(actor['did'] == did for actor in result['actors'])


def run(output, backend, apply=False):
    if os.environ.get('FLY_APP_NAME') != 'typeahead-pds-repair' or not os.environ.get('FLY_MACHINE_ID'):
        raise ValueError('Pilot must execute on its dedicated Fly repair app')
    parsed = urllib.parse.urlsplit(backend)
    if parsed.scheme != 'https' or parsed.netloc not in ('typeahead-tantivy-a.fly.dev', 'typeahead-tantivy-b.fly.dev') or parsed.path:
        raise ValueError('Expected an explicit managed backend origin')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    client = Turso({'url': os.environ['TURSO_URL'], 'token': os.environ['TURSO_AUTH_TOKEN']})
    ledger = Ledger(output / 'ledger.db')
    started = time.monotonic()
    with (output / 'events.jsonl').open('x') as log:
        def event(kind, **values):
            log.write(json.dumps({'event': kind, 'at': time.time(), **values}) + '\n')
            log.flush()
            os.fsync(log.fileno())

        def health():
            if time.monotonic() - started > 600:
                raise TimeoutError('Pilot wall-time budget exceeded')
            status = read_json(backend + '/health')
            if not status.get('ready') or status.get('engine') != 'tantivy' or status.get('sync_lag_s', 9999) > 120:
                raise ValueError('Serving health gate failed')
            return status

        event('start', apply=apply, health=health())
        for handle, did in CANDIDATES.items():
            health()
            item = prepare(client.execute, did, handle, read_json('https://plc.directory/' + did))
            if not search_present(handle, did):
                raise ValueError('Global baseline missing')
            event('would_write', **item)
            if not apply:
                continue
            write_started = time.monotonic()
            receipt = apply_prepared(ledger, client.execute, item)
            event('write', receipt=receipt, seconds=time.monotonic() - write_started)
            current = source(client.execute, did)
            if current['pds'] != item['proposed_pds'] or current['handle'] != handle:
                raise ValueError('Source verification failed')
            host = urllib.parse.urlsplit(item['proposed_pds']).hostname
            deadline = min(started + 600, time.monotonic() + 180)
            while True:
                health()
                if search_present(handle, did, host):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Scoped result did not propagate')
                time.sleep(10)
            if not search_present(handle, did) or search_present(handle, did, 'unrelated-pilot.invalid'):
                raise ValueError('Global or negative-scope verification failed')
            event('verified', did=did, propagation_seconds=time.monotonic() - write_started, health=health())
        event('complete', seconds=time.monotonic() - started, apply=apply)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--backend', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    run(args.output, args.backend, args.apply)
