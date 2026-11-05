import hashlib,io,json,os,resource,shutil,signal,tarfile,tempfile,time
from pathlib import Path
signal.alarm(300)
def cpu():
 r=resource.getrusage(resource.RUSAGE_SELF);return r.ru_utime+r.ru_stime
with tempfile.TemporaryDirectory(prefix='typeahead-install-cpu-',dir='/tmp') as folder:
 root=Path(folder);archive=root/'fixture.tar';payload=bytes(range(256))*4096
 class Source:
  def read(self,n):return payload[:n]
 size=512*1024**2
 with tarfile.open(archive,'w:') as tar:
  for i in range(16):
   info=tarfile.TarInfo('index/part-'+str(i));info.size=size//16
   tar.addfile(info,Source())
 with archive.open('rb') as f:expected=hashlib.file_digest(f,'sha256').hexdigest()
 # Drain startup burst credits before measuring sustained installation work.
 before=cpu();began=time.monotonic()
 while cpu()-before<12:hashlib.sha256(payload).digest()
 burn={'wall_s':time.monotonic()-began,'cpu_s':cpu()-before}
 results=[]
 for name in ['hash-then-extract','hash-during-extract']:
  target=root/name;target.mkdir();before=cpu();began=time.monotonic()
  if name=='hash-then-extract':
   with archive.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==expected
  class Reader:
   def __init__(self,f):self.f=f;self.digest=hashlib.sha256()
   def read(self,n=-1):
    data=self.f.read(n);self.digest.update(data);return data
  with archive.open('rb') as f:
   reader=Reader(f)
   with tarfile.open(fileobj=reader if name=='hash-during-extract' else f,mode='r|') as tar:
    for item in tar:
     source=tar.extractfile(item);digest=hashlib.sha256()
     with (target/Path(item.name).name).open('xb') as out:
      while chunk:=source.read(1024**2):digest.update(chunk);out.write(chunk)
      out.flush();os.fsync(out.fileno())
   if name=='hash-during-extract':
    while reader.read(1024**2):pass
    assert reader.digest.hexdigest()==expected
  results.append({'name':name,'wall_s':time.monotonic()-began,'cpu_s':cpu()-before})
  shutil.rmtree(target)
 print(json.dumps({'event':'installation_cpu_comparison','machine':os.environ.get('FLY_MACHINE_ID'),'fixture_bytes':size,'archive_sha256':expected,'burn':burn,'results':results}),flush=True)
print(json.dumps({'event':'installation_cpu_cleanup','free_bytes':shutil.disk_usage('/tmp').free}),flush=True)
