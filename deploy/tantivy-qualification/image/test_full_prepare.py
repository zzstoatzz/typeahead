"""Actual local HTTP downloads and optional frozen Linux native/Tantivy integration."""
import copy
from contextlib import closing
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0,str(REPO/'scripts/snapshot_compaction'))
from reference_build import build
spec = importlib.util.spec_from_file_location('full_prepare',HERE/'full_prepare.py')
prepare = importlib.util.module_from_spec(spec); spec.loader.exec_module(prepare)

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.directory = Path(cls.temp.name).resolve()
        source = cls.directory/'legacy.db'
        with closing(sqlite3.connect(source)) as db:
            db.executescript('CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY,blob BLOB NOT NULL) WITHOUT ROWID; CREATE TABLE prefix_top_base(key TEXT PRIMARY KEY,dids BLOB NOT NULL) WITHOUT ROWID; CREATE TABLE index_meta(key TEXT PRIMARY KEY,value TEXT);')
            for i in range(32):
                blob = bytearray(struct.pack('<BBf',1,False,float(i)))
                for value in [f'actor{i}.fixture.test',f'Actor {i}','','[]','https://local.test','','{}']:
                    value=value.encode();blob.extend(struct.pack('<H',len(value)));blob.extend(value)
                db.execute('INSERT INTO actor_compact_base VALUES(?,?)',(f'did:fixture:{i:02}',blob))
            db.execute('INSERT INTO prefix_top_base VALUES(?,?)',('h:a',b'fixture-unused'))
            db.executemany('INSERT INTO index_meta VALUES(?,?)',{'build_id':'fixture-full-prepare','normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','source_watermark':'1793230000','actor_count':'32','prefix_count':'1'}.items());db.commit()
        cls.release=cls.directory/'release';build(source,cls.release)
        database=cls.release/'index.db';prefix=cls.release/'index.db.prefix'
        cls.manifest={'manifest_version':2,'format_version':5,'active_build':'fixture-full-prepare','source_watermark':1793230000,'actor_count':32,'prefix_count':1,'normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','byte_size':database.stat().st_size,'sha256':sha(database),'snapshot_url':'releases/search/fixture-full-prepare/index.db','prefix_map':{'object_key':'releases/search/fixture-full-prepare/index.db.prefix','byte_size':prefix.stat().st_size,'sha256':sha(prefix)}}
        cls.mode='valid';cls.requests=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path.split('?')[0])
                if cls.mode=='redirect':
                    self.send_response(302);self.send_header('Location','/credential-exfiltration');self.end_headers();return
                name=self.path.split('?')[0].rsplit('/',1)[-1]
                if name not in {'index.db','index.db.prefix'}:self.send_error(404);return
                data=(cls.release/name).read_bytes()
                if cls.mode=='truncated':data=data[:-1]
                if cls.mode=='corrupt':data=bytes([data[0]^1])+data[1:]
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def setUp(self):
        type(self).mode='valid';type(self).requests=[]
        self.root=self.directory/('run-'+str(time.time_ns()))
    def job(self,**kwargs):
        return prepare.Preparation(self.root,copy.deepcopy(self.manifest),'tantivy',time.time()+90,allow_loopback_fixture=True,**kwargs)
    def access(self):
        return {'build_id':self.manifest['active_build'],**{field:self.origin+'/releases/search/fixture-full-prepare/'+name+'?secret=never-log' for field,name in [('database','index.db'),('prefix','index.db.prefix')]}}
    def test_actual_http_download_matches_bytes_and_digest(self):
        job=self.job();job.root.mkdir();job.stage.mkdir()
        target=job.stage/'index.db';job.download(self.access()['database'],target,self.manifest,self.manifest['snapshot_url'])
        self.assertEqual(sha(target),self.manifest['sha256']);self.assertFalse(target.with_suffix('.db.part').exists())
    def test_truncation_sha_mismatch_redirect_clean_failures(self):
        for mode,error in [('truncated','source_size'),('corrupt','source_sha256'),('redirect','source_redirect')]:
            self.root=self.directory/('run-'+str(time.time_ns()));type(self).mode=mode
            job=self.job()
            with self.assertRaisesRegex(prepare.PreparationError,error):job.run(self.access())
            self.assertFalse(job.stage.exists());self.assertFalse(job.base.exists())
            self.assertNotIn('/credential-exfiltration',self.requests)
    def test_release_binding_cannot_read_different_object(self):
        with self.assertRaisesRegex(prepare.PreparationError,'source_object_binding'):
            self.job().check_url(self.origin+'/wrong/index.db?secret=x',self.manifest['snapshot_url'])
        access=self.access();access['build_id']='different'
        with self.assertRaisesRegex(prepare.PreparationError,'source_release_binding'):self.job().run(access)
        self.assertFalse((self.root/'baseline').exists())
    def test_actual_free_space_budget_and_deadline(self):
        job=self.job(min_free=shutil.disk_usage(self.directory).free+prepare.GIB)
        with self.assertRaisesRegex(prepare.PreparationError,'disk_reserve'):job.run(self.access())
        self.assertFalse(job.stage.exists())
        self.root=self.directory/('run-'+str(time.time_ns()));job=self.job();job.deadline=time.time()-1
        with self.assertRaisesRegex(prepare.PreparationError,'preparation_deadline'):job.run(self.access())
    def test_existing_or_symlink_root_is_never_overwritten(self):
        self.root.mkdir();(self.root/'witness').write_text('keep')
        with self.assertRaisesRegex(prepare.PreparationError,'scratch_exists'):self.job()
        self.assertEqual((self.root/'witness').read_text(),'keep')
        link=self.directory/'link';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(prepare.PreparationError,'scratch_symlink'):
            prepare.Preparation(link,self.manifest,'native',time.time()+5,allow_loopback_fixture=True)
    def test_child_failure_cleans_source_payload(self):
        job=self.job(app=self.directory/'no-binaries')
        with self.assertRaises(FileNotFoundError):job.run(self.access())
        self.assertFalse(job.stage.exists());self.assertFalse(job.base.exists())
    def test_deadline_stops_actual_child_process_group(self):
        job=self.job();job.root.mkdir();job.stage.mkdir()
        child=job.spawn([sys.executable,'-c','import time;time.sleep(30)'])
        job.deadline=time.time()+.15
        try:
            with self.assertRaisesRegex(prepare.PreparationError,'preparation_deadline'):job.wait([child])
        finally:job.stop()
        self.assertIsNotNone(child.poll())

    @unittest.skipUnless(Path('/app/bin/typeahead-tantivy').is_file(),'requires frozen Linux qualification image')
    def test_actual_exporter_importer_and_native_overlay(self):
        job=self.job();result=job.run(self.access())
        self.assertTrue(result['prepared']);self.assertFalse(result['serving_started'])
        evidence=result['evidence'];self.assertTrue(evidence['direct_stream'])
        self.assertEqual(evidence['import']['expiry_coverage']['input_actors_checked'],32)
        self.assertTrue((job.base/'tantivy/index/meta.json').exists())
        self.assertFalse(list(job.base.rglob('*.jsonl')))
        self.assertEqual(sha(job.base/'source/index.db.prefix'),self.manifest['prefix_map']['sha256'])
        legacy=self.directory/'legacy.db'
        with closing(sqlite3.connect(legacy)) as db:
            blob=bytearray(struct.pack('<BBf',1,False,1.0))
            for value in ['bad-expiry.fixture.test','Actor','','[{"val":"spam","src":"did:plc:ar7c4by46qjdydhdevvrndac","exp":"unsupported expiry"}]','https://local.test','','{}']:
                value=value.encode();blob.extend(struct.pack('<H',len(value)));blob.extend(value)
            db.execute('UPDATE actor_compact_base SET blob=? WHERE did=?',(blob,'did:fixture:00'));db.commit()
        invalid=self.directory/'unsupported-release';build(legacy,invalid)
        previous_release=self.release
        try:
            type(self).release=invalid
            manifest=copy.deepcopy(self.manifest);manifest['sha256']=sha(invalid/'index.db');manifest['prefix_map']['byte_size']=(invalid/'index.db.prefix').stat().st_size;manifest['prefix_map']['sha256']=sha(invalid/'index.db.prefix')
            invalid_job=prepare.Preparation(self.directory/'unsupported-job',manifest,'tantivy',time.time()+60,allow_loopback_fixture=True)
            with self.assertRaisesRegex(prepare.PreparationError,'expiry_coverage_incomplete'):invalid_job.run(self.access())
            self.assertFalse(invalid_job.base.exists());self.assertFalse(invalid_job.stage.exists())
        finally:type(self).release=previous_release
        # The production native builder supplies a genuinely readable native release,
        # independently of the actor-only reference fixture used above.
        real=self.directory/'native-source.db'
        with closing(sqlite3.connect(real)) as db:
            db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
            db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(f'did:fixture:{i:02}',f'actor{i}.fixture.test',f'Actor {i}','','[]','https://local.test','','{}',0,i,1793230000) for i in range(32)]);db.commit()
        subprocess.run(['/app/bin/typeahead-ingester'],env={'PATH':os.environ.get('PATH',''),'MODE':'indexer','INDEX_CHANNEL':'local','INDEX_SOURCE':'export','INDEX_SAMPLE_LIMIT':'0','INDEX_PDS_MAX_ACTORS':'10000','INDEX_REUSE_SOURCE':str(real),'INDEX_BUILD_ROOT':str(self.directory/'build'),'INDEXER_VERSION':'qualification-full-tiny'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=60)
        legacy_snapshot=next((self.directory/'build').glob('build-*/index.db'))
        compact=self.directory/'native-compact';build(legacy_snapshot,compact);snapshot=compact/'index.db'
        old_release=self.release;old_manifest=self.manifest
        try:
            type(self).release=snapshot.parent
            with closing(sqlite3.connect(snapshot)) as db:meta=dict(db.execute('SELECT key,value FROM index_meta'))
            manifest=copy.deepcopy(old_manifest);manifest.update(active_build=meta['build_id'],actor_count=int(meta['actor_count']),source_watermark=int(meta['source_watermark']),byte_size=snapshot.stat().st_size,sha256=sha(snapshot),snapshot_url=f'releases/search/{meta["build_id"]}/index.db')
            prefix=Path(str(snapshot)+'.prefix');manifest['prefix_map']={'object_key':manifest['snapshot_url']+'.prefix','byte_size':prefix.stat().st_size,'sha256':sha(prefix)}
            root=self.directory/'native-job'
            native=prepare.Preparation(root,manifest,'native',time.time()+90,allow_loopback_fixture=True)
            access={'build_id':manifest['active_build'],'database':self.origin+'/'+manifest['snapshot_url'],'prefix':self.origin+'/'+manifest['prefix_map']['object_key']}
            value=native.run(access);self.assertTrue(value['evidence']['independent_empty_overlay'])
            self.assertTrue((native.base/'source/index.db.prefix.admitted').exists())
            bound_prefix=native.base/'source/index.db.prefix'
            admission=bound_prefix.with_name(bound_prefix.name+'.admitted').read_bytes()
            self.assertEqual(int.from_bytes(admission[72:88],'little',signed=True),bound_prefix.stat().st_ctime_ns)
            with closing(sqlite3.connect(f'file:{native.base}/overlay.db?mode=ro',uri=True)) as db:self.assertEqual(db.execute('SELECT count(*) FROM actor_overlay').fetchone()[0],0)
        finally:type(self).release=old_release

if __name__=='__main__':unittest.main()
