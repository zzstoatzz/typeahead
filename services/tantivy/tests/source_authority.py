import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from smoke import BIN, actor, request, query, start, stop, write_seed


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix='tantivy-source-authority-') as temporary:
        root = Path(temporary)
        seed, data = root/'seed.jsonl', root/'data'
        row = actor(1, 'source.test', 'Original Source')
        configured_policy = os.environ.get('TA_IDENTITY_POLICY_TEST_PATH')
        policy = Path(configured_policy) if configured_policy else Path(__file__).resolve().parents[3]/'src/identity-quarantine.json'
        quarantine = json.loads(policy.read_text())
        excluded = [actor(10+n, handle.upper(), 'Quarantine Name', score=1000+n) for n, handle in enumerate(quarantine)]
        eligible = actor(99, 'ordinary.test', 'Quarantine Name', score=1)
        write_seed(seed, [row, *excluded, eligible])
        subprocess.run([str(BIN), 'import', '--data', str(data), '--input', str(seed), '--allow-partial', 'true'], check=True, capture_output=True)
        secret = secrets.token_urlsafe(32)
        with (root/'server.log').open('wb') as log:
            child, public, admin = start(data, log, secret)
            try:
                for params in [{}, {'pds':'local.test'}, {'prefer_pds':'local.test', 'preference':'pds_first'}]:
                    assert [a['did'] for a in query(public, 'Quarantine', limit=1, **params)[0]] == [eligible['did']]
                for handle in quarantine:
                    assert not query(public, handle)[0]
                checks.append('shared_identity_exclusions_before_topk_scope_and_preference')
                for endpoint, body in [('/admin/source-state', {'dids': []}), ('/admin/source-page', {'after_did':'', 'limit':10}), ('/admin/source-reconcile', {})]:
                    assert request(public, endpoint, body, secret)[0] == 404
                assert request(admin, '/admin/source-state', {'dids':[]})[0] == 401
                checks.append('private_route_exclusion_and_auth')
                state = request(admin, '/admin/source-state', {'dids':[row['did']]}, secret)[2]
                changed = copy.deepcopy(row)
                changed.update(display_name='Observed Source', version=99)
                body = {'actors':[changed], 'expected_documents':state['actors'], 'source_cursor':{'observation':1}, 'expected_source_cursor':state['source_cursor'], 'expected_observation_revision':state['source_observation_revision'], 'batch_id':hashlib.sha256(b'first').hexdigest()}
                bad = copy.deepcopy(body)
                bad['expected_documents'][0]['document_sha256'] = '0'*64
                assert request(admin, '/admin/source-reconcile', bad, secret)[0] == 409
                assert query(public, 'Original Source')[0]
                bad = copy.deepcopy(body)
                bad['expected_observation_revision'] = 1
                assert request(admin, '/admin/source-reconcile', bad, secret)[0] == 409
                checks.append('document_and_revision_cas_reject_atomically')
                code, _, receipt = request(admin, '/admin/source-reconcile', body, secret)
                assert code == 200, (code, receipt)
                assert receipt['source_observation_revision'] == 1 and receipt['applied'] == 1
                assert query(public, 'Observed Source')[0]
                assert request(admin, '/admin/apply', {'actors':[]}, secret)[0] == 409
                assert request(admin, '/index/status')[2]['format'] == 'tantivy-0.27-f32-did-expiry-v4'
                assert 'last_source_batch' not in request(public, '/index/status')[2]
                checks.append('explicit_source_observation_and_legacy_write_fence')
            finally:
                stop(child)
            child, public, admin = start(data, log, secret)
            try:
                assert request(admin, '/admin/source-reconcile', body, secret)[2] == receipt
                assert request(admin, '/index/status')[2]['source_observation_revision'] == 1
                bad = copy.deepcopy(body)
                bad['actors'][0]['display_name'] = 'Reused Batch'
                assert request(admin, '/admin/source-reconcile', bad, secret)[0] == 409
                assert not query(public, 'Reused Batch')[0]
                checks.append('durable_lost_ack_retry_and_reused_id_rejection')
                state = request(admin, '/admin/source-state', {'dids':[]}, secret)[2]
                empty = {'actors':[], 'expected_documents':[], 'source_cursor':{'observation':2}, 'expected_source_cursor':state['source_cursor'], 'expected_observation_revision':1, 'batch_id':hashlib.sha256(b'empty').hexdigest()}
                assert request(admin, '/admin/source-reconcile', empty, secret)[2]['source_observation_revision'] == 2
                assert request(admin, '/admin/source-reconcile', body, secret)[0] == 409
                checks.append('metadata_only_commit_and_old_retry_fence')
            finally:
                stop(child)
    result = {'passed':True, 'checks':checks, 'binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest()}
    destination = os.environ.get('TA_SOURCE_AUTHORITY_RECEIPT')
    if destination:
        Path(destination).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    run()
