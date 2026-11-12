"""The public read endpoints: /actors and /corpus, and the loopback
/admin/corpus the co-located store reports its count to."""
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import smoke

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get('TA_BINARY', str(ROOT / 'target/release/typeahead-tantivy')))
smoke.BIN = BIN

STORE_COUNT = {'live': 9, 'total': 8, 'with_handles': 7, 'with_avatars': 5, 'hidden': 1, 'scanned_at': int(time.time())}


def actors(base, *names):
    query = urllib.parse.urlencode([('actor', name) for name in names])
    return smoke.request(base, '/actors?' + query)


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix='tantivy-actor-read-') as temporary:
        root = Path(temporary)
        data, seed, secret = root / 'data', root / 'seed.jsonl', secrets.token_urlsafe(32)
        rows = [
            smoke.actor(1, 'Alice.Read.Test', 'Alice Reader', 10),
            smoke.actor(2, 'hidden.read.test', 'Hidden Reader', 5, hidden=True),
            smoke.actor(3, 'handle.invalid', 'Unverified Reader', 1),
        ]
        rows[0].update(description='line one\nline two', banner_url='bafkbanner', updated_at=1794000000,
                       profile_checked=True, followers_count=7, follows_count=3, posts_count=2)
        smoke.write_seed(seed, rows)
        imported = subprocess.run(
            [str(BIN), 'import', '--data', str(data), '--input', str(seed), '--allow-partial', 'true'],
            capture_output=True, text=True, timeout=30)
        assert imported.returncode == 0, imported.stderr

        with (root / 'server.log').open('w') as log:
            child, base, admin = smoke.start(data, log, secret)
            try:
                binary = hashlib.sha256(BIN.read_bytes()).hexdigest()

                status, headers, body = actors(base, rows[0]['did'])
                assert status == 200, body
                assert headers['x-typeahead-engine'] == 'tantivy' and headers['x-typeahead-binary-sha256'] == binary
                assert headers['x-typeahead-build'] and headers['x-typeahead-format'] and headers['x-typeahead-retrieval-strategy']
                assert headers['cache-control'] == 'no-store'
                assert body == {'actors': [{
                    'did': rows[0]['did'], 'handle': 'Alice.Read.Test', 'display_name': 'Alice Reader',
                    'avatar_url': 'bafkfixture', 'labels': [], 'created_at': '2026-10-28T00:00:00Z', 'associated': {},
                    'pds': 'https://local.test', 'description': 'line one\nline two', 'banner_url': 'bafkbanner',
                    'followers_count': 7, 'follows_count': 3, 'posts_count': 2, 'quality_score': 10.0,
                    'updated_at': 1794000000, 'profile_checked': True, 'hidden': False, 'hidden_stored': False,
                }]}, body
                checks.append('stored_fields_by_did_with_the_slot_identity_headers')

                status, _, body = actors(base, 'alice.read.test', rows[1]['did'], 'nobody.read.test', rows[0]['did'])
                assert status == 200 and [a['did'] for a in body['actors']] == [rows[0]['did'], rows[1]['did']], body
                assert [a['hidden'] for a in body['actors']] == [False, True]
                assert body['actors'][1]['description'] == '' and body['actors'][1]['profile_checked'] is False
                checks.append('by_handle_in_request_order_once_each_with_hidden_actors_flagged')

                assert actors(base)[0] == 400
                assert actors(base, *[f'did:plc:test{n:04}' for n in range(26)])[0] == 400
                assert actors(base, '')[0] == 400
                assert smoke.request(base, '/actors?actor=' + 'a' * 2049)[0] == 400
                checks.append('no_actor_too_many_and_oversized_are_refused')

                status, headers, body = smoke.request(base, '/corpus')
                assert status == 200 and headers['x-typeahead-binary-sha256'] == binary
                assert body['store'] is None
                assert body['index']['actors'] == 3 and body['index']['searchable'] == 1
                assert body['index']['handle_invalid'] == 1 and body['index']['quarantined'] == 0
                checks.append('index_counts_before_the_store_has_reported')

                assert smoke.request(base, '/admin/corpus', STORE_COUNT, secret)[0] == 404
                assert smoke.request(admin, '/admin/corpus', STORE_COUNT)[0] == 401
                assert smoke.request(admin, '/admin/corpus', {**STORE_COUNT, 'hidden': 10}, secret)[0] == 400
                assert smoke.request(admin, '/admin/corpus', {'live': 1}, secret)[0] in (400, 422)
                assert smoke.request(base, '/corpus')[2]['store'] is None
                assert smoke.request(admin, '/admin/corpus', STORE_COUNT, secret)[0] == 200
                assert smoke.request(base, '/corpus')[2]['store'] == STORE_COUNT
                checks.append('store_count_accepted_on_the_loopback_listener_only_and_served')
            finally:
                smoke.stop(child)

            child, base, admin = smoke.start(data, log, secret)
            try:
                assert smoke.request(base, '/corpus')[2]['store'] is None
                checks.append('store_count_is_not_kept_across_a_restart')
            finally:
                smoke.stop(child)

            child, base, admin = smoke.start(data, log, secret, extra=('--allow-stale', 'false', '--max-sync-lag-s', '1'))
            try:
                assert actors(base, rows[0]['did'])[0] == 503
                assert smoke.request(base, '/corpus')[0] == 503
                checks.append('a_stale_index_answers_503')
            finally:
                smoke.stop(child)

    result = {'passed': True, 'checks': checks}
    receipt = os.environ.get('TA_ACTOR_READ_RECEIPT')
    if receipt:
        Path(receipt).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
