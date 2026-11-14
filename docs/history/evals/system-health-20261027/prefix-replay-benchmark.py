import hashlib,json,re,shutil,sqlite3,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[3]
source=(root/'services/src/index/overlay.zig').read_text()
new=json.loads(re.search(r'const upsert_prefix_sql = (".*");',source).group(1))
old='INSERT OR REPLACE INTO prefix_overlay (key, did, score, op, updated_at, source) VALUES (?, ?, ?, ?, ?, 0)'
def configure(c):
 c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL');c.execute('PRAGMA cache_size=-20000');c.execute('PRAGMA wal_autocheckpoint=1000')
def digest(c):
 h=hashlib.sha256()
 for row in c.execute('SELECT * FROM prefix_overlay ORDER BY key,did'):h.update(repr(row).encode())
 return h.hexdigest()
report={'scope':'Synthetic SQL mechanism test; not a corpus capacity or end-to-end latency benchmark','sqlite_version':sqlite3.sqlite_version,'prefix_rows':40000,'updates_per_arm':20000,'trials':[]}
# Owner: Typeahead health audit. Local scratch only, 128 MiB budget, no corpus copies.
with tempfile.TemporaryDirectory(prefix='typeahead-prefix-replay-') as temporary:
 scratch=Path(temporary);base=scratch/'base.db';c=sqlite3.connect(base);configure(c)
 c.executescript('CREATE TABLE prefix_overlay(key TEXT NOT NULL,did TEXT NOT NULL,score REAL NOT NULL,op INTEGER NOT NULL,updated_at INTEGER NOT NULL,source INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(key,did)) WITHOUT ROWID; CREATE INDEX idx_prefix_overlay_rank ON prefix_overlay(key,score DESC,did ASC) WHERE op=1; CREATE INDEX idx_prefix_overlay_updated_at ON prefix_overlay(updated_at);')
 rows=[(f'h:key-{k:02}',f'did:{i:04}',float(i%10),1,100,0) for i in range(1000) for k in range(40)]
 c.executemany('INSERT INTO prefix_overlay VALUES(?,?,?,?,?,?)',rows);c.commit();c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()
 for trial in range(3):
  for scenario in ['identical_replay','mixed_80_percent_replay','new_timestamp','rescore','delete']:
   for name,sql in ([('replace',old),('guarded_upsert',new)] if trial%2==0 else [('guarded_upsert',new),('replace',old)]):
    with tempfile.TemporaryDirectory(dir=scratch) as work:
     path=Path(work)/'overlay.db';shutil.copyfile(base,path);c=sqlite3.connect(path);configure(c)
     changes=[]
     for key,did,score,op,ts,_ in rows[:20000]:
      actor=int(did[4:]);fresh=scenario not in ['identical_replay','mixed_80_percent_replay'] or (scenario=='mixed_80_percent_replay' and actor%5==0)
      if fresh:
       ts=200
       if scenario in ['rescore','mixed_80_percent_replay']:score+=1
       if scenario=='delete':score=0;op=0
      changes.append((key,did,score,op,ts))
     before=c.total_changes;wall=time.perf_counter();cpu=time.process_time()
     with c:c.executemany(sql,changes)
     cpu=time.process_time()-cpu;wall=time.perf_counter()-wall
     wal=Path(str(path)+'-wal');result={'trial':trial,'scenario':scenario,'implementation':name,'wall_ms':round(wall*1000,3),'cpu_ms':round(cpu*1000,3),'changed_rows':c.total_changes-before,'wal_bytes':wal.stat().st_size if wal.exists() else 0,'state_sha256':digest(c)}
     # Compare behavior after compaction as well as immediately after projection.
     c.execute('DELETE FROM prefix_overlay WHERE updated_at<=150 AND source=0');c.commit();result['compacted_sha256']=digest(c);c.close();report['trials'].append(result)
 groups={}
 for r in report['trials']:groups.setdefault((r['trial'],r['scenario']),[]).append(r)
 assert all(len({r['state_sha256'] for r in rs})==1 and len({r['compacted_sha256'] for r in rs})==1 for rs in groups.values())
 report['all_states_and_compactions_identical']=True
out=root/'docs/evals/system-health-20261027/prefix-replay-benchmark.json';out.write_text(json.dumps(report,indent=2)+'\n')
for scenario in ['identical_replay','mixed_80_percent_replay','new_timestamp','rescore','delete']:
 for name in ['replace','guarded_upsert']:
  rs=[r for r in report['trials'] if r['scenario']==scenario and r['implementation']==name]
  print(json.dumps({'scenario':scenario,'implementation':name,'mean_ms':round(sum(r['wall_ms'] for r in rs)/3,2),'mean_wal_bytes':int(sum(r['wal_bytes'] for r in rs)/3),'changed_rows':rs[0]['changed_rows']}))
print('states and compactions identical; scratch cleaned')
