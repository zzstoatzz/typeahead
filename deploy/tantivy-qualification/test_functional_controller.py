import hashlib
import http.client
import http.server
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('functional_controller',HERE/'controller.py')
controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)

class FunctionalControllerTests(unittest.TestCase):
    def test_committed_source_binding(self):
        self.assertEqual(controller.functional_script_sha256(),controller.FUNCTIONAL_SCRIPT_SHA256)
    def test_real_http_chunked_upload_hash_and_single_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            target=str(Path(directory)/'probe.py');calls=[];lost=[False]
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));calls.append(body)
                    if self.path!='/machines/fixture/exec' or body['command'][:2]!=['python3','-c'] or len(body['command'][2])>4000:
                        self.send_error(400);return
                    result=subprocess.run(body['command'],capture_output=True,text=True,timeout=body['timeout'])
                    raw=json.dumps({'stdout':result.stdout,'stderr':result.stderr,'exit_code':result.returncode,'exit_signal':0}).encode()
                    if lost[0]:
                        self.connection.close();return
                    self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                def log_message(self,*args):pass
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def transport(method,path,body):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                try:
                    conn.request(method,'/'+path,json.dumps(body),{'Content-Type':'application/json'});response=conn.getresponse()
                    self.assertEqual(response.status,200);return json.loads(response.read())
                finally:conn.close()
            try:
                source=("#"+'x'*5000+"\nimport json,sys\nprint('QUALIFICATION_POST_MEASUREMENT_FUNCTIONAL '+json.dumps({'passed':True,'timed_result_sha256':sys.argv[1]}))\n").encode()
                sha=hashlib.sha256(source).hexdigest();timed='d'*64
                result=controller._execute_functional('fixture',source,[timed],sha,transport,target)
                self.assertTrue(result['passed']);self.assertEqual(result['timed_result_sha256'],timed)
                self.assertEqual(Path(target).read_bytes(),source);self.assertEqual(len(calls),4)
                before=len(calls)
                with self.assertRaisesRegex(RuntimeError,'upload hash differs'):controller._execute_functional('fixture',source,[timed],'0'*64,transport,target)
                self.assertEqual(len(calls),before)
                with self.assertRaisesRegex(RuntimeError,'execution unconfirmed'):controller._execute_functional('fixture',source,[timed],sha,transport,target)
                self.assertEqual(len(calls),before+1)
                lost[0]=True;before=len(calls);lost_target=str(Path(directory)/'uncertain.py')
                with self.assertRaises(http.client.HTTPException):controller._execute_functional('fixture',source,[timed],sha,transport,lost_target)
                self.assertEqual(len(calls),before+1);self.assertEqual(Path(lost_target).read_bytes(),source[:1800])
            finally:server.shutdown();server.server_close();thread.join(2)

if __name__=='__main__':unittest.main()
