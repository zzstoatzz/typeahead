"""One-off committed projection benchmark on the isolated Fly evaluation copies.
Requires /tmp/index-tests built from this checkout; never targets production.
WAL size is a sampled lower bound on peak size, at 50 ms intervals.
"""
import json,os,resource,subprocess,sys,time
from pathlib import Path
path=Path(sys.argv[1]).resolve()
if path.parent!=Path('/data/eval') or path.name not in ['plain.db','ranked.db']:
 raise ValueError('An explicit disposable evaluation copy is required')
def limits():
 resource.setrlimit(resource.RLIMIT_AS,(1536*1024**2,)*2)
 resource.setrlimit(resource.RLIMIT_CPU,(120,120))
 resource.setrlimit(resource.RLIMIT_CORE,(0,0))
started=time.monotonic(); peak=0
with open(str(path)+'.fanout.log','w') as log:
 p=subprocess.Popen(['/tmp/index-tests'],stdout=log,stderr=log,env={'PATH':'/usr/bin:/bin','TYPEAHEAD_FANOUT_SCRATCH':str(path),'TYPEAHEAD_FANOUT_ALLOW_WRITES':'disposable-copy'},preexec_fn=limits)
 while p.poll() is None:
  try:peak=max(peak,Path(str(path)+'-wal').stat().st_size)
  except FileNotFoundError:pass
  if time.monotonic()-started>300:
   p.kill();p.wait();break
  time.sleep(.05)
r={'database':path.name,'returncode':p.returncode,'wall_s':time.monotonic()-started,'observed_peak_wal_bytes_50ms':peak,'peak_rss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}
Path(str(path)+'.fanout.json').write_text(json.dumps(r,indent=2));print(json.dumps(r));print(''.join(s for s in Path(str(path)+'.fanout.log').read_text().splitlines(True) if 'FANOUT' in s or 'failed' in s or 'passed' in s));sys.exit(p.returncode)
