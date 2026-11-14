import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from prefect.blocks.system import Secret

root = Path(__file__).resolve().parent
image = "registry.fly.io/typeahead-tantivy-b@sha256:bc66ce9eec3aca5d568135e6d0637f2e2be6e0c305095fc29e4fd50c15d0d34d"
binary = "ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d"
plan_sha = "6ae0d7622dd7dc1a2973883698d2cb7ee2e9ec2d137cf755e14c2542c1ea5ac1"

def read(name):
    return json.loads((root / name).read_bytes())

def digest(name):
    return hashlib.sha256((root / name).read_bytes()).hexdigest()

plan, preparation = read("plan.json"), read("preparation.json")
assert digest("plan.json") == plan_sha
assert preparation["phase"] == "prepared" and preparation["plan_sha256"] == plan_sha
assert plan["image_digest"] == image and plan["test_only"] is False
assert plan["machine_id"] == "812056a97999d8"
assert read("functional.json")["passed"] and read("source-receipt.json")["passed"]
assert read("source-receipt.json")["backend_binary_sha256"] == binary
assert read("source-receipt.json")["sync_source_sha256"] == read("image-files.json")["/app/input/sync.py"]
assert "OK (skipped=1)" in (root / "slot-integration.log").read_text()
assert read("matched-performance.json")["passed"] and read("natural-recovery.json")["passed"]

machines = json.loads(subprocess.check_output(["fly", "machine", "list", "-a", "typeahead-tantivy-b", "--json"]))
machine = next(m for m in machines if m["id"] == plan["machine_id"])
config = machine["config"]
assert machine["state"] == "started" and config["image"] == image
guest = {k:config["guest"][k] for k in ["cpu_kind", "cpus", "memory_mb"]}
assert guest == dict(cpu_kind="shared", cpus=2, memory_mb=1024)
assert len(config["mounts"]) == 1
mount = config["mounts"][0]
assert mount["volume"] == plan["volume_id"] and mount["path"] == "/data"
assert mount["encrypted"] is True and mount["size_gb"] == 20
assert config["env"]["EXPECTED_DEPLOYMENT_SHA256"] == plan["deployment_sha256"]
assert config["metadata"]["request_id"] == "ab34d45098d34c8493a70cb11ff82051"

with urllib.request.urlopen(urllib.request.Request("https://typeahead-tantivy-b.fly.dev/index/status",
        headers={"User-Agent":"typeahead-readiness-check/1.0"}), timeout=10) as response:
    status = json.load(response)
assert status["ready"] and status["full_source"] and status["import_complete"]
assert status["machine_id"] == plan["machine_id"] and status["binary_sha256"] == binary
assert status["build_id"] == plan["candidate"]["build_id"]
assert status["expiry_coverage_ready"] and status["moderation"]["fresh"]
assert status["pds_scope_version"] == 1 and status["actor_count"] > 13_000_000
assert status["query_config"]["max_sync_lag_s"] == 120 and status["sync_lag_s"] < 120
assert status["query_config"]["allow_stale"] is False
assert time.time() < 1793901807 - 6 * 3600
(root / "candidate-status-release.json").write_text(json.dumps(status, indent=2)+"\n")

samples = [json.loads(line) for line in (root / "candidate-file-reads.jsonl").read_text().splitlines()]
before = [s for s in samples if s.get("budget",{}).get("pending_reservation") is False and
          any(p["pid"] == "1110" for p in s.get("processes",[]))][-1]
after = [s for s in samples if s.get("budget",{}).get("pending_reservation") is False and
         any(p["source"].endswith("/tantivy-slot.py") and p["started_at"] > 1793851890
             for p in s.get("processes",[]))][0]
assert before["budget"]["window_started_at"] == after["budget"]["window_started_at"]
assert after["budget"]["source_bytes"] >= before["budget"]["source_bytes"]
recovery = dict(passed=True, before_restart=before, after_restart=after,
                local_slot_test_sha256=digest("slot-integration.log"),
                source_crash_test_sha256=digest("source-receipt.json"))

def secret(name):
    return Secret.load(name).get()

client = boto3.client("s3", endpoint_url=secret("index-r2-endpoint"), region_name="auto",
                      aws_access_key_id=secret("index-r2-access-key-id"),
                      aws_secret_access_key=secret("index-r2-secret-access-key"))
bucket = secret("index-r2-bucket")
route_response = client.get_object(Bucket=bucket, Key="control/search-routing.json")
with route_response["Body"] as stream:
    route = json.loads(stream.read(65536))
assert route == plan["expected_route"] and route_response["ETag"] == plan["expected_current_etag"]

binding = dict(binary_sha256=binary, retrieval_strategy="global-merge",
               canonical_rules_source_sha256=status["expiry_coverage"]["canonical_rules_source_sha256"],
               image_digest=image, guest=guest, volume_size_gib=20)
gates = {
    "matched_performance": dict(passed=True, result=read("matched-performance.json")),
    "full_functional": dict(passed=True, functional=read("functional.json"), source=read("source-receipt.json"),
                            image_files=read("image-files.json")),
    "live_source_coverage": dict(passed=True, at=time.time(), status=status,
        observed_coverage_deadline=1793901807, process=read("candidate-soak-start.json"),
        scope="Current full checkpoint coverage, fresh live source, moderation and strict 120s readiness. First post-change hourly boundary is still under observation."),
    "worker_http_smoke": dict(passed=True, matched_candidate_backend=read("matched-performance.json")["passed"],
        public_recovery=read("natural-recovery.json"),
        scope="Candidate backend headers, results and scope match the unchanged production binary. Existing public Worker fallback/recovery verified during natural old-backend failure. Actual public candidate machine is verified after guarded promotion."),
    "recovery": recovery,
    "refresh_lifecycle": dict(passed=True, plan=plan, preparation=preparation,
        scope="Existing cloud deployment prepared the new image on independent inactive storage and qualified cached restart. Reader, installer, artifact format and publisher are unchanged. This is not a new full-snapshot build or a sustained refresh-time guarantee."),
}

def publish(prefix, value):
    data = json.dumps(value, sort_keys=True, separators=(",",":")).encode()+b"\n"
    sha = hashlib.sha256(data).hexdigest()
    key = prefix + sha + ".json"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/json", IfNoneMatch="*")
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
            raise
    response = client.get_object(Bucket=bucket, Key=key)
    with response["Body"] as stream:
        assert stream.read(len(data)+1) == data
    (root / (sha+".json")).write_bytes(data)
    return dict(key=key, sha256=sha, verified=True)

published = {}
for name, evidence in gates.items():
    evidence.update(schema_version=1, binding=binding)
    receipt = publish("control/tantivy-release-evidence/", evidence)
    published[name] = dict(passed=True, evidence_key=receipt["key"], evidence_sha256=receipt["sha256"])
proof = dict(schema_version=1, engine="tantivy", test_only=False, passed=True,
             binding=binding, gates=published, limitations=[
    "First post-change hourly boundary remains under observation; this proof does not claim that observation is complete.",
    "Performance is a small paired warm-engine regression, not a production latency percentile or sustained-load SLO.",
    "This image retains the existing checkpoint-expiry behavior. The 06:00/18:00 UTC refresh cadence remains required.",
])
receipt = publish("control/tantivy-releases/", proof)
(root / "release-publication.json").write_text(json.dumps(receipt, indent=2)+"\n")
(root / "release-proof.json").write_bytes((root / (receipt["sha256"]+".json")).read_bytes())
print(json.dumps(receipt))
