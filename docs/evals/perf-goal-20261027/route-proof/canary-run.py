import datetime,json,math,os,signal,subprocess,time
from pathlib import Path

root=Path('/Users/nate/.codex/worktrees/search-monitoring/typeahead')
folder=root/'docs/evals/perf-goal-20261027/route-proof/canary'
folder.mkdir(exist_ok=False)
old='40a014e4-5ec0-467d-a8e7-214b626436ce'
new='b9cedc8d-d677-4d7c-b075-05351ee52a8c'
owner='route-proof-5eeae7e-20261027'
state={'owner':owner,'old':old,'new':new,'source':'5eeae7e','deadline_utc':datetime.datetime.fromtimestamp(time.time()+3600,datetime.timezone.utc).isoformat(),'events':[]}
started=time.monotonic();keep=False;owned=None

def save(phase,**values):
 state.update(phase=phase,**values);(folder/'state.json').write_text(json.dumps(state,indent=2)+'\n');print(json.dumps({'phase':phase,**values}),flush=True)
def run(args,**kwargs):
 return subprocess.run(args,cwd=root,check=True,text=True,capture_output=True,timeout=45,**kwargs).stdout
def current():
 return max(json.loads(run(['bunx','wrangler','deployments','list','--config','wrangler.search-transport.jsonc','--json'])),key=lambda x:x['created_on'])
def deploy(percent):
 global owned
 d=current();assert d['id']==owned,'Deployment ownership changed'
 args=['bunx','wrangler','versions','deploy',new+'@'+str(percent),old+'@'+str(100-percent),'--config','wrangler.search-transport.jsonc','--yes','--message',owner+' '+str(percent)+'%']
 (folder/('deploy-'+str(percent)+'.log')).write_text(run(args))
 d=current();assert d['annotations']['workers/message']==owner+' '+str(percent)+'%';owned=d['id'];state['events'].append(d);save('deployed',percentage=percent,deployment_id=owned)
def health():
 js='const r=await fetch("https://typeahead.waow.tech/health/freshness",{signal:AbortSignal.timeout(10000)});console.log(JSON.stringify({status:r.status,...await r.json()}));'
 d=json.loads(run(['node','--input-type=module','-e',js]));assert d['status']==200 and d['ok'] and not d['stale'] and 0<=d['replica_lag_s']<600 and d['build_id']=='b1793109259-2b36-px05',d
 state['last_health']=d
 if time.monotonic()-started>3600:raise RuntimeError('Qualification deadline')
def wait_until(end):
 next_health=0
 while time.time()<end:
  if time.monotonic()>next_health:health();next_health=time.monotonic()+60;save('observing')
  time.sleep(min(25,max(0,end-time.time())))
def interrupted(*_):raise InterruptedError('Qualification interrupted')
signal.signal(signal.SIGTERM,interrupted)
signal.signal(signal.SIGINT,interrupted)
try:
 d=current();assert d['versions']==[{'version_id':old,'percentage':100}];owned=d['id'];state['before']=d
 for percent in (10,50,100):
  deploy(percent);health()
  command='set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; set +a; exec python3 scripts/smoke.py --url https://typeahead.waow.tech'
  with (folder/('smoke-'+str(percent)+'.log')).open('w') as output:subprocess.run(['sh','-c',command],cwd=root,check=True,stdout=output,stderr=subprocess.STDOUT,timeout=240)
  save('smoke-passed',percentage=percent)
  if percent==10:wait_until(time.time()+60)
  if percent==50:
   first_ab=math.ceil((time.time()+30)/300);last_ab=first_ab+2
   save('paired-natural-window',paired_start_utc=datetime.datetime.fromtimestamp(first_ab*300,datetime.timezone.utc).isoformat(),paired_end_utc=datetime.datetime.fromtimestamp(last_ab*300,datetime.timezone.utc).isoformat())
   wait_until(last_ab*300+120)
   command='set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; set +a; exec node /tmp/typeahead-public-phase-query.mjs "$1" "$2" "$3"'
   run(['sh','-c',command,'query',state['paired_start_utc'],state['paired_end_utc'],str(folder/'paired-public-components.json')])
   components=json.loads((folder/'paired-public-components.json').read_text());assert components['success']
   event_groups=next(c['aggregates'] for c in components['calculations'] if c['alias']=='events')
   assert event_groups and all(next(g['value'] for g in row['groups'] if g['key']=='outcome')=='ok' for row in event_groups),'Observed backend error'
   phases=next(c['aggregates'] for c in components['calculations'] if c['alias']=='search_ms')
   sides={}
   for row in phases:
    groups={g['key']:g['value'] for g in row['groups']};assert groups['backend_route_class']=='routed'
    sides[(groups['backend_strategy'],groups['ingress_colo'])]=row
   matched=[]
   for strategy,colo in sides:
    if strategy=='proof' and ('retained',colo) in sides:
     candidate=sides[(strategy,colo)];reference=sides[('retained',colo)];weight=min(candidate['count'],reference['count']);matched.append({'colo':colo,'weight':weight,'candidate_mean_ms':candidate['value'],'reference_mean_ms':reference['value']})
   weight=sum(x['weight'] for x in matched)
   assert weight>=20,{'insufficient_matched_sample':weight}
   comparison={'matched':matched,'weighted_candidate_mean_ms':sum(x['candidate_mean_ms']*x['weight'] for x in matched)/weight,'weighted_reference_mean_ms':sum(x['reference_mean_ms']*x['weight'] for x in matched)/weight}
   (folder/'paired-comparison.json').write_text(json.dumps(comparison,indent=2)+'\n')
   assert comparison['weighted_candidate_mean_ms']<comparison['weighted_reference_mean_ms'],comparison
   save('paired-gate-passed',comparison=comparison)
 first=math.ceil((time.time()+30)/300);last=first+3
 save('natural-window',first_bucket=first,last_bucket_exclusive=last,window_start_utc=datetime.datetime.fromtimestamp(first*300,datetime.timezone.utc).isoformat(),window_end_utc=datetime.datetime.fromtimestamp(last*300,datetime.timezone.utc).isoformat())
 wait_until(last*300+180)
 helper='set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; set +a; exec python3 docs/evals/perf-goal-20261027/transport-overlap/shared-hint/counter-snapshot.py "$1" "$2" "$3"'
 run(['sh','-c',helper,'counter',str(folder/'settled-counters.json'),str(first),str(last)])
 d=json.loads((folder/'settled-counters.json').read_text());m=d['metrics'][0];errors=sum(r['hits'] for r in d['request_status'] if r['status']!=200);answered=sum(r['hits'] for r in d['request_status'] if r['status']==200)
 result={'misses':m['searches'],'mean_ms':m['total_ms']/m['searches'],'cache_hits':m['cache_hits'],'answered':answered,'errors':errors,'reconciled':answered==m['searches']+m['cache_hits']}
 state['result']=result;assert errors==0 and result['reconciled'] and m['searches']>=100 and result['mean_ms']<=169,result
 health();assert current()['id']==owned;keep=True;save('qualified',result=result)
except BaseException as error:
 save('not-qualified',error=str(error))
finally:
 if not keep:
  try:
   d=current()
   if d['annotations'].get('workers/message','').startswith(owner):
    (folder/'rollback.log').write_text(run(['bunx','wrangler','versions','deploy',old+'@100','--config','wrangler.search-transport.jsonc','--yes','--message',owner+' restore retained transport']))
    save('restored-retained',deployment=current())
   else:save('ownership-changed-no-rollback',deployment=d)
  except Exception as error:save('rollback-needs-attention',error=str(error))
