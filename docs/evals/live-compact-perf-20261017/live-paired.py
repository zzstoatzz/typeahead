import urllib.request,urllib.parse,time,json,statistics
from pathlib import Path
out=Path(__file__).with_suffix('.json')
queries=['nate','zzstoatzz','paul','dan','alice','bob','sky','dev','a','zzstoatzz.io']
urls={'legacy':'https://typeahead-search.fly.dev','compact':'https://typeahead-search-compact.fly.dev'}
rows=[]
for round in range(5):
 for i,q in enumerate(queries):
  for name in (['legacy','compact'] if (round+i)%2==0 else ['compact','legacy']):
   url=urls[name]+'/search?'+urllib.parse.urlencode({'q':q,'limit':10})
   t=time.perf_counter()
   try:
    with urllib.request.urlopen(url,timeout=15) as r: body=r.read();status=r.status
    data=json.loads(body);error=None
   except Exception as e: data=None;status=0;error=str(e)
   rows.append(dict(round=round,query=q,backend=name,ms=(time.perf_counter()-t)*1000,status=status,data=data,error=error))
   out.write_text(json.dumps(rows,indent=2))
   time.sleep(.1)
for name in urls:
 a=sorted(r['ms'] for r in rows if r['backend']==name and r['round']>0)
 print(name,'n',len(a),'p50',statistics.median(a),'p95',a[int(.95*(len(a)-1))], 'errors',sum(r['status']!=200 for r in rows if r['backend']==name))
