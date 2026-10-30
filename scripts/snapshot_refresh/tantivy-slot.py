"""Managed immutable Tantivy slot; source freshness is qualified separately from install."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import tantivy_install as installer
from install import atomic_json, object_key

RESERVE = 1024**3
DEPLOYMENT_FIELDS = {'deployment_schema_version','request_id','artifact_manifest_key','artifact_manifest_sha256','source_manifest_key','provider_app','image_digest','guest','volume_id','volume_size_gib','public_service_ports'}


def checksum(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def metadata(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024*1024:raise ValueError('Invalid cached metadata')
    return json.loads(path.read_bytes())


class R2:
    def __init__(self, env):
        endpoint=urllib.parse.urlsplit(env['INDEX_R2_ENDPOINT'])
        if endpoint.scheme!='https' or not re.fullmatch(r'[a-z0-9]+\.r2\.cloudflarestorage\.com',endpoint.netloc) or endpoint.path not in ('','/') or endpoint.query or endpoint.fragment:raise ValueError('Invalid R2 endpoint')
        self.bucket=env['INDEX_R2_BUCKET']
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,62}',self.bucket):raise ValueError('Invalid R2 bucket')
        self.config=tempfile.NamedTemporaryFile(mode='w',prefix='tantivy-slot-r2-',suffix='.conf')
        try:
            self.config.write('[r2]\ntype = s3\nprovider = Cloudflare\nregion = auto\nno_check_bucket = true\n')
            for key,name in [('endpoint','INDEX_R2_ENDPOINT'),('access_key_id','INDEX_R2_ACCESS_KEY_ID'),('secret_access_key','INDEX_R2_SECRET_ACCESS_KEY')]:
                value=env[name]
                if not value or any(c in value for c in '\r\n\0'):raise ValueError('Invalid R2 configuration')
                self.config.write(key+' = '+value+'\n')
            self.config.flush()
        except BaseException:self.close();raise
    def close(self):self.config.close()
    def download(self,key,path,max_bytes):
        remote='r2:'+self.bucket+'/'+object_key(key)
        deadline=time.monotonic()+3600
        with path.open('xb') as output:
            child=subprocess.Popen(['rclone','--config',self.config.name,'--contimeout','10s','--timeout','30s','--retries','3','cat',remote],stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.DEVNULL)
            try:
                while child.poll() is None:
                    if path.stat().st_size>max_bytes or shutil.disk_usage(path.parent).free<RESERVE or time.monotonic()>deadline:raise ValueError('Bounded artifact transfer failed')
                    time.sleep(.1)
                if child.returncode or path.stat().st_size>max_bytes:raise ValueError('Artifact transfer failed')
                output.flush();os.fsync(output.fileno())
            except BaseException:
                child.kill();child.wait();raise


class Slot:
    def __init__(self,env,root=Path('/data/tantivy'),binary=Path('/app/typeahead-tantivy'),transport=None):
        self.env=dict(env);self.root=Path(root);self.binary=Path(binary);self.transport=transport
        self.app=self.env['FLY_APP_NAME'];self.build=self.env['EXPECTED_BUILD_ID'];self.key=self.env['TANTIVY_DEPLOYMENT_KEY'];self.expected=self.env['EXPECTED_DEPLOYMENT_SHA256']
        if self.app not in installer.SLOT_APPS or self.env.get('FLY_MACHINE_ID') in installer.PROTECTED_MACHINES:raise ValueError('Unapproved slot')
        match=re.fullmatch(r'control/tantivy-slots/'+re.escape(self.app)+r'/([a-f0-9]{32})\.json',self.key)
        if not match or not installer.BUILD.fullmatch(self.build):raise ValueError('Invalid deployment identity')
        installer.digest(self.expected);self.request_id=match[1]
        for parent in [self.root,*self.root.parents]:
            if parent.is_symlink():raise ValueError('Symlink slot root')
        self.cache=self.root.parent/'.tantivy-slot'
        self.children=[]
    def fetch(self,key,name,limit,sha=None):
        target=self.cache/name
        if target.exists():raise ValueError('Unexpected incomplete slot transfer')
        self.transport.download(key,target,limit)
        if target.stat().st_size>limit or sha is not None and checksum(target)!=sha:raise ValueError('Artifact byte identity mismatch')
        return target
    def validate_deployment(self,raw):
        if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=self.expected:raise ValueError('Deployment byte identity mismatch')
        value=json.loads(raw);installer.validate_manifest(value,self.build)
        if value.get('deployment_schema_version')!=1 or value.get('request_id')!=self.request_id or value['provider_app']!=self.app or value['public_service_ports']!=[80,443]:raise ValueError('Deployment target mismatch')
        if value['image_digest']!=self.env.get('FLY_IMAGE_REF'):raise ValueError('Runtime immutable image differs')
        if value.get('artifact_manifest_key')!=f'releases/search-tantivy/{self.build}/manifest.json' or value.get('source_manifest_key')!=f"releases/search/{value['source_build_id']}/manifest.json":raise ValueError('Deployment artifact key differs')
        installer.digest(value.get('artifact_manifest_sha256'))
        if checksum(self.binary)!=value['binary_sha256']:raise ValueError('Runtime reader differs')
        return value
    def validate_cursor(self,state):
        source=Path(__file__).resolve().parent/'input'
        if not source.is_dir():source=Path(__file__).resolve().parents[2]/'services/tantivy-input'
        modules=[]
        for name in ['sync','current_state']:
            spec=importlib.util.spec_from_file_location('slot_'+name,source/(name+'.py'))
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);modules.append(module)
        replay,current=modules
        try:current.ObservedSynchronizer(None,None,replay).cursor(state)
        except replay.SafeError:raise ValueError('Cached source cursor rejected') from None
    def actual_metadata(self,value):
        result=subprocess.run([str(self.binary),'status','--data',str(self.root),'--writer-memory-mb','32'],capture_output=True,timeout=60)
        if result.returncode or len(result.stdout)>8*1024**2:raise ValueError('Cached reader rejected index')
        state=json.loads(result.stdout)
        installer.validate_metadata(state,value,require_coverage_ready=False,require_source_ready=False)
        self.validate_cursor(state);return state
    def install(self):
        self.cache.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.cache.is_symlink():raise ValueError('Symlink metadata cache')
        cached=self.cache/'deployment.json'
        if (self.root/'installation.json').exists():
            if cached.is_symlink() or not cached.is_file() or cached.stat().st_size>1024*1024 or (self.root/'index').is_symlink():raise ValueError('Invalid cached generation paths')
            value=self.validate_deployment(cached.read_bytes());receipt=metadata(self.root/'installation.json')
            if receipt.get('manifest_sha256')!=installer.digest_json(value) or receipt.get('deployment_sha256')!=self.expected or receipt.get('passed') is not True or receipt.get('binary_sha256')!=value['binary_sha256'] or receipt.get('bundle_sha256')!=value['sha256']:raise ValueError('Cached installation differs')
            receipt['runtime_qualified']=False;atomic_json(self.root/'installation.json',receipt)
            self.actual_metadata(value);return value
        if self.root.exists():raise ValueError('Unadmitted slot destination exists')
        if any(self.cache.iterdir()):raise ValueError('Incomplete installation requires inactive recovery')
        owned_transport=False
        if self.transport is None:self.transport=R2(self.env);owned_transport=True
        try:
            deployment=self.fetch(self.key,'deployment.json',1024*1024,self.expected)
            value=self.validate_deployment(deployment.read_bytes())
            portable=self.fetch(value['artifact_manifest_key'],'artifact-manifest.json',1024*1024,value['artifact_manifest_sha256'])
            artifact=json.loads(portable.read_bytes());installer.validate_artifact_manifest(artifact,self.build)
            if set(value)!=set(artifact)|DEPLOYMENT_FIELDS or any(value.get(k)!=v for k,v in artifact.items()) or set(artifact)&DEPLOYMENT_FIELDS:raise ValueError('Portable and target manifests differ')
            source=self.fetch(value['source_manifest_key'],'source-manifest.json',1024*1024,value['source_manifest_sha256'])
            installer.validate_source_manifest(value,source.read_bytes())
            needed=value['byte_size']+sum(row['byte_size'] for row in value['files'])+RESERVE
            if shutil.disk_usage(self.cache).free<needed:raise ValueError('Insufficient final slot reserve')
            bundle=self.fetch(value['object_key'],'index.tar',value['byte_size'],value['sha256'])
            receipt=installer.install_bundle(bundle,self.root,value,self.binary)
            receipt.update(deployment_sha256=self.expected,deployment_key=self.key,artifact_manifest_sha256=value['artifact_manifest_sha256'])
            atomic_json(self.root/'installation.json',receipt)
            bundle.unlink();return value
        finally:
            if owned_transport:self.transport.close();self.transport=None
    def stop(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait()
    def runtime_gates(self,value):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/index/status',timeout=3) as response:
                raw=response.read(1024*1024+1)
                if len(raw)>1024*1024:raise ValueError('Runtime metadata too large')
                status=json.loads(raw)
            gates=installer.validate_runtime(status,value)
            receipt=metadata(self.root/'installation.json');receipt.update(runtime_qualified=True,runtime_qualified_at=int(time.time()),runtime_gates=gates,slo_empirically_qualified=False)
        except (OSError,ValueError,KeyError):
            receipt=metadata(self.root/'installation.json');receipt.update(runtime_qualified=False)
        atomic_json(self.root/'installation.json',receipt)
    def start_reader(self,value,listen='0.0.0.0:8080',admin_listen='127.0.0.1:8081'):
        server=subprocess.Popen([str(self.binary),'serve','--data',str(self.root),'--listen',listen,'--admin-listen',admin_listen,'--max-sync-lag-s','120','--retrieval',value['retrieval_strategy']],env=self.env)
        self.children.append(server);return server
    def serve(self,value):
        if not all(self.env.get(name) for name in ['ADMIN_SECRET','TURSO_URL','TURSO_AUTH_TOKEN']):raise ValueError('Missing source runtime capability')
        server=self.start_reader(value)
        while server.poll() is None:
            logpath=self.root/'source.log'
            if logpath.exists() and logpath.stat().st_size>16*1024**2:logpath.replace(self.root/'source.previous.log')
            with logpath.open('ab') as log:
                source=subprocess.Popen(['python3','/app/input/sync.py','--source-mode','observed','--policy-mode','snapshot','--status-file',str(self.root/'source-status.json'),'--budget-file',str(self.root/'source-budget.json'),'--budget-build-id',value['active_build'],'--max-run-seconds','3600','--max-source-bytes','536870912','--poll-reserve-bytes','134217728','--anti-entropy-pages','64','--max-sweep-seconds','20','--max-cycle-seconds','30','--interval','40'],env=self.env,stdout=log,stderr=subprocess.STDOUT)
            self.children.append(source)
            while source.poll() is None and server.poll() is None:self.runtime_gates(value);time.sleep(5)
            if source.poll() is None:
                source.terminate()
                try:source.wait(timeout=10)
                except subprocess.TimeoutExpired:source.kill();source.wait()
            self.children.remove(source)
            if server.poll() is None:time.sleep(2)
        raise RuntimeError('Slot reader stopped')


def main():
    slot=None
    def interrupted(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    try:
        slot=Slot(os.environ);value=slot.install();slot.serve(value)
    except BaseException as error:
        if slot:slot.stop()
        print(json.dumps({'event':'tantivy_slot_stopped','error_class':type(error).__name__}),flush=True)
        return 1


if __name__=='__main__':raise SystemExit(main())
