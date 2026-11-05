import hashlib,json,os,signal,subprocess,sys,tempfile,time
from pathlib import Path

started=time.monotonic()
def expired(*_):raise TimeoutError('Ranking execution budget expired')
signal.signal(signal.SIGALRM,expired);signal.alarm(180)
with tempfile.TemporaryDirectory(prefix='typeahead-ranking-') as scratch:
 subprocess.run([sys.executable,'-m','pip','install','--no-input','--disable-pip-version-check','--target',scratch,'httpx==0.28.1'],check=True,timeout=60,stdout=subprocess.DEVNULL)
 sys.path.insert(0,scratch);sys.path.insert(0,'/qualification')
 import asyncio,bench,httpx
 async def run():
  origin='https://typeahead-tantivy-b.fly.dev'
  async with httpx.AsyncClient(headers={'User-Agent':'typeahead-release-ranking/1.0','X-Client':'bench-ranking'},follow_redirects=False) as client:
   status=(await client.get(origin+'/index/status',timeout=10)).json()
   assert status['ready'] and status['machine_id']=='7841e35f99d338'
   results=await bench.run_ranking(client,origin,bench.RANKING_CORPUS,True)
   failures=bench.acceptance_checks(results)
   report=dict(event='canonical_ranking',at=time.time(),target=origin,build_id=status['build_id'],machine_id=status['machine_id'],source_sha256=hashlib.sha256(Path('/qualification/bench.py').read_bytes()).hexdigest(),structural_passed=not any(x.startswith('[structural]') for x in failures),failures=failures,queries=[dict(query=r.query,ours_status=r.ours_status,bsky_status=r.bsky_status,ours_ms=r.ours_ms,ours_handles=sorted(r.ours_handles),bsky_top=r.bsky_top_handle) for r in results],elapsed_s=time.monotonic()-started)
   print('\nTA_RANKING_RESULT '+json.dumps(report,separators=(',',':')),flush=True)
   return 0 if report['structural_passed'] else 1
 code=asyncio.run(run())
raise SystemExit(code)
