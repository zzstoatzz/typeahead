"""Measure an experimental index on a disposable overlay copy only. Mutates that copy."""
import sqlite3,time,statistics,json,os,argparse
a=argparse.ArgumentParser(description=__doc__)
a.add_argument("--scratch-overlay", required=True)
a.add_argument("--output", required=True)
args=a.parse_args()
p=args.scratch_overlay; c=sqlite3.connect(p)
r={'sqlite':sqlite3.sqlite_version,'rows':c.execute('select count(*) from prefix_overlay').fetchone()[0],'actors':c.execute('select count(*) from actor_overlay').fetchone()[0]}
sql="SELECT did,score,op FROM prefix_overlay WHERE key=? AND op=1 ORDER BY score DESC,did ASC LIMIT 50"
rows=c.execute('select key,did,score from prefix_overlay where op=1 limit 2000').fetchall()
def measure():
 times=[]
 for _ in range(8):
  t=time.perf_counter();c.execute(sql,('d:bs',)).fetchall();times.append((time.perf_counter()-t)*1000)
 updates=[]
 for _ in range(7):
  c.execute('begin');t=time.perf_counter();c.executemany('update prefix_overlay set score=? where key=? and did=?',[(s+1,k,d) for k,d,s in rows]);updates.append((time.perf_counter()-t)*1000);c.rollback()
 return {'top50_median_ms':statistics.median(times[1:]),'update_2000_median_ms_excluding_commit':statistics.median(updates),'plan':[list(x) for x in c.execute('explain query plan '+sql,('d:bs',))]}
r['before']=measure();before=os.stat(p).st_size;t=time.perf_counter();c.execute('CREATE INDEX eval_rank ON prefix_overlay(key,score DESC,did ASC) WHERE op=1');c.commit();r['index_build_ms']=(time.perf_counter()-t)*1000;r['index_bytes']=os.stat(p).st_size-before;r['after']=measure();open(args.output,'w').write(json.dumps(r,indent=2));print(json.dumps(r,indent=2))
