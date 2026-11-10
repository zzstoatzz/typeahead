"""Temporary unscheduled comparison of the shadow's store with production's newest search bundle.

Reads one published bundle from R2 and the shadow's store over HTTP; writes nothing outside its scratch root.
"""

import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from prefect import flow

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cloud

OWNER = "the shadow comparison, issue 43"
GIB = 1024**3
DEADLINE_SECONDS = 4 * 3600
CLEANUP_RESERVE_SECONDS = 300
TOOLING = HERE.parents[1] / "scripts" / "shadow_compare"
REPORTED_DIDS = 2000


@dataclass(frozen=True)
class Plan:
    shadow_url: str
    shadow_secret_env: str = "SHADOW_STORE_SECRET"
    build_id: str | None = None
    input_jsonl: str | None = None
    watermark: int | None = None
    scratch_parent: str = "/var/tmp"
    required_free_gib: float = 80
    budget_gib: float = 60
    run_records: int = 250_000
    page_size: int = 500
    request_delay: float = 0.05
    max_requests: int = 80_000
    max_mib: int = 24_000
    settle_delay: float = 120
    arbiter: bool = True
    arbiter_delay: float = 1.0

    def __post_init__(self):
        if self.build_id is not None and not cloud.BUILD.fullmatch(self.build_id):
            raise ValueError("Invalid build id")
        if not self.shadow_secret_env.isidentifier():
            raise ValueError("The secret is named by an environment variable")
        if self.input_jsonl and self.watermark is None:
            raise ValueError("An actor file needs its watermark")


def redact(text, plan):
    secret = os.environ.get(plan.shadow_secret_env)
    text = cloud.redact(text)
    return text.replace(secret, f"[{plan.shadow_secret_env}]") if secret else text


def sort_views(supervisor, plan, scratch, source):
    views = scratch / "views.jsonl"
    command = ["uv", "run", "--script", TOOLING / "bundle.py", "sort", "--out", views, "--tmp", scratch / "sort", "--run-records", plan.run_records]
    if source["kind"] == "jsonl":
        records = supervisor.run("sort", [*command, "--input", source["path"]])
        return views, cloud.last(records, "sort"), None
    count = supervisor.logs / "sort-documents.txt"
    pipeline = 'set -o pipefail; "$1" "$2" 2>"$3" | "${@:4}"'
    try:
        records = supervisor.run("sort", ["bash", "-c", pipeline, "pipeline", source["dump"], source["index"], count, *command, "--input", "-"])
    except RuntimeError:
        if count.exists():
            cloud.emit({"phase": "sort", "event": "dump_stderr", "text": count.read_text()[-1000:]})
        raise
    return views, cloud.last(records, "sort"), int(count.read_text().split()[-1])


def walk(supervisor, plan, scratch, views, watermark, build_id):
    out = scratch / "report"
    command = [
        "uv", "run", "--script", TOOLING / "compare.py", "--phases", "corpus",
        "--production-bundle", views, "--bundle-watermark", watermark, "--bundle-build-id", build_id,
        "--shadow-store", plan.shadow_url, "--shadow-store-secret-env", plan.shadow_secret_env,
        "--page-size", plan.page_size, "--corpus-delay", plan.request_delay,
        "--corpus-max-requests", plan.max_requests, "--corpus-max-mib", plan.max_mib,
        "--settle-delay", plan.settle_delay, "--arbiter-delay", plan.arbiter_delay,
        "--checkpoint", scratch / "walk.json", "--out-dir", out,
    ]
    if plan.arbiter:
        command.append("--arbiter")
    supervisor.run("compare", command, check=False)
    report = out / "report.json"
    if not report.exists():
        raise RuntimeError("The comparison did not finish; its stderr is in the command_failed record")
    return json.loads(report.read_text()), (out / "README.md").read_text()


def trimmed(report):
    """The report without the full list of differing DIDs when that list is long."""
    flagged = report.get("corpus", {}).get("flagged_dids")
    if flagged is None or len(flagged) <= REPORTED_DIDS:
        return report
    digest = hashlib.sha256("\n".join(flagged).encode()).hexdigest()
    corpus = {**report["corpus"], "flagged_dids": flagged[:REPORTED_DIDS], "flagged_dids_total": len(flagged), "flagged_dids_sha256": digest}
    return {**report, "corpus": corpus}


def phases(supervisor, plan, root, scratch, summary):
    if plan.input_jsonl:
        path = Path(plan.input_jsonl).resolve()
        source = {"kind": "jsonl", "path": path}
        watermark, build_id = plan.watermark, plan.build_id or "local-actor-file"
        summary["input"] = {"kind": "jsonl", "jsonl_bytes": path.stat().st_size, "source_watermark": watermark}
    else:
        tools = scratch / "tools"
        tools.mkdir()
        summary["tools"] = {}
        env = cloud.rust_toolchain(supervisor, tools, summary["tools"])
        dump = cloud.build_dump_actors(supervisor, root, tools, env, summary["tools"])
        cloud.emit({"phase": "tools", "event": "tools", **summary["tools"]})
        index, facts = cloud.acquire(supervisor, plan, scratch)
        source = {"kind": "index", "dump": dump, "index": index}
        watermark, build_id = plan.watermark or facts["source_watermark"], facts["build_id"]
        summary["input"] = {"kind": "bundle", **facts}
    if not isinstance(watermark, int):
        raise RuntimeError("The bundle manifest has no source watermark")
    cloud.emit({"phase": "input", "event": "input", **summary["input"]})

    views, summary["sort"], documents = sort_views(supervisor, plan, scratch, source)
    summary["input"]["documents"] = documents if documents is not None else summary["sort"]["lines"]
    if documents is not None and documents != summary["sort"]["lines"]:
        raise RuntimeError("The sorter read a different number of lines than dump-actors printed")
    if source["kind"] == "index":
        shutil.rmtree(scratch / "bundle")
        shutil.rmtree(scratch / "tools")
    summary["scratch_after_sort_bytes"] = supervisor.used()

    report, text = walk(supervisor, plan, scratch, views, watermark, build_id)
    for line in text.splitlines():
        print(line, flush=True)
    summary["report"] = trimmed(report)
    summary["verdict"] = report["verdict"]["gates"]["corpus"]


def compare(run_id, root, plan):
    if not os.environ.get(plan.shadow_secret_env):
        raise RuntimeError(f"Environment variable {plan.shadow_secret_env} is unset or empty")
    parent = Path(plan.scratch_parent)
    free_before = shutil.disk_usage(parent).free
    if free_before < plan.required_free_gib * GIB:
        raise RuntimeError("Insufficient free disk for the shadow comparison")
    scratch = parent / f"typeahead-shadow-compare-{run_id}"
    scratch.mkdir()
    started = time.monotonic()
    deadline = started + DEADLINE_SECONDS - CLEANUP_RESERVE_SECONDS
    summary = {
        "event": "shadow_compare_summary",
        "owner": OWNER,
        "run_id": str(run_id),
        "scratch": str(scratch),
        "status": "failed",
        "plan": asdict(plan),
    }
    supervisor = None
    try:
        supervisor = cloud.Supervisor(scratch, deadline, int(plan.budget_gib * GIB))
        summary["host"] = cloud.host_facts(scratch)
        cloud.emit({"phase": "preflight", "event": "preflight", "scratch": str(scratch), **summary["host"]})
        phases(supervisor, plan, root, scratch, summary)
        summary["status"] = "completed"
    except BaseException as error:
        summary["error"] = redact(f"{type(error).__name__}: {error}", plan)[:500]
        raise
    finally:
        if supervisor is not None:
            supervisor.stop_all()
            summary["scratch_peak_bytes"] = supervisor.peak_used
        shutil.rmtree(scratch, ignore_errors=True)
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary["cleanup"] = {
            "scratch_removed": not scratch.exists(),
            "free_before_bytes": free_before,
            "free_after_bytes": shutil.disk_usage(parent).free,
        }
        print(redact(json.dumps(summary, separators=(",", ":"), ensure_ascii=False), plan), flush=True)
    return summary


@flow(
    name="typeahead-shadow-compare",
    retries=0,
    timeout_seconds=DEADLINE_SECONDS,
    log_prints=True,
)
def shadow_compare(
    shadow_url: str,
    shadow_secret_env: str = "SHADOW_STORE_SECRET",
    build_id: str | None = None,
    input_jsonl: str | None = None,
    watermark: int | None = None,
    scratch_parent: str = "/var/tmp",
    required_free_gib: float = 80,
    budget_gib: float = 60,
    run_records: int = 250_000,
    page_size: int = 500,
    request_delay: float = 0.05,
    max_requests: int = 80_000,
    max_mib: int = 24_000,
    settle_delay: float = 120,
    arbiter: bool = True,
    arbiter_delay: float = 1.0,
) -> dict:
    from prefect.runtime import flow_run

    plan = Plan(
        shadow_url=shadow_url,
        shadow_secret_env=shadow_secret_env,
        build_id=build_id,
        input_jsonl=input_jsonl,
        watermark=watermark,
        scratch_parent=scratch_parent,
        required_free_gib=required_free_gib,
        budget_gib=budget_gib,
        run_records=run_records,
        page_size=page_size,
        request_delay=request_delay,
        max_requests=max_requests,
        max_mib=max_mib,
        settle_delay=settle_delay,
        arbiter=arbiter,
        arbiter_delay=arbiter_delay,
    )
    return compare(flow_run.id or uuid.uuid4().hex, HERE, plan)
