import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

program = '''import pathlib,os,time,json
now=time.time(); uptime=float(pathlib.Path("/proc/uptime").read_text().split()[0]); hz=os.sysconf("SC_CLK_TCK"); rows=[]
for p in pathlib.Path("/proc").iterdir():
 if not p.name.isdigit(): continue
 try:
  a=(p/"cmdline").read_bytes().split(bytes([0])); files=[]
  if len(a)<2 or a[1] not in [b"/app/input/sync.py",b"/app/tantivy-slot.py"]: continue
  for fd in (p/"fd").iterdir():
   try:
    name=os.readlink(fd)
    if name.startswith("/data/") and pathlib.Path(name).is_file():
     info=dict(x.split(":",1) for x in (p/"fdinfo"/fd.name).read_text().splitlines() if ":" in x)
     files.append({"name":name,"fd":fd.name,"position":int(info["pos"].strip())})
   except (OSError,ValueError): pass
  stat=(p/"stat").read_text().split()
  rows.append({"pid":p.name,"source":a[1].decode(),"started_at":now-uptime+int(stat[21])/hz,"files":files})
 except (OSError,ValueError): pass
result={"at":now,"processes":rows}
try:
 ledger=json.loads(pathlib.Path("/data/tantivy/source-budget.json").read_bytes())
 result["budget"]={k:ledger.get(k) for k in ["window_started_at","window_ends_at","source_bytes","poll_bytes","sweep_bytes","pending_reservation"]}
except (OSError,ValueError): pass
print(json.dumps(result))
'''
root = Path(__file__).resolve().parent
machine = sys.argv[1] if len(sys.argv)>1 else "812056a97999d8"
app = sys.argv[2] if len(sys.argv)>2 else "typeahead-tantivy-b"
duration = float(sys.argv[3]) if len(sys.argv)>3 else 6000
name = sys.argv[4] if len(sys.argv)>4 else "candidate-file-reads.jsonl"
until = time.monotonic() + duration
with (root / name).open("a", buffering=1) as log:
    while time.monotonic() < until:
        started = time.monotonic()
        try:
            result = subprocess.run(["fly", "machine", "exec", machine, "-a",
                app, "python3 -c " + shlex.quote(program)],
                capture_output=True, text=True, timeout=8)
            if result.returncode == 0:
                row = json.loads(result.stdout)
                log.write(json.dumps(row, separators=(",", ":")) + "\n")
            else:
                log.write(json.dumps({"at":time.time(),"error":"exec-failed"}) + "\n")
        except (subprocess.TimeoutExpired, ValueError):
            log.write(json.dumps({"at":time.time(),"error":"exec-or-decode-timeout"}) + "\n")
        time.sleep(max(0, min(10 - (time.monotonic() - started), until - time.monotonic())))
