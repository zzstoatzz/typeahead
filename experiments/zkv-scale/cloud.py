"""Temporary unscheduled cloud measurement of zkv at Typeahead corpus scale.

Reads one published search bundle from R2; writes nothing outside its scratch root.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from prefect import flow

OWNER = "the embedded-store evaluation, issue 32"
GIB = 1024**3
DEADLINE_SECONDS = 3 * 3600
CLEANUP_RESERVE_SECONDS = 300
ZIG_VERSION = "0.17.0"
ZIG_HOME = Path("/root/.local/opt/zig-x86_64-linux-0.17.0")
ZIG_URL = "https://ziglang.org/download/0.17.0/zig-x86_64-linux-0.17.0.tar.xz"
ZIG_SHA256 = "1cbe9df9f27e6b78d14ccbca43b6703a404ef79ef1c463de901d7f088d4e2026"
RUST_TOOLCHAIN = "1.91.1"
RELEASES = "releases/search-tantivy/"
BUILD = re.compile(r"b[0-9]{10}-[A-Za-z0-9_-]+")
INDEX_MEMBER = re.compile(r"index/[A-Za-z0-9_.-]+")
OPTIONAL_PHASES = (
    "count",
    "stat",
    "settle",
    "get",
    "get_stride32",
    "churn",
    "storm",
    "final_count",
    "second_load",
)


@dataclass(frozen=True)
class Plan:
    build_id: str | None = None
    input_jsonl: str | None = None
    skip: tuple[str, ...] = ()
    scratch_parent: str = "/var/tmp"
    required_free_gib: float = 80
    budget_gib: float = 60
    get_reads: int = 20_000
    churn_seconds: int = 600
    churn_commits_per_minute: int = 30
    churn_keys_per_commit: int = 60
    burst_every_seconds: int = 120
    burst_keys: int = 3000
    storm_keys: int = 1_000_000
    second_block_bytes: int = 65536
    optimize: str = "ReleaseSafe"

    def __post_init__(self):
        unknown = sorted(set(self.skip) - set(OPTIONAL_PHASES))
        if unknown:
            raise ValueError(f"Unknown phases to skip: {unknown}")
        if self.build_id is not None and not BUILD.fullmatch(self.build_id):
            raise ValueError("Invalid build id")
        if self.optimize not in ("ReleaseSafe", "ReleaseFast"):
            raise ValueError("Unsupported optimize mode")

    def runs(self, phase):
        return phase not in self.skip


SECRET_NAMES = (
    "INDEX_R2_ENDPOINT",
    "INDEX_R2_BUCKET",
    "INDEX_R2_ACCESS_KEY_ID",
    "INDEX_R2_SECRET_ACCESS_KEY",
)


def redact(text):
    for name in SECRET_NAMES:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, f"[{name}]")
            text = text.replace(json.dumps(value)[1:-1], f"[{name}]")
    return text


def emit(record):
    print(redact(json.dumps(record, separators=(",", ":"))), flush=True)


@dataclass
class Job:
    phase: str
    process: subprocess.Popen
    stdout: Path
    stderr: Path
    started: float
    offset: int = 0
    records: list = field(default_factory=list)


class Supervisor:
    def __init__(self, scratch, deadline, budget_bytes):
        self.scratch = scratch
        self.deadline = deadline
        self.budget_bytes = budget_bytes
        self.free_at_start = shutil.disk_usage(scratch).free
        self.peak_used = 0
        self.logs = scratch / "logs"
        self.logs.mkdir()
        self.jobs = []
        self.sequence = 0

    def remaining(self):
        return self.deadline - time.monotonic()

    def used(self):
        used = max(0, self.free_at_start - shutil.disk_usage(self.scratch).free)
        self.peak_used = max(self.peak_used, used)
        return used

    def check(self):
        if self.remaining() <= 0:
            raise TimeoutError("zkv scale deadline reached")
        if self.used() > self.budget_bytes:
            raise RuntimeError("zkv scale disk budget exceeded")

    def spawn(self, phase, command, env=None, cwd=None):
        self.check()
        self.sequence += 1
        stem = self.logs / f"{self.sequence:03}-{phase}"
        stdout, stderr = stem.with_suffix(".out"), stem.with_suffix(".err")
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                [str(part) for part in command],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        job = Job(phase, process, stdout, stderr, time.monotonic())
        self.jobs.append(job)
        return job

    def drain(self, job, final=False):
        with job.stdout.open("rb") as stream:
            stream.seek(job.offset)
            data = stream.read()
        end = len(data) if final else data.rfind(b"\n") + 1
        job.offset += end
        for line in data[:end].splitlines():
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                record = {"line": text[:500]}
            job.records.append(record)
            emit({"phase": job.phase, **record})

    def finish(self, job, check=True):
        try:
            while job.process.poll() is None:
                self.check()
                self.drain(job)
                time.sleep(0.2)
        finally:
            self.stop(job)
        self.drain(job, final=True)
        elapsed = round(time.monotonic() - job.started, 3)
        if job.process.returncode:
            tail = job.stderr.read_bytes()[-2000:].decode(errors="replace")
            failure = {
                "phase": job.phase,
                "event": "command_failed",
                "returncode": job.process.returncode,
                "seconds": elapsed,
                "stderr_tail": tail,
            }
            emit(failure)
            if check:
                raise RuntimeError(
                    f"{job.phase} failed with exit {job.process.returncode}"
                )
            return [*job.records, failure]
        return job.records

    def run(self, phase, command, check=True, env=None, cwd=None):
        return self.finish(self.spawn(phase, command, env=env, cwd=cwd), check=check)

    def stop(self, job):
        if job.process.poll() is None:
            try:
                os.killpg(job.process.pid, signal.SIGTERM)
                job.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(job.process.pid, signal.SIGKILL)
                job.process.wait(timeout=5)
            except ProcessLookupError:
                job.process.wait(timeout=5)
        if job in self.jobs:
            self.jobs.remove(job)

    def stop_all(self):
        for job in list(self.jobs):
            self.stop(job)


def last(records, event):
    found = [record for record in records if record.get("event") == event]
    if not found:
        raise RuntimeError(f"No {event} result")
    return found[-1]


def host_facts(scratch):
    usage = shutil.disk_usage(scratch)
    memory = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory = int(line.split()[1]) * 1024
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "memory_bytes": memory,
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
    }


def zig_version(binary):
    result = subprocess.run(
        [str(binary), "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def locate_zig(supervisor, tools):
    candidates = [ZIG_HOME / "zig", shutil.which("zig")]
    for candidate in candidates:
        if (
            candidate
            and Path(candidate).exists()
            and zig_version(candidate) == ZIG_VERSION
        ):
            return Path(candidate), "present"
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError(f"Zig {ZIG_VERSION} is required on PATH")
    archive = tools / "zig.tar.xz"
    supervisor.run(
        "tools",
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            ZIG_URL,
            "-o",
            archive,
        ],
    )
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != ZIG_SHA256:
            raise RuntimeError("Zig archive checksum mismatch")
    supervisor.run("tools", ["tar", "-xJf", archive, "-C", tools])
    archive.unlink()
    return tools / "zig-x86_64-linux-0.17.0" / "zig", "downloaded"


def build_tools(supervisor, root, tools, optimize):
    env = dict(os.environ)
    facts = {"optimize": optimize}
    if sys.platform == "linux" and shutil.which("cc") is None:
        supervisor.run(
            "tools",
            [
                "sudo",
                "env",
                "NEEDRESTART_MODE=l",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "-y",
                "-qq",
                "--no-install-recommends",
                "build-essential",
                "pkg-config",
            ],
        )
        facts["installed_build_essential"] = True
    if shutil.which("cargo") is None:
        env["RUSTUP_HOME"] = str(tools / "rustup")
        env["CARGO_HOME"] = str(tools / "cargo")
        installer = tools / "rustup.sh"
        supervisor.run(
            "tools",
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "https://sh.rustup.rs",
                "-o",
                installer,
            ],
        )
        supervisor.run(
            "tools",
            [
                "sh",
                installer,
                "-y",
                "--profile",
                "minimal",
                "--default-toolchain",
                RUST_TOOLCHAIN,
                "--no-modify-path",
            ],
            env=env,
        )
        env["PATH"] = str(tools / "cargo" / "bin") + os.pathsep + env["PATH"]
        facts["installed_rust"] = RUST_TOOLCHAIN
    zig, origin = locate_zig(supervisor, tools)
    facts["zig"] = origin
    env["CARGO_TARGET_DIR"] = str(tools / "cargo-target")
    env["CARGO_BUILD_JOBS"] = str(os.cpu_count() or 1)
    env["ZIG_GLOBAL_CACHE_DIR"] = str(tools / "zig-global-cache")
    env["ZIG_LOCAL_PKG_DIR"] = str(tools / "zig-pkg")

    started = time.monotonic()
    supervisor.run(
        "tools",
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            root / "dump-actors" / "Cargo.toml",
        ],
        env=env,
    )
    facts["dump_actors_build_seconds"] = round(time.monotonic() - started, 1)
    started = time.monotonic()
    supervisor.run(
        "tools",
        [
            zig,
            "build",
            f"-Doptimize={optimize}",
            "--prefix",
            tools / "zkvscale",
            "--cache-dir",
            tools / "zig-cache",
        ],
        env=env,
        cwd=root / "zkvscale",
    )
    facts["zkvscale_build_seconds"] = round(time.monotonic() - started, 1)
    dump = tools / "cargo-target" / "release" / "dump-actors"
    zkvscale = tools / "zkvscale" / "bin" / "zkvscale"
    for binary in (dump, zkvscale):
        with binary.open("rb") as stream:
            facts[binary.name + "_sha256"] = hashlib.file_digest(
                stream, "sha256"
            ).hexdigest()
    return dump, zkvscale, facts


def r2_client():
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["INDEX_R2_ENDPOINT"],
        region_name="auto",
        aws_access_key_id=os.environ["INDEX_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["INDEX_R2_SECRET_ACCESS_KEY"],
    )
    return client, os.environ["INDEX_R2_BUCKET"]


def newest_build(client, bucket):
    manifests, bundles = {}, set()
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=RELEASES
    ):
        for item in page.get("Contents", []):
            parts = item["Key"].split("/")
            if len(parts) != 4 or not BUILD.fullmatch(parts[2]):
                continue
            if parts[3] == "manifest.json":
                manifests[parts[2]] = item["LastModified"]
            elif parts[3] == "index.tar":
                bundles.add(parts[2])
    complete = [
        (published, build) for build, published in manifests.items() if build in bundles
    ]
    if not complete:
        raise RuntimeError("No published search bundle found")
    return max(complete)[1]


def validate_manifest(manifest, build):
    if manifest.get("active_build") != build or manifest.get("engine") != "tantivy":
        raise ValueError("Unexpected bundle manifest identity")
    if manifest.get("object_key") != f"{RELEASES}{build}/index.tar":
        raise ValueError("Bundle manifest names another object")
    if not isinstance(manifest.get("byte_size"), int) or manifest["byte_size"] < 1:
        raise ValueError("Bundle manifest has no size")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256"))):
        raise ValueError("Bundle manifest has no checksum")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Bundle manifest has no file inventory")
    names = {item.get("path") for item in files}
    if "index/meta.json" not in names:
        raise ValueError("Bundle manifest has no index commit")
    return manifest


def read_manifest(client, bucket, build):
    body = client.get_object(Bucket=bucket, Key=f"{RELEASES}{build}/manifest.json")[
        "Body"
    ]
    try:
        raw = body.read(32 * 1024 * 1024 + 1)
    finally:
        body.close()
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("Bundle manifest too large")
    return validate_manifest(json.loads(raw), build)


class HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes = 0

    def read(self, size=-1):
        chunk = self.stream.read(size)
        self.digest.update(chunk)
        self.bytes += len(chunk)
        return chunk


def extract_index(bundle, manifest, destination):
    expected = {
        item["path"]: item["byte_size"]
        for item in manifest["files"]
        if item["path"].startswith("index/")
    }
    if any(not INDEX_MEMBER.fullmatch(name) for name in expected):
        raise ValueError("Bundle inventory has an unsafe index path")
    index = destination / "index"
    index.mkdir(parents=True)
    seen = set()
    with bundle.open("rb") as stream:
        reader = HashingReader(stream)
        with tarfile.open(fileobj=reader, mode="r|", bufsize=1024 * 1024) as archive:
            for member in archive:
                if member.name not in expected:
                    continue
                if not member.isfile() or member.name in seen:
                    raise ValueError("Bundle index member is not a unique regular file")
                if member.size != expected[member.name]:
                    raise ValueError(
                        "Bundle index member size differs from its manifest"
                    )
                seen.add(member.name)
                source = archive.extractfile(member)
                with (destination / member.name).open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        while reader.read(1024 * 1024):
            pass
    if (
        reader.bytes != manifest["byte_size"]
        or reader.digest.hexdigest() != manifest["sha256"]
    ):
        raise ValueError("Bundle differs from its manifest checksum")
    if seen != set(expected):
        raise ValueError("Bundle is missing index files")
    return {
        "index_files": len(seen),
        "index_bytes": sum(expected.values()),
        "bundle_bytes": reader.bytes,
        "bundle_sha256_verified": True,
    }


def fetch(manifest_path, destination):
    from boto3.s3.transfer import TransferConfig

    manifest = json.loads(Path(manifest_path).read_text())
    destination = Path(destination)
    bundle = destination / "index.tar"
    client, bucket = r2_client()
    started = time.monotonic()
    client.download_file(
        bucket,
        manifest["object_key"],
        str(bundle),
        Config=TransferConfig(max_concurrency=8, multipart_chunksize=64 * 1024 * 1024),
    )
    downloaded = time.monotonic()
    facts = extract_index(bundle, manifest, destination)
    bundle.unlink()
    emit(
        {
            "event": "fetch",
            **facts,
            "download_seconds": round(downloaded - started, 1),
            "verify_extract_seconds": round(time.monotonic() - downloaded, 1),
        }
    )


def select_bundle(plan):
    from botocore.exceptions import BotoCoreError, ClientError

    client, bucket = r2_client()
    try:
        build = plan.build_id or newest_build(client, bucket)
        return build, read_manifest(client, bucket, build)
    except (
        BotoCoreError,
        ClientError,
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
    ) as error:
        raise RuntimeError(
            redact(f"Bundle selection failed: {type(error).__name__}: {error}")
        ) from None
    finally:
        client.close()


def acquire(supervisor, plan, scratch):
    build, manifest = select_bundle(plan)
    if manifest["byte_size"] * 2 > supervisor.budget_bytes - supervisor.used():
        raise RuntimeError("Bundle does not fit the disk budget")
    bundle_dir = scratch / "bundle"
    bundle_dir.mkdir()
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    records = supervisor.run(
        "fetch",
        [sys.executable, Path(__file__).resolve(), "fetch", manifest_path, bundle_dir],
    )
    facts = {
        "build_id": build,
        "source_actor_count": manifest.get("source_actor_count"),
        "source_watermark": manifest.get("source_watermark"),
        **{
            key: value
            for key, value in last(records, "fetch").items()
            if key != "event"
        },
    }
    return bundle_dir / "index", facts


def load(supervisor, phase, zkvscale, store, source, extra=()):
    command = [zkvscale, "load", "--store", store, *extra]
    if source["kind"] == "jsonl":
        records = supervisor.run(phase, [*command, "--input", source["path"]])
        return last(records, "load"), None
    count = supervisor.logs / f"{phase}-documents.txt"
    pipeline = 'set -o pipefail; "$1" "$2" 2>"$3" | "${@:4}"'
    try:
        records = supervisor.run(
            phase,
            [
                "bash",
                "-c",
                pipeline,
                "pipeline",
                source["dump"],
                source["index"],
                count,
                *command,
                "--input",
                "-",
            ],
        )
    except RuntimeError:
        if count.exists():
            emit(
                {
                    "phase": phase,
                    "event": "dump_stderr",
                    "text": count.read_text()[-1000:],
                }
            )
        raise
    return last(records, "load"), int(count.read_text().split()[-1])


def stats(supervisor, phase, zkvscale, store):
    results = []
    for handle in ("writer", "reader"):
        for stride in (1, 32):
            records = supervisor.run(
                phase,
                [
                    zkvscale,
                    "stat",
                    "--store",
                    store,
                    f"--{handle}",
                    "--fence-stride",
                    stride,
                ],
            )
            results.append(last(records, "stat"))
    return results


def drop_page_cache(supervisor):
    if not Path("/proc/sys/vm/drop_caches").exists():
        record = {"phase": "drop_caches", "event": "drop_caches", "dropped": False}
        emit(record)
        return record
    supervisor.run(
        "drop_caches",
        ["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
    )
    record = {"phase": "drop_caches", "event": "drop_caches", "dropped": True}
    emit(record)
    return record


def churn(supervisor, plan, zkvscale, store, dids):
    writer = supervisor.spawn(
        "churn",
        [
            zkvscale,
            "churn",
            "--store",
            store,
            "--dids",
            dids,
            "--seconds",
            plan.churn_seconds,
            "--commits-per-minute",
            plan.churn_commits_per_minute,
            "--keys-per-commit",
            plan.churn_keys_per_commit,
            "--burst-every",
            plan.burst_every_seconds,
            "--burst-keys",
            plan.burst_keys,
        ],
    )
    scans = []
    try:
        while writer.process.poll() is None:
            supervisor.drain(writer)
            scans.extend(
                supervisor.run(
                    "churn_scan",
                    [zkvscale, "scan", "--store", store, "--prefix", "a", "--reader"],
                    check=False,
                )
            )
            if scans[-1].get("event") == "command_failed":
                time.sleep(1)
    finally:
        records = supervisor.finish(writer)
    return last(records, "churn"), scans


def scan_summary(scans):
    complete = [scan for scan in scans if scan.get("event") == "scan"]
    return {
        "scans": len(complete),
        "failed_scans": sum(scan.get("event") == "command_failed" for scan in scans),
        "missing_errors": sum(scan["totals"]["missing_errors"] for scan in complete),
        "retries": sum(scan["totals"]["retries"] for scan in complete),
        "slowest_ms": max((scan["wall_ms"] for scan in complete), default=None),
        "fastest_ms": min((scan["wall_ms"] for scan in complete), default=None),
        "entries": sorted({scan["totals"]["entries"] for scan in complete}),
    }


def entries_by_prefix(count):
    return {row["prefix"]: row["entries"] for row in count["prefixes"]}


def second_load_block(supervisor, results):
    spent = results["load"]["wall_ms"] / 1000
    spent += results.get("settle", {}).get("wall_ms", 0) / 1000
    peaks = [results["load"]["disk_peak_bytes"]]
    peaks += [
        results[name]["disk_peak_bytes"]
        for name in ("settle", "churn", "storm")
        if name in results
    ]
    need_seconds = 2 * spent + 120
    need_bytes = int(1.5 * max(peaks))
    available = supervisor.budget_bytes - supervisor.used()
    if supervisor.remaining() < need_seconds:
        return {
            "reason": "time",
            "need_seconds": round(need_seconds),
            "remaining_seconds": round(supervisor.remaining()),
        }
    if available < need_bytes:
        return {
            "reason": "disk",
            "need_bytes": need_bytes,
            "available_bytes": available,
        }
    return None


def phases(supervisor, plan, root, scratch, summary):
    results = summary["phases"]
    tools = scratch / "tools"
    tools.mkdir()
    dump, zkvscale, summary["tools"] = build_tools(
        supervisor, root, tools, plan.optimize
    )
    emit({"phase": "tools", "event": "tools", **summary["tools"]})

    if plan.input_jsonl:
        path = Path(plan.input_jsonl).resolve()
        source = {"kind": "jsonl", "path": path}
        summary["input"] = {"kind": "jsonl", "jsonl_bytes": path.stat().st_size}
    else:
        index, facts = acquire(supervisor, plan, scratch)
        source = {"kind": "index", "dump": dump, "index": index}
        summary["input"] = {"kind": "bundle", **facts}
    emit({"phase": "input", "event": "input", **summary["input"]})

    store = scratch / "store"
    results["load"], documents = load(supervisor, "load", zkvscale, store, source)
    summary["input"]["documents"] = (
        documents if documents is not None else results["load"]["records"]
    )
    summary["input"]["jsonl_bytes"] = results["load"]["input_bytes"]
    emit({"phase": "input", "event": "input_measured", **summary["input"]})

    if plan.runs("count"):
        results["count"] = last(
            supervisor.run("count", [zkvscale, "count", "--store", store]), "count"
        )
    if plan.runs("stat"):
        results["stat"] = stats(supervisor, "stat", zkvscale, store)
    if plan.runs("settle"):
        results["settle"] = last(
            supervisor.run("settle", [zkvscale, "settle", "--store", store]), "settle"
        )
        if plan.runs("count"):
            results["count_settled"] = last(
                supervisor.run("count_settled", [zkvscale, "count", "--store", store]),
                "count",
            )
        if plan.runs("stat"):
            results["stat_settled"] = stats(supervisor, "stat_settled", zkvscale, store)

    dids = scratch / "dids.txt"
    bursts = (
        plan.churn_seconds // plan.burst_every_seconds
        if plan.burst_every_seconds
        else 0
    )
    churn_keys = (
        plan.churn_seconds * plan.churn_commits_per_minute // 60 + 1
    ) * plan.churn_keys_per_commit + bursts * plan.burst_keys
    wanted = max(
        plan.get_reads if plan.runs("get") or plan.runs("get_stride32") else 0,
        churn_keys if plan.runs("churn") else 0,
        plan.storm_keys if plan.runs("storm") else 0,
    )
    if wanted:
        results["sample"] = last(
            supervisor.run(
                "sample",
                [zkvscale, "sample", "--store", store, "--n", wanted, "--out", dids],
            ),
            "sample",
        )
    for phase, stride in (("get", 1), ("get_stride32", 32)):
        if not plan.runs(phase):
            continue
        dropped = drop_page_cache(supervisor)
        results[phase] = {
            "page_cache_dropped": dropped["dropped"],
            **last(
                supervisor.run(
                    phase,
                    [
                        zkvscale,
                        "get",
                        "--store",
                        store,
                        "--dids",
                        dids,
                        "--n",
                        plan.get_reads,
                        "--fence-stride",
                        stride,
                    ],
                ),
                "get",
            ),
        }
    if plan.runs("churn"):
        results["churn"], scans = churn(supervisor, plan, zkvscale, store, dids)
        results["churn_scan"] = scan_summary(scans)
        emit({"phase": "churn_scan", "event": "scan_summary", **results["churn_scan"]})
    if plan.runs("storm"):
        results["storm"] = last(
            supervisor.run(
                "storm",
                [
                    zkvscale,
                    "storm",
                    "--store",
                    store,
                    "--dids",
                    dids,
                    "--keys",
                    plan.storm_keys,
                ],
            ),
            "storm",
        )
    results["stat_final"] = last(
        supervisor.run("stat_final", [zkvscale, "stat", "--store", store, "--reader"]),
        "stat",
    )
    if plan.runs("final_count"):
        results["count_final"] = last(
            supervisor.run("count_final", [zkvscale, "count", "--store", store]),
            "count",
        )
        if "count" in results:
            results["count_final"]["matches_first_count"] = entries_by_prefix(
                results["count_final"]
            ) == entries_by_prefix(results["count"])

    if plan.runs("second_load"):
        blocked = second_load_block(supervisor, results)
        if blocked:
            results["second_load"] = {"skipped": True, **blocked}
            emit({"phase": "second_load", "event": "skipped", **blocked})
        else:
            second = scratch / "store-block"
            block = ["--block-bytes", plan.second_block_bytes]
            loaded, _ = load(supervisor, "second_load", zkvscale, second, source, block)
            results["second_load"] = loaded
            if plan.runs("settle"):
                results["second_settle"] = last(
                    supervisor.run(
                        "second_settle", [zkvscale, "settle", "--store", second, *block]
                    ),
                    "settle",
                )
            results["second_stat"] = last(
                supervisor.run(
                    "second_stat", [zkvscale, "stat", "--store", second, "--reader"]
                ),
                "stat",
            )


def measure(run_id, root, plan):
    parent = Path(plan.scratch_parent)
    free_before = shutil.disk_usage(parent).free
    if free_before < plan.required_free_gib * GIB:
        raise RuntimeError("Insufficient free disk for the zkv scale measurement")
    scratch = parent / f"typeahead-zkv-scale-{run_id}"
    scratch.mkdir()
    started = time.monotonic()
    deadline = started + DEADLINE_SECONDS - CLEANUP_RESERVE_SECONDS
    summary = {
        "event": "zkv_scale_summary",
        "owner": OWNER,
        "run_id": str(run_id),
        "scratch": str(scratch),
        "status": "failed",
        "plan": asdict(plan),
        "phases": {},
    }
    supervisor = None
    try:
        supervisor = Supervisor(scratch, deadline, int(plan.budget_gib * GIB))
        summary["host"] = host_facts(scratch)
        emit(
            {
                "phase": "preflight",
                "event": "preflight",
                "scratch": str(scratch),
                **summary["host"],
            }
        )
        phases(supervisor, plan, root, scratch, summary)
        summary["status"] = "completed"
    except BaseException as error:
        summary["error"] = redact(f"{type(error).__name__}: {error}")[:500]
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
        emit(summary)
    return summary


@flow(
    name="typeahead-zkv-scale",
    retries=0,
    timeout_seconds=DEADLINE_SECONDS,
    log_prints=True,
)
def zkv_scale(
    build_id: str | None = None,
    input_jsonl: str | None = None,
    skip: list[str] | None = None,
    scratch_parent: str = "/var/tmp",
    required_free_gib: float = 80,
    budget_gib: float = 60,
    get_reads: int = 20_000,
    churn_seconds: int = 600,
    churn_commits_per_minute: int = 30,
    churn_keys_per_commit: int = 60,
    burst_every_seconds: int = 120,
    burst_keys: int = 3000,
    storm_keys: int = 1_000_000,
    second_block_bytes: int = 65536,
    optimize: str = "ReleaseSafe",
) -> dict:
    from prefect.runtime import flow_run

    plan = Plan(
        build_id=build_id,
        input_jsonl=input_jsonl,
        skip=tuple(skip or ()),
        scratch_parent=scratch_parent,
        required_free_gib=required_free_gib,
        budget_gib=budget_gib,
        get_reads=get_reads,
        churn_seconds=churn_seconds,
        churn_commits_per_minute=churn_commits_per_minute,
        churn_keys_per_commit=churn_keys_per_commit,
        burst_every_seconds=burst_every_seconds,
        burst_keys=burst_keys,
        storm_keys=storm_keys,
        second_block_bytes=second_block_bytes,
        optimize=optimize,
    )
    return measure(
        flow_run.id or uuid.uuid4().hex, Path(__file__).resolve().parent, plan
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Supervised child of the zkv scale flow."
    )
    parser.add_argument("action", choices=["fetch"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    fetch(arguments.manifest, arguments.destination)
