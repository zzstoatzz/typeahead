import datetime,json,os,selectors,signal,subprocess,time,urllib.request
from pathlib import Path
import sys
sys.path.insert(0,str(Path("scripts/perf").resolve()))
from tail_observer import TailObserver, WORKERS
OLD='e8c21659-0b2c-4cec-beb4-86b4ecf91191'; NEW='40a014e4-5ec0-467d-a8e7-214b626436ce'
root=Path('docs/evals/perf-goal-20261027/transport-overlap/shared-hint/canary100-overlap-observers'); root.mkdir(exist_ok=False); output=root/'canary-100pct.json'
if output.exists():raise RuntimeError('preserve previous canary evidence')
record={'public_masked_failures':0,'public_status':{},'public_outcomes':{},'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'old':OLD,'candidate':NEW,'candidate_percentage':100,'generated_search_requests':0,'samples':[],'health':[],'outcomes':{},'status':'preparing'}
observer=None;tail=None;public_tail=None;smoke=None;changed=False;success=False;buffer='';public_buffer='';owned_deployment=None;decoder=json.JSONDecoder();started=time.monotonic();next_health=0;transport_buffer=''

def counters(name,first=None,last=None):
 first=first if first is not None else record.setdefault('counter_first_bucket',int(time.time())//300)
 helper=Path('docs/evals/perf-goal-20261027/transport-overlap/shared-hint/counter-snapshot.py')
 command='set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; set +a; python3 '+str(helper)+' '+str(root/name)+' '+str(first)+((' '+str(last)) if last is not None else '')
 subprocess.run(['sh','-c',command],check=True,timeout=30,stdout=subprocess.DEVNULL)

def save(): output.write_text(json.dumps(record,indent=2)+'\n')
def deploy(spec,message):
 with (root/('canary-rollback.log' if spec==[OLD+'@100%'] else 'canary-deploy.log')).open('ab') as log:
  subprocess.run(['node_modules/.bin/wrangler','versions','deploy',*spec,'-c','wrangler.search-transport.jsonc','--yes','--message',message],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
def deployment():
 return max(json.loads(subprocess.check_output(['node_modules/.bin/wrangler','deployments','list','-c','wrangler.search-transport.jsonc','--json'],timeout=60)),key=lambda d:d['created_on'])
def health():
 request=urllib.request.Request('https://typeahead.waow.tech/health/freshness',headers={'User-Agent':'typeahead-perf/1.0'})
 with urllib.request.urlopen(request,timeout=15) as response:data=json.load(response)
 record['health'].append({'elapsed_s':round(time.monotonic()-started,1),**data})
 if not data.get('ok') or data.get('build_id')!='b1793081565-0228-px05':raise RuntimeError('public freshness/build gate failed')
try:
 current=json.loads(subprocess.check_output(['node_modules/.bin/wrangler','deployments','list','-c','wrangler.search-transport.jsonc','--json'],timeout=60))
 if max(current,key=lambda d:d['created_on'])['versions'] != [{'version_id':OLD,'percentage':100}]:raise RuntimeError('live transport changed')
 health()
 observer=TailObserver()
 for role in WORKERS:observer.add(role,0)
 observer.wait_ready(1);observer.armed=True
 record['observer_stdin_held_open']=True;record['observer_wrangler']='4.153.0';record['observer_strategy']='overlap across deployment and replacement, fail on total coverage loss'
 counters('counters-before.json')
 changed=True
 deployment_message='Typeahead perf shared-hint '+record['started_at'];record['deployment_message']=deployment_message
 deploy([NEW+'@100%'],deployment_message)
 latest=deployment()
 if sorted((v['version_id'],v['percentage']) for v in latest['versions']) != sorted([(NEW,100)]) or latest.get('annotations',{}).get('workers/message')!=deployment_message:raise RuntimeError('canary deployment ownership/readback mismatch')
 owned_deployment=latest['id'];record['deployment_id']=owned_deployment
 for role in WORKERS:observer.add(role,1)
 observer.wait_ready(2)
 record['status']='observing';save();print('100% guarded observation active',flush=True)
 smoke_log=(root/'canary-smoke.log').open('wb')
 smoke=subprocess.Popen(['sh','-c','set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; TYPEAHEAD_SECRET="$ADMIN_SECRET"; set +a; just smoke'],stdout=smoke_log,stderr=subprocess.STDOUT,start_new_session=True)
 smoke_log.close()
 started=time.monotonic();next_health=60
 while time.monotonic()-started<1800:
  if smoke.poll() is not None and smoke.returncode:raise RuntimeError('smoke correctness gate failed')
  if smoke.poll()==0 and not record.get('steady_counter_started'):
   counters('counters-steady-before.json');record['steady_counter_started']=datetime.datetime.now(datetime.timezone.utc).isoformat();record['qualified_start_bucket']=int(time.time())//300+1;save()
  if time.monotonic()-started>=next_health:
   if deployment()['id']!=owned_deployment:raise RuntimeError('transport ownership changed')
   health();next_health+=60;save()
  for role,entry in observer.pump(.5):
   public_stream=role=='public'
   event=entry.get('event') or {};request=event.get('request') or {}
   path=urllib.parse.urlsplit(request.get('url','')).path
   if public_stream:
    if path not in ['/xrpc/tech.waow.typeahead.searchActors','/xrpc/app.bsky.actor.searchActorsTypeahead']:continue
    status=(event.get('response') or {}).get('status');record['public_status'][str(status)]=record['public_status'].get(str(status),0)+1
    if isinstance(status,int) and status>=500:raise RuntimeError('public HTTP failure')
    outcome=entry.get('outcome','unknown');record['public_outcomes'][outcome]=record['public_outcomes'].get(outcome,0)+1
    if entry.get('exceptions') or outcome in ['exception','exceededCpu','exceededMemory','unknown']:raise RuntimeError('public runtime failure')
    for log in entry.get('logs',[]):
     for message in log.get('message',[]):
      if not isinstance(message,str):continue
      try:event_name=json.loads(message).get('event')
      except (ValueError,AttributeError):continue
      if event_name=='search_backend_unreachable':
       record['public_masked_failures']+=1;raise RuntimeError('public search masked backend failure')
    continue
   if path!='/search':continue
   outcome=entry.get('outcome','unknown');record['outcomes'][outcome]=record['outcomes'].get(outcome,0)+1
   if entry.get('exceptions') or outcome in ['exception','exceededCpu','exceededMemory','unknown']:raise RuntimeError('transport runtime failure')
   for log in entry.get('logs',[]):
    for message in log.get('message',[]):
     if not isinstance(message,str):continue
     try:sample=json.loads(message)
     except (ValueError,TypeError):continue
     if not isinstance(sample,dict) or sample.get('event')!='search_transport_timing':continue
     clean={k:sample[k] for k in ['status','mode','route_ms','origin_headers_ms','total_headers_ms','outcome','sample_rate','hint_source','hint_wait_ms'] if k in sample}
     record['samples'].append(clean)
     if clean.get('status',500)>=500:raise RuntimeError('candidate backend/route error')

 if smoke.poll() is None:raise RuntimeError('smoke exceeded canary window')
 record['smoke_exit_code']=smoke.returncode
 record['limitations']=['small passive cohort cannot establish public latency percentiles or causal population gain','native and public goal gates remain separate']
 health()
 overlap=[s for s in record['samples'] if s.get('mode')=='overlap' and s.get('hint_source')=='shared']
 concurrent=sorted(max(0,s['route_ms']+s['origin_headers_ms']-s['total_headers_ms']) for s in overlap)
 record['shared_overlaps']=len(overlap);record['concurrent_phase_median_ms']=concurrent[len(concurrent)//2] if concurrent else None
 if len(overlap)<30 or len(record['samples'])<100 or record['concurrent_phase_median_ms']<10:raise RuntimeError('insufficient complete timing coverage for continued canary')
 end_bucket=(int(time.time())-180)//300
 counters('counters-qualified-window.json',record['qualified_start_bucket'],end_bucket)
 window=json.loads((root/'counters-qualified-window.json').read_text());metrics=window['metrics'][0]
 misses=int(metrics['searches'] or 0);hits=int(metrics['cache_hits'] or 0)
 statuses={int(r['status']):int(r['hits']) for r in window['request_status']}
 if any(n and status>=500 for status,n in statuses.items()):raise RuntimeError('public counter contains HTTP failures')
 if statuses.get(200,0)+statuses.get(304,0)!=misses+hits:raise RuntimeError('unreconciled public answers; latency accounting incomplete')
 if misses<100:raise RuntimeError('insufficient post-smoke public misses')
 record['public_counter_window']={'start_bucket':window['first_bucket'],'end_bucket_exclusive':end_bucket,'read_at':window['read_at'],'misses':misses,'cache_hits':hits,'status_counts':statuses,'miss_mean_ms':float(metrics['total_ms'] or 0)/misses,'limitations':['legacy counters are asynchronous and best-effort','window uses complete buckets starting after smoke and ending at least three minutes before readback','does not establish a causal cross-cohort speedup']}
 record['status']='passed-limited-canary';success=True;save();print(json.dumps({'status':record['status'],'samples':len(record['samples']),'overlap':len(overlap)}),flush=True)
except BaseException as exc:
 record['status']='gate-failed';record['error']=type(exc).__name__+': '+str(exc);save();print(record['error'],flush=True)
finally:
 try:
  if changed and not success:
   latest=deployment()
   owns=(latest['id']==owned_deployment) if owned_deployment else latest.get('annotations',{}).get('workers/message')==record.get('deployment_message')
   if owns:
    deploy([OLD+'@100%'],'Restore retained transport version after bounded canary gate')
    if deployment()['versions'] != [{'version_id':OLD,'percentage':100}]:raise RuntimeError('rollback readback mismatch')
    record['candidate_percentage']=0;record['status']='rolled-back'
   elif latest['versions']==[{'version_id':OLD,'percentage':100}]:
    record['candidate_percentage']=0
   else:
    record['rollback_skipped']='another deployment owns the transport';record['status']='ownership-lost';record['candidate_percentage']=None
 except BaseException as exc:
  record['rollback_error']=type(exc).__name__+': '+str(exc);record['status']='rollback-unconfirmed'
 finally:
  import re
  if observer:
   try:record['observer_overlap']=observer.close()
   except BaseException as exc:record['observer_cleanup_error']=type(exc).__name__+': '+str(exc)
  record['observer_exit']={}
  for name,child in [('smoke',smoke),('transport',tail),('public',public_tail)]:
   if not child:continue
   before=child.poll()
   try:os.killpg(child.pid,signal.SIGTERM)
   except ProcessLookupError:pass
   try:child.wait(timeout=5)
   except subprocess.TimeoutExpired:
    os.killpg(child.pid,signal.SIGKILL);child.wait()
   if name!='smoke':
    os.set_blocking(child.stderr.fileno(),False)
    raw=child.stderr.read(65536) or b''
    flags=sorted(set(re.findall(r'ECONNRESET|ETIMEDOUT|EPIPE|1006|1011|401|403|429|authentication|unauthorized|rate limit|websocket|closed|disconnect',raw.decode(errors='replace'),re.IGNORECASE)))
    record['observer_exit'][name]={'exit_before_stop':before,'final_exit':child.returncode,'diagnostic_flags':flags}
  try:counters('counters-after.json')
  except BaseException as exc:record['counter_error']=type(exc).__name__
  record['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
