"""Explicit cloud pilot preparation/provisioning; importing performs no API calls.

prepare pins image/binary hashes and writes all pending names before creation.
launch_reaper is the only first paid guest. provision requires its matching ARMED
receipt and checks its running ownership before every subsequent allocation.
Engines/generator are created paused with auto_destroy false. Their final public
configuration atomically enables auto_destroy before explicit start. A lost update
reply is confirmed by GET, never retried against an auto-destroy guest. Platform supplies ADMIN_SECRET, QUAL_CONTROL_SECRET and
QUAL_REAPER_CAPABILITY; values never enter this module, requests or ledger.
"""
import argparse
import base64
import copy
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import secrets
import time
import urllib.parse

HERE=Path(__file__).resolve().parent

def local_module(name):
    spec=importlib.util.spec_from_file_location('qualification_'+name,HERE/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

controller=local_module('controller')
reaper=local_module('reaper')
APP=reaper.APP
OWNER=reaper.OWNER
ENGINE={'cpu_kind':'shared','cpus':2,'memory_mb':1024}
SMALL={'cpu_kind':'shared','cpus':1,'memory_mb':256}
ENGINE_ROLES={'native-control':'native','tantivy-control':'tantivy'}
SECRETS={'ADMIN_SECRET','QUAL_CONTROL_SECRET','QUAL_REAPER_CAPABILITY'}

class PilotError(Exception):pass

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()
def binary_sha(value):return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None

class ControlAPI:
    def __init__(self,control,monotonic=time.monotonic):self.control=control;self.monotonic=monotonic;self.end=None
    def request(self,method,kind,rid=None,action=None):
        if kind not in {'machines','volumes'} or rid is not None and (not reaper.ID.fullmatch(rid) or rid in reaper.PROTECTED_MACHINES|reaper.PROTECTED_VOLUMES):raise reaper.SafeError('control_resource_guard')
        if self.end is not None and self.monotonic()>=self.end:raise reaper.APIError('cleanup_budget_exhausted')
        path=kind+('/'+rid if rid else '')+('/stop' if action=='stop' else '')
        if method=='DELETE' and kind=='machines':path+='?force=true'
        body={'signal':'SIGTERM','timeout':'5s'} if action=='stop' else None
        try:return self.control.api(method,path,body)
        except RuntimeError as error:
            if str(error)=='qualification API HTTP 404':return None
            raise reaper.APIError('control_api_failed') from None

class Pilot:
    def __init__(self,control=controller,now=time.time,monotonic=time.monotonic,sleep=time.sleep):
        if control.APP!=APP or control.OWNER!=OWNER or set(control.PROTECTED)!=reaper.PROTECTED_MACHINES:raise PilotError('controller_scope_mismatch')
        self.control=control;self.now=now;self.monotonic=monotonic;self.sleep=sleep
    def state(self):
        value=self.control.ledger()
        if value.get('app')!=APP or value.get('owner')!=OWNER:raise PilotError('ledger_scope_mismatch')
        return value
    def save(self,value):self.control.save(value)
    def plan(self):
        value=self.state();pilot=value.get('pilot')
        if not isinstance(pilot,dict):raise PilotError('pilot_not_prepared')
        try:reaper.validate_manifest(pilot['manifest'])
        except reaper.SafeError:raise PilotError('pilot_manifest_invalid') from None
        if pilot['manifest_sha256']!=digest(pilot['manifest']):raise PilotError('writeahead_manifest_changed')
        return value,pilot
    def guard(self,pilot):return reaper.Reaper(pilot['manifest'],ControlAPI(self.control,self.monotonic),now=self.now,monotonic=self.monotonic,sleep=self.sleep)
    def call(self,method,path,body=None):
        try:return self.control.api(method,path,body)
        except RuntimeError:raise PilotError('qualification_api_failed') from None
    def prepare(self,image,native_binary_sha256,tantivy_binary_sha256,seconds=600,region='ewr',run_id=None):
        value=self.state()
        if value.get('machines') or value.get('volumes') or value.get('pilot',{}).get('phase') not in (None,'cleaned'):
            raise PilotError('existing_run_requires_cleanup')
        if not re.fullmatch(re.escape('registry.fly.io/'+APP+'@sha256:')+'[a-f0-9]{64}',image):raise PilotError('immutable_qualification_image_required')
        if not binary_sha(native_binary_sha256) or not binary_sha(tantivy_binary_sha256):raise PilotError('frozen_binary_hashes_required')
        if type(seconds) is not int or not 1<=seconds<=600 or region!='ewr':raise PilotError('pilot_budget_or_region')
        run_id=run_id or secrets.token_hex(6);now=self.now();deadline=now+seconds
        def entry(role,kind='machine'):
            return {'name':('tq-' if kind=='machine' else 'tq_')+run_id+('-' if kind=='machine' else '_')+role,'role':role}
        manifest={'schema':1,'app':APP,'owner':OWNER,'run_id':run_id,'mode':'pilot','created_after':now-30,
                  'created_before':deadline,'deadline':deadline,'machines':[entry('native-control'),entry('tantivy-control'),entry('generator')],
                  'volumes':[entry('native','volume'),entry('tantivy','volume')],'reaper':entry('reaper')}
        try:reaper.validate_manifest(manifest)
        except reaper.SafeError:raise PilotError('invalid_writeahead_plan') from None
        previous=value.get('pilot')
        if previous:
            if previous.get('cleanup_receipt',{}).get('status')!='cleaned' or previous.get('reaper_retired') is not True:
                raise PilotError('prior_cleanup_not_confirmed')
            value.setdefault('retired_pilots',[]).append({
                'manifest':copy.deepcopy(previous['manifest']),'manifest_sha256':previous['manifest_sha256'],
                'resolved':copy.deepcopy(previous['resolved']),
                'terminal_dispositions':copy.deepcopy(previous['cleanup_receipt'].get('volume_dispositions',[]))})
        pilot={'phase':'prepared','image':image,'region':region,'manifest':manifest,'manifest_sha256':digest(manifest),
               'nonce':secrets.token_hex(16),'watermark':int(now)-60,'attempts':{},'resolved':{},'armed':None,
               'native_binary_sha256':native_binary_sha256,'tantivy_binary_sha256':tantivy_binary_sha256,
               'required_platform_secrets':sorted(SECRETS)}
        value.update(pilot=pilot,phase='pilot-prepared');self.save(value)
        return {'phase':'prepared','run_id':run_id,'deadline':deadline,'manifest_sha256':pilot['manifest_sha256']}
    def entry(self,pilot,kind,role):
        entries=pilot['manifest']['volumes'] if kind=='volume' else pilot['manifest']['machines']+[pilot['manifest']['reaper']]
        return next(item for item in entries if item['role']==role)
    def lease(self,pilot,creating=False):
        if pilot['manifest']['deadline']-self.now()<(70 if creating else 1):raise PilotError('insufficient_pilot_lease')
    def retired_terminal(self,kind,actual,machines):
        for retired in self.state().get('retired_pilots',[]):
            manifest=retired.get('manifest')
            if retired.get('manifest_sha256')!=digest(manifest):raise PilotError('retired_manifest_changed')
            try:guardian=reaper.Reaper(manifest,ControlAPI(self.control,self.monotonic))
            except reaper.SafeError:raise PilotError('retired_manifest_invalid') from None
            entries=manifest['volumes'] if kind=='volume' else manifest['machines']+[manifest['reaper']]
            for entry in entries:
                if entry['name']!=actual.get('name') or retired['resolved'].get(kind+':'+entry['role'])!=actual.get('id'):continue
                try:guardian.bind(kind,entry|{'id':actual['id']},actual)
                except reaper.SafeError:raise PilotError('retired_identity_mismatch') from None
                if kind=='machine':return actual.get('state')=='destroyed'
                if actual.get('state') not in {'destroyed','pending_destroy'} or actual.get('attached_machine_id') or actual.get('attached_alloc_id'):return False
                for machine in machines:
                    if machine.get('state')=='destroyed':continue
                    mounts=machine.get('config',{}).get('mounts') or []
                    if any(mount.get('volume')==actual['id'] for mount in mounts):return False
                return True
        return False
    def inventory(self,pilot):
        guard=self.guard(pilot);machines=self.call('GET','machines');volumes=self.call('GET','volumes')
        if not isinstance(machines,list) or not isinstance(volumes,list):raise PilotError('inventory_shape')
        for kind,rows,entries in [('machine',machines,pilot['manifest']['machines']+[pilot['manifest']['reaper']]),('volume',volumes,pilot['manifest']['volumes'])]:
            expected={item['name']:item for item in entries}
            for actual in rows:
                if not isinstance(actual,dict):raise PilotError('inventory_shape')
                if actual.get('name') not in expected:
                    if self.retired_terminal(kind,actual,machines):continue
                    raise PilotError('unknown_app_resource_requires_reconciliation')
                try:guard.bind(kind,expected[actual['name']],actual)
                except reaper.SafeError:raise PilotError('app_resource_identity_mismatch') from None
    def registered(self,pilot,kind,role):return pilot['resolved'].get(kind+':'+role)
    def record(self,kind,entry,actual):
        value,pilot=self.plan();guard=self.guard(pilot)
        try:guard.bind(kind,entry,actual)
        except reaper.SafeError:raise PilotError('creation_identity_mismatch') from None
        rid=actual['id'];key=kind+':'+entry['role']
        old=pilot['resolved'].get(key)
        if old and old!=rid:raise PilotError('resolved_id_changed')
        record={'id':rid,'name':entry['name'],'role':entry['role'],'run_id':pilot['manifest']['run_id']}
        records=value.setdefault('machines' if kind=='machine' else 'volumes',[])
        if not any(item['id']==rid for item in records):records.append(record)
        pilot['resolved'][key]=rid;pilot['attempts'][key]='resolved';self.save(value)
        return actual
    def resolve(self,kind,entry):
        _,pilot=self.plan();expected=copy.deepcopy(entry);rid=self.registered(pilot,kind,entry['role'])
        if rid:expected['id']=rid
        try:actual=self.guard(pilot).resolve(kind,expected)
        except reaper.SafeError:raise PilotError('planned_identity_resolution_failed') from None
        return self.record(kind,entry,actual) if actual is not None else None
    def alive_reaper(self,pilot):
        if not pilot.get('armed'):raise PilotError('reaper_not_armed')
        rid=self.registered(pilot,'machine','reaper')
        try:
            guardian=self.guard(pilot);guardian.verify_self(rid or '')
            actual=guardian.resolve('machine',pilot['manifest']['reaper'])
        except reaper.SafeError:raise PilotError('reaper_ownership_or_runtime_failed') from None
        if actual is None or actual.get('state')!='started':raise PilotError('reaper_not_running')
    def metadata(self,pilot,role):return {'owner':OWNER,'run_id':pilot['manifest']['run_id'],'role':role}
    def env(self,pilot,role):
        values={'QUAL_RUN_ID':pilot['manifest']['run_id'],'QUAL_PILOT_NONCE':pilot['nonce'],
                'QUAL_WATERMARK':str(pilot['watermark']),'QUAL_DEADLINE':str(pilot['manifest']['deadline'])}
        if role in ENGINE_ROLES:values['QUAL_ROLE']=role
        return values
    def hard_command(self,command,reaper_mode=False):
        script="import os,sys,time;d=float(os.environ['QUAL_DEADLINE'])+%d-time.time();sys.exit(0) if d<=0 else None;os.execvp('timeout',['timeout','--signal=TERM','--kill-after=5',str(max(1,int(d)))]+sys.argv[1:])"%(330 if reaper_mode else 0)
        return ['python3','-c',script,*command]
    def machine_payload(self,pilot,role):
        entry=self.entry(pilot,'machine',role);env=self.env(pilot,role);mounts=[]
        if role=='reaper':
            data=base64.b64encode(encoded(pilot['manifest'])).decode();env['QUAL_REAPER_MANIFEST_B64']=data
            script="import base64,os,pathlib,sys;p=pathlib.Path('/tmp/reaper.json');b=base64.b64decode(os.environ['QUAL_REAPER_MANIFEST_B64']);f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(f,b);os.close(f);os.execv(sys.executable,[sys.executable,'/app/input/reaper.py','--daemon','--manifest',str(p)])"
            command=self.hard_command(['python3','-c',script],True)
        else:
            command=self.hard_command(['python3','/app/pilot_bootstrap.py' if role in ENGINE_ROLES else '/app/generator.py'])
            if role in ENGINE_ROLES:
                vid=self.registered(pilot,'volume',ENGINE_ROLES[role])
                if not vid:raise PilotError('engine_volume_unresolved')
                mounts=[{'volume':vid,'path':'/data'}]
        config={'image':pilot['image'],'auto_destroy':role=='reaper','guest':copy.deepcopy(ENGINE if role in ENGINE_ROLES else SMALL),
                'services':[],'mounts':mounts,'restart':{'policy':'no'},'metadata':self.metadata(pilot,role),
                'init':{'exec':command},'env':env,'stop_config':{'signal':'SIGTERM','timeout':5000000000}}
        return {'name':entry['name'],'region':pilot['region'],'skip_service_registration':True,'skip_launch':role!='reaper','config':config}
    def verify_machine(self,pilot,role,actual):
        expected=self.machine_payload(pilot,role)['config'];config=actual.get('config',{})
        for key in ['image','guest']:
            if config.get(key)!=expected[key]:raise PilotError('machine_shape_or_image_mismatch')
        auto=config.get('auto_destroy',False)
        if type(auto) is not bool:raise PilotError('auto_destroy_must_be_explicit')
        if role=='reaper' or role in pilot.get('configured',{}):
            if not auto:raise PilotError('auto_destroy_required')
        elif not auto and actual.get('state') not in {'created','stopped'}:raise PilotError('unconfigured_guest_must_remain_paused')
        mounts=config.get('mounts') or []
        if not isinstance(mounts,list) or any(not isinstance(mount,dict) for mount in mounts):raise PilotError('machine_mount_shape')
        if [{key:mount.get(key) for key in ['volume','path']} for mount in mounts]!=expected['mounts']:raise PilotError('machine_mount_identity_mismatch')
        if config.get('init',{}).get('exec')!=expected['init']['exec']:raise PilotError('guest_hard_timer_or_program_changed')
        if role in ENGINE_ROLES:
            vid=self.registered(pilot,'volume',ENGINE_ROLES[role]);volume=self.call('GET','volumes/'+vid)
            self.verify_volume(pilot,volume)
            try:self.guard(pilot).bind('volume',self.entry(pilot,'volume',ENGINE_ROLES[role]),volume)
            except reaper.SafeError:raise PilotError('engine_volume_identity_mismatch') from None
        if config.get('services') or config.get('restart',{}).get('policy')!='no':raise PilotError('machine_lifecycle_mismatch')
        environment=config.get('env',{})
        if not isinstance(environment,dict) or any(key in SECRETS or key.startswith('FLY_') and key!='FLY_PROCESS_GROUP' for key in environment):raise PilotError('reserved_or_inline_secret_environment')
        if any(environment.get(key)!=value for key,value in self.env(pilot,role).items()):raise PilotError('run_environment_binding_mismatch')
    def verify_volume(self,pilot,actual):
        if not isinstance(actual,dict) or actual.get('size_gb')!=20 or actual.get('encrypted') is not True:raise PilotError('volume_shape_mismatch')
    def create(self,kind,role,payload):
        value,pilot=self.plan();self.lease(pilot,creating=True)
        if role!='reaper':self.alive_reaper(pilot)
        entry=self.entry(pilot,kind,role);key=kind+':'+role
        actual=self.resolve(kind,entry)
        if actual is not None:
            if kind=='machine':self.verify_machine(pilot,role,actual)
            else:self.verify_volume(pilot,actual)
            return actual
        if key in pilot['attempts']:raise PilotError('creation_outcome_unresolved_no_retry')
        value,pilot=self.plan();pilot['attempts'][key]='attempted';pilot.setdefault('creation_request_sha256',{})[key]=digest(payload);self.save(value)
        try:actual=self.call('POST','machines' if kind=='machine' else 'volumes',payload)
        except PilotError:
            actual=self.resolve(kind,entry)
            if actual is None:
                value,pilot=self.plan();pilot['attempts'][key]='unknown';pilot['phase']='creation-unknown';self.save(value)
                raise PilotError('creation_outcome_unknown_reconcile_only') from None
        actual=self.record(kind,entry,actual)
        if kind=='machine':self.verify_machine(pilot,role,actual)
        else:self.verify_volume(pilot,actual)
        return actual
    def launch_reaper(self):
        _,pilot=self.plan();self.inventory(pilot)
        actual=self.create('machine','reaper',self.machine_payload(pilot,'reaper'))
        value,pilot=self.plan();pilot['phase']='reaper-created';value['phase']='pilot-reaper-created';self.save(value)
        return {'id':actual['id'],'name':actual['name'],'phase':pilot['phase'],'manifest_sha256':pilot['manifest_sha256']}
    def record_armed(self,receipt):
        value,pilot=self.plan();expected=pilot['manifest'];rid=self.registered(pilot,'machine','reaper')
        if any(receipt.get(key)!=target for key,target in {'app':APP,'owner':OWNER,'run_id':expected['run_id'],
            'deadline':expected['deadline'],'manifest_sha256':pilot['manifest_sha256']}.items()):raise PilotError('reaper_armed_binding_mismatch')
        if receipt.get('self')!={'id':rid,'name':expected['reaper']['name'],'auto_destroy':True}:raise PilotError('reaper_armed_identity_mismatch')
        pilot['armed']=copy.deepcopy(receipt);self.alive_reaper(pilot);self.lease(pilot)
        pilot['phase']='reaper-armed';self.save(value)
    def provision(self,armed_receipt=None):
        if armed_receipt is not None:self.record_armed(armed_receipt)
        _,pilot=self.plan();self.alive_reaper(pilot);self.inventory(pilot)
        for role in ['native','tantivy']:
            entry=self.entry(pilot,'volume',role)
            self.create('volume',role,{'name':entry['name'],'region':pilot['region'],'size_gb':20,'encrypted':True,
                'auto_backup_enabled':False,'compute':copy.deepcopy(ENGINE)})
        for role in ['native-control','tantivy-control','generator']:
            _,pilot=self.plan();actual=self.create('machine',role,self.machine_payload(pilot,role))
            if actual.get('state') not in {'created','stopped'}:raise PilotError('provision_requires_paused_guests')
        value,pilot=self.plan();pilot['phase']='provisioned-paused';value['phase']='pilot-provisioned-paused';self.save(value)
        return {'phase':pilot['phase'],'machines':copy.deepcopy(value['machines']),'volumes':copy.deepcopy(value['volumes'])}
    def owned_machine(self,role):
        _,pilot=self.plan();actual=self.resolve('machine',self.entry(pilot,'machine',role))
        if actual is None:raise PilotError('recorded_machine_absent')
        self.verify_machine(pilot,role,actual);return actual
    def confirm_configuration(self,role,actual,configuration_sha256,keys):
        value,pilot=self.plan();self.verify_machine(pilot,role,actual)
        if actual.get('state') not in {'created','stopped'} or actual['config'].get('auto_destroy') is not True:
            raise PilotError('final_configuration_not_confirmed')
        observed={key:actual['config']['env'].get(key) for key in keys}
        if digest(observed)!=configuration_sha256:raise PilotError('final_configuration_not_confirmed')
        pilot.setdefault('configured',{})[role]=configuration_sha256
        pilot.setdefault('configuration_keys',{})[role]=list(keys)
        pilot.setdefault('configuration_attempts',{})[role]={'sha256':configuration_sha256,'keys':list(keys),'state':'committed'}
        self.save(value)
        return {'id':actual['id'],'role':role,'configuration_sha256':configuration_sha256,'state':'paused','auto_destroy':True}
    def update_env(self,role,values):
        value,pilot=self.plan();self.lease(pilot);self.alive_reaper(pilot);actual=self.owned_machine(role)
        if role=='reaper' or actual.get('state') not in {'created','stopped'}:raise PilotError('configuration_requires_paused_owned_guest')
        if not isinstance(values,dict) or any(not key.startswith('QUAL_') or key in SECRETS or not isinstance(v,str) for key,v in values.items()):raise PilotError('public_configuration_only')
        desired=digest(values);keys=sorted(values)
        if actual['config'].get('auto_destroy') is True:
            return self.confirm_configuration(role,actual,desired,keys)
        if role in pilot.get('configuration_attempts',{}):raise PilotError('final_configuration_outcome_unresolved_no_retry')
        config=copy.deepcopy(actual['config']);config.setdefault('env',{}).update(values);config['auto_destroy']=True
        value,pilot=self.plan();pilot.setdefault('configuration_attempts',{})[role]={'sha256':desired,'keys':keys,'state':'attempted'};self.save(value)
        try:self.call('POST','machines/'+actual['id'],{'config':config,'skip_launch':True})
        except PilotError:
            value,pilot=self.plan();pilot['configuration_attempts'][role]['state']='unknown';self.save(value)
        until=min(self.monotonic()+30,self.monotonic()+max(0,pilot['manifest']['deadline']-self.now()))
        while True:
            _,current=self.plan()
            observed=self.resolve('machine',self.entry(current,'machine',role))
            if observed is None:raise PilotError('configuration_target_missing')
            if observed.get('state') in {'created','stopped'} and observed['config'].get('auto_destroy') is True:
                return self.confirm_configuration(role,observed,desired,keys)
            if observed.get('state') not in {'created','stopped','updating','replacing','stopping'}:
                raise PilotError('unexpected_configuration_transition')
            if self.monotonic()>=until:raise PilotError('configuration_transition_unconfirmed_reconcile_only')
            self.sleep(1)
    def configure_engine(self,role,observer):
        if role not in ENGINE_ROLES:raise PilotError('invalid_engine_role')
        actual=self.owned_machine(role);_,pilot=self.plan();volume=self.registered(pilot,'volume',ENGINE_ROLES[role])
        expected={'app':APP,'machine_id':actual['id'],'guest':ENGINE,'volume_ids':[volume],'final_volume_gib':20,'public_services':[]}
        if any(observer.get(key)!=target for key,target in expected.items()) or not binary_sha(observer.get('machines_api_response_sha256')):raise PilotError('provider_observer_mismatch')
        safe={key:copy.deepcopy(observer[key]) for key in [*expected,'machines_api_response_sha256']}
        return self.update_env(role,{'QUAL_OBSERVER_JSON':json.dumps(safe,sort_keys=True,separators=(',',':'))})
    def private_url(self,value,port):
        parsed=urllib.parse.urlsplit(value)
        host=parsed.hostname or ''
        try:private=ipaddress.ip_address(host).version==6 and host.lower().startswith('fdaa:')
        except ValueError:private=re.fullmatch('[a-f0-9]+\\.vm\\.'+re.escape(APP)+r'\.internal',host) is not None
        if parsed.scheme!='http' or not private or parsed.port!=port or parsed.path not in ('','/') or parsed.username or parsed.password or parsed.query or parsed.fragment:raise PilotError('machine_specific_private_origin_required')
        return value.rstrip('/')
    def configure_generator(self,native_url,native_control_url,tantivy_url,tantivy_control_url):
        _,pilot=self.plan()
        values={key:self.private_url(value,port) for key,value,port in [
            ('QUAL_NATIVE_URL',native_url,8080),('QUAL_NATIVE_CONTROL_URL',native_control_url,8082),
            ('QUAL_TANTIVY_URL',tantivy_url,8080),('QUAL_TANTIVY_CONTROL_URL',tantivy_control_url,8082)]}
        for role,keys in [('native-control',['QUAL_NATIVE_URL','QUAL_NATIVE_CONTROL_URL']),('tantivy-control',['QUAL_TANTIVY_URL','QUAL_TANTIVY_CONTROL_URL'])]:
            actual=self.owned_machine(role)
            expected=actual['id']+'.vm.'+APP+'.internal'
            for key in keys:
                host=urllib.parse.urlsplit(values[key]).hostname
                if host!=expected and host!=actual.get('private_ip'):raise PilotError('private_origin_is_not_owned_engine')
        values.update(QUAL_NATIVE_BINARY_SHA256=pilot['native_binary_sha256'],QUAL_TANTIVY_BINARY_SHA256=pilot['tantivy_binary_sha256'])
        return self.update_env('generator',values)
    def start(self,role):
        value,pilot=self.plan();self.lease(pilot);self.alive_reaper(pilot)
        if role not in ['native-control','tantivy-control','generator'] or role not in pilot.get('configured',{}):raise PilotError('configuration_required_before_start')
        actual=self.owned_machine(role)
        keys=pilot['configuration_keys'][role]
        if digest({key:actual['config']['env'].get(key) for key in keys})!=pilot['configured'][role]:raise PilotError('configured_environment_changed')
        if actual.get('state') not in {'created','stopped'}:raise PilotError('guest_already_started_or_transitioning')
        self.call('POST','machines/'+actual['id']+'/start')
        value,pilot=self.plan();pilot.setdefault('started',[]).append(role);self.save(value)
        return {'id':actual['id'],'role':role,'start_requested':True}
    def reconcile(self):
        value,pilot=self.plan();self.inventory(pilot);pending=[]
        for kind,entries in [('machine',pilot['manifest']['machines']+[pilot['manifest']['reaper']]),('volume',pilot['manifest']['volumes'])]:
            for entry in entries:
                if kind+':'+entry['role'] in pilot['attempts'] and self.resolve(kind,entry) is None:pending.append(kind+':'+entry['role'])
        _,pilot=self.plan();unconfirmed=[]
        for role,attempt in pilot.get('configuration_attempts',{}).items():
            if attempt.get('state')=='committed':continue
            try:self.confirm_configuration(role,self.owned_machine(role),attempt['sha256'],attempt['keys'])
            except PilotError:unconfirmed.append(role)
        return {'resolved':self.plan()[1]['resolved'],'unresolved_attempts':pending,'unresolved_configuration_attempts':unconfirmed,'safe_to_recreate':False}
    def cleanup(self):
        value,pilot=self.plan();guardian=self.guard(pilot)
        unresolved=[]
        for key,state in list(pilot['attempts'].items()):
            if state not in {'attempted','unknown'}:continue
            kind,role=key.split(':',1)
            if self.resolve(kind,self.entry(pilot,kind,role)) is None:unresolved.append(key)
        value,pilot=self.plan()
        if unresolved and self.now()<pilot['manifest']['deadline']+reaper.GRACE:
            result={'status':'cleanup_failed','class':'creation_outcome_unresolved','unresolved_attempts':unresolved,'reaper_retained':True}
            pilot['cleanup_receipt']=result;pilot['phase']='cleanup-failed';self.save(value);return result
        result=guardian.cleanup(creation_settled=True)
        value,pilot=self.plan();pilot['cleanup_receipt']=result;pilot['phase']='cleaned' if result['status']=='cleaned' else 'cleanup-failed';self.save(value)
        if result['status']!='cleaned':return result
        actual=guardian.resolve('machine',pilot['manifest']['reaper'])
        if actual is not None:
            guardian.verify_self(actual['id'])
            retired=guardian.clean_machine(pilot['manifest']['reaper'])
            until=self.monotonic()+30
            while not retired and self.monotonic()<until:
                self.sleep(1)
                actual=guardian.resolve('machine',pilot['manifest']['reaper'])
                retired=actual is None or actual.get('state')=='destroyed'
            if not retired:raise PilotError('reaper_retirement_unconfirmed')
        value,pilot=self.plan();pilot['reaper_retired']=True;value['machines']=[];value['volumes']=[];value['phase']='pilot-cleaned';self.save(value)
        return result|{'reaper_retired':True}

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('--image',required=True);p.add_argument('--native-binary-sha256',required=True);p.add_argument('--tantivy-binary-sha256',required=True);p.add_argument('--seconds',type=int,default=600)
    sub.add_parser('launch-reaper');p=sub.add_parser('provision');p.add_argument('--armed-receipt',type=Path,required=True)
    p=sub.add_parser('configure-engine');p.add_argument('--role',choices=sorted(ENGINE_ROLES),required=True);p.add_argument('--observer',type=Path,required=True)
    p=sub.add_parser('configure-generator')
    for key in ['native-url','native-control-url','tantivy-url','tantivy-control-url']:p.add_argument('--'+key,required=True)
    p=sub.add_parser('start');p.add_argument('--role',choices=['native-control','tantivy-control','generator'],required=True)
    sub.add_parser('reconcile');sub.add_parser('cleanup');args=parser.parse_args();pilot=Pilot()
    if args.action=='prepare':result=pilot.prepare(args.image,args.native_binary_sha256,args.tantivy_binary_sha256,args.seconds)
    elif args.action=='launch-reaper':result=pilot.launch_reaper()
    elif args.action=='provision':result=pilot.provision(json.loads(args.armed_receipt.read_text()))
    elif args.action=='configure-engine':result=pilot.configure_engine(args.role,json.loads(args.observer.read_text()))
    elif args.action=='configure-generator':result=pilot.configure_generator(args.native_url,args.native_control_url,args.tantivy_url,args.tantivy_control_url)
    elif args.action=='start':result=pilot.start(args.role)
    elif args.action=='reconcile':result=pilot.reconcile()
    else:result=pilot.cleanup()
    print(json.dumps(result,separators=(',',':')))

if __name__=='__main__':main()
