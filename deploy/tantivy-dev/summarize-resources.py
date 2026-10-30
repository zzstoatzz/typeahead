import hashlib,json,os
from pathlib import Path

def counters(text):
 result={}
 for line in (text or '').splitlines():
  parts=line.replace(':','',1).split()
  if len(parts)>1:
   try:result[parts[0]]=int(parts[1])*(1024 if len(parts)>2 and parts[2]=='kB' else 1)
   except ValueError:pass
 return result

def pressure(text):
 result={}
 for line in (text or '').splitlines():
  parts=line.split()
  for token in parts[1:]:
   if token.startswith('total='):result[parts[0]]=int(token[6:])
 return result

def delta(first,last):
 return {key:last[key]-first[key] for key in first.keys()&last.keys()}

def summarize(path):
 if path.stat().st_size>8*1024**2:raise RuntimeError('resource receipt exceeds 8 MiB')
 raw=path.read_bytes();lines=raw.splitlines();rows=[];incomplete=0
 for i,line in enumerate(lines):
  try:rows.append(json.loads(line))
  except ValueError:
   if i!=len(lines)-1 or raw.endswith(b'\n'):raise
   incomplete+=1
 if not 2<=len(rows)<=240:raise RuntimeError('resource sample count outside2..240')
 span=rows[-1]['time']-rows[0]['time']
 if span<=0:raise RuntimeError('resource clock did not advance')
 memory=[counters(row['meminfo']) for row in rows]
 server=lambda row:[p for p in row['processes'] if 'typeahead' in p['comm']]
 rss=[sum(counters(p['status']).get('VmRSS',0) for p in server(row)) for row in rows]
 result={'samples':len(rows),'incomplete_final_samples':incomplete,'start':rows[0]['time'],'end':rows[-1]['time'],'span_s':span,'source_sha256':hashlib.sha256(raw).hexdigest(),'source_bytes':len(raw),'vm_memory_total_bytes':sorted({m['MemTotal'] for m in memory}),'minimum_available_bytes':min(m['MemAvailable'] for m in memory),'maximum_server_rss_bytes':max(rss),'maximum_swap_used_bytes':max(m.get('SwapTotal',0)-m.get('SwapFree',0) for m in memory),'server_pids':sorted({p['pid'] for row in rows for p in server(row)})}
 result['vmstat_delta']={k:v for k,v in delta(counters(rows[0]['vmstat']),counters(rows[-1]['vmstat'])).items() if k in ['pgfault','pgmajfault','pswpin','pswpout','oom_kill','pgscan_direct','pgscan_kswapd','allocstall_normal','allocstall_dma32']}
 result['cgroup_memory_events_delta']=delta(counters(rows[0]['memory_events']),counters(rows[-1]['memory_events'])) if rows[0]['memory_events'] is not None and rows[-1]['memory_events'] is not None else None
 result['cgroup_cpu_delta']=delta(counters(rows[0]['cpu_stat']),counters(rows[-1]['cpu_stat'])) if rows[0]['cpu_stat'] is not None and rows[-1]['cpu_stat'] is not None else None
 for field in ['memory_pressure','io_pressure']:
  changes=delta(pressure(rows[0][field]),pressure(rows[-1][field]));result[field]={'total_us_delta':changes,'percent_of_elapsed':{k:v/(span*1000000)*100 for k,v in changes.items()}}
 initial={p['pid']:counters(p['io']) for p in server(rows[0])}
 final={p['pid']:counters(p['io']) for p in server(rows[-1])}
 if not initial or not final:raise RuntimeError('serving process missing at resource endpoints')
 result['same_server_pids_at_endpoints']=set(initial)==set(final)
 result['server_io_delta_by_pid']={str(pid):delta(initial[pid],final[pid]) for pid in initial.keys()&final.keys()}
 result['limitations']=['VM-wide fault/pressure totals include replay and telemetry work.','RSS includes resident mapped pages; it is not heap allocation.','Filesystem cache warmth is not a physical-disk cold guarantee.']
 return result

if __name__=='__main__':
 if os.environ.get('FLY_APP_NAME')!='typeahead-tantivy-dev':raise RuntimeError('summary is cloud dev-only')
 print('TANTIVY_RESOURCE_SUMMARY '+json.dumps(summarize(Path('/data/load-resources.jsonl')),separators=(',',':')))
