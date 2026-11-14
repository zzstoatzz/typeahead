import datetime,json,os,selectors,signal,subprocess,time,urllib.request
from pathlib import Path
OLD='e8c21659-0b2c-4cec-beb4-86b4ecf91191'; NEW='7504eb57-d149-41f3-b1c7-ff0fe91813d1'
root=Path('docs/evals/perf-goal-20261027/transport-overlap/full-timing-canary'); root.mkdir(exist_ok=False); output=root/'canary-10pct.json'
if output.exists():raise RuntimeError('preserve previous canary evidence')
record={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'old':OLD,'candidate':NEW,'candidate_percentage':10,'generated_search_requests':0,'samples':[],'health':[],'outcomes':{},'status':'preparing'}
selector=selectors.DefaultSelector();tail=None;smoke=None;changed=False;success=False;buffer='';decoder=json.JSONDecoder();started=time.monotonic();next_health=0

def save(): output.write_text(json.dumps(record,indent=2)+'\n')
def deploy(spec,message):
 with (root/('canary-deploy.log' if len(spec)==2 else 'canary-rollback.log')).open('ab') as log:
  subprocess.run(['node_modules/.bin/wrangler','versions','deploy',*spec,'-c','deploy/search-transport/wrangler.jsonc','--yes','--message',message],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
def health():
 request=urllib.request.Request('https://typeahead.waow.tech/health/freshness',headers={'User-Agent':'typeahead-perf/1.0'})
 with urllib.request.urlopen(request,timeout=15) as response:data=json.load(response)
 record['health'].append({'elapsed_s':round(time.monotonic()-started,1),**data})
 if not data.get('ok') or data.get('build_id')!='b1793081565-0228-px05':raise RuntimeError('public freshness/build gate failed')
try:
 current=json.loads(subprocess.check_output(['node_modules/.bin/wrangler','deployments','list','-c','deploy/search-transport/wrangler.jsonc','--json'],timeout=60))
 if max(current,key=lambda d:d['created_on'])['versions'] != [{'version_id':OLD,'percentage':100}]:raise RuntimeError('live transport changed')
 health()
 tail=subprocess.Popen(['node_modules/.bin/wrangler','tail','typeahead-search-transport','--format','json','--method','GET'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
 selector.register(tail.stdout,selectors.EVENT_READ);time.sleep(3)
 changed=True
 deploy([OLD+'@90%',NEW+'@10%'],'Bounded 10% search routing overlap canary; retain prior version')
 record['status']='observing';save();print('10% canary active',flush=True)
 smoke_log=(root/'canary-smoke.log').open('wb')
 smoke=subprocess.Popen(['sh','-c','set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; TYPEAHEAD_SECRET="$ADMIN_SECRET"; set +a; just smoke'],stdout=smoke_log,stderr=subprocess.STDOUT,start_new_session=True)
 smoke_log.close()
 started=time.monotonic();next_health=60
 while time.monotonic()-started<600:
  if tail.poll() is not None:raise RuntimeError('passive tail stopped')
  if smoke.poll() is not None and smoke.returncode:raise RuntimeError('smoke correctness gate failed')
  if time.monotonic()-started>=next_health:health();next_health+=60;save()
  for key,_ in selector.select(.5):
   chunk=os.read(key.fileobj.fileno(),65536)
   if not chunk:raise RuntimeError('passive tail closed')
   buffer+=chunk.decode(errors='replace')
   while buffer:
    first=buffer.find('{')
    if first<0:buffer='';break
    buffer=buffer[first:]
    try:entry,end=decoder.raw_decode(buffer)
    except json.JSONDecodeError:break
    buffer=buffer[end:]
    event=entry.get('event') or {};request=event.get('request') or {}
    if urllib.parse.urlsplit(request.get('url','')).path!='/search':continue
    outcome=entry.get('outcome','unknown');record['outcomes'][outcome]=record['outcomes'].get(outcome,0)+1
    if entry.get('exceptions') or outcome in ['exception','exceededCpu','exceededMemory','unknown']:raise RuntimeError('transport runtime failure')
    for log in entry.get('logs',[]):
     for message in log.get('message',[]):
      if not isinstance(message,str):continue
      try:sample=json.loads(message)
      except (ValueError,TypeError):continue
      if not isinstance(sample,dict) or sample.get('event')!='search_transport_timing':continue
      clean={k:sample[k] for k in ['status','mode','route_ms','origin_headers_ms','total_headers_ms','outcome','sample_rate'] if k in sample}
      record['samples'].append(clean)
      if clean.get('status',500)>=500:raise RuntimeError('candidate backend/route error')
   if len(buffer)>1048576:raise RuntimeError('tail buffer budget')
 if smoke.poll() is None:raise RuntimeError('smoke exceeded canary window')
 record['smoke_exit_code']=smoke.returncode
 record['limitations']=['small passive cohort cannot establish public latency percentiles or causal population gain','native and public goal gates remain separate']
 health()
 overlap=[s for s in record['samples'] if s.get('mode')=='overlap']
 if len(overlap)<3 or len(record['samples'])<10:raise RuntimeError('insufficient complete timing coverage for continued canary')
 record['status']='passed-limited-canary';success=True;save();print(json.dumps({'status':record['status'],'samples':len(record['samples']),'overlap':len(overlap)}),flush=True)
except BaseException as exc:
 record['status']='rolled-back';record['error']=type(exc).__name__+': '+str(exc);save();print(record['error'],flush=True)
finally:
 if changed and not success:
  deploy([OLD+'@100%'],'Restore retained transport version after bounded canary gate');record['candidate_percentage']=0;save()
 for child in [smoke,tail]:
  if child and child.poll() is None:
   os.killpg(child.pid,signal.SIGTERM)
   try:child.wait(timeout=5)
   except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
 record['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
