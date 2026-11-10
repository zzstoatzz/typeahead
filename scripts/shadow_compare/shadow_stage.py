#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Run the corpus phase on the shadow machine, against views a builder packed into R2.

`run` fetches the manifest, the sidecar and the gzipped views of one builder
run with rclone, checks each against the manifest and the manifest against the
SHA-256 given on the command line, then walks the store on loopback. A second
`run` for the same run id finds the views in place and resumes the walk from
its checkpoint. `clean` removes the fetched inputs and leaves the report.

Credentials are read from the environment and never printed: the five
`COMPARE_R2_*` values for rclone, and `STORE_READ_SECRET`, taken from this
process's environment or else from a running process that has it.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bundle
import compare
from transport import TransportError

GIB = 1024**3
ROOT = "/data/compare"
STORE = "http://127.0.0.1:8082"
STORE_SECRET = "STORE_READ_SECRET"
RESERVE_GIB = 12.0
WORKING_BYTES = GIB
R2_REQUIRED = ("COMPARE_R2_ENDPOINT", "COMPARE_R2_BUCKET", "COMPARE_R2_ACCESS_KEY_ID", "COMPARE_R2_SECRET_ACCESS_KEY")
R2_SESSION = "COMPARE_R2_SESSION_TOKEN"
REMOTE_OVERRIDE = "COMPARE_REMOTE"
NICENESS = 10
MAX_REQUESTS = 80_000
MAX_MIB = 32_768
CHECKPOINT_INTERVAL_S = 60


class StageError(TransportError):
    pass


def redact(text, environ):
    for name in (*R2_REQUIRED[2:], R2_SESSION, STORE_SECRET):
        value = environ.get(name)
        if value:
            text = text.replace(value, f"[{name}]")
    return text


def remote(environ):
    """Where the builder's runs are, as an rclone path, and the environment that lets rclone read it."""
    if environ.get(REMOTE_OVERRIDE):
        return environ[REMOTE_OVERRIDE].rstrip("/"), dict(environ)
    missing = [name for name in R2_REQUIRED if not environ.get(name)]
    if missing:
        raise StageError("unset: " + ", ".join(missing))
    settings = {
        "TYPE": "s3",
        "PROVIDER": "Cloudflare",
        "REGION": "auto",
        "NO_CHECK_BUCKET": "true",
        "ENDPOINT": environ["COMPARE_R2_ENDPOINT"],
        "ACCESS_KEY_ID": environ["COMPARE_R2_ACCESS_KEY_ID"],
        "SECRET_ACCESS_KEY": environ["COMPARE_R2_SECRET_ACCESS_KEY"],
    }
    if environ.get(R2_SESSION):
        settings["SESSION_TOKEN"] = environ[R2_SESSION]
    env = {**environ, **{"RCLONE_CONFIG_COMPARE_" + name: value for name, value in settings.items()}}
    return f"compare:{environ['COMPARE_R2_BUCKET']}/{bundle.SCRATCH_PREFIX}", env


def failed(what, stderr, environ):
    tail = redact(stderr.decode(errors="replace").strip()[-600:], environ)
    return StageError(f"could not fetch {what}" + (": " + tail if tail else ""))


def fetch_small(source, env, limit):
    """One small object's bytes; refuses an object over `limit`."""
    done = subprocess.run(["rclone", "cat", source, "--count", str(limit + 1)], env=env, capture_output=True, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise failed(source, done.stderr, env)
    if len(done.stdout) > limit:
        raise StageError(f"{source} is larger than {limit} bytes")
    return done.stdout


def fetch_views(source, env, manifest, target):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(["rclone", "cat", source], env=env, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL)
        problem = None
        try:
            bundle.unpack(process.stdout, manifest, target)
        except bundle.BundleError as error:
            problem = error
            process.kill()
        finally:
            process.stdout.close()
            process.wait()
        if problem is None and process.returncode == 0:
            return
        target.unlink(missing_ok=True)
        errors.seek(0)
        if process.returncode > 0:
            raise failed(source, errors.read(), env)
        raise problem


def require_disk(directory, need, reserve):
    free = shutil.disk_usage(directory).free
    if free - need < reserve:
        raise StageError(
            f"{directory} has {free / GIB:.1f} GiB free; this step needs {need / GIB:.1f} GiB and must leave {reserve / GIB:.1f} GiB"
        )


def held(inputs, sha256):
    """The manifest of inputs already fetched and checked for this SHA-256, or None."""
    saved, views = inputs / bundle.MANIFEST_NAME, inputs / "views.jsonl"
    if not saved.exists() or not views.exists() or not bundle.meta_path(views).exists():
        return None
    raw = saved.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        return None
    manifest = bundle.read_manifest(raw, sha256)
    return manifest if views.stat().st_size == manifest["views"]["unpacked_bytes"] else None


def acquire(run_dir, run_id, sha256, environ, reserve):
    inputs = run_dir / "inputs"
    manifest = held(inputs, sha256)
    if manifest is not None:
        return manifest, inputs / "views.jsonl"
    base, env = remote(environ)
    base = f"{base}/{run_id}"
    raw = fetch_small(f"{base}/{bundle.MANIFEST_NAME}", env, bundle.MANIFEST_MAX_BYTES)
    manifest = bundle.read_manifest(raw, sha256)
    if manifest["run_id"] != run_id:
        raise StageError("manifest belongs to another run")
    shutil.rmtree(inputs, ignore_errors=True)
    inputs.mkdir(parents=True)
    require_disk(inputs, manifest["views"]["unpacked_bytes"] + manifest["sidecar"]["bytes"] + WORKING_BYTES, reserve)
    sidecar = fetch_small(f"{base}/{bundle.SIDECAR_NAME}", env, manifest["sidecar"]["bytes"])
    if (len(sidecar), hashlib.sha256(sidecar).hexdigest()) != (manifest["sidecar"]["bytes"], manifest["sidecar"]["sha256"]):
        raise StageError("sidecar differs from its manifest")
    views = inputs / "views.jsonl"
    fetch_views(f"{base}/{bundle.VIEWS_NAME}", env, manifest, views)
    bundle.meta_path(views).write_bytes(sidecar)
    (inputs / bundle.MANIFEST_NAME).write_bytes(raw)
    return manifest, views


def store_secret(environ, proc=Path("/proc")):
    """The store's read bearer: from this environment, else from a running process that was started with it."""
    if environ.get(STORE_SECRET):
        return environ[STORE_SECRET]
    prefix = STORE_SECRET.encode() + b"="
    for entry in sorted((item for item in proc.iterdir() if item.name.isdigit()), key=lambda item: int(item.name)):
        try:
            variables = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for variable in variables:
            if variable.startswith(prefix) and len(variable) > len(prefix):
                return variable[len(prefix) :].decode()
    raise StageError(f"{STORE_SECRET} is set neither here nor in a running process")


def run(args, extra, environ):
    """Fetch what is missing and walk; `environ` must be the process environment, which compare.py reads the bearer from."""
    run_dir = Path(args.root) / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    reserve = int(args.reserve_gib * GIB)
    environ[STORE_SECRET] = store_secret(environ)
    manifest, views = acquire(run_dir, args.run_id, args.manifest_sha256, environ, reserve)
    require_disk(run_dir, WORKING_BYTES, reserve)
    command = [
        "--phases", "corpus",
        "--production-bundle", str(views),
        "--bundle-watermark", str(manifest["source_watermark"]),
        "--bundle-build-id", manifest["build_id"],
        "--shadow-store", args.store,
        "--shadow-store-secret-env", STORE_SECRET,
        "--corpus-delay", str(args.delay),
        "--corpus-max-requests", str(MAX_REQUESTS),
        "--corpus-max-mib", str(MAX_MIB),
        "--checkpoint", str(run_dir / "walk.json"),
        "--checkpoint-interval", str(CHECKPOINT_INTERVAL_S),
        "--out-dir", str(run_dir / "report"),
    ]
    if not args.no_arbiter:
        command.append("--arbiter")
    return compare.main([*command, *extra])


def clean(args):
    inputs = Path(args.root) / args.run_id / "inputs"
    present = inputs.exists()
    shutil.rmtree(inputs, ignore_errors=True)
    print(f"{inputs}: " + ("removed" if present and not inputs.exists() else "absent" if not present else "could not be removed"))
    return 1 if inputs.exists() else 0


def run_id(value):
    if not bundle.RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("a run id is 1 to 64 letters, digits, dashes or underscores")
    return value


def sha256(value):
    if not bundle.SHA256.fullmatch(value):
        raise argparse.ArgumentTypeError("64 lowercase hex digits")
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=ROOT, help="directory that holds one subdirectory per run")
    commands = p.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run", help="fetch the views if they are not here, then walk; further arguments go to compare.py")
    command.add_argument("--run-id", required=True, type=run_id, help="the builder's run id")
    command.add_argument("--manifest-sha256", required=True, type=sha256, help="SHA-256 of the run's manifest, from the builder's summary")
    command.add_argument("--store", default=STORE, help="origin of the store process")
    command.add_argument("--delay", type=float, default=0.1, help="seconds between requests to the store")
    command.add_argument("--reserve-gib", type=float, default=RESERVE_GIB, help="free space that must remain on the volume")
    command.add_argument("--no-arbiter", action="store_true")
    command = commands.add_parser("clean", help="remove a run's fetched inputs; its report and checkpoint stay")
    command.add_argument("--run-id", required=True, type=run_id)
    return p


def main(argv=None):
    environ = os.environ
    args, extra = parser().parse_known_args(argv)
    if args.command == "clean":
        if extra:
            parser().error("unrecognized arguments: " + " ".join(extra))
        return clean(args)
    try:
        os.nice(NICENESS)
        return run(args, extra, environ)
    except (TransportError, OSError) as error:
        print("error: " + redact(str(error), environ), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
