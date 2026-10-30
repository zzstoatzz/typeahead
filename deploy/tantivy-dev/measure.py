import hashlib,importlib.util,json,sys
from pathlib import Path
import control

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('stage_helpers',ROOT/'stage-helpers.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)

def start():
 d=control.ledger();m=control.owned(d['machine_id'])
 if d['phase']!='publish' or m['config']['guest'].get('memory_mb')!=1024:
  raise RuntimeError('resource sampling requires final published dev shape')
 digest=hashlib.sha256((ROOT/'sample-resources.py').read_bytes()).hexdigest()
 source='''import hashlib,json,os,subprocess,time
from pathlib import Path
assert os.environ.get("FLY_APP_NAME")=="typeahead-tantivy-dev"
script=Path("/tmp/tantivy-dev-sample-resources.py")
assert hashlib.sha256(script.read_bytes()).hexdigest()==DIGEST
assert not Path("/data/load-resources.jsonl").exists()
assert not Path("/data/resource-sampler.json").exists()
child=subprocess.Popen(["python3",str(script)],stdout=subprocess.DEVNULL,stderr=open("/data/resource-sampler.stderr","ab"),start_new_session=True)
receipt={"owner":"tantivy-dev-20261028","pid":child.pid,"created_at":time.time(),"script_sha256":DIGEST,"replay_log_start_bytes":Path("/data/replay.log").stat().st_size}
Path("/data/resource-sampler.json").write_text(json.dumps(receipt)+"\\n")
print("HELPER_OK "+json.dumps(receipt))
'''.replace('DIGEST',repr(digest))
 output=helpers.remote(source);receipt=json.loads(output.split('HELPER_OK ',1)[1]);(ROOT/'resource-sampler.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))

def collect():
 control.owned(control.ledger()['machine_id'])
 source='''import json,os,runpy,signal,time
from pathlib import Path
assert os.environ.get("FLY_APP_NAME")=="typeahead-tantivy-dev"
receipt=json.loads(Path("/data/resource-sampler.json").read_text());assert receipt["owner"]=="tantivy-dev-20261028"
proc=Path("/proc")/str(receipt["pid"])
if proc.exists():
 cmd=(proc/"cmdline").read_bytes().split(bytes([0]))
 if cmd[:2]==[b"python3",b"/tmp/tantivy-dev-sample-resources.py"]:os.kill(receipt["pid"],signal.SIGTERM);time.sleep(.2)
runpy.run_path("/tmp/tantivy-dev-summarize-resources.py",run_name="__main__")
print("HELPER_OK "+json.dumps({"stderr_tail":Path("/data/resource-sampler.stderr").read_text()[-1000:],"sampler_stopped":not proc.exists() or not (proc/"cmdline").read_bytes()}))
'''
 output=helpers.remote(source)
 line=next(s for s in output.splitlines() if s.startswith('TANTIVY_RESOURCE_SUMMARY '));summary=json.loads(line.split(' ',1)[1])
 summary.update(json.loads(next(s for s in output.splitlines() if s.startswith('HELPER_OK ')).split(' ',1)[1]))
 if (ROOT/'load-results.json').exists():
  load=json.loads((ROOT/'load-results.json').read_text());summary['covers_load_window']=summary['start']<=load['started_at']+5 and summary['end']>=load['finished_at']-5
 (ROOT/'resource-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
 print(json.dumps({'samples':summary['samples'],'span_s':summary['span_s'],'maximum_server_rss_bytes':summary['maximum_server_rss_bytes'],'minimum_available_bytes':summary['minimum_available_bytes'],'same_server_pids_at_endpoints':summary['same_server_pids_at_endpoints']}))

if __name__=='__main__':
 if sys.argv[1]=='start':start()
 elif sys.argv[1]=='collect':collect()
 else:raise ValueError('expected start or collect')
