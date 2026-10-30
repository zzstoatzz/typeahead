import json,sys,urllib.request,urllib.parse,time,os
from pds_repair_ledger import Ledger
creds=json.load(sys.stdin);base=creds['TURSO_URL'].replace('libsql://','https://').rstrip('/')+'/v2/pipeline'
def sql(query,params=None):
 args=[{'name':k,'value':{'type':'integer' if isinstance(v,int) else 'text','value':str(v)}} for k,v in (params or {}).items()]
 req=urllib.request.Request(base,data=json.dumps({'requests':[{'type':'execute','stmt':{'sql':query,'named_args':args}},{'type':'close'}]}).encode(),headers={'Authorization':'Bearer '+creds['TURSO_AUTH_TOKEN'],'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=15) as r:d=json.load(r)
 v=d['results'][0]
 if v['type']!='ok':raise RuntimeError(v.get('error',{}).get('code','query failed'))
 return [[int(c['value']) if c['type']=='integer' else c.get('value') for c in row] for row in v['response']['result']['rows']]
def fetch(url):
 with urllib.request.urlopen(urllib.request.Request(url,headers={'X-Client':'smoke-testing','User-Agent':'typeahead-metadata-pilot/1.0 (@zzstoatzz.io)'}),timeout=10) as r:return json.load(r)
def health():
 s=fetch('https://typeahead-tantivy-a.fly.dev/health');i=fetch('https://typeahead-ingester.fly.dev/health')
 assert s['ready'] and s['sync_lag_s']<120 and i['status']=='ok' and not i['ingest']['backpressure_active'] and i['ingest']['queued']<100
 return {'search':s,'ingest':i,'at':time.time()}
def search(row,pds):
 u='https://typeahead-tantivy-a.fly.dev/search?'+urllib.parse.urlencode({'q':row['handle'],'pds':pds,'limit':10})
 return any(a['did']==row['did'] for a in fetch(u)['actors'])
import random,sqlite3,concurrent.futures
with sqlite3.connect('/tmp/typeahead-archive-backfill/ledger.db') as local:
 rows=local.execute("SELECT did,pds FROM attempts WHERE state='confirmed' AND prepared_at>? AND prepared_at<?",(1793392733,time.time()-120)).fetchall()
assert len(rows)>=30, 'Not enough settled new repairs yet'
chosen=random.Random(20261031).sample(rows,30)
actual=sql('SELECT did,pds,handle FROM actors WHERE did IN (SELECT value FROM json_each(:dids))',{'dids':json.dumps([r[0] for r in chosen])})
expected=dict(chosen)
def verify(row):
 did,pds,handle=row
 if did.startswith('did:plc:'):url='https://plc.directory/'+did
 else:
  parts=did.removeprefix('did:web:').split(':');host=urllib.parse.unquote(parts[0]);path='/'.join(parts[1:]);url='https://'+host+('/'+path if path else '/.well-known')+'/did.json'
 doc=fetch(url);assert doc['id']==did
 authority=next((x['serviceEndpoint'] for x in doc.get('service',[]) if x.get('type')=='AtprotoPersonalDataServer' and x.get('id') in ('#atproto_pds',did+'#atproto_pds')),None)
 r={'did':did,'handle':handle}
 return {'did':did,'pds':pds,'source_matches':pds==expected[did],'authority_matches':authority is not None and authority.rstrip('/')==pds.rstrip('/'),'right_scope':search(r,pds),'wrong_scope_absent':not search(r,'pds-archive-pilot.invalid')}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:checks=list(pool.map(verify,actual))
result={'at':time.time(),'eligible_cohort':len(rows),'checks':checks}
open('/tmp/typeahead-archive-backfill/run-2-checks.json','w').write(json.dumps(result))
print(json.dumps({'eligible_cohort':len(rows),'sample':len(checks),'all_pass':len(checks)==30 and all(all(r[k] for k in ('source_matches','authority_matches','right_scope','wrong_scope_absent')) for r in checks),'failures':[r for r in checks if not all(r[k] for k in ('source_matches','authority_matches','right_scope','wrong_scope_absent'))]}))
