import hashlib
import http.client
import http.server
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('qualification_entry',HERE/'entrypoint.py')
entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)

class Proxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls=[]
        class Upstream(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.calls.append(self.path)
                if self.path=='/health':body=b'{"ready":true}'
                elif self.path=='/search?large=1':body=b'x'*524289
                else:body=b'{"actors":[{"did":"did:fixture:a"}]}'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('X-Typeahead-Pds','local.test');self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        cls.up=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        entry.UPSTREAM_PORT=cls.up.server_port
        cls.fixture=tempfile.TemporaryDirectory(dir=HERE,prefix='.guard-proxy-')
        cls.binary=Path(cls.fixture.name)/'binary';cls.binary.write_text('fixture-executable')
        config={'nonce':'synthetic-qualification-proxy','arm':'native','machine_id':'fixture-machine','volume_id':'fixture-volume',
                'guest':{'cpu_kind':'shared','cpus':2,'memory_mb':1024},'corpus_sha256':'a'*64,'updates_sha256':'b'*64,'_observer_sha256':'c'*64}
        cls.proxy=entry.PrivateHTTPServer(('::1',0),entry.handler(config,cls.binary))
        for server in [cls.up,cls.proxy]:threading.Thread(target=server.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        for server in [cls.up,cls.proxy]:server.shutdown();server.server_close()
        cls.fixture.cleanup()
    def request(self,path,method='GET'):
        conn=http.client.HTTPConnection('::1',self.proxy.server_port,timeout=3)
        conn.request(method,path);response=conn.getresponse();data=response.read();headers=dict(response.getheaders());conn.close();return response.status,headers,data
    def test_same_bytes_and_ack(self):
        code,headers,body=self.request('/search?q=a');self.assertEqual(code,200);self.assertEqual(body,b'{"actors":[{"did":"did:fixture:a"}]}');self.assertEqual(headers['x-typeahead-pds'],'local.test')
    def test_admin_and_post_not_forwarded(self):
        before=len(self.calls);self.assertEqual(self.request('/admin/apply')[0],404);self.assertEqual(self.request('/search','POST')[0],405);self.assertEqual(len(self.calls),before)
    def test_body_bound(self):
        code,_,body=self.request('/search?large=1');self.assertEqual(code,502);self.assertEqual(json.loads(body)['error'],'qualification upstream failed')
    def test_header_bound(self):
        before=len(self.calls)
        with socket.create_connection(('::1',self.proxy.server_port),timeout=3) as conn:
            conn.sendall(b'GET /search HTTP/1.1\r\nHost: local\r\nX-Large: '+b'x'*5000+b'\r\n\r\n')
            response=conn.recv(4096)
        self.assertTrue(response.startswith(b'HTTP/1.1 431'));self.assertEqual(len(self.calls),before)
    def test_request_line_bound(self):
        before=len(self.calls)
        with socket.create_connection(('::1',self.proxy.server_port),timeout=3) as conn:
            conn.sendall(b'GET /search?'+b'x'*5000+b' HTTP/1.1\r\nHost: local\r\n\r\n')
            response=conn.recv(4096)
        self.assertTrue(response.startswith(b'HTTP/1.1 414'));self.assertEqual(len(self.calls),before)
    def test_identity_explicitly_synthetic(self):
        code,_,body=self.request('/__evaluation');data=json.loads(body);self.assertEqual(code,200);self.assertTrue(data['ready']);self.assertFalse(data['source_ready_attestation']);self.assertEqual(data['source_kind'],'frozen-synthetic-controller');self.assertEqual(data['engine_binary_sha256'],hashlib.sha256(self.binary.read_bytes()).hexdigest())

if __name__=='__main__':
    program=unittest.main(exit=False)
    report={'passed':program.result.wasSuccessful(),'kind':'actual-local-HTTP-private-proxy',
            'tests':program.result.testsRun,'cloud_calls':0,
            'entrypoint_sha256':hashlib.sha256((HERE/'entrypoint.py').read_bytes()).hexdigest()}
    (HERE/'proxy-results.json').write_text(json.dumps(report,indent=2)+'\n')
    raise SystemExit(0 if report['passed'] else 1)
