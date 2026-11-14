import json,subprocess,shlex
from pathlib import Path
root=Path(__file__).resolve().parent
code="""import json,os,sys,time,urllib.request
sys.path.insert(0,'/app/input');import sync
with urllib.request.urlopen('http://127.0.0.1:8080/index/status',timeout=5) as r:state=json.load(r)
c=state['source_cursor'];a=c['actors'];upper=(c.get('cycle') or {}).get('upper_bound',int(time.time()))
sql='SELECT COUNT(*) AS pending FROM (SELECT 1 FROM actors WHERE updated_at >= ?1 AND (updated_at > ?1 OR did > ?2) AND updated_at <= ?3 LIMIT 250001)'
args=[{'type':'integer','value':str(a['updated_at'])},{'type':'text','value':a['did']},{'type':'integer','value':str(upper)}]
h=sync.Http(timeout=10)
def query(sql):
 body={'requests':[{'type':'execute','stmt':{'sql':sql,'args':args,'want_rows':True}},{'type':'close'}]}
 result,size=h.request(sync.source_url(os.environ['TURSO_URL'],False),os.environ['TURSO_AUTH_TOKEN'],body,max_bytes=65536)
 r=result['results'][0];assert r['type']=='ok', 'query rejected';return r['response']['result']
p=query('EXPLAIN QUERY PLAN '+sql);details=[row[-1]['value'] for row in p['rows']]
assert any('SEARCH' in x and 'COVERING INDEX' in x for x in details),details
r=query(sql);pending=int(r['rows'][0][0]['value']);print(json.dumps(dict(at=time.time(),cursor=a,upper_bound=upper,pending_actors_capped=pending,cap=250001,query_plan=details,response_bytes=h.received_bytes)))
"""
r=json.loads(subprocess.check_output(['fly','machine','exec','840e95a2324068','python3 -c '+shlex.quote(code),'-a','typeahead-tantivy-b','--json','--timeout','30']))
assert r.get('exit_code',0)==0,r.get('stderr','')
v=json.loads(r['stdout']);(root/'backlog.json').write_text(json.dumps(v,indent=2)+'\n')
with (root/'backlog-progress.jsonl').open('a') as f:f.write(json.dumps(v)+'\n')
print(json.dumps(v))
