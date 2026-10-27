import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
from mps.typeahead_refresh import Refresh

APP='typeahead-startup-20261024'
MID='080d415b734708'
VOLUME='vol_4915xomx28k0ml5r'
IMAGE='registry.fly.io/typeahead-search-refresh@sha256:b0492f07c8237d94c854da8530b3af5eab905506e69ff0104fa1ba5d98449bcd'
ROOT=Path('docs/evals/sync-hardening-release-20261026')
BACKUP=Path('/tmp/typeahead-ram-config-backup.json')
STORE='/Users/nate/tangled.org/zzstoatzz.io/secrets/prod.yaml'

def interrupt(_signum,_frame):
    raise KeyboardInterrupt()

signal.signal(signal.SIGTERM,interrupt)
signal.signal(signal.SIGINT,interrupt)

def remote(script):
    result=subprocess.run(['fly','ssh','console','--quiet','-a',APP,'--machine',MID,'-C','python3 -c '+shlex.quote(script)],capture_output=True,text=True,timeout=150)
    if result.returncode:
        raise RuntimeError('remote probe failed; code='+str(result.returncode))
    return json.loads(result.stdout)

POLICY='''import sqlite3,hashlib,json
c=sqlite3.connect("file:/data/refresh/overlay.db?mode=ro",uri=True)
a=c.execute("SELECT value FROM sync_meta WHERE key='pds_scope_policy'").fetchone()
s=c.execute("SELECT value FROM sync_meta WHERE key='last_sync'").fetchone()
c.close()
c=sqlite3.connect("file:/data/refresh/index/generations/b1792908655-14d5-px05/index.db?mode=ro",uri=True)
b=c.execute("SELECT value FROM index_meta WHERE key='pds_scope_policy'").fetchone();c.close()
print(json.dumps({"ready":bool(a and b and a[0]==b[0]),"policy_sha256":hashlib.sha256(b[0].encode()).hexdigest() if b else None,"last_sync":s[0] if s else None}))'''

for env,name in {'INDEX_R2_ENDPOINT':'index-r2-endpoint','INDEX_R2_BUCKET':'index-r2-bucket','INDEX_R2_ACCESS_KEY_ID':'index-r2-access-key-id','INDEX_R2_SECRET_ACCESS_KEY':'index-r2-secret-access-key','FLY_PRIMARY_TOKEN':'typeahead-startup-fly-deploy-token','FLY_REFRESH_TOKEN':'typeahead-refresh-fly-deploy-token'}.items():
    os.environ[env]=subprocess.check_output(['sops','-d','--extract',f'["prefect"]["blocks"]["{name}"]',STORE]).decode().rstrip('\n')
os.environ['INDEX_REFRESH_IMAGE']=IMAGE
service=Refresh(logging.getLogger('ram-comparison'))
route,etag=service.route()
assert route['current']['origin'] != 'https://'+APP+'.fly.dev'
before=service.fly('GET',APP,'machines/'+MID)
base=copy.deepcopy(before['config'])
assert base['image']==IMAGE
assert base['metadata']['owner']=='typeahead-snapshot-refresh'
assert [m['volume'] for m in base['mounts']]==[VOLUME]
policy=remote(POLICY)
assert policy['ready'], 'clean PDS replay is not persisted'
service.healthy({'origin':'https://'+APP+'.fly.dev','build_id':'b1792908655-14d5-px05','source_watermark':1792908646})
with os.fdopen(os.open(BACKUP,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
    json.dump(base,f)
report={'image':IMAGE,'policy_before':policy,'arms':[],'restored':False}

def restart(config):
    service.serving_guard(route,etag,APP)
    current=service.fly('GET',APP,'machines/'+MID)
    if current['state']!='stopped':
        try: service.fly('POST',APP,'machines/'+MID+'/stop',{'signal':'SIGTERM','timeout':60})
        except Exception: pass
    stopped=service.wait_machine(APP,MID,'stopped')
    service.serving_guard(route,etag,APP)
    try: service.fly('POST',APP,'machines/'+MID,{'config':config,'skip_launch':True,'current_version':stopped['instance_id']})
    except Exception: pass
    service.wait_machine(APP,MID,'stopped',guest=config['guest'])
    readback=service.fly('GET',APP,'machines/'+MID)
    assert readback['config']==config
    service.serving_guard(route,etag,APP)
    start_at=int(time.time())
    try: service.fly('POST',APP,'machines/'+MID+'/start')
    except Exception: pass
    started=service.wait_machine(APP,MID,'started',guest=config['guest'])
    deadline=time.monotonic()+120
    while True:
        try:
            code,health,_=service.get('https://'+APP+'.fly.dev','/health')
            if code==200 and health.get('ready'): break
        except Exception: pass
        assert time.monotonic()<deadline,'candidate did not start'
        time.sleep(2)
    return started,start_at

try:
    for name,mb in [('1g-first',1024),('2g',2048),('1g-repeat',1024)]:
        config=copy.deepcopy(base)
        config['guest']={'cpu_kind':'shared','cpus':2,'memory_mb':mb}
        machine,started_at=restart(config)
        quiet=remote('''import sqlite3,time,json
start=time.monotonic();prev=None;stable=time.monotonic()
while time.monotonic()-start<90:
 c=sqlite3.connect("file:/data/refresh/overlay.db?mode=ro",uri=True); r=c.execute("SELECT value FROM sync_meta WHERE key='last_sync'").fetchone();c.close()
 value=int(r[0]) if r else 0
 if value!=prev:stable=time.monotonic();prev=value
 if value>='''+str(started_at-2)+''' and time.monotonic()-stable>=5:
  print(json.dumps({"last_sync":value,"stable_s":time.monotonic()-stable}));break
 time.sleep(0.5)
else:raise RuntimeError("startup sync did not finish")''')
        result=remote((ROOT/'ram-workload.py').read_text())
        result.update(arm=name,instance_id=machine['instance_id'],guest=config['guest'],policy=remote(POLICY),quiet_window=quiet)
        assert result['policy']['ready']
        report['arms'].append(result)
        (ROOT/('ram-'+name+'.json')).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'arm':name,'complete':result['complete'],'samples':len(result['samples']),'elapsed_s':result['elapsed_s'],'summary':result['summary']}),flush=True)
    signatures=[]
    for arm in report['arms']:
        signatures.append({(s['round'],s['query']):s['sha256'] for s in arm['samples']})
    common=set(signatures[0]) & set(signatures[1]) & set(signatures[2])
    report['common_samples']=len(common)
    report['same_query_coverage']=set(signatures[0])==set(signatures[1])==set(signatures[2])
    identical={key for key in common if signatures[0][key]==signatures[1][key]==signatures[2][key]}
    report['identical_common_samples']=len(identical)
    report['identical_responses']=bool(common) and len(identical)==len(common)
    report['sync_quiet_in_all_arms']=all(arm.get('sync_quiet') for arm in report['arms'])
finally:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    restore=copy.deepcopy(base)
    restore['guest']={'cpu_kind':'shared','cpus':2,'memory_mb':1024}
    machine,_=restart(restore)
    assert service.fly('GET',APP,'machines/'+MID)['config']==restore
    report['restored']=True
    report['restored_guest']=restore['guest']
    report['policy_after']=remote(POLICY)
    report['serving_route_unchanged']=service.route()==(route,etag)
    (ROOT/'ram-comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    BACKUP.unlink()
    service.close()
    print(json.dumps({'restored':True,'guest':restore['guest'],'identical_responses':report.get('identical_responses')}),flush=True)
