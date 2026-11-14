"""Export metadata only on the existing archive host; no live-source writes."""
import hashlib,json,os,resource,signal,sqlite3,struct,sys,time
from pathlib import Path
sys.path.insert(0,'/tmp/typeahead-overlap-reader')
from rocksdict import Rdict,Options,AccessType
from sync import canonical_archive_pds
root=Path('/tmp/typeahead-pds-join-full')
assert sys.platform=='linux' and Path.cwd()==root
resource.setrlimit(resource.RLIMIT_FSIZE,(3*1024**3,3*1024**3))
signal.signal(signal.SIGALRM,lambda *_:(_ for _ in ()).throw(TimeoutError('mapping export deadline')))
signal.alarm(1200)
path=root/'pds-fallback.sqlite';assert not path.exists()
start=time.time();counts={'archive_rows':0,'mapped':0,'excluded':0};pending=[]
opt=Options(raw_mode=True);opt.create_if_missing(False)
archive=Rdict('/data/stream/meta.rocksdb',options=opt,access_type=AccessType.read_only())
db=sqlite3.connect(path)
try:
 db.executescript('PRAGMA user_version=1; PRAGMA journal_mode=DELETE; PRAGMA cache_size=-32768; CREATE TABLE pds_fallback(did TEXT PRIMARY KEY,pds TEXT NOT NULL) WITHOUT ROWID;')
 for key,value in archive.items():
  if not key.startswith(b'repo/'):continue
  counts['archive_rows']+=1
  if value[0]!=5:raise ValueError('archive encoding changed')
  a,b,h,j,p,e=struct.unpack_from('<IIHHHH',value,67)
  if len(value)!=84+a+b+h+j+p+e:raise ValueError('invalid archive metadata')
  host=value[84+a+b:84+a+b+h].decode()
  pds=canonical_archive_pds('https://'+host)
  if pds is None:counts['excluded']+=1;continue
  did=key[5:].decode()
  if not did.startswith('did:') or len(did)>2048:raise ValueError('invalid archive DID')
  pending.append((did,pds));counts['mapped']+=1
  if len(pending)==10000:
   db.executemany('INSERT INTO pds_fallback VALUES (?,?)',pending);db.commit();pending.clear()
  if counts['archive_rows']%1000000==0:print(json.dumps(dict(counts,seconds=time.time()-start,bytes=path.stat().st_size)),flush=True)
 db.executemany('INSERT INTO pds_fallback VALUES (?,?)',pending);db.commit()
 assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
finally:db.close();archive.close()
with path.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
result=dict(counts,started_at=start,finished_at=time.time(),seconds=time.time()-start,byte_size=path.stat().st_size,sha256=sha,schema_version=1,source='existing Stream repo host metadata',authority='archive-fallback-only',live_source_writes=0)
(root/'mapping-receipt.json').write_text(json.dumps(result,indent=2));path.chmod(0o400)
print(json.dumps(result),flush=True)
