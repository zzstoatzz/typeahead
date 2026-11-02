import tempfile
import threading
import time
from pathlib import Path
import unittest

from server import Canary, CanaryHTTPServer, Handler
import http.client
import json


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'state.json'
        self.now = 1000
        self.release = threading.Event()
        self.result = {'passed': True, 'equal': True}
        def evaluate(*args):
            self.release.wait(2)
            return dict(self.result)
        self.canary = Canary(Path('/binary'), Path('/overlay'), Path('/snapshot'),
                             self.state, 2000, evaluator=evaluate, clock=lambda: self.now)

    def tearDown(self):
        self.release.set()
        self.canary.pool.shutdown(wait=True)
        self.temp.cleanup()

    def finish(self):
        self.release.set()
        for _ in range(200):
            if not self.canary.status()['busy']:
                return
            time.sleep(.005)
        self.fail('Comparison did not finish')

    def test_http_auth_and_input_boundaries(self):
        server = CanaryHTTPServer(('::1', 0), Handler)
        server.token = 'x' * 40
        server.canary = self.canary
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def request(method, path, body=None, token=None):
            conn = http.client.HTTPConnection('::1', server.server_port, timeout=2)
            headers = {'Authorization': 'Bearer ' + token} if token else {}
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            code = response.status
            response.read()
            conn.close()
            return code
        try:
            self.assertEqual(request('GET', '/health'), 200)
            self.assertEqual(request('GET', '/status'), 401)
            self.assertEqual(request('GET', '/status', token='wrong'), 401)
            self.assertEqual(request('GET', '/status', token=server.token), 200)
            self.assertEqual(request('POST', '/control', {'enabled': True}, server.token), 200)
            self.assertEqual(request('POST', '/compare', {'q': 'x' * 257}, server.token), 400)
            self.assertEqual(request('POST', '/compare', {'q': 'bs'}, server.token), 202)
            self.assertEqual(request('POST', '/control', {'enabled': False}, server.token), 200)
            self.assertEqual(request('POST', '/compare', {'q': 'nate'}, server.token), 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_pause_resume_and_inflight_limit(self):
        self.assertEqual(self.canary.submit('first'), 503)
        self.assertTrue(self.canary.control(True))
        self.assertEqual(self.canary.submit('first'), 202)
        self.now += 20
        self.assertEqual(self.canary.submit('second'), 429)
        self.assertTrue(self.canary.control(False))
        self.finish()
        self.assertEqual(self.canary.submit('third'), 503)
        self.assertTrue(self.canary.control(True))
        self.assertEqual(self.canary.submit('third'), 202)

    def test_rate_limit(self):
        self.canary.control(True)
        self.assertEqual(self.canary.submit('first'), 202)
        self.finish()
        self.assertEqual(self.canary.submit('second'), 429)
        self.now += 15
        self.assertEqual(self.canary.submit('second'), 202)

    def test_failure_pauses_and_does_not_retain_query(self):
        self.result = {'passed': False, 'equal': False}
        self.canary.control(True)
        self.canary.submit('private query must not persist')
        self.finish()
        self.assertTrue(self.canary.status()['paused'])
        self.assertEqual(self.canary.status()['failed'], 1)
        self.assertNotIn('private query', self.state.read_text())

    def test_expiry_and_total_budget(self):
        self.now = 2000
        self.assertFalse(self.canary.control(True))
        self.assertEqual(self.canary.submit('no'), 503)
        self.now = 1000
        self.canary.state['accepted'] = 120
        self.assertFalse(self.canary.control(True))

    def test_restart_is_paused_and_preserves_budget(self):
        self.canary.control(True)
        self.canary.submit('first')
        self.finish()
        other = Canary(Path('/binary'), Path('/overlay'), Path('/snapshot'), self.state, 2000)
        try:
            self.assertTrue(other.status()['paused'])
            self.assertEqual(other.status()['accepted'], 1)
        finally:
            other.pool.shutdown()


if __name__ == '__main__':
    unittest.main()
