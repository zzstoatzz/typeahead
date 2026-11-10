"""A write that follows a restart must not bring a replaced document back.

Regression (2026-11-10): a process killed with merges pending restarted with
eight or more mergeable segments. Its first write flushed a segment, which
started that merge while the write's delete was still uncommitted, and the
merged segment came back holding the replaced document. /search then served
the old handle beside the new one and /admin/source-state answered 500
("multiple live versions of actor"). Measured before the fix: 8 of 11 runs.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from smoke import BIN, query, request, start, write_seed

ACTORS = 3000
ATTEMPTS = 6


def actor(n, generation):
    return {'did': f'did:plc:restart{n:05}', 'handle': f'u{n}g{generation}.restart.test', 'display_name': f'Restart {n}',
            'quality_score': 1.0, 'version': 100 + generation, 'labels': [], 'associated': {}}


class Source:
    def __init__(self, admin, secret):
        self.admin, self.secret, self.sequence = admin, secret, 0

    def reconcile(self, actors):
        state = request(self.admin, '/admin/source-state', {'dids': [a['did'] for a in actors]}, self.secret)
        assert state[0] == 200, state
        self.sequence += 1
        body = {'actors': actors, 'expected_documents': state[2]['actors'], 'source_cursor': {'sequence': self.sequence},
                'expected_source_cursor': state[2]['source_cursor'],
                'expected_observation_revision': state[2]['source_observation_revision'],
                'batch_id': hashlib.sha256(str(self.sequence).encode()).hexdigest()}
        code, _, receipt = request(self.admin, '/admin/source-reconcile', body, self.secret)
        assert code == 200, (code, receipt)
        return receipt


def attempt(root, pending_writes):
    seed, data = root/'seed.jsonl', root/'data'
    write_seed(seed, [])
    subprocess.run([str(BIN), 'import', '--data', str(data), '--input', str(seed), '--allow-partial', 'true'], check=True, capture_output=True)
    secret = secrets.token_urlsafe(32)
    with (root/'server.log').open('wb') as log:
        child, public, admin = start(data, log, secret)
        try:
            source = Source(admin, secret)
            for at in range(0, ACTORS, 500):
                source.reconcile([actor(n, 0) for n in range(at, at + 500)])
            for n in range(pending_writes):
                source.reconcile([actor(100 + n, 1)])
        finally:
            child.kill()
            child.wait(timeout=5)
        child, public, admin = start(data, log, secret)
        try:
            source = Source(admin, secret)
            source.sequence = 1000
            assert source.reconcile([actor(8, 1)])['applied'] == 1
            for _ in range(40):
                source.reconcile([])
                time.sleep(.02)
                state = request(admin, '/admin/source-state', {'dids': [actor(8, 0)['did']]}, secret)
                assert state[0] == 200, 'replaced document came back: ' + json.dumps(state[2])
                assert not query(public, actor(8, 0)['handle'])[0], 'old handle is searchable again'
            assert [a['handle'] for a in query(public, actor(8, 1)['handle'])[0]] == [actor(8, 1)['handle']]
        finally:
            child.kill()
            child.wait(timeout=5)


def run():
    for n in range(ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix='tantivy-restart-merge-') as temporary:
            attempt(Path(temporary), 2 + n % 3)
    result = {'passed': True, 'attempts': ATTEMPTS, 'actors': ACTORS, 'binary_sha256': hashlib.sha256(BIN.read_bytes()).hexdigest()}
    destination = os.environ.get('TA_RESTART_MERGE_RECEIPT')
    if destination:
        Path(destination).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    run()
