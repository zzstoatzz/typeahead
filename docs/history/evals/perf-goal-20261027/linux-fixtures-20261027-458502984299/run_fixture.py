STATE='/var/lib/typeahead-perf-fixtures/20261027-458502984299'
RUN='20261027-458502984299'
CODE='/var/lib/typeahead-perf-fixture-code/20261027-458502984299'
import json,os,signal,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(STATE)
PAYLOAD=ROOT/'payload'
def cleanup():
 if (ROOT/'owner.json').exists():
  assert json.loads((ROOT/'owner.json').read_text())['run']==RUN
  assert not PAYLOAD.is_symlink()
  before=shutil.disk_usage(ROOT).free
  if PAYLOAD.exists():shutil.rmtree(PAYLOAD)
  (ROOT/'cleanup.json').write_text(json.dumps({'payload_absent':not PAYLOAD.exists(),'free_before':before,'free_after':shutil.disk_usage(ROOT).free}))
if '--cleanup-only' in sys.argv:cleanup();raise SystemExit()
(ROOT/'owner.json').write_text(json.dumps({'run':RUN,'owner':'Typeahead performance goal','payload_budget_bytes':256*1024**2}))
PAYLOAD.mkdir(exist_ok=False)
env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TMPDIR':str(PAYLOAD),'TYPEAHEAD_EVAL_SERVER':CODE+'/eval-server','TYPEAHEAD_COMPACT_LIBRARY':CODE+'/libtypeahead_compact.so'}
start=time.monotonic();peak=0;p=None;report={}
try:
 with (ROOT/'test.log').open('wb') as output:
  p=subprocess.Popen(['/usr/bin/python3','-m','unittest','discover','-s',CODE+'/perf','-p','test_evaluator.py'],env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
  while p.poll() is None:
   size=sum(f.stat().st_size for f in PAYLOAD.rglob('*') if f.is_file());peak=max(peak,size)
   if size>256*1024**2 or time.monotonic()-start>120:raise RuntimeError('fixture storage/wall budget')
   time.sleep(.05)
 report['exit_code']=p.returncode
except BaseException as e:report['error']=type(e).__name__+': '+str(e)
finally:
 if p and p.poll() is None:
  os.killpg(p.pid,signal.SIGTERM)
  try:p.wait(timeout=3)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
 report.update(elapsed_s=time.monotonic()-start,peak_payload_bytes=peak)
 (ROOT/'report.json').write_text(json.dumps(report,indent=2))
