"""Restore small, verified batches of missing active accounts using existing APIs.

Defaults to planning only. --apply requires a contemporaneous operator review
of latency/error metrics as well as the automatic readiness/freshness checks.
Never changes the relay cursor or deletes an account. Canonical ingest records
restored actors as recently active; this is recovery activity, not proof of a
new user action. Enrichment applies current moderation/domain policy.
"""
import argparse
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import httpx


def load_health_check():
    spec = importlib.util.spec_from_file_location('audit_recovery', Path(__file__).with_name('audit-ingest-recovery.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.serving_healthy


def turso_presence(client, env, dids):
    url = env['TURSO_URL'].replace('libsql://', 'https://').rstrip('/') + '/v2/pipeline'
    statement = {'sql': 'SELECT did FROM actors WHERE did IN (' + ','.join('?' for _ in dids) + ')',
                 'args': [{'type': 'text', 'value': did} for did in dids]}
    response = client.post(url, headers={'Authorization': 'Bearer ' + env['TURSO_AUTH_TOKEN']},
                           json={'requests': [{'type': 'execute', 'stmt': statement}, {'type': 'close'}]})
    response.raise_for_status()
    result = response.json()['results'][0]
    if result['type'] != 'ok':
        raise RuntimeError('Presence query failed')
    return {row[0]['value'] for row in result['response']['result']['rows']}


def main(args):
    db = sqlite3.connect(args.audit, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS restoration (did TEXT PRIMARY KEY, state TEXT, checked_at INTEGER)')
    dids = [row[0] for row in db.execute('''
        SELECT a.did FROM audit a JOIN repo_status s USING(did)
        JOIN profile_available p USING(did) LEFT JOIN restoration r USING(did)
        WHERE a.present=0 AND s.active=1 AND p.available=1 AND (r.did IS NULL OR r.state='restore_pending' OR r.state LIKE 'profile_error:%')
        ORDER BY a.did LIMIT ?
    ''', (args.limit,))]
    print(json.dumps({'selected': len(dids), 'apply': args.apply}), flush=True)
    if not args.apply or not dids:
        return
    env = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines()
               if '=' in line and not line.startswith('#'))
    env = {key: value.strip('\"\'') for key, value in env.items()}
    health_check = load_health_check()
    with httpx.Client(timeout=20, headers={'User-Agent': 'typeahead-recovery/1'}) as client:
        if not health_check(client):
            raise RuntimeError('Serving health gate closed; no writes')
        present = turso_presence(client, env, dids)
        eligible = []
        pending = {row[0] for row in db.execute("SELECT did FROM restoration WHERE state='restore_pending' OR state LIKE 'profile_error:%'")}
        for did in dids:
            if did in present and did not in pending:
                db.execute('INSERT OR REPLACE INTO restoration VALUES (?,?,?)', (did, 'already_present', int(time.time())))
                continue
            response = client.get('https://bsky.network/xrpc/com.atproto.sync.getRepoStatus', params={'did': did})
            response.raise_for_status()
            if response.json().get('active') is not True:
                db.execute('INSERT OR REPLACE INTO restoration VALUES (?,?,?)', (did, 'now_inactive', int(time.time())))
                continue
            eligible.append(did)
        db.commit()
        if not eligible:
            return
        if not health_check(client):
            raise RuntimeError('Serving health changed; no writes')
        headers = {'Authorization': 'Bearer ' + env['ADMIN_SECRET']}
        # Checkpoint before mutation: uncertain HTTP outcomes remain retryable.
        db.executemany('INSERT OR REPLACE INTO restoration VALUES (?,?,?)',
                       [(did, 'restore_pending', int(time.time())) for did in eligible])
        db.commit()
        missing = [did for did in eligible if did not in present]
        if missing:
            response = client.post('https://typeahead.waow.tech/admin/ingest', headers=headers,
                                   json={'events': [{'did': did} for did in missing]})
            response.raise_for_status()
            if response.json().get('ingested') != len(missing):
                raise RuntimeError('Unexpected ingest response; inspect before retry')
        if not health_check(client):
            raise RuntimeError('Health gate closed after insert; profiles checkpointed for retry')
        response = client.post('https://typeahead.waow.tech/admin/enrich-profiles', headers=headers,
                               json={'dids': eligible})
        response.raise_for_status()
        result = response.json()
        errors = {entry['did']: entry['reason'] for entry in result['errors']}
        if result['written'] != len(eligible) - len(errors):
            raise RuntimeError('Unexpected enrichment result; inspect checkpoint')
        verified = turso_presence(client, env, eligible)
        for did in eligible:
            state = 'profile_error:' + errors[did] if did in errors else 'enriched'
            if did not in verified:
                state = 'verification_missing'
            db.execute('UPDATE restoration SET state=?,checked_at=? WHERE did=?', (state, int(time.time()), did))
        db.commit()
        print(json.dumps({'attempted': len(eligible), 'written': result['written'],
                          'errors': len(errors), 'present_after': len(verified),
                          'healthy_after': health_check(client)}), flush=True)
    db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', required=True)
    parser.add_argument('--limit', type=int, default=5, choices=range(1, 26))
    parser.add_argument('--apply', action='store_true')
    main(parser.parse_args())
