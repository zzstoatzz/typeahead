import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from recovery_fixture import create


class RunningRelease:
    def __init__(self, root, index_root=None):
        self.root = root
        self.index_root = root if index_root is None else index_root
        self.process = None
        self.serial = 0

    def start(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.serial += 1
        self.log = self.root / f'process-{self.serial}.log'
        env = {k: v for k, v in os.environ.items() if not k.startswith('INDEX_R2_')}
        env.update(MODE='search', INDEX_BUILD_ROOT=str(self.index_root),
                   LOCAL_DB_PATH=str(self.root / 'overlay.db'), SQLITE_TMPDIR=str(self.root / 'tmp'), PORT=str(self.port),
                   INDEX_POLL_SECS='86400', TURSO_URL='127.0.0.1:1',
                   TURSO_AUTH_TOKEN='isolated-no-access', ADMIN_SECRET='local-recovery-test')
        with self.log.open('w') as log:
            self.process = subprocess.Popen([os.environ['TYPEAHEAD_TEST_RELEASE']], env=env, stdout=log, stderr=log)

    def request(self, path, body=None):
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}' + path,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={'Authorization': 'Bearer local-recovery-test',
                                                  'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def wait(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(self.log.read_text())
            try:
                if predicate(self.request('/index/status')[1]):
                    return
            except OSError:
                pass
            time.sleep(.05)
        raise AssertionError(self.log.read_text())

    def crash(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)


@unittest.skipUnless(os.environ.get('TYPEAHEAD_TEST_RELEASE'), 'release executable required')
class Tests(unittest.TestCase):
    def test_corrupt_current_and_modified_legacy_twin_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create(root / 'fixtures')
            index = root / 'index'
            index.mkdir()
            (index / 'generations').symlink_to(root / 'fixtures/builds', target_is_directory=True)
            manifests = {name: json.loads((root / 'fixtures/builds' / name / 'manifest.json').read_text())
                         for name in ('compact', 'legacy')}
            active = index / 'active.json'
            active.write_text(json.dumps({'version': 1,
                'current': {'manifest': manifests['compact'], 'location': 'generation'},
                'previous': {'manifest': manifests['legacy'], 'location': 'generation'}}))
            original = active.read_bytes()
            (root / 'fixtures/builds/compact/index.db.prefix').write_bytes(b'truncated')
            with sqlite3.connect(root / 'fixtures/builds/legacy/index.db') as connection:
                connection.execute("INSERT INTO index_meta VALUES('untrusted-change','still valid SQLite')")
            server = RunningRelease(root)
            try:
                server.start()
                server.wait(lambda state: 'promote check failed' in server.log.read_text())
                self.assertFalse(server.request('/index/status')[1]['attached'])
                self.assertEqual(server.request('/search?q=a')[0], 503)
                self.assertEqual(active.read_bytes(), original)
            finally:
                server.crash()

    @unittest.skipUnless(shutil.which('rclone'), 'real rclone transport required')
    def test_restart_removes_interrupted_generation_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create(root / 'fixtures')
            index = root / 'index'
            index.mkdir()
            (index / 'generations').symlink_to(root / 'fixtures/builds', target_is_directory=True)
            manifest = json.loads((root / 'fixtures/builds/legacy/manifest.json').read_text())
            active = index / 'active.json'
            active.write_text(json.dumps({'version': 1, 'current': {'manifest': manifest, 'location': 'generation'}}))
            original = active.read_bytes()
            staging = index / '.staging-interrupted'
            staging.mkdir()
            source = root / 'download-source'
            source.write_bytes(b'partial snapshot' * (1024 * 1024))
            with (root / 'rclone.log').open('w') as log:
                copy = subprocess.Popen(['rclone', '--config', os.devnull, 'copyto', '--bwlimit', '1M',
                                         '--multi-thread-streams', '0', '--local-no-preallocate',
                                         str(source), str(staging / 'index.db')], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    self.assertIsNone(copy.poll(), (root / 'rclone.log').read_text())
                    if any(0 < path.stat().st_size < source.stat().st_size for path in staging.glob('index.db*')):
                        break
                    time.sleep(.02)
                else:
                    self.fail('rclone did not create a partial download')
            finally:
                copy.kill()
                copy.wait(timeout=5)
            self.assertTrue(any(staging.iterdir()))
            server = RunningRelease(root)
            try:
                server.start()
                server.wait(lambda state: state['build_id'] == 'legacy' and state['overlay_compact']['complete'])
                self.assertEqual(server.request('/search?q=a')[1]['actors'][0]['did'], 'did:plc:recovery')
                self.assertFalse(staging.exists())
                self.assertEqual(active.read_bytes(), original)
            finally:
                server.crash()

    def test_crash_fallback_preserves_newer_hide_and_prunes_only_covered_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create(root / 'fixtures')
            index = root / 'index'
            index.mkdir()
            (index / 'generations').symlink_to(root / 'fixtures/builds', target_is_directory=True)
            manifests = {v: json.loads((root / 'fixtures/builds' / v / 'manifest.json').read_text())
                         for v in ('legacy', 'compact')}
            activation = {'version': 1, 'current': {'manifest': manifests['compact'], 'location': 'generation'},
                          'previous': {'manifest': manifests['legacy'], 'location': 'generation'}}
            active = index / 'active.json'
            active.write_text(json.dumps(activation))
            server = RunningRelease(root)
            try:
                server.start()
                server.wait(lambda s: s['build_id'] == 'compact' and s['overlay_compact']['complete'])
                self.assertEqual(server.request('/search?q=a')[1]['actors'][0]['did'], 'did:plc:recovery')
                status, _ = server.request('/admin/refresh', {
                    'did': 'did:plc:recovery', 'handle': 'recovery.test', 'hidden': 1, 'updated_at': 200,
                })
                self.assertEqual(status, 200)
                self.assertEqual(server.request('/search?q=a')[1]['actors'], [])
                server.crash()
                with sqlite3.connect(root / 'overlay.db') as connection:
                    connection.execute("INSERT INTO actor_overlay(did,blob,hidden,updated_at,source) VALUES('covered',X'00',0,99,0)")
                (index / 'active.json.part').write_text('{unfinished')
                (root / 'fixtures/builds/compact/index.db.prefix').write_bytes(b'truncated')
                server.start()
                server.wait(lambda s: s['build_id'] == 'legacy' and s['overlay_compact']['complete'])
                self.assertEqual(server.request('/search?q=a')[1]['actors'], [])
                self.assertEqual(json.loads(active.read_text())['current']['manifest']['active_build'], 'legacy')
                server.crash()
                with sqlite3.connect(root / 'overlay.db') as connection:
                    self.assertEqual(connection.execute('SELECT did,hidden,updated_at FROM actor_overlay').fetchall(),
                                     [('did:plc:recovery', 1, 200)])
                activation['current']['manifest'] = dict(manifests['legacy'], active_build='future',
                                                         snapshot_url='builds/future/index.db', source_watermark=101)
                active.write_text(json.dumps(activation))
                server.start()
                server.wait(lambda s: 'RollbackRequiresReplay' in server.log.read_text())
                self.assertFalse(server.request('/index/status')[1]['attached'])
                self.assertEqual(server.request('/search?q=a')[0], 503)
            finally:
                server.crash()


if __name__ == '__main__':
    unittest.main()
