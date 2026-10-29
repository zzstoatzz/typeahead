"""Bounded allowlisted cleanup, independently hosted in the qualification app.

Manifest schema1: app/owner fixed below; run_id8..16 lowercasehex;
created_after<=created_before<=deadline; mode pilot/full (10/90 minute lease).
Each machines/volumes entry has exact unpredictable name, role, optional id.
Names are tq-<run_id>-... / tq_<run_id>_... . A separate reaper entry has role
reaper and optional id. Machine metadata owner/run_id/role must match exactly.
Pending names are a write-ahead whitelist, not authority over other app assets.

Daemon verifies its own private256MiB/1shared/auto_destroy VM, emits ARMED,
waits independently, then spends at most300s cleaning. API outage may leave
volumes; cleanup_failed is never success. Engine/generator hard timers remain
independent. Controller success cleanup can use --now with creation settled.
Only platform env QUAL_REAPER_CAPABILITY supplies the app-scoped capability.
"""
import argparse
from datetime import datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import time
import urllib.parse

APP='typeahead-qual-20261029'
OWNER='tantivy-qualification-20261029'
PROTECTED_MACHINES={'8e3d79b70dd748','801539a6551778','7845636a3d0ee8','840e95a2153308'}
PROTECTED_VOLUMES={'vol_40o3xy5812d6kkk4','vol_v8eyqk61q9dy9gkv','vol_4oj6mqjpnlgeyqxr'}
REQUEST_TIMEOUT=15
GRACE=300
API_BASE='https://api.machines.dev/v1/apps/'+APP+'/'
ID=re.compile(r'[a-zA-Z0-9_]{1,64}\Z')
ROLE=re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')

class SafeError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)

class APIError(SafeError):pass

def stamp(value):
    if type(value) in (int,float) and math.isfinite(value):return float(value)
    if isinstance(value,str):
        try:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is not None:return parsed.timestamp()
        except ValueError:pass
    raise SafeError('invalid_created_timestamp')

def validate_manifest(value):
    if not isinstance(value,dict) or type(value.get('schema')) is not int or value.get('schema')!=1 or value.get('app')!=APP or value.get('owner')!=OWNER:
        raise SafeError('manifest_scope_mismatch')
    run=value.get('run_id')
    if not isinstance(run,str) or not re.fullmatch('[a-f0-9]{8,16}',run):raise SafeError('invalid_run_id')
    mode=value.get('mode');lease={'pilot':600,'full':5400}.get(mode)
    if lease is None:raise SafeError('invalid_mode')
    for key in ['created_after','created_before','deadline']:
        if type(value.get(key)) not in (int,float) or not math.isfinite(value[key]):raise SafeError('invalid_deadline')
    if not (0<value['created_after']<=value['created_before']<=value['deadline'] and value['deadline']-value['created_after']<=lease+60):
        raise SafeError('creation_or_lease_bounds')
    machines=value.get('machines');volumes=value.get('volumes');reaper=value.get('reaper')
    if not isinstance(machines,list) or len(machines)>8 or not isinstance(volumes,list) or len(volumes)>6 or not isinstance(reaper,dict):
        raise SafeError('manifest_resource_budget')
    names=set();ids=set()
    for kind,entries in [('machine',machines+[reaper]),('volume',volumes)]:
        prefix=('tq-' if kind=='machine' else 'tq_')+run+('-' if kind=='machine' else '_')
        for entry in entries:
            if not isinstance(entry,dict):raise SafeError('invalid_resource_entry')
            name=entry.get('name');role=entry.get('role')
            if not isinstance(name,str) or not name.startswith(prefix) or len(name)>63 or not re.fullmatch('[a-z0-9_-]+',name) or not isinstance(role,str) or not ROLE.fullmatch(role):
                raise SafeError('resource_name_or_role')
            if (kind,name) in names:raise SafeError('duplicate_planned_name')
            names.add((kind,name))
            rid=entry.get('id')
            if rid is not None:
                if not isinstance(rid,str) or not ID.fullmatch(rid):raise SafeError('invalid_resource_id')
                if rid in PROTECTED_MACHINES|PROTECTED_VOLUMES:raise SafeError('protected_resource_id')
                if kind=='volume' and not rid.startswith('vol_'):raise SafeError('invalid_volume_id')
                if rid in ids:raise SafeError('duplicate_resource_id')
                ids.add(rid)
    if reaper['role']!='reaper' or any(entry['role']=='reaper' for entry in machines):raise SafeError('reaper_role_separation')
    return value

class API:
    def __init__(self,capability,base=API_BASE,monotonic=time.monotonic,request_timeout=REQUEST_TIMEOUT):
        url=urllib.parse.urlsplit(base)
        if base!=API_BASE and not (url.scheme=='http' and url.hostname in {'127.0.0.1','localhost','::1'} and url.path=='/v1/apps/'+APP+'/'):
            raise SafeError('api_scope_mismatch')
        if url.username or url.password or url.query or url.fragment:raise SafeError('api_scope_mismatch')
        if not capability:raise SafeError('missing_reaper_capability')
        if not isinstance(capability,str) or len(capability)>16384 or any(not 0x20<=ord(c)<=0x7e for c in capability):raise SafeError('invalid_reaper_capability_shape')
        if not 0<request_timeout<=REQUEST_TIMEOUT:raise SafeError('invalid_api_timeout')
        self.url=url;self.capability=capability;self.monotonic=monotonic;self.end=None;self.request_timeout=request_timeout
    def request(self,method,kind,rid=None,action=None):
        if kind not in {'machines','volumes'} or rid is not None and (not ID.fullmatch(rid) or rid in PROTECTED_MACHINES|PROTECTED_VOLUMES):raise SafeError('api_resource_guard')
        allowed=(method=='GET' and action is None) or (method=='POST' and kind=='machines' and rid is not None and action=='stop') or (method=='DELETE' and rid is not None and action is None)
        if not allowed:raise SafeError('api_method_guard')
        timeout=self.request_timeout if self.end is None else min(self.request_timeout,self.end-self.monotonic())
        if timeout<=0:raise APIError('cleanup_budget_exhausted')
        path=self.url.path+kind+('/'+rid if rid else '')+('/stop' if action else '')
        if method=='DELETE' and kind=='machines':path+='?force=true'
        connection=(http.client.HTTPSConnection if self.url.scheme=='https' else http.client.HTTPConnection)(self.url.hostname,self.url.port,timeout=timeout)
        body=json.dumps({'signal':'SIGTERM','timeout':'5s'}) if action=='stop' else None
        start=self.monotonic()
        try:
            connection.request(method,path,body=body,headers={'Authorization':'Bearer '+self.capability,'Content-Type':'application/json','Connection':'close'})
            transport=connection.sock
            remaining=timeout-(self.monotonic()-start)
            if remaining<=0:raise TimeoutError()
            if transport:transport.settimeout(remaining)
            response=connection.getresponse();data=bytearray()
            while not response.isclosed():
                remaining=timeout-(self.monotonic()-start)
                if remaining<=0:raise TimeoutError()
                if transport:transport.settimeout(remaining)
                chunk=response.read1(65536)
                if not chunk:break
                data.extend(chunk)
                if len(data)>1048576:raise APIError('api_response_budget')
            if response.status==404:return None
            if not 200<=response.status<300:raise APIError('api_http_'+str(response.status))
            if not data:return None
            try:return json.loads(data)
            except (ValueError,UnicodeError):raise APIError('api_invalid_json') from None
        except (OSError,http.client.HTTPException):raise APIError('api_transport_failed') from None
        finally:connection.close()

class Reaper:
    def __init__(self,manifest,api,now=time.time,monotonic=time.monotonic,sleep=time.sleep,emit=None):
        self.manifest=validate_manifest(manifest);self.api=api;self.now=now;self.monotonic=monotonic;self.sleep=sleep
        self.emit=emit or (lambda event:None);self.resolved={};self.seen=set();self.events=[];self.self_verified=None;self.volume_dispositions={}
    def bind(self,kind,expected,actual):
        if not isinstance(actual,dict):raise SafeError('api_resource_shape')
        rid=actual.get('id')
        if not isinstance(rid,str) or not ID.fullmatch(rid) or rid in PROTECTED_MACHINES|PROTECTED_VOLUMES:raise SafeError('protected_or_invalid_api_id')
        if kind=='volume' and not rid.startswith('vol_'):raise SafeError('invalid_api_volume_id')
        if expected.get('id') and rid!=expected['id']:raise SafeError('api_resource_id_mismatch')
        if actual.get('name')!=expected['name']:raise SafeError('resource_name_mismatch')
        if actual.get('app_name',APP)!=APP:raise SafeError('resource_app_mismatch')
        created=stamp(actual.get('created_at'))
        if not self.manifest['created_after']<=created<=self.manifest['created_before']:raise SafeError('resource_creation_mismatch')
        if kind=='machine':
            config=actual.get('config')
            if not isinstance(config,dict):raise SafeError('machine_config_shape')
            metadata=config.get('metadata',{})
            if not isinstance(metadata,dict):raise SafeError('machine_metadata_shape')
            if any(metadata.get(key)!=value for key,value in {'owner':OWNER,'run_id':self.manifest['run_id'],'role':expected['role']}.items()):raise SafeError('machine_owner_mismatch')
            if config.get('services') not in (None,[]):raise SafeError('machine_public_services')
        key=(kind,expected['name'])
        if key in self.resolved and self.resolved[key]!=rid:raise SafeError('resolved_identity_changed')
        if key not in self.resolved:
            self.resolved[key]=rid
            event={'event':'resolved','kind':kind,'id':rid,'name':expected['name'],'role':expected['role'],'run_id':self.manifest['run_id']}
            self.events.append(event);self.emit(event)
        self.seen.add(key)
        return actual
    def resolve(self,kind,expected):
        key=(kind,expected['name']);rid=self.resolved.get(key,expected.get('id'));collection=kind+'s'
        if rid:
            actual=self.api.request('GET',collection,rid)
            if actual is not None:return self.bind(kind,expected,actual)
        rows=self.api.request('GET',collection)
        if not isinstance(rows,list):raise SafeError('api_list_shape')
        matches=[row for row in rows if isinstance(row,dict) and row.get('name')==expected['name']]
        if len(matches)>1:raise SafeError('ambiguous_planned_name')
        return self.bind(kind,expected,matches[0]) if matches else None
    def verify_self(self,machine_id):
        if not ID.fullmatch(machine_id) or machine_id in PROTECTED_MACHINES:raise SafeError('protected_reaper_id')
        expected=self.manifest['reaper']
        if expected.get('id') and expected['id']!=machine_id:raise SafeError('reaper_id_mismatch')
        actual=self.api.request('GET','machines',machine_id)
        if actual is None:raise SafeError('reaper_missing')
        self.bind('machine',expected,actual)
        config=actual['config'];restart=config.get('restart')
        if not isinstance(restart,dict) or config.get('guest')!={'cpu_kind':'shared','cpus':1,'memory_mb':256} or config.get('mounts') not in (None,[]) or config.get('auto_destroy') is not True or restart.get('policy')!='no':raise SafeError('reaper_runtime_shape')
        self.self_verified={'id':machine_id,'name':expected['name'],'auto_destroy':True}
        return self.self_verified
    def clean_machine(self,expected):
        actual=self.resolve('machine',expected)
        if actual is None:return True
        rid=actual['id'];state=actual.get('state')
        if state=='destroyed':return True
        if state not in {'created','started','starting','stopped','stopping','destroyed','destroying','replacing','updating','launch_failed','failed'}:raise SafeError('unexpected_machine_state')
        if state not in {'stopped','destroyed','destroying'}:
            self.api.request('POST','machines',rid,'stop')
            actual=self.api.request('GET','machines',rid)
            if actual is None:return True
            self.bind('machine',expected,actual)
            if actual.get('state')=='destroyed':return True
        self.api.request('DELETE','machines',rid)
        actual=self.api.request('GET','machines',rid)
        if actual is None:return True
        self.bind('machine',expected,actual)
        return actual.get('state')=='destroyed'
    def clean_volume(self,expected):
        actual=self.resolve('volume',expected)
        if actual is None:return True
        if actual.get('state')=='destroyed':return True
        rid=actual['id']
        if actual.get('attached_machine_id') or actual.get('attached_alloc_id'):raise SafeError('volume_still_attached')
        machines=self.api.request('GET','machines')
        if not isinstance(machines,list):raise SafeError('api_list_shape')
        for machine in machines:
            if not isinstance(machine,dict) or not isinstance(machine.get('config'),dict):raise SafeError('api_machine_list_shape')
            mounts=machine['config'].get('mounts') or []
            if not isinstance(mounts,list) or any(not isinstance(mount,dict) for mount in mounts):raise SafeError('api_mount_list_shape')
            if any(mount.get('volume')==rid for mount in mounts):raise SafeError('volume_still_mounted')
        if actual.get('state')=='pending_destroy':
            self.volume_dispositions[rid]={'id':rid,'state':'pending_destroy','non_billable':True,'physical_purge_confirmed':False}
            return True
        if actual.get('state')!='pending_destroy':self.api.request('DELETE','volumes',rid)
        actual=self.api.request('GET','volumes',rid)
        if actual is None:return True
        self.bind('volume',expected,actual)
        if actual.get('state')=='pending_destroy':
            self.volume_dispositions[rid]={'id':rid,'state':'pending_destroy','non_billable':True,'physical_purge_confirmed':False}
            return True
        return actual.get('state')=='destroyed'
    def cleanup(self,creation_settled=False,grace=GRACE):
        if not 0<grace<=GRACE:raise SafeError('invalid_cleanup_budget')
        start=self.now();end=self.monotonic()+grace;self.api.end=end
        last=[];attempts=0;machine_done=set();volume_done=set()
        while self.monotonic()<end:
            attempts+=1;last=[];machine_done=set();volume_done=set()
            for expected in self.manifest['machines']:
                try:
                    if self.clean_machine(expected):machine_done.add(expected['name'])
                except SafeError as error:last.append(self.failure('machine',expected,error.code))
            if len(machine_done)==len(self.manifest['machines']):
                for expected in self.manifest['volumes']:
                    try:
                        if self.clean_volume(expected):volume_done.add(expected['name'])
                    except SafeError as error:last.append(self.failure('volume',expected,error.code))
            unresolved=[(kind,item) for kind,items in [('machine',self.manifest['machines']),('volume',self.manifest['volumes'])] for item in items if (kind,item['name']) not in self.seen]
            complete=len(machine_done)==len(self.manifest['machines']) and len(volume_done)==len(self.manifest['volumes']) and not last
            if complete and (creation_settled or not unresolved):break
            remaining=end-self.monotonic()
            if remaining<=0:break
            self.sleep(min(5,remaining))
        complete=len(machine_done)==len(self.manifest['machines']) and len(volume_done)==len(self.manifest['volumes']) and not last
        pending=[]
        for kind,items,done in [('machine',self.manifest['machines'],machine_done),('volume',self.manifest['volumes'],volume_done)]:
            for expected in items:
                if expected['name'] not in done:pending.append(self.failure(kind,expected,'remaining'))
        return {'schema':1,'app':APP,'owner':OWNER,'run_id':self.manifest['run_id'],'status':'cleaned' if complete else 'cleanup_failed',
                'deadline':self.manifest['deadline'],'started_at':start,'finished_at':self.now(),'attempts':attempts,
                'creation_settled_by_controller':creation_settled,'resolved':self.events,'failures':last,'remaining':pending,
                'remaining_whitelist_ids':sorted({item['id'] for item in pending if item.get('id')}),
                'unresolved_whitelist_names':sorted(item['name'] for item in pending if not item.get('id')),
                'self':self.self_verified,'api_timeout_seconds':REQUEST_TIMEOUT,'cleanup_grace_seconds':grace,
                'volume_dispositions':list(self.volume_dispositions.values()),
                'limitations':['Provider outages may leave volumes; failed cleanup must be reconciled before another run.',
                               'Fly retains non-billable pending_destroy volume records for provider-managed purge; absence of those records is not required or claimed.']}
    def failure(self,kind,expected,code):
        return {'kind':kind,'name':expected['name'],'id':self.resolved.get((kind,expected['name']),expected.get('id')),'class':code}
    def wake_deadline(self):
        remaining=max(0,self.manifest['deadline']-self.now())
        limit={'pilot':600,'full':5400}[self.manifest['mode']]+60
        if remaining>limit:raise SafeError('future_deadline_exceeds_lease')
        return self.monotonic()+remaining
    def wait(self,wake=None):
        if wake is None:wake=self.wake_deadline()
        while self.monotonic()<wake:self.sleep(min(30,wake-self.monotonic()))

def failed_receipt(manifest,reaper,code):
    remaining=[]
    for kind,entries in [('machine',manifest['machines']),('volume',manifest['volumes'])]:
        for entry in entries:remaining.append(reaper.failure(kind,entry,'confirmation_required'))
    return {'schema':1,'app':APP,'owner':OWNER,'run_id':manifest['run_id'],'status':'cleanup_failed',
            'class':code,'resolved':reaper.events,'remaining':remaining,
            'remaining_whitelist_ids':sorted({item['id'] for item in remaining if item.get('id')}),
            'unresolved_whitelist_names':sorted(item['name'] for item in remaining if not item.get('id')),
            'self':reaper.self_verified,'cleanup_grace_seconds':GRACE}

def log(event):
    marker='QUALIFICATION_REAPER_RESOLVED ' if event.get('event')=='resolved' else 'QUALIFICATION_REAPER_RECEIPT '
    print(marker+json.dumps(event,separators=(',',':')),flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--manifest',type=Path,required=True)
    group=parser.add_mutually_exclusive_group(required=True);group.add_argument('--daemon',action='store_true');group.add_argument('--now',action='store_true')
    parser.add_argument('--receipt',type=Path);args=parser.parse_args()
    try:
        manifest_bytes=args.manifest.read_bytes()
        manifest=validate_manifest(json.loads(manifest_bytes))
        api=API(os.environ.get('QUAL_REAPER_CAPABILITY'))
        reaper=Reaper(manifest,api,emit=log)
        if args.daemon:
            if os.environ.get('FLY_APP_NAME')!=APP:raise SafeError('reaper_app_environment')
            machine_id=os.environ.get('FLY_MACHINE_ID','')
            for attempt in range(3):
                try:reaper.verify_self(machine_id);break
                except APIError:
                    if attempt==2:raise
                    time.sleep(2**attempt)
            wake=reaper.wake_deadline()
            if manifest['deadline']>reaper.now():
                print('QUALIFICATION_REAPER_ARMED '+json.dumps({'app':APP,'owner':OWNER,'run_id':manifest['run_id'],
                      'deadline':manifest['deadline'],'self':reaper.self_verified,'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest()},separators=(',',':')),flush=True)
            reaper.wait(wake)
        def expired(*_):raise SafeError('cleanup_grace_exhausted')
        previous=signal.signal(signal.SIGALRM,expired)
        signal.setitimer(signal.ITIMER_REAL,GRACE)
        try:result=reaper.cleanup(creation_settled=args.now)
        except SafeError as error:result=failed_receipt(manifest,reaper,error.code)
        finally:
            signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
    except SafeError as error:
        result=failed_receipt(manifest,reaper,error.code) if 'reaper' in locals() else {'schema':1,'app':APP,'owner':OWNER,'status':'cleanup_failed','class':error.code}
    except (OSError,ValueError,KeyError,TypeError,OverflowError):
        result=failed_receipt(manifest,reaper,'invalid_reaper_input_or_state') if 'reaper' in locals() else {'schema':1,'app':APP,'owner':OWNER,'status':'cleanup_failed','class':'invalid_reaper_input_or_state'}
    if args.receipt:
        try:
            temporary=args.receipt.with_suffix(args.receipt.suffix+'.part');temporary.write_text(json.dumps(result,indent=2)+'\n');temporary.replace(args.receipt)
        except OSError:result['local_receipt_error']='receipt_write_failed'
    log(result)
    return 0 if result['status']=='cleaned' else 1

if __name__=='__main__':raise SystemExit(main())
