"""Reconcile confirmed deleted/deactivated primary-source accounts.

Defaults to a plan. Backups and checkpoints are local; --apply uses the
canonical delete writer, never a cursor. Review latency/error metrics first.
Takedowns, overrides, alternate sources, and reactivated accounts are excluded.
"""
import argparse
import json
import runpy
import sqlite3
import time
from pathlib import Path
import httpx


def eligible(row, status):
    return (row.get('profile_source') in (None, '', 'bsky')
            and row.get('override_action') is None
            and status.get('active') is False
            and status.get('status') in ('deleted', 'deactivated'))


def main(args):
    db = sqlite3.connect(args.audit, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS inactive_recovery(did TEXT PRIMARY KEY,state TEXT,backup TEXT,checked_at INTEGER)')
    dids = [r[0] for r in db.execute('''SELECT p.did FROM inactive_policy_review p
        LEFT JOIN inactive_recovery r USING(did)
        WHERE p.relay_status IN ('deleted','deactivated')
          AND p.override_action IS NULL AND (p.profile_source IS NULL OR p.profile_source IN ('','bsky'))
          AND (r.did IS NULL OR r.state='pending') ORDER BY p.did LIMIT ?''', (args.limit,))]
    print(json.dumps({'selected': len(dids), 'apply': args.apply}), flush=True)
    if not dids or not args.apply: return
    env = dict(l.split('=', 1) for l in Path('.env').read_text().splitlines() if '=' in l and not l.startswith('#'))
    env = {k: v.strip('\"\'') for k, v in env.items()}
    healthy = runpy.run_path(str(Path(__file__).with_name('audit-ingest-recovery.py')))['serving_healthy']
    with httpx.Client(timeout=20, headers={'User-Agent': 'typeahead-recovery/1'}) as client:
        def rows_for(ds):
            sql = 'SELECT a.*,m.action AS override_action FROM actors a LEFT JOIN mod_overrides m ON m.did=a.did WHERE a.did IN ('+','.join('?' for _ in ds)+')'
            r = client.post(env['TURSO_URL'].replace('libsql://', 'https://').rstrip('/')+'/v2/pipeline',
                headers={'Authorization': 'Bearer '+env['TURSO_AUTH_TOKEN']},
                json={'requests': [{'type': 'execute', 'stmt': {'sql': sql, 'args': [{'type': 'text', 'value': d} for d in ds]}}, {'type': 'close'}]})
            r.raise_for_status(); result = r.json()['results'][0]
            if result['type'] != 'ok': raise RuntimeError('Presence/policy query failed')
            data = result['response']['result']; cols = [x['name'] for x in data['cols']]
            rows = [dict(zip(cols, [x.get('value') for x in r])) for r in data['rows']]
            return {row['did']: row for row in rows}
        if not healthy(client): raise RuntimeError('Serving gate closed')
        stored = rows_for(dids)
        selected = []
        for did in dids:
            if did not in stored:
                db.execute('''INSERT INTO inactive_recovery VALUES (?,?,?,?)
                    ON CONFLICT(did) DO UPDATE SET state=excluded.state,checked_at=excluded.checked_at''',
                    (did, 'already_absent', None, int(time.time())))
                continue
            response = client.get('https://bsky.network/xrpc/com.atproto.sync.getRepoStatus', params={'did': did})
            response.raise_for_status()
            if not eligible(stored[did], response.json()):
                db.execute('INSERT OR REPLACE INTO inactive_recovery VALUES (?,?,?,?)', (did, 'status_or_policy_changed', json.dumps(stored[did]), int(time.time())))
                continue
            selected.append(did)
            db.execute('INSERT OR REPLACE INTO inactive_recovery VALUES (?,?,?,?)', (did, 'pending', json.dumps(stored[did]), int(time.time())))
        db.commit()
        if not selected: return
        if not healthy(client): raise RuntimeError('Serving gate changed; backups retained')
        # Recheck source/override and concurrent record changes after the status calls.
        current = rows_for(selected)
        selected = [d for d in selected if d in current and current[d] == stored[d]]
        if not selected: return
        response = client.post('https://typeahead.waow.tech/admin/delete',
            headers={'Authorization': 'Bearer '+env['ADMIN_SECRET']}, json={'dids': selected})
        response.raise_for_status(); result = response.json()
        if result.get('ok') is not True: raise RuntimeError('Delete result not confirmed')
        remaining = rows_for(selected)
        for did in selected:
            db.execute('UPDATE inactive_recovery SET state=?,checked_at=? WHERE did=?',
                       ('still_present_review' if did in remaining else 'verified_absent', int(time.time()), did))
        db.commit()
        print(json.dumps({'requested': len(selected), 'deleted': result['deleted'], 'verified_absent': len(selected)-len(remaining), 'errors': len(remaining), 'healthy_after': healthy(client)}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--audit', required=True)
    p.add_argument('--limit', type=int, default=5, choices=range(1, 6))
    p.add_argument('--apply', action='store_true')
    main(p.parse_args())
