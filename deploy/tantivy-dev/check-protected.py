import concurrent.futures,datetime,json,subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/'docs/evals/tantivy-dev-20261028'
before=json.loads((EVIDENCE/'inventory-before.json').read_text())
protected={row['app']:row for row in before if row['machines']}

def check(item):
 app,old=item
 machines=json.loads(subprocess.check_output(['fly','machine','list','-a',app,'--json'],text=True,timeout=30))
 def select(machine,original=False):
  config=machine if original else machine['config']
  return {'id':machine['id'],'instance_id':machine.get('instance_id'),'state':machine['state'],'image':config.get('image'),'guest':config.get('guest'),'mounts':sorted((v['volume'],v['path']) for v in (config.get('mounts') or [])),'build_id':(config.get('metadata') or {}).get('build_id')}
 expected=sorted((select(m,True) for m in old['machines']),key=lambda m:m['id'])
 actual=sorted((select(m) for m in machines),key=lambda m:m['id'])
 return {'app':app,'unchanged':actual==expected,'before':expected,'current':actual}

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:checks=list(pool.map(check,protected.items()))
code='fetch("https://typeahead-search-transport.n8-3e9.workers.dev/route").then(async r=>{if(!r.ok)throw Error("route HTTP "+r.status);console.log(JSON.stringify(await r.json()))})'
route=json.loads(subprocess.check_output(['node','-e',code],text=True,timeout=20))
expected=json.loads((EVIDENCE/'route-before.json').read_text())
result={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'machines':checks,'route':route,'route_unchanged':route==expected}
(EVIDENCE/'protected-latest.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'machines_unchanged':all(c['unchanged'] for c in checks),'route_unchanged':result['route_unchanged']}))
if not all(c['unchanged'] for c in checks) or not result['route_unchanged']:raise RuntimeError('protected state changed; inspect evidence before continuing')
