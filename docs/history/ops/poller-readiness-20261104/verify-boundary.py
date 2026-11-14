import datetime as dt
import hashlib
import json
import time
from pathlib import Path

root = Path(__file__).resolve().parent
def read(name):
    return json.loads((root / name).read_bytes())

soak = read("soak.json")
assert time.time() >= soak["finish_after"], "Former hourly deadline has not passed"
begin, end = read("candidate-soak-start.json"), read("candidate-soak-end.json")
p0, p1 = begin["processes"][0], end["processes"][0]
assert p0["pid"] == p1["pid"]
assert abs(p0["started_at"]-p1["started_at"]) < 1
assert p0["arguments"] == p1["arguments"]
assert p1["arguments"][p1["arguments"].index("--max-run-seconds")+1] == "0"
budget0, budget1 = begin["source-budget.json"], end["source-budget.json"]
assert budget1["window_started_at"] >= budget0["window_ends_at"]
assert budget1["window_ends_at"] == budget1["window_started_at"] + 3600
assert budget1["max_source_bytes"] == budget0["max_source_bytes"] == 536870912
assert 0 <= budget1["source_bytes"] <= budget1["max_source_bytes"]

rows = [json.loads(line) for line in (root / "observations.jsonl").read_text().splitlines()]
b = [r for r in rows if r["target"] == "b" and r["at"] >= soak["started_at"]]
assert len(b) >= 240 and b[0]["at"]-soak["started_at"] <= 20
assert b[-1]["at"] >= soak["finish_after"]
assert b[-1]["at"]-b[0]["at"] >= 3660
assert max(y["at"]-x["at"] for x,y in zip(b,b[1:])) <= 30
assert all(r.get("status") == 200 and r.get("data",{}).get("ready") is True for r in b)
assert all(0 <= r["data"]["sync_lag_s"] < 120 for r in b)
machines, versions = set(), set()
for row in b:
    headers = {k.lower():v for k,v in row["headers"].items()}
    machines.add(headers["x-typeahead-machine-id"])
    versions.add(headers["x-typeahead-machine-version"])
assert machines == {"812056a97999d8"} and len(versions) == 1

cutover = dt.datetime(2026,11,5,4,26,8,tzinfo=dt.UTC).timestamp()
public = [r for r in rows if r["target"] == "public" and r["at"] >= cutover]
assert public and all(r.get("status") == 200 and r.get("data",{}).get("ok") is True
                      and r["data"]["stale"] is False and r["data"]["probe_ok"] is True for r in public)
status = read("candidate-status-end.json")
assert status["ready"] and status["full_source"] and status["import_complete"]
assert status["query_config"]["max_sync_lag_s"] == 120
assert status["query_config"]["allow_stale"] is False
assert status["machine_id"] == "812056a97999d8"
assert status["binary_sha256"] == "ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d"
assert read("public-final.json")["passed"]
assert read("registration.json")["release_sha256"] == "2cb383df761467c5d7afcb374e9baa01e040faf35170d78b518ac655d3e6b4ba"
assert len(read("routing-after.json")["pending_slots_clear"]) == 2

old = [r for r in rows if r["target"] == "a" and r["at"] >= dt.datetime(2026,11,5,4,0,tzinfo=dt.UTC).timestamp()]
result = dict(schema_version=1, passed=True, verified_at=time.time(),
    binding=read("release-proof.json")["binding"],
    release_sha256="2cb383df761467c5d7afcb374e9baa01e040faf35170d78b518ac655d3e6b4ba",
    sampled_seconds=b[-1]["at"]-b[0]["at"], samples=len(b),
    readiness_failures=0, unknown_samples=0,
    max_source_lag_s=max(r["data"]["sync_lag_s"] for r in b),
    public_freshness_samples=len(public), public_freshness_failures=0,
    poller_pid=p1["pid"], poller_started_at=p1["started_at"],
    process_age_s=end["at"]-p1["started_at"], process_unchanged=True,
    byte_budget_renewed_in_process=True, budget_before=budget0, budget_after=budget1,
    old_control_unready_samples=sum(r.get("data",{}).get("ready") is False for r in old),
    observations_sha256=hashlib.sha256((root/"observations.jsonl").read_bytes()).hexdigest(),
    scope="15-second readiness sampling across the former hourly process deadline and a real hourly quota renewal. This is not a universal availability or request-latency SLO.")
(root / "verification.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps({k:result[k] for k in ["passed","sampled_seconds","samples","readiness_failures","unknown_samples","max_source_lag_s","process_unchanged","byte_budget_renewed_in_process"]}))
