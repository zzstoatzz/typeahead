import contextlib
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from scripts import smoke


class DeprecatedParameterFailureTest(unittest.TestCase):
    def check_responses(self, responses, expected_failures):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlsplit(self.path).query)
                status, actors = responses['q' if 'q' in params else 'term']
                body = json.dumps({'actors': actors}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            previous_failures = smoke.failures
            try:
                smoke.failures = 0
                with contextlib.redirect_stdout(io.StringIO()):
                    smoke.test_deprecated_param(f'http://127.0.0.1:{server.server_port}')
                self.assertEqual(smoke.failures, expected_failures)
            finally:
                smoke.failures = previous_failures
                server.shutdown()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_real_http_failures_and_false_empty_answers(self):
        actor = {'did': 'did:plc:fixture'}
        cases = [
            ({'q': (200, [actor]), 'term': (200, [actor])}, 0),
            ({'q': (503, []), 'term': (503, [])}, 1),
            ({'q': (503, []), 'term': (200, [actor])}, 1),
            ({'q': (200, []), 'term': (200, [actor])}, 1),
            ({'q': (200, []), 'term': (200, [])}, 1),
        ]
        for responses, expected in cases:
            with self.subTest(responses=responses):
                self.check_responses(responses, expected)
