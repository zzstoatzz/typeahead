"""One isolated engine plus the identical private HTTP hop for matched trials."""
import argparse
import hashlib
import http.client
import http.server
import io
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import urllib.parse

PROTECTED={'8e3d79b70dd748','801539a6551778','7845636a3d0ee8','840e95a2153308'}
APP=Path('/app')
UPSTREAM_PORT=18080
ALLOWED={'/search','/health','/ready','/index/status','/debug/search',
         '/xrpc/tech.waow.typeahead.searchActors','/xrpc/app.bsky.actor.searchActorsTypeahead'}

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def owned_path(root,value):
    path=Path(value)
    if not path.is_absolute() or path.resolve()!=path or path==root or not path.is_relative_to(root):
        raise ValueError('qualification path is not canonical and private')
    return path

def load(path):
    if not path.is_absolute() or path.resolve()!=path:raise ValueError('run manifest must be a canonical private path')
    config=json.loads(path.read_text())
    root=Path(config['root'])
    if not root.is_absolute() or root.resolve()!=root or root==Path('/data') or root.is_relative_to('/data/refresh'):
        raise ValueError('invalid qualification root')
    owned_path(root,path)
    owned_path(root,root/'owner')
    if (root/'owner').read_text()!='native-qualification-v1':raise ValueError('qualification owner mismatch')
    if config['schema']!=1 or config['arm'] not in {'native','tantivy'} or not config['app'].startswith('typeahead-qual-'):
        raise ValueError('unsupported qualification role/owner')
    if os.environ.get('FLY_APP_NAME')!=config['app'] or os.environ.get('FLY_MACHINE_ID')!=config['machine_id'] or config['machine_id'] in PROTECTED:
        raise ValueError('runtime app/machine ownership mismatch')
    if not 16<=len(config['nonce'])<=128:raise ValueError('nonce budget')
    for key in ['corpus_sha256','updates_sha256']:
        if len(config[key])!=64 or any(c not in '0123456789abcdef' for c in config[key]):raise ValueError('input digest shape')
    if config['guest']!={'cpu_kind':'shared','cpus':2,'memory_mb':1024} or config['final_volume_gib']!=20:
        raise ValueError('qualification requires final matched shape/disk')
    observer_path=owned_path(root,config['observer_receipt'])
    observer=json.loads(observer_path.read_text())
    if any(observer.get(key)!=config[key] for key in ['app','machine_id','guest']) or observer.get('public_services')!=[] or observer.get('volume_ids')!=[config['volume_id']]:
        raise ValueError('external ownership receipt mismatch')
    if observer.get('final_volume_gib')!=20 or not observer.get('machines_api_response_sha256'):
        raise ValueError('external final-volume/provider witness required')
    for key,value in config['paths'].items():owned_path(root,value)
    address=ipaddress.ip_address(config['private_listen'])
    if address.version!=6 or not (address.exploded.startswith('fdaa:') or address==ipaddress.IPv6Address('::')):
        raise ValueError('listener must be Fly private IPv6')
    if any(key.startswith('TURSO_') for key in os.environ):raise ValueError('live source environment is not admitted by frozen qualification runtime')
    settings=config.setdefault('engine_settings',{})
    if not isinstance(settings,dict):raise ValueError('engine settings must be explicit object')
    if config['arm']=='tantivy':
        if settings.setdefault('retrieval_strategy','per-segment') not in {'per-segment','global-merge'}:raise ValueError('invalid retrieval strategy')
        for key,default,low,high in [('writer_memory_mb',128,32,1024),('concurrency',4,1,8),('max_candidates',10000,100,100000)]:
            value=settings.setdefault(key,default)
            if type(value) is not int or not low<=value<=high:raise ValueError('engine setting exceeds qualification bounds')
    config['_observer_sha256']=sha(observer_path)
    config['_synthetic_local_fixture']=observer.get('synthetic_local_fixture') is True
    return config

def upstream(path):
    conn=http.client.HTTPConnection('127.0.0.1',UPSTREAM_PORT,timeout=3)
    started=time.monotonic()
    try:
        conn.request('GET',path,headers={'Connection':'close'})
        response=conn.getresponse();body=bytearray()
        while True:
            if time.monotonic()-started>3:raise TimeoutError('upstream deadline')
            chunk=response.read1(65536)
            if not chunk:break
            body.extend(chunk)
            if len(body)>524288:raise ValueError('upstream response exceeds 512KiB')
        admitted_headers={'content-type','server-timing','x-typeahead-pds','x-typeahead-prefer-pds','x-typeahead-preference','x-typeahead-boost','x-typeahead-engine','x-typeahead-build','x-typeahead-format','x-typeahead-binary-sha256','x-typeahead-retrieval-strategy','x-typeahead-machine-id','x-typeahead-machine-version'}
        headers={k.lower():v for k,v in response.getheaders() if k.lower() in admitted_headers}
        return response.status,headers,bytes(body)
    finally:conn.close()

def handler(config,binary):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def handle_one_request(self):
            try:
                self.raw_requestline=self.rfile.readline(4097)
                if not self.raw_requestline:self.close_connection=True;return
                if not self.parse_request():return
                method=getattr(self,'do_'+self.command,None)
                if method is None:self.send_error(501);return
                method();self.wfile.flush()
            except (TimeoutError,ConnectionError):self.close_connection=True
        def parse_request(self):
            self.command=None;self.request_version='HTTP/1.1';self.requestline=''
            if len(self.raw_requestline)>4096:self.send_error(414);return False
            original=self.rfile;headers=bytearray()
            while True:
                remaining=4096-len(self.raw_requestline)-len(headers)
                if remaining<=0:self.send_error(431);return False
                line=original.readline(remaining+1);headers.extend(line)
                if len(line)>remaining:self.send_error(431);return False
                if line in (b'\r\n',b'\n',b''):break
            self.rfile=io.BytesIO(headers)
            try:return super().parse_request()
            finally:self.rfile=original
        def setup(self):
            super().setup();self.connection.settimeout(4)
        def emit(self,status,headers,body):
            self.send_response(status)
            for name,value in headers.items():self.send_header(name,value)
            self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body);self.close_connection=True
        def do_GET(self):
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0')!='0':self.send_error(400);return
            path=urllib.parse.urlsplit(self.path)
            if path.path=='/__evaluation':
                try:
                    status,_,raw=upstream('/health');health=json.loads(raw)
                    ready=status==200 and health.get('ready') is True
                except Exception:ready=False
                process_verified=False
                executable_sha256=None
                try:
                    pid=config.get('_engine_pid')
                    if pid:
                        executable_sha256=sha(Path('/proc')/str(pid)/'exe')
                        actual_binary=sha(binary)
                        process_verified=executable_sha256==actual_binary
                        if not process_verified and not config.get('_synthetic_local_fixture'):ready=False
                    elif not os.environ.get('FLY_MACHINE_ID'):
                        actual_binary=sha(binary)
                    else:
                        actual_binary=None;ready=False
                except OSError:
                    actual_binary=None;ready=False
                data={key:config[key] for key in ['nonce','arm','machine_id','volume_id','guest','corpus_sha256','updates_sha256']}
                data.update({'control_machine_id':config['machine_id'],'control_volume_id':config['volume_id'],'ready':ready,
                             'observer_receipt_sha256':config['_observer_sha256'],'engine_binary_sha256':actual_binary,'runtime_process_verified':process_verified,
                             'process_executable_sha256':executable_sha256,'synthetic_local_fixture':config.get('_synthetic_local_fixture',False),
                             'source_kind':'frozen-synthetic-controller','source_ready_attestation':False,'engine_settings':config.get('engine_settings',{})})
                self.emit(200,{'Content-Type':'application/json'},json.dumps(data,separators=(',',':')).encode());return
            if path.path not in ALLOWED or path.scheme or path.netloc:self.send_error(404);return
            try:self.emit(*upstream(self.path))
            except Exception:self.emit(502,{'Content-Type':'application/json'},b'{"error":"qualification upstream failed"}')
        def do_POST(self):self.send_error(405)
        def log_message(self,*args):pass
    return Handler

class PrivateHTTPServer(http.server.ThreadingHTTPServer):
    address_family=socket.AF_INET6
    daemon_threads=True
    request_queue_size=32

def commands(config):
    paths=config['paths'];root=config['root']
    shared={**os.environ,'SQLITE_TMPDIR':paths['tmp']}
    if config['arm']=='native':
        binary=APP/'bin/eval-server'
        shared.update({'TYPEAHEAD_EVAL_QUALIFICATION':'1','TYPEAHEAD_EVAL_PRIVATE_ROOT':root,
            'TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT':'1','TYPEAHEAD_EVAL_SNAPSHOT':paths['snapshot'],
            'TYPEAHEAD_EVAL_NONCE':config['nonce'],'TYPEAHEAD_EVAL_INPUT_ID':config['corpus_sha256'],
            'LOCAL_DB_PATH':paths['overlay'],'PORT':'18080','NATIVE_QUAL_ROOT':root,
            'NATIVE_QUAL_REQUEST':paths['request'],'NATIVE_QUAL_RESPONSE':paths['response'],
            'NATIVE_QUAL_FEED_SHA256':config['updates_sha256'],'NATIVE_QUAL_SECONDS':'900'})
        return binary,[([str(binary)],shared),([str(APP/'bin/qualification-native')],shared)]
    binary=APP/'bin/typeahead-tantivy'
    settings=config['engine_settings']
    command=[str(binary),'serve','--data',paths['tantivy_data'],'--listen','127.0.0.1:18080',
             '--admin-listen','127.0.0.1:18081','--writer-memory-mb',str(settings['writer_memory_mb']),
             '--concurrency',str(settings['concurrency']),'--max-candidates',str(settings['max_candidates']),
             '--retrieval',settings['retrieval_strategy']]
    if config.get('allow_stale') is True:command.extend(['--allow-stale','true'])
    if config.get('allow_partial') is True:command.extend(['--allow-partial','true'])
    if not os.environ.get('ADMIN_SECRET'):raise ValueError('loopback admin capability required')
    return binary,[(command,shared)]

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['serve'],nargs='?',default='serve')
    parser.add_argument('--run',type=Path,default=Path('/data/qualification/run/run.json'));args=parser.parse_args()
    config=load(args.run);binary,processes=commands(config)
    if not binary.is_file():raise ValueError('selected engine is absent from this image')
    Path(config['paths']['tmp']).mkdir(exist_ok=True)
    children=[]
    def terminate(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    start=time.monotonic();limit=config.get('measurement_deadline_s',900)
    if not 1<=limit<=900:raise ValueError('measurement deadline exceeds budget')
    try:
        for index,(argv,env) in enumerate(processes):
            children.append(subprocess.Popen(argv,env=env,start_new_session=True))
            if index==0:
                config['_engine_pid']=children[0].pid
                startup_deadline=time.monotonic()+60
                while True:
                    if children[0].poll() is not None:raise RuntimeError('engine stopped during startup')
                    try:
                        status,_,_=upstream('/health')
                        if status==200:break
                    except Exception:pass
                    if time.monotonic()>startup_deadline:raise TimeoutError('engine startup deadline')
                    time.sleep(.05)
        server=PrivateHTTPServer((config['private_listen'],8080),handler(config,binary));server.timeout=.5
        while time.monotonic()-start<limit:
            if any(child.poll() is not None for child in children):raise RuntimeError('selected engine process stopped')
            server.handle_request()
    finally:
        if 'server' in locals():server.server_close()
        for child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)

if __name__=='__main__':main()
