import subprocess,selectors,os,time,json,signal,datetime
from pathlib import Path
root=Path("docs/evals/perf-goal-20261027/transport-overlap/shared-hint/observer-new-cli-preflight.json")
s=selectors.DefaultSelector(); children={}; count={}; start=time.monotonic(); result={"started_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"stdin_held_open":True,"production_mutations":0,"wrangler":"4.153.0","limitations":["process liveness does not establish gap-free log delivery"]}
try:
 for name in ["typeahead","typeahead-search-transport"]:
  p=subprocess.Popen(["/tmp/typeahead-observer-wrangler-4.153.0/node_modules/.bin/wrangler","tail",name,"--format","json","--method","GET"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
  children[name]=p;count[name]=0;s.register(p.stdout,selectors.EVENT_READ,name)
 while time.monotonic()-start<720:
  for key,_ in s.select(1):
   b=os.read(key.fileobj.fileno(),65536)
   if not b:raise RuntimeError("observer closed: "+key.data)
   count[key.data]+=len(b)
 result["status"]="passed-twelve-minute-liveness-check"
except BaseException as e:result.update(status="failed",error=str(e))
finally:
 result["elapsed_s"]=round(time.monotonic()-start,1);result["bytes_discarded"]=count;result["exit_before_stop"]={n:p.poll() for n,p in children.items()}
 for p in children.values():
  try:os.killpg(p.pid,signal.SIGTERM)
  except ProcessLookupError:pass
  try:p.wait(timeout=5)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
 result["finished_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat();root.write_text(json.dumps(result,indent=2)+"\n");print(json.dumps(result),flush=True)
