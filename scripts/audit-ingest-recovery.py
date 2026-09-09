"""Read-only, paced point lookups of captured candidate DIDs in Turso.

uv run --with httpx scripts/audit-ingest-recovery.py --capture /tmp/typeahead-recovery.sqlite --audit /tmp/typeahead-recovery-audit.sqlite
No database writes are sent to Turso. Local results are restartable; presence
is not evidence that all historical profile changes were applied.
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path
import httpx


def serving_healthy(client):
    try:
        ingest_response = client.get('https://typeahead-ingester.fly.dev/health', headers={'User-Agent': 'typeahead-recovery/1'}, timeout=5)
        ingest_response.raise_for_status()
        ingest = ingest_response.json()['ingest']
        search_response = client.get('https://typeahead-search.fly.dev/health', headers={'User-Agent': 'typeahead-recovery/1'}, timeout=5)
        search_response.raise_for_status()
        search = search_response.json()
        now = time.time()
        return (search['ready'] is True and search['status'] == 'ok'
                and 0 <= search['sync_lag_s'] < 300
                and ingest['backpressure_active'] is False
                and ingest['queued'] < 400 and ingest['deletes_queued'] < 400
                and 0 <= now - ingest['source_event_at'] < 120
                and 0 <= now - ingest['last_write_at'] < 60)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return False


def main(args):
    env=dict(l.split('=',1) for l in Path('.env').read_text().splitlines() if '=' in l and not l.startswith('#'))
    url=env['TURSO_URL'].strip('\"\'').replace('libsql://','https://').rstrip('/')+'/v2/pipeline'
    headers={'Authorization':'Bearer '+env['TURSO_AUTH_TOKEN'].strip('\"\'')}
    db=sqlite3.connect(args.audit,timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute('CREATE TABLE IF NOT EXISTS audit (did TEXT PRIMARY KEY, present INTEGER, checked_at INTEGER, updated_at INTEGER, hidden INTEGER, audited_at INTEGER)')
    db.execute('ATTACH DATABASE ? AS capture',(args.capture,))
    healthy_until = 0
    with httpx.Client(timeout=20) as client:
        while True:
            if time.monotonic() >= healthy_until:
                if not serving_healthy(client):
                    print(json.dumps({'waiting_for_serving_health': True}), flush=True)
                    time.sleep(30)
                    continue
                healthy_until = time.monotonic() + 30
            dids=[r[0] for r in db.execute('SELECT c.did FROM capture.candidates c LEFT JOIN audit a ON a.did=c.did WHERE a.did IS NULL LIMIT 250')]
            if not dids:
                progress=db.execute('SELECT cursor,end_seq FROM capture.progress WHERE id=1').fetchone()
                if progress and progress[0]>=progress[1]:break
                time.sleep(10)
                continue
            sql='SELECT did,profile_checked_at,updated_at,hidden FROM actors WHERE did IN ('+','.join('?' for _ in dids)+')'
            body={'requests':[{'type':'execute','stmt':{'sql':sql,'args':[{'type':'text','value':d} for d in dids]}},{'type':'close'}]}
            try:
                r=client.post(url,headers=headers,json=body)
                r.raise_for_status()
                result=r.json()['results'][0]
                if result['type']!='ok':raise RuntimeError('Database rejected read; pausing audit')
                rows=result['response']['result']['rows']
            except (httpx.HTTPError,RuntimeError):
                print(json.dumps({'read_failed':True,'retry_in_seconds':30}),flush=True)
                time.sleep(30)
                continue
            found={row[0]['value']:row[1:] for row in rows}
            now=int(time.time())
            for did in dids:
                vals=found.get(did)
                values=[int(v.get('value') or 0) for v in vals] if vals else [None]*3
                db.execute('INSERT OR REPLACE INTO audit VALUES (?,?,?,?,?,?)',(did,int(vals is not None),*values,now))
            db.commit()
            n,missing=db.execute('SELECT count(*),sum(present=0) FROM audit').fetchone()
            if n%2500==0:print(json.dumps({'audited':n,'absent':missing}),flush=True)
            time.sleep(5)
    print(json.dumps({'complete':True,'audited_and_absent':db.execute('SELECT count(*),sum(present=0) FROM audit').fetchone()}),flush=True)
    db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--capture',required=True)
    p.add_argument('--audit',required=True)
    main(p.parse_args())
