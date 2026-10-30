"""Resume read-only verification of confirmed cloud repairs without replaying writes."""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
from pathlib import Path

from pds_cloud_pilot import API, read_json, source
from pds_repair_canary import Turso


def verify(directory, backend):
    if os.environ.get('FLY_APP_NAME') != 'typeahead-pds-repair':
        raise ValueError('Dedicated Fly repair app required')
    if backend not in ('https://typeahead-tantivy-a.fly.dev', 'https://typeahead-tantivy-b.fly.dev'):
        raise ValueError('Explicit managed backend required')
    directory = Path(directory)
    rows = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
    proposals = {r['did']: r for r in rows if r['event'] == 'would_write'}
    written = {r['receipt']['did'] for r in rows if r['event'] == 'write' and r['receipt']['state'] == 'confirmed'}
    passed = {r['did'] for r in rows if r['event'] == 'verification' and r['passed']}
    client = Turso({'url': os.environ['TURSO_URL'], 'token': os.environ['TURSO_AUTH_TOKEN']})
    started = time.monotonic()
    with (directory / 'verification-resumed.jsonl').open('x') as log:
        def event(kind, **values):
            log.write(json.dumps({'event': kind, 'at': time.time(), **values}) + '\n')
            log.flush()
            os.fsync(log.fileno())

        def get(url):
            for attempt in range(3):
                if time.monotonic() - started > 900:
                    raise TimeoutError('Verification budget exhausted')
                time.sleep(1.2)
                try:
                    return read_json(url)
                except urllib.error.HTTPError as error:
                    event('http_error', status=error.code, url=url, attempt=attempt + 1)
                    if error.code not in (429, 502, 503, 504) or attempt == 2:
                        raise
                    time.sleep(10 * (attempt + 1))

        event('start', confirmed=len(written), previously_passed=len(passed))
        for did in sorted(written - passed):
            item = proposals[did]
            current = source(client.execute, did)
            if current['pds'] != item['proposed_pds'] or current['handle'] != item['handle']:
                raise ValueError('Source changed; re-evaluate identity before proceeding')
            health = get(backend + '/health')
            if not health.get('ready') or health.get('sync_lag_s', 9999) > 120:
                raise ValueError('Serving health gate failed')
            for origin in (backend + '/search', API):
                for scope, expected in ((None, True), (urllib.parse.urlsplit(item['proposed_pds']).hostname, True), ('unrelated-pilot.invalid', False)):
                    params = {'q': item['handle'], 'limit': 10}
                    if scope:
                        params['pds'] = scope
                    result = get(origin + '?' + urllib.parse.urlencode(params))
                    actual = any(actor['did'] == did for actor in result['actors'])
                    if actual != expected:
                        event('mismatch', did=did, origin=origin, scope=scope)
                        raise ValueError('Search verification failed')
            event('verification', did=did, passed=True)
        event('complete', confirmed=len(written), newly_verified=len(written - passed), seconds=time.monotonic() - started)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--backend', required=True)
    args = parser.parse_args()
    verify(args.directory, args.backend)
