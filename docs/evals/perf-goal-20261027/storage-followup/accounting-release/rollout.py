import json,subprocess,datetime,sys
from pathlib import Path
root=Path('/Users/nate/.codex/worktrees/search-monitoring/typeahead');out=root/'docs/evals/perf-goal-20261027/storage-followup/accounting-release';out.mkdir(exist_ok=False)
old='a0c42973-77c2-4895-9ee0-9207a7a4ffc0';new=sys.argv[1];owner='search-accounting-8557617-20261027'
def run(args,timeout=60):return subprocess.run(args,cwd=root,capture_output=True,text=True,check=True,timeout=timeout).stdout
def current():return max(json.loads(run(['bunx','wrangler','deployments','list','--name','typeahead','--json'])),key=lambda x:x['created_on'])
before=current();assert before['versions']==[{'version_id':old,'percentage':100}],before
state={'source':'8557617ec1fe58db6e951218e94997a0f9fc6394','before':before,'candidate':new,'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
def save(): (out/'state.json').write_text(json.dumps(state,indent=2)+'\n')
save();keep=False
try:
 assert current()['id']==before['id']
 (out/'deploy.log').write_text(run(['bunx','wrangler','versions','deploy',new+'@100','--yes','--message',owner]))
 state['deployment']=current();assert state['deployment']['annotations']['workers/message']==owner;save()
 command='set -a; . /Users/nate/tangled.org/zzstoatzz.io/typeahead/.env; set +a; exec python3 scripts/smoke.py --url https://typeahead.waow.tech'
 with (out/'smoke.log').open('w') as f:subprocess.run(['sh','-c',command],cwd=root,check=True,stdout=f,stderr=subprocess.STDOUT,timeout=240)
 assert current()['id']==state['deployment']['id'];keep=True;state['phase']='smoke-passed';save()
except BaseException as error:state['error']=str(error);save();raise
finally:
 if not keep:
  d=current()
  if d['annotations'].get('workers/message')==owner:
   (out/'rollback.log').write_text(run(['bunx','wrangler','versions','deploy',old+'@100','--yes','--message',owner+' rollback']))
   state['phase']='restored';save()
print(json.dumps({'phase':state['phase'],'candidate':new}),flush=True)
