import hashlib
import copy
import http.server
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('full_generator',HERE/'full_generator.py')
gen=importlib.util.module_from_spec(spec);spec.loader.exec_module(gen)
NONCE='synthetic-full-generator-fixture'
SECRET='synthetic-local-control-capability'
BINARY='a'*64
ORACLE='b'*64

class Fixture:
    def __init__(self,root,arm='native'):
        self.arm=arm;self.root=root;self.state_path=root/(arm+'-state.json');self.state_path.write_text('0')
        self.lock=threading.Lock();self.acked=0;self.pending=None;self.calls=[];self.delay=0;self.override=None;self.prep_status=200;self.metrics_count=0
        self.cases=[{'id':'a','q':'alpha','limit':10,'native_shared_features':True},{'id':'b','q':'beta','limit':10,'prefer_pds':'local.test','native_shared_features':False}]
        cases={'schema_version':1,'source_kind':'frozen-materialized-hidden','cases':self.cases}
        actors=[{'did':f'did:fixture:{i}'}for i in range(64)]
        feed={'schema_version':1,'build_id':'fixture-build','source_watermark':1000,'source_kind':'frozen-materialized-hidden','baseline_actors':actors,'batches':[{'cursor':i,'expected_cursor':i-1,'actors':actors}for i in range(1,10)]}
        self.truth={cursor:{case['id']:{'status':200,'ids':[f'did:fixture:{cursor}:{case["id"]}'],'native_shared_features':case['native_shared_features']}for case in self.cases}for cursor in range(10)}
        receipt={'source_build_id':'fixture-build','source_watermark':1000,'actors_checked':64,'source_actor_count':64,'queries_sha256':gen.digest(gen.encoded(cases)),'updates_sha256':gen.digest(gen.encoded(feed)),'source_kind':'frozen-materialized-hidden','live_policy_or_freshness_qualification':False,'expected_ids_derived_from_engine_responses':False,'raw_source_sha256':'c'*64,'actor_stream_sha256':'d'*64,'identity_policy_sha256':'e'*64,'oracle_binary_sha256':ORACLE}
        oracle={'schema_version':1,'receipt':receipt,'checkpoints':[{'cursor':cursor,'queries':answers}for cursor,answers in self.truth.items()]}
        self.inputs={name:gen.encoded(value)for name,value in [('cases',cases),('updates',feed),('oracle',oracle)]}
        self.prepared={'schema_version':1,'mode':'full','arm':arm,'state':'prepared','prepared':True,'nonce':NONCE,'app':gen.APP,'machine_id':arm+'-fixture','volume_id':arm+'-volume','guest':dict(gen.SHAPE),'final_volume_gib':20,'public_services':[],'observer_receipt_sha256':'f'*64,'corpus_sha256':'1'*64,'updates_sha256':gen.digest(self.inputs['updates']),'build_id':'fixture-build','source_watermark':1000,'actor_count':64,'affected_actors':64,'feed_batches':9,'source_kind':'frozen-release-snapshot','source_ready_attestation':False,'oracle_ready':arm=='native','cases_sha256':gen.digest(self.inputs['cases']),'oracle_sha256':gen.digest(self.inputs['oracle'])}
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def emit(self,status,value,headers=None,raw=False):
                data=value if raw else gen.encoded(value)
                try:
                    self.send_response(status);self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close')
                    for key,value in (headers or {}).items():self.send_header(key,str(value))
                    self.end_headers();self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_GET(self):
                path=urllib.parse.urlsplit(self.path).path
                if path=='/prepared':self.emit(owner.prep_status,owner.summary());return
                if path=='/__evaluation':
                    value={key:owner.summary()[key]for key in ['arm','nonce','machine_id','volume_id','guest','corpus_sha256','updates_sha256','observer_receipt_sha256','source_kind','source_ready_attestation']}
                    value.update(ready=True,runtime_process_verified=True,process_executable_sha256=BINARY,engine_binary_sha256=BINARY,engine_settings={'retrieval_strategy':'global-merge','writer_memory_mb':128,'concurrency':4,'max_candidates':10000})
                    self.emit(200,value);return
                if path=='/search':
                    with owner.lock:low=owner.acked;high=max(low,owner.pending or 0)
                    q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('q',[''])[0];case='a'if q=='alpha'else'b'
                    if owner.delay:time.sleep(owner.delay)
                    with owner.lock:high=max(high,owner.acked,owner.pending or 0)
                    ids=owner.truth[low][case]['ids'];headers={'X-Qualification-Cursor-Min':low,'X-Qualification-Cursor-Max':high}
                    if owner.override:ids,headers=owner.override
                    self.emit(200,{'actors':[{'did':did}for did in ids]},headers);return
                if self.headers.get('X-Pilot-Authorization')!=SECRET:self.emit(403,{});return
                if path.startswith('/inputs/')and path.split('/')[-1]in owner.inputs:self.emit(200,owner.inputs[path.split('/')[-1]],raw=True);return
                if path=='/metrics':
                    owner.metrics_count+=1;n=owner.metrics_count
                    self.emit(200,owner.summary()|{'telemetry':{'schema_version':1,'summed_process_rss_bytes':1000,'guest_meminfo_bytes':{'MemAvailable':2000,'Cached':500},'guest_vmstat':{'pgmajfault':n,'oom_kill':0},'guest_cpu_ticks':{'steal':n},'cgroup':{'memory_current_bytes':1500,'cpu_stat':{'usage_usec':n,'throttled_usec':0,'nr_throttled':0}},'guest_diskstats':[]}});return
                self.emit(404,{})
            def do_POST(self):
                if self.headers.get('X-Pilot-Authorization')!=SECRET:self.emit(403,{});return
                if self.headers.get('Content-Length','0')!='0':self.emit(400,{});return
                owner.calls.append(self.path)
                if self.path.startswith('/apply/'):
                    cursor=int(self.path.split('/')[-1])
                    with owner.lock:
                        if cursor==owner.acked:replay=True
                        elif cursor==owner.acked+1:replay=False;owner.pending=cursor
                        else:self.emit(409,{});return
                    if not replay:
                        time.sleep(.001);owner.state_path.write_text(str(cursor))
                        with owner.lock:owner.acked=cursor;owner.pending=None
                    self.emit(200,{'acked':cursor,'replay':replay});return
                if self.path=='/restart':
                    with owner.lock:owner.acked=int(owner.state_path.read_text());owner.pending=None
                if self.path in {'/serve','/pause','/restart'}:self.emit(200,{'serving':self.path!='/pause'});return
                self.emit(404,{})
            def log_message(self,*_):pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        url='http://127.0.0.1:'+str(self.server.server_port)
        self.search=gen.Client(url,8080,fixture=True);self.control=gen.Client(url,8082,SECRET,fixture=True)
    def summary(self):return self.prepared|{'acked':self.acked}
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(1)

class FullGenerator(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.fixtures=[]
    def tearDown(self):
        for fixture in self.fixtures:fixture.close()
        self.temp.cleanup()
    def fixture(self,arm='native'):
        fixture=Fixture(self.root,arm);self.fixtures.append(fixture);return fixture
    def run_phase(self,fixture,state=None,**kwargs):
        detail=gen.Detail(self.root/('requests-'+str(time.monotonic_ns())+'.jsonl'))
        try:return gen.phase(fixture.search,fixture.cases,fixture.truth,state or gen.State(),kwargs.pop('count',4),kwargs.pop('rate',1000),detail,**kwargs)
        finally:detail.close()
    def test_real_http_cursor_window_accepts_pending_generation_but_rejects_future(self):
        fixture=self.fixture();state=gen.State();state.begin(1)
        fixture.override=(fixture.truth[1]['a']['ids'],{'X-Qualification-Cursor-Min':0,'X-Qualification-Cursor-Max':1})
        fixture.cases=fixture.cases[:1]
        self.assertEqual(self.run_phase(fixture,state)['all']['truth_mismatches'],0)
        fixture.override=(fixture.truth[2]['a']['ids'],{'X-Qualification-Cursor-Min':0,'X-Qualification-Cursor-Max':1})
        self.assertEqual(self.run_phase(fixture,state)['all']['truth_mismatches'],4)
        fixture.override=(fixture.truth[9]['a']['ids'],{'X-Qualification-Cursor-Min':0,'X-Qualification-Cursor-Max':9})
        self.assertEqual(self.run_phase(fixture,state)['all']['transport_failures'],4)
        fixture.override=(fixture.truth[0]['a']['ids'],{})
        self.assertEqual(self.run_phase(fixture,state)['all']['transport_failures'],4)
    def test_real_http_total_deadline_and_bounded_queue_report_every_request(self):
        fixture=self.fixture();fixture.delay=.15
        begin=time.monotonic();result=self.run_phase(fixture,count=40,rate=10000,job_deadline=.03,queue_size=2)
        self.assertEqual(result['all']['requests'],40);self.assertEqual(result['all']['transport_failures'],40)
        self.assertLess(time.monotonic()-begin,.5)
    def test_exact_inputs_resource_identity_and_runtime_hash_rejection(self):
        native=self.fixture();tantivy=self.fixture('tantivy')
        prepared={'native':native.summary(),'tantivy':tantivy.summary()};gen.validate_prepared(prepared,NONCE,fixture=True)
        cases,truth,proof=gen.load_inputs(native.control,prepared['native'],ORACLE,self.root,fixture=True)
        self.assertEqual(len(cases),2);self.assertEqual(list(truth),list(range(10)));self.assertEqual(proof['oracle_binary_sha256'],ORACLE)
        with self.assertRaisesRegex(gen.QualificationError,'oracle_binary_differs'):gen.load_inputs(native.control,prepared['native'],'9'*64,self.root,fixture=True)
        bad=dict(prepared['native'],source_watermark=True)
        with self.assertRaises(gen.QualificationError):gen.validate_prepared({'native':bad,'tantivy':prepared['tantivy']},NONCE,fixture=True)
        with self.assertRaisesRegex(gen.QualificationError,'evaluation_engine_binary'):gen.verify_identity(native.search,native.control,'native',prepared['native'],NONCE,'9'*64,fixture=True)
        with self.assertRaises(gen.QualificationError):gen.origin('http://typeahead-tantivy-dev.fly.dev:8080',8080)
        with self.assertRaises(gen.QualificationError):gen.origin('http://abc.vm.typeahead-qual-wrong.internal:8080',8080)
    def test_updates_replay_restart_and_quiescent_checks_use_real_control_and_disk(self):
        fixture=self.fixture();rows=fixture.summary()
        phases=(('first_touch',4,1000),('warm',8,1000),('mixed',32,500))
        result=gen.exercise('native',fixture.search,fixture.control,rows,fixture.cases,fixture.truth,NONCE,BINARY,self.root,time.time()+120,fixture=True,phases=phases,update_offset=.001,update_interval=.004)
        self.assertEqual([row['cursor']for row in result['updates']],list(range(1,10)))
        for cursor in range(1,10):self.assertEqual(fixture.calls.count('/apply/'+str(cursor)),3 if cursor==9 else 2)
        self.assertEqual(fixture.state_path.read_text(),'9');self.assertIn('/restart',fixture.calls);self.assertEqual(fixture.calls[-1],'/pause')
        self.assertEqual(result['post_restart_quiescent']['all']['truth_mismatches'],0);self.assertFalse(result['post_restart_quiescent']['performance_phase'])
        self.assertTrue(all(result['phases'][name]['all']['transport_failures']==0 for name,_,_ in phases))
        self.assertGreater(result['resources']['samples'],1)
    def test_failed_http_observation_requires_authoritative_terminal_state(self):
        fixture=self.fixture();fixture.prep_status=503;observed=[]
        def provider(machine):observed.append(machine);return 'destroyed'
        with self.assertRaisesRegex(gen.QualificationError,'provider_terminal_destroyed'):gen.wait_prepared({'native':fixture.control},time.time()+1,NONCE,provider,pause=.001)
        self.assertEqual(observed,[None])
        begin=time.monotonic()
        with self.assertRaisesRegex(gen.QualificationError,'observation_unknown'):gen.wait_prepared({'native':fixture.control},time.time()+.04,NONCE,provider=lambda _:None,pause=.001)
        self.assertGreaterEqual(time.monotonic()-begin,.035);self.assertEqual(fixture.calls,[])
    def test_provider_evidence_rejects_shape_ownership_and_runtime_substitution(self):
        prepared={'machine_id':'abc123','volume_id':'vol_fixture'}
        image='registry.fly.io/'+gen.APP+'@sha256:'+'a'*64;run='0123456789ab'
        machine={'id':'abc123','instance_id':'instance-1','state':'started','config':{'image':image,'guest':dict(gen.SHAPE),'metadata':{'owner':'tantivy-qualification-20261029','run_id':run,'role':'native-control'},'mounts':[{'volume':'vol_fixture','path':'/data'}]}}
        volume={'id':'vol_fixture','size_gb':20,'encrypted':True,'attached_machine_id':'abc123'}
        evaluation={'runtime_machine_id':'abc123','runtime_instance_id':'instance-1'}
        def validate(m=machine,v=volume,e=evaluation):return gen.validate_provider_records('native',prepared,e,m,v,image,run)
        self.assertEqual(validate()['volume_gib'],20)
        mutations=[('state','stopped'),('instance_id','instance-2')]
        for key,value in mutations:
            bad=copy.deepcopy(machine);bad[key]=value
            with self.subTest(key=key),self.assertRaises(gen.QualificationError):validate(m=bad)
        for key,value in [('guest',dict(gen.SHAPE,memory_mb=2048)),('image',image+'changed'),('services',[{'ports':[{'port':443}]}]),('env',{'FLY_MACHINE_ID':'abc123'}),('metadata',{'owner':'other'}),('mounts',[{'volume':'other','path':'/data'}])]:
            bad=copy.deepcopy(machine);bad['config'][key]=value
            with self.subTest(key=key),self.assertRaises(gen.QualificationError):validate(m=bad)
        for key,value in [('encrypted',False),('size_gb',40),('attached_machine_id','other')]:
            bad=dict(volume);bad[key]=value
            with self.subTest(key=key),self.assertRaises(gen.QualificationError):validate(v=bad)

    def test_qualification_requires_complete_resource_observation_and_rejects_swap(self):
        stats={'requests':1,'transport_failures':0,'truth_mismatches':0,'p95_ms':10,'p99_ms':10}
        phase={'all':stats,'shared_native_features':stats,'new_features':dict(stats,requests=0)}
        resources={'samples':3,'sample_failures':0,'guest_available_min_bytes':1000000,'guest_major_faults_delta':0,'free_disk_min_bytes':2*1024**3,'oom_kill_delta':0,'swap_in_pages_delta':0,'swap_out_pages_delta':0,'maximum_sample_gap_seconds':2}
        arms={arm:{'phases':{name:copy.deepcopy(phase)for name,_,_ in gen.PHASES},'post_restart_quiescent':copy.deepcopy(phase),'resources':dict(resources)}for arm in ['native','tantivy']}
        self.assertTrue(gen.gates(arms)['passed'])
        for key,value in [('sample_failures',1),('swap_in_pages_delta',1),('swap_out_pages_delta',1),('oom_kill_delta',1),('maximum_sample_gap_seconds',9),('free_disk_min_bytes',1024),('guest_major_faults_delta',None)]:
            bad=copy.deepcopy(arms);bad['tantivy']['resources'][key]=value
            with self.subTest(key=key):self.assertFalse(gen.gates(bad)['passed'])

    def test_native_wrong_ids_keep_timings_and_missing_metrics_remain_unknown(self):
        fixture=self.fixture();fixture.cases=fixture.cases[:1];fixture.override=(['did:fixture:wrong'],{'X-Qualification-Cursor-Min':0,'X-Qualification-Cursor-Max':0})
        result=self.run_phase(fixture,count=8)
        self.assertEqual(result['shared_native_features']['requests'],8);self.assertEqual(result['shared_native_features']['truth_mismatches'],8);self.assertEqual(result['shared_native_features']['transport_failures'],0)
        self.assertIsNotNone(result['shared_native_features']['p99_ms'])
        empty=gen.summarize_metrics([{'schema_version':1},{'schema_version':1}]);self.assertIsNone(empty['guest_major_faults_delta']);self.assertIsNone(empty['guest_available_min_bytes'])
        arms={arm:{'phases':{name:result for name,_,_ in gen.PHASES},'post_restart_quiescent':result}for arm in ['native','tantivy']}
        gate=gen.gates(arms);self.assertEqual(gate['comparison_status'],'conclusive');self.assertFalse(gate['tantivy_truth_and_transport_passed'])
        result['shared_native_features']['transport_failures']=1
        self.assertEqual(gen.gates(arms)['comparison_status'],'inconclusive_native_shared_transport_failures')

if __name__=='__main__':unittest.main()
