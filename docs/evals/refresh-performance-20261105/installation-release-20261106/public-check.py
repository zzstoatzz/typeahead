import hashlib,json,time,urllib.parse,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parent;plan=json.loads((root/'plan.json').read_bytes());expected=plan['machine_id']
rows=[]
for scoped in [False,True]:
 bodies=[]
 for endpoint in ['tech.waow.typeahead.searchActors','app.bsky.actor.searchActorsTypeahead']:
  query=dict(q='ZzSToaTzz.iO',limit=10)
  if scoped:query['pds']='https://pds.zzstoatzz.io'
  url='https://typeahead.waow.tech/xrpc/'+endpoint+'?'+urllib.parse.urlencode(query)
  for attempt in range(5):
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'typeahead-install-release/1','Cache-Control':'no-cache'}),timeout=15) as r:
    raw=r.read(1048577);headers=dict(r.headers);assert len(raw)<=1048576
   machine=next((v for k,v in headers.items() if k.lower()=='x-typeahead-machine-id'),None)
   if machine==expected:break
   if attempt==4:raise RuntimeError('public response still names another backend')
   time.sleep(30)
  actors=json.loads(raw)['actors'];assert actors and actors[0]['handle'].lower()=='zzstoatzz.io'
  assert next(v for k,v in headers.items() if k.lower()=='x-typeahead-engine')=='tantivy'
  assert not any(k.lower()=='x-typeahead-failover' for k in headers)
  bodies.append(raw);rows.append(dict(endpoint=endpoint,scoped=scoped,machine_id=machine,body_sha256=hashlib.sha256(raw).hexdigest(),headers={k:v for k,v in headers.items() if k.lower().startswith(('x-typeahead','server-timing','cf-cache'))}))
 assert bodies[0]==bodies[1]
foreign=[]
for endpoint in ['tech.waow.typeahead.searchActors','app.bsky.actor.searchActorsTypeahead']:
 query=dict(q='ZzSToaTzz.iO',limit=10,pds='https://bsky.social')
 url='https://typeahead.waow.tech/xrpc/'+endpoint+'?'+urllib.parse.urlencode(query)
 with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'typeahead-install-release/1'}),timeout=15) as r:
  raw=r.read(1048577);assert len(raw)<=1048576
  assert r.headers['X-Typeahead-Machine-Id']==expected and r.headers['X-Typeahead-Engine']=='tantivy'
 assert all(a['did']!='did:plc:xbtmt2zjwlrfegqvch7fboei' for a in json.loads(raw)['actors'])
 foreign.append(raw)
 rows.append(dict(endpoint=endpoint,scoped=True,pds='https://bsky.social',foreign_host_excludes_actor=True,body_sha256=hashlib.sha256(raw).hexdigest()))
assert foreign[0]==foreign[1]
with urllib.request.urlopen(urllib.request.Request('https://typeahead.waow.tech/health/freshness',headers={'User-Agent':'typeahead-install-release/1'}),timeout=15) as r:health=json.load(r)
assert health['ok'] and not health['stale'] and health['probe_ok'] and health['build_id']==plan['candidate']['build_id']
result=dict(passed=True,at=time.time(),expected_machine=expected,aliases_byte_identical=True,samples=rows,freshness=health)
(root/'public-after-promotion.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(dict(passed=True,machine_id=expected,freshness=health)))
