import json,time,urllib.parse,urllib.request
from pathlib import Path
root=Path('/tmp/typeahead-pds-join-full')
base='https://typeahead-tantivy-b.fly.dev'
def get(path):
    req=urllib.request.Request(base+path,headers={'X-Client':'smoke-testing'})
    with urllib.request.urlopen(req,timeout=15) as response:return json.load(response)
status=get('/index/status')
expected=json.loads((root/'candidate-result.json').read_bytes())
assert status['build_id']==expected['build_id'] and status['ready']
samples=json.loads((root/'live-update-samples.json').read_bytes())
assert samples['updates_after']>=int(expected['completed_at'])
results=[]
for row in samples['samples']:
    assert row['source_updated_at']>samples['updates_after'] and not row['source_pds']
    matches=get('/search?'+urllib.parse.urlencode({'q':row['handle'],'pds':row['pds'],'limit':100}))['actors']
    wrong=get('/search?'+urllib.parse.urlencode({'q':row['handle'],'pds':'wrong.invalid','limit':100}))['actors']
    found=next((a for a in matches if a['did']==row['did']),None)
    results.append({'did':row['did'],'found':found is not None,'wrong_scope':any(a['did']==row['did'] for a in wrong),'display_matches':found is not None and found.get('displayName','')==(row['display_name'] or '')})
result={'observed_at':int(time.time()),'build_id':status['build_id'],'watermark':status['watermark'],'source_observation_revision':status['source_observation_revision'],'updates_after':samples['updates_after'],'samples':len(results),'matched':sum(r['found'] for r in results),'wrong_scope':sum(r['wrong_scope'] for r in results),'display_matches':sum(r['display_matches'] for r in results),'live_source_writes':0,'results':results}
result['passed']=len(results)>=20 and result['matched']==len(results) and not result['wrong_scope'] and result['watermark']>=max(r['source_updated_at'] for r in samples['samples'])
(root/'verification-live-updates.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='results'}))
if not result['passed']:raise SystemExit(1)
