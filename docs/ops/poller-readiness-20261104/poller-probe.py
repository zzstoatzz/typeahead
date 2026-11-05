import pathlib,time,os,json
root=pathlib.Path("/data/tantivy")
now=time.time(); uptime=float(pathlib.Path("/proc/uptime").read_text().split()[0]); hz=os.sysconf("SC_CLK_TCK")
result={"at":now,"processes":[]}
for p in pathlib.Path("/proc").iterdir():
 try:
  a=(p/"cmdline").read_bytes().split(bytes([0])); s=(p/"stat").read_text().split()
  if len(a)>1 and a[1]==b"/app/input/sync.py":
   io=dict(line.split(": ",1) for line in (p/"io").read_text().splitlines())
   result["processes"].append({"pid":p.name,"started_at":now-uptime+int(s[21])/hz,"cpu_s":(int(s[13])+int(s[14]))/hz,"rss_bytes":int(s[23])*os.sysconf("SC_PAGE_SIZE"),"read_bytes":int(io["read_bytes"]),"rchar":int(io["rchar"]),"arguments":[x.decode() for x in a if x]})
 except (OSError,ValueError): pass
for name in ["source-budget.json","source-status.json"]:
 try:
  x=json.loads((root/name).read_bytes())
  result[name]={k:x.get(k) for k in ["phase","caught_up","through","wall_seconds","source_bytes","poll_bytes","sweep_bytes","max_source_bytes","window_started_at","window_ends_at"] if k in x}
 except (OSError,ValueError): pass
print(json.dumps(result))
