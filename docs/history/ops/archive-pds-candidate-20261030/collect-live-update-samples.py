import importlib.util,json,sqlite3,sys,time
from pathlib import Path
root=Path('/tmp/typeahead-pds-join-full')
spec=importlib.util.spec_from_file_location('sync',root/'code/typeahead/services/tantivy-input/sync.py')
sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)
credentials=json.load(sys.stdin)
source=sync.Turso(credentials['TURSO_URL'],credentials['TURSO_AUTH_TOKEN'],max_bytes=8*1024*1024)
now=source.select(sync.CLOCK_SQL,[])[0]['source_now']
floor=max(now-3600,int(json.loads((root/'candidate-result.json').read_bytes())['completed_at']))
rows=source.select(sync.ACTORS_SQL,[floor,floor,'',now-60,1000])
samples=[]
with sqlite3.connect((root/'source.db').as_uri()+'?mode=ro&immutable=1',uri=True) as checkpoint, sqlite3.connect((root/'pds-fallback.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True) as mapping:
    for row in rows:
        if row.get('pds') or row.get('hidden') or row.get('labels') not in (None,'','[]') or not row.get('handle'):continue
        match=mapping.execute('SELECT pds FROM pds_fallback WHERE did=?',(row['did'],)).fetchone()
        if not match:continue
        old=checkpoint.execute('SELECT updated_at FROM actors WHERE did=?',(row['did'],)).fetchone()
        if old and old[0]>=row['updated_at']:continue
        samples.append({'did':row['did'],'handle':row['handle'],'display_name':row.get('display_name'), 'pds':match[0],'source_pds':row.get('pds'),'source_updated_at':row['updated_at'],'checkpoint_updated_at':old[0] if old else None})
        if len(samples)==30:break
result={'observed_at':int(time.time()),'updates_after':floor,'source_rows_checked':len(rows),'source_requests':source.requests,'source_bytes':source.bytes,'live_source_writes':0,'samples':samples}
(root/'live-update-samples.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='samples'}|{'samples':len(samples)}))
