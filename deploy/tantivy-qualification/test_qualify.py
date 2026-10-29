import copy
import http.server
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('qualification', HERE / 'qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)

class Pilot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.root = Path(self.temp.name)
        self.servers = []
        inputs = {'corpus': {'actors': [{'did': 'did:fixture:a', 'handle': 'alice.test'}]},
                  'queries': [{'id': 'exact', 'path': '/search?q=alice.test&limit=10'},
                              {'id': 'recall', 'path': '/search?q=alice%20name&limit=10'}],
                  'updates': [],
                  'oracle': {'exact': {a: {'status': 200, 'ids': ['did:fixture:a']} for a in ['native', 'tantivy']},
                             'recall': {'native': {'status': 200, 'ids': []},
                                        'tantivy': {'status': 200, 'ids': ['did:fixture:a']},
                                        'intentional_difference': 'native capped candidate fixture loses matching actor'}}}
        self.m = {'schema': 1, 'mode': 'pilot', 'nonce': 'local-fixture-only', 'inputs': {}, 'arms': {},
                  'load_generator_id': 'separate-fixture-client',
                  'native_source': {'immutable_r2': True, 'protected_disk_used': False},
                  'phases': [{'name': 'pilot', 'attempts': 4, 'rps': 10, 'concurrency': 2, 'deadline_s': 11}]}
        for name, data in inputs.items():
            path = self.root / (name + '.json'); path.write_text(json.dumps(data))
            self.m['inputs'][name] = {'path': path.name, 'sha256': q.digest(path)}
        self.m['inputs']['oracle']['method'] = 'independent-raw-profile'
        for arm in ['native', 'tantivy']:
            config = {'machine_id': 'fixture-' + arm, 'volume_id': 'volume-' + arm,
                      'guest': {'cpu_kind': 'shared', 'cpus': 2, 'memory_mb': 1024}}
            self.m['arms'][arm] = config
            owner = self
            def handler(role):
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        if self.path == '/__evaluation':
                            data = {**owner.m['arms'][role], 'arm': role, 'nonce': owner.m['nonce'], 'ready': True,
                                    'corpus_sha256': owner.m['inputs']['corpus']['sha256'],
                                    'updates_sha256': owner.m['inputs']['updates']['sha256']}
                        else:
                            ids = [] if role == 'native' and 'alice%20name' in self.path else ['did:fixture:a']
                            data = {'actors': [{'did': did} for did in ids]}
                        payload = json.dumps(data).encode()
                        self.send_response(200);self.send_header('Content-Length', str(len(payload)));self.end_headers();self.wfile.write(payload)
                    def log_message(self, *args):
                        pass
                return Handler
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler(arm))
            threading.Thread(target=server.serve_forever, daemon=True).start();self.servers.append(server)
            config['url'] = 'http://127.0.0.1:' + str(server.server_port)
        self.path = self.root / 'manifest.json'
        self.save()

    def tearDown(self):
        for server in self.servers:
            server.shutdown();server.server_close()
        self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.m))

    def test_real_http_paired_pilot(self):
        m, cases, oracle = q.load(self.path)
        reports = {a: [q.phase(m, a, m['phases'][0], cases, oracle)] for a in ['native', 'tantivy']}
        result = q.compare(m, reports)
        self.assertTrue(result['correctness_passed'])
        self.assertFalse(result['full_qualification'])
        self.assertEqual(sum(len(x['attempts']) for ps in reports.values() for x in ps), 8)
        (HERE / 'local-pilot.json').write_text(json.dumps({'kind': 'synthetic-local-HTTP-pilot',
            'correctness_passed': result['correctness_passed'], 'attempts': 8,
            'intentional_recall_difference_checked': True, 'full_qualification': False,
            'cloud_calls': 0, 'performance_claim': None}, indent=2) + '\n')

    def test_input_tampering_fails(self):
        (self.root / 'updates.json').write_text('[{}]')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            q.load(self.path)

    def test_protected_and_colocated_rejected(self):
        self.m['arms']['native']['machine_id'] = '8e3d79b70dd748';self.save()
        with self.assertRaisesRegex(ValueError, 'protected'):
            q.load(self.path)
        self.m['arms']['native']['machine_id'] = self.m['load_generator_id'];self.save()
        with self.assertRaisesRegex(ValueError, 'separate'):
            q.load(self.path)

    def test_wrong_identity_fails_before_load(self):
        self.m['nonce'] = 'expected'
        m = copy.deepcopy(self.m);m['nonce'] = 'different'
        with self.assertRaisesRegex(ValueError, 'ownership'):
            q.identity(m, 'native')

    def test_wrong_oracle_fails_correctness(self):
        m, cases, oracle = q.load(self.path)
        oracle['exact']['tantivy']['ids'] = ['did:fixture:missing']
        result = q.phase(m, 'tantivy', m['phases'][0], cases, oracle)
        self.assertEqual(result['failures'], 2)

    def test_undeclared_recall_difference_rejected(self):
        p = self.root / 'oracle.json';d = json.loads(p.read_text());del d['recall']['intentional_difference'];p.write_text(json.dumps(d))
        self.m['inputs']['oracle']['sha256'] = q.digest(p);self.save()
        with self.assertRaisesRegex(ValueError, 'different semantics'):
            q.load(self.path)

if __name__ == '__main__':
    unittest.main()
