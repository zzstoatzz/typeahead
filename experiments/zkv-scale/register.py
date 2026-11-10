"""Create or delete the temporary unscheduled deployment of the zkv scale measurement."""

import argparse
import asyncio
import importlib.util
import re
from pathlib import Path

from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import ConcurrencyLimitStrategy, ConcurrencyOptions
from prefect.exceptions import ObjectNotFound
from prefect.utilities.callables import parameter_schema

FLOW = "typeahead-zkv-scale"
NAME = "issue-32"
POOL = "typeahead-ephemeral"
REPOSITORY = "https://github.com/zzstoatzz/typeahead.git"
ENTRYPOINT = "experiments/zkv-scale/cloud.py:zkv_scale"
MPS_PIN = "26fae38154907881e5b25133e86c168a1c0c38e1"
VM_TIMEOUT_SECONDS = 12000


def deployment(commit, mps_pin):
    plugins = {
        "PREFECT_PLUGINS_ENABLED": "true",
        "PREFECT_PLUGINS_ALLOW": "mps-secrets",
    }
    package = f"my-prefect-server @ git+https://github.com/zzstoatzz/my-prefect-server.git@{mps_pin}"
    command = " ".join(
        [
            "env",
            *(f"{name}={value}" for name, value in plugins.items()),
            "uv run --no-project",
            f"--with '{package}'",
            "--with prefect==3.8.2",
            "prefect flow-run execute",
        ]
    )
    return {
        "name": NAME,
        "version": commit,
        "description": "Temporary measurement of zkv at Typeahead corpus scale (issue 32).",
        "tags": ["experiment", "zkv-scale"],
        "work_pool_name": POOL,
        "entrypoint": ENTRYPOINT,
        "pull_steps": [
            {
                "prefect.deployments.steps.git_clone": {
                    "repository": REPOSITORY,
                    "commit_sha": commit,
                }
            }
        ],
        "job_variables": {
            "server_type": "cpx32",
            "location": "hel1",
            "timeout_seconds": VM_TIMEOUT_SECONDS,
            "working_dir": "/var/lib/typeahead-indexer",
            "command": command,
            "env": {
                **plugins,
                "INDEX_R2_ENDPOINT": "prefect-block://index-r2-endpoint",
                "INDEX_R2_BUCKET": "prefect-block://index-r2-bucket",
                "INDEX_R2_ACCESS_KEY_ID": "prefect-block://index-r2-access-key-id",
                "INDEX_R2_SECRET_ACCESS_KEY": "prefect-block://index-r2-secret-access-key",
            },
        },
        "parameters": {},
        "paused": False,
        "enforce_parameter_schema": True,
        "concurrency_limit": 1,
        "concurrency_options": ConcurrencyOptions(
            collision_strategy=ConcurrencyLimitStrategy.CANCEL_NEW
        ),
    }


def flow_schema():
    location = Path(__file__).resolve().with_name("cloud.py")
    spec = importlib.util.spec_from_file_location("zkv_scale_cloud", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.zkv_scale.name != FLOW:
        raise ValueError("Flow name differs from the registered name")
    return parameter_schema(module.zkv_scale.fn).model_dump_for_openapi()


async def create(commit, mps_pin):
    async with get_client() as client:
        flow_id = await client.create_flow_from_name(FLOW)
        deployment_id = await client.create_deployment(
            flow_id=flow_id,
            parameter_openapi_schema=flow_schema(),
            **deployment(commit, mps_pin),
        )
    script = Path(__file__).resolve()
    print(f"deployment id: {deployment_id}")
    print(f"source: {REPOSITORY} at {commit}")
    print(f"start one run: just prefect deployment run '{FLOW}/{NAME}'")
    print(f"delete: just prefect --script {script} --delete")


async def delete():
    async with get_client() as client:
        try:
            found = await client.read_deployment_by_name(f"{FLOW}/{NAME}")
        except ObjectNotFound:
            print(f"{FLOW}/{NAME} is already absent")
            return
        await client.delete_deployment(found.id)
        print(f"deleted deployment {found.id}")


def sha(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("a full 40-character commit SHA is required")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", nargs="?", type=sha, help="typeahead commit to clone")
    parser.add_argument("--mps-pin", type=sha, default=MPS_PIN)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    if args.delete == bool(args.commit):
        parser.error("pass a commit SHA to create, or --delete alone to remove")
    asyncio.run(delete() if args.delete else create(args.commit, args.mps_pin))


if __name__ == "__main__":
    main()
