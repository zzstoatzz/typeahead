import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse

ROOT = Path(__file__).resolve().parent
SOURCE = Path('/home/stoat/typeahead-rollout-20261008')
BINARY = Path('/home/stoat/typeahead-release-20261017/release-cancel-fix-v2')
PREFIX = 'staging/compact-release-20261017/native-reload'
sys.path.insert(0, '/home/stoat/typeahead-release-20261017/scripts/snapshot_compaction')
from publish import Publisher, configuration


def manifests():
    cache = SOURCE / 'cache-isolated'
    initial = {v: json.loads((cache / (v + '.json')).read_text()) for v in ('baseline', 'compact')}
    incoming = {v: json.loads((SOURCE / 'reload-pair' / ('legacy' if v == 'baseline' else v) / 'manifest.json').read_text()) for v in initial}
    return initial, incoming


def sources(variant):
    initial, incoming = manifests()
    return [SOURCE / 'cache-isolated/generations' / initial[variant]['active_build'], SOURCE / 'reload-pair' / ('legacy' if variant == 'baseline' else variant)]


def identities(variant):
    result = {}
    for suffix in ('', '.prefix') if variant == 'compact' else ('',):
        paths = [p / ('index.db' + suffix) for p in sources(variant)]
        stats = [p.stat() for p in paths]
        assert (stats[0].st_dev, stats[0].st_ino) != (stats[1].st_dev, stats[1].st_ino)
        result[suffix or '.db'] = [dict(path=str(p), device=s.st_dev, inode=s.st_ino, bytes=s.st_size) for p, s in zip(paths, stats)]
    return result


def verify_artifacts():
    protocol=json.loads((ROOT/'reload-latency-formal-protocol.json').read_text())
    assert hashlib.sha256(BINARY.read_bytes()).hexdigest()==protocol['binary_sha256']['compact']
    initial,incoming=manifests()
    evidence=[]
    for variant in ('baseline','compact'):
        for manifest,source in zip((initial[variant],incoming[variant]),sources(variant)):
            for name,descriptor in [('index.db',manifest)]+([('index.db.prefix',manifest['prefix_map'])] if variant=='compact' else []):
                path=source/name;digest=hashlib.sha256()
                with path.open('rb') as stream:
                    while chunk:=stream.read(8*1024*1024): digest.update(chunk)
                assert path.stat().st_size==descriptor['byte_size']
                assert digest.hexdigest()==descriptor['sha256']
                evidence.append({'path':str(path),'sha256':digest.hexdigest(),'bytes':path.stat().st_size})
    return evidence


def summary(values):
    values = sorted(values)
    return {'p95_ms': values[int(len(values)*.95)], 'p99_ms': values[int(len(values)*.99)], 'samples_ms': values}


class Server:
    def __init__(self, variant, family, label, alternate=None):
        self.variant = variant
        self.d = ROOT / os.environ['EVAL_OUTPUT'] / 'runs' / label
        self.d.mkdir(parents=True, exist_ok=False)
        self.initial, self.incoming = manifests()
        self.context = configuration(None)
        self.config = self.context.__enter__()
        self.publisher = Publisher(self.config, os.environ['INDEX_R2_BUCKET'], '/home/stoat/.local/bin/rclone')
        fields = 'format_version prefix_map legacy_twin manifest_version active_build previous_build promoted_at snapshot_url byte_size sha256 source_watermark actor_count prefix_count normalize_version scoring_version top_n_short top_n_medium top_n_long overlay_top_n hydrate_cap builder_version cjk_key_count cjk_actor_count'.split()
        for v in ('baseline','compact'):
            for m, source in zip((self.initial[v],self.incoming[v]), sources(v)):
                target = self.d / 'index/generations' / m['active_build']
                target.mkdir(parents=True)
                for filename in ('index.db','index.db.prefix'):
                    if (source / filename).exists():
                        os.link(source / filename,target / filename)
                normalized = dict(format_version=0,prefix_map=None,legacy_twin=None,previous_build=None,cjk_key_count=0,cjk_actor_count=0)
                normalized.update(m)
                (target/'manifest.json').write_text(json.dumps({f:normalized[f] for f in fields},separators=(',',':')))
        for m, source in ((self.initial['baseline'], SOURCE/'cache-isolated/generations'/self.initial['baseline']['active_build']), (self.incoming['baseline'], SOURCE/'reload-pair/legacy')):
            self.publisher.immutable(source/'manifest.json', PREFIX+'/builds/'+m['active_build']+'/manifest.json')
        state = {'version':1,'current':{'manifest':self.initial[variant],'location':'generation'}}
        if variant=='compact': state['previous']={'manifest':self.initial['baseline'],'location':'generation'}
        (self.d/'index/active.json').write_text(json.dumps(state))
        self.point(self.initial[variant])
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); self.port=listener.getsockname()[1]
        env=dict(os.environ,MODE='search',READ_POOL_SIZE='8',INDEX_BUILD_ROOT=str(self.d),LOCAL_DB_PATH=str(self.d/'overlay.db'),SQLITE_TMPDIR=str(self.d/'tmp'),PORT=str(self.port),INDEX_POLL_SECS='1',INDEX_LATEST_KEY='latest.json',INDEX_R2_BUCKET=os.environ['INDEX_R2_BUCKET']+'/'+PREFIX,ADMIN_SECRET='local-reload-test',TURSO_URL='127.0.0.1:1',TURSO_AUTH_TOKEN='isolated-no-access')
        self.log=self.d/'server.log'
        with self.log.open('w') as log:
            self.p=subprocess.Popen([str(BINARY)],env=env,stdout=log,stderr=log)
        try:
            self.await_build(self.initial[variant]['active_build'],900,initial=True)
            for row in json.loads((SOURCE/'data/heldout3/mutations.json').read_text()):
                code,body,_=self.request('/admin/refresh',row); assert code==200,(code,body[:200])
        except BaseException:
            self.p.kill();self.p.wait();self.context.__exit__(None,None,None);raise

    def point(self, manifest):
        path=self.d/'pointer.json';path.write_text(json.dumps(manifest,separators=(',',':')))
        subprocess.run([*self.publisher.command,'copyto',str(path),self.publisher.remote(PREFIX+'/latest.json')],check=True)
        self.publisher.verify(path,PREFIX+'/latest.json')

    def await_build(self, expected, timeout, initial=False):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            assert self.p.poll() is None,self.log.read_text()
            try:
                code,body,_=self.request('/index/status');state=json.loads(body)
                if code==200 and state.get('build_id')==expected and state['overlay_compact']['complete']:
                    if not initial or 'incremental: done' in self.log.read_text(): return
            except (OSError,ValueError): pass
            time.sleep(.05)
        raise RuntimeError('Native admission deadline exceeded')

    def request(self,path,body=None):
        started=time.monotonic();connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=30)
        try:
            connection.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),headers={'Connection':'close','Authorization':'Bearer local-reload-test','Content-Type':'application/json'})
            response=connection.getresponse();payload=response.read()
            return response.status,payload,(time.monotonic()-started)*1000
        finally: connection.close()

    def query(self,q): return self.request('/search?'+urllib.parse.urlencode({'q':q,'limit':10}))
    def body(self,q):
        status,body,_=self.query(q);assert status==200;return body
    def usage(self):
        fields=Path('/proc',str(self.p.pid),'stat').read_text().rsplit(')',1)[1].split()
        cpu=(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
        io={k:int(v) for k,v in (line.split(':') for line in Path('/proc',str(self.p.pid),'io').read_text().splitlines())}
        return {'cpu_seconds':cpu,'io':io,'status':Path('/proc',str(self.p.pid),'status').read_text()}
    def close_connections(self): pass
    def reload(self):
        start=time.monotonic();target=self.incoming[self.variant]
        self.point(target);self.await_build(target['active_build'],178)
        return 200,b'admitted',(time.monotonic()-start)*1000
    def close(self):
        self.p.terminate()
        try:
            assert self.p.wait(timeout=20)==0
        except BaseException:
            self.p.kill();self.p.wait();raise
        finally: self.context.__exit__(None,None,None)
