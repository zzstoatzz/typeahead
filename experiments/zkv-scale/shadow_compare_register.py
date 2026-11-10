"""Create or delete the temporary unscheduled deployment of the shadow comparison."""

import argparse
import asyncio
import importlib.util
import re
from pathlib import Path

from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import ConcurrencyLimitStrategy, ConcurrencyOptions
from prefect.exceptions import ObjectNotFound
from prefect.utilities.callables import parameter_schema

FLOW = "typeahead-shadow-compare"
NAME = "issue-43"
POOL = "typeahead-ephemeral"
REPOSITORY = "https://github.com/zzstoatzz/typeahead.git"
ENTRYPOINT = "experiments/zkv-scale/shadow_compare_cloud.py:shadow_compare"
MPS_PIN = "26fae38154907881e5b25133e86c168a1c0c38e1"
VM_TIMEOUT_SECONDS = 15600
SECRET_ENV = "SHADOW_STORE_SECRET"


def deployment(commit, mps_pin, shadow_url, secret_block):
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
        "description": "Temporary comparison of the shadow's store with production's newest search bundle (issue 43).",
        "tags": ["experiment", "shadow-compare"],
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
                SECRET_ENV: f"prefect-block://{secret_block}",
            },
        },
        "parameters": {"shadow_url": shadow_url, "shadow_secret_env": SECRET_ENV},
        "paused": False,
        "enforce_parameter_schema": True,
        "concurrency_limit": 1,
        "concurrency_options": ConcurrencyOptions(
            collision_strategy=ConcurrencyLimitStrategy.CANCEL_NEW
        ),
    }


def flow_schema():
    location = Path(__file__).resolve().with_name("shadow_compare_cloud.py")
    spec = importlib.util.spec_from_file_location("shadow_compare_cloud", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.shadow_compare.name != FLOW:
        raise ValueError("Flow name differs from the registered name")
    return parameter_schema(module.shadow_compare.fn).model_dump_for_openapi()


async def create(commit, mps_pin, shadow_url, secret_block):
    async with get_client() as client:
        flow_id = await client.create_flow_from_name(FLOW)
        deployment_id = await client.create_deployment(
            flow_id=flow_id,
            parameter_openapi_schema=flow_schema(),
            **deployment(commit, mps_pin, shadow_url, secret_block),
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


def block(value):
    if not re.fullmatch(r"[a-z0-9-]+", value):
        raise argparse.ArgumentTypeError("a secret block name is lowercase letters, digits and dashes")
    return value


def origin(value):
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]+)?", value):
        raise argparse.ArgumentTypeError("the shadow's store is an https origin without a path")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", nargs="?", type=sha, help="typeahead commit to clone")
    parser.add_argument("--shadow-url", type=origin, help="origin of the shadow's store process")
    parser.add_argument("--shadow-secret-block", type=block, help="name of the Prefect secret block holding the store's read bearer")
    parser.add_argument("--mps-pin", type=sha, default=MPS_PIN)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    if args.delete == bool(args.commit):
        parser.error("pass a commit SHA to create, or --delete alone to remove")
    if args.delete:
        asyncio.run(delete())
        return
    if not args.shadow_url or not args.shadow_secret_block:
        parser.error("creating the deployment needs --shadow-url and --shadow-secret-block")
    asyncio.run(create(args.commit, args.mps_pin, args.shadow_url, args.shadow_secret_block))


if __name__ == "__main__":
    main()
