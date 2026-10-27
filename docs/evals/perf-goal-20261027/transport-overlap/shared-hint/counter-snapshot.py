import json,os,sys,time,urllib.request,datetime
from pathlib import Path
out=Path(sys.argv[1]); first=int(sys.argv[2]);last=first+24
if out.exists():raise RuntimeError("preserve counter evidence")
queries={"metrics":"SELECT SUM(searches) AS searches,SUM(total_ms) AS total_ms,SUM(cache_hits) AS cache_hits,SUM(cache_ms) AS cache_ms FROM metrics WHERE hour >= ? AND hour < ?", "request_status":"SELECT status,SUM(hits) AS hits FROM request_buckets WHERE bucket >= ? AND bucket < ? AND endpoint='search' GROUP BY status ORDER BY status"}
requests=[{"type":"execute","stmt":{"sql":sql,"args":[{"type":"integer","value":str(first)},{"type":"integer","value":str(last)}]}} for sql in queries.values()]+[{"type":"close"}]
u=os.environ["TURSO_URL"].replace("libsql://","https://").rstrip("/")+"/v2/pipeline"
r=urllib.request.Request(u,data=json.dumps({"requests":requests}).encode(),headers={"Authorization":"Bearer "+os.environ["TURSO_AUTH_TOKEN"],"Content-Type":"application/json"})
with urllib.request.urlopen(r,timeout=20) as f:data=json.load(f)
result={"read_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"first_bucket":first,"last_bucket_exclusive":last,"limitations":["legacy miss latency excludes backend-unreachable empty answers","asynchronous writes and two sequential statements can skew count reconciliation","cumulative counters; compare snapshots with identical bounds"]}
for name,row in zip(queries,data["results"]):
 s=row["response"]["result"];result[name]=[{k["name"]:(int(v["value"]) if v["type"]=="integer" else v.get("value")) for k,v in zip(s["cols"],r)} for r in s["rows"]]
out.write_text(json.dumps(result,indent=2)+"\n");print(json.dumps({"read_at":result["read_at"],"written":str(out)}))
