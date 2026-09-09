"""Offline SQLite plan/latency/storage experiment. Does not access production."""
import json, sqlite3, statistics, tempfile, time
from pathlib import Path

TOP = 'SELECT did,score,op FROM (SELECT did,score,op FROM prefix_overlay WHERE key=? AND op=1 ORDER BY score DESC,did ASC LIMIT 50)'
SQL = TOP + ' UNION ALL SELECT did,score,op FROM prefix_overlay WHERE key=? AND did IN ('+','.join('?' for _ in range(300))+')'
ARGS=['d:bs','d:bs']+[f'did:{i:08}' for i in range(300)]

def measure(c,sql,args):
    samples=[]
    for _ in range(7):
        start=time.perf_counter(); rows=c.execute(sql,args).fetchall(); samples.append((time.perf_counter()-start)*1000)
    return {'median_ms':round(statistics.median(samples),3),'rows':len(rows),'plan':[r[3] for r in c.execute('EXPLAIN QUERY PLAN '+sql,args)]}

def updates(c):
    start=time.perf_counter()
    with c:
        c.executemany('UPDATE prefix_overlay SET score=? WHERE key=? AND did=?',[(i%71,'d:bs',f'did:{i:08}') for i in range(2000)])
    return round((time.perf_counter()-start)*1000,3)

with tempfile.TemporaryDirectory() as tmp:
    c=sqlite3.connect(str(Path(tmp)/'bench.db'))
    c.executescript('PRAGMA journal_mode=WAL; CREATE TABLE prefix_overlay(key TEXT,did TEXT,score REAL,op INTEGER,updated_at INTEGER,source INTEGER,PRIMARY KEY(key,did)) WITHOUT ROWID; CREATE INDEX updated ON prefix_overlay(updated_at);')
    with c:
        c.executemany('INSERT INTO prefix_overlay VALUES(?,?,?,?,1,0)',(('d:bs',f'did:{i:08}',float(i%997),int(i%11!=0)) for i in range(333000)))
    result={'sqlite':sqlite3.sqlite_version,'rows_in_fixture':333000}
    result['full']=measure(c,'SELECT did,score,op FROM prefix_overlay WHERE key=?',['d:bs'])
    result['bounded']=measure(c,SQL,ARGS)
    before=c.execute('PRAGMA page_count').fetchone()[0]*c.execute('PRAGMA page_size').fetchone()[0]
    result['update_2000_without_index_ms']=updates(c)
    with c:
        c.executemany('UPDATE prefix_overlay SET score=? WHERE key=? AND did=?',[(i%997,'d:bs',f'did:{i:08}') for i in range(2000)])
    start=time.perf_counter();c.execute('CREATE INDEX ranked ON prefix_overlay(key,score DESC,did ASC) WHERE op=1');c.commit()
    result['index_build_ms']=round((time.perf_counter()-start)*1000,3)
    result['indexed']=measure(c,SQL,ARGS)
    result['update_2000_with_index_ms']=updates(c)
    after=c.execute('PRAGMA page_count').fetchone()[0]*c.execute('PRAGMA page_size').fetchone()[0]
    result['additional_index_bytes']=after-before
    print(json.dumps(result,indent=2))
