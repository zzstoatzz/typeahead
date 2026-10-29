import hashlib,json,os,shutil,signal,subprocess,sys,time,urllib.parse,urllib.request
from pathlib import Path
ROOT=Path('/data')
SOURCE=ROOT/'source'
DATA=ROOT/'tantivy'
START=time.monotonic()
DEADLINE=int(os.environ.get('BOOTSTRAP_DEADLINE_SECONDS','5400'))
PAYLOAD_BUDGET=32*1024**3
RESERVE=4*1024**3
children=[]
def atomic(path,value):
 part=path.with_suffix('.tmp');part.write_text(json.dumps(value,separators=(',',':'))+'\n')
 with part.open('rb') as stream:os.fsync(stream.fileno())
 part.replace(path)
def status(phase,**kw):
 atomic(ROOT/'bootstrap.json',{'phase':phase,'elapsed_s':round(time.monotonic()-START,2),**kw})
 print(json.dumps({'phase':phase,**kw}),flush=True)
def guard():
 if time.monotonic()-START>DEADLINE:raise TimeoutError('bootstrap deadline')
 if shutil.disk_usage(ROOT).free<RESERVE:raise RuntimeError('dev disk reserve breached')
 # Include source and growing index/merge files; never walk any external volume.
 total=0
 for p in ROOT.rglob('*'):
  try:
   if p.is_file():total+=p.stat().st_size
  except FileNotFoundError:pass
 if total>PAYLOAD_BUDGET:raise RuntimeError('dev payload budget breached')
def download(cap,path,expected):
 parsed=urllib.parse.urlsplit(cap)
 if parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.r2.cloudflarestorage.com'):raise ValueError('source capability endpoint rejected')
 if path.is_file() and path.stat().st_size==expected['byte_size']:
  verified=hashlib.sha256()
  with path.open('rb') as existing:
   while chunk:=existing.read(1024*1024):verified.update(chunk)
  if verified.hexdigest()==expected['sha256']:return
 part=path.with_suffix(path.suffix+'.part');sha=hashlib.sha256();total=0;started=time.monotonic()
 with urllib.request.urlopen(cap,timeout=45) as response,part.open('wb') as out:
  while block:=response.read(1024*1024):
   total+=len(block)
   if total>expected['byte_size']:raise ValueError('source transfer exceeds manifest')
   out.write(block);sha.update(block)
   if total%(64*1024*1024)<1024*1024:guard();status('downloading',artifact=path.name,bytes=total,expected=expected['byte_size'])
   time.sleep(max(0,total/(80*1024*1024)-(time.monotonic()-started)))
  out.flush();os.fsync(out.fileno())
 if total!=expected['byte_size'] or sha.hexdigest()!=expected['sha256']:raise ValueError('source digest or size mismatch')
 part.replace(path)
def stop():
 for child in reversed(children):
  if child.poll() is None:
   os.killpg(child.pid,signal.SIGTERM)
   try:child.wait(timeout=10)
   except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
def interrupted(*_):raise KeyboardInterrupt()
def expired(*_):raise TimeoutError('bootstrap hard deadline')
signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
signal.signal(signal.SIGALRM,expired);signal.alarm(DEADLINE)
def main():
 ROOT.mkdir(exist_ok=True)
 # Successful imports survive service restarts; no artifact fetch on ordinary boot.
 if (ROOT/'admitted.json').exists():
  receipt=json.loads((ROOT/'admitted.json').read_text())
  if receipt.get('complete') and DATA.is_dir():
   status('serving-existing-generation',build_id=receipt['build_id'])
   return serve()
 manifest=json.loads(Path('/app/source-manifest.json').read_text())
 access=json.loads(os.environ['TANTIVY_SOURCE_ACCESS'])
 if access['build_id']!=manifest['active_build']:raise ValueError('source capability release differs')
 SOURCE.mkdir(exist_ok=True)
 status('source-validation',build_id=manifest['active_build'],source_actor_count=manifest['actor_count'])
 for name,expected,key in [('index.db',manifest,'database'),('index.db.prefix',manifest['prefix_map'],'prefix')]:
  guard();download(access[key],SOURCE/name,expected)
 (SOURCE/'manifest.json').write_text(json.dumps(manifest)+'\n')
 status('importing',build_id=manifest['active_build'],source_actor_count=manifest['actor_count'])
 exporter=['python3','/app/input/export.py','--manifest',str(SOURCE/'manifest.json'),'--database',str(SOURCE/'index.db'),'--prefix',str(SOURCE/'index.db.prefix'),'--build-id',manifest['active_build'],'--scratch-root',str(ROOT),'--output','-','--receipt',str(ROOT/'export-receipt.json'),'--max-seconds',str(DEADLINE)]
 # The importer owns its commit-complete marker and checks stream count/hash/footer.
 producer=subprocess.Popen(exporter,stdout=subprocess.PIPE,stderr=open(ROOT/'export.log','ab'),start_new_session=True);children.append(producer)
 consumer=subprocess.Popen(['/app/typeahead-tantivy','import','--data',str(DATA),'--input','-','--build-id',manifest['active_build']],stdin=producer.stdout,stdout=open(ROOT/'import.log','ab'),stderr=subprocess.STDOUT,start_new_session=True);children.append(consumer);producer.stdout.close()
 while consumer.poll() is None:
  guard();time.sleep(5)
 if consumer.returncode or producer.wait(timeout=10):raise RuntimeError('cloud-local export/import failed')
 receipt=json.loads((ROOT/'export-receipt.json').read_text())
 if receipt.get('partial') or receipt.get('emitted_actor_count')!=manifest['actor_count']:raise ValueError('full corpus receipt mismatch')
 atomic(ROOT/'admitted.json',{'complete':True,'build_id':manifest['active_build'],'actor_count':manifest['actor_count'],'source_watermark':manifest['source_watermark'],'export_receipt':receipt})
 shutil.rmtree(SOURCE)
 status('import-complete',build_id=manifest['active_build'],actor_count=manifest['actor_count'],source_payload_retired=True)
 return serve()
def serve():
 signal.alarm(0)
 signal.alarm(0)
 server=subprocess.Popen(['/app/typeahead-tantivy','serve','--data',str(DATA),'--listen','0.0.0.0:8080','--admin-listen','127.0.0.1:8081','--max-sync-lag-s','120'],start_new_session=True)
 children.append(server)
 try:
  # Public read API and loopback replay live on this cloud VM; source SQL stays read-only.
  deadline=time.monotonic()+120
  while True:
   if server.poll() is not None:raise RuntimeError('dev server exited during startup')
   try:
    with urllib.request.urlopen('http://127.0.0.1:8080/index/status',timeout=2) as response:
     if response.status==200 and json.load(response).get('import_complete'):break
   except OSError:pass
   if time.monotonic()>deadline:raise TimeoutError('dev server readiness')
   time.sleep(.5)
  status('serving-and-replaying')
  while server.poll() is None:
   replay=subprocess.Popen(['python3','/app/input/sync.py','--policy-mode','snapshot','--status-file','/data/replay-status.json','--max-run-seconds','3600','--max-source-bytes','536870912'],stdout=open(ROOT/'replay.log','ab'),stderr=subprocess.STDOUT,start_new_session=True)
   children.append(replay);started=time.monotonic()
   while replay.poll() is None:
    if server.poll() is not None:raise RuntimeError('dev search server stopped')
    time.sleep(2)
   children.remove(replay)
   if time.monotonic()-started<30:
    status('replay-process-failed',exit_code=replay.returncode)
    time.sleep(30)
   # Bounded replay lifetimes rotate; index cursor survives each process restart.
   for name in ['replay.log','import.log','export.log']:
    path=ROOT/name
    if path.exists() and path.stat().st_size>16*1024*1024:path.replace(ROOT/(name+'.previous'))
  raise RuntimeError('dev search server exited')
 finally:stop()
try:main()
except BaseException as exc:
 stop();status('failed',error_type=type(exc).__name__)
 raise
