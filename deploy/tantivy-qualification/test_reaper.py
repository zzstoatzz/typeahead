import copy
import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.parse

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('qualification_reaper',HERE/'reaper.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
TOKEN='synthetic-local-fixture-capability'
RUN='ab12cd34ef56'

class Clock:
    def __init__(self):self.value=1600.;self.sleeps=[];self.callback=None
    def now(self):return self.value
    def sleep(self,seconds):
        self.sleeps.append(seconds);self.value+=seconds
        if self.callback:self.callback(self.value)

def planned(role,kind='machine',rid=None):
    entry={'name':('tq-' if kind=='machine' else 'tq_')+RUN+('-' if kind=='machine' else '_')+role,'role':role}
    if rid:entry['id']=rid
    return entry

def manifest():
    return {'schema':1,'app':r.APP,'owner':r.OWNER,'run_id':RUN,'created_after':1000,'created_before':1500,'deadline':1600,
            'mode':'pilot','machines':[planned('native',rid='native1'),planned('tantivy',rid='tantivy1'),planned('generator',rid='generator1')],
            'volumes':[planned('native','volume','vol_native1'),planned('tantivy','volume','vol_tantivy1')],
            'reaper':planned('reaper')}

def machine(entry,rid=None):
    role=entry['role'];rid=rid or entry.get('id')
    return {'id':rid,'name':entry['name'],'created_at':'1970-01-01T00:18:20Z','state':'started',
            'config':{'metadata':{'owner':r.OWNER,'run_id':RUN,'role':role},'services':[],
                      'guest':{'cpu_kind':'shared','cpus':1,'memory_mb':256},'mounts':[],
                      'auto_destroy':True,'restart':{'policy':'no'},'env':{'PRIVATE_UNLOGGED':TOKEN}}}

def volume(entry,rid=None):
    return {'id':rid or entry.get('id'),'name':entry['name'],'created_at':'1970-01-01T00:18:20Z','attached_machine_id':None,'state':'created'}

class Fixture:
    def __init__(self,m):
        self.m=m;self.machines={e['id']:machine(e) for e in m['machines'] if e.get('id')}
        self.machines['reaper1']=machine(m['reaper'],'reaper1')
        self.volumes={e['id']:volume(e) for e in m['volumes'] if e.get('id')}
        self.requests=[];self.mutations=[];self.fail={};self.list_calls=0;self.on_list=None;self.slow=False;self.redirect=False;self.retain_destroyed=False;self.pending_destroy=False;self.tombstones={}
        for engine in ['native','tantivy']:
            if engine+'1' in self.machines and 'vol_'+engine+'1' in self.volumes:
                self.machines[engine+'1']['config']['mounts']=[{'volume':'vol_'+engine+'1','path':'/data'}]
                self.volumes['vol_'+engine+'1']['attached_machine_id']=engine+'1'
        fixture=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_call(self):
                url=urllib.parse.urlsplit(self.path);prefix='/v1/apps/'+r.APP+'/'
                fixture.requests.append((self.command,self.path,hashlib.sha256(self.headers.get('Authorization','').encode()).hexdigest()))
                if not url.path.startswith(prefix):self.respond(403,{'error':TOKEN});return
                if fixture.redirect:
                    self.send_response(302);self.send_header('Location','https://api.machines.dev/v1/apps/typeahead-tantivy-dev/machines');self.send_header('Content-Length','0');self.end_headers();return
                tail=url.path[len(prefix):].split('/');kind=tail[0];rid=tail[1] if len(tail)>1 else None
                key=(self.command,kind,rid)
                if fixture.fail.get(key,0):
                    fixture.fail[key]-=1;self.respond(500,{'error':TOKEN});return
                if fixture.slow:time.sleep(.2)
                records=fixture.machines if kind=='machines' else fixture.volumes
                if self.command=='GET':
                    if rid is None:
                        fixture.list_calls+=1
                        if fixture.on_list:fixture.on_list(fixture)
                        self.respond(200,list(records.values()))
                    else:
                        actual=records.get(rid,fixture.tombstones.get(rid))
                        self.respond(200 if actual is not None else 404,actual or {})
                elif self.command=='POST':
                    fixture.mutations.append((self.command,kind,rid))
                    if rid not in records:self.respond(404,{});return
                    records[rid]['state']='stopped';self.respond(200,{})
                elif self.command=='DELETE':
                    fixture.mutations.append((self.command,kind,rid))
                    if rid not in records:self.respond(404,{});return
                    if kind=='machines':
                        removed=records.pop(rid)
                        if fixture.retain_destroyed:removed['state']='destroyed';fixture.tombstones[rid]=removed
                        for v in fixture.volumes.values():
                            if v.get('attached_machine_id')==rid:v['attached_machine_id']=None
                    else:
                        if records[rid].get('attached_machine_id'):self.respond(409,{});return
                        if fixture.pending_destroy:records[rid]['state']='pending_destroy'
                        else:records.pop(rid)
                    self.respond(200,{})
            def respond(self,status,body):
                data=json.dumps(body).encode()
                try:
                    self.send_response(status);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
            do_GET=handle_call;do_POST=handle_call;do_DELETE=handle_call
            def log_message(self,*args):pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)+'/v1/apps/'+r.APP+'/'
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

class ReaperTests(unittest.TestCase):
    def setUp(self):
        self.m=manifest();self.clock=Clock();self.fixture=Fixture(self.m);self.events=[]
        self.api=r.API(TOKEN,self.fixture.base,self.clock.now)
        self.reaper=r.Reaper(self.m,self.api,now=self.clock.now,monotonic=self.clock.now,sleep=self.clock.sleep,emit=self.events.append)
    def tearDown(self):self.fixture.close()
    def clean(self,**kw):return self.reaper.cleanup(**kw)
    def test_success_owner_checked_detach_before_volume_and_idempotent(self):
        unknown=planned('unrelated',rid='unrelated1');self.fixture.machines['unrelated1']=machine(unknown)
        result=self.clean(creation_settled=True)
        self.assertEqual(result['status'],'cleaned');self.assertEqual(set(self.fixture.machines),{'reaper1','unrelated1'});self.assertFalse(self.fixture.volumes)
        deleted=[item for item in self.fixture.mutations if item[0]=='DELETE']
        self.assertEqual([item[1] for item in deleted],['machines']*3+['volumes']*2)
        count=len(self.fixture.mutations);again=self.clean(creation_settled=True)
        self.assertEqual(again['status'],'cleaned');self.assertEqual(len(self.fixture.mutations),count)
        self.assertEqual(len(self.events),5)
        self.assertTrue(all(path.startswith('/v1/apps/'+r.APP+'/') for _,path,_ in self.fixture.requests))
        self.assertTrue(all(auth==hashlib.sha256(('Bearer '+TOKEN).encode()).hexdigest() for _,_,auth in self.fixture.requests))
    def test_pending_names_survive_controller_crash_before_id_save(self):
        for entry in self.m['machines']+self.m['volumes']:entry.pop('id')
        self.reaper=r.Reaper(self.m,self.api,now=self.clock.now,monotonic=self.clock.now,sleep=self.clock.sleep,emit=self.events.append)
        result=self.clean()
        self.assertEqual(result['status'],'cleaned');self.assertEqual(len(result['resolved']),5)
        self.assertFalse(result['remaining']);self.assertEqual(self.clock.sleeps,[])
    def test_writeahead_pending_late_creation_is_resolved_then_reaped(self):
        self.m['machines']=[planned('native')];self.m['volumes']=[planned('native','volume')]
        self.fixture.machines={'reaper1':self.fixture.machines['reaper1']};self.fixture.volumes={}
        def create(value):
            if value>=1610 and 'native1' not in self.fixture.machines and not self.fixture.mutations:
                self.fixture.machines['native1']=machine(self.m['machines'][0],'native1')
                self.fixture.volumes['vol_native1']=volume(self.m['volumes'][0],'vol_native1')
        self.clock.callback=create
        result=self.clean();self.assertEqual(result['status'],'cleaned');self.assertGreaterEqual(self.clock.now(),1610)
        self.assertIn(('DELETE','machines','native1'),self.fixture.mutations);self.assertIn(('DELETE','volumes','vol_native1'),self.fixture.mutations)
    def test_never_created_pending_waits_bounded_grace_not_false_early_success(self):
        self.m['machines']=[planned('native')];self.m['volumes']=[];self.fixture.machines={'reaper1':self.fixture.machines['reaper1']}
        result=self.clean();self.assertEqual(result['status'],'cleaned');self.assertEqual(self.clock.now(),1900)
        self.assertFalse(self.fixture.mutations)
    def test_all_four_protected_ids_rejected_without_api(self):
        before=len(self.fixture.requests)
        for protected in r.PROTECTED_MACHINES:
            value=manifest();value['machines'][0]['id']=protected
            with self.assertRaisesRegex(r.SafeError,'protected_resource_id'):r.validate_manifest(value)
        self.assertEqual(len(self.fixture.requests),before)
    def test_wrong_owner_creation_or_name_never_mutated(self):
        for corrupt in ['owner','created','name','app']:
            actual=self.fixture.machines['native1'];backup=copy.deepcopy(actual)
            if corrupt=='owner':actual['config']['metadata']['owner']='someone-else'
            elif corrupt=='created':actual['created_at']=999
            elif corrupt=='name':actual['name']='not-the-planned-name'
            else:actual['app_name']='different-app'
            before=len(self.fixture.mutations);result=self.clean(creation_settled=True,grace=5)
            self.assertEqual(result['status'],'cleanup_failed');self.assertNotIn(('DELETE','machines','native1'),self.fixture.mutations[before:])
            self.fixture.machines['native1']=backup
    def test_ambiguous_pending_volume_is_never_deleted(self):
        self.m['machines']=[];self.m['volumes']=[planned('native','volume')]
        self.fixture.volumes={'vol_one':volume(self.m['volumes'][0],'vol_one'),'vol_two':volume(self.m['volumes'][0],'vol_two')}
        result=self.clean(creation_settled=True,grace=5)
        self.assertEqual(result['status'],'cleanup_failed');self.assertIn('ambiguous_planned_name',[e['class'] for e in result['failures']])
        self.assertFalse(any(kind=='volumes' for _,kind,_ in self.fixture.mutations))
    def test_wrong_explicit_id_cannot_hide_existing_planned_name(self):
        self.m['machines']=[planned('native',rid='typoid')];self.m['volumes']=[]
        result=self.clean(creation_settled=True,grace=5)
        self.assertEqual(result['status'],'cleanup_failed');self.assertIn('api_resource_id_mismatch',[e['class'] for e in result['failures']]);self.assertFalse(self.fixture.mutations)
    def test_unknown_machine_mount_blocks_volume_deletion(self):
        self.m['machines']=[];self.m['volumes']=[planned('native','volume','vol_native1')]
        self.fixture.volumes['vol_native1']['attached_machine_id']=None
        self.fixture.machines['native1']['config']['metadata']['owner']='unknown-owner'
        result=self.clean(creation_settled=True,grace=5)
        self.assertEqual(result['status'],'cleanup_failed');self.assertNotIn(('DELETE','volumes','vol_native1'),self.fixture.mutations)
    def test_transient_api_failure_retries_but_permanent_failure_reports_remaining(self):
        self.fixture.fail[('DELETE','machines','native1')]=1
        result=self.clean(creation_settled=True);self.assertEqual(result['status'],'cleaned');self.assertGreater(result['attempts'],1)
        self.fixture.machines['native1']=machine(self.m['machines'][0]);self.fixture.fail[('DELETE','machines','native1')]=1000
        result=self.clean(creation_settled=True,grace=20)
        self.assertEqual(result['status'],'cleanup_failed');self.assertIn('native1',result['remaining_whitelist_ids'])
        self.assertNotIn(TOKEN,json.dumps(result));self.assertLessEqual(result['attempts'],5)
    def test_self_is_private_small_autodestroy_and_is_never_api_deleted(self):
        self.assertEqual(self.reaper.verify_self('reaper1')['id'],'reaper1')
        result=self.clean(creation_settled=True);self.assertEqual(result['self']['auto_destroy'],True)
        self.assertNotIn(('DELETE','machines','reaper1'),self.fixture.mutations)
        self.fixture.machines['reaper1']['config']['auto_destroy']=False
        with self.assertRaisesRegex(r.SafeError,'reaper_runtime_shape'):self.reaper.verify_self('reaper1')
    def test_wait_is_independent_monotonic_and_bounded(self):
        self.clock.value=1100;self.reaper.wait();self.assertEqual(self.clock.now(),1600);self.assertTrue(all(s<=30 for s in self.clock.sleeps))
    def test_http_error_and_redirect_do_not_expose_capability(self):
        self.fixture.fail[('GET','machines','native1')]=1
        with self.assertRaises(r.APIError) as caught:self.api.request('GET','machines','native1')
        self.assertNotIn(TOKEN,str(caught.exception));self.assertEqual(caught.exception.code,'api_http_500')
        self.fixture.redirect=True;before=len(self.fixture.requests)
        with self.assertRaises(r.APIError) as caught:self.api.request('GET','machines','native1')
        self.assertEqual(caught.exception.code,'api_http_302');self.assertEqual(len(self.fixture.requests),before+1)
        with self.assertRaises(r.SafeError):r.API(TOKEN,'http://example.com/v1/apps/'+r.APP+'/')
        with self.assertRaises(r.SafeError) as caught:r.API('secret\nnot-printed',self.fixture.base)
        self.assertEqual(str(caught.exception),'invalid_reaper_capability_shape')
    def test_cleanup_deadline_with_real_slow_http_reports_remaining(self):
        self.fixture.slow=True
        api=r.API(TOKEN,self.fixture.base,request_timeout=.2)
        worker=r.Reaper(self.m,api)
        started=time.monotonic();result=worker.cleanup(creation_settled=True,grace=.08)
        self.assertLess(time.monotonic()-started,.3);self.assertEqual(result['status'],'cleanup_failed')
        self.assertIn('native1',result['remaining_whitelist_ids']);self.assertNotIn(TOKEN,json.dumps(result))
    def test_destroyed_get_tombstones_and_nonbillable_pending_volumes(self):
        self.fixture.retain_destroyed=True;self.fixture.pending_destroy=True
        result=self.clean(creation_settled=True)
        self.assertEqual(result['status'],'cleaned');self.assertEqual(set(self.fixture.machines),{'reaper1'})
        self.assertEqual(len(self.fixture.tombstones),3)
        self.assertTrue(all(row['state']=='destroyed' for row in self.fixture.tombstones.values()))
        self.assertEqual(len(result['volume_dispositions']),2)
        self.assertTrue(all(row['non_billable'] and not row['physical_purge_confirmed'] for row in result['volume_dispositions']))
        self.assertTrue(all(v['state']=='pending_destroy' and not v['attached_machine_id'] for v in self.fixture.volumes.values()))
    def test_pending_destroy_not_accepted_while_attached_or_live_mounted(self):
        self.m['machines']=[];self.m['volumes']=[planned('native','volume','vol_native1')]
        self.fixture.volumes['vol_native1']['state']='pending_destroy'
        result=self.clean(creation_settled=True,grace=5)
        self.assertEqual(result['status'],'cleanup_failed');self.assertIn('volume_still_attached',[row['class'] for row in result['failures']])
        self.fixture.volumes['vol_native1']['attached_machine_id']=None
        result=self.clean(creation_settled=True,grace=5)
        self.assertEqual(result['status'],'cleanup_failed');self.assertIn('volume_still_mounted',[row['class'] for row in result['failures']])
        self.assertFalse(self.fixture.mutations)
        self.fixture.machines['native1']['config']['mounts']=[]
        result=self.clean(creation_settled=True,grace=5)
        self.assertEqual(result['status'],'cleaned');self.assertEqual(result['volume_dispositions'][0]['physical_purge_confirmed'],False)
        self.assertFalse(self.fixture.mutations)
    def test_real_http_timeout_is_bounded(self):
        self.fixture.slow=True
        api=r.API(TOKEN,self.fixture.base,request_timeout=.05)
        started=time.monotonic()
        with self.assertRaises(r.APIError):api.request('GET','machines','native1')
        self.assertLess(time.monotonic()-started,.3)

if __name__=='__main__':unittest.main()
