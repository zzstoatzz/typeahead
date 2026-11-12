import contextlib
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from scripts import smoke

SECRET = 'admin-secret'


class OverrideLifecycleTest(unittest.TestCase):
    """The override lifecycle against a stand-in for the admin API: it must
    never write or clear the real override, and must clear its own whatever
    happens on the way."""

    def run_against(self, fail_on=None, link_present=True):
        overrides = {smoke.LINK_DID: 'show'} if link_present else {}
        writes = []

        class Handler(BaseHTTPRequestHandler):
            def answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                rows = [{'did': did, 'action': action, 'reason': '', 'created_at': 1} for did, action in overrides.items()]
                self.answer(200, {'overrides': rows})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                did, action = body.get('did'), body.get('action')
                if action not in ('show', 'hide') or not str(did).startswith('did:'):
                    return self.answer(400, {'error': 'bad request'})
                writes.append(('set', did, action))
                if fail_on == action:
                    return self.answer(500, {'error': 'boom'})
                overrides[did] = action
                self.answer(200, {'did': did, 'action': action, 'hidden': 0 if action == 'show' else 1})

            def do_DELETE(self):
                did = parse_qs(urlsplit(self.path).query)['did'][0]
                writes.append(('clear', did, None))
                overrides.pop(did, None)
                self.answer(200, {'did': did, 'hidden': 0})

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            previous = smoke.failures
            try:
                smoke.failures = 0
                with contextlib.redirect_stdout(io.StringIO()):
                    smoke.test_mod_overrides(f'http://127.0.0.1:{server.server_port}', SECRET)
                return smoke.failures, writes, overrides
            finally:
                smoke.failures = previous
                server.shutdown()
                thread.join(timeout=2)

    def test_the_lifecycle_touches_only_its_own_did_and_leaves_nothing(self):
        failures, writes, overrides = self.run_against()
        self.assertEqual(failures, 0)
        self.assertEqual({did for _, did, _ in writes}, {smoke.SMOKE_OVERRIDE_DID})
        self.assertEqual([kind for kind, _, _ in writes], ['set', 'set', 'clear'])
        self.assertEqual(overrides, {smoke.LINK_DID: 'show'})

    def test_a_failure_part_way_still_clears_the_test_override(self):
        for step in ('show', 'hide'):
            failures, writes, overrides = self.run_against(fail_on=step)
            self.assertGreater(failures, 0)
            self.assertEqual(writes[-1], ('clear', smoke.SMOKE_OVERRIDE_DID, None))
            self.assertEqual({did for _, did, _ in writes}, {smoke.SMOKE_OVERRIDE_DID})
            self.assertEqual(overrides, {smoke.LINK_DID: 'show'})

    def test_a_missing_real_override_is_reported_and_not_written(self):
        failures, writes, overrides = self.run_against(link_present=False)
        self.assertGreater(failures, 0)
        self.assertNotIn(smoke.LINK_DID, {did for _, did, _ in writes})
        self.assertEqual(overrides, {})

    def test_the_test_did_is_one_the_worker_and_the_slots_accept(self):
        did = smoke.SMOKE_OVERRIDE_DID
        self.assertTrue(did.startswith('did:plc:'))
        self.assertEqual(len(did), len('did:plc:') + 24)
        self.assertTrue(set(did[len('did:plc:'):]) <= set('abcdefghijklmnopqrstuvwxyz234567'))
        self.assertNotEqual(did, smoke.LINK_DID)


if __name__ == '__main__':
    unittest.main()
