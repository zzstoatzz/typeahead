"""Moderation policy through the source protocol, against the real binary.

A policy snapshot rides on a source batch. This checks that a snapshot older
than its lifetime stops the reader, that a bad snapshot is refused whole, that
a policy-only batch changes who is eligible before the top results are cut,
and that a snapshot commits or does not with the batch that carried it.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.parse

from smoke import BIN, actor, request, query, start, stop, write_seed

LABELER = 'did:plc:ar7c4by46qjdydhdevvrndac'


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix='tantivy-source-policy-') as temporary:
        root = Path(temporary)
        seed, data = root/'seed.jsonl', root/'data'
        blocked = [actor(100+i, f'policy-blocked{i:02d}.blocked.test', 'Blocked', score=1000-i) for i in range(24)]
        eligible = [actor(200+i, f'policy-eligible{i:02d}.safe.test', 'Eligible', score=20-i) for i in range(12)]
        labelled = actor(300, 'policy-label.safe.test', 'Labelled', score=900)
        expired = actor(301, 'policy-expired.safe.test', 'Expired', score=500, hidden=True)
        hide = {'src': LABELER, 'val': '!hide'}
        blocked[0]['labels'] = [hide]
        labelled['labels'] = [hide]
        expired['labels'] = [dict(hide, exp='1900-01-01T00:00:00Z')]
        atomic = actor(400, 'atomic.fixture.test', 'Atomic')
        write_seed(seed, [*blocked, *eligible, labelled, expired, atomic])
        subprocess.run([str(BIN), 'import', '--data', str(data), '--input', str(seed), '--allow-partial', 'true'], check=True, capture_output=True)
        secret = secrets.token_urlsafe(32)
        dids = lambda actors: [a['did'] for a in actors]

        with (root/'server.log').open('wb') as log:
            child, public, admin = start(data, log, secret)

            def status():
                return request(admin, '/index/status')[2]

            def state(wanted=()):
                return request(admin, '/admin/source-state', {'dids': list(wanted)}, secret)[2]

            def batch(policy=None, actors=(), cursor=None):
                """One source batch on top of the current state, with an optional snapshot."""
                current = state([a['did'] for a in actors])
                body = {
                    'actors': list(actors), 'expected_documents': current['actors'],
                    'expected_source_cursor': current['source_cursor'],
                    'source_cursor': {'observation': current['source_observation_revision'] + 1} if cursor is None else cursor,
                    'expected_observation_revision': current['source_observation_revision'],
                }
                if policy is not None:
                    body['policy_snapshot'] = policy
                body['batch_id'] = digest(body)
                return body

            def send(body):
                return request(admin, '/admin/source-reconcile', body, secret)

            def snapshot(observed_at=None, overrides=(), domains=(), complete=True):
                return {'schema_version': 1, 'source_observed_at': int(time.time()) if observed_at is None else observed_at,
                        'overrides': list(overrides), 'blocked_domains': list(domains), 'complete': complete}

            def search(q='policy', **params):
                return dids(query(public, q, limit=10, **params)[0])

            try:
                # a snapshot past its lifetime stops the reader, whatever --allow-stale says
                stale = snapshot(observed_at=int(time.time())-1000)
                assert send(batch(stale))[0] == 200
                for path in ['/ready', '/search?q=policy']:
                    assert request(public, path)[0] == 503, path
                stopped = status()
                assert not stopped['moderation']['fresh'] and not stopped['ready']
                assert 'policy_snapshot' not in stopped and 'last_source_batch' not in stopped
                # a batch without a snapshot leaves the policy and the refusal as they were
                assert send(batch())[0] == 200
                assert status()['moderation']['source_observed_at'] == stopped['moderation']['source_observed_at']
                assert not status()['ready']
                checks.append('expired_policy_stops_the_reader')

                for bad in [dict(stale, complete=False), dict(stale, source_observed_at=int(time.time())+1000)]:
                    revision = status()['source_observation_revision']
                    assert send(batch(bad))[0] == 400
                    assert not status()['ready'] and status()['source_observation_revision'] == revision
                assert send(batch(snapshot()))[0] == 200
                assert status()['ready']
                revision = status()['source_observation_revision']
                assert send(batch(stale))[0] == 400
                assert status()['ready'] and status()['source_observation_revision'] == revision
                checks.append('incomplete_future_and_backwards_snapshots_are_refused_whole')

                # policy decides eligibility before the top ten are cut, for every scope
                rules = snapshot(
                    overrides=[{'did': blocked[0]['did'], 'action': 'show'}, {'did': eligible[0]['did'], 'action': 'hide'}],
                    domains=['blocked.test'])
                assert send(batch(rules))[0] == 200
                expected = [blocked[0]['did'], expired['did'], *dids(eligible[1:9])]
                for params in [{}, {'pds': 'local.test'}, {'prefer_pds': 'local.test', 'preference': 'pds_first'}, {'prefer_pds': 'local.test', 'boost': 100}]:
                    assert search(**params) == expected, params
                memberships = status()['indexed_memberships']
                # dropping the show override is a policy-only write: no document changes
                assert send(batch(snapshot(overrides=[{'did': eligible[0]['did'], 'action': 'hide'}], domains=['blocked.test'])))[0] == 200
                assert search() == [expired['did'], *dids(eligible[1:10])]
                assert status()['indexed_memberships'] == memberships
                assert send(batch(snapshot(overrides=[{'did': eligible[0]['did'], 'action': 'hide'}])))[0] == 200
                assert search() == dids(blocked[1:11])
                assert status()['indexed_memberships'] == memberships
                checks.append('policy_only_batches_change_eligibility_before_topk')

                # an expiry the reader cannot evaluate is a 503, never an empty answer
                broken = copy.deepcopy(atomic)
                broken['labels'] = [dict(hide, exp='not-a-date')]
                assert send(batch(actors=[broken]))[0] == 200
                assert request(public, '/search?q=atomic')[0] == 503
                assert send(batch(actors=[atomic]))[0] == 200
                assert search('atomic') == [atomic['did']]
                checks.append('unsupported_label_expiry_is_refused_not_empty')

                for invalid in [snapshot(complete=False), snapshot(domains=[f'blocked{i}.test' for i in range(10001)])]:
                    revision = status()['source_observation_revision']
                    assert send(batch(invalid))[0] == 400
                    assert status()['source_observation_revision'] == revision
                checks.append('incomplete_and_oversized_snapshots_are_refused')

                # the snapshot commits, or does not, with the batch that carried it
                for killpoint in ['before_commit', 'after_commit']:
                    moved = copy.deepcopy(atomic)
                    moved['handle'] = 'atomic-' + killpoint.replace('_', '-') + '.fixture.test'
                    before, old_policy = state([atomic['did']]), status()['moderation']
                    cursor = dict(before['source_cursor'] or {}, source_crash_probe=killpoint)
                    body = batch(snapshot(overrides=[{'did': eligible[0]['did'], 'action': 'hide'}], domains=[killpoint + '.unmatched.test']), [moved], cursor)
                    stop(child)
                    child, public, admin = start(data, log, secret, crash=killpoint)
                    try:
                        send(body)
                        raise AssertionError('the kill point did not end the server')
                    except OSError:
                        pass
                    child.wait(timeout=5)
                    assert child.returncode != 0
                    child, public, admin = start(data, log, secret)
                    committed = killpoint == 'after_commit'
                    recovered = status()
                    assert recovered['source_cursor'] == (cursor if committed else before['source_cursor'])
                    assert recovered['source_observation_revision'] == before['source_observation_revision'] + int(committed)
                    assert (recovered['moderation']['sha256'] != old_policy['sha256']) == committed
                    assert (state([atomic['did']])['actors'][0]['document_sha256'] != before['actors'][0]['document_sha256']) == committed
                    code, _, accepted = send(body)
                    assert code == 200 and accepted['source_observation_revision'] == before['source_observation_revision'] + 1
                    assert status()['source_cursor'] == cursor
                    assert search('atomic-' + killpoint.replace('_', '-')) == [atomic['did']]
                    stop(child)
                    child, public, admin = start(data, log, secret)
                    assert send(body)[2] == accepted
                    assert status()['moderation']['blocked_domain_count'] == 1
                    atomic = moved
                checks.append('a_snapshot_commits_with_its_batch_at_both_kill_points')

                saved = status()['moderation']
                stop(child)
                child, public, admin = start(data, log, secret)
                restored = status()['moderation']
                assert (restored['sha256'], restored['source_observed_at']) == (saved['sha256'], saved['source_observed_at'])
                assert restored['complete'] and restored['override_count'] == 1 and restored['blocked_domain_count'] == 1
                public_status = request(public, '/index/status')[2]
                assert 'policy_snapshot' not in public_status and 'last_source_batch' not in public_status
                assert 'overrides' not in public_status['moderation'] and 'blocked_domains' not in public_status['moderation']
                checks.append('the_policy_survives_a_restart_and_is_not_published')
            finally:
                stop(child)
    result = {'passed': True, 'checks': checks, 'binary_sha256': hashlib.sha256(BIN.read_bytes()).hexdigest()}
    destination = os.environ.get('TA_SOURCE_POLICY_RECEIPT')
    if destination:
        Path(destination).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    run()
