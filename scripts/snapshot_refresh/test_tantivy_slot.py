import copy
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
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
                cursor['reconciliation'].update(last_completed_at=now-1,last_completed_started_at=now-10)
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
        restarted=subprocess.run([sys.executable,'-c',code,str(HERE),json.dumps(self.env),str(self.root),str(BINARY)],capture_output=True,timeout=30)
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
    def test_durable_hourly_quota_survives_restart_and_early_exit(self):
        self.instance().install();first=self.instance();self.assertIsNone(first.source_window(1000))
        restarted=self.instance();self.assertEqual(restarted.source_window(1001),4600);self.assertEqual(restarted.source_window(4599),4600)
        self.assertIsNone(restarted.source_window(4600));self.assertEqual(first.source_window(4601),8200)

if __name__=='__main__':unittest.main()
