"""Read-only current-profile comparison for historical profile/identity candidates.

Differences are reconciliation candidates, not proof of historical event loss.
Missing appview profiles never authorize deletion; source policy needs review.
"""
import argparse
import json
import runpy
import sqlite3
import time
from pathlib import Path

import httpx


def cid(url):
    return url.rstrip('/').rsplit('/', 1)[-1].split('@', 1)[0] if url else ''


def differences(row, profile):
    expected = {'handle': profile.get('handle') or '',
                'display_name': profile.get('displayName') or '',
                'avatar_url': cid(profile.get('avatar')),
                'description': profile.get('description') or '',
                'banner_url': cid(profile.get('banner'))}
    return [field for field, value in expected.items() if (row.get(field) or '') != value]


def main(args):
    env = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines()
               if '=' in line and not line.startswith('#'))
    env = {key: value.strip('\"\'') for key, value in env.items()}
    db = sqlite3.connect(args.audit, timeout=30)
    db.execute('ATTACH DATABASE ? AS capture', (args.capture,))
    db.execute('CREATE TABLE IF NOT EXISTS profile_comparison (did TEXT PRIMARY KEY, status TEXT, changed_fields TEXT, profile_source TEXT, checked_at INTEGER)')
    health = runpy.run_path(str(Path(__file__).with_name('audit-ingest-recovery.py')))['serving_healthy']
    completed = 0
    with httpx.Client(timeout=20, headers={'User-Agent': 'typeahead-recovery/1'}) as client:
        while completed < args.limit:
            refresh_join = 'LEFT JOIN profile_refresh r USING(did)' if args.recheck_refreshed else ''
            pending_filter = "r.state='refreshed_needs_verification'" if args.recheck_refreshed else 'p.did IS NULL'
            dids = [row[0] for row in db.execute(f'''
                SELECT c.did FROM capture.candidates c JOIN audit a USING(did)
                LEFT JOIN profile_comparison p USING(did)
                LEFT JOIN repo_status s USING(did)
                {refresh_join}
                WHERE (c.profile=1 OR c.identity=1) AND a.present=1
                  AND {pending_filter} AND (s.active IS NULL OR s.active=1)
                ORDER BY c.did LIMIT ?
            ''', (min(25, args.limit-completed),))]
            if not dids: break
            if not health(client):
                print(json.dumps({'waiting_for_health': True}), flush=True)
                time.sleep(30)
                continue
            columns = ['did', 'handle', 'display_name', 'avatar_url', 'description', 'banner_url', 'profile_source']
            sql = 'SELECT ' + ','.join(columns) + ' FROM actors WHERE did IN (' + ','.join('?' for _ in dids) + ')'
            response = client.post(env['TURSO_URL'].replace('libsql://', 'https://').rstrip('/')+'/v2/pipeline',
                headers={'Authorization': 'Bearer '+env['TURSO_AUTH_TOKEN']},
                json={'requests': [{'type': 'execute', 'stmt': {'sql': sql, 'args': [{'type': 'text', 'value': d} for d in dids]}}, {'type': 'close'}]})
            response.raise_for_status()
            result = response.json()['results'][0]
            if result['type'] != 'ok': raise RuntimeError('Comparison read failed')
            stored = {row[0]['value']: dict(zip(columns, [cell.get('value') for cell in row])) for row in result['response']['result']['rows']}
            response = client.get('https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles', params=[('actors', d) for d in dids])
            response.raise_for_status()
            profiles = {p['did']: p for p in response.json()['profiles']}
            for did in dids:
                changed = []
                source = stored.get(did, {}).get('profile_source')
                if did not in stored: status = 'no_longer_present'
                elif did not in profiles: status = 'appview_unavailable'
                elif source not in (None, '', 'bsky'): status = 'alternate_source_review'
                elif profiles[did].get('handle') == 'handle.invalid': status = 'identity_review'
                else:
                    changed = differences(stored[did], profiles[did])
                    status = 'differs' if changed else 'matches'
                db.execute('INSERT OR REPLACE INTO profile_comparison VALUES (?,?,?,?,?)', (did, status, json.dumps(changed), source, int(time.time())))
                if args.recheck_refreshed:
                    db.execute('UPDATE profile_refresh SET state=?,checked_at=? WHERE did=?',
                               ('verified' if status == 'matches' else 'needs_review', int(time.time()), did))
            db.commit()
            completed += len(dids)
            print(json.dumps({'compared_this_run': completed, 'totals': db.execute('SELECT status,count(*) FROM profile_comparison GROUP BY status').fetchall()}), flush=True)
            time.sleep(5)
    db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', required=True)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--recheck-refreshed', action='store_true')
    main(parser.parse_args())
