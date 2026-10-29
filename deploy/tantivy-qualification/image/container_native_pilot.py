"""Executed only inside the local Linux image with --network none."""
import hashlib,json,os,pathlib,signal,sqlite3,subprocess,time,urllib.request
root=pathlib.Path('/data/qualification/run');root.mkdir(parents=True);(root/'owner').write_text('native-qualification-v1')
source=root/'source.db'
with sqlite3.connect(source) as db:
 db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
 db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(f'did:fixture:{i:02}',f'g{i:02}.fixture.test',f'Actor {i}','','[]','https://local.test','','{}',0,i,int(time.time())-60) for i in range(32)])
env={**os.environ,'MODE':'indexer','INDEX_CHANNEL':'local','INDEX_SOURCE':'export','INDEX_SAMPLE_LIMIT':'0','INDEX_PDS_MAX_ACTORS':'10000',
     'INDEX_REUSE_SOURCE':str(source),'INDEX_BUILD_ROOT':str(root/'build'),'INDEXER_VERSION':'linux-container-native-qualification-fixture'}
with (root/'builder.log').open('w') as log:subprocess.run(['/app/bin/typeahead-ingester'],env=env,stdout=log,stderr=log,check=True,timeout=60)
snapshot=next((root/'build').glob('build-*/index.db'))
with sqlite3.connect(snapshot) as db:
 floor=int(db.execute("SELECT value FROM index_meta WHERE key='source_watermark'").fetchone()[0])
 marker=db.execute("SELECT value FROM index_meta WHERE key='flat_prefix_sha256'").fetchone()
if marker:
 subprocess.run(['/app/bin/admit-prefix'],env={**os.environ,'TYPEAHEAD_ADMIT_PREFIX_PATH':str(snapshot)+'.prefix','TYPEAHEAD_ADMIT_PREFIX_SHA256':marker[0]},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=20)
feed=[{'cursor':1,'expected_cursor':0,'actors':[{'did':'did:fixture:00','handle':'renamed.fixture.test','display_name':'Renamed Actor','quality_score':10,'version':floor+1}]}]
feed_bytes=json.dumps(feed,separators=(',',':')).encode();feedsha=hashlib.sha256(feed_bytes).hexdigest();(root/'feed.json').write_bytes(feed_bytes)
corpus=hashlib.sha256(snapshot.read_bytes()).hexdigest()
observer={'app':'typeahead-qual-container-fixture','machine_id':'fixture-container-machine','guest':{'cpu_kind':'shared','cpus':2,'memory_mb':1024},
          'volume_ids':['fixture-volume'],'final_volume_gib':20,'public_services':[],
          'machines_api_response_sha256':hashlib.sha256(b'SYNTHETIC-LOCAL-NOT-PROVIDER-EVIDENCE').hexdigest(),'synthetic_local_fixture':True}
(root/'observer.json').write_text(json.dumps(observer))
config={'schema':1,'arm':'native','root':str(root),'app':observer['app'],'machine_id':observer['machine_id'],'guest':observer['guest'],
        'volume_id':'fixture-volume','final_volume_gib':20,'nonce':'linux-container-native-fixture','corpus_sha256':corpus,'updates_sha256':feedsha,
        'observer_receipt':str(root/'observer.json'),'private_listen':'::','measurement_deadline_s':60,
        'paths':{key:str(root/name) for key,name in [('overlay','overlay.db'),('tmp','tmp'),('request','request.json'),('response','response.json'),('tantivy_data','tantivy')]}}
config['paths']['snapshot']=str(snapshot);(root/'run.json').write_text(json.dumps(config))
child=None
try:
 log=(root/'runtime.log').open('w')
 child=subprocess.Popen(['python3','/app/entrypoint.py','serve'],env={**os.environ,'FLY_APP_NAME':observer['app'],'FLY_MACHINE_ID':observer['machine_id']},stdout=log,stderr=log,start_new_session=True)
 def get(path):
  with urllib.request.urlopen('http://[::1]:8080'+path,timeout=2) as response:return json.load(response)
 deadline=time.monotonic()+25
 while True:
  try:
   identity=get('/__evaluation')
   if identity['ready']:break
  except OSError:pass
  if child.poll() is not None or time.monotonic()>deadline:raise RuntimeError('runtime startup failed: '+(root/'runtime.log').read_text()[-2000:])
  time.sleep(.05)
 assert identity['arm']=='native' and not identity['source_ready_attestation']
 assert get('/search?q=g00.fixture.test&limit=10')['actors'][0]['did']=='did:fixture:00'
 request=feed[0]|{'feed_sha256':feedsha};temp=root/'request.part';temp.write_text(json.dumps(request));temp.replace(root/'request.json')
 deadline=time.monotonic()+10
 while (root/'request.json').exists():
  if time.monotonic()>deadline:raise TimeoutError('native file-inbox acknowledgement')
  time.sleep(.02)
 receipt=json.loads((root/'response.json').read_text());assert receipt['accepted'] and receipt['applied']==1 and receipt['transactions']==1
 assert get('/search?q=g00.fixture.test&limit=10')['actors']==[]
 assert get('/search?q=renamed.fixture.test&limit=10')['actors'][0]['did']=='did:fixture:00'
 print('QUALIFICATION_CONTAINER_NATIVE '+json.dumps({'passed':True,'actors':32,'actual_linux_builder':True,'actual_native_search':True,
       'actual_frozen_batch_commit':True,'private_proxy':True,'source_ready_attestation':False,'provider_hardware_attested':False,
       'network':'none','cloud_calls':0,'scratch_bytes':sum(p.stat().st_size for p in root.rglob('*') if p.is_file())}))
finally:
 if child and child.poll() is None:
  child.send_signal(signal.SIGTERM)
  try:child.wait(timeout=5)
  except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
