"""Check current relay status for missing actors and historical account events.
Read-only network calls; results are local and distinguish inactive from unknown.
"""
import argparse
import concurrent.futures
import json
import sqlite3
import time
import httpx


def main(args):
    db=sqlite3.connect(args.audit,timeout=30)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS repo_status (did TEXT PRIMARY KEY, active INTEGER, status TEXT, checked_at INTEGER)')
    db.execute('ATTACH DATABASE ? AS capture',(args.capture,))
    client=httpx.Client(timeout=15,headers={'User-Agent':'typeahead-recovery/1'},limits=httpx.Limits(max_connections=2))
    def check(did):
        try:
            r=client.get('https://bsky.network/xrpc/com.atproto.sync.getRepoStatus',params={'did':did})
            r.raise_for_status()
            x=r.json()
            if type(x.get('active')) is not bool:raise ValueError('Missing active status')
            return (did,int(x['active']),x.get('status','active' if x['active'] else 'inactive'),int(time.time()))
        except (httpx.HTTPError,ValueError):
            return (did,None,'unverified',int(time.time()))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        while True:
            dids=[r[0] for r in db.execute('SELECT c.did FROM capture.candidates c LEFT JOIN audit a ON a.did=c.did LEFT JOIN repo_status s ON s.did=c.did WHERE (a.present=0 OR c.account=1) AND s.did IS NULL LIMIT 20')]
            if not dids:
                progress=db.execute('SELECT cursor,end_seq FROM capture.progress WHERE id=1').fetchone()
                remaining=db.execute('SELECT count(*) FROM capture.candidates c LEFT JOIN audit a ON a.did=c.did WHERE a.did IS NULL').fetchone()[0]
                if progress and progress[0]>=progress[1] and remaining==0:
                    print(json.dumps({'complete':True,'statuses':db.execute('SELECT status,count(*) FROM repo_status GROUP BY status').fetchall()}),flush=True)
                    break
                print(json.dumps({'waiting':True,'statuses':db.execute('SELECT status,count(*) FROM repo_status GROUP BY status').fetchall()}),flush=True)
                time.sleep(30)
                continue
            rows=list(pool.map(check,dids))
            db.executemany('INSERT OR REPLACE INTO repo_status VALUES (?,?,?,?)',rows)
            db.commit()
            n=db.execute('SELECT count(*) FROM repo_status').fetchone()[0]
            if n%100==0:print(json.dumps({'checked':n,'statuses':db.execute('SELECT status,count(*) FROM repo_status GROUP BY status').fetchall()}),flush=True)
            time.sleep(2)
    client.close()
    db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--capture',required=True)
    p.add_argument('--audit',required=True)
    main(p.parse_args())
