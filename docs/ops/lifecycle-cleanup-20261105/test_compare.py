from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

import compare


@contextmanager
def backend(machine,*,timing=1,wrong_build=False,redirect=False,spurious_scope=False):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(time.monotonic())
            if redirect:
                self.send_response(302);self.send_header('Location','/redirect');self.end_headers();return
            body = b'{"actors":[{"handle":"zzstoatzz.io"}]}'
            self.send_response(200)
            headers = {'X-Typeahead-Engine':'tantivy',
                'X-Typeahead-Format':'tantivy-0.27-f32-did-expiry-v4',
                'X-Typeahead-Build':'b1793850000-wrong' if wrong_build else 'b1793850000-fixture',
                'X-Typeahead-Binary-Sha256':compare.BINARY,
                'X-Typeahead-Retrieval-Strategy':'global-merge',
                'X-Typeahead-Machine-Id':machine,'X-Typeahead-Machine-Version':'fixture-'+machine,
                'Server-Timing':f'search;dur={timing}',
                'X-Typeahead-Pds':'pds.zzstoatzz.io' if spurious_scope or
                    'pds' in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query) else '',
                'Content-Length':str(len(body))}
            for key,value in headers.items():self.send_header(key,value)
            self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}',requests
    finally:
        server.shutdown();server.server_close();thread.join(2)


class ComparisonTests(unittest.TestCase):
    def command(self,a,b,path):
        return [sys.executable,str(Path(compare.__file__)), '--baseline',a,'--candidate',b,
            '--baseline-machine','a'*14,'--candidate-machine','b'*14,
            '--build-id','b1793850000-fixture','--output',str(path),'--test-loopback']
    def test_complete_paced_cohort_and_slow_engine_are_not_confused_with_network_time(self):
        for timing,passed in [(1,True),(500,False)]:
            with self.subTest(timing=timing), tempfile.TemporaryDirectory() as folder, \
                    backend('a'*14) as (a,ar), backend('b'*14,timing=timing) as (b,br):
                path=Path(folder)/'result.json'
                child=subprocess.run(self.command(a,b,path),capture_output=True,timeout=65)
                report=json.loads(path.read_bytes())
                self.assertEqual(child.returncode,0 if passed else 1,child.stderr)
                self.assertEqual(report['passed'],passed);self.assertTrue(report['test_only'])
                self.assertEqual(len(report['pairs']),39);self.assertEqual(len(report['samples']),78)
                self.assertEqual(report['metrics']['candidate']['samples'],26)
                self.assertEqual(report['metrics']['candidate']['sample_p95_ms'],timing)
                for requests in [ar,br]:
                    self.assertEqual(len(requests),39)
                    self.assertGreaterEqual(min(y-x for x,y in zip(requests,requests[1:])),.98)
    def test_wrong_identity_preserves_failure_and_cannot_overwrite_it(self):
        with tempfile.TemporaryDirectory() as folder, backend('a'*14,wrong_build=True) as (a,_), \
                backend('b'*14) as (b,_):
            path=Path(folder)/'failure.json';command=self.command(a,b,path)
            self.assertEqual(subprocess.run(command,capture_output=True,timeout=5).returncode,1)
            raw=path.read_bytes();report=json.loads(raw)
            self.assertFalse(report['passed']);self.assertEqual(len(report['samples']),1)
            self.assertIn('identity',report['samples'][0]['error']['detail'])
            before=hashlib.sha256(raw).hexdigest()
            self.assertNotEqual(subprocess.run(command,capture_output=True,timeout=5).returncode,0)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
    def test_redirect_does_not_escape_the_request_budget(self):
        with tempfile.TemporaryDirectory() as folder, backend('a'*14,redirect=True) as (a,requests), \
                backend('b'*14) as (b,_):
            path=Path(folder)/'redirect.json'
            self.assertEqual(subprocess.run(self.command(a,b,path),capture_output=True,timeout=5).returncode,1)
            self.assertFalse(json.loads(path.read_bytes())['passed']);self.assertEqual(len(requests),1)
    def test_scoped_answer_cannot_qualify_as_a_global_answer(self):
        with tempfile.TemporaryDirectory() as folder, backend('a'*14,spurious_scope=True) as (a,_), \
                backend('b'*14) as (b,_):
            path=Path(folder)/'scope.json'
            self.assertEqual(subprocess.run(self.command(a,b,path),capture_output=True,timeout=5).returncode,1)
            report=json.loads(path.read_bytes());self.assertFalse(report['passed'])
            self.assertIn('global',report['samples'][0]['error']['detail'])


if __name__=='__main__':unittest.main()
