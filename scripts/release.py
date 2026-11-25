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
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RELEASE = re.compile(r'\d{4}\.\d{4}\.\d{6}')

DIGEST_LINE = re.compile(r'worker-build-v1 ([a-z-]+) ([0-9a-f]{64})')
BUILD_OUTPUT = Path('.cloudflare/output/v1')

SLOT_DOCKERIGNORE = 'deploy/slot/Dockerfile.dockerignore'
SLOT_BUILD_FILES = frozenset({'deploy/slot/Dockerfile', SLOT_DOCKERIGNORE, 'deploy/slot/fly.toml'})


@dataclass(frozen=True)
class Worker:
    name: str
    mode: str | None
    recipe: str


# Deploy order: the transport and the renderer go before the public Worker
# that binds them.
WORKERS = (
    Worker('typeahead-search-transport', 'transport', 'deploy-search-transport'),
    Worker('typeahead-image-renderer', 'renderer', 'deploy-image-renderer'),
    Worker('typeahead-eval-collector', 'collector', 'deploy-eval-collector'),
    Worker('typeahead', None, 'deploy-worker'),
)


@dataclass(frozen=True)
class Plan:
    head: str
    previous: str | None
    commits: tuple[str, ...]
    workers: tuple[Worker, ...]
    digests: dict[str, str]
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


def artifact_digest(output: Path) -> str:
    if not (output / 'config.json').is_file() or not (output / 'workers/default/worker.config.json').is_file():
        raise Refused('cf build did not produce a complete Worker artifact')
    files: dict[str, str] = {}
    for path in sorted(output.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            with path.open('rb') as file:
                files[path.relative_to(output).as_posix()] = hashlib.file_digest(file, 'sha256').hexdigest()
        elif not stat.S_ISDIR(mode):
            raise Refused(f'unexpected file in cf Build Output: {path.relative_to(output)}')
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def prepare_build(root: Path, head: str, project: Path) -> None:
    archive = project.parent / 'source.tar'
    git(root, 'archive', f'--output={archive}', head)
    subprocess.run(['tar', '-xf', archive, '-C', project], check=True, capture_output=True, text=True)
    archive.unlink()
    subprocess.run(['bun', 'install', '--frozen-lockfile'], cwd=project, check=True, capture_output=True, text=True)


def build_workers(project: Path, artifacts: Path) -> dict[str, str]:
    digests = {}
    for worker in WORKERS:
        print(f'building {worker.name}')
        cmd = [str(project / 'node_modules/.bin/cf'), 'build']
        if worker.mode:
            cmd += ['--mode', worker.mode]
        subprocess.run(cmd, cwd=project, check=True, capture_output=True, text=True)
        output = project / BUILD_OUTPUT
        digests[worker.name] = artifact_digest(output)
        output.rename(artifacts / worker.name)
    return digests


def released_digests(root: Path, previous: str | None) -> dict[str, str]:
    if previous is None:
        return {}
    message = git(root, 'for-each-ref', '--format=%(contents)', f'refs/tags/{previous}')
    lines = [line for line in message.splitlines() if line.startswith('worker-build-')]
    if not lines:
        return {}
    digests = {}
    for line in lines:
        match = DIGEST_LINE.fullmatch(line)
        if match is None or match[1] in digests:
            raise Refused(f'invalid Worker build digests on release tag {previous}')
        digests[match[1]] = match[2]
    if set(digests) != {worker.name for worker in WORKERS}:
        raise Refused(f'incomplete Worker build digests on release tag {previous}')
    return digests


def slot_inputs(dockerignore: str) -> frozenset[str]:
    """The slot image's build context, which its dockerignore allowlists."""
    allowed = {line[1:].strip() for line in dockerignore.splitlines() if line.startswith('!')}
    return frozenset(allowed | SLOT_BUILD_FILES)


def touched(changed: frozenset[str], inputs: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(path for path in changed if any(path == item or path.startswith(item + '/') for item in inputs)))


def make_plan(
    root: Path, head: str, previous: str | None, digests: dict[str, str],
    pending: set[str], *, every_worker: bool,
) -> Plan:
    released = released_digests(root, previous)
    workers = tuple(
        worker for worker in WORKERS
        if every_worker or worker.name in pending or digests[worker.name] != released.get(worker.name)
    )
    if previous is None:
        return Plan(head, None, (), workers, digests, ())
    commits = tuple(git(root, 'log', '--oneline', f'{previous}..{head}').splitlines())
    changed = frozenset(git(root, 'diff', '--name-only', '--no-renames', previous, head).splitlines())
    slot_paths = touched(changed, slot_inputs(git(root, 'show', f'{head}:{SLOT_DOCKERIGNORE}')))
    return Plan(head, previous, commits, workers, digests, slot_paths)


def pending_workers(path: Path, previous: str | None) -> set[str]:
    if not path.exists():
        return set()
    pending = json.loads(path.read_text())
    return set(pending['workers']) if pending['previous'] == previous else set()


def record_attempt(path: Path, previous: str | None, pending: set[str]) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'previous': previous, 'workers': sorted(pending)}) + '\n')
    temporary.replace(path)


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
    lines += ['', *(f'worker-build-v1 {name} {digest}' for name, digest in sorted(plan.digests.items()))]
    return '\n'.join(lines) + '\n'


def tag_and_push(root: Path, version: str, plan: Plan) -> None:
    ref = f'refs/tags/{version}'
    git(root, 'tag', '--annotate', version, plan.head, '--message', tag_message(version, plan))
    tag = git(root, 'rev-parse', ref)
    try:
        git(root, 'push', 'origin', ref)
    except subprocess.CalledProcessError:
        git(root, 'update-ref', '-d', ref, tag)
        raise


def deploy(root: Path, worker: Worker, project: Path, head: str) -> bool:
    env = {**os.environ, 'TYPEAHEAD_RELEASE_PROJECT': str(project), 'TYPEAHEAD_RELEASE_HEAD': head}
    return subprocess.run(['just', worker.recipe], cwd=root, env=env, check=False).returncode == 0


def release(root: Path, args: argparse.Namespace, state: Path, scratch: Path) -> int:
    head = require_published(root)
    previous = previous_release(root)
    pending = pending_workers(state, previous)
    project = scratch / 'project'
    artifacts = scratch / 'artifacts'
    project.mkdir()
    artifacts.mkdir()
    prepare_build(root, head, project)
    digests = build_workers(project, artifacts)
    plan = make_plan(root, head, previous, digests, pending, every_worker=args.all)
    print(describe(plan))
    if plan.empty:
        print('nothing to release: no Worker and no slot image input changed', file=sys.stderr)
        return 1
    if args.plan:
        return 0
    if not args.yes and input('deploy and tag? [y/N] ').strip().lower() != 'y':
        return 1

    if require_published(root) != head or previous_release(root) != previous:
        raise Refused('the published commit or release tag changed during planning; rerun the release')
    for worker in plan.workers:
        print(f'--- {worker.name} ---')
        pending.add(worker.name)
        record_attempt(state, previous, pending)
        (artifacts / worker.name).rename(project / BUILD_OUTPUT)
        if not deploy(root, worker, project, head):
            print(f'{worker.name} failed; nothing is tagged. Fix it and run the release again.', file=sys.stderr)
            return 1
        (project / BUILD_OUTPUT).rename(artifacts / worker.name)

    if require_published(root) != head or previous_release(root) != previous:
        raise Refused('the published commit or release tag changed during deployment; rerun the release')
    version = datetime.now(UTC).strftime('%Y.%m%d.%H%M%S')
    tag_and_push(root, version, plan)
    state.unlink(missing_ok=True)
    print(f'released {version}')
    if plan.slot_paths:
        print('slot image inputs changed and no slot was deployed: docs/operating.md#upgrade-the-slots')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--plan', action='store_true', help='print the plan and stop')
    parser.add_argument('--all', action='store_true', help='deploy every Worker whatever changed')
    parser.add_argument('--yes', action='store_true', help='do not ask before deploying')
    args = parser.parse_args()

    root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel'))
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    try:
        with (common / 'typeahead-release.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refused('a release is already running in this repository') from error
            with tempfile.TemporaryDirectory(prefix='typeahead-release-') as scratch:
                return release(root, args, common / 'typeahead-release-pending.json', Path(scratch))
    except (Refused, OSError, ValueError) as failure:
        print(failure, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as failure:
        print(f'{" ".join(failure.cmd)} failed:\n{failure.stderr}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
