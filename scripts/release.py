#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
deploy the Workers that changed since the last release, then tag the commit.

a release tag is YYYY.MMDD.HHMMSS in UTC. it names a commit of origin/main
whose Workers are deployed and passed their smoke suites. slots are never
deployed from here: when the slot image's inputs changed, the plan says so and
docs/operating.md has the procedure.

usage:
    just release           # print the plan, ask, deploy, tag
    just release --plan    # print the plan and stop
    just release --all     # deploy every Worker whatever changed
    just release --yes     # do not ask
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RELEASE = re.compile(r'\d{4}\.\d{4}\.\d{6}')

# Every Worker bundles from these, whatever its entry imports.
SHARED_INPUTS = frozenset({'package.json', 'bun.lock'})

SLOT_DOCKERIGNORE = 'deploy/slot/Dockerfile.dockerignore'
SLOT_BUILD_FILES = frozenset({'deploy/slot/Dockerfile', SLOT_DOCKERIGNORE, 'deploy/slot/fly.toml'})


@dataclass(frozen=True)
class Worker:
    name: str
    config: str
    recipe: str


# Deploy order: the transport and the renderer go before the public Worker
# that binds them.
WORKERS = (
    Worker('typeahead-search-transport', 'deploy/search-transport/wrangler.jsonc', 'deploy-search-transport'),
    Worker('typeahead-image-renderer', 'deploy/image-renderer/wrangler.jsonc', 'deploy-image-renderer'),
    Worker('typeahead-eval-collector', 'deploy/eval/wrangler.jsonc', 'deploy-eval-collector'),
    Worker('typeahead', 'wrangler.jsonc', 'deploy-worker'),
)


@dataclass(frozen=True)
class Plan:
    head: str
    previous: str | None
    commits: tuple[str, ...]
    workers: tuple[Worker, ...]
    slot_paths: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.workers and not self.slot_paths


class Refused(Exception):
    pass


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def require_published(root: Path) -> str:
    git(root, 'fetch', '--quiet', '--tags', 'origin', 'main')
    if git(root, 'status', '--porcelain', '--untracked-files=no'):
        raise Refused('uncommitted changes: commit and push before releasing')
    head = git(root, 'rev-parse', 'HEAD')
    if head != git(root, 'rev-parse', 'origin/main'):
        raise Refused('HEAD is not origin/main: push, or check out origin/main, before releasing')
    return head


def latest_release(tags: list[str]) -> str | None:
    releases = sorted(tag for tag in tags if RELEASE.fullmatch(tag))
    return releases[-1] if releases else None


def previous_release(root: Path) -> str | None:
    return latest_release(git(root, 'tag', '--merged', 'HEAD').splitlines())


def repo_inputs(root: Path, config: str, bundled: list[str]) -> frozenset[str]:
    """The files in this repository a Worker is built from.

    esbuild names each bundled file relative to the directory of the Worker's
    configuration. Dependencies resolve outside the tracked tree and are
    covered by the lockfile instead.
    """
    base = (root / config).parent
    inputs = {config}
    for name in bundled:
        path = Path(os.path.normpath(base / name))
        if not path.is_relative_to(root):
            continue
        relative = path.relative_to(root).as_posix()
        if 'node_modules' not in relative.split('/'):
            inputs.add(relative)
    return frozenset(inputs | SHARED_INPUTS)


def bundle_inputs(root: Path, worker: Worker) -> frozenset[str]:
    with tempfile.TemporaryDirectory(prefix='typeahead-release-') as out:
        subprocess.run(
            ['bunx', 'wrangler', 'deploy', '-c', worker.config, '--dry-run', '--outdir', out, '--metafile'],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        bundled = json.loads((Path(out) / 'bundle-meta.json').read_text())['inputs']
    return repo_inputs(root, worker.config, list(bundled))


def slot_inputs(dockerignore: str) -> frozenset[str]:
    """The slot image's build context, which its dockerignore allowlists."""
    allowed = {line[1:].strip() for line in dockerignore.splitlines() if line.startswith('!')}
    return frozenset(allowed | SLOT_BUILD_FILES)


def touched(changed: frozenset[str], inputs: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(path for path in changed if any(path == item or path.startswith(item + '/') for item in inputs)))


def make_plan(root: Path, head: str, *, every_worker: bool) -> Plan:
    previous = previous_release(root)
    if previous is None:
        return Plan(head, None, (), WORKERS, ())
    commits = tuple(git(root, 'log', '--oneline', f'{previous}..HEAD').splitlines())
    changed = frozenset(git(root, 'diff', '--name-only', previous, 'HEAD').splitlines())
    workers = WORKERS if every_worker else tuple(
        worker for worker in WORKERS if touched(changed, bundle_inputs(root, worker))
    )
    slot_paths = touched(changed, slot_inputs((root / SLOT_DOCKERIGNORE).read_text()))
    return Plan(head, previous, commits, workers, slot_paths)


def describe(plan: Plan) -> str:
    lines = [f'commit   {plan.head[:12]}']
    if plan.previous is None:
        lines += ['previous none: every Worker deploys, and the slot image is not compared']
    else:
        lines += [f'previous {plan.previous} ({len(plan.commits)} commits since)']
        lines += [f'  {commit}' for commit in plan.commits]
    lines += ['deploy   ' + (', '.join(worker.name for worker in plan.workers) or 'no Worker changed')]
    if plan.slot_paths:
        lines += ['slots    image inputs changed; if the image differs, docs/operating.md#upgrade-the-slots']
        lines += [f'  {path}' for path in plan.slot_paths]
    return '\n'.join(lines)


def tag_message(version: str, plan: Plan) -> str:
    lines = [f'release {version}', '']
    lines += ['workers: ' + (', '.join(worker.name for worker in plan.workers) or 'none')]
    if plan.slot_paths:
        lines += ['slot image inputs changed']
    return '\n'.join(lines) + '\n'


def tag_and_push(root: Path, version: str, plan: Plan) -> None:
    git(root, 'tag', '--annotate', version, plan.head, '--message', tag_message(version, plan))
    git(root, 'push', 'origin', f'refs/tags/{version}')


def deploy(root: Path, worker: Worker) -> bool:
    return subprocess.run(['just', worker.recipe], cwd=root, check=False).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--plan', action='store_true', help='print the plan and stop')
    parser.add_argument('--all', action='store_true', help='deploy every Worker whatever changed')
    parser.add_argument('--yes', action='store_true', help='do not ask before deploying')
    args = parser.parse_args()

    root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    try:
        head = require_published(root)
        plan = make_plan(root, head, every_worker=args.all)
    except Refused as refusal:
        print(refusal, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as failure:
        print(f'{" ".join(failure.cmd)} failed:\n{failure.stderr}', file=sys.stderr)
        return 1
    print(describe(plan))
    if plan.empty:
        print('nothing to release: no Worker and no slot image input changed', file=sys.stderr)
        return 1
    if args.plan:
        return 0
    if not args.yes and input('deploy and tag? [y/N] ').strip().lower() != 'y':
        return 1

    for worker in plan.workers:
        print(f'--- {worker.name} ---')
        if not deploy(root, worker):
            print(f'{worker.name} failed; nothing is tagged. Fix it and run the release again.', file=sys.stderr)
            return 1

    version = datetime.now(UTC).strftime('%Y.%m%d.%H%M%S')
    tag_and_push(root, version, plan)
    print(f'released {version}')
    if plan.slot_paths:
        print('slot image inputs changed and no slot was deployed: docs/operating.md#upgrade-the-slots')
    return 0


if __name__ == '__main__':
    sys.exit(main())
