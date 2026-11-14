import hashlib,json,random,re,shutil,sqlite3,tempfile,time
from pathlib import Path
repo=Path.cwd();out=repo/'docs/evals/perf-goal-20261027/writer-cache-model.json'
if out.exists():raise RuntimeError('preserve prior evidence')
sql=json.loads(re.search(r'const upsert_prefix_sql = (".*");',(repo/'services/src/index/overlay.zig').read_text()).group(1))
report={'scope':'SQLite write-cache mechanism experiment on macOS; not Fly capacity or HTTP latency','sqlite_version':sqlite3.sqlite_version,'actor_count':4000,'prefixes_per_actor':50,'batch_actors':500,'scratch_budget_bytes':256*1024**2,'trials':[]}
def setup(c,kib):
 c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL');c.execute('PRAGMA cache_size=-'+str(kib));c.execute('PRAGMA wal_autocheckpoint=1000')
def digest(c):
 h=hashlib.sha256()
 for row in c.execute('SELECT * FROM prefix_overlay ORDER BY key,did'):h.update(repr(row).encode())
 return h.hexdigest()
with tempfile.TemporaryDirectory(prefix='typeahead-writer-cache-') as directory:
 root=Path(directory)
 def guard():
  size=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
  if size>report['scratch_budget_bytes']:raise RuntimeError('scratch budget exceeded')
 base=root/'base.db';c=sqlite3.connect(base);setup(c,131072)
 c.executescript('CREATE TABLE prefix_overlay(key TEXT NOT NULL,did TEXT NOT NULL,score REAL NOT NULL,op INTEGER NOT NULL,updated_at INTEGER NOT NULL,source INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(key,did)) WITHOUT ROWID; CREATE INDEX idx_prefix_overlay_rank ON prefix_overlay(key,score DESC,did ASC) WHERE op=1; CREATE INDEX idx_prefix_overlay_updated_at ON prefix_overlay(updated_at);')
 for actor in range(4000):
  c.executemany('INSERT INTO prefix_overlay VALUES(?,?,?,?,?,?)',[(f'h:key-{k:02}',f'did:plc:fixtureactor{actor:06}',float(actor%10),1,100,0) for k in range(50)])
 c.commit();c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close();guard()
 report['base_bytes']=base.stat().st_size
 order=list(range(4000));random.Random(20261027).shuffle(order)
 for trial in range(3):
  for kib in ([20000,131072] if trial%2==0 else [131072,20000]):
   target=root/'working.db';shutil.copyfile(base,target);c=sqlite3.connect(target);setup(c,kib)
   wall=0;cpu=0;frames=0;peak_wal=0
   for first in range(0,4000,500):
    updates=[(f'h:key-{k:02}',f'did:plc:fixtureactor{actor:06}',float(actor%10)+(1 if actor%2 else 0),1,200 if actor%2 else 100) for actor in order[first:first+500] for k in range(50)]
    start=time.perf_counter();tick=time.process_time()
    with c:c.executemany(sql,updates)
    wall+=time.perf_counter()-start;cpu+=time.process_time()-tick
    checkpoint=c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone();assert checkpoint[0]==0 and checkpoint[1]>=1000,checkpoint
    frames+=checkpoint[1];peak_wal=max(peak_wal,Path(str(target)+'-wal').stat().st_size);guard()
   row={'trial':trial,'cache_kib':kib,'projection_ms':round(wall*1000,2),'cpu_ms':round(cpu*1000,2),'wal_frames':frames,'wal_frame_bytes':frames*4120,'peak_wal_bytes':peak_wal,'state_sha256':digest(c)}
   c.execute('DELETE FROM prefix_overlay WHERE updated_at<=150 AND source=0');c.commit();row['compacted_sha256']=digest(c);c.close();report['trials'].append(row)
   for suffix in ['','-wal','-shm']:(root/('working.db'+suffix)).unlink(missing_ok=True)
   print(json.dumps(row),flush=True)
 report['all_states_and_compactions_identical']=len({r['state_sha256'] for r in report['trials']})==1 and len({r['compacted_sha256'] for r in report['trials']})==1
 assert report['all_states_and_compactions_identical']
report['scratch_retired']=not root.exists();out.write_text(json.dumps(report,indent=2)+'\n')
