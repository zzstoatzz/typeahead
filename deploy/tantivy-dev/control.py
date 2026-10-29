import base64,json,os,secrets,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
APP='typeahead-tantivy-dev'
LEDGER=ROOT/'resources.json'
AUTH=Path('/tmp/typeahead-tantivy-dev-control.private.json')
PROTECTED={'8e3d79b70dd748','801539a6551778','7845636a3d0ee8'}

def credential():
 if AUTH.exists():
  d=json.loads(AUTH.read_text())
  if d['app']!=APP or AUTH.stat().st_mode&0o077:raise RuntimeError('unsafe dev credential file')
  return d['token']
 name='codex-tantivy-dev-'+secrets.token_hex(6)
 d=json.loads(subprocess.check_output(['fly','tokens','create','deploy','-a',APP,'--expiry','4h','--name',name,'--json'],text=True))
 token=d.get('token') if isinstance(d,dict) else d
 with open(AUTH,'x',opener=lambda p,f:os.open(p,f,0o600)) as out:json.dump({'app':APP,'name':name,'token':token},out)
 return token

def api(method,path,body=None):
 if path.startswith('/') or '..' in path:raise ValueError('invalid dev API path')
 code="let s='';for await(const b of process.stdin)s+=b;const p=JSON.parse(s);const r=await fetch(p.url,{method:p.method,headers:{'Content-Type':'application/json',Authorization:'Bearer '+p.token},...(p.body===null?{}:{body:JSON.stringify(p.body)}),signal:AbortSignal.timeout(60000)});const raw=await r.text();console.log(JSON.stringify({status:r.status,raw}));"
 result=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps({'url':'https://api.machines.dev/v1/apps/'+APP+'/'+path,'method':method,'body':body,'token':credential()}),capture_output=True,text=True,timeout=65)
 if result.returncode:raise RuntimeError('dev control transport failed')
 d=json.loads(result.stdout)
 if d['status']>=400:raise RuntimeError('dev API HTTP '+str(d['status']))
 return json.loads(d['raw']) if d['raw'] and method!='DELETE' else None

def ledger():return json.loads(LEDGER.read_text()) if LEDGER.exists() else {'app':APP,'owner':'tantivy-dev-20261028'}
def save(d):LEDGER.write_text(json.dumps(d,indent=2)+'\n')
def owned(mid):
 if mid in PROTECTED:raise RuntimeError('protected machine mutation rejected')
 d=api('GET','machines/'+mid)
 if d['config'].get('metadata',{}).get('owner')!='tantivy-dev-20261028':raise RuntimeError('dev machine ownership differs')
 return d

def ensure_started(mid):
 deadline=time.monotonic()+60;requested=False
 while time.monotonic()<deadline:
  machine=owned(mid);state=machine['state']
  if state=='started':return
  if state=='stopped':
   if requested:raise RuntimeError('dev machine stopped after explicit start; inspect failure before retry')
   api('POST','machines/'+mid+'/start');requested=True
  elif state not in ['created','starting','stopping','replacing','updating']:raise RuntimeError('unexpected dev machine state '+state)
  time.sleep(2)
 raise TimeoutError('dev machine startup observation deadline; inspect same machine')

def publication_ready(mid):
 code='''import json,urllib.request
from pathlib import Path
manifest=json.loads(Path("/app/source-manifest.json").read_text())
admitted=json.loads(Path("/data/admitted.json").read_text())
with urllib.request.urlopen("http://127.0.0.1:8080/index/status",timeout=5) as response:status=json.load(response)
with urllib.request.urlopen("http://127.0.0.1:8080/ready",timeout=5) as response:ready=json.load(response)
assert ready.get("ready") and status.get("ready")
assert status["sync_lag_s"]<=120
assert status.get("full_source") and status.get("import_complete") and admitted.get("complete")
assert admitted["actor_count"]==manifest["actor_count"]==status["input_actors"]
assert admitted["build_id"]==manifest["active_build"]==status["source_build_id"]
print("DEV_PUBLISH_GUARD_OK")
'''
 encoded=base64.b64encode(code.encode()).decode()
 command="python3 -c 'import base64;exec(base64.b64decode(\""+encoded+"\"))'"
 result=subprocess.run(['fly','machine','exec',mid,'-a',APP,command],env={**os.environ,'FLY_API_TOKEN':credential()},stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30)
 if result.returncode or result.stderr or 'DEV_PUBLISH_GUARD_OK' not in result.stdout:
  raise RuntimeError('dev publication requires admitted full source and fresh readiness; inspect same machine')

def uncordon_ready(mid):
 machine=owned(mid)
 if machine['config']['guest']!={'cpu_kind':'shared','cpus':2,'memory_mb':1024} or not machine['config'].get('services'):
  raise RuntimeError('public dev routing requires final serving shape and services')
 publication_ready(mid)
 if machine.get('cordoned'):api('POST','machines/'+mid+'/uncordon')
 if owned(mid).get('cordoned'):raise RuntimeError('owned dev machine remains cordoned')

def provision_volume():
 d=ledger()
 if d.get('volume_id'):return d
 existing=api('GET','volumes')
 if existing:raise RuntimeError('unrecorded dev volumes require reconciliation')
 v=api('POST','volumes',{'name':'tantivy_dev_data','region':'ewr','size_gb':40,'encrypted':True,'auto_backup_enabled':False,'compute':{'cpu_kind':'performance','cpus':2,'memory_mb':4096}})
 d['volume_id']=v['id'];save(d);return d

def create_pilot(image):
 if '@sha256:' not in image or not image.startswith('registry.fly.io/'+APP):raise RuntimeError('immutable app-scoped image required')
 d=provision_volume()
 if d.get('machine_id'):raise RuntimeError('dev machine already recorded')
 if api('GET','machines'):raise RuntimeError('unrecorded dev machine requires reconciliation')
 m=api('POST','machines',{'name':'tantivy-dev','region':'ewr','skip_service_registration':True,'config':{'image':image,'guest':{'cpu_kind':'shared','cpus':2,'memory_mb':1024},'mounts':[{'volume':d['volume_id'],'path':'/data'}],'services':[],'init':{'exec':['sleep','1800']},'restart':{'policy':'no'},'stop_config':{'signal':'SIGTERM','timeout':20000000000},'metadata':{'owner':'tantivy-dev-20261028','role':'dev-pilot'}}})
 d.update(machine_id=m['id'],image=image,phase='pilot-created');save(d);return d

def configure(stage):
 d=ledger();m=owned(d['machine_id']);c=m['config']
 if stage=='publish':publication_ready(m['id'])
 c['init']={'exec':['python3','/app/bootstrap.py']};c['metadata']['role']='tantivy-dev'
 c['env']={**c.get('env',{}),'BOOTSTRAP_DEADLINE_SECONDS':'5400'}
 c['guest']={'cpu_kind':'performance','cpus':2,'memory_mb':4096} if stage=='bootstrap' else {'cpu_kind':'shared','cpus':2,'memory_mb':1024}
 c['restart']={'policy':'no'} if stage=='bootstrap' else {'policy':'on-failure','max_retries':3}
 c['services']=[] if stage=='bootstrap' else [{'protocol':'tcp','internal_port':8080,'autostop':False,'autostart':False,'ports':[{'port':80,'handlers':['http'],'force_https':True},{'port':443,'handlers':['tls','http']}]}]
 api('POST','machines/'+m['id'],{'config':c,'skip_service_registration':stage=='bootstrap'})
 ensure_started(m['id'])
 if stage=='publish':uncordon_ready(m['id'])
 d['phase']=stage;save(d);return d

def update_image(image):
 if not image.startswith('registry.fly.io/'+APP+'@sha256:'):raise RuntimeError('immutable dev image required')
 d=ledger();m=owned(d['machine_id']);c=m['config'];c['image']=image
 if c.get('services'):raise RuntimeError('pilot image update requires no registered services')
 api('POST','machines/'+m['id'],{'config':c,'skip_service_registration':True})
 ensure_started(m['id'])
 d.update(image=image,phase='pilot-image-updated');save(d);return d

def rollout_dev(image):
 if not image.startswith('registry.fly.io/'+APP+'@sha256:'):raise RuntimeError('immutable dev image required')
 d=ledger();m=owned(d['machine_id']);c=m['config']
 if c['image']!=d['image']:raise RuntimeError('recorded dev image differs; reconcile before update')
 if c['guest']!={'cpu_kind':'shared','cpus':2,'memory_mb':1024}:raise RuntimeError('dev serving budget differs')
 mounts=c.get('mounts') or []
 if not c.get('services') or len(mounts)!=1 or mounts[0].get('volume')!=d['volume_id'] or mounts[0].get('path')!='/data':raise RuntimeError('dev service or owned volume differs')
 c['image']=image
 api('POST','machines/'+m['id'],{'config':c,'skip_service_registration':False})
 d.update(image=image,phase='readiness-repair-started');save(d)
 ensure_started(m['id'])
 return d

if __name__=='__main__':
 import sys
 if sys.argv[1]=='create-pilot':d=create_pilot(sys.argv[2])
 elif sys.argv[1]=='update-image':d=update_image(sys.argv[2])
 elif sys.argv[1]=='rollout-dev':d=rollout_dev(sys.argv[2])
 elif sys.argv[1] in ['bootstrap','publish']:d=configure(sys.argv[1])
 elif sys.argv[1]=='status':d=owned(ledger()['machine_id']);d={k:d.get(k) for k in ['id','state','instance_id']}
 else:raise RuntimeError('unknown dev control operation')
 print(json.dumps(d))
