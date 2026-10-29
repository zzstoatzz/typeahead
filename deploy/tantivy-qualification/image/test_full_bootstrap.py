"""Local real-file and real-engine tests; no cloud or live source access."""
from contextlib import closing, redirect_stdout
import copy
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
sys.path.insert(0,str(HERE))
import full_bootstrap as bootstrap
sys.path.insert(0,str(REPO/'scripts/snapshot_compaction'))
from reference_build import build
BIN=Path(os.environ.get('TA_BINARY',REPO/'services/tantivy/target/release/typeahead-tantivy'))
TOOLS=Path(os.environ.get('TA_NATIVE_TOOLS',REPO/'experiments/tantivy-qualification-native/.tools/bin'))
SECRET='local-fixture-control-capability'
ADMIN='local-fixture-admin-capability'

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def request(port,path,method='GET',auth=True,body=None):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=20)
    try:
        c.request(method,path,body,{'X-Pilot-Authorization':SECRET} if auth else {})
        r=c.getresponse();data=r.read();return r.status,json.loads(data)
    finally:c.close()

class Local(bootstrap.FullQualification):
    def entrypoint_argv(self):
        return [str(BIN),'serve','--data',str(self.working/'tantivy'),'--listen',f'127.0.0.1:{self.search_port}','--admin-listen',f'127.0.0.1:{self.admin_port}','--allow-stale','true','--retrieval','global-merge']
    def engine_env(self):return {'ADMIN_SECRET':ADMIN,'PATH':os.environ.get('PATH','')}

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.directory=Path(cls.temp.name).resolve();cls.source=cls.directory/'legacy.db'
        with closing(sqlite3.connect(cls.source)) as db:
            db.executescript('CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY,blob BLOB NOT NULL) WITHOUT ROWID; CREATE TABLE prefix_top_base(key TEXT PRIMARY KEY,dids BLOB NOT NULL) WITHOUT ROWID; CREATE TABLE index_meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE pds_scopes(host TEXT PRIMARY KEY,enabled INTEGER);')
            for i in range(128):
                blob=bytearray(struct.pack('<BBf',1,False,float(i)))
                for value in [f'actor{i}.fixture.test',f'Actor {i}','','[]','https://local.test','','{}']:
                    raw=value.encode();blob.extend(struct.pack('<H',len(raw)));blob.extend(raw)
                db.execute('INSERT INTO actor_compact_base VALUES(?,?)',(f'did:fixture:{i:03}',blob))
            db.execute('INSERT INTO prefix_top_base VALUES(?,?)',('h:a',b'fixture-unused'))
            db.executemany('INSERT INTO pds_scopes VALUES(?,1)',[('local.test',),('remote.test',)])
            db.executemany('INSERT INTO index_meta VALUES(?,?)',{'build_id':'fixture-full-control','normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','source_watermark':'1793230000','actor_count':'128','prefix_count':'1','pds_scope_version':'1'}.items());db.commit()
        cls.release=cls.directory/'release';build(cls.source,cls.release)
        database=cls.release/'index.db';prefix=cls.release/'index.db.prefix'
        cls.manifest={'manifest_version':2,'format_version':5,'active_build':'fixture-full-control','source_watermark':1793230000,'actor_count':128,'prefix_count':1,'normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','byte_size':database.stat().st_size,'sha256':bootstrap.sha(database),'snapshot_url':'releases/search/fixture-full-control/index.db','prefix_map':{'object_key':'releases/search/fixture-full-control/index.db.prefix','byte_size':prefix.stat().st_size,'sha256':bootstrap.sha(prefix)}}
        cls.app=cls.directory/'app';(cls.app/'input').mkdir(parents=True);shutil.copy(REPO/'services/tantivy-input/export.py',cls.app/'input/export.py')
        (cls.app/'bin').mkdir()
        if (TOOLS/'admit-prefix').exists():shutil.copy(TOOLS/'admit-prefix',cls.app/'bin/admit-prefix')
        cls.observer={'app':'typeahead-qual-fixture','machine_id':'fixture-owned','guest':{'cpu_kind':'shared','cpus':2,'memory_mb':1024},'volume_ids':['vol_fixture'],'final_volume_gib':20,'public_services':[],'machines_api_response_sha256':'a'*64}
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):self.root=self.directory/('run-'+str(time.time_ns()));self.services=[]
    def tearDown(self):
        for service in self.services:service.close()
        if self.root.exists():bootstrap.remove_working(self.root)
    def service(self,arm='tantivy'):
        s=Local(self.root,copy.deepcopy(self.manifest),arm,self.observer,'fixture-nonce-0123456789',time.time()+300,app=self.app,fixture=True)
        s.search_port=port();s.admin_port=port();self.services.append(s);return s
    def baseline(self,s,engine=False):
        self.root.mkdir();(self.root/'owner').write_text(bootstrap.OWNER);source=s.baseline/'source';source.mkdir(parents=True)
        for name in ['index.db','index.db.prefix']:shutil.copy(self.release/name,source/name)
        record={'prepared':True,'arm':s.arm,'build_id':self.manifest['active_build'],'source_database_sha256':self.manifest['sha256'],'source_prefix_sha256':self.manifest['prefix_map']['sha256']}
        (s.baseline/'prepared.json').write_bytes(bootstrap.encoded(record))
        (source/'manifest.json').write_bytes(bootstrap.encoded(self.manifest))
        if engine:
            raw=self.root/'fixture.jsonl';exporter=bootstrap.export_module(self.app/'input/export.py')
            with (source/'index.db.prefix').open('rb') as stream:
                budget=exporter.Budget(type('BudgetArgs',(),{'max_seconds':10})());actors=[exporter.unpack_actor(did,blob,self.manifest['source_watermark']) for did,blob in exporter.FlatActors(stream,budget,1024**2)]
            lines=[exporter.jsonline(a) for a in actors]
            header={'type':'manifest','schema_version':1,'build_id':self.manifest['active_build'],'watermark':str(self.manifest['source_watermark']),'source_watermark':self.manifest['source_watermark'],'actor_count':128,'source_actor_count':128,'partial':False,'normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','source_database_sha256':self.manifest['sha256'],'source_prefix_sha256':self.manifest['prefix_map']['sha256']}
            raw.write_bytes(exporter.jsonline(header)+b''.join(lines)+exporter.jsonline({'type':'end','build_id':self.manifest['active_build'],'actor_count':128,'actors_sha256':hashlib.sha256(b''.join(lines)).hexdigest()}))
            r=subprocess.run([str(BIN),'import','--data',str(s.baseline/'tantivy'),'--input',str(raw),'--batch-size','64'],capture_output=True,timeout=30)
            self.assertEqual(r.returncode,0,r.stderr.decode());raw.unlink()
        for directory,dirs,files in os.walk(s.baseline,topdown=False):
            for name in files:(Path(directory)/name).chmod(0o444)
            Path(directory).chmod(0o555)
        s.admit_prepared()
    def wait_ready(self,s):
        until=time.time()+10
        while time.time()<until:
            if s.child.poll() is not None:self.fail((self.root/'engine.log').read_text())
            try:
                if s.admin('/index/status').get('import_complete'):return
            except (OSError,bootstrap.BootstrapError):time.sleep(.03)
        self.fail('real engine did not start')
    def test_staged_phase_guest_contract_is_exclusive(self):
        prep=copy.deepcopy(self.observer);prep['guest']=dict(bootstrap.PREPARATION_GUEST)
        final=copy.deepcopy(self.observer)
        for phase,observer in [('legacy',prep),('resume',prep),('prepare',final)]:
            with self.assertRaisesRegex(bootstrap.BootstrapError,'provider_shape'):
                bootstrap.FullQualification(self.root,self.manifest,'tantivy',observer,'fixture-nonce-0123456789',time.time()+300,app=self.app,fixture=True,phase=phase)
        with self.assertRaisesRegex(bootstrap.BootstrapError,'exclusive_qualification_phase'):
            bootstrap.FullQualification(self.root,self.manifest,'tantivy',final,'fixture-nonce-0123456789',time.time()+300,app=self.app,fixture=True,phase='other')

    def staged_fixture(self,actual_process=False):
        observer=copy.deepcopy(self.observer);observer['guest']=dict(bootstrap.PREPARATION_GUEST)
        deadline=time.time()+300;shutil.copy(BIN,self.app/'bin/typeahead-tantivy')
        s=bootstrap.FullQualification(self.root,copy.deepcopy(self.manifest),'tantivy',observer,'fixture-nonce-0123456789',deadline,app=self.app,fixture=True,phase='prepare')
        self.services.append(s)
        if actual_process:
            release=self.release
            class Files(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    name=self.path.rsplit('/',1)[-1]
                    if name not in {'index.db','index.db.prefix'}:self.send_error(404);return
                    data=(release/name).read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                def log_message(self,*args):pass
            (self.app/'source-manifest.json').write_bytes(bootstrap.encoded(self.manifest))
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Files);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            origin=f'http://127.0.0.1:{server.server_port}'
            access={'build_id':self.manifest['active_build'],'database':origin+'/'+self.manifest['snapshot_url'],'prefix':origin+'/'+self.manifest['prefix_map']['object_key']}
            program="""
import pathlib,sys
sys.path.insert(0,sys.argv[1]);import full_bootstrap as boot
boot.APP=pathlib.Path(sys.argv[2])
Original=boot.FullQualification
class Fixture(Original):
 def __init__(self,*args,**kwargs):super().__init__(*args,**(kwargs|{'fixture':True}))
boot.FullQualification=Fixture
boot.main()
"""
            env={'PATH':os.environ.get('PATH',''),'QUAL_MODE':'full','QUAL_PHASE':'prepare','QUAL_ROLE':'tantivy-control','QUAL_OBSERVER_JSON':json.dumps(observer),'QUAL_PILOT_NONCE':s.nonce,'QUAL_DEADLINE':str(deadline),'QUAL_ROOT':str(self.root),'QUAL_SOURCE_ACCESS':json.dumps(access),'FLY_MACHINE_VERSION':'fixture-preparation-instance'}
            try:result=subprocess.run([sys.executable,'-c',program,str(HERE),str(self.app)],env=env,capture_output=True,text=True,timeout=30)
            finally:server.shutdown();server.server_close();thread.join()
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            line=next(line for line in result.stdout.splitlines() if line.startswith('QUALIFICATION_PREPARATION_COMPLETE '))
            receipt=json.loads(line.removeprefix('QUALIFICATION_PREPARATION_COMPLETE '))
            self.assertFalse(s.prepared);self.assertIsNone(s.child)
        else:
            self.baseline(s,True)
            previous=os.environ.get('FLY_MACHINE_VERSION');os.environ['FLY_MACHINE_VERSION']='fixture-preparation-instance'
            try:receipt=s.preparation_checkpoint()
            finally:
                if previous is None:os.environ.pop('FLY_MACHINE_VERSION',None)
                else:os.environ['FLY_MACHINE_VERSION']=previous
        return s,receipt

    def resumed_fixture(self,s,receipt,**changes):
        options=dict(root=self.root,manifest=copy.deepcopy(self.manifest),arm='tantivy',observer=copy.deepcopy(self.observer),nonce=s.nonce,deadline=s.deadline,app=self.app,fixture=True,phase='resume')
        options.update(changes)
        resumed=bootstrap.FullQualification(**options);self.services.append(resumed)
        resumed.resume_checkpoint(receipt['checkpoint_sha256']);return resumed

    @unittest.skipUnless(BIN.is_file(),'requires already built local Rust binary; does not compile')
    def test_staged_resume_rejects_tampered_bindings_without_bulk_work(self):
        s,receipt=self.staged_fixture()
        self.assertEqual(receipt['guest'],bootstrap.PREPARATION_GUEST);self.assertEqual(receipt['runtime_instance_id'],'fixture-preparation-instance')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'preparation_cannot_serve'):s.serve()
        wrong=copy.deepcopy(self.observer);wrong['volume_ids']=['vol_wrong']
        with self.assertRaisesRegex(bootstrap.BootstrapError,'identity_mismatch'):self.resumed_fixture(s,receipt,observer=wrong)
        with self.assertRaisesRegex(bootstrap.BootstrapError,'deadline_mismatch'):self.resumed_fixture(s,receipt,deadline=s.deadline+1)
        with self.assertRaisesRegex(bootstrap.BootstrapError,'digest_mismatch'):self.resumed_fixture(s,receipt|{'checkpoint_sha256':'0'*64})
        bootstrap.atomic(s.phase_path,{'schema':1,'acked':0,'inflight':1})
        with self.assertRaisesRegex(bootstrap.BootstrapError,'cursor_not_zero'):self.resumed_fixture(s,receipt)
        bootstrap.atomic(s.phase_path,{'schema':1,'acked':0,'inflight':None})
        feed=self.root/'updates.json';original=feed.read_bytes();feed.chmod(0o600);feed.write_bytes(original+b' ')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'fixed_input_mismatch'):self.resumed_fixture(s,receipt)
        feed.write_bytes(original)
        binary=self.app/'bin/typeahead-tantivy'
        try:
            with binary.open('ab') as stream:stream.write(b'changed')
            with self.assertRaisesRegex(bootstrap.BootstrapError,'binary_mismatch'):self.resumed_fixture(s,receipt)
        finally:shutil.copy(BIN,binary)
        metadata=s.working/'tantivy/index/meta.json';metadata.write_bytes(metadata.read_bytes()+b' ')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'file_witness_mismatch'):self.resumed_fixture(s,receipt)

    @unittest.skipUnless(BIN.is_file(),'requires already built local Rust binary; does not compile')
    def test_new_process_resume_runs_real_http_update_restart_without_reimport(self):
        s,receipt=self.staged_fixture(actual_process=True);control,public,admin=port(),port(),port()
        original_prefix=(s.baseline/'source/index.db.prefix').stat()
        program="""
import http.server,json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]);import full_bootstrap as boot
root,app=pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3])
observer=json.loads(sys.argv[4]);deadline=float(sys.argv[5]);expected=sys.argv[6]
control,public,admin=map(int,sys.argv[7:10])
def forbidden(*args,**kwargs):raise AssertionError('bulk work forbidden on resume')
boot.full_prepare.Preparation.run=forbidden;boot.generate_feed=forbidden
original_sha=boot.sha
def bounded_sha(path):
 if str(path).endswith('index.db.prefix'):raise AssertionError('prefix rehash forbidden')
 return original_sha(path)
boot.sha=bounded_sha
class Runtime(boot.FullQualification):
 def __init__(self,*args,**kwargs):
  super().__init__(*args,**(kwargs|{'fixture':True}));self.admin_port=admin
 def entrypoint_argv(self):return [str(app/'bin/typeahead-tantivy'),'serve','--data',str(self.working/'tantivy'),'--listen',f'127.0.0.1:{public}','--admin-listen',f'127.0.0.1:{admin}','--allow-stale','true','--retrieval','global-merge']
 def engine_env(self):return {'PATH':os.environ.get('PATH',''),'ADMIN_SECRET':os.environ['ADMIN_SECRET']}
 prepare_oracle=forbidden
import socket
class Control(boot.ControlServer):
 address_family=socket.AF_INET
 def __init__(self,address,handler):super().__init__(('127.0.0.1',control),handler)
boot.APP=app;boot.FullQualification=Runtime;boot.ControlServer=Control
os.environ.update(QUAL_MODE='full',QUAL_PHASE='resume',QUAL_ROLE='tantivy-control',QUAL_OBSERVER_JSON=json.dumps(observer),QUAL_PILOT_NONCE='fixture-nonce-0123456789',QUAL_ROOT=str(root),QUAL_DEADLINE=str(deadline),QUAL_CHECKPOINT_SHA256=expected,QUAL_SOURCE_ACCESS='invalid-unused-inherited-capability')
boot.main()
"""
        child=subprocess.Popen([sys.executable,'-c',program,str(HERE),str(self.root),str(self.app),json.dumps(self.observer),str(s.deadline),receipt['checkpoint_sha256'],str(control),str(public),str(admin)],env={'PATH':os.environ.get('PATH',''),'ADMIN_SECRET':ADMIN,'QUAL_CONTROL_SECRET':SECRET},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        def wait_http(p,path):
            until=time.monotonic()+10
            while time.monotonic()<until:
                if child.poll() is not None:self.fail('new resume process exited')
                try:
                    status,value=request(p,path,auth=False)
                    if status==200:return value
                except OSError:time.sleep(.03)
            self.fail('new resume process HTTP did not start')
        try:
            ready=wait_http(control,'/prepared');self.assertTrue(ready['prepared']);self.assertEqual(ready['guest'],bootstrap.FINAL_GUEST);self.assertEqual(ready['overall_deadline'],s.deadline);self.assertEqual(ready['qualification_phase'],'resume')
            self.assertEqual(request(control,'/serve','POST')[0],200);wait_http(public,'/health')
            self.assertEqual(request(control,'/apply/1','POST')[0],200)
            self.assertEqual(request(control,'/restart','POST')[0],200);wait_http(public,'/health')
            self.assertEqual(request(control,'/apply/1','POST')[1]['receipt']['replayed'],64)
            self.assertEqual(request(control,'/apply/2','POST')[0],200)
        finally:
            child.terminate();child.wait(timeout=10)
        current=(s.baseline/'source/index.db.prefix').stat()
        self.assertEqual((original_prefix.st_ino,original_prefix.st_size,original_prefix.st_mtime_ns,original_prefix.st_ctime_ns),(current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns))

    def test_failure_diagnostics_preserve_stage_and_type_without_payload(self):
        s=self.service();self.baseline(s);s.stage='working-clone'
        for error in [subprocess.TimeoutExpired(['private-profile-and-secret'],1,output=b'secret-output'),subprocess.CalledProcessError(7,['private-command'],output=b'private-output'),ValueError('private-profile')]:
            output=io.StringIO()
            with redirect_stdout(output):value=s.record_failure(error)
            self.assertEqual(value['stage'],'working-clone')
            self.assertEqual(value['exception_class'],type(error).__name__)
            self.assertNotIn('private',output.getvalue());self.assertNotIn('secret',output.getvalue())
        self.assertEqual(s.summary()['preparation_stage'],'working-clone')

    def test_feed_determinism_and_all_fixed_lifecycle_states(self):
        first=bootstrap.generate_feed(self.manifest,self.release,self.app/'input/export.py',time.time()+10)
        second=bootstrap.generate_feed(self.manifest,self.release,self.app/'input/export.py',time.time()+10)
        feed,data,digest=first;self.assertEqual(first,second);self.assertFalse(data.endswith(b'\n'));self.assertEqual(hashlib.sha256(data).hexdigest(),digest)
        self.assertEqual(len(feed['baseline_actors']),64);self.assertEqual(len({a['did'] for b in feed['batches'] for a in b['actors']}),64)
        self.assertEqual([b['batch_id'] for b in feed['batches']],list(bootstrap.STAGES))
        self.assertTrue(all(a['hidden'] for a in feed['batches'][3]['actors']));self.assertTrue(all(a['deleted'] for a in feed['batches'][5]['actors']))
        self.assertTrue(all(not a['deleted'] for a in feed['batches'][6]['actors']))
        for before,after in zip(feed['baseline_actors'],feed['batches'][-1]['actors']):
            self.assertEqual(before,after|{'version':before['version']})
        self.assertLess(len(data),bootstrap.MAX_FEED)
    def test_mutable_copies_immutable_links_and_symlink_guards(self):
        s=self.service();self.baseline(s)
        p=s.baseline/'source/index.db.prefix';self.assertEqual(p.stat().st_ino,(s.working/'source/index.db.prefix').stat().st_ino)
        self.assertNotEqual((s.baseline/'source/index.db').stat().st_ino,(s.working/'source/index.db').stat().st_ino)
        with closing(sqlite3.connect(s.working/'source/index.db')) as db:db.execute("UPDATE index_meta SET value='changed' WHERE key='build_id'");db.commit()
        s.baseline_intact();self.assertEqual(bootstrap.sha(s.baseline/'source/index.db'),self.manifest['sha256'])
        s.reset();self.assertEqual(bootstrap.sha(s.working/'source/index.db'),self.manifest['sha256']);self.assertEqual(s.phase()['acked'],0)
        (s.working/'unsafe').symlink_to('/tmp')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'scratch_symlink'):s.guard()
        (s.working/'unsafe').unlink()
        oversize=self.root/'sparse-budget-witness'
        try:
            with oversize.open('wb') as stream:stream.truncate(20*bootstrap.GIB)
            with self.assertRaisesRegex(bootstrap.BootstrapError,'unique_inode_payload_budget'):s.guard()
        finally:oversize.unlink()
        s.deadline=time.time()
        with self.assertRaisesRegex(bootstrap.BootstrapError,'guest_deadline'):s.guard()
    @unittest.skipUnless((REPO/'experiments/tantivy-oracle/target/release/tantivy-raw-oracle').is_file(),'requires already built local independent oracle; does not compile')
    def test_actual_streamed_oracle_binds_feed_cases_full_footer(self):
        s=self.service();self.baseline(s);s.arm='native'
        shutil.copy(REPO/'experiments/tantivy-oracle/target/release/tantivy-raw-oracle',self.app/'tantivy-raw-oracle')
        shutil.copy(REPO/'src/identity-quarantine.json',self.app/'identity-quarantine.json')
        try:s.prepare_oracle(fixture_cases=128)
        except bootstrap.BootstrapError:
            for name in ['oracle-freeze.log','oracle-run.log','oracle-export.log']:
                path=self.root/name
                if path.exists():print(name,path.read_text()[-3000:])
            raise
        self.assertTrue(s.oracle_ready);self.assertEqual(s.summary()['oracle_sha256'],bootstrap.sha(self.root/'oracle.json'))
        value=json.loads((self.root/'oracle.json').read_bytes());self.assertEqual(value['receipt']['actors_checked'],128)
        self.assertEqual(value['receipt']['updates_sha256'],s.feed_sha);self.assertEqual([c['cursor'] for c in value['checkpoints']],list(range(10)))
        self.assertFalse((self.root/'oracle-sample.jsonl').exists());self.assertFalse(list(self.root.rglob('*.jsonl')))
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),bootstrap.control_handler(s,SECRET));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.assertEqual(request(server.server_port,'/inputs/cases')[0],200)
            self.assertEqual(request(server.server_port,'/inputs/oracle')[1],value)
            self.assertEqual(request(server.server_port,'/inputs/oracle',auth=False)[0],403)
        finally:server.shutdown();server.server_close();thread.join()

    @unittest.skipUnless((TOOLS/'qualification-native').is_file() and (TOOLS/'typeahead-ingester').is_file(),'requires already built local native tools; does not compile')
    def test_actual_native_writer_all_batches_replay_restart_and_prefix_revalidation(self):
        real=self.root.with_name(self.root.name+'-actors.db')
        output=self.root.with_name(self.root.name+'-build')
        compact=self.root.with_name(self.root.name+'-compact')
        try:
            with closing(sqlite3.connect(real)) as db:
                db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
                db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(f'did:fixture:{i:03}',f'actor{i}.fixture.test',f'Actor {i}','','[]','https://'+('local.test' if i%2 else 'remote.test'),'','{}',0,i,1793230000) for i in range(128)]);db.commit()
            result=subprocess.run([str(TOOLS/'typeahead-ingester')],env={'PATH':os.environ.get('PATH',''),'MODE':'indexer','INDEX_CHANNEL':'local','INDEX_SOURCE':'export','INDEX_SAMPLE_LIMIT':'0','INDEX_PDS_MAX_ACTORS':'10000','INDEX_REUSE_SOURCE':str(real),'INDEX_BUILD_ROOT':str(output),'INDEXER_VERSION':'qualification-full-test'},capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode()[-2000:])
            snapshot=next(output.glob('build-*/index.db'));build(snapshot,compact)
            with closing(sqlite3.connect(compact/'index.db')) as db:meta=dict(db.execute('SELECT key,value FROM index_meta'))
            s=self.service('native');s.manifest=copy.deepcopy(self.manifest)
            s.manifest.update(active_build=meta['build_id'],source_watermark=int(meta['source_watermark']),byte_size=(compact/'index.db').stat().st_size,sha256=bootstrap.sha(compact/'index.db'))
            s.manifest['prefix_map'].update(sha256=bootstrap.sha(compact/'index.db.prefix'),byte_size=(compact/'index.db.prefix').stat().st_size)
            self.root.mkdir();(self.root/'owner').write_text(bootstrap.OWNER);source=s.baseline/'source';source.mkdir(parents=True)
            for name in ['index.db','index.db.prefix']:shutil.copy(compact/name,source/name)
            for name in ['index.db','index.db.prefix']:(source/name).chmod(0o444)
            subprocess.run([str(TOOLS/'admit-prefix')],env={'TYPEAHEAD_ADMIT_PREFIX_PATH':str(source/'index.db.prefix'),'TYPEAHEAD_ADMIT_PREFIX_SHA256':s.manifest['prefix_map']['sha256']},check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            # Use the actual native schema writer to initialize the independent empty overlay.
            (s.baseline/'tmp').mkdir()
            native_env={'PATH':os.environ.get('PATH',''),'NATIVE_QUAL_ROOT':str(self.root),'TYPEAHEAD_EVAL_SNAPSHOT':str(source/'index.db'),'LOCAL_DB_PATH':str(s.baseline/'overlay.db'),'SQLITE_TMPDIR':str(s.baseline/'tmp'),'NATIVE_QUAL_REQUEST':str(s.baseline/'request.json'),'NATIVE_QUAL_RESPONSE':str(s.baseline/'response.json'),'NATIVE_QUAL_FEED_SHA256':'0'*64,'NATIVE_QUAL_SECONDS':'1','TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT':'1'}
            r=subprocess.run([str(TOOLS/'qualification-native')],env=native_env,capture_output=True,timeout=5);self.assertEqual(r.returncode,0,r.stderr.decode())
            (s.baseline/'prepared.json').write_bytes(bootstrap.encoded({'prepared':True,'arm':'native','build_id':s.manifest['active_build'],'source_database_sha256':s.manifest['sha256'],'source_prefix_sha256':s.manifest['prefix_map']['sha256']}))
            for directory,dirs,files in os.walk(s.baseline,topdown=False):
                for name in files:
                    item=Path(directory)/name
                    if item.stat().st_mode & 0o777 != 0o444:item.chmod(0o444)
                Path(directory).chmod(0o555)
            native_app=self.root/'app-without-admitter';(native_app/'input').mkdir(parents=True)
            shutil.copy(REPO/'services/tantivy-input/export.py',native_app/'input/export.py');s.app=native_app
            prefix_before=(s.baseline/'source/index.db.prefix').stat()
            s.admit_prepared();self.assertEqual(s.native_cursor(),0);self.assertEqual(s.children,[])
            s.entrypoint_argv=lambda:[str(TOOLS/'qualification-native')]
            s.engine_env=lambda: {'PATH':os.environ.get('PATH',''),'NATIVE_QUAL_ROOT':str(self.root),'TYPEAHEAD_EVAL_SNAPSHOT':str(s.baseline/'source/index.db'),'LOCAL_DB_PATH':str(s.working/'overlay.db'),'SQLITE_TMPDIR':str(s.working/'tmp'),'NATIVE_QUAL_REQUEST':str(s.working/'request.json'),'NATIVE_QUAL_RESPONSE':str(s.working/'response.json'),'NATIVE_QUAL_FEED_SHA256':s.feed_sha,'NATIVE_QUAL_SECONDS':'60','TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT':'1'}
            s.serve()
            for cursor in range(1,10):
                value=s.apply(cursor);self.assertTrue(value['receipt']['accepted']);self.assertEqual(s.native_cursor(),cursor)
                with closing(sqlite3.connect(s.working/'overlay.db')) as db:
                    rows=db.execute('SELECT hidden,blob FROM actor_overlay').fetchall()
                self.assertEqual(len(rows),64)
                if cursor==4:self.assertTrue(all(hidden for hidden,blob in rows))
                elif cursor==6:self.assertTrue(all(blob is None for hidden,blob in rows))
                else:self.assertTrue(all(not hidden and blob is not None for hidden,blob in rows))
                if cursor==1:
                    self.assertTrue(s.apply(cursor)['replay']);s.pause();s.serve()
            self.assertTrue(s.apply(9)['replay']);s.pause();s.baseline_intact()
            s.reset();self.assertEqual(s.native_cursor(),0);self.assertEqual(s.phase()['acked'],0)
            self.assertFalse((s.working/'source').exists())
            self.assertEqual(s.config['paths']['snapshot'],str(s.baseline/'source/index.db'))
            prefix=s.baseline/'source/index.db.prefix';cache=Path(str(prefix)+'.admitted')
            self.assertEqual(int.from_bytes(cache.read_bytes()[72:88],'little',signed=True),prefix.stat().st_ctime_ns)
            self.assertEqual(prefix.stat().st_ctime_ns,prefix_before.st_ctime_ns)
            self.assertEqual(s.children,[])
        finally:
            for service in self.services:service.close()
            real.unlink(missing_ok=True)
            for path in [output,compact]:
                if path.exists():bootstrap.remove_working(path)

    @unittest.skipUnless(BIN.is_file(),'requires already built local Rust binary; does not compile')
    def test_actual_entrypoint_terminates_engine_tree_and_observation_respects_control_lock(self):
        for family,host,p in [(socket.AF_INET6,'::',8080),(socket.AF_INET,'127.0.0.1',18080),(socket.AF_INET,'127.0.0.1',18081)]:
            with socket.socket(family) as probe:
                try:probe.bind((host,p))
                except OSError:self.skipTest('actual entrypoint fixed local port is in use')
        s=self.service();self.baseline(s,True)
        shutil.copy(BIN,self.app/'bin/typeahead-tantivy')
        children=self.root/'entrypoint-children.jsonl'
        program="""
import json,os,pathlib,sys
sys.path.insert(0,sys.argv[1])
import entrypoint
entrypoint.APP=pathlib.Path(sys.argv[2])
original=entrypoint.subprocess.Popen
receipt=pathlib.Path(sys.argv[4])
def launch(*args,**kwargs):
 child=original(*args,**kwargs)
 with receipt.open('a') as stream:stream.write(json.dumps({'pid':child.pid})+'\\n')
 return child
entrypoint.subprocess.Popen=launch
sys.argv=['entrypoint','--run',sys.argv[3]]
entrypoint.main()
"""
        s.entrypoint_argv=lambda:[sys.executable,'-c',program,str(HERE),str(self.app),str(self.root/'run.json'),str(children)]
        s.engine_env=lambda:{'ADMIN_SECRET':ADMIN,'PATH':os.environ.get('PATH',''),'FLY_APP_NAME':s.observer['app'],'FLY_MACHINE_ID':s.observer['machine_id'],'FLY_MACHINE_VERSION':'fixture-process-tree'}
        def ready():
            until=time.monotonic()+10
            while time.monotonic()<until:
                if s.child.poll() is not None:self.fail('actual entrypoint stopped before health')
                connection=http.client.HTTPConnection('::1',8080,timeout=.5)
                try:
                    connection.request('GET','/health');response=connection.getresponse();response.read()
                    if response.status==200:return
                except OSError:time.sleep(.03)
                finally:connection.close()
            self.fail('actual entrypoint proxy did not start')
        def absent(pid):
            try:os.kill(pid,0)
            except ProcessLookupError:return True
            return False
        s.serve();ready();parent=s.child.pid;engine=json.loads(children.read_text().splitlines()[-1])['pid']
        self.assertFalse(absent(engine));s.pause();self.assertTrue(absent(parent));self.assertTrue(absent(engine))
        s.serve();ready();engine=json.loads(children.read_text().splitlines()[-1])['pid']
        os.killpg(s.child.pid,signal.SIGTERM);s.child.wait(timeout=10);self.assertTrue(absent(engine))
        with s.lock:
            observer=threading.Thread(target=s.observe_runtime);observer.start();observer.join(timeout=1)
            self.assertFalse(observer.is_alive());self.assertEqual(s.state,'serving');self.assertEqual(s.failures,[])
        s.observe_runtime();self.assertEqual(s.state,'paused');self.assertEqual(len(s.failures),1)
        s.observe_runtime();self.assertEqual(len(s.failures),1)
        self.assertGreater(bootstrap.ENTRYPOINT_SHUTDOWN_SECONDS,2*(3+3))

    @unittest.skipUnless(BIN.is_file(),'requires already built local Rust binary; does not compile')
    def test_actual_rust_control_commit_restart_replay_recovery_reset(self):
        old=os.environ.get('ADMIN_SECRET');os.environ['ADMIN_SECRET']=ADMIN
        s=self.service();self.baseline(s,True)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),bootstrap.control_handler(s,SECRET));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();p=server.server_port
        try:
            status,value=request(p,'/prepared',auth=False);self.assertEqual(status,200);self.assertEqual(value['state'],'prepared');self.assertIsNone(s.child)
            self.assertEqual(request(p,'/serve','POST',False)[0],403)
            self.assertEqual(request(p,'/apply/1','POST',True,b'{"actors":[]}')[0],400)
            self.assertEqual(request(p,'/inputs/updates')[1],s.feed)
            self.assertEqual(request(p,'/serve','POST')[0],200);self.wait_ready(s)
            self.assertEqual(request(p,'/apply/3','POST')[0],503)
            self.assertEqual(request(p,'/apply/1','POST')[0],200)
            self.assertEqual(request(p,'/apply/1','POST')[1]['receipt']['replayed'],64)
            bootstrap.atomic(s.phase_path,{'schema':1,'acked':0,'inflight':1})
            self.assertTrue(request(p,'/apply/1','POST')[1]['receipt']['ack_recovered'])
            self.assertEqual(request(p,'/restart','POST')[0],200);self.wait_ready(s)
            self.assertEqual(s.admin('/index/status')['source_cursor'],s.cursor_value(s.feed['batches'][0]))
            for cursor in range(2,10):
                status,value=request(p,f'/apply/{cursor}','POST');self.assertEqual(status,200,value)
                self.assertEqual(s.phase(),{'schema':1,'acked':cursor,'inflight':None})
                handle=s.feed['batches'][cursor-1]['actors'][0]['handle']
                status,answer=request(s.search_port,'/search?q='+urllib.parse.quote(handle),auth=False)
                self.assertEqual(status,200,answer)
                expected=[] if cursor in {4,6} else [s.feed['baseline_actors'][0]['did']]
                self.assertEqual([actor['did'] for actor in answer['actors']],expected)
            self.assertEqual(request(p,'/apply/9','POST')[1]['receipt']['replayed'],64)
            self.assertEqual(request(p,'/apply/1','POST')[0],503)
            self.assertEqual(request(p,'/metrics')[0],200)
            self.assertEqual(request(p,'/reset','POST')[0],200);self.assertEqual(s.phase()['acked'],0);self.assertIsNotNone(s.child.poll());s.baseline_intact()
            self.assertEqual(request(p,'/serve','POST')[0],200);self.wait_ready(s);self.assertIsNone(s.admin('/index/status')['source_cursor'])
            s.admin('/admin/apply',{'actors':[],'expected_source_cursor':None,'source_cursor':{'unexpected':True}})
            self.assertEqual(request(p,'/apply/1','POST')[0],503)
            self.assertEqual(s.phase(),{'schema':1,'acked':0,'inflight':1})
            self.assertEqual(request(p,'/pause','POST')[0],200)
            c=socket.create_connection(('127.0.0.1',p));c.sendall(b'GET /prepared HTTP/1.1\r\nX-Large: '+b'x'*5000+b'\r\n\r\n');self.assertIn(b'431',c.recv(4096));c.close()
        finally:
            server.shutdown();server.server_close();thread.join();s.close()
            if old is None:os.environ.pop('ADMIN_SECRET',None)
            else:os.environ['ADMIN_SECRET']=old

if __name__=='__main__':unittest.main()
