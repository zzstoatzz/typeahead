"""Explicit cloud pilot/full preparation/provisioning; importing performs no API calls.

prepare pins image/binary hashes and writes all pending names before creation.
launch_reaper is the only first paid guest. provision requires its matching ARMED
receipt and checks its running ownership before every subsequent allocation.
Engines/generator are created paused with auto_destroy false. Their final public
configuration atomically enables auto_destroy before explicit start. A lost update
reply is confirmed by GET, never retried against an auto-destroy guest. Platform supplies ADMIN_SECRET, QUAL_CONTROL_SECRET and
QUAL_REAPER_CAPABILITY; full mode additionally requires QUAL_SOURCE_ACCESS.
Secret values never enter this module, requests or ledger. Pilot remains the
default 600-second lease; full requires --mode full and allows at most 5400 seconds.

Full-only --staged-preparation creates performance/2CPU/4096MiB preparation
engines, retaining the same 20GiB volumes and original reaper deadline. Use
configure-preparation with a PREP provider observer, then start-preparation.
finish-preparation requires its bound checkpoint receipt plus authoritative
successful stopped/no-OOM exit evidence, then resizes paused guests to FINAL.
Fresh FINAL attestation and configure-engine bind resume/checkpoint and enable
auto_destroy. Start both final engines before starting the separate generator.
Retry an ambiguous update only with its original observer/receipt; reconciliation
reads the same IDs and never reposts a recorded update. This is preparation
capacity, not final serving or benchmark hardware.

--synthetic-staged-pilot additionally bounds this lifecycle to 128 local actors
and at most 900 seconds, without a source capability. Provider/runtime checks
remain mandatory; synthetic lifecycle success never qualifies corpus or speed.
"""
import argparse
from contextlib import contextmanager
import fcntl
import os
import tempfile
import base64
import copy
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import secrets
import subprocess
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
PREP={'cpu_kind':'performance','cpus':2,'memory_mb':4096}
SMALL={'cpu_kind':'shared','cpus':1,'memory_mb':256}
ENGINE_ROLES={'native-control':'native','tantivy-control':'tantivy'}
SECRETS={'ADMIN_SECRET','QUAL_CONTROL_SECRET','QUAL_REAPER_CAPABILITY','QUAL_SOURCE_ACCESS'}
PILOT_SECRETS=SECRETS-{'QUAL_SOURCE_ACCESS'}
LEASE_SECONDS={'pilot':600,'full':5400}

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
    def prepare(self,image,native_binary_sha256,tantivy_binary_sha256,seconds=600,region='ewr',run_id=None,mode='pilot',oracle_binary_sha256=None,staged_preparation=False,synthetic_staged_pilot=False):
        value=self.state()
        if value.get('machines') or value.get('volumes') or value.get('pilot',{}).get('phase') not in (None,'cleaned'):
            raise PilotError('existing_run_requires_cleanup')
        if not re.fullmatch(re.escape('registry.fly.io/'+APP+'@sha256:')+'[a-f0-9]{64}',image):raise PilotError('immutable_qualification_image_required')
        if not binary_sha(native_binary_sha256) or not binary_sha(tantivy_binary_sha256):raise PilotError('frozen_binary_hashes_required')
        if mode not in LEASE_SECONDS:raise PilotError('qualification_mode')
        if mode=='full' and not binary_sha(oracle_binary_sha256):raise PilotError('frozen_oracle_binary_hash_required')
        if type(seconds) is not int or not 1<=seconds<=LEASE_SECONDS[mode] or region!='ewr':raise PilotError('pilot_budget_or_region')
        if type(staged_preparation) is not bool or staged_preparation and mode!='full':raise PilotError('staged_preparation_requires_full')
        if type(synthetic_staged_pilot) is not bool or synthetic_staged_pilot and (mode!='full' or not staged_preparation or seconds>900):raise PilotError('synthetic_staged_pilot_bounds')
        run_id=run_id or secrets.token_hex(6);now=self.now();deadline=now+seconds
        def entry(role,kind='machine'):
            return {'name':('tq-' if kind=='machine' else 'tq_')+run_id+('-' if kind=='machine' else '_')+role,'role':role}
        manifest={'schema':1,'app':APP,'owner':OWNER,'run_id':run_id,'mode':mode,'created_after':now-30,
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
               'required_platform_secrets':sorted(SECRETS if mode=='full' and not synthetic_staged_pilot else PILOT_SECRETS)}
        if staged_preparation:pilot['staged_preparation']=True
        if synthetic_staged_pilot:pilot['synthetic_staged_pilot']=True;pilot['synthetic_actor_count']=128
        if mode=='full':pilot['oracle_binary_sha256']=oracle_binary_sha256
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
                state=actual.get('state')
                if state not in {'destroyed','pending_destroy'}:
                    # Fly's SDK treats these as deletion states. Require our earlier
                    # confirmed retirement as well; an unknown volume never qualifies.
                    # https://github.com/superfly/fly-go/blob/master/flaps/flaps_volumes.go
                    prior=next((row for row in retired.get('terminal_dispositions',[]) if row.get('id')==actual['id']),{})
                    if state not in {'scheduling_destroy','fork_cleanup','waiting_for_detach','destroying'} or prior.get('state')!='pending_destroy' or prior.get('non_billable') is not True:return False
                if actual.get('attached_machine_id') or actual.get('attached_alloc_id'):return False
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
        values={'QUAL_MODE':pilot['manifest']['mode'],'QUAL_RUN_ID':pilot['manifest']['run_id'],'QUAL_PILOT_NONCE':pilot['nonce'],
                'QUAL_WATERMARK':str(pilot['watermark']),'QUAL_DEADLINE':str(pilot['manifest']['deadline'])}
        if role in ENGINE_ROLES:values['QUAL_ROLE']=role
        if pilot.get('synthetic_staged_pilot'):values['QUAL_SYNTHETIC_STAGED_PILOT']='1'
        if pilot['manifest']['mode']=='full':values['QUAL_IMAGE_DIGEST']=pilot['image']
        if pilot['manifest']['mode']=='full' and role in {'native-control','generator'}:
            oracle=pilot.get('oracle_binary_sha256')
            if not binary_sha(oracle):raise PilotError('frozen_oracle_binary_hash_required')
            values['QUAL_ORACLE_BINARY_SHA256']=oracle
        return values
    def hard_command(self,command,reaper_mode=False):
        script="import os,sys,time;d=float(os.environ['QUAL_DEADLINE'])+%d-time.time();sys.exit(0) if d<=0 else None;os.execvp('timeout',['timeout','--signal=TERM','--kill-after=5',str(max(1,int(d)))]+sys.argv[1:])"%(330 if reaper_mode else 0)
        return ['python3','-c',script,*command]
    def staged(self,pilot,role):return pilot.get('staged_preparation') is True and role in ENGINE_ROLES
    def guest(self,pilot,role):
        if self.staged(pilot,role) and role not in pilot.get('preparation_finished',{}):return PREP
        return ENGINE if role in ENGINE_ROLES else SMALL
    def machine_payload(self,pilot,role):
        entry=self.entry(pilot,'machine',role);env=self.env(pilot,role);mounts=[]
        if self.staged(pilot,role):env['QUAL_PHASE']='prepare'
        if role=='reaper':
            data=base64.b64encode(encoded(pilot['manifest'])).decode();env['QUAL_REAPER_MANIFEST_B64']=data
            script="import base64,os,pathlib,sys;p=pathlib.Path('/tmp/reaper.json');b=base64.b64decode(os.environ['QUAL_REAPER_MANIFEST_B64']);f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(f,b);os.close(f);os.execv(sys.executable,[sys.executable,'/app/input/reaper.py','--daemon','--manifest',str(p)])"
            command=self.hard_command(['python3','-c',script],True)
        else:
            full=pilot['manifest']['mode']=='full'
            program=('full_bootstrap.py' if full else 'pilot_bootstrap.py') if role in ENGINE_ROLES else ('full_generator.py' if full else 'generator.py')
            command=self.hard_command(['python3','/app/'+program])
            if role in ENGINE_ROLES:
                vid=self.registered(pilot,'volume',ENGINE_ROLES[role])
                if not vid:raise PilotError('engine_volume_unresolved')
                mounts=[{'volume':vid,'path':'/data'}]
        config={'image':pilot['image'],'auto_destroy':role=='reaper','guest':copy.deepcopy(self.guest(pilot,role)),
                'services':[],'mounts':mounts,'restart':{'policy':'no'},'metadata':self.metadata(pilot,role),
                'init':{'exec':command},'env':env,'stop_config':{'signal':'SIGTERM','timeout':5000000000}}
        return {'name':entry['name'],'region':pilot['region'],'skip_service_registration':True,'skip_launch':role!='reaper','config':config}
    def verify_machine(self,pilot,role,actual):
        expected=self.machine_payload(pilot,role)['config'];config=actual.get('config',{})
        for key in ['image','guest']:
            if config.get(key)!=expected[key]:raise PilotError('machine_shape_or_image_mismatch')
        auto=config.get('auto_destroy',False)
        if type(auto) is not bool:raise PilotError('auto_destroy_must_be_explicit')
        if self.staged(pilot,role) and role not in pilot.get('preparation_finished',{}):
            if auto:raise PilotError('preparation_autodestroy_forbidden')
            if actual.get('state') not in {'created','stopped'} and not (actual.get('state')=='started' and role in pilot.get('preparation_started',{})):raise PilotError('preparation_state_unexpected')
        elif role=='reaper' or role in pilot.get('configured',{}):
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
        if self.staged(pilot,role):
            finished=pilot.get('preparation_finished',{}).get(role)
            if finished and auto:
                if environment.get('QUAL_PHASE')!='resume' or environment.get('QUAL_CHECKPOINT_SHA256')!=finished['checkpoint_sha256']:raise PilotError('resume_checkpoint_binding')
            elif environment.get('QUAL_PHASE')!='prepare' or 'QUAL_CHECKPOINT_SHA256' in environment:raise PilotError('preparation_phase_binding')
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
                'auto_backup_enabled':False,'compute':copy.deepcopy(PREP if pilot.get('staged_preparation') else ENGINE)})
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
        if self.staged(pilot,role) and role not in pilot.get('preparation_finished',{}):raise PilotError('preparation_not_finished')
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
    def staged_signature(self,config):
        return {key:copy.deepcopy(config.get(key)) for key in ['image','guest','init','env','restart']}|{
            'auto_destroy':config.get('auto_destroy',False),'services':config.get('services') or [],
            'metadata':{key:config.get('metadata',{}).get(key) for key in ['owner','run_id','role']},
            'mounts':[{key:mount.get(key) for key in ['volume','path']} for mount in config.get('mounts') or []]}
    def staged_update(self,role,kind,config):
        value,pilot=self.plan();self.lease(pilot);self.alive_reaper(pilot)
        desired=self.staged_signature(config);key=role+':'+kind
        attempt=pilot.get('staged_attempts',{}).get(key)
        if attempt and attempt['desired_sha256']!=digest(desired):raise PilotError('staged_update_intent_changed')
        if not attempt:
            actual=self.owned_machine(role)
            if actual.get('state') not in {'created','stopped'} or actual['config'].get('auto_destroy',False):raise PilotError('staged_update_requires_paused_mutable_guest')
            pilot.setdefault('staged_attempts',{})[key]={'desired_sha256':digest(desired),'state':'attempted'};self.save(value)
            try:self.call('POST','machines/'+actual['id'],{'config':config,'skip_launch':True})
            except PilotError:
                value,pilot=self.plan();pilot['staged_attempts'][key]['state']='unknown';self.save(value)
        until=min(self.monotonic()+30,self.monotonic()+max(0,pilot['manifest']['deadline']-self.now()))
        while True:
            _,pilot=self.plan();actual=self.resolve('machine',self.entry(pilot,'machine',role))
            if actual is None:raise PilotError('staged_target_missing')
            if actual.get('state') in {'created','stopped'} and self.staged_signature(actual.get('config',{}))==desired:
                value,pilot=self.plan();pilot['staged_attempts'][key]['state']='committed';self.save(value);return actual
            if actual.get('state') not in {'created','stopped','updating','replacing','stopping'} or actual.get('config',{}).get('auto_destroy',False):raise PilotError('staged_transition_unexpected')
            if self.monotonic()>=until:raise PilotError('staged_transition_unconfirmed_reconcile_only')
            self.sleep(1)
    def configure_preparation(self,role,observer):
        _,pilot=self.plan()
        if not self.staged(pilot,role) or role in pilot.get('preparation_finished',{}):raise PilotError('staged_preparation_not_pending')
        actual=self.owned_machine(role);vid=self.registered(pilot,'volume',ENGINE_ROLES[role])
        expected={'app':APP,'machine_id':actual['id'],'guest':PREP,'volume_ids':[vid],'final_volume_gib':20,'public_services':[]}
        if any(observer.get(key)!=wanted for key,wanted in expected.items()) or not binary_sha(observer.get('machines_api_response_sha256')):raise PilotError('preparation_observer_mismatch')
        safe={key:copy.deepcopy(observer[key]) for key in [*expected,'machines_api_response_sha256']}
        config=copy.deepcopy(actual['config']);config.setdefault('env',{})['QUAL_OBSERVER_JSON']=json.dumps(safe,sort_keys=True,separators=(',',':'));config['auto_destroy']=False
        observed=self.staged_update(role,'configure',config);self.verify_machine(self.plan()[1],role,observed)
        value,pilot=self.plan();pilot.setdefault('preparation_configured',{})[role]=digest(safe);self.save(value)
        return {'id':observed['id'],'role':role,'preparation_configured':True,'auto_destroy':False}
    def start_preparation(self,role):
        value,pilot=self.plan();self.lease(pilot,creating=True);self.alive_reaper(pilot)
        if not self.staged(pilot,role) or role not in pilot.get('preparation_configured',{}) or role in pilot.get('preparation_finished',{}):raise PilotError('preparation_configuration_required')
        actual=self.owned_machine(role)
        observer=json.loads(actual['config']['env'].get('QUAL_OBSERVER_JSON','{}'))
        if digest(observer)!=pilot['preparation_configured'][role]:raise PilotError('preparation_observer_changed')
        if role in pilot.get('preparation_started',{}):return {'id':actual['id'],'role':role,'preparation_start_previously_requested':True,'state':actual['state']}
        if actual.get('state') not in {'created','stopped'}:raise PilotError('preparation_start_requires_paused')
        pilot.setdefault('preparation_started',{})[role]={'requested_at':self.now()};self.save(value)
        self.call('POST','machines/'+actual['id']+'/start')
        return {'id':actual['id'],'role':role,'preparation_start_requested':True,'auto_destroy':False}
    def validate_preparation_exit(self,pilot,role,actual,receipt):
        volume=self.registered(pilot,'volume',ENGINE_ROLES[role])
        expected={'app':APP,'machine_id':actual['id'],'volume_id':volume,'arm':ENGINE_ROLES[role],
                  'nonce':pilot['nonce'],'prepared':True,'guest':PREP,'runtime_instance_id':actual.get('instance_id')}
        if any(receipt.get(key)!=wanted for key,wanted in expected.items()) or receipt.get('prepared') is not True or not actual.get('instance_id'):raise PilotError('preparation_receipt_binding')
        if not binary_sha(receipt.get('checkpoint_sha256')) or not isinstance(receipt.get('build_id'),str) or not re.fullmatch('[A-Za-z0-9._-]{1,120}',receipt['build_id']):raise PilotError('preparation_checkpoint_identity')
        begin=pilot.get('preparation_started',{}).get(role,{}).get('requested_at');prepared=receipt.get('prepared_at')
        if type(prepared) not in {int,float} or begin is None or not begin<=prepared<=self.now()+2:raise PilotError('preparation_receipt_time')
        if actual.get('state')!='stopped':raise PilotError('preparation_not_stopped')
        exits=[event for event in actual.get('events',[]) if event.get('type')=='exit' and type(event.get('timestamp')) in {int,float}]
        if not exits:raise PilotError('preparation_exit_evidence_missing')
        event=max(exits,key=lambda item:item['timestamp']);exit=event.get('request',{}).get('exit_event',{})
        if event.get('source')!='flyd' or event.get('status')!='stopped' or event['timestamp']/1000<prepared or event['timestamp']/1000>self.now()+2 or any(exit.get(key)!=wanted for key,wanted in {'exit_code':0,'guest_exit_code':0,'oom_killed':False,'requested_stop':False,'restarting':False,'guest_error':'','error':''}.items()) or type(exit.get('exit_code')) is not int or type(exit.get('guest_exit_code')) is not int or exit.get('oom_killed') is not False or exit.get('requested_stop') is not False or exit.get('restarting') is not False or exit.get('signal') not in {-1,0} or exit.get('guest_signal') not in {-1,0}:raise PilotError('preparation_unsuccessful_exit')
        return {'instance_id':actual['instance_id'],'exit_event_timestamp':event['timestamp'],'exit_code':0,'oom_killed':False}
    def finish_preparation(self,role,receipt):
        value,pilot=self.plan();self.lease(pilot,creating=True);self.alive_reaper(pilot)
        if not self.staged(pilot,role):raise PilotError('staged_preparation_required')
        prior=pilot.get('preparation_finished',{}).get(role)
        if prior:
            if prior['receipt_sha256']!=digest(receipt):raise PilotError('preparation_receipt_changed')
            self.owned_machine(role);return {'role':role,'preparation_finished':True,'idempotent':True}
        recorded=pilot.get('preparation_receipts',{}).get(role)
        actual=self.resolve('machine',self.entry(pilot,'machine',role))
        if actual is None:raise PilotError('preparation_machine_missing')
        if recorded:
            if recorded['receipt_sha256']!=digest(receipt):raise PilotError('preparation_receipt_changed')
        else:
            self.verify_machine(pilot,role,actual)
            proof=self.validate_preparation_exit(pilot,role,actual,receipt)
            pilot.setdefault('preparation_receipts',{})[role]={'receipt_sha256':digest(receipt),'receipt':copy.deepcopy(receipt),'exit_proof':proof};self.save(value)
        config=copy.deepcopy(actual['config']);config['guest']=copy.deepcopy(ENGINE);config['auto_destroy']=False
        observed=self.staged_update(role,'resize',config)
        value,pilot=self.plan();pilot.setdefault('preparation_finished',{})[role]={'checkpoint_sha256':receipt['checkpoint_sha256'],'build_id':receipt['build_id'],'receipt_sha256':digest(receipt)};self.save(value)
        self.verify_machine(pilot,role,observed)
        return {'id':observed['id'],'role':role,'preparation_finished':True,'guest':ENGINE,'volume_id':self.registered(pilot,'volume',ENGINE_ROLES[role]),'auto_destroy':False}
    def configure_engine(self,role,observer):
        if role not in ENGINE_ROLES:raise PilotError('invalid_engine_role')
        actual=self.owned_machine(role);_,pilot=self.plan();volume=self.registered(pilot,'volume',ENGINE_ROLES[role])
        if self.staged(pilot,role) and role not in pilot.get('preparation_finished',{}):raise PilotError('preparation_not_finished')
        expected={'app':APP,'machine_id':actual['id'],'guest':ENGINE,'volume_ids':[volume],'final_volume_gib':20,'public_services':[]}
        if any(observer.get(key)!=target for key,target in expected.items()) or not binary_sha(observer.get('machines_api_response_sha256')):raise PilotError('provider_observer_mismatch')
        safe={key:copy.deepcopy(observer[key]) for key in [*expected,'machines_api_response_sha256']}
        values={'QUAL_OBSERVER_JSON':json.dumps(safe,sort_keys=True,separators=(',',':'))}
        if self.staged(pilot,role):values.update(QUAL_PHASE='resume',QUAL_CHECKPOINT_SHA256=pilot['preparation_finished'][role]['checkpoint_sha256'])
        return self.update_env(role,values)
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
        if pilot['manifest']['mode']=='full':values['QUAL_ORACLE_BINARY_SHA256']=pilot['oracle_binary_sha256']
        return self.update_env('generator',values)
    def start(self,role):
        value,pilot=self.plan();self.lease(pilot);self.alive_reaper(pilot)
        if role not in ['native-control','tantivy-control','generator'] or role not in pilot.get('configured',{}):raise PilotError('configuration_required_before_start')
        if role=='generator' and pilot.get('staged_preparation') and (set(pilot.get('preparation_finished',{}))!=set(ENGINE_ROLES) or not set(ENGINE_ROLES).issubset(pilot.get('configured',{}))):raise PilotError('both_final_engines_required')
        if role=='generator' and pilot.get('staged_preparation'):
            for engine in ENGINE_ROLES:
                if self.owned_machine(engine).get('state')!='started':raise PilotError('both_final_engines_must_be_running')
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
        _,pilot=self.plan();staged_pending=[]
        for key,attempt in pilot.get('staged_attempts',{}).items():
            if attempt.get('state')=='committed':continue
            role,kind=key.rsplit(':',1)
            actual=self.resolve('machine',self.entry(pilot,'machine',role))
            if actual is not None and actual.get('state') in {'created','stopped'} and digest(self.staged_signature(actual.get('config',{})))==attempt['desired_sha256']:
                value,current=self.plan();current['staged_attempts'][key]['state']='committed';self.save(value)
            else:staged_pending.append(key)
        _,pilot=self.plan()
        finish_pending=[role for role in ENGINE_ROLES if pilot.get('staged_attempts',{}).get(role+':resize',{}).get('state')=='committed' and role not in pilot.get('preparation_finished',{})]
        return {'resolved':pilot['resolved'],'unresolved_attempts':pending,'unresolved_configuration_attempts':unconfirmed,
                'unresolved_staged_attempts':staged_pending,'staged_finish_receipt_required':finish_pending,'safe_to_recreate':False}
    @contextmanager
    def supervisor_lock(self):
        ledger=Path(getattr(self.control,'LEDGER',getattr(self.control,'path',HERE/'resources.json')))
        descriptor=os.open(ledger.with_suffix('.advance.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise PilotError('another_supervisor_owns_lease') from None
            yield
        finally:os.close(descriptor)
    def advance_to_measurement(self,run_id,poll_seconds=2):
        if not isinstance(run_id,str) or not re.fullmatch('[a-f0-9]{8,16}',run_id) or not 1<=poll_seconds<=5:raise PilotError('supervisor_bounds')
        with self.supervisor_lock():return self._advance_owned(run_id,poll_seconds)
    def _advance_owned(self,run_id,poll):
        _,initial=self.plan()
        if initial['manifest']['run_id']!=run_id or not initial.get('staged_preparation'):raise PilotError('supervisor_run_binding')
        expected_manifest=initial['manifest_sha256'];expected_ids=copy.deepcopy(initial['resolved'])
        if initial.get('supervisor',{}).get('state') in {'completed','failed'} and initial.get('cleanup_receipt',{}).get('status')=='cleaned':return initial['supervisor']
        required={'machine:'+role for role in ['native-control','tantivy-control','generator','reaper']}|{'volume:native','volume:tantivy'}
        if not required.issubset(expected_ids) or not set(ENGINE_ROLES).issubset(initial.get('preparation_started',{})):raise PilotError('supervisor_requires_existing_started_preparation')
        def bound():
            value,pilot=self.plan()
            if pilot['manifest']['run_id']!=run_id or pilot['manifest_sha256']!=expected_manifest or pilot['resolved']!=expected_ids:raise PilotError('supervisor_run_binding_changed')
            return value,pilot
        def journal(**fields):
            value,pilot=bound();pilot.setdefault('supervisor',{}).update(fields,heartbeat_at=self.now(),owner_pid=os.getpid());self.save(value)
        def observed(role):
            _,pilot=bound()
            for attempt in range(3):
                try:actual=self.control.owned(expected_ids['machine:'+role]);break
                except (RuntimeError,subprocess.TimeoutExpired):
                    if attempt==2 or self.now()+attempt+1>=pilot['manifest']['deadline']-15:raise PilotError('supervisor_observation_unavailable') from None
                    self.sleep(attempt+1)
            if any(actual.get('config',{}).get('metadata',{}).get(key)!=wanted for key,wanted in self.metadata(pilot,role).items()):raise PilotError('supervisor_machine_identity')
            return actual
        def start_once(role):
            value,pilot=bound();actual=observed(role)
            if actual.get('state')=='started':
                self.owned_machine(role)
                keys=pilot.get('configuration_keys',{}).get(role,[])
                if not keys or digest({key:actual['config']['env'].get(key) for key in keys})!=pilot.get('configured',{}).get(role):raise PilotError('configured_environment_changed')
                if role not in pilot.get('started',[]):pilot.setdefault('started',[]).append(role);self.save(value)
                return
            attempted=role in pilot.get('supervisor',{}).get('start_attempts',{})
            if not attempted:
                pilot.setdefault('supervisor',{}).setdefault('start_attempts',{})[role]=self.now();self.save(value)
                try:self.start(role)
                except PilotError:pass
            until=min(self.monotonic()+30,self.monotonic()+max(0,pilot['manifest']['deadline']-self.now()-15))
            while self.monotonic()<until:
                actual=observed(role)
                if actual.get('state')=='started' or role=='generator' and actual.get('state') in {'destroyed','stopped','failed'}:return
                if actual.get('state') not in {'created','stopped','starting','replacing','updating'}:raise PilotError('supervisor_start_terminal')
                self.sleep(1)
            raise PilotError('supervisor_start_unconfirmed_no_retry')
        def final_observer(role):
            _,pilot=bound();cached=pilot.get('supervisor',{}).get('observers',{}).get(role)
            if cached:return cached
            with tempfile.TemporaryDirectory(prefix='qualification-observer-') as directory:
                observer=self.control.attest(expected_ids['machine:'+role],Path(directory)/'observer.json')
            value,pilot=bound();pilot.setdefault('supervisor',{}).setdefault('observers',{})[role]=observer;self.save(value);return observer
        journal(state='advancing',run_id=run_id,deadline=initial['manifest']['deadline'])
        failure=None
        try:
            while True:
                _,pilot=bound();self.lease(pilot,creating=True);self.alive_reaper(pilot);journal(phase='preparation')
                for role in ENGINE_ROLES:
                    _,pilot=bound()
                    if role not in pilot.get('preparation_finished',{}):
                        actual=observed(role)
                        if actual.get('state')=='stopped':
                            try:receipts=self.control.log_receipts(actual['id'])
                            except (RuntimeError,subprocess.TimeoutExpired):continue
                            matches=[row['receipt'] for row in receipts if row.get('marker')=='QUALIFICATION_PREPARATION_COMPLETE' and row.get('receipt',{}).get('nonce')==pilot['nonce']]
                            if matches:self.finish_preparation(role,matches[-1])
                            else:
                                value,pilot=bound();seen=pilot.setdefault('supervisor',{}).setdefault('stopped_without_receipt',{}).setdefault(role,self.now());self.save(value)
                                if self.now()-seen>=30:raise PilotError('supervisor_preparation_receipt_missing')
                                continue
                        elif actual.get('state') in {'destroyed','failed','launch_failed'}:raise PilotError('supervisor_preparation_terminal')
                        else:continue
                    _,pilot=bound()
                    if role not in pilot.get('configured',{}):self.configure_engine(role,final_observer(role))
                _,pilot=bound()
                if set(ENGINE_ROLES).issubset(pilot.get('configured',{})):break
                self.sleep(poll)
            journal(phase='starting_final_engines')
            for role in ENGINE_ROLES:start_once(role)
            _,pilot=bound()
            if 'generator' not in pilot.get('configured',{}):
                def url(role,port):return 'http://'+expected_ids['machine:'+role]+'.vm.'+APP+'.internal:'+str(port)
                self.configure_generator(url('native-control',8080),url('native-control',8082),url('tantivy-control',8080),url('tantivy-control',8082))
            start_once('generator');journal(phase='measurement')
            terminal_seen=None
            while True:
                _,pilot=bound()
                if self.now()>=pilot['manifest']['deadline']-10:raise PilotError('supervisor_measurement_deadline')
                actual=observed('generator')
                try:logs=self.control.log_receipts(actual['id'])
                except (RuntimeError,subprocess.TimeoutExpired):logs=[]
                marker='QUALIFICATION_SYNTHETIC_STAGED_PILOT_RESULT' if pilot.get('synthetic_staged_pilot') else 'QUALIFICATION_FULL_RESULT'
                for row in logs:
                    if row.get('marker')!=marker:continue
                    result=row.get('receipt')
                    if not isinstance(result,dict) or type(result.get('passed')) is not bool or result.get('production_contact') is not False or len(encoded(result))>65536:raise PilotError('supervisor_generator_result_contract')
                    checksum=result.get('metadata_sha256') if 'arms' not in result and binary_sha(result.get('metadata_sha256')) else digest(result)
                    _,pilot=bound();prior=pilot.get('supervisor',{}).get('generator_result')
                    if prior and prior['sha256']!=checksum:raise PilotError('supervisor_generator_result_changed')
                    journal(generator_result={'receipt':result,'sha256':checksum,'truncated':'arms' not in result,'captured_at':self.now()})
                _,pilot=bound();captured=pilot.get('supervisor',{}).get('generator_result')
                if actual.get('state') in {'destroyed','stopped','failed','launch_failed'}:
                    if captured:break
                    if terminal_seen is None:terminal_seen=self.now()
                    if self.now()-terminal_seen>=30:raise PilotError('supervisor_terminal_result_missing')
                journal(phase='measurement');self.sleep(poll)
            if not captured['receipt']['passed']:raise PilotError('supervisor_generator_reported_failure')
            journal(phase='post_measurement')
            _,pilot=bound();functional=pilot.get('supervisor',{}).get('functional')
            if functional is None:
                runner=getattr(self.control,'run_functional',None)
                if callable(runner) and pilot['manifest']['deadline']-self.now()>=90 and all(observed(role).get('state')=='started' for role in ENGINE_ROLES):
                    journal(functional={'state':'attempted','timed_result_sha256':captured['sha256']})
                    try:
                        receipt=runner(expected_ids['machine:tantivy-control'],expected_ids['machine:native-control'],run_id,pilot,captured['sha256'])
                        if not isinstance(receipt,dict) or receipt.get('timed_result_sha256')!=captured['sha256'] or len(encoded(receipt))>65536:raise PilotError('supervisor_functional_contract')
                        journal(functional={'state':'completed','receipt':receipt})
                    except Exception:journal(functional={'state':'inconclusive','reason':'functional_execution_uncertain_no_retry'})
                else:journal(functional={'state':'inconclusive','reason':'functional_runner_or_lease_unavailable'})
            journal(phase='cleanup')
        except Exception as error:
            failure=str(error) if isinstance(error,PilotError) and re.fullmatch('[A-Za-z0-9_]{1,120}',str(error)) else type(error).__name__
            journal(error=failure,phase='cleanup')
        finally:
            bound()
            try:cleanup=self.cleanup()
            except Exception as error:cleanup={'status':'cleanup_failed','class':type(error).__name__}
            journal(state='failed' if failure or cleanup.get('status')!='cleaned' else 'completed',cleanup=cleanup)
        return bound()[1]['supervisor']
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

def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('--image',required=True);p.add_argument('--native-binary-sha256',required=True);p.add_argument('--tantivy-binary-sha256',required=True);p.add_argument('--seconds',type=int,default=600);p.add_argument('--mode',choices=sorted(LEASE_SECONDS),default='pilot');p.add_argument('--oracle-binary-sha256');p.add_argument('--staged-preparation',action='store_true');p.add_argument('--synthetic-staged-pilot',action='store_true',help='128 synthetic actors, no R2 source; requires staged full mode and at most 900 seconds')
    sub.add_parser('launch-reaper');p=sub.add_parser('provision');p.add_argument('--armed-receipt',type=Path,required=True)
    p=sub.add_parser('configure-engine');p.add_argument('--role',choices=sorted(ENGINE_ROLES),required=True);p.add_argument('--observer',type=Path,required=True)
    for action in ['configure-preparation','start-preparation','finish-preparation']:
        p=sub.add_parser(action);p.add_argument('--role',choices=sorted(ENGINE_ROLES),required=True)
        if action=='configure-preparation':p.add_argument('--observer',type=Path,required=True)
        if action=='finish-preparation':p.add_argument('--receipt',type=Path,required=True)
    p=sub.add_parser('configure-generator')
    for key in ['native-url','native-control-url','tantivy-url','tantivy-control-url']:p.add_argument('--'+key,required=True)
    p=sub.add_parser('start');p.add_argument('--role',choices=['native-control','tantivy-control','generator'],required=True)
    p=sub.add_parser('advance-to-measurement');p.add_argument('--run-id',required=True);p.add_argument('--poll-seconds',type=int,default=2)
    sub.add_parser('reconcile');sub.add_parser('cleanup');return parser.parse_args(argv)

def main():
    args=arguments();pilot=Pilot()
    if args.action=='prepare':result=pilot.prepare(args.image,args.native_binary_sha256,args.tantivy_binary_sha256,args.seconds,mode=args.mode,oracle_binary_sha256=args.oracle_binary_sha256,staged_preparation=args.staged_preparation,synthetic_staged_pilot=args.synthetic_staged_pilot)
    elif args.action=='launch-reaper':result=pilot.launch_reaper()
    elif args.action=='provision':result=pilot.provision(json.loads(args.armed_receipt.read_text()))
    elif args.action=='configure-engine':result=pilot.configure_engine(args.role,json.loads(args.observer.read_text()))
    elif args.action=='configure-preparation':result=pilot.configure_preparation(args.role,json.loads(args.observer.read_text()))
    elif args.action=='start-preparation':result=pilot.start_preparation(args.role)
    elif args.action=='finish-preparation':result=pilot.finish_preparation(args.role,json.loads(args.receipt.read_text()))
    elif args.action=='configure-generator':result=pilot.configure_generator(args.native_url,args.native_control_url,args.tantivy_url,args.tantivy_control_url)
    elif args.action=='start':result=pilot.start(args.role)
    elif args.action=='advance-to-measurement':result=pilot.advance_to_measurement(args.run_id,args.poll_seconds)
    elif args.action=='reconcile':result=pilot.reconcile()
    else:result=pilot.cleanup()
    print(json.dumps(result,separators=(',',':')))

if __name__=='__main__':main()
