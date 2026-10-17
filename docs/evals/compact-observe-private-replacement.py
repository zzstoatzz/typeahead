import datetime,json,subprocess,shlex,time
from pathlib import Path
root=Path(__file__).resolve().parent
output=root/'replacement-live-observations.jsonl'
expected='compact-b1792141282-189b'
def fetch(paths):
 command=shlex.join(['curl','-fsS','--max-time','20',*['http://127.0.0.1:8080'+p for p in paths]])
 r=subprocess.run(['fly','ssh','console','-a','typeahead-search-compact','--machine','781e732c264678','-C',command],capture_output=True,text=True,timeout=60)
 if r.returncode: raise RuntimeError('SSH or HTTP probe failed with exit '+str(r.returncode))
 data=[];raw=r.stdout;decoder=json.JSONDecoder()
 while raw.strip():
  raw=raw.lstrip();value,n=decoder.raw_decode(raw);data.append(value);raw=raw[n:]
 assert len(data)==len(paths)
 return data
completed_sync=None
for _ in range(120):
 row={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'ready':False}
 try:
  health,status=fetch(['/health','/index/status'])
  if completed_sync is None:
   logs=subprocess.run(['fly','logs','-a','typeahead-search-compact','-m','781e732c264678','--no-tail','--json'],capture_output=True,text=True,check=True,timeout=30).stdout
   decoder=json.JSONDecoder()
   while logs.strip():
    logs=logs.lstrip();event,n=decoder.raw_decode(logs);logs=logs[n:]
    if event['timestamp']>'2026-10-17T06:32:43Z' and 'incremental: done (' in event.get('message',''):
     completed_sync={k:event[k] for k in ['timestamp','message']}
     (root/'replacement-complete-sync.json').write_text(json.dumps(completed_sync,indent=2)+'\n')
  row['completed_sync_pass']=completed_sync
  row.update(health=health,index={k:status.get(k) for k in ['attached','build_id','snapshot_format','source_watermark','actor_count','overlay_actor_rows','overlay_compact']})
  lag=health.get('sync_lag_s',-1)
  if completed_sync and health.get('ready') and status.get('attached') and status.get('build_id')==expected and 0<=lag<=600 and status.get('actor_count',0)>0 and status.get('overlay_compact',{}).get('complete'):
   search,=fetch(['/search?q=zzstoatzz.io&limit=10']);row['probe_handles']=[a['handle'] for a in search.get('actors',[])];row['ready']=bool(row['probe_handles'] and row['probe_handles'][0]=='zzstoatzz.io')
 except (RuntimeError,ValueError,subprocess.TimeoutExpired,subprocess.CalledProcessError) as e:row['probe_error']=str(e)
 with output.open('a') as f:f.write(json.dumps(row)+'\n')
 print(json.dumps(row),flush=True)
 if row['ready']:
  (root/'replacement-live-ready.json').write_text(json.dumps(row,indent=2)+'\n');break
 time.sleep(60)
else:raise RuntimeError('Observation deadline; inspect current machine without restarting')
