import json,sys,urllib.request,urllib.parse,time,os
from pds_repair_ledger import Ledger
creds=json.load(sys.stdin);base=creds['TURSO_URL'].replace('libsql://','https://').rstrip('/')+'/v2/pipeline'
def sql(query,params=None):
 args=[{'name':k,'value':{'type':'integer' if isinstance(v,int) else 'text','value':str(v)}} for k,v in (params or {}).items()]
 req=urllib.request.Request(base,data=json.dumps({'requests':[{'type':'execute','stmt':{'sql':query,'named_args':args}},{'type':'close'}]}).encode(),headers={'Authorization':'Bearer '+creds['TURSO_AUTH_TOKEN'],'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=60) as r:d=json.load(r)
 v=d['results'][0]
 if v['type']!='ok':raise RuntimeError(v.get('error',{}).get('code','query failed'))
 return [[int(c['value']) if c['type']=='integer' else c.get('value') for c in row] for row in v['response']['result']['rows']]
def fetch(url):
 with urllib.request.urlopen(urllib.request.Request(url,headers={'X-Client':'smoke-testing','User-Agent':'typeahead-metadata-pilot/1.0 (@zzstoatzz.io)'}),timeout=10) as r:return json.load(r)
def health():
 for attempt in range(181):
  s=fetch('https://typeahead-tantivy-a.fly.dev/health');i=fetch('https://typeahead-ingester.fly.dev/health')
  ok=s['ready'] and s['sync_lag_s']<120 and i['status']=='ok' and not i['ingest']['backpressure_active'] and i['ingest']['queued']<400 and time.time()-i['ingest']['last_write_at']<15 and 0<=time.time()-i['ingest']['source_event_at']<30
  if ok:return {'search':s,'ingest':i,'at':time.time()}
  print(json.dumps({'waiting_health':{'lag':s['sync_lag_s'],'queued':i['ingest']['queued'],'write_age':time.time()-i['ingest']['last_write_at'],'backpressure':i['ingest']['backpressure_active'],'source_age':time.time()-i['ingest']['source_event_at']}}),flush=True)
  if attempt==180 or time.time()-start>args.seconds:raise RuntimeError('Health or elapsed-time budget exhausted')
  time.sleep(5)
def search(row,pds):
 u='https://typeahead-tantivy-a.fly.dev/search?'+urllib.parse.urlencode({'q':row['handle'],'pds':pds,'limit':10})
 return any(a['did']==row['did'] for a in fetch(u)['actors'])
import argparse,sqlite3,struct,fcntl,hashlib
from pathlib import Path
sys.path.insert(0,'/tmp/typeahead-overlap-reader')
from rocksdict import Rdict,Options,AccessType
p=argparse.ArgumentParser();p.add_argument('--limit',type=int,default=10000);p.add_argument('--seconds',type=int,default=900);args=p.parse_args()
root=Path('/tmp/typeahead-archive-backfill');root.mkdir(exist_ok=True)
lock=(root/'lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
ledger=Ledger(root/'ledger.db')
with sqlite3.connect(ledger.path) as local:
 if local.execute("SELECT count(*) FROM attempts WHERE state NOT IN ('confirmed','conflict')").fetchone()[0]:raise RuntimeError('Reconcile unfinished attempts before resuming')
checkpoint=root/'checkpoint.json'
state=json.loads(checkpoint.read_text()) if checkpoint.exists() else {'cursor':0,'upper':sql('SELECT max(rowid) FROM actors')[0][0],'rows':0,'archive_absent':0,'excluded':0,'hidden':0,'completed':False}
def save():
 temp=root/'checkpoint.tmp';temp.write_text(json.dumps(state));os.replace(temp,checkpoint)
opt=Options(raw_mode=True);opt.create_if_missing(False)
db=Rdict('/data/stream/meta.rocksdb',options=opt,access_type=AccessType.read_only())
excluded={'','unknown','invalid-pds','bsky.network','relay1.us-east.bsky.network','bridgy-hubble.microcosm.blue'}
start=time.time();written=0;events=(root/'events.jsonl').open('a');samples=(root/'samples.jsonl').open('a');pending=[]
print(json.dumps({'start':state,'limit':args.limit}),flush=True)
def flush():
 global written
 if not pending:save();return
 health_before=health();attempts=ledger.prepare_batch(pending);t=time.time();outcome=ledger.send_batch(attempts,sql)
 written+=outcome['confirmed'];save()
 record={'at':time.time(),'cursor':state['cursor'],'batch':len(pending),'outcome':outcome,'write_seconds':time.time()-t,'elapsed':time.time()-start,'health':health_before}
 events.write(json.dumps(record)+'\n');events.flush();print(json.dumps({k:v for k,v in record.items() if k!='health'}),flush=True)
 for row in pending:
  if int.from_bytes(hashlib.sha256(row['did'].encode()).digest()[:2],'little')%1000==0:samples.write(json.dumps(row)+'\n')
 samples.flush();pending.clear();time.sleep(1)
try:
 health()
 while written<args.limit and time.time()-start<args.seconds and not state['completed']:
  if (root/'STOP').exists():break
  if sum(f.stat().st_size for f in root.iterdir() if f.is_file())>4*1024**3:raise RuntimeError('4 GiB audit disk budget reached')
  page=sql("SELECT rowid,did,updated_at,hidden FROM actors WHERE rowid>:cursor AND rowid<=:upper AND pds='' ORDER BY rowid LIMIT 2000",{'cursor':state['cursor'],'upper':state['upper']})
  if not page:state['completed']=True;save();break
  for rowid,did,updated,hidden in page:
   if written+len(pending)>=args.limit:break
   state['cursor']=rowid;state['rows']+=1
   if hidden:state['hidden']+=1;continue
   value=db.get(('repo/'+did).encode())
   if value is None:state['archive_absent']+=1;continue
   if value[0]!=5:raise RuntimeError('Unexpected archive encoding')
   a,b,h,j,pl,e=struct.unpack_from('<IIHHHH',value,67)
   if len(value)!=84+a+b+h+j+pl+e:raise RuntimeError('Invalid archive encoding')
   host=value[84+a+b:84+a+b+h].decode()
   if host in excluded or '.' not in host or '/' in host or '@' in host or any(c.isspace() for c in host):state['excluded']+=1;continue
   pending.append({'did':did,'pds':'https://'+host,'observed_updated_at':updated})
   if len(pending)==1000:flush()
  flush()
  if time.time()-start>=args.seconds:break
 save();print(json.dumps({'end':state,'written_this_run':written,'seconds':time.time()-start}),flush=True)
finally:db.close();events.close();samples.close()
