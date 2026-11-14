import hashlib,json,re,statistics,time,urllib.parse,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parent
plan=json.loads((root/'plan.json').read_bytes());build=plan['candidate']['build_id'];binary=plan['candidate']['binary_sha256']
origins={'baseline':'https://typeahead-tantivy-a.fly.dev','candidate':'https://typeahead-tantivy-b.fly.dev'}
queries=[dict(q=q,limit=10) for q in ['zzstoatzz.io','jola.dev','pfrazee.com','dholms.xyz','alice','nate','john','rebecca','東京','atproto','jay','dan']]+[dict(q='zzstoatzz',limit=10,pds='https://pds.zzstoatzz.io')]
def request(name,query):
 start=time.monotonic()
 with urllib.request.urlopen(urllib.request.Request(origins[name]+'/search?'+urllib.parse.urlencode(query),headers={'User-Agent':'typeahead-install-release/1'}),timeout=10) as r:
  raw=r.read(1048577);assert len(raw)<=1048576
  assert r.headers['X-Typeahead-Binary-Sha256']==binary
  assert r.headers['X-Typeahead-Build']==build
  assert r.headers['X-Typeahead-Retrieval-Strategy']=='global-merge'
  if name=='candidate':assert r.headers['X-Typeahead-Machine-Id']==plan['machine_id']
  if 'pds' in query:assert r.headers['X-Typeahead-Pds']==urllib.parse.urlsplit(query['pds']).hostname
  return dict(target=name,query=query,engine_ms=float(re.search(r'search;dur=([0-9.]+)',r.headers['Server-Timing'])[1]),elapsed_ms=(time.monotonic()-start)*1000,body_sha256=hashlib.sha256(raw).hexdigest(),actor_count=len(json.loads(raw)['actors']))
samples=[];pairs=[]
for repeat in range(3):
 for q in queries:
  pair=[request(n,q) for n in origins]
  for r in pair:r['warmup']=repeat==0;samples.append(r)
  pairs.append(dict(query=q,warmup=repeat==0,identical=pair[0]['body_sha256']==pair[1]['body_sha256']))
  time.sleep(.5)
def metrics(name):
 t=sorted(r['engine_ms'] for r in samples if r['target']==name and not r['warmup'])
 return dict(samples=len(t),median_ms=statistics.median(t),sample_p95_ms=t[int((len(t)-1)*.95)],max_ms=max(t))
a,b=metrics('baseline'),metrics('candidate')
passed=all(p['identical'] for p in pairs) and b['median_ms']<=a['median_ms']+2 and b['sample_p95_ms']<=a['sample_p95_ms']+5 and b['sample_p95_ms']<=100
result=dict(passed=passed,test_only=False,build_id=build,machine_id=plan['machine_id'],baseline=a,candidate=b,pairs=pairs,samples=samples,acceptance='All bodies identical; candidate median <= baseline +2ms, sample p95 <= baseline +5ms and <=100ms.',limitations='Small paced warm-engine regression; not a production percentile or load test.')
(root/'matched-performance.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k] for k in ['passed','baseline','candidate']}))
raise SystemExit(0 if passed else 1)
