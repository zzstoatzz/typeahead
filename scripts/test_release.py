import contextlib
import io
import hashlib
import os
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import release

TRANSPORT, RENDERER, COLLECTOR, PUBLIC = release.WORKERS

DIGESTS = {worker.name: hashlib.sha256(worker.name.encode()).hexdigest() for worker in release.WORKERS}


class PureTest(unittest.TestCase):
    def test_latest_release_ignores_other_tags(self):
        tags = ['pds-compact-31ec142-20261020', '2026.1101.000000', '2026.0930.235959', 'v2026.1201.000000']
        self.assertEqual(release.latest_release(tags), '2026.1101.000000')
        self.assertIsNone(release.latest_release(['compact-candidate-cc2d5a1']))

    def test_digest_includes_paths_and_bytes_but_not_locations_and_timestamps(self):
        with tempfile.TemporaryDirectory() as scratch:
            original = Path(scratch) / 'original'
            original.mkdir()
            (original / 'config.json').write_text('{}')
            worker = original / 'workers/default'
            worker.mkdir(parents=True)
            (worker / 'worker.config.json').write_text('{}')
            bundle = worker / 'worker.js'
            bundle.write_text('export default {}')
            copied = Path(scratch) / 'copied'
            shutil.copytree(original, copied)
            os.utime(copied / 'workers/default/worker.js', (0, 0))
            digest = release.artifact_digest(original)
            self.assertEqual(release.artifact_digest(copied), digest)
            (worker / 'worker.config.json').write_text('{"previewUrls":false}')
            self.assertNotEqual(release.artifact_digest(original), digest)
            (worker / 'worker.config.json').write_text('{}')
            bundle.rename(worker / 'renamed.js')
            self.assertNotEqual(release.artifact_digest(original), digest)
            (worker / 'renamed.js').unlink()
            (worker / 'renamed.js').symlink_to(bundle)
            with self.assertRaisesRegex(release.Refused, 'unexpected file'):
                release.artifact_digest(original)

    def test_incomplete_build_output_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(release.Refused, 'complete Worker artifact'):
                release.artifact_digest(Path(scratch))

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

        self.digests = dict(DIGESTS)
        skip_install = mock.patch.object(release, 'prepare_build')
        skip_install.start()
        self.addCleanup(skip_install.stop)

        def build_workers(project, artifacts):
            (project / release.BUILD_OUTPUT).parent.mkdir(parents=True)
            for name, digest in self.digests.items():
                output = artifacts / name
                output.mkdir()
                (output / 'digest').write_text(digest)
            return self.digests

        builds = mock.patch.object(release, 'build_workers', build_workers)
        builds.start()
        self.addCleanup(builds.stop)

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

    def release_tag(self, version, *, digests=DIGESTS):
        message = '\n'.join(f'worker-build-v1 {name} {digest}' for name, digest in digests.items())
        release.git(self.root, 'tag', '--annotate', version, '--message', message or version)

    def plan(self, *, every_worker=False):
        head = release.git(self.root, 'rev-parse', 'HEAD')
        previous = release.previous_release(self.root)
        return release.make_plan(self.root, head, previous, self.digests, set(), every_worker=every_worker)

    def origin_tags(self):
        return release.git(self.origin, 'tag').splitlines()

    def run_main(self, *arguments, deploys):
        deployed = []

        def deploy(_root, worker, project, head):
            self.assertEqual(head, release.git(self.root, "rev-parse", "HEAD"))
            self.assertEqual((project / release.BUILD_OUTPUT / "digest").read_text(), self.digests[worker.name])
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
        self.digests[TRANSPORT.name] = 'a' * 64
        plan = self.plan()
        self.assertEqual(plan.previous, '2026.1101.000000')
        self.assertEqual(plan.workers, (TRANSPORT,))
        self.assertEqual(plan.slot_paths, ())
        self.assertEqual(len(plan.commits), 1)

    def test_workers_with_changed_digests_keep_deploy_order(self):
        self.release_tag('2026.1101.000000')
        self.commit({'src/shared.ts': '1\n'})
        self.digests[PUBLIC.name] = 'a' * 64
        self.digests[TRANSPORT.name] = 'b' * 64
        self.assertEqual(self.plan().workers, (TRANSPORT, PUBLIC))

    def test_measures_from_the_newest_release_tag(self):
        self.release_tag('2026.1101.000000')
        self.commit({'src/eval/collector.ts': '1\n'})
        self.digests[COLLECTOR.name] = 'a' * 64
        self.release_tag('2026.1102.000000', digests=self.digests)
        self.commit({'src/image-renderer/og.ts': '1\n'})
        self.digests[RENDERER.name] = 'b' * 64
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
        self.assertEqual(release.released_digests(self.origin, version), self.digests)

    def test_legacy_release_tag_bootstraps_all_digests(self):
        self.release_tag('2026.1101.000000', digests={})
        self.assertEqual(self.plan().workers, release.WORKERS)

    def test_incomplete_digest_manifest_is_not_accepted(self):
        self.release_tag('2026.1101.000000', digests={PUBLIC.name: DIGESTS[PUBLIC.name]})
        with self.assertRaisesRegex(release.Refused, 'incomplete Worker build digests'):
            self.plan()

    def test_a_source_change_with_the_same_artifact_does_not_deploy(self):
        self.release_tag('2026.1101.000000')
        self.commit({'src/index.ts': '2\n'})
        self.assertTrue(self.plan().empty)
        self.assertEqual(self.plan(every_worker=True).workers, release.WORKERS)

    def test_retry_redeploys_a_worker_attempted_since_the_baseline_even_after_revert(self):
        self.release_tag('2026.1101.000000')
        self.digests[TRANSPORT.name] = 'a' * 64
        self.digests[RENDERER.name] = 'b' * 64
        status, deployed, _ = self.run_main('--yes', deploys=lambda worker: worker is not RENDERER)
        self.assertEqual((status, deployed), (1, [TRANSPORT.name, RENDERER.name]))
        self.digests.update(DIGESTS)
        status, deployed, _ = self.run_main('--yes', deploys=lambda _worker: True)
        self.assertEqual((status, deployed), (0, [TRANSPORT.name, RENDERER.name]))
        self.assertFalse((self.root / '.git/typeahead-release-pending.json').exists())

    def test_a_rejected_tag_push_leaves_the_previous_baseline(self):
        hooks = self.origin / 'hooks'
        hook = hooks / 'pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        status, deployed, output = self.run_main('--yes', deploys=lambda _worker: True)
        self.assertEqual(status, 1)
        self.assertEqual(deployed, [worker.name for worker in release.WORKERS])
        self.assertIn('remote rejected', output)
        self.assertIsNone(release.previous_release(self.root))
        self.assertTrue((self.root / '.git/typeahead-release-pending.json').exists())

    def test_main_advancing_during_smoke_stops_the_release_untagged(self):
        def deploy(worker):
            if worker is PUBLIC:
                self.commit({'src/index.ts': '2\n'})
            return True

        status, deployed, output = self.run_main('--yes', deploys=deploy)
        self.assertEqual(status, 1)
        self.assertEqual(deployed, [worker.name for worker in release.WORKERS])
        self.assertIn('changed during deployment', output)
        self.assertEqual(self.origin_tags(), [])

    def test_declining_the_prompt_deploys_nothing(self):
        with mock.patch('builtins.input', return_value=''):
            status, deployed, _ = self.run_main(deploys=lambda _worker: True)
        self.assertEqual((status, deployed), (1, []))
        self.assertEqual(self.origin_tags(), [])


if __name__ == '__main__':
    unittest.main()
