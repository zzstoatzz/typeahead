import errno
import json
import os
from pathlib import Path
import tempfile
import unittest

from recovery_fixture import create
from test_release_recovery import RunningRelease


@unittest.skipUnless(os.environ.get('TYPEAHEAD_TEST_RELEASE') and os.environ.get('TYPEAHEAD_TEST_TMPFS'),
                     'release executable and isolated small tmpfs required')
class Tests(unittest.TestCase):
    def test_full_activation_filesystem_fails_closed_and_recovers(self):
        mount = Path(os.environ['TYPEAHEAD_TEST_TMPFS']).resolve()
        self.assertTrue(os.path.ismount(mount))
        filesystem = os.statvfs(mount)
        self.assertLessEqual(filesystem.f_blocks * filesystem.f_frsize, 128 * 1024 * 1024)
        self.assertEqual(list(mount.iterdir()), [])
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory(dir=mount) as activation_dir:
            root = Path(temporary)
            create(root / 'fixtures')
            index = Path(activation_dir) / 'index'
            index.mkdir()
            (index / 'generations').symlink_to(root / 'fixtures/builds', target_is_directory=True)
            manifests = {name: json.loads((root / 'fixtures/builds' / name / 'manifest.json').read_text())
                         for name in ('compact', 'legacy')}
            activation = {'version': 1, 'current': {'manifest': manifests['compact'], 'location': 'generation'},
                          'previous': {'manifest': manifests['legacy'], 'location': 'generation'}}
            active = index / 'active.json'
            active.write_text(json.dumps(activation))
            original = active.read_bytes()
            server = RunningRelease(root, Path(activation_dir))
            filler = mount / 'fill'
            try:
                server.start()
                server.wait(lambda state: state['build_id'] == 'compact' and state['overlay_compact']['complete'])
                self.assertEqual(server.request('/admin/refresh', {
                    'did': 'did:plc:recovery', 'handle': 'recovery.test', 'hidden': 1, 'updated_at': 200,
                })[0], 200)
                server.crash()
                (root / 'fixtures/builds/compact/index.db.prefix').write_bytes(b'truncated')
                with filler.open('wb', buffering=0) as stream:
                    with self.assertRaises(OSError) as raised:
                        while True:
                            stream.write(bytes(64 * 1024))
                    self.assertEqual(raised.exception.errno, errno.ENOSPC)
                server.start()
                server.wait(lambda state: 'promote check failed' in server.log.read_text())
                self.assertFalse(server.request('/index/status')[1]['attached'])
                self.assertEqual(server.request('/search?q=a')[0], 503)
                self.assertEqual(active.read_bytes(), original)
                server.crash()
                filler.unlink()
                server.start()
                server.wait(lambda state: state['build_id'] == 'legacy' and state['overlay_compact']['complete'])
                self.assertEqual(server.request('/search?q=a')[1]['actors'], [])
                self.assertEqual(json.loads(active.read_text())['current']['manifest']['active_build'], 'legacy')
            finally:
                server.crash()
                filler.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
