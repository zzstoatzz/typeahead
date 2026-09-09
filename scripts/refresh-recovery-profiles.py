"""Refresh previously-present, differing profiles through the canonical writer.

Plan-only by default. Requires manual latency/error review before --apply,
plus fail-closed serving checks and current relay/presence checks. Does not
insert bare actors, send cursors, delete rows, or override alternate sources.
"""
import argparse
import json
import runpy
import sqlite3
import time
from pathlib import Path

import httpx


def main(args):
    db = sqlite3.connect(args.audit, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS profile_refresh (did TEXT PRIMARY KEY, state TEXT, checked_at INTEGER)')
    dids = [r[0] for r in db.execute('''
        SELECT p.did FROM profile_comparison p LEFT JOIN profile_refresh r USING(did)
        WHERE p.status='differs' AND (r.did IS NULL OR r.state='pending')
        ORDER BY p.did LIMIT ?
    ''', (args.limit,))]
    print(json.dumps({'selected': len(dids), 'apply': args.apply}), flush=True)
    if not args.apply or not dids: return
    env = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    env = {k: v.strip('\"\'') for k, v in env.items()}
    healthy = runpy.run_path(str(Path(__file__).with_name('audit-ingest-recovery.py')))['serving_healthy']
    with httpx.Client(timeout=20, headers={'User-Agent': 'typeahead-recovery/1'}) as client:
        if not healthy(client): raise RuntimeError('Serving gate closed; no writes')
        sql = 'SELECT did,profile_source FROM actors WHERE did IN (' + ','.join('?' for _ in dids) + ')'
        response = client.post(env['TURSO_URL'].replace('libsql://', 'https://').rstrip('/')+'/v2/pipeline',
            headers={'Authorization': 'Bearer '+env['TURSO_AUTH_TOKEN']},
            json={'requests': [{'type': 'execute', 'stmt': {'sql': sql, 'args': [{'type': 'text', 'value': d} for d in dids]}}, {'type': 'close'}]})
        response.raise_for_status()
        result = response.json()['results'][0]
        if result['type'] != 'ok': raise RuntimeError('Presence/source check failed')
        sources = {r[0]['value']: r[1].get('value') for r in result['response']['result']['rows']}
        eligible = []
        for did in dids:
            state = None
            if did not in sources: state = 'no_longer_present'
            elif sources[did] not in (None, '', 'bsky'): state = 'alternate_source_review'
            else:
                response = client.get('https://bsky.network/xrpc/com.atproto.sync.getRepoStatus', params={'did': did})
                response.raise_for_status()
                if response.json().get('active') is not True: state = 'inactive_review'
            if state:
                db.execute('INSERT OR REPLACE INTO profile_refresh VALUES (?,?,?)', (did, state, int(time.time())))
            else: eligible.append(did)
        db.commit()
        if not eligible: return
        if not healthy(client): raise RuntimeError('Serving gate changed; no writes')
        db.executemany('INSERT OR REPLACE INTO profile_refresh VALUES (?,?,?)', [(d, 'pending', int(time.time())) for d in eligible])
        db.commit()
        response = client.post('https://typeahead.waow.tech/admin/enrich-profiles',
            headers={'Authorization': 'Bearer '+env['ADMIN_SECRET']}, json={'dids': eligible})
        response.raise_for_status()
        result = response.json()
        errors = {e['did']: e['reason'] for e in result['errors']}
        if result['written'] != len(eligible)-len(errors): raise RuntimeError('Unexpected write count; inspect checkpoint')
        for did in eligible:
            state = 'error:'+errors[did] if did in errors else 'refreshed_needs_verification'
            db.execute('UPDATE profile_refresh SET state=?,checked_at=? WHERE did=?', (state, int(time.time()), did))
        db.commit()
        print(json.dumps({'requested': len(eligible), 'written': result['written'], 'errors': len(errors), 'healthy_after': healthy(client)}), flush=True)
    db.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--audit', required=True)
    p.add_argument('--limit', type=int, default=25, choices=range(1, 26))
    p.add_argument('--apply', action='store_true')
    main(p.parse_args())
