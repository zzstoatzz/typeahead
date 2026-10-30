import concurrent.futures,hashlib,http.client,json,math,os,random,socket,ssl,string,threading,time,urllib.parse
from pathlib import Path
TARGET=os.environ['TANTIVY_LOAD_TARGET'].rstrip('/')
u=urllib.parse.urlsplit(TARGET)
if u.scheme not in ['http','https'] or not u.hostname or not (u.hostname=='typeahead-tantivy-dev.fly.dev' or u.hostname.endswith('.vm.typeahead-tantivy-dev.internal')):raise RuntimeError('load target is not isolated dev backend')
STARTED_AT=time.time();START=time.monotonic();HARD=900
local=threading.local()
def request(path):
 started=time.monotonic()
 try:
  if not hasattr(local,'conn'):
   local.conn=http.client.HTTPSConnection(u.hostname,u.port or 443,timeout=5,context=ssl.create_default_context()) if u.scheme=='https' else http.client.HTTPConnection(u.hostname,u.port or 80,timeout=5)
  local.conn.request('GET',path,headers={'User-Agent':'typeahead-tantivy-dev-qualification/1','Connection':'keep-alive'})
  response=local.conn.getresponse();body=response.read(524289)
  if len(body)>524288:raise RuntimeError('response byte budget')
  headers=dict((k.lower(),v) for k,v in response.getheaders());headers['_body_sha256']=hashlib.sha256(body).hexdigest();data=json.loads(body) if body and 'application/json' in headers.get('content-type','') else None
  return response.status,headers,data,(time.monotonic()-started)*1000,None
 except Exception as e:
  if hasattr(local,'conn'):
   local.conn.close();del local.conn
  return None,{},None,(time.monotonic()-started)*1000,type(e).__name__
def search_path(q,limit=10,**kw):return '/search?'+urllib.parse.urlencode({'q':q,'limit':limit,**kw})
def correct(status,headers,body,expect=200,exact=None,pds=None):
 if status!=expect or not isinstance(body,dict):return False
 if expect!=200:return 'error' in body
 actors=body.get('actors')
 if not isinstance(actors,list) or len(actors)>100:return False
 dids=[a.get('did') for a in actors]
 if any(not isinstance(d,str) or not d.startswith('did:') for d in dids) or len(dids)!=len(set(dids)):return False
 if exact and (not actors or actors[0].get('handle','').lower()!=exact.lower()):return False
 if pds and headers.get('x-typeahead-pds')!=pds:return False
 return True
QUERIES=['nate','alice','bob','music','justin','Justin Fa','Fa Justin','eric','éric','photographer','developer','東京','日本','Alice 日','zzstoatzz.io','pfrazee.com','jay.bsky.team','natemoo.re','alex','art','a','b','c','jo','an','ma','al','sam','john','rose','science','test nonexistent-unlikely-term-4729','selfhosted.social']
CASES=[(search_path(q),{'exact':q if q in ['zzstoatzz.io','pfrazee.com','jay.bsky.team','natemoo.re'] else None}) for q in QUERIES]
CASES += [(search_path('a',pds='selfhosted.social'),{'pds':'selfhosted.social'}),(search_path('a',prefer_pds='selfhosted.social',preference='pds_first'),{}),(search_path('a',prefer_pds='selfhosted.social',preference='boost',boost='100'),{}),(search_path('a',limit=100),{})]

def pct(v,p):return sorted(v)[math.ceil(len(v)*p)-1] if v else None
def snapshot():
 status,_,body,_,error=request('/index/status')
 if status!=200 or error or body.get('engine')!='tantivy' or not body.get('import_complete') or not body.get('full_source'):raise RuntimeError('not an admitted full dev index')
 return {k:body.get(k) for k in ['build_id','source_build_id','actor_count','watermark','snapshot_watermark','source_cursor','segments','ready','sync_lag_s','pds_missing_actor_count','pds_invalid_actor_count','pds_unknown_actor_count','pds_invalid_reason_counts']}
def phase(name,duration,rps,concurrency,cases=None):
 cases=CASES if cases is None else cases
 before=snapshot();started_at=time.time();start=time.monotonic();attempts=int(duration*rps)
 def one(i):
  scheduled=start+i/rps;time.sleep(max(0,scheduled-time.monotonic()));queue=(time.monotonic()-scheduled)*1000
  if time.monotonic()-START>HARD:raise TimeoutError('load deadline')
  path,policy=cases[i%len(cases)];status,headers,body,elapsed,error=request(path)
  return {'case':i%len(cases),'path':path,'correct':correct(status,headers,body,**policy),'status':status,'request_ms':elapsed,'queue_ms':queue,'total_ms':(time.monotonic()-scheduled)*1000,'error':error}
 with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:rows=list(pool.map(one,range(attempts)))
 after=snapshot();failures=[r for r in rows if not r['correct']]
 summary={'phase':name,'started_at':started_at,'finished_at':time.time(),'attempts':attempts,'failures':len(failures),'failure_examples':failures[:12],'target_rps':rps,'concurrency':concurrency,'before':before,'after':after,'latency':{k:{str(p):pct([r[k] for r in rows],p) for p in [.5,.95,.99,1]} for k in ['request_ms','queue_ms','total_ms']}}
 print('TANTIVY_DEV_LOAD_PHASE '+json.dumps(summary,separators=(',',':')),flush=True)
 return summary

def diverse_cases():
 prefixes=set();seed_responses=0
 for letter in string.ascii_lowercase:
  code,headers,body,_,error=request(search_path(letter,100))
  if error or not correct(code,headers,body):raise RuntimeError('diverse query seeding failed')
  seed_responses+=1
  for actor in body['actors']:
   handle=actor['handle']
   if not handle.isascii():continue
   for n in [2,3,4,5,6,8,10,12]:
    if len(handle)>n and all(c.isalnum() or c in '.-' for c in handle[:n]):prefixes.add(handle[:n])
 paths=sorted(search_path(p) for p in prefixes);random.Random(20261028).shuffle(paths);paths=paths[:512]
 if len(paths)<256:raise RuntimeError('insufficient diverse query coverage')
 return [(path,{}) for path in paths],{'seed_requests':seed_responses,'distinct_cases':len(paths),'case_paths_sha256':hashlib.sha256('\n'.join(paths).encode()).hexdigest(),'derivation':'26 alphabet queries at limit100; deterministic sample of handle prefixes2..12; no source reads'}

def overload():
 barrier=threading.Barrier(32)
 def one(i):
  if i<32:barrier.wait(timeout=10)
  path,policy=CASES[i%len(CASES)];code,headers,body,elapsed,error=request(path)
  rejected=code==503 and isinstance(body,dict) and body.get('error')=='search admission exhausted'
  return {'correct':error is None and (correct(code,headers,body,**policy) or rejected),'status':code,'rejected':rejected,'request_ms':elapsed,'error':error,'path':path}
 with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:rows=list(pool.map(one,range(256)))
 recovery=request(search_path('zzstoatzz.io'));ready=request('/ready')
 return {'attempts':len(rows),'concurrency':32,'failures':sum(not r['correct'] for r in rows),'failure_examples':[r for r in rows if not r['correct']][:12],'admission_rejections':sum(r['rejected'] for r in rows),'admission_exhaustion_observed':any(r['rejected'] for r in rows),'accepted':sum(r['status']==200 for r in rows),'recovered':correct(recovery[0],recovery[1],recovery[2],exact='zzstoatzz.io') and ready[0]==200,'request_p99_ms':pct([r['request_ms'] for r in rows],.99)}

def main():
 initial=snapshot()
 if not initial['ready']:raise RuntimeError('dev source is not ready/fresh')
 checks=[]
 for path,expected in [('/ready',200),('/admin/apply',404),('/search',400),(search_path('a',pds='bad@host.test'),400),(search_path('a'*513),400)]:
  status,headers,body,elapsed,error=request(path);checks.append({'path':path[:100],'expected_status':expected,'status':status,'correct':status==expected and error is None})
 q='zzstoatzz.io';query=urllib.parse.urlencode({'q':q,'limit':10})
 a=request('/xrpc/tech.waow.typeahead.searchActors?'+query);b=request('/xrpc/app.bsky.actor.searchActorsTypeahead?'+query)
 checks.append({'case':'alias-byte-equivalent-shape','correct':a[0]==b[0]==200 and a[2]==b[2] and a[1].get('_body_sha256')==b[1].get('_body_sha256')})
 first=phase('post-restart-varied-prefix',len(CASES)/2,2,1)
 diverse,seed=diverse_cases()
 phases=[first,phase('diverse-handle-prefixes',len(diverse)/10,10,4,diverse),phase('warm',120,10,4),phase('live-replay',300,10,4)]
 burst=overload()
 result={'owner':'tantivy-dev-20261028','kind':'cloud-separated-load-generator','target':TARGET,'initial':initial,'checks':checks,'diverse_seed':seed,'phases':[{k:v for k,v in p.items() if k not in ['before','after','failure_examples']} for p in phases],'overload':burst,'passed_correctness':all(c['correct'] for c in checks) and all(p['failures']==0 for p in phases) and burst['failures']==0 and burst['recovered'],'latency_gate':all(p['latency']['total_ms']['0.95']<=50 and p['latency']['total_ms']['0.99']<=100 for p in phases if p['phase'] in ['warm','live-replay']),'limitations':['Source is live; exact expected topK for broad real queries is checked separately, not inferred from response shape.','Post-restart phase is not a provider-physical-disk cold guarantee.','Real replay mutation count must be corroborated with source/application receipts; empty cursor commits are not actor batches.','Burst admission rejection is expected and reported separately from normal-rate correctness/latency.'],'wall_s':time.monotonic()-START,'started_at':STARTED_AT,'finished_at':time.time()}
 print('TANTIVY_DEV_LOAD_RESULT '+json.dumps(result,separators=(',',':')),flush=True)
if __name__=='__main__':main()
