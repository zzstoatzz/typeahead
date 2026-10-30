import copy
from datetime import datetime,timezone
import http.client
import http.server
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.parse

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('qualification_pilot',HERE/'pilot_control.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
IMAGE='registry.fly.io/'+p.APP+'@sha256:'+'a'*64
NATIVE='b'*64
TANTIVY='c'*64
ORACLE='d'*64

class Clock:
    def __init__(self):self.value=1000.
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds

class APIState:
    def __init__(self,ledger,clock):
        self.ledger=ledger;self.clock=clock;self.machines={};self.volumes={};self.calls=[];self.violations=[];self.counter=0;self.lost={};self.lost_update={};self.tombstones={'machines':{},'volumes':{}};self.retain_destroyed=False;self.pending_destroy_volumes=False;self.update_transition={};self.pending_updates={};self.retirement_delay={};self.retiring={};self.get_failures={}
        state=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do(self):
                parsed=urllib.parse.urlsplit(self.path);prefix='/v1/apps/'+p.APP+'/'
                if not parsed.path.startswith(prefix):self.respond(403,{});return
                parts=parsed.path[len(prefix):].split('/');kind=parts[0];rid=parts[1] if len(parts)>1 else None;action=parts[2] if len(parts)>2 else None
                length=int(self.headers.get('Content-Length','0'));body=json.loads(self.rfile.read(length)) if length else None
                state.calls.append((self.command,kind,rid,action,copy.deepcopy(body)))
                records=state.machines if kind=='machines' else state.volumes
                if self.command=='GET':
                    if state.get_failures.get(rid,0):
                        state.get_failures[rid]-=1;self.respond(503,{});return
                    if kind=='machines' and rid in state.retiring:
                        remaining=state.retiring[rid]
                        if remaining==0:
                            removed=records.pop(rid);removed['state']='destroyed';state.tombstones[kind][rid]=removed;state.retiring.pop(rid)
                        elif remaining is not None:state.retiring[rid]-=1
                    if kind=='machines' and rid in state.pending_updates:
                        pending=state.pending_updates[rid]
                        if pending['remaining']==0:
                            records[rid]['config']=pending['config'];records[rid]['state']='stopped';state.pending_updates.pop(rid)
                        elif pending['remaining'] is not None:pending['remaining']-=1
                    actual=records.get(rid,state.tombstones[kind].get(rid))
                    self.respond(200 if rid is None or actual is not None else 404,list(records.values()) if rid is None else actual or {});return
                if self.command=='POST' and rid is None:
                    data=json.loads(state.ledger.read_text());plan=data['pilot'];name=body['name']
                    entries=plan['manifest']['machines']+[plan['manifest']['reaper']] if kind=='machines' else plan['manifest']['volumes']
                    entry=next((e for e in entries if e['name']==name),None)
                    key=('machine' if kind=='machines' else 'volume')+':'+entry['role'] if entry else ''
                    if entry is None or plan['attempts'].get(key)!='attempted':state.violations.append('missing writeahead')
                    if entry and entry['role']!='reaper' and not plan.get('armed'):state.violations.append('allocation before armed')
                    state.counter+=1;newid=f'{state.counter:014x}' if kind=='machines' else 'vol_'+f'{state.counter:014x}'
                    created=datetime.fromtimestamp(state.clock.now(),timezone.utc).isoformat()
                    if kind=='machines':
                        config=copy.deepcopy(body['config'])
                        for mount in config.get('mounts',[]):mount.update(encrypted=True,name='derived-api-name',size_gb=20,unknown_safe_field={'value':1})
                        actual={'id':newid,'name':name,'created_at':created,'config':config,'state':'created' if body.get('skip_launch') else 'started','private_ip':'fdaa::'+str(state.counter),'region':body['region'],'instance_id':'fixture-version'}
                    else:actual={'id':newid,'name':name,'created_at':created,'size_gb':body['size_gb'],'encrypted':body['encrypted'],'region':body['region'],'attached_machine_id':None,'state':'created'}
                    lose=state.lost.pop(key,None)
                    if lose!='absent':records[newid]=actual
                    if lose:
                        self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                    self.respond(201,actual);return
                if rid not in records:self.respond(404,{});return
                if self.command=='POST' and action in {'start','stop'}:
                    records[rid]['state']='started' if action=='start' else 'stopped';self.respond(200,{});return
                if self.command=='POST':
                    if records[rid]['config'].get('auto_destroy') is True:self.respond(400,{'error':'unable to update machine configured for auto destroy'});return
                    role=records[rid]['config']['metadata']['role'];lose=state.lost_update.pop(role,None)
                    if lose!='absent':
                        if role in state.update_transition:
                            state.pending_updates[rid]={'config':copy.deepcopy(body['config']),'remaining':state.update_transition.pop(role)}
                            records[rid]['state']='updating'
                        else:records[rid]['config']=body['config']
                    if lose:
                        self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                    self.respond(200,records[rid]);return
                if self.command=='DELETE':
                    if kind=='machines' and rid in state.retirement_delay:
                        records[rid]['state']='destroying';state.retiring[rid]=state.retirement_delay.pop(rid);self.respond(200,{});return
                    if kind=='volumes' and state.pending_destroy_volumes:
                        records[rid]['state']='pending_destroy';records[rid]['attached_machine_id']=None;self.respond(200,{});return
                    removed=records.pop(rid)
                    if state.retain_destroyed:removed['state']='destroyed';state.tombstones[kind][rid]=removed
                    for vol in state.volumes.values():
                        if vol.get('attached_machine_id')==rid:vol['attached_machine_id']=None
                    self.respond(200,{})
            def respond(self,status,value):
                value=copy.deepcopy(value)
                for actual in value if isinstance(value,list) else [value]:
                    if isinstance(actual,dict) and isinstance(actual.get('config'),dict) and actual['config'].get('auto_destroy') is False:
                        actual['config'].pop('auto_destroy')
                raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            do_GET=do;do_POST=do;do_DELETE=do
            def log_message(self,*args):pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

class Control:
    APP=p.APP;OWNER=p.OWNER;PROTECTED=p.reaper.PROTECTED_MACHINES
    def __init__(self,path,server):self.path=path;self.server=server;self.save({'schema':1,'app':self.APP,'owner':self.OWNER,'machines':[],'volumes':[]})
    def ledger(self):return json.loads(self.path.read_text())
    def save(self,value):
        temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps(value));temporary.replace(self.path)
    def api(self,method,path,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server.server_port,timeout=2)
        try:
            conn.request(method,'/v1/apps/'+self.APP+'/'+path,body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json'})
            response=conn.getresponse();raw=response.read()
            if response.status>=400:raise RuntimeError('qualification API HTTP '+str(response.status))
            return json.loads(raw) if raw and method!='DELETE' else None
        except (OSError,http.client.HTTPException):raise RuntimeError('qualification API transport failed') from None
        finally:conn.close()

    def owned(self,mid):
        if mid in self.PROTECTED or mid not in [m['id'] for m in self.ledger()['machines']]:raise RuntimeError('unowned machine')
        actual=self.api('GET','machines/'+mid)
        if actual['config']['metadata']['owner']!=self.OWNER:raise RuntimeError('owner differs')
        return actual
    def attest(self,mid,destination):
        actual=self.owned(mid);volumes={v['id']:v for v in self.api('GET','volumes')}
        value={'app':p.APP,'machine_id':mid,'guest':actual['config']['guest'],'volume_ids':[m['volume'] for m in actual['config']['mounts']],
               'final_volume_gib':sum(volumes[m['volume']]['size_gb'] for m in actual['config']['mounts']),'public_services':[],
               'machines_api_response_sha256':'e'*64}
        Path(destination).write_text(json.dumps(value));return value
    def log_receipts(self,mid):
        if getattr(self,'log_timeouts',0):
            self.log_timeouts-=1;raise subprocess.TimeoutExpired('fixture-log-reader',25)
        actual=self.owned(mid)
        if actual['config']['metadata']['role']=='generator' and actual['state']=='started' and hasattr(self,'generator_result'):
            actual=self.server.machines.pop(mid);actual['state']='destroyed';self.server.tombstones['machines'][mid]=actual
            return [{'marker':'QUALIFICATION_FULL_RESULT','receipt':copy.deepcopy(self.generator_result)}]
        return copy.deepcopy(getattr(self,'logs',{}).get(mid,[]))

class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.json';self.clock=Clock()
        self.api=APIState(self.path,self.clock);self.control=Control(self.path,self.api)
        self.pilot=p.Pilot(self.control,self.clock.now,self.clock.now,self.clock.sleep)
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY,run_id='ab12cd34ef56')
    def tearDown(self):self.api.close();self.temp.cleanup()
    def arm(self):
        self.pilot.launch_reaper();_,plan=self.pilot.plan()
        receipt={key:plan['manifest'][key] for key in ['app','owner','run_id','deadline']}
        receipt.update(manifest_sha256=plan['manifest_sha256'],self={'id':plan['resolved']['machine:reaper'],'name':plan['manifest']['reaper']['name'],'auto_destroy':True})
        self.pilot.record_armed(receipt);return receipt
    def provision(self):self.arm();return self.pilot.provision()
    def observer(self,role):
        _,plan=self.pilot.plan();actual=self.pilot.owned_machine(role);vid=plan['resolved']['volume:'+p.ENGINE_ROLES[role]]
        return {'app':p.APP,'machine_id':actual['id'],'guest':copy.deepcopy(actual['config']['guest']),'volume_ids':[vid],
                'final_volume_gib':20,'public_services':[],'machines_api_response_sha256':'d'*64,'raw_should_not_be_copied':'discard'}
    def configure_all(self):
        for role in p.ENGINE_ROLES:self.pilot.configure_engine(role,self.observer(role))
        _,plan=self.pilot.plan();native=plan['resolved']['machine:native-control'];tantivy=plan['resolved']['machine:tantivy-control']
        def url(mid,port):return f'http://{mid}.vm.{p.APP}.internal:{port}'
        self.pilot.configure_generator(url(native,8080),url(native,8082),url(tantivy,8080),url(tantivy,8082))
    def staged(self):
        self.control.save({'schema':1,'app':p.APP,'owner':p.OWNER,'machines':[],'volumes':[]})
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY,mode='full',seconds=5400,oracle_binary_sha256=ORACLE,staged_preparation=True,run_id='1234abcd5678')
        self.provision()
    def prepared_exit(self,role):
        self.pilot.configure_preparation(role,self.observer(role));self.pilot.start_preparation(role)
        self.clock.sleep(1)
        _,plan=self.pilot.plan();actual=self.pilot.owned_machine(role)
        self.api.machines[actual['id']]['state']='stopped'
        self.api.machines[actual['id']]['events']=[{'type':'exit','status':'stopped','source':'flyd','timestamp':self.clock.now()*1000,
            'request':{'exit_event':{'requested_stop':False,'restarting':False,'guest_exit_code':0,'guest_signal':-1,'guest_error':'','exit_code':0,'signal':-1,'error':'','oom_killed':False}}}]
        return {'app':p.APP,'machine_id':actual['id'],'volume_id':plan['resolved']['volume:'+p.ENGINE_ROLES[role]],
                'arm':p.ENGINE_ROLES[role],'nonce':plan['nonce'],'build_id':'b1793109259-fixture',
                'checkpoint_sha256':'d'*64,'prepared':True,'guest':copy.deepcopy(p.PREP),'runtime_instance_id':actual['instance_id'],'prepared_at':self.clock.now()-.1}

    def supervisor_ready(self):
        self.staged();self.control.logs={}
        self.control.functional_script_sha256=lambda:p.controller.FUNCTIONAL_SCRIPT_SHA256
        self.control.run_functional=lambda tv,native,run,pilot,timed:{'passed':True,'timed_result_sha256':timed}
        for role in p.ENGINE_ROLES:
            receipt=self.prepared_exit(role)
            self.control.logs[receipt['machine_id']]=[{'marker':'QUALIFICATION_PREPARATION_COMPLETE','receipt':receipt}]
        self.control.generator_result={'passed':True,'production_contact':False,'arms':{'native':{},'tantivy':{}}}
        self.api.retain_destroyed=True
    def test_supervisor_advances_captures_destroyed_generator_and_cleans_once(self):
        self.supervisor_ready()
        result=self.pilot.advance_to_measurement('1234abcd5678')
        self.assertEqual(result['state'],'completed');self.assertEqual(result['cleanup']['status'],'cleaned')
        self.assertTrue(result['generator_result']['receipt']['passed']);self.assertFalse(result['generator_result']['truncated'])
        self.assertEqual(result['functional']['state'],'completed');self.assertFalse(self.api.machines);self.assertFalse(self.api.volumes)
        mutations=[c for c in self.api.calls if c[0]!='GET']
        again=self.pilot.advance_to_measurement('1234abcd5678')
        self.assertEqual(again,result);self.assertEqual(mutations,[c for c in self.api.calls if c[0]!='GET'])
    def test_supervisor_transient_same_id_reads_and_log_timeout_do_not_abort(self):
        self.supervisor_ready();_,plan=self.pilot.plan()
        self.api.get_failures[plan['resolved']['machine:native-control']]=2;self.control.log_timeouts=1
        result=self.pilot.advance_to_measurement('1234abcd5678')
        self.assertEqual(result['state'],'completed');self.assertFalse(self.api.machines)
        self.assertEqual(len([call for call in self.api.calls if call[0]=='POST' and call[2] is None and call[1]=='machines']),4)
    def test_supervisor_failed_result_preserved_and_cleanup_runs(self):
        self.supervisor_ready();self.control.generator_result['passed']=False
        result=self.pilot.advance_to_measurement('1234abcd5678')
        self.assertEqual(result['state'],'failed');self.assertEqual(result['error'],'supervisor_generator_reported_failure')
        self.assertFalse(result['generator_result']['receipt']['passed']);self.assertFalse(self.api.machines)
        self.assertEqual(result['functional']['state'],'completed');self.assertTrue(result['functional']['receipt']['passed'])
    def test_supervisor_missing_prep_receipt_is_bounded_and_cleans(self):
        self.supervisor_ready();self.control.logs={};start=self.clock.now()
        result=self.pilot.advance_to_measurement('1234abcd5678')
        self.assertEqual(result['state'],'failed');self.assertEqual(result['error'],'supervisor_preparation_receipt_missing')
        self.assertLess(self.clock.now()-start,40);self.assertFalse(self.api.machines)
    def test_supervisor_full_requires_functional_preflight_before_mutations(self):
        self.supervisor_ready();del self.control.run_functional;before=len(self.api.calls)
        with self.assertRaisesRegex(p.PilotError,'supervisor_full_functional_runner_required'):self.pilot.advance_to_measurement('1234abcd5678')
        self.assertEqual(len(self.api.calls),before)
    def test_supervisor_wrong_run_and_second_local_owner_do_not_mutate(self):
        self.supervisor_ready();before=len(self.api.calls)
        with self.assertRaisesRegex(p.PilotError,'supervisor_run_binding'):self.pilot.advance_to_measurement('ffffffff')
        with self.pilot.supervisor_lock():
            with self.assertRaisesRegex(p.PilotError,'another_supervisor_owns_lease'):self.pilot.advance_to_measurement('1234abcd5678')
        self.assertEqual(len(self.api.calls),before)
    def test_staged_preparation_full_lifecycle_preserves_volume_and_final_shape(self):
        self.staged();_,plan=self.pilot.plan()
        for role in p.ENGINE_ROLES:
            actual=self.pilot.owned_machine(role);self.assertEqual(actual['config']['guest'],p.PREP)
            self.assertEqual(actual['config']['env']['QUAL_PHASE'],'prepare');self.assertFalse(actual['config'].get('auto_destroy',False))
            with self.assertRaisesRegex(p.PilotError,'preparation_not_finished'):self.pilot.configure_engine(role,self.observer(role))
            with self.assertRaisesRegex(p.PilotError,'preparation_not_finished'):self.pilot.update_env(role,{'QUAL_OBSERVER_JSON':'{}'})
            receipt=self.prepared_exit(role)
            before=self.pilot.owned_machine(role);self.pilot.finish_preparation(role,receipt)
            resized=self.pilot.owned_machine(role)
            self.assertEqual(resized['id'],before['id']);self.assertEqual(resized['config']['guest'],p.ENGINE)
            self.assertEqual(resized['config']['mounts'],before['config']['mounts'])
            self.assertFalse(resized['config'].get('auto_destroy',False));self.assertEqual(resized['state'],'stopped')
            self.pilot.configure_engine(role,self.observer(role));final=self.pilot.owned_machine(role)
            self.assertTrue(final['config']['auto_destroy']);self.assertEqual(final['config']['env']['QUAL_PHASE'],'resume')
            self.assertEqual(final['config']['env']['QUAL_CHECKPOINT_SHA256'],receipt['checkpoint_sha256'])
        self.configure_all()
        with self.assertRaisesRegex(p.PilotError,'both_final_engines_must_be_running'):self.pilot.start('generator')
        for role in ['native-control','tantivy-control','generator']:self.pilot.start(role)
        self.assertEqual(len(self.api.volumes),2);self.assertEqual(len(self.api.machines),4)
        self.assertFalse(self.api.violations)
        self.assertEqual(self.pilot.cleanup()['status'],'cleaned')
    def test_staged_requires_full_opt_in_and_successful_authoritative_exit(self):
        self.control.save({'schema':1,'app':p.APP,'owner':p.OWNER,'machines':[],'volumes':[]})
        with self.assertRaisesRegex(p.PilotError,'staged_preparation_requires_full'):self.pilot.prepare(IMAGE,NATIVE,TANTIVY,staged_preparation=True)
        self.staged();receipt=self.prepared_exit('native-control');mid=receipt['machine_id']
        event=copy.deepcopy(self.api.machines[mid]['events'][0]);original=copy.deepcopy(receipt)
        posts=lambda:sum(call[0]=='POST' and call[2]==mid and call[3] is None for call in self.api.calls)
        before=posts()
        for key,value in [('exit_code',1),('guest_exit_code',1),('oom_killed',True),('requested_stop',True),('guest_error','failure'),('signal',9)]:
            self.api.machines[mid]['events']=[copy.deepcopy(event)];self.api.machines[mid]['events'][0]['request']['exit_event'][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(p.PilotError,'preparation_unsuccessful_exit'):self.pilot.finish_preparation('native-control',receipt)
        self.api.machines[mid]['events']=[event]
        for key,value in [('runtime_instance_id','different'),('checkpoint_sha256','invalid'),('guest',p.ENGINE),('nonce','different'),('prepared_at',self.clock.now()+10),('prepared',1)]:
            receipt=copy.deepcopy(original);receipt[key]=value
            with self.subTest(key=key),self.assertRaises(p.PilotError):self.pilot.finish_preparation('native-control',receipt)
        self.assertEqual(posts(),before)
        self.api.machines[mid]['events']=[copy.deepcopy(event)];self.api.machines[mid]['events'][0]['timestamp']=(original['prepared_at']-1)*1000
        with self.assertRaisesRegex(p.PilotError,'preparation_unsuccessful_exit'):self.pilot.finish_preparation('native-control',original)
        self.api.machines[mid]['events']=[]
        with self.assertRaisesRegex(p.PilotError,'preparation_exit_evidence_missing'):self.pilot.finish_preparation('native-control',original)
    def test_synthetic_staged_pilot_is_small_explicit_and_has_no_source_capability(self):
        self.control.save({'schema':1,'app':p.APP,'owner':p.OWNER,'machines':[],'volumes':[]})
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY,mode='full',seconds=900,oracle_binary_sha256=ORACLE,staged_preparation=True,synthetic_staged_pilot=True)
        _,plan=self.pilot.plan();self.assertEqual(plan['synthetic_actor_count'],128)
        self.assertNotIn('QUAL_SOURCE_ACCESS',plan['required_platform_secrets'])
        self.assertEqual(plan['manifest']['deadline']-self.clock.now(),900)
        self.provision()
        for role in ['native-control','tantivy-control','generator']:
            actual=self.pilot.owned_machine(role)
            self.assertEqual(actual['config']['env']['QUAL_SYNTHETIC_STAGED_PILOT'],'1')
            self.assertFalse(actual['config'].get('auto_destroy',False));self.assertEqual(actual['config']['services'],[])
        self.assertFalse(self.api.violations)
        args=p.arguments(['prepare','--image',IMAGE,'--native-binary-sha256',NATIVE,'--tantivy-binary-sha256',TANTIVY,'--mode','full','--seconds','900','--oracle-binary-sha256',ORACLE,'--staged-preparation','--synthetic-staged-pilot'])
        self.assertTrue(args.synthetic_staged_pilot)
    def test_synthetic_pilot_rejects_large_or_nonstaged_runs_before_http(self):
        for mode,staged,seconds in [('pilot',False,600),('full',False,900),('full',True,901)]:
            self.control.save({'schema':1,'app':p.APP,'owner':p.OWNER,'machines':[],'volumes':[]})
            with self.assertRaisesRegex(p.PilotError,'synthetic_staged_pilot_bounds'):
                self.pilot.prepare(IMAGE,NATIVE,TANTIVY,mode=mode,seconds=seconds,oracle_binary_sha256=ORACLE,staged_preparation=staged,synthetic_staged_pilot=True)
        self.assertFalse(self.api.calls)

    def test_staged_finish_never_extends_original_lease(self):
        self.staged();receipt=self.prepared_exit('native-control')
        _,plan=self.pilot.plan();deadline=plan['manifest']['deadline'];self.clock.value=deadline-60
        before=len([call for call in self.api.calls if call[0]=='POST'])
        with self.assertRaisesRegex(p.PilotError,'insufficient_pilot_lease'):self.pilot.finish_preparation('native-control',receipt)
        self.assertEqual(len([call for call in self.api.calls if call[0]=='POST']),before)
        self.assertEqual(self.pilot.plan()[1]['manifest']['deadline'],deadline)

    def test_staged_resize_lost_reply_and_transition_reconcile_without_repost(self):
        self.staged();self.api.lost_update['native-control']='created';self.api.update_transition['native-control']=2
        receipt=self.prepared_exit('native-control');mid=receipt['machine_id']
        self.api.lost_update['native-control']='created';self.api.update_transition['native-control']=2
        self.pilot.finish_preparation('native-control',receipt)
        posts=lambda:sum(call[0]=='POST' and call[2]==mid and call[3] is None for call in self.api.calls)
        before=posts();self.assertTrue(self.pilot.finish_preparation('native-control',receipt)['idempotent']);self.assertEqual(posts(),before)
        bad=dict(receipt,checkpoint_sha256='e'*64)
        with self.assertRaisesRegex(p.PilotError,'preparation_receipt_changed'):self.pilot.finish_preparation('native-control',bad)
    def test_staged_unknown_resize_never_reposts_or_enables_final_start(self):
        self.staged();receipt=self.prepared_exit('native-control');mid=receipt['machine_id']
        self.api.lost_update['native-control']='absent'
        with self.assertRaisesRegex(p.PilotError,'staged_transition_unconfirmed'):self.pilot.finish_preparation('native-control',receipt)
        before=sum(call[0]=='POST' and call[2]==mid and call[3] is None for call in self.api.calls)
        with self.assertRaisesRegex(p.PilotError,'staged_transition_unconfirmed'):self.pilot.finish_preparation('native-control',receipt)
        self.assertEqual(sum(call[0]=='POST' and call[2]==mid and call[3] is None for call in self.api.calls),before)
        with self.assertRaisesRegex(p.PilotError,'preparation_not_finished'):self.pilot.configure_engine('native-control',self.observer('native-control'))
        with self.assertRaisesRegex(p.PilotError,'configuration_required_before_start'):self.pilot.start('native-control')
        self.assertFalse(self.api.machines[mid]['config'].get('auto_destroy',False))
        status=self.pilot.reconcile();self.assertIn('native-control:resize',status['unresolved_staged_attempts'])
        self.api.machines[mid]['config']['guest']=copy.deepcopy(p.ENGINE)
        status=self.pilot.reconcile();self.assertEqual(status['unresolved_staged_attempts'],[]);self.assertEqual(status['staged_finish_receipt_required'],['native-control'])
        self.pilot.finish_preparation('native-control',receipt)
        self.assertEqual(sum(call[0]=='POST' and call[2]==mid and call[3] is None for call in self.api.calls),before)
        self.assertEqual(self.pilot.owned_machine('native-control')['config']['guest'],p.ENGINE)

    def test_default_pilot_remains_600_seconds_and_original_programs(self):
        _,plan=self.pilot.plan()
        self.assertEqual(plan['manifest']['mode'],'pilot')
        self.assertEqual(plan['manifest']['deadline']-self.clock.now(),600)
        self.assertEqual(set(plan['required_platform_secrets']),p.PILOT_SECRETS)
        self.provision()
        for role in ['native-control','tantivy-control','generator']:
            actual=self.pilot.owned_machine(role)
            self.assertEqual(actual['config']['env']['QUAL_MODE'],'pilot')
            self.assertIn('/app/generator.py' if role=='generator' else '/app/pilot_bootstrap.py',actual['config']['init']['exec'])
        args=p.arguments(['prepare','--image',IMAGE,'--native-binary-sha256',NATIVE,'--tantivy-binary-sha256',TANTIVY])
        self.assertEqual((args.mode,args.seconds),('pilot',600))

    def test_full_mode_http_provision_configuration_and_secret_contract(self):
        self.control.save({'schema':1,'app':p.APP,'owner':p.OWNER,'machines':[],'volumes':[]})
        result=self.pilot.prepare(IMAGE,NATIVE,TANTIVY,seconds=5400,mode='full',run_id='1234abcd5678',oracle_binary_sha256=ORACLE)
        self.assertEqual(result['deadline']-self.clock.now(),5400)
        self.assertFalse(self.api.calls)
        _,plan=self.pilot.plan()
        self.assertEqual(plan['manifest']['mode'],'full')
        self.assertEqual(set(plan['required_platform_secrets']),p.SECRETS)
        p.reaper.validate_manifest(plan['manifest'])
        with self.assertRaisesRegex(p.PilotError,'reaper_not_armed'):self.pilot.provision()
        self.provision();self.configure_all()
        for role in ['native-control','tantivy-control','generator']:
            before=self.pilot.owned_machine(role)
            self.assertEqual(before['state'],'created')
            self.assertIs(before['config']['auto_destroy'],True)
            self.assertEqual(before['config']['env']['QUAL_MODE'],'full')
            self.assertEqual(float(before['config']['env']['QUAL_DEADLINE']),result['deadline'])
            self.assertIn('/app/full_generator.py' if role=='generator' else '/app/full_bootstrap.py',before['config']['init']['exec'])
            self.assertEqual(before['config']['guest'],p.SMALL if role=='generator' else p.ENGINE)
            self.assertEqual(before['config']['services'],[])
            self.assertNotIn('QUAL_SOURCE_ACCESS',before['config']['env'])
            if role in {'native-control','generator'}:self.assertEqual(before['config']['env']['QUAL_ORACLE_BINARY_SHA256'],ORACLE)
            else:self.assertNotIn('QUAL_ORACLE_BINARY_SHA256',before['config']['env'])
            self.pilot.start(role)
        self.assertFalse(self.api.violations)
        self.assertEqual({v['size_gb'] for v in self.api.volumes.values()},{20})
        _,plan=self.pilot.plan();self.assertEqual(len(plan['started']),3)
        self.assertEqual(self.pilot.cleanup()['status'],'cleaned')

    def test_mode_lease_limits_and_cli_opt_in(self):
        for mode,seconds in [('pilot',601),('full',5401),('unknown',600),('full',True),('full',0)]:
            self.control.save({'schema':1,'app':p.APP,'owner':p.OWNER,'machines':[],'volumes':[]})
            with self.assertRaises(p.PilotError):self.pilot.prepare(IMAGE,NATIVE,TANTIVY,seconds=seconds,mode=mode,oracle_binary_sha256=ORACLE)
        self.assertFalse(self.api.calls)
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY,mode='full',oracle_binary_sha256=ORACLE)
        _,plan=self.pilot.plan();self.assertEqual(plan['manifest']['deadline']-self.clock.now(),600)
        args=p.arguments(['prepare','--image',IMAGE,'--native-binary-sha256',NATIVE,'--tantivy-binary-sha256',TANTIVY,'--mode','full','--seconds','5400','--oracle-binary-sha256',ORACLE])
        self.assertEqual((args.mode,args.seconds),('full',5400));self.assertEqual(args.oracle_binary_sha256,ORACLE)

    def test_full_requires_frozen_oracle_binding_before_any_allocation(self):
        for oracle in [None,'','x'*64,'a'*63]:
            self.control.save({'schema':1,'app':p.APP,'owner':p.OWNER,'machines':[],'volumes':[]})
            with self.assertRaisesRegex(p.PilotError,'frozen_oracle_binary_hash_required'):
                self.pilot.prepare(IMAGE,NATIVE,TANTIVY,mode='full',oracle_binary_sha256=oracle)
        self.assertFalse(self.api.calls)
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY)
        _,plan=self.pilot.plan();self.assertNotIn('oracle_binary_sha256',plan)
        self.assertNotIn('QUAL_ORACLE_BINARY_SHA256',self.pilot.env(plan,'native-control'))

    def test_source_access_is_never_accepted_inline_in_any_mode(self):
        self.provision()
        with self.assertRaisesRegex(p.PilotError,'public_configuration_only'):
            self.pilot.update_env('native-control',{'QUAL_SOURCE_ACCESS':'synthetic-not-a-real-capability'})
        actual=self.pilot.owned_machine('native-control')
        self.api.machines[actual['id']]['config']['env']['QUAL_SOURCE_ACCESS']='synthetic-not-a-real-capability'
        with self.assertRaisesRegex(p.PilotError,'reserved_or_inline_secret_environment'):self.pilot.owned_machine('native-control')
        self.assertNotIn('synthetic-not-a-real-capability',self.path.read_text())

    def test_reaper_first_writeahead_and_paused_matched_shapes(self):
        with self.assertRaisesRegex(p.PilotError,'reaper_not_armed'):self.pilot.provision()
        self.assertFalse(self.api.calls)
        self.provision();self.assertFalse(self.api.violations)
        creates=[c for c in self.api.calls if c[0]=='POST' and c[2] is None]
        self.assertEqual([c[1] for c in creates],['machines','volumes','volumes','machines','machines','machines'])
        self.assertEqual(creates[0][4]['config']['guest'],p.SMALL);self.assertFalse(creates[0][4]['config']['mounts'])
        for call in creates[3:]:self.assertTrue(call[4]['skip_launch']);self.assertFalse(call[4]['config']['auto_destroy']);self.assertEqual(call[4]['config']['services'],[])
        for role in p.ENGINE_ROLES:
            actual=self.pilot.owned_machine(role);self.assertEqual(actual['state'],'created');self.assertEqual(actual['config']['guest'],p.ENGINE)
        self.assertNotIn('ADMIN_SECRET',json.dumps([c[4] for c in creates]));self.assertNotIn('QUAL_CONTROL_SECRET',json.dumps([c[4] for c in creates]))
    def test_lost_create_response_reconciles_name_without_duplicate(self):
        self.api.lost['machine:reaper']='created';self.arm()
        self.api.lost['volume:native']='created';self.pilot.provision()
        creates=[c for c in self.api.calls if c[0]=='POST' and c[2] is None]
        self.assertEqual(len(creates),6);self.assertEqual(len(self.control.ledger()['volumes']),2)
    def test_unknown_create_absence_never_retried_or_early_reaper_retired(self):
        self.arm();self.api.lost['volume:native']='absent'
        with self.assertRaisesRegex(p.PilotError,'creation_outcome_unknown'):self.pilot.provision()
        count=sum(c[0]=='POST' and c[1]=='volumes' and c[2] is None for c in self.api.calls)
        with self.assertRaisesRegex(p.PilotError,'creation_outcome_unresolved'):self.pilot.provision()
        self.assertEqual(sum(c[0]=='POST' and c[1]=='volumes' and c[2] is None for c in self.api.calls),count)
        result=self.pilot.cleanup();self.assertEqual(result['status'],'cleanup_failed');self.assertTrue(result['reaper_retained'])
        self.assertIn(self.control.ledger()['pilot']['resolved']['machine:reaper'],self.api.machines)
    def test_configuration_attestation_then_start_and_no_secret_persistence(self):
        self.provision()
        with self.assertRaisesRegex(p.PilotError,'configuration_required'):self.pilot.start('native-control')
        self.configure_all()
        for role in ['native-control','tantivy-control','generator']:self.pilot.start(role)
        _,plan=self.pilot.plan();self.assertEqual(set(plan['started']),{'native-control','tantivy-control','generator'})
        native=self.api.machines[plan['resolved']['machine:native-control']]
        observer=json.loads(native['config']['env']['QUAL_OBSERVER_JSON']);self.assertNotIn('raw_should_not_be_copied',observer)
        gen=self.api.machines[plan['resolved']['machine:generator']]
        self.assertEqual(gen['config']['env']['QUAL_NATIVE_BINARY_SHA256'],NATIVE);self.assertEqual(gen['config']['env']['QUAL_TANTIVY_BINARY_SHA256'],TANTIVY)
    def test_final_update_enables_autodestroy_once_and_recovers_lost_reply(self):
        self.provision();_,plan=self.pilot.plan();mid=plan['resolved']['machine:native-control']
        self.assertFalse(self.api.machines[mid]['config']['auto_destroy'])
        observer=self.observer('native-control');self.api.lost_update['native-control']='committed'
        result=self.pilot.configure_engine('native-control',observer)
        self.assertTrue(result['auto_destroy']);self.assertTrue(self.api.machines[mid]['config']['auto_destroy'])
        updates=lambda:sum(c[0]=='POST' and c[2]==mid and c[3] is None for c in self.api.calls)
        self.assertEqual(updates(),1)
        self.pilot.configure_engine('native-control',observer);self.assertEqual(updates(),1)
        changed=observer|{'machines_api_response_sha256':'e'*64}
        with self.assertRaisesRegex(p.PilotError,'final_configuration_not_confirmed'):self.pilot.configure_engine('native-control',changed)
        self.assertEqual(updates(),1)
    def test_uncommitted_final_update_is_not_retried_or_started(self):
        self.provision();observer=self.observer('native-control');self.api.lost_update['native-control']='absent'
        with self.assertRaisesRegex(p.PilotError,'configuration_transition_unconfirmed_reconcile_only'):self.pilot.configure_engine('native-control',observer)
        _,plan=self.pilot.plan();mid=plan['resolved']['machine:native-control']
        before=sum(c[0]=='POST' and c[2]==mid and c[3] is None for c in self.api.calls)
        with self.assertRaisesRegex(p.PilotError,'outcome_unresolved_no_retry'):self.pilot.configure_engine('native-control',observer)
        self.assertEqual(sum(c[0]=='POST' and c[2]==mid and c[3] is None for c in self.api.calls),before)
        with self.assertRaisesRegex(p.PilotError,'configuration_required'):self.pilot.start('native-control')
        self.assertFalse(self.api.machines[mid]['config']['auto_destroy'])
        desired=next(c[4]['config'] for c in reversed(self.api.calls) if c[0]=='POST' and c[2]==mid and c[3] is None)
        self.api.machines[mid]['config']=copy.deepcopy(desired)
        reconciled=self.pilot.reconcile();self.assertFalse(reconciled['unresolved_configuration_attempts'])
        self.assertEqual(sum(c[0]=='POST' and c[2]==mid and c[3] is None for c in self.api.calls),before)
    def test_false_autodestroy_allowed_only_unconfigured_paused(self):
        self.provision();_,plan=self.pilot.plan();mid=plan['resolved']['machine:generator']
        self.api.machines[mid]['state']='started'
        with self.assertRaisesRegex(p.PilotError,'must_remain_paused'):self.pilot.owned_machine('generator')
        self.api.machines[mid]['state']='created';self.configure_all();self.api.machines[mid]['config']['auto_destroy']=False
        with self.assertRaisesRegex(p.PilotError,'auto_destroy_required'):self.pilot.start('generator')
    def test_omitted_false_response_is_paused_only_and_provision_is_idempotent(self):
        self.provision();_,plan=self.pilot.plan()
        self.api.machines[plan['resolved']['machine:native-control']]['state']='stopped'
        for role in ['native-control','tantivy-control','generator']:
            actual=self.pilot.owned_machine(role)
            self.assertNotIn('auto_destroy',actual['config']);self.assertIn(actual['state'],{'created','stopped'})
        reaper=self.pilot.owned_machine('reaper');self.assertIs(reaper['config']['auto_destroy'],True)
        creates=sum(c[0]=='POST' and c[2] is None for c in self.api.calls)
        self.pilot.provision();self.assertEqual(sum(c[0]=='POST' and c[2] is None for c in self.api.calls),creates)
        self.configure_all();mid=plan['resolved']['machine:generator'];self.api.machines[mid]['config'].pop('auto_destroy')
        with self.assertRaisesRegex(p.PilotError,'auto_destroy_required'):self.pilot.start('generator')
    def test_provider_transient_updating_old_false_is_observed_without_retry(self):
        self.provision();observer=self.observer('native-control');self.api.update_transition['native-control']=2
        started=self.clock.now();result=self.pilot.configure_engine('native-control',observer)
        self.assertTrue(result['auto_destroy']);self.assertEqual(self.clock.now()-started,2)
        _,plan=self.pilot.plan();mid=plan['resolved']['machine:native-control']
        self.assertEqual(self.api.machines[mid]['state'],'stopped')
        self.assertEqual(sum(c[0]=='POST' and c[2]==mid and c[3] is None for c in self.api.calls),1)
        self.assertEqual(plan['configuration_attempts']['native-control']['state'],'committed')
    def test_provider_neverending_update_has_bounded_wait_and_no_repost(self):
        self.provision();observer=self.observer('native-control');self.api.update_transition['native-control']=None
        started=self.clock.now()
        with self.assertRaisesRegex(p.PilotError,'configuration_transition_unconfirmed_reconcile_only'):self.pilot.configure_engine('native-control',observer)
        self.assertEqual(self.clock.now()-started,30)
        _,plan=self.pilot.plan();mid=plan['resolved']['machine:native-control']
        self.assertNotIn('native-control',plan.get('configured',{}))
        self.assertEqual(sum(c[0]=='POST' and c[2]==mid and c[3] is None for c in self.api.calls),1)
        with self.assertRaisesRegex(p.PilotError,'configuration_required'):self.pilot.start('native-control')
        reconciled=self.pilot.reconcile();self.assertIn('native-control',reconciled['unresolved_configuration_attempts'])
        self.assertEqual(sum(c[0]=='POST' and c[2]==mid and c[3] is None for c in self.api.calls),1)
    def test_explicit_null_autodestroy_is_not_canonical_false(self):
        self.provision();_,plan=self.pilot.plan();mid=plan['resolved']['machine:native-control']
        self.api.machines[mid]['config']['auto_destroy']=None
        with self.assertRaisesRegex(p.PilotError,'auto_destroy_must_be_explicit'):self.pilot.owned_machine('native-control')
    def test_owner_verified_destroyed_get_tombstones_do_not_hold_cleanup(self):
        self.provision();self.api.retain_destroyed=True
        result=self.pilot.cleanup();self.assertEqual(result['status'],'cleaned');self.assertTrue(result['reaper_retired'])
        self.assertFalse(self.api.machines);self.assertFalse(self.api.volumes)
        self.assertTrue(all(item['state']=='destroyed' for item in self.api.tombstones['machines'].values()))
        self.assertEqual(self.control.ledger()['phase'],'pilot-cleaned')
    def test_foreign_or_public_generator_urls_rejected_without_update(self):
        self.provision();_,plan=self.pilot.plan();a=plan['resolved']['machine:native-control'];b=plan['resolved']['machine:tantivy-control']
        def url(mid,port):return f'http://{mid}.vm.{p.APP}.internal:{port}'
        for bad in ['https://typeahead.waow.tech','http://fdaa::1','http://[fdaa::ffff]:8080',url('deadbeef',8080)]:
            before=len(self.api.calls)
            with self.assertRaises(p.PilotError):self.pilot.configure_generator(bad,url(a,8082),url(b,8080),url(b,8082))
            self.assertFalse(any(c[0]=='POST' for c in self.api.calls[before:]))
    def test_mount_derived_fields_accepted_but_independent_volume_size_checked(self):
        self.provision();actual=self.pilot.owned_machine('native-control');self.assertIn('unknown_safe_field',actual['config']['mounts'][0])
        _,plan=self.pilot.plan();vid=plan['resolved']['volume:native'];self.api.volumes[vid]['size_gb']=40
        with self.assertRaisesRegex(p.PilotError,'volume_shape_mismatch'):self.pilot.owned_machine('native-control')
    def test_wrong_armed_receipt_or_dead_reaper_blocks_disks(self):
        self.arm();value=self.control.ledger();value['pilot']['armed']['manifest_sha256']='e'*64;bad=value['pilot']['armed']
        with self.assertRaisesRegex(p.PilotError,'armed_binding'):self.pilot.record_armed(bad)
        _,plan=self.pilot.plan();self.api.machines[plan['resolved']['machine:reaper']]['state']='stopped'
        with self.assertRaisesRegex(p.PilotError,'reaper_not_running'):self.pilot.provision()
        self.assertFalse(self.api.volumes)
    def test_cleanup_receipt_persisted_before_reaper_retirement(self):
        self.provision();result=self.pilot.cleanup();self.assertEqual(result['status'],'cleaned');self.assertTrue(result['reaper_retired']);self.assertFalse(self.api.machines);self.assertFalse(self.api.volumes)
        value=self.control.ledger();self.assertEqual(value['pilot']['cleanup_receipt']['status'],'cleaned');self.assertEqual(value['phase'],'pilot-cleaned')
    def test_reaper_retirement_two_pending_polls_then_destroyed_mutates_once(self):
        self.provision();_,plan=self.pilot.plan();rid=plan['resolved']['machine:reaper'];self.api.retirement_delay[rid]=3
        started=self.clock.now();result=self.pilot.cleanup()
        self.assertEqual(result['status'],'cleaned');self.assertTrue(result['reaper_retired']);self.assertEqual(self.clock.now()-started,3)
        calls=[c for c in self.api.calls if c[2]==rid]
        self.assertEqual(sum(c[0]=='POST' and c[3]=='stop' for c in calls),1)
        self.assertEqual(sum(c[0]=='DELETE' for c in calls),1)
        deletion=next(i for i,c in enumerate(calls) if c[0]=='DELETE')
        self.assertTrue(all(c[0]=='GET' for c in calls[deletion+1:]))
        self.assertEqual(self.api.tombstones['machines'][rid]['state'],'destroyed')
        self.assertTrue(self.control.ledger()['pilot']['reaper_retired'])
    def test_neverterminal_reaper_retirement_is_bounded_and_unconfirmed(self):
        self.provision();_,plan=self.pilot.plan();rid=plan['resolved']['machine:reaper'];self.api.retirement_delay[rid]=None
        started=self.clock.now()
        with self.assertRaisesRegex(p.PilotError,'reaper_retirement_unconfirmed'):self.pilot.cleanup()
        self.assertEqual(self.clock.now()-started,30)
        calls=[c for c in self.api.calls if c[2]==rid]
        self.assertEqual(sum(c[0]=='POST' and c[3]=='stop' for c in calls),1)
        self.assertEqual(sum(c[0]=='DELETE' for c in calls),1)
        deletion=next(i for i,c in enumerate(calls) if c[0]=='DELETE')
        self.assertTrue(all(c[0]=='GET' for c in calls[deletion+1:]))
        value=self.control.ledger();self.assertFalse(value['pilot'].get('reaper_retired',False))
        self.assertEqual(value['pilot']['cleanup_receipt']['status'],'cleaned')
        self.assertNotEqual(value['phase'],'pilot-cleaned');self.assertTrue(value['machines'])
    def test_protected_or_unknown_app_resource_blocks_allocation(self):
        rogue={'id':'8e3d79b70dd748','name':'unknown','created_at':1000,'config':{'metadata':{'owner':'someone'}}}
        self.api.machines[rogue['id']]=rogue
        with self.assertRaisesRegex(p.PilotError,'unknown_app_resource'):self.pilot.launch_reaper()
        self.assertFalse(any(c[0]=='POST' for c in self.api.calls))
    def test_second_run_accepts_only_recorded_retired_unattached_pending_volumes(self):
        self.provision();self.api.retain_destroyed=True;self.api.pending_destroy_volumes=True
        self.assertEqual(self.pilot.cleanup()['status'],'cleaned');oldids=set(self.api.volumes)
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY,run_id='1234abcd5678')
        self.pilot.launch_reaper();self.assertEqual(len(self.control.ledger()['retired_pilots']),1)
        self.assertEqual(set(self.api.volumes),oldids)
        self.assertTrue(all(v['state']=='pending_destroy' for v in self.api.volumes.values()))
    def test_prior_confirmed_retirement_survives_provider_purge_state_changes(self):
        self.provision();self.api.pending_destroy_volumes=True;self.assertEqual(self.pilot.cleanup()['status'],'cleaned')
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY,run_id='1234abcd5678')
        volume=next(iter(self.api.volumes.values()))
        for state in ['scheduling_destroy','fork_cleanup','waiting_for_detach','destroying']:
            volume['state']=state
            self.assertTrue(self.pilot.retired_terminal('volume',volume,[]))
        value=self.control.ledger();value['retired_pilots'][0]['terminal_dispositions']=[];self.control.save(value)
        self.assertFalse(self.pilot.retired_terminal('volume',volume,[]))
        volume['attached_machine_id']='unrelated'
        self.assertFalse(self.pilot.retired_terminal('volume',volume,[]))

    def test_unrecorded_pending_or_live_retired_volume_blocks_new_allocation(self):
        self.provision();self.api.pending_destroy_volumes=True;self.assertEqual(self.pilot.cleanup()['status'],'cleaned')
        self.pilot.prepare(IMAGE,NATIVE,TANTIVY,run_id='1234abcd5678')
        original=copy.deepcopy(next(iter(self.api.volumes.values())));clone=copy.deepcopy(original);clone['id']='vol_unrecorded'
        self.api.volumes[clone['id']]=clone
        before=len(self.api.calls)
        with self.assertRaisesRegex(p.PilotError,'unknown_app_resource'):self.pilot.launch_reaper()
        self.assertFalse(any(c[0]=='POST' for c in self.api.calls[before:]))
        self.api.volumes.pop(clone['id']);self.api.volumes[original['id']]['state']='created'
        with self.assertRaisesRegex(p.PilotError,'unknown_app_resource'):self.pilot.launch_reaper()
        self.api.volumes[original['id']]['state']='pending_destroy';self.api.volumes[original['id']]['attached_machine_id']='unknown-machine'
        with self.assertRaisesRegex(p.PilotError,'unknown_app_resource'):self.pilot.launch_reaper()
    def test_expired_or_insufficient_lease_prevents_creation(self):
        self.clock.value=1531
        with self.assertRaisesRegex(p.PilotError,'insufficient_pilot_lease'):self.pilot.launch_reaper()
        self.assertFalse(self.api.machines)

if __name__=='__main__':unittest.main()
