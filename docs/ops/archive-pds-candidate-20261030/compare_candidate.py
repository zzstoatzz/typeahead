import json,re,statistics,time,urllib.parse,urllib.request
from pathlib import Path
queries=['zzstoatzz.io','zzst','a','john','alice','Justin Fa','東京','alex']
origins={'production':'https://typeahead-tantivy-a.fly.dev','candidate':'https://typeahead-tantivy-b.fly.dev'}
rows=[]
for round in range(4):
    for q in queries:
        for label in (['production','candidate'] if round%2==0 else ['candidate','production']):
            req=urllib.request.Request(origins[label]+'/search?'+urllib.parse.urlencode({'q':q,'limit':10}),headers={'X-Client':'smoke-testing','User-Agent':'typeahead-candidate-check/1'})
            start=time.monotonic()
            with urllib.request.urlopen(req,timeout=10) as response:
                body=json.load(response);timing=response.headers['Server-Timing'];build=response.headers['X-Typeahead-Build']
            elapsed=(time.monotonic()-start)*1000
            match=re.fullmatch(r'search;dur=([0-9.]+)',timing);assert match
            rows.append({'round':round,'query':q,'target':label,'server_ms':float(match[1]),'elapsed_ms':elapsed,'count':len(body['actors']),'build_id':build})
            time.sleep(.1)
def summary(label):
    values=sorted(r['server_ms'] for r in rows if r['target']==label and r['round']>0)
    return {'requests':len(values),'p50_ms':statistics.median(values),'p95_ms':values[int(.95*(len(values)-1))],'max_ms':max(values)}
result={'observed_at':int(time.time()),'warmup_rounds':1,'measurement_rounds':3,'queries':queries,'production':summary('production'),'candidate':summary('candidate'),'total_requests':len(rows),'failures':0,'rows':rows,'scope':'small interleaved warm-query regression check; not a load test'}
Path('/tmp/typeahead-pds-join-full/matched-query-check.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='rows'}))
