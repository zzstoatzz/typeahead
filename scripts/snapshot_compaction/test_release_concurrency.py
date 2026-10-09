import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from recovery_fixture import create
from test_release_recovery import RunningRelease


@unittest.skipUnless(os.environ.get('TYPEAHEAD_TEST_RELEASE'), 'release executable required')
class Tests(unittest.TestCase):
    def test_eight_persistent_clients_with_mixed_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create(root / 'fixtures')
            index = root / 'index'
            index.mkdir()
            (index / 'generations').symlink_to(root / 'fixtures/builds', target_is_directory=True)
            manifest = json.loads((root / 'fixtures/builds/legacy/manifest.json').read_text())
            (index / 'active.json').write_text(json.dumps({
                'version': 1, 'current': {'manifest': manifest, 'location': 'generation'},
            }))
            server = RunningRelease(root)
            connections = []
            local = threading.local()
            barrier = threading.Barrier(8)

            def request(writer):
                if not hasattr(local, 'connection'):
                    local.connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=10)
                    connections.append(local.connection)
                barrier.wait(timeout=10)
                body = json.dumps({'did': 'did:plc:recovery', 'handle': 'recovery.test',
                                   'display_name': 'Recovery', 'quality_score': 1, 'updated_at': 200}) if writer else None
                local.connection.request('POST' if writer else 'GET', '/admin/refresh' if writer else '/search?q=a',
                                         body=body, headers={'Authorization': 'Bearer local-recovery-test',
                                                             'Content-Type': 'application/json'})
                response = local.connection.getresponse()
                payload = response.read()
                self.assertEqual(response.status, 200, payload)
                return json.loads(payload)

            try:
                server.start()
                server.wait(lambda s: s['attached'] and 'incremental: done' in server.log.read_text())
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    for _ in range(5):
                        started = time.monotonic()
                        list(pool.map(request, [False] * 7 + [True]))
                        self.assertLess(time.monotonic() - started, 2, 'clients waited for another connection to time out')
            finally:
                for connection in connections:
                    connection.close()
                server.crash()


if __name__ == '__main__':
    unittest.main()
