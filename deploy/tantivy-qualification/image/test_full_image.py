"""Exercise the complete full-run data/control path with tiny offline Linux inputs."""
import json
from pathlib import Path
import subprocess

HERE=Path(__file__).resolve().parent
PROGRAM=r'''
import hashlib,http.server,json,os,pathlib,signal,socket,sqlite3,subprocess,sys,tempfile,threading,time
sys.path.insert(0,'/app')
import full_bootstrap as boot
import full_generator as gen
sys.path.insert(0,'/reference')
from reference_build import build as compact
root=pathlib.Path(tempfile.mkdtemp(prefix='full-image-integration-'))
import full_fixture
fixture=full_fixture.build(root/'source-fixture')
manifest=fixture['manifest'];database=fixture['source']/'index.db';prefix=fixture['source']/'index.db.prefix';build=manifest['active_build']
def sha(p):
 with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
class Files(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  p=prefix if self.path.endswith('.prefix') else database
  data=p.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*_):pass
files=http.server.ThreadingHTTPServer(('127.0.0.1',0),Files);threading.Thread(target=files.serve_forever,daemon=True).start()
access={'build_id':build,'database':f'http://127.0.0.1:{files.server_port}/releases/search/{build}/index.db','prefix':f'http://127.0.0.1:{files.server_port}/releases/search/{build}/index.db.prefix'}
nonce='actual-offline-full-image-nonce';secret='fixture-control-capability';os.environ['ADMIN_SECRET']='fixture-admin-capability'
class Local(boot.FullQualification):
 def engine_env(self):
  return {**super().engine_env(),'FLY_APP_NAME':self.observer['app'],'FLY_MACHINE_ID':self.observer['machine_id'],'FLY_MACHINE_VERSION':'synthetic-local-version'}
services={};servers={};controls={};observers={}
try:
 for arm in ['native','tantivy']:
  observer={'app':'typeahead-qual-image-fixture','machine_id':'fixture-'+arm,'guest':dict(gen.SHAPE),'volume_ids':['vol_fixture_'+arm],'final_volume_gib':20,'public_services':[],'machines_api_response_sha256':'a'*64}
  service=Local(root/arm,manifest,arm,observer,nonce,time.time()+240,fixture=True)
  services[arm]=service;service.prepare(access)
  if arm=='native':service.prepare_oracle(fixture_cases=128)
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),boot.control_handler(service,secret));servers[arm]=server
  threading.Thread(target=server.serve_forever,daemon=True).start()
  controls[arm]=gen.Client(f'http://127.0.0.1:{server.server_port}',8082,secret,fixture=True)
  observers[arm]=service.summary()
 gen.validate_prepared(observers,nonce,fixture=True)
 cases,truth,proof=gen.load_inputs(controls['native'],observers['native'],sha(pathlib.Path('/app/tantivy-raw-oracle')),root,fixture=True)
 results={}
 for arm in ['native','tantivy']:
  search=gen.Client('http://[::1]:8080',8080,fixture=True)
  binary=sha(pathlib.Path('/app/bin/eval-server' if arm=='native' else '/app/bin/typeahead-tantivy'))
  phases=(('first_touch',len(cases),50),('warm',64,50),('mixed',128,50))
  try:results[arm]=gen.exercise(arm,search,controls[arm],observers[arm],cases,truth,nonce,binary,root,time.time()+180,fixture=True,phases=phases,update_offset=.1,update_interval=.1)
  except BaseException:
   print('LOCAL_FAILURE '+json.dumps({'arm':arm,'state':services[arm].summary()}));raise
  assert results[arm]['durable_cursor_replay_confirmed']
  assert all(row['all']['transport_failures']==0 for row in results[arm]['phases'].values())
 assert all(row['all']['truth_mismatches']==0 for row in results['tantivy']['phases'].values())
 assert results['tantivy']['post_restart_quiescent']['all']['truth_mismatches']==0
 print('FULL_IMAGE_INTEGRATION '+json.dumps({'passed':True,'actors':128,'cases':len(cases),'feed_batches':9,'affected_actors':64,'actual_native_builder':True,'actual_rust_engine':True,'actual_independent_oracle':True,'actual_private_proxy':True,'actual_generator':True,'runtime_process_provider_proof':False,'full_corpus_qualified':False,'performance_qualified':False,'network':'none','cloud_calls':0,'native_truth_mismatches':sum(row['all']['truth_mismatches']for row in results['native']['phases'].values()),'tantivy_truth_mismatches':0}))
finally:
 for service in services.values():service.close()
 for server in servers.values():server.shutdown();server.server_close()
 files.shutdown();files.server_close()
'''

def main():
    result=subprocess.run(['docker','run','--rm','-i','--network','none','--memory','1g','--cpus','2','--platform','linux/amd64',
                           '-v',str(HERE.parents[2]/'scripts/snapshot_compaction')+':/reference:ro',
                           '--entrypoint','timeout','typeahead-qualification:full-local','240','python3','-'],
                          input=PROGRAM,capture_output=True,text=True,timeout=260)
    if result.returncode:raise RuntimeError(result.stdout[-2000:]+result.stderr[-5000:])
    line=next(x for x in result.stdout.splitlines() if x.startswith('FULL_IMAGE_INTEGRATION '))
    report=json.loads(line.removeprefix('FULL_IMAGE_INTEGRATION '))
    (HERE/'full-image-integration.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))

if __name__=='__main__':main()
