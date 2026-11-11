"""The serving writer's merge settings bound segments and deleted documents.

Regression (2026-11-11): typeahead-shadow-a, fed by the index bridge and never
rebuilt from a snapshot, held 24.7 million documents of which 11.4 million were
deleted, in 12 to 18 segments: Tantivy's default policy merges a level only at
eight segments and never because of deletions. With the settings the shadow
slot passes, a level merges at three segments or when a segment is more than a
fifth deleted, and no replaced document comes back through those merges. The
writes here are 500 and 100 documents each, below the smallest level, so the
whole index is that level and settles into one or two segments.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from smoke import BIN, query, request, start, stop, write_seed

ACTORS = 3000
REWRITTEN = 1500
SETTINGS = {'min_segments': 3, 'level_log_size': 3.0, 'min_layer_size': 1000, 'delete_ratio': 0.2, 'max_docs': 1000000000}
FLAGS = ['--merge-min-segments', '3', '--merge-level-log-size', '3', '--merge-min-layer-size', '1000', '--merge-delete-ratio', '0.2', '--merge-max-docs', '1000000000']


def actor(n, generation):
    return {'did': f'did:plc:merge{n:05}', 'handle': f'u{n}g{generation}.merge.test', 'display_name': f'Merge {n}',
            'quality_score': float(n % 97), 'version': 100 + generation, 'labels': [], 'associated': {}}


class Source:
    def __init__(self, admin, secret, sequence=0):
        self.admin, self.secret, self.sequence = admin, secret, sequence

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


def settle(source, public):
    """Commit until the merges in flight have landed; returns the status and the most segments seen."""
    most = 0
    for _ in range(400):
        source.reconcile([])
        status = request(public, '/index/status')[2]
        most = max(most, status['segments'])
        if status['segments'] <= 2 and status['deleted_documents'] * 5 <= status['documents']:
            return status, most
        time.sleep(.05)
    raise AssertionError('merges did not settle: ' + json.dumps({k: status[k] for k in ('segments', 'documents', 'deleted_documents')}))


def check(public, admin, secret, generation_of):
    for at in range(0, ACTORS, 500):
        expected = [actor(n, generation_of(n)) for n in range(at, at + 500)]
        code, _, state = request(admin, '/admin/source-state', {'dids': [a['did'] for a in expected], 'fields': True}, secret)
        assert code == 200, 'a replaced document came back: ' + json.dumps(state)
        assert [a['fields']['handle'] for a in state['actors']] == [a['handle'] for a in expected]
    for n in range(0, REWRITTEN, 97):
        assert not query(public, actor(n, 0)['handle'])[0], 'old handle is searchable again'
        assert [a['handle'] for a in query(public, actor(n, 1)['handle'])[0]] == [actor(n, 1)['handle']]


def run():
    with tempfile.TemporaryDirectory(prefix='tantivy-merge-policy-') as temporary:
        root = Path(temporary)
        seed, data = root/'seed.jsonl', root/'data'
        write_seed(seed, [])
        subprocess.run([str(BIN), 'import', '--data', str(data), '--input', str(seed), '--allow-partial', 'true'], check=True, capture_output=True)
        secret = secrets.token_urlsafe(32)
        refused = subprocess.run([str(BIN), 'serve', '--data', str(data), '--merge-delete-ratio', '0'], capture_output=True, text=True, timeout=30)
        assert refused.returncode != 0 and 'invalid merge settings' in refused.stderr, refused.stderr
        generation_of = lambda n: 1 if n < REWRITTEN else 0
        with (root/'server.log').open('wb') as log:
            child, public, admin = start(data, log, secret, extra=FLAGS)
            try:
                assert request(public, '/index/status')[2]['query_config']['merge'] == SETTINGS
                source = Source(admin, secret)
                most = 0
                for at in range(0, ACTORS, 500):
                    source.reconcile([actor(n, 0) for n in range(at, at + 500)])
                for at in range(0, REWRITTEN, 100):
                    assert source.reconcile([actor(n, 1) for n in range(at, at + 100)])['applied'] == 100
                    most = max(most, request(public, '/index/status')[2]['segments'])
                status, later = settle(source, public)
                most = max(most, later)
                assert status['documents'] - status['deleted_documents'] == ACTORS, status
                assert status['actor_count'] == ACTORS
                check(public, admin, secret, generation_of)
            finally:
                stop(child)
            child, public, admin = start(data, log, secret, extra=FLAGS)
            try:
                source = Source(admin, secret, sequence=source.sequence)
                restarted, _ = settle(source, public)
                assert restarted['documents'] - restarted['deleted_documents'] == ACTORS, restarted
                check(public, admin, secret, generation_of)
            finally:
                stop(child)
    result = {'passed': True, 'actors': ACTORS, 'rewritten': REWRITTEN, 'settings': SETTINGS, 'most_segments_seen': most,
              'settled': {k: status[k] for k in ('segments', 'documents', 'deleted_documents')},
              'binary_sha256': hashlib.sha256(BIN.read_bytes()).hexdigest()}
    destination = os.environ.get('TA_MERGE_POLICY_RECEIPT')
    if destination:
        Path(destination).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    run()
