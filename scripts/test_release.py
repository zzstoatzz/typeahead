import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import release

TRANSPORT, RENDERER, COLLECTOR, PUBLIC = release.WORKERS

INPUTS = {
    TRANSPORT.name: frozenset({TRANSPORT.config, 'src/transport', 'src/shared.ts'}),
    RENDERER.name: frozenset({RENDERER.config, 'src/image-renderer'}),
    COLLECTOR.name: frozenset({COLLECTOR.config, 'src/eval'}),
    PUBLIC.name: frozenset({PUBLIC.config, 'src/index.ts', 'src/shared.ts'}),
}


class PureTest(unittest.TestCase):
    def test_latest_release_ignores_other_tags(self):
        tags = ['pds-compact-31ec142-20261020', '2026.1101.000000', '2026.0930.235959', 'v2026.1201.000000']
        self.assertEqual(release.latest_release(tags), '2026.1101.000000')
        self.assertIsNone(release.latest_release(['compact-candidate-cc2d5a1']))

    def test_repo_inputs_are_relative_to_the_repository(self):
        root = Path('/repo')
        bundled = [
            '../../src/transport/search-transport.ts',
            '../../src/worker-tracing.ts',
            '../../node_modules/zod/index.js',
            '../../../elsewhere/node_modules/zod/index.js',
        ]
        self.assertEqual(
            release.repo_inputs(root, 'deploy/search-transport/wrangler.jsonc', bundled),
            {
                'deploy/search-transport/wrangler.jsonc',
                'src/transport/search-transport.ts',
                'src/worker-tracing.ts',
                'package.json',
                'bun.lock',
            },
        )

    def test_slot_inputs_come_from_the_dockerignore_allowlist(self):
        inputs = release.slot_inputs('*\n!deploy/slot/supervise\n!services/src\n')
        self.assertLessEqual({'deploy/slot/supervise', 'services/src', 'deploy/slot/Dockerfile'}, inputs)
        self.assertNotIn('*', inputs)

    def test_touched_matches_files_and_directories_only(self):
        changed = frozenset({'services/src/store.zig', 'services/srcx/other.zig', 'src/moderation.ts'})
        self.assertEqual(release.touched(changed, frozenset({'services/src'})), ('services/src/store.zig',))


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='typeahead-release-test-')
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        # The calling repository's Git environment must not reach these subprocesses.
        environment = {
            'PATH': os.environ['PATH'],
            'HOME': str(self.scratch),
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_AUTHOR_NAME': 'test',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'test',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        }
        patched = mock.patch.dict(os.environ, environment, clear=True)
        patched.start()
        self.addCleanup(patched.stop)

        self.origin = self.scratch / 'origin.git'
        self.root = self.scratch / 'checkout'
        subprocess.run(['git', 'init', '--quiet', '--bare', '--initial-branch=main', str(self.origin)], check=True)
        subprocess.run(['git', 'clone', '--quiet', str(self.origin), str(self.root)], check=True, capture_output=True)
        release.git(self.root, 'checkout', '--quiet', '-b', 'main')
        self.commit({release.SLOT_DOCKERIGNORE: '*\n!services/src\n', 'src/index.ts': '1\n'})

        inputs = mock.patch.object(release, 'bundle_inputs', lambda _root, worker: INPUTS[worker.name])
        inputs.start()
        self.addCleanup(inputs.stop)

    def commit(self, files, *, push=True):
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        release.git(self.root, 'add', '--all')
        release.git(self.root, 'commit', '--quiet', '--message', 'change')
        if push:
            release.git(self.root, 'push', '--quiet', 'origin', 'main')
        return release.git(self.root, 'rev-parse', 'HEAD')

    def release_tag(self, version):
        release.git(self.root, 'tag', '--annotate', version, '--message', version)

    def plan(self, **options):
        return release.make_plan(self.root, release.git(self.root, 'rev-parse', 'HEAD'), every_worker=False, **options)

    def origin_tags(self):
        return release.git(self.origin, 'tag').splitlines()

    def run_main(self, *arguments, deploys):
        deployed = []

        def deploy(_root, worker):
            deployed.append(worker.name)
            return deploys(worker)

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with (
            mock.patch.object(release, 'deploy', deploy),
            mock.patch('sys.argv', ['release', *arguments]),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            status = release.main()
        return status, deployed, out.getvalue() + err.getvalue()

    def test_refuses_uncommitted_changes(self):
        (self.root / 'src/index.ts').write_text('2\n')
        with self.assertRaisesRegex(release.Refused, 'uncommitted'):
            release.require_published(self.root)

    def test_refuses_an_unpushed_commit(self):
        self.commit({'src/index.ts': '2\n'}, push=False)
        with self.assertRaisesRegex(release.Refused, 'not origin/main'):
            release.require_published(self.root)

    def test_accepts_origin_main_with_untracked_files(self):
        (self.root / 'scratch.txt').write_text('untracked\n')
        self.assertEqual(release.require_published(self.root), release.git(self.root, 'rev-parse', 'origin/main'))

    def test_first_release_deploys_every_worker(self):
        plan = self.plan()
        self.assertIsNone(plan.previous)
        self.assertEqual(plan.workers, release.WORKERS)

    def test_deploys_only_workers_whose_inputs_changed(self):
        self.release_tag('2026.1101.000000')
        self.commit({'src/transport/route-cache.ts': '1\n'})
        plan = self.plan()
        self.assertEqual(plan.previous, '2026.1101.000000')
        self.assertEqual(plan.workers, (TRANSPORT,))
        self.assertEqual(plan.slot_paths, ())
        self.assertEqual(len(plan.commits), 1)

    def test_a_shared_module_deploys_each_worker_that_bundles_it_in_order(self):
        self.release_tag('2026.1101.000000')
        self.commit({'src/shared.ts': '1\n'})
        self.assertEqual(self.plan().workers, (TRANSPORT, PUBLIC))

    def test_measures_from_the_newest_release_tag(self):
        self.release_tag('2026.1101.000000')
        self.commit({'src/eval/collector.ts': '1\n'})
        self.release_tag('2026.1102.000000')
        self.commit({'src/image-renderer/og.ts': '1\n'})
        self.assertEqual(self.plan().workers, (RENDERER,))

    def test_reports_slot_image_inputs_without_a_worker(self):
        self.release_tag('2026.1101.000000')
        self.commit({'services/src/store.zig': '1\n'})
        plan = self.plan()
        self.assertEqual(plan.workers, ())
        self.assertEqual(plan.slot_paths, ('services/src/store.zig',))
        self.assertFalse(plan.empty)

    def test_documentation_alone_is_nothing_to_release(self):
        self.release_tag('2026.1101.000000')
        self.commit({'docs/operating.md': '1\n'})
        status, deployed, output = self.run_main('--yes', deploys=lambda _worker: True)
        self.assertEqual((status, deployed), (1, []))
        self.assertIn('nothing to release', output)
        self.assertEqual(self.origin_tags(), [])

    def test_plan_deploys_and_tags_nothing(self):
        status, deployed, _ = self.run_main('--plan', deploys=lambda _worker: True)
        self.assertEqual((status, deployed), (0, []))
        self.assertEqual(self.origin_tags(), [])

    def test_a_failed_deploy_stops_the_release_untagged(self):
        status, deployed, output = self.run_main('--yes', deploys=lambda worker: worker is not RENDERER)
        self.assertEqual(status, 1)
        self.assertEqual(deployed, [TRANSPORT.name, RENDERER.name])
        self.assertIn('nothing is tagged', output)
        self.assertEqual(release.git(self.root, 'tag'), '')
        self.assertEqual(self.origin_tags(), [])

    def test_release_tags_the_deployed_commit_on_origin(self):
        head = release.git(self.root, 'rev-parse', 'HEAD')
        status, deployed, _ = self.run_main('--yes', deploys=lambda _worker: True)
        self.assertEqual(status, 0)
        self.assertEqual(deployed, [worker.name for worker in release.WORKERS])
        (version,) = self.origin_tags()
        self.assertRegex(version, release.RELEASE)
        self.assertEqual(release.git(self.origin, 'rev-parse', f'{version}^{{commit}}'), head)
        self.assertEqual(release.git(self.origin, 'cat-file', '-t', version), 'tag')
        self.assertEqual(release.previous_release(self.root), version)

    def test_declining_the_prompt_deploys_nothing(self):
        with mock.patch('builtins.input', return_value=''):
            status, deployed, _ = self.run_main(deploys=lambda _worker: True)
        self.assertEqual((status, deployed), (1, []))
        self.assertEqual(self.origin_tags(), [])


if __name__ == '__main__':
    unittest.main()
