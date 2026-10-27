import http.server
import json
import threading
import time
import unittest

import qualify


class QualificationTest(unittest.TestCase):
    def setUp(self):
        self.paths = []
        self.empty = False
        self.delay = 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append(self.path)
                if self.path == '/__evaluation':
                    body, status = {'nonce': 'isolated-fixture-123', 'input_id': 'a' * 64, 'ready': True}, 200
                elif 'unavailable' in self.path:
                    body, status = {'error': 'PdsScopeUnavailable'}, 503
                else:
                    time.sleep(owner.delay)
                    body, status = {'actors': [] if owner.empty else [{'handle': 'fixture.test'}]}, 200
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.cases = [{'id': 'actor', 'path': '/search?q=fixture', 'status': 200,
                       'min_actors': 1, 'first_handle': 'fixture.test'},
                      {'id': 'unavailable', 'path': '/search?q=unavailable', 'status': 503,
                       'error': 'PdsScopeUnavailable'}]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_identity_refuses_public_targets_and_mismatched_nonce_before_workload(self):
        with self.assertRaises(ValueError):
            qualify.verify_target('https://typeahead.waow.tech', 'isolated-fixture-123', .2)
        self.assertEqual(self.paths, [])
        with self.assertRaises(ValueError):
            qualify.verify_target(self.base, 'wrong-fixture', .2)
        self.assertEqual(self.paths, ['/__evaluation'])
        with self.assertRaises(ValueError):
            qualify.verify_target(self.base, 'isolated-fixture-123', .2, 'b' * 64)
        self.assertEqual(self.paths, ['/__evaluation', '/__evaluation'])

    def test_valid_contracts_pass_and_fast_empty_success_cannot_buy_a_pass(self):
        qualify.verify_target(self.base, 'isolated-fixture-123', .2)
        reference = qualify.capture(self.base, self.cases, .2, rps=10)
        self.assertTrue(reference['passed'])
        rows = qualify.measure(self.base, self.cases, reference, 2, 2, 10, .2, 2)
        self.assertEqual(len(rows), 4)
        self.assertTrue(qualify.summarize(rows, 1000, 1000)['passed'])
        self.empty = True
        rows = qualify.measure(self.base, self.cases, reference, 2, 2, 10, .2, 2)
        summary = qualify.summarize(rows, 1000, 1000)
        self.assertEqual(summary['attempts'], 4)
        self.assertEqual(summary['failures'], 2)
        self.assertFalse(summary['passed'])

    def test_timeouts_remain_in_attempts_and_offered_load_latency(self):
        cases = self.cases[:1]
        reference = qualify.capture(self.base, cases, .2, rps=10)
        self.delay = .15
        rows = qualify.measure(self.base, cases, reference, 2, 1, 10, .03, 2)
        summary = qualify.summarize(rows, 1000, 1000)
        self.assertEqual(summary['attempts'], 2)
        self.assertEqual(summary['failures'], 2)
        self.assertGreaterEqual(summary['p50_ms'], 20)
        self.assertTrue(all(row['error'] for row in rows))
        self.assertFalse(summary['passed'])

    def test_capture_checks_nonempty_and_error_contracts(self):
        self.empty = True
        reference = qualify.capture(self.base, self.cases, .2, rps=10)
        self.assertFalse(reference['passed'])
        self.assertFalse(reference['attempts'][0]['correct'])
        self.assertTrue(reference['attempts'][1]['correct'])


if __name__ == '__main__':
    unittest.main()
