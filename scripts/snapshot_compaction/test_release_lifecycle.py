import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request

from recovery_fixture import create


@unittest.skipUnless(os.environ.get('TYPEAHEAD_TEST_RELEASE'), 'release executable required')
class Tests(unittest.TestCase):
    def test_sigterm_before_and_after_snapshot_admission(self):
        binary = str(Path(os.environ['TYPEAHEAD_TEST_RELEASE']).resolve())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create(root / 'fixtures')
            for variant in (None, 'legacy', 'compact', 'legacy-stalled'):
                with self.subTest(variant=variant):
                    work = root / (variant or 'unready')
                    index = work / 'index'
                    index.mkdir(parents=True)
                    if variant:
                        (index / 'generations').symlink_to(root / 'fixtures/builds', target_is_directory=True)
                        manifest = json.loads((root / 'fixtures/builds' / variant.split('-')[0] / 'manifest.json').read_text())
                        (index / 'active.json').write_text(json.dumps({
                            'version': 1, 'current': {'manifest': manifest, 'location': 'generation'},
                        }))
                    with socket.socket() as listener:
                        listener.bind(('127.0.0.1', 0))
                        port = listener.getsockname()[1]
                    upstream = socket.socket()
                    upstream.bind(('127.0.0.1', 0))
                    upstream.listen()
                    upstream.settimeout(5)
                    stalled = variant == 'legacy-stalled'
                    accepted = None
                    env = {k: v for k, v in os.environ.items() if not k.startswith('INDEX_R2_')}
                    env.update(MODE='search', INDEX_BUILD_ROOT=str(work), LOCAL_DB_PATH=str(work / 'overlay.db'),
                               PORT=str(port), INDEX_POLL_SECS='86400', TURSO_URL='127.0.0.1:1',
                               TURSO_AUTH_TOKEN='isolated-no-access')
                    if stalled:
                        env['TURSO_URL'] = f'127.0.0.1:{upstream.getsockname()[1]}'
                    log_path = work / 'server.log'
                    with log_path.open('w') as log:
                        process = subprocess.Popen([binary], env=env, stdout=log, stderr=log)
                    try:
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            self.assertIsNone(process.poll(), log_path.read_text())
                            try:
                                with urllib.request.urlopen(f'http://127.0.0.1:{port}/index/status', timeout=1) as response:
                                    status = json.load(response)
                                if status['attached'] == bool(variant):
                                    if not variant or stalled or 'incremental: done' in log_path.read_text():
                                        break
                            except OSError:
                                pass
                            time.sleep(.05)
                        else:
                            self.fail(log_path.read_text())
                        if stalled:
                            accepted, _ = upstream.accept()
                        process.terminate()
                        try:
                            self.assertEqual(process.wait(timeout=5), 0, log_path.read_text())
                        except subprocess.TimeoutExpired:
                            self.fail('SIGTERM did not drain background tasks: ' + log_path.read_text())
                    finally:
                        if accepted is not None:
                            accepted.close()
                        upstream.close()
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
