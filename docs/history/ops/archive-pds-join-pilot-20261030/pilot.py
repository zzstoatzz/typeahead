"""Bounded cloud-only archive join pilot; source credentials are read-only stdin."""
import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT=Path('/tmp/typeahead-pds-join-pilot')
assert sys.platform=='linux' and Path.cwd()==ROOT
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
resource.setrlimit(resource.RLIMIT_FSIZE,(512*1024**2,512*1024**2))
signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(TimeoutError('pilot deadline')))
signal.alarm(300)

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod);return mod

sync=load('join_sync','sync.py');current=load('join_current','current_state.py')
fixture=load('join_fixture','test_sync.py');smoke=load('join_smoke','smoke.py')
smoke.BIN=ROOT/'typeahead-tantivy'
assert hashlib.sha256(smoke.BIN.read_bytes()).hexdigest()=='d1cc0366ed3b28b34daca15d23ee6f06aa1d08844e483c357b80cdb778d5cfa2'
creds=json.load(sys.stdin)
source=sync.Turso(creds['TURSO_URL'],creds['TURSO_AUTH_TOKEN'])
# Reuse the measured uniform rowid sample, not another corpus scan.
sampled=json.load(open('/tmp/typeahead-overlap.json'))['missing_sample'][-1000:]
rows=[];started=time.monotonic()
reader=current.CurrentStateSource(source,sync)
for offset in range(0,len(sampled),250):
    dids=sorted(r['did'] for r in sampled[offset:offset+250])
    # The same bounded source query used by live reconciliation.
    result=source.select(sync.CURRENT_SQL, [json.dumps(dids)])
    for entry in result:
        for values in json.loads(entry['observations_json']):
            row=current.compact_profile([values[0],*values[2:]],values[0],sync)
            if row['did'] is not None:rows.append(row)
print(json.dumps({'phase':'source_read','rows':len(rows),'seconds':time.monotonic()-started}),flush=True)
result={'source_rows':len(rows),'source_read_seconds':time.monotonic()-started,'source_writes':0,'production_routing_changed':False}
# Persist the small source sample only inside the pilot scratch root.
work=ROOT/'work';work.mkdir(exist_ok=False)
f=fixture.Fixture(work,now=int(time.time()))
with sqlite3.connect(f.path) as db:
    db.executemany('INSERT INTO actors VALUES ('+','.join('?' for _ in sync.ACTOR_COLUMNS)+')',[[r[k] for k in sync.ACTOR_COLUMNS] for r in rows]);db.commit()
source_hash=hashlib.sha256(f.path.read_bytes()).hexdigest()
sys.path.insert(0,'/tmp/typeahead-overlap-reader')
from rocksdict import Rdict,Options,AccessType
opt=Options(raw_mode=True);opt.create_if_missing(False)
archive=Rdict('/data/stream/meta.rocksdb',options=opt,access_type=AccessType.read_only())
started=time.monotonic();mapping=work/'pds.sqlite';pairs=[];excluded=0;absent=0
for row in rows:
    value=archive.get(('repo/'+row['did']).encode())
    if value is None:absent+=1;continue
    assert value[0]==5
    a,b,h,j,p,e=struct.unpack_from('<IIHHHH',value,67)
    assert len(value)==84+a+b+h+j+p+e
    host=value[84+a+b:84+a+b+h].decode()
    pds=sync.canonical_archive_pds('https://'+host)
    if pds is None:excluded+=1;continue
    pairs.append((row['did'],pds))
archive.close()
with sqlite3.connect(mapping) as db:
    db.executescript('PRAGMA user_version=1; CREATE TABLE pds_fallback(did TEXT PRIMARY KEY,pds TEXT NOT NULL) WITHOUT ROWID;')
    db.executemany('INSERT INTO pds_fallback VALUES (?,?)',pairs);db.commit()
sha=hashlib.sha256(mapping.read_bytes()).hexdigest()
result.update(mapping_rows=len(pairs),archive_absent=absent,excluded=excluded,mapping_bytes=mapping.stat().st_size,mapping_seconds=time.monotonic()-started,mapping_sha256=sha)
base=[];joined=[]
for r in rows:
    a=sync.actor_record(r);a['version']=f.floor;base.append(a)
with sync.pds_fallback(mapping,sha):
    for r in rows:
        a=sync.actor_record(r);a['version']=f.floor;joined.append(a)
result['filled']=sum(not a.get('pds') and bool(b.get('pds')) for a,b in zip(base,joined))
result['known_unchanged']=all(a['pds']==b['pds'] for a,b in zip(base,joined) if a.get('pds'))
assert result['filled']>=10 and result['known_unchanged']

def seed(path,actors):
    lines=[json.dumps(a,separators=(',',':')).encode()+b'\n' for a in sorted(actors,key=lambda a:a['did'])]
    header={'type':'manifest','build_id':'b1793390000-archive-join-pilot','source_watermark':f.floor,'actor_count':len(actors),'source_actor_count':len(actors),'normalize_version':'v2-cjk-bigram','partial':True}
    footer={'type':'end','build_id':header['build_id'],'actor_count':len(actors),'actors_sha256':hashlib.sha256(b''.join(lines)).hexdigest()}
    path.write_bytes(json.dumps(header).encode()+b'\n'+b''.join(lines)+json.dumps(footer).encode()+b'\n')

def size(path):return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())

child=None
try:
    result['imports']={}
    for name,actors in [('baseline',base),('joined',joined)]:
        seedfile=work/(name+'.jsonl');seed(seedfile,actors);index=work/name
        t=time.monotonic()
        completed=subprocess.run([str(smoke.BIN),'import','--data',str(index),'--input',str(seedfile),'--allow-partial','true','--writer-memory-mb','64'],capture_output=True,text=True,timeout=60)
        if completed.returncode:raise RuntimeError(completed.stderr[-1500:])
        result['imports'][name]={'seconds':time.monotonic()-t,'index_bytes':size(index),'seed_bytes':seedfile.stat().st_size,'receipt':json.loads(completed.stdout)}
    assert hashlib.sha256(f.path.read_bytes()).hexdigest()==source_hash
    candidates=[b for a,b in zip(base,joined) if not a.get('pds') and b.get('pds') and not b.get('hidden') and not b.get('deleted') and b['handle'].lower()!='handle.invalid']
    assert len(candidates)>=10
    with (work/'reader.log').open('w') as log:
        child,public,private=smoke.start(work/'joined',log,'join-pilot-loopback')
        def present(actor,pds):return any(r['did']==actor['did'] for r in smoke.query(public,actor['handle'],pds=pds,limit=100)[0])
        checked=candidates[:10]
        assert all(present(a,a['pds']) and not present(a,'wrong.invalid') for a in checked)
        result['initial_scope_checks']=len(checked)
        with sync.pds_fallback(mapping,sha):
            admin=sync.Admin(private,'join-pilot-loopback',public+'/index/status')
            writer=current.ObservationWriter(f.source(),admin,sync)
            cursor=None
            def reconcile(dids,stage):
                global cursor
                next_cursor={'schema_version':1,'pilot_stage':stage}
                reply=writer.commit(dids,cursor,next_cursor)
                cursor=reply['source_cursor']
            with sqlite3.connect(f.path) as db:
                for a in checked:db.execute('UPDATE actors SET display_name=?,updated_at=? WHERE did=?',('Pilot profile update',f.now,a['did']))
                db.commit()
            reconcile([a['did'] for a in checked],'profile')
            assert all(present(a,a['pds']) for a in checked)
            result['profile_updates_preserve_pds']=len(checked)
            migrated=checked[0]
            with sqlite3.connect(f.path) as db:
                db.execute('UPDATE actors SET pds=?,updated_at=? WHERE did=?',('https://migration.invalid',f.now,migrated['did']));db.commit()
            reconcile([migrated['did']],'migration')
            assert present(migrated,'migration.invalid') and not present(migrated,migrated['pds'])
            result['authoritative_migration_wins']=True
            deleted=checked[1]
            with sqlite3.connect(f.path) as db:db.execute('DELETE FROM actors WHERE did=?',(deleted['did'],));db.commit()
            reconcile([deleted['did']],'deletion')
            assert not present(deleted,deleted['pds']);result['deletion_wins']=True
        smoke.stop(child);child=None
        child,public,private=smoke.start(work/'joined',log,'join-pilot-loopback')
        assert all(present(a,a['pds']) for a in checked[2:])
        assert present(migrated,'migration.invalid') and not present(migrated,migrated['pds']) and not present(deleted,deleted['pds'])
        result['restart_preserves_results']=True
    result['peak_scratch_bytes']=size(work)
    result['passed']=True
finally:
    if child is not None:smoke.stop(child)
    f.close()
    (ROOT/'result.json').write_text(json.dumps(result,indent=2))
    shutil.rmtree(work)
    result['scratch_removed']=not work.exists()
    (ROOT/'result.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result),flush=True)
