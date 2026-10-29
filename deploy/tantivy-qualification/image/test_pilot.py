"""Run the exact cloud guest programs offline in a disposable bounded Linux container."""
import json
from pathlib import Path
import subprocess

HERE=Path(__file__).resolve().parent
PROGRAM=r'''
import importlib.util,json,os,pathlib,shutil,signal,subprocess,time
spec=importlib.util.spec_from_file_location('pilot_client','/app/generator.py')
client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
results=[];watermark=int(time.time())-60
for arm in ['native','tantivy']:
 root=pathlib.Path('/data/qualification/run')
 observer={'app':'typeahead-qual-container-fixture','machine_id':'fixture-'+arm,'guest':{'cpu_kind':'shared','cpus':2,'memory_mb':1024},
           'volume_ids':['fixture-'+arm+'-volume'],'final_volume_gib':20,'public_services':[],
           'machines_api_response_sha256':'a'*64,'synthetic_local_fixture':True}
 deadline=time.time()+90;nonce='local-container-pilot-nonce'
 env={**os.environ,'FLY_APP_NAME':observer['app'],'FLY_MACHINE_ID':observer['machine_id'],'FLY_MACHINE_VERSION':'fixture-version',
      'QUAL_ROLE':arm+'-control','QUAL_DEADLINE':str(deadline),'QUAL_OBSERVER_JSON':json.dumps(observer),
      'QUAL_WATERMARK':str(watermark),'QUAL_PILOT_NONCE':nonce,'ADMIN_SECRET':'fixture-loopback-only','QUAL_CONTROL_SECRET':'fixture-control-only'}
 log=pathlib.Path('/tmp/'+arm+'-pilot.log')
 with log.open('w') as stream:
  child=subprocess.Popen(['python3','/app/pilot_bootstrap.py'],env=env,stdout=stream,stderr=stream,start_new_session=True)
 try:
  try:results.append(client.exercise('http://[::1]:8080','http://[::1]:8082',nonce,deadline,arm,allow_local_fixture=True,control_secret='fixture-control-only'))
  except BaseException:
   print(log.read_text()[-5000:]);raise
 finally:
  if child.poll() is None:
   child.send_signal(signal.SIGTERM)
   try:child.wait(timeout=8)
   except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
  if root.exists():shutil.rmtree(root)
assert results[0]['identity']['corpus_sha256']==results[1]['identity']['corpus_sha256']
assert results[0]['identity']['updates_sha256']==results[1]['identity']['updates_sha256']
assert results[0]['identity']['engine_binary_sha256']!=results[1]['identity']['engine_binary_sha256']
print('OFFLINE_PILOT '+json.dumps({'passed':True,'arms':results,'network':'none','memory_limit_bytes':1073741824,'cpus':2,'cloud_calls':0}))
'''

def main():
    result=subprocess.run(['docker','run','--rm','-i','--network','none','--memory','1g','--cpus','2','--platform','linux/amd64',
                           '--entrypoint','timeout','typeahead-qualification:paired-local','210','python3','-'],
                          input=PROGRAM,capture_output=True,text=True,timeout=230)
    if result.returncode:raise RuntimeError(result.stdout[-6000:]+result.stderr[-3000:])
    line=next(x for x in result.stdout.splitlines() if x.startswith('OFFLINE_PILOT '))
    report=json.loads(line.removeprefix('OFFLINE_PILOT '))
    (HERE/'offline-pilot-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

if __name__=='__main__':main()
