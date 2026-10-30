import json,os,time
from pathlib import Path

if os.environ.get('FLY_APP_NAME')!='typeahead-tantivy-dev' or os.environ.get('FLY_MACHINE_ID')!='840e95a2153308':
 raise RuntimeError('resource sampling restricted to owned dev machine')
out=Path('/data/load-resources.jsonl')
if out.exists():raise RuntimeError('resource receipt already exists')

def read(path):
 try:return Path(path).read_text().strip()
 except OSError:return None

with out.open('x') as stream:
 for _ in range(240):
  row={'time':time.time(),'meminfo':read('/proc/meminfo'),'vmstat':read('/proc/vmstat'),'diskstats':read('/proc/diskstats'),'memory_current':read('/sys/fs/cgroup/memory.current'),'memory_max':read('/sys/fs/cgroup/memory.max'),'memory_events':read('/sys/fs/cgroup/memory.events'),'memory_stat':read('/sys/fs/cgroup/memory.stat'),'memory_pressure':read('/proc/pressure/memory'),'io_pressure':read('/proc/pressure/io'),'io_stat':read('/sys/fs/cgroup/io.stat'),'cpu_stat':read('/sys/fs/cgroup/cpu.stat')}
  row['processes']=[]
  for p in Path('/proc').iterdir():
   if not p.name.isdecimal():continue
   command=read(p/'comm')
   if command and ('typeahead' in command or command=='python3'):
    row['processes'].append({'pid':int(p.name),'comm':command,'status':read(p/'status'),'io':read(p/'io')})
  stream.write(json.dumps(row,separators=(',',':'))+'\n');stream.flush();time.sleep(5)
