import datetime,json,os,signal,subprocess,time,tomllib,urllib.request
from pathlib import Path
OLD='e8c21659-0b2c-4cec-beb4-86b4ecf91191';NEW='40a014e4-5ec0-467d-a8e7-214b626436ce'
root=Path('docs/evals/perf-goal-20261027/transport-overlap/metrics-canary');root.mkdir(exist_ok=False)
record={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'preparing','old':OLD,'candidate':NEW,'health':[],'provider':[],'generated_transport_probes':0,'public_mean_gate_ms':185,'public_miss_minimum':100};owned=None;success=False;changed=False;smoke=None

def save(): (root/'result.json').write_text(json.dumps(record,indent=2)+'\n')
def deployment():return max(json.loads(subprocess.check_output(['node_modules/.bin/wrangler','deployments','list','-c','wrangler.search-transport.jsonc','--json'],timeout=45)),key=lambda x:x['created_on'])
def deploy(version,message):
 with (root/('rollback.log' if version==OLD else 'deploy.log')).open('ab') as f:subprocess.run(['node_modules/.bin/wrangler','versions','deploy',version+'@100%','-c','wrangler.search-transport.jsonc','--yes','--message',message],stdout=f,stderr=subprocess.STDOUT,check=True,timeout=90)
def get(url):
 r=urllib.request.Request(url,headers={'User-Agent':'typeahead-perf/1.0'})
 with urllib.request.urlopen(r,timeout=12) as f:
  body=f.read(131073)
  if len(body)>131072:raise RuntimeError('probe body budget')
  return f.status,json.loads(body)
def health():
 status,data=get('https://typeahead.waow.tech/health/freshness');record['health'].append({'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':status,**data})
 if status!=200 or not data.get('ok') or data.get('build_id')!='b1793081565-0228-px05':raise RuntimeError('freshness/build gate')
def probe():
 status,data=get('https://typeahead-search-transport.n8-3e9.workers.dev/search?q=zzstoatzz.io&limit=1');record['generated_transport_probes']+=1
 if status!=200 or not data.get('actors') or data['actors'][0].get('handle')!='zzstoatzz.io':raise RuntimeError('exact-handle outcome gate')
def provider(first,last):
 token=tomllib.loads((Path.home()/'Library/Preferences/.wrangler/config/default.toml').read_text())['oauth_token']
 def api(path,body=None):
  request=urllib.request.Request('https://api.cloudflare.com/client/v4/'+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
  with urllib.request.urlopen(request,timeout=15) as f:return json.load(f)
 account=api('accounts')['result'][0]['id']
 field='workersInvocationsAdaptive(limit:10,filter:{scriptName:%s,datetime_geq:%s,datetime_lt:%s}){dimensions{status} sum{requests errors}}'
 stamp=lambda t:datetime.datetime.fromtimestamp(t,datetime.timezone.utc).isoformat()
 q='{viewer{accounts(filter:{accountTag:'+json.dumps(account)+'}){ public:'+field%(json.dumps('typeahead'),json.dumps(stamp(first)),json.dumps(stamp(last)))+' transport:'+field%(json.dumps('typeahead-search-transport'),json.dumps(stamp(first)),json.dumps(stamp(last)))+'}}}'
 result=api('graphql',{'query':q})
 if result.get('errors'):raise RuntimeError('provider analytics unavailable')
 accounts=result.get('data',{}).get('viewer',{}).get('accounts',[])
 if len(accounts)!=1:raise RuntimeError('provider account coverage missing')
 data=accounts[0];record['provider'].append({'start':stamp(first),'end':stamp(last),'data':data})
 for role in ['public','transport']:
  if not data.get(role) or sum(r['sum']['requests'] for r in data[role])==0:raise RuntimeError('provider invocation coverage missing')
  if any(r['sum']['errors'] for r in data[role]):raise RuntimeError('provider runtime error gate')
 return data
try:
 if deployment()['versions']!=[{'version_id':OLD,'percentage':100}]:raise RuntimeError('transport changed before qualification')
 health();provider(time.time()-1200,time.time()-300)
 message='Typeahead performance: bounded provider-metrics qualification '+record['started_at'];record['message']=message;changed=True;deploy(NEW,message)
 live=deployment()
 if live['versions']!=[{'version_id':NEW,'percentage':100}] or live.get('annotations',{}).get('workers/message')!=message:raise RuntimeError('deployment ownership/readback gate')
 owned=live['id'];record['deployment_id']=owned;probe();health()
 with (root/'smoke.log').open('wb') as log:smoke=subprocess.Popen(['sh','-c','set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; TYPEAHEAD_SECRET="$ADMIN_SECRET"; set +a; just smoke'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 start=time.time();next_health=start+60;next_provider=start+300;record['status']='observing';save();print('bounded metrics qualification active',flush=True)
 while time.time()-start<1980:
  if smoke.poll() is not None and smoke.returncode:raise RuntimeError('smoke gate')
  if smoke.poll()==0 and 'first_bucket' not in record:record['first_bucket']=int(time.time())//300+1;record['smoke_exit_code']=0;save()
  if time.time()>=next_health:
   if deployment()['id']!=owned:raise RuntimeError('deployment ownership changed')
   health();next_health+=60;save()
  if time.time()>=next_provider:provider(start,time.time()-180);next_provider+=300;save()
  time.sleep(.5)
 if smoke.poll() is None:raise RuntimeError('smoke unfinished')
 probe();health();provider(start,start+1800)
 end_bucket=int((start+1800)//300)
 helper=Path('docs/evals/perf-goal-20261027/transport-overlap/shared-hint/counter-snapshot.py')
 cmd='set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; set +a; python3 '+str(helper)+' '+str(root/'public-window.json')+' '+str(record['first_bucket'])+' '+str(end_bucket)
 subprocess.run(['sh','-c',cmd],stdout=subprocess.DEVNULL,check=True,timeout=30)
 window=json.loads((root/'public-window.json').read_text());metrics=window['metrics'][0];misses=int(metrics['searches'] or 0);hits=int(metrics['cache_hits'] or 0);statuses={int(r['status']):int(r['hits']) for r in window['request_status']}
 if any(n and s>=500 for s,n in statuses.items()):raise RuntimeError('public HTTP error gate')
 if statuses.get(200,0)+statuses.get(304,0)!=misses+hits:raise RuntimeError('public answers not fully reconciled with latency counters')
 if misses<100:raise RuntimeError('insufficient natural cache misses')
 mean=float(metrics['total_ms'])/misses;record['public_window']={'misses':misses,'cache_hits':hits,'mean_ms':mean,'status_counts':statuses,'start_bucket':record['first_bucket'],'end_bucket_exclusive':end_bucket}
 if mean>185:raise RuntimeError('public mean did not meet 185ms gate')
 record['status']='passed-bounded-public-qualification';success=True;save();print(json.dumps(record['public_window']),flush=True)
except BaseException as exc:record['status']='gate-failed';record['error']=type(exc).__name__+': '+str(exc);save();print(record['error'],flush=True)
finally:
 if changed and not success:
  try:
   live=deployment();ours=live['id']==owned if owned else live.get('annotations',{}).get('workers/message')==record.get('message')
   if ours:
    deploy(OLD,'Restore retained transport after bounded performance gate')
    if deployment()['versions']!=[{'version_id':OLD,'percentage':100}]:raise RuntimeError('rollback unconfirmed')
    record['status']='rolled-back'
   else:record['rollback_skipped']='deployment ownership changed'
  except BaseException as exc:record['rollback_error']=type(exc).__name__+': '+str(exc)
 if smoke and smoke.poll() is None:
  os.killpg(smoke.pid,signal.SIGTERM)
  try:smoke.wait(timeout=5)
  except subprocess.TimeoutExpired:os.killpg(smoke.pid,signal.SIGKILL);smoke.wait()
 record['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
