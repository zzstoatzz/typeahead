import copy
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
MODULE=Path(os.environ.get('TA_SLOT_MODULE',HERE/'tantivy-slot.py'))
sys.path.insert(0,str(MODULE.parent))
spec=importlib.util.spec_from_file_location('tantivy_slot',Path(os.environ.get('TA_SLOT_MODULE',HERE/'tantivy-slot.py')));slot=importlib.util.module_from_spec(spec);spec.loader.exec_module(slot)
import test_tantivy_install as fixtures
adapter=slot.installer
smoke=fixtures.smoke
ROOT=HERE.parents[1]
BINARY=Path(os.environ.get('TA_BINARY',ROOT/'services/tantivy/target/release/typeahead-tantivy')).resolve()

class Store:
    def __init__(self,objects):
        self.objects=objects;self.keys=[];outer=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                key=self.path[1:];outer.keys.append(key)
                if key not in outer.objects:self.send_error(404);return
                raw=outer.objects[key];self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def log_message(self,*args):pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def download(self,key,path,max_bytes):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.server.server_port}/'+key,timeout=3) as response:path.write_bytes(response.read(max_bytes+1))
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(2)

@unittest.skipUnless(shutil.which('rclone'),'actual rclone unavailable')
class TransferTests(unittest.TestCase):
    def test_real_rclone_resumes_interrupted_http_range(self):
        raw=bytes(range(256))*8192
        offsets=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200);self.send_header('Content-Type','application/octet-stream' if self.path.endswith('/index.tar') else 'text/html');self.send_header('Content-Length',str(len(raw)));self.end_headers()
            def do_GET(self):
                if not self.path.endswith('/index.tar'):
                    listing=b'<a href="index.tar">index.tar</a>';self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(listing)));self.end_headers();self.wfile.write(listing);return
                value=self.headers.get('Range','bytes=0-');start=int(value.split('=')[1].split('-')[0]);offsets.append(start)
                self.send_response(206 if start else 200)
                self.send_header('Content-Length',str(len(raw)-start))
                if start:self.send_header('Content-Range',f'bytes {start}-{len(raw)-1}/{len(raw)}')
                self.end_headers()
                self.wfile.write(raw[start:start+65536] if len(offsets)==1 else raw[start:])
                self.wfile.flush();self.close_connection=True
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                folder=Path(folder);config=folder/'rclone.conf'
                config.write_text(f'[r2]\ntype = http\nurl = http://127.0.0.1:{server.server_port}/\n')
                transport=slot.R2.__new__(slot.R2);transport.bucket='fixture';transport.config=config.open()
                try:
                    target=folder/'index.tar';target.write_bytes(raw[:32768])
                    transport.download('index.tar',target,len(raw))
                    self.assertEqual(target.read_bytes(),raw)
                    self.assertEqual(offsets[0],32768)
                    self.assertTrue(any(offset>32768 for offset in offsets),offsets)
                finally:transport.close()
        finally:server.shutdown();server.server_close();thread.join()

@unittest.skipUnless(BINARY.is_file(),'actual Rust binary unavailable')
class SlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='tantivy-slot-');root=Path(cls.temp.name);data=root/'seed';seed=root/'seed.jsonl';fixture=fixtures.AdmissionTests();manifest=fixture.manifest();now=int(time.time())
        manifest['binary_sha256']=slot.checksum(BINARY)
        actor=smoke.actor(1,'real.test','Real Actor');raw=json.dumps(actor,separators=(',',':')).encode()+b'\n';manifest['source_actors_sha256']=hashlib.sha256(raw).hexdigest()
        header={'type':'manifest','build_id':manifest['active_build'],'source_watermark':100,'actor_count':1,'source_actor_count':1,'normalize_version':manifest['normalize_version'],'source_database_sha256':manifest['source_database_sha256'],'source_prefix_sha256':manifest['source_prefix_sha256'],'partial':False}
        seed.write_bytes(json.dumps(header).encode()+b'\n'+raw+json.dumps({'type':'end','build_id':header['build_id'],'actor_count':1,'actors_sha256':manifest['source_actors_sha256']}).encode()+b'\n')
        result=subprocess.run([str(BINARY),'import','--data',str(data),'--input',str(seed)],capture_output=True,timeout=30)
        if result.returncode:raise RuntimeError('fixture import failed')
        secret=secrets.token_urlsafe(24)
        with (root/'server.log').open('wb') as log:
            child,public,admin=fixtures.start_strict(BINARY,data,log,secret)
            try:
                state=smoke.request(admin,'/admin/source-state',{'dids':[]},secret)[2];cursor=fixture.status()['source_cursor'];cursor.update(through=now,actors={'updated_at':now,'did':''},tombstones={'deleted_at':now,'did':''},lookback_seconds=300)
                cursor['reconciliation'].update(last_completed_at=now-1,last_completed_started_at=now-10,source_after_did='',backend_after_did='',source_done=True,backend_done=True,round=1,round_started_at=now-10)
                body={'actors':[],'expected_documents':[],'expected_source_cursor':state['source_cursor'],'source_cursor':cursor,'expected_observation_revision':state['source_observation_revision'],'batch_id':hashlib.sha256(b'slot-qualified').hexdigest(),'watermark':now,'policy_snapshot':{'schema_version':1,'source_observed_at':now,'overrides':[],'blocked_domains':[],'complete':True}}
                code,_,reply=smoke.request(admin,'/admin/source-reconcile',body,secret)
                if code!=200:raise RuntimeError('fixture source reconcile failed '+str(reply))
            finally:smoke.stop(child)
        source={'manifest_version':2,'format_version':5,'active_build':manifest['source_build_id'],'source_watermark':100,'actor_count':1,'sha256':manifest['source_database_sha256'],'normalize_version':manifest['normalize_version'],'prefix_map':{'sha256':manifest['source_prefix_sha256']},'legacy_twin':{'build_id':manifest['legacy_build_id'],'source_watermark':100}}
        source_bytes=json.dumps(source).encode();manifest['source_manifest_sha256']=hashlib.sha256(source_bytes).hexdigest()
        portable=adapter.freeze_bundle(data,root/'published',manifest,source_bytes,BINARY);portable_bytes=(root/'published/manifest.json').read_bytes()
        cls.request='a'*32;cls.app='typeahead-tantivy-a';cls.key=f'control/tantivy-slots/{cls.app}/{cls.request}.json'
        cls.bound={**portable,'provider_app':cls.app,'image_digest':'registry.fly.io/typeahead-tantivy-a@sha256:'+'b'*64,'guest':adapter.GUEST,'volume_id':'vol_fixture','volume_size_gib':20,'public_service_ports':[80,443],'deployment_schema_version':1,'request_id':cls.request,'artifact_manifest_key':f"releases/search-tantivy/{portable['active_build']}/manifest.json",'artifact_manifest_sha256':hashlib.sha256(portable_bytes).hexdigest(),'source_manifest_key':f"releases/search/{portable['source_build_id']}/manifest.json"}
        cls.deployment=json.dumps(cls.bound,separators=(',',':')).encode()
        cls.objects={cls.key:cls.deployment,cls.bound['artifact_manifest_key']:portable_bytes,cls.bound['source_manifest_key']:source_bytes,portable['object_key']:(root/'published/index.tar').read_bytes()}
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):
        self.work=tempfile.TemporaryDirectory();self.store=Store(copy.deepcopy(self.objects));self.root=Path(self.work.name).resolve()/'tantivy'
        self.env={'FLY_APP_NAME':self.app,'FLY_MACHINE_ID':'abcdef12345678','FLY_IMAGE_REF':self.bound['image_digest'],'EXPECTED_BUILD_ID':self.bound['active_build'],'EXPECTED_DEPLOYMENT_SHA256':hashlib.sha256(self.deployment).hexdigest(),'TANTIVY_DEPLOYMENT_KEY':self.key}
    def tearDown(self):self.store.close();self.work.cleanup()
    def instance(self,env=None):return slot.Slot(env or self.env,self.root,BINARY,self.store)
    def test_real_install_cached_restart_and_reader_corruption(self):
        value=self.instance().install();receipt=json.loads((self.root/'installation.json').read_bytes())
        self.assertTrue(receipt['passed']);self.assertFalse(receipt['runtime_qualified']);self.assertEqual(receipt['deployment_sha256'],self.env['EXPECTED_DEPLOYMENT_SHA256']);self.assertEqual(len(self.store.keys),4)
        self.assertFalse((self.root.parent/'.tantivy-slot/index.tar').exists())
        self.assertEqual(self.instance().install(),value);self.assertEqual(len(self.store.keys),4)
        code='import importlib.util,json,sys;sys.path.insert(0,sys.argv[1]);s=importlib.util.spec_from_file_location("slot",sys.argv[1]+"/tantivy-slot.py");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.Slot(json.loads(sys.argv[2]),m.Path(sys.argv[3]),m.Path(sys.argv[4])).install()'
        restarted=subprocess.run([sys.executable,'-c',code,str(MODULE.parent),json.dumps(self.env),str(self.root),str(BINARY)],capture_output=True,timeout=30)
        self.assertEqual(restarted.returncode,0,restarted.stderr);self.assertEqual(len(self.store.keys),4)
        with (Path(self.work.name)/'running.log').open('wb') as log:
            child,public,admin=fixtures.start_strict(BINARY,self.root,log,secrets.token_urlsafe(24))
            try:
                self.assertEqual(smoke.request(public,'/search?q=real')[0],200)
                adapter.validate_runtime(smoke.request(public,'/index/status')[2],value)
            finally:smoke.stop(child)
        (self.root/'index/meta.json').write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'reader rejected'):self.instance().install()
        self.assertEqual(len(self.store.keys),4)
    def test_verified_partial_transfer_resumes_before_install(self):
        instance=self.instance();instance.cache.mkdir()
        for key,name in [(self.key,'deployment.json'),(self.bound['artifact_manifest_key'],'artifact-manifest.json'),(self.bound['source_manifest_key'],'source-manifest.json')]:
            (instance.cache/name).write_bytes(self.store.objects[key])
        (instance.cache/'index.tar').write_bytes(self.store.objects[self.bound['object_key']][:100])
        instance.install()
        self.assertEqual(self.store.keys,[self.bound['object_key']])
        self.assertTrue((self.root/'installation.json').is_file())

    def test_actual_slot_reader_acknowledges_global_merge(self):
        portable=json.loads(self.store.objects[self.bound['artifact_manifest_key']]);portable['retrieval_strategy']='global-merge'
        portable_raw=json.dumps(portable).encode();self.store.objects[self.bound['artifact_manifest_key']]=portable_raw
        bound=dict(self.bound,retrieval_strategy='global-merge',artifact_manifest_sha256=hashlib.sha256(portable_raw).hexdigest())
        raw=json.dumps(bound).encode();self.store.objects[self.key]=raw;self.env['EXPECTED_DEPLOYMENT_SHA256']=hashlib.sha256(raw).hexdigest();self.env['ADMIN_SECRET']=secrets.token_urlsafe(24)
        instance=self.instance();value=instance.install();public,admin=smoke.port(),smoke.port()
        child=instance.start_reader(value,f'127.0.0.1:{public}',f'127.0.0.1:{admin}')
        try:
            for _ in range(100):
                try:code,headers,status=smoke.request(f'http://127.0.0.1:{public}','/index/status');break
                except OSError:time.sleep(.02)
            self.assertEqual({key.lower():value for key,value in headers.items()}['x-typeahead-retrieval-strategy'],'global-merge')
            self.assertEqual(status['retrieval_strategy'],'global-merge')
            self.assertEqual(smoke.request(f'http://127.0.0.1:{public}','/search?q=real')[0],200)
        finally:instance.stop()
    def test_separate_process_cached_restart_continues_actual_policy_polling(self):
        self.instance().install()
        sys.path.insert(0,str(ROOT/'services/tantivy-input'));import test_sync
        with tempfile.TemporaryDirectory() as directory:
            fixture=test_sync.Fixture(Path(directory));fixture.actor('did:example:local',fixture.floor+1)
            try:
                program="import importlib.util,json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1]);spec=importlib.util.spec_from_file_location('slot',sys.argv[1]+'/tantivy-slot.py');slot=importlib.util.module_from_spec(spec);spec.loader.exec_module(slot)\nslot.Slot(json.loads(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4])).install()\nsys.path.insert(0,sys.argv[5]);import sync,current_state,budget\nsource=budget.accounted_source(sync,sys.argv[6],sys.argv[7],Path(sys.argv[3])/'source-budget.json','fixture',536870912,allow_test_loopback=True,window_seconds=3600,min_request_interval=0)\ntry:\n result=current_state.CurrentPolicySource(source,sync).observe_with_policy(['did:example:local']);assert result['observations'][0]['actor']['did']=='did:example:local'\n print(json.dumps(source.accounting()))\nfinally:source.close()\n"
                args=[sys.executable,'-c',program,str(MODULE.parent),json.dumps(self.env),str(self.root),str(BINARY),os.environ.get('TA_INPUT_ROOT',str(ROOT/'services/tantivy-input')),fixture.url,fixture.source_secret]
                def poll():
                    child=subprocess.run(args,capture_output=True,timeout=30);self.assertEqual(child.returncode,0,child.stderr);return json.loads(child.stdout)
                first=poll();second=poll()
                self.assertEqual(len(fixture.requests),2);self.assertEqual(len(self.store.keys),4)
                self.assertGreater(second['source_bytes'],first['source_bytes']);self.assertEqual(second['window_started_at'],first['window_started_at']);self.assertEqual(second['max_source_bytes'],536870912)
                self.assertEqual(second['recovered_reservation_bytes'],0);self.assertEqual(second['poll_reserve_bytes'],134217728)
            finally:fixture.close()
    @unittest.skipUnless(os.environ.get('TA_SLOT_SERVE_TEST')=='1','exact guest paths/ports are exercised in isolated Linux container')
    def test_actual_serve_source_polling_survives_separate_process_restart(self):
        self.instance().install();sys.path.insert(0,str(ROOT/'services/tantivy-input'));import test_sync
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);fixture=test_sync.Fixture(work,now=int(time.time()));fixture.actor('did:plc:fixture000001',fixture.floor+1)
            key=work/'key.pem';cert=work/'cert.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=10)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key);fixture.server.socket=context.wrap_socket(fixture.server.socket,server_side=True)
            env={**os.environ,**self.env,'ADMIN_SECRET':fixture.admin_secret,'TURSO_URL':fixture.url.replace('http:','https:'),'TURSO_AUTH_TOKEN':fixture.source_secret,'SSL_CERT_FILE':str(cert)}
            program="import importlib.util,os,signal,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1]);spec=importlib.util.spec_from_file_location('slot','/app/tantivy-slot.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\ninstance=module.Slot(os.environ,Path(sys.argv[2]),Path(sys.argv[3]))\ndef stop(*_):raise KeyboardInterrupt()\nsignal.signal(signal.SIGTERM,stop)\ntry:instance.serve(instance.install())\nexcept KeyboardInterrupt:pass\nfinally:instance.stop()\n"
            def run_until_polled(previous_requests):
                child=subprocess.Popen([sys.executable,'-c',program,str(MODULE.parent),str(self.root),str(BINARY)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                try:
                    until=time.monotonic()+15
                    while time.monotonic()<until:
                        self.assertIsNone(child.poll())
                        ledger=self.root/'source-budget.json';status=self.root/'source-status.json'
                        if any(request['sql']==test_sync.sync.CURRENT_POLICY_SQL for request in fixture.requests[previous_requests:]) and ledger.exists() and status.exists():
                            state=json.loads(ledger.read_bytes())
                            if not state['pending_reservation']:return state
                        time.sleep(.05)
                    self.fail('actual source polling did not resume: '+(self.root/'source.log').read_text()[-3000:])
                finally:
                    child.terminate()
                    try:child.wait(12)
                    except subprocess.TimeoutExpired:child.kill();child.wait(5)
            try:
                time.sleep(max(0,fixture.now+3-time.time()));fixture.now=int(time.time())
                first=run_until_polled(0);requests=len(fixture.requests)
                time.sleep(max(0,fixture.now+3-time.time()));fixture.now=int(time.time())
                second=run_until_polled(requests)
                self.assertGreater(len(fixture.requests),requests);self.assertEqual(len(self.store.keys),4)
                self.assertGreater(second['source_bytes'],first['source_bytes']);self.assertEqual(second['window_started_at'],first['window_started_at']);self.assertEqual(second['max_source_bytes'],536870912);self.assertEqual(second['poll_reserve_bytes'],134217728)
            finally:fixture.close()
    def test_cached_structural_mode_accepts_inflight_cycle_but_rejects_identity_drift(self):
        instance=self.instance();value=instance.install();state=instance.actual_metadata(value)
        state['source_cursor']['cycle']={'upper_bound':state['watermark']+1,'stream':'actors'}
        adapter.validate_metadata(state,value,require_source_ready=False);instance.validate_cursor(state)
        corrupt=copy.deepcopy(state);corrupt['source_cursor']['cycle']={}
        with self.assertRaisesRegex(ValueError,'Cached source cursor rejected'):instance.validate_cursor(corrupt)
        with self.assertRaisesRegex(ValueError,'cycle is incomplete'):adapter.validate_metadata(state,value)
        corrupt=copy.deepcopy(state);corrupt['source_cursor']['build_id']='b1793236000-other'
        with self.assertRaisesRegex(ValueError,'identity/policy'):adapter.validate_metadata(corrupt,value,require_source_ready=False)
        corrupt=copy.deepcopy(state);corrupt['snapshot_watermark']+=1;corrupt['source_cursor']['snapshot_watermark']+=1
        with self.assertRaisesRegex(ValueError,'immutable artifact provenance'):adapter.validate_metadata(corrupt,value,require_source_ready=False)
    def test_corrupt_bundle_fails_before_admission_and_preserves_no_installed_receipt(self):
        self.store.objects[self.bound['object_key']]=b'broken'
        with self.assertRaisesRegex(ValueError,'byte identity'):self.instance().install()
        self.assertFalse((self.root/'installation.json').exists());self.assertFalse(self.root.exists())
    def test_wrong_binding_and_cached_receipt_do_not_fetch_or_overwrite(self):
        bad=dict(self.env,FLY_IMAGE_REF='wrong')
        with self.assertRaisesRegex(ValueError,'immutable image'):self.instance(bad).install()
        self.assertEqual(self.store.keys,[self.key]);self.assertFalse(self.root.exists())
    def test_cached_receipt_and_expected_deployment_drift_fail_without_network(self):
        self.instance().install();receipt=slot.metadata(self.root/'installation.json');receipt['deployment_sha256']='0'*64
        slot.atomic_json(self.root/'installation.json',receipt)
        with self.assertRaisesRegex(ValueError,'Cached installation differs'):self.instance().install()
        self.assertEqual(len(self.store.keys),4)
        stale=dict(self.env,EXPECTED_DEPLOYMENT_SHA256='1'*64)
        with self.assertRaisesRegex(ValueError,'Deployment byte identity'):self.instance(stale).install()
        self.assertEqual(len(self.store.keys),4)




class CheckpointExpiryTests(unittest.TestCase):
    def test_real_reader_and_canonical_reconciler_stop_and_cached_restart_refuses_expired_coverage(self):
        if not BINARY.is_file():self.skipTest('actual Rust binary unavailable')
        with tempfile.TemporaryDirectory(prefix='checkpoint-expiry-') as directory:
            work=Path(directory).resolve()
            with fixtures.checkpoint_runtime_fixture(work/'case',BINARY,slot_app='typeahead-tantivy-a') as case:
                portable=case['portable'];bound={**case['manifest'],
                    'deployment_schema_version':1,'request_id':'e'*32,
                    'artifact_manifest_key':f"releases/search-tantivy/{portable['active_build']}/manifest.json",
                    'artifact_manifest_sha256':hashlib.sha256(json.dumps(portable).encode()).hexdigest(),
                    'source_manifest_key':f"releases/search/{portable['rollback_native_anchor']['build_id']}/manifest.json"}
                key='control/tantivy-slots/typeahead-tantivy-a/'+('e'*32)+'.json'
                deployment=json.dumps(bound).encode()
                store=Store({key:deployment,bound['artifact_manifest_key']:json.dumps(portable).encode(),
                    bound['source_manifest_key']:case['native'],bound['object_key']:case['bundle'].read_bytes()})
                env=dict(os.environ,FLY_APP_NAME='typeahead-tantivy-a',FLY_MACHINE_ID='abcdef12345678',
                    FLY_MACHINE_VERSION='fixture-instance',FLY_IMAGE_REF=bound['image_digest'],
                    EXPECTED_BUILD_ID=bound['active_build'],TANTIVY_DEPLOYMENT_KEY=key,
                    EXPECTED_DEPLOYMENT_SHA256=hashlib.sha256(deployment).hexdigest(),
                    ADMIN_SECRET=case['secret'],TURSO_URL=case['source'].url,TURSO_AUTH_TOKEN=case['source'].source_secret,
                    TANTIVY_TEST_FIXTURE='1')
                clock=[time.time()]
                instance=slot.Slot(env,work/'installed',BINARY,store,fixture_clock=lambda:clock[0])
                reconciler=None
                try:
                    value=instance.install();public,admin=8080,smoke.port()
                    wrong_source=slot.Slot(dict(env,TURSO_URL='https://different-source.invalid'),work/'installed',BINARY,store,fixture_clock=lambda:clock[0])
                    with self.assertRaisesRegex(ValueError,'source differs'):
                        wrong_source.start_reader(value,f'127.0.0.1:{public}',f'127.0.0.1:{admin}')
                    self.assertEqual(wrong_source.children,[])
                    reader=instance.start_reader(value,f'127.0.0.1:{public}',f'127.0.0.1:{admin}')
                    for _ in range(100):
                        try:
                            if smoke.request(f'http://127.0.0.1:{public}','/index/status')[0]==200:break
                        except OSError:pass
                        time.sleep(.02)
                    self.assertEqual(smoke.request(f'http://127.0.0.1:{public}','/search?q=real')[0],200)
                    marker=work/'source-polled.json'
                    program="import sys,time,json\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1]);import sync,current_state\nsource=sync.Turso(sys.argv[2],sys.argv[3],allow_test_loopback=True,retries=1,backoff=0,max_bytes=1048576)\nadmin=sync.Admin(sys.argv[4],sys.argv[5],sys.argv[6])\nresult=current_state.ObservedSynchronizer(source,admin,sync,policy_mode='snapshot',anti_entropy_pages=0,safety_lag=0,max_pages=10,max_seconds=5,retries=1,backoff=0).run()\nPath(sys.argv[7]).write_text(json.dumps(result));time.sleep(10)\n"
                    reconciler=subprocess.Popen([sys.executable,'-c',program,str(ROOT/'services/tantivy-input'),
                        case['source'].url,case['source'].source_secret,f'http://127.0.0.1:{admin}',case['secret'],
                        f'http://127.0.0.1:{public}/index/status',str(marker)],stdout=case['log'],stderr=case['log'])
                    instance.children.append(reconciler)
                    until=time.monotonic()+5
                    while not marker.exists() and time.monotonic()<until:
                        self.assertIsNone(reconciler.poll());time.sleep(.02)
                    self.assertTrue(marker.exists())
                    deadline=instance.coverage_deadline
                    instance.runtime_gates(value)
                    self.assertTrue(slot.metadata(instance.root/'installation.json')['runtime_qualified'])
                    self.assertEqual(instance.coverage_deadline,deadline,'ordinary polling renewed full coverage')
                    self.assertIsNone(reader.poll());self.assertIsNone(reconciler.poll())
                    case['source'].now=int(time.time())
                    remote_source=case['source'].source()
                    remote_admin=case['replay'].Admin(f'http://127.0.0.1:{admin}',case['secret'],f'http://127.0.0.1:{public}/index/status')
                    case['current'].ObservedSynchronizer(remote_source,remote_admin,case['replay'],policy_mode='snapshot',anti_entropy_pages=4,safety_lag=0,max_pages=10,max_seconds=5,retries=1,backoff=0).run()
                    self.assertTrue(smoke.request(f'http://127.0.0.1:{public}','/index/status')[2]['cutover_source_ready'])
                    clock[0]=time.time();instance.runtime_gates(value)
                    self.assertTrue(slot.metadata(instance.root/'installation.json')['runtime_qualified'])
                    self.assertGreater(instance.coverage_deadline,deadline)
                    deadline=instance.coverage_deadline
                    clock[0]=deadline-59
                    with self.assertRaisesRegex(ValueError,'coverage expired'):
                        instance.check_coverage(value)
                    self.assertIsNotNone(reader.poll());self.assertIsNotNone(reconciler.poll())
                    with self.assertRaisesRegex(ValueError,'coverage expired'):
                        instance.start_reader(value,f'127.0.0.1:{public}',f'127.0.0.1:{admin}')
                    restarted=slot.Slot(env,work/'installed',BINARY,store,fixture_clock=lambda:clock[0])
                    with self.assertRaisesRegex(ValueError,'coverage expired'):
                        restarted.install()
                    self.assertEqual(len(store.keys),4,'cached restart fetched artifacts')
                    with self.assertRaisesRegex(ValueError,'unavailable in production'):
                        slot.Slot({k:v for k,v in env.items() if k!='TANTIVY_TEST_FIXTURE'},work/'other',BINARY,store,fixture_clock=lambda:0)
                finally:
                    instance.stop();store.close()

if __name__=='__main__':unittest.main()
