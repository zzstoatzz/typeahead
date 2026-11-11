"""Temporary unscheduled builder for the shadow comparison: production's newest search bundle as packed views.

Reads one published bundle from R2, sorts its actors into comparable views, and writes the gzipped views, their sidecar and a
manifest under a scratch prefix of the same bucket. The walk itself runs on the shadow machine, which fetches them.
"""

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
TOOLING = HERE.parents[1] / "scripts" / "shadow_compare"
sys.path[:0] = [str(HERE), str(TOOLING)]

import bundle
import cloud

OWNER = "the shadow comparison, issue 43"
GIB = 1024**3
DEADLINE_SECONDS = 2 * 3600
CLEANUP_RESERVE_SECONDS = 300
OBJECTS = (bundle.VIEWS_NAME, bundle.SIDECAR_NAME, bundle.MANIFEST_NAME)


@dataclass(frozen=True)
class Plan:
    build_id: str | None = None
    input_jsonl: str | None = None
    watermark: int | None = None
    output_dir: str | None = None
    scratch_parent: str = "/var/tmp"
    required_free_gib: float = 80
    budget_gib: float = 60
    run_records: int = 250_000

    def __post_init__(self):
        if self.build_id is not None and not cloud.BUILD.fullmatch(self.build_id):
            raise ValueError("Invalid build id")
        if self.input_jsonl and self.watermark is None:
            raise ValueError("An actor file needs its watermark")


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


def pack_views(supervisor, scratch, views, run_id, build_id, watermark):
    packed = scratch / "packed"
    command = [
        "uv", "run", "--script", TOOLING / "bundle.py", "pack", "--views", views, "--out-dir", packed,
        "--run-id", run_id, "--build-id", build_id, "--watermark", watermark,
    ]
    record = cloud.last(supervisor.run("pack", command), "pack")
    return packed, {key: value for key, value in record.items() if key != "event"}


def publish(packed, run_id, plan, storage=None):
    """Put the three objects where the shadow stage fetches them, the manifest last so its presence means the run is whole."""
    if plan.output_dir:
        target = Path(plan.output_dir) / run_id
        target.mkdir(parents=True)
        for name in OBJECTS:
            shutil.copyfile(packed / name, target / name)
        return {"kind": "directory", "path": str(target), "objects": list(OBJECTS)}
    client, bucket = storage or cloud.r2_client()
    prefix = f"{bundle.SCRATCH_PREFIX}/{run_id}"
    try:
        for name in OBJECTS:
            client.upload_file(str(packed / name), bucket, f"{prefix}/{name}")
            stored = client.head_object(Bucket=bucket, Key=f"{prefix}/{name}")["ContentLength"]
            if stored != (packed / name).stat().st_size:
                raise RuntimeError(f"{name} is {stored} bytes in the bucket")
    except Exception as error:
        raise RuntimeError(cloud.redact(f"Publishing failed: {type(error).__name__}: {error}")) from None
    finally:
        if storage is None:
            client.close()
    return {"kind": "r2", "prefix": prefix, "objects": list(OBJECTS)}


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

    packed, summary["pack"] = pack_views(supervisor, scratch, views, summary["run_id"], build_id, watermark)
    supervisor.check()
    summary["published"] = publish(packed, summary["run_id"], plan)
    cloud.emit({"phase": "publish", "event": "published", "run_id": summary["run_id"], "manifest_sha256": summary["pack"]["manifest_sha256"], **summary["published"]})


def build(run_id, root, plan):
    run_id = str(run_id)
    if not bundle.RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid run id")
    parent = Path(plan.scratch_parent)
    free_before = shutil.disk_usage(parent).free
    if free_before < plan.required_free_gib * GIB:
        raise RuntimeError("Insufficient free disk for the shadow comparison builder")
    scratch = parent / f"typeahead-shadow-compare-{run_id}"
    scratch.mkdir()
    started = time.monotonic()
    deadline = started + DEADLINE_SECONDS - CLEANUP_RESERVE_SECONDS
    summary = {
        "event": "shadow_compare_summary",
        "owner": OWNER,
        "run_id": run_id,
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
        summary["error"] = cloud.redact(f"{type(error).__name__}: {error}")[:500]
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
        cloud.emit(summary)
    return summary


@flow(
    name="typeahead-shadow-compare",
    retries=0,
    timeout_seconds=DEADLINE_SECONDS,
    log_prints=True,
)
def shadow_compare(
    build_id: str | None = None,
    input_jsonl: str | None = None,
    watermark: int | None = None,
    output_dir: str | None = None,
    scratch_parent: str = "/var/tmp",
    required_free_gib: float = 80,
    budget_gib: float = 60,
    run_records: int = 250_000,
) -> dict:
    from prefect.runtime import flow_run

    plan = Plan(
        build_id=build_id,
        input_jsonl=input_jsonl,
        watermark=watermark,
        output_dir=output_dir,
        scratch_parent=scratch_parent,
        required_free_gib=required_free_gib,
        budget_gib=budget_gib,
        run_records=run_records,
    )
    return build(flow_run.id or uuid.uuid4().hex, HERE, plan)
