"""Owned full-corpus qualification guest; frozen feed and private bounded control.

QUAL_MODE=full explicitly enables the 5,400-second guest lease. Preparation has a
3,600-second cap. Neither engine starts before POST /serve; /pause, /restart and
/reset operate only on this guest's owned working clone. Baseline SQLite/meta
files are copied; immutable prefix/segment files are linked and never chmodded.
Native prefix admission is repeated after linking because its ctime changed.

GET /prepared exposes metadata. All other controls require the independent
X-Pilot-Authorization capability: GET /inputs/{updates,cases,oracle} or /metrics; empty-body
POST /serve, /pause, /restart, /reset, /metrics, /apply/1 through /apply/9.
No control accepts actor payloads or paths. updates.json is the exact canonical
UTF-8 feed (no LF); its SHA256 binds both arms and the independent oracle.
The native guest freezes raw/feed-derived cases, then streams the complete source
through the independent oracle before advertising prepared/oracle_ready. Only
validated receipts and fixed process metadata are retained; raw child output is
discarded. Frozen stale-source allowance never attests live-source health.
The owning controller/reaper retires the complete scratch volume on completion.
"""
import copy
from contextlib import closing
import hashlib
import hmac
import http.client
import http.server
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from types import SimpleNamespace

import full_prepare

OWNER = full_prepare.OWNER
GIB = 1024 ** 3
MAX_FEED = 8 * 1024 * 1024
MAX_ACTOR = 8192
FIELDS = ('did','handle','display_name','avatar_url','labels','associated','pds','created_at','quality_score','hidden','deleted','version','followers_count','follows_count','posts_count','last_activity_at')
NATIVE_FIELDS = FIELDS[:12]
STAGES = ('rename','rescore','metadata','hide','show','delete','restore-deleted','pds-move','restore-baseline')

class BootstrapError(Exception):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def atomic(path, value):
    data=encoded(value)
    temporary=path.with_name(path.name+'.part')
    try:
        with temporary.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def canonical(path):
    path=Path(path).absolute()
    for item in [path,*path.parents]:
        if item.is_symlink():raise BootstrapError('owned_path_symlink')
    if path.resolve()!=path:raise BootstrapError('owned_path_not_canonical')
    return path


def observer_check(observer, nonce, fixture=False):
    required={'app','machine_id','guest','volume_ids','final_volume_gib','public_services','machines_api_response_sha256'}
    if not isinstance(observer,dict) or not required<=observer.keys():raise BootstrapError('observer_shape')
    if not isinstance(nonce,str) or not 16<=len(nonce)<=128 or not nonce.isascii():raise BootstrapError('run_nonce')
    if not observer['app'].startswith('typeahead-qual-') or observer['machine_id'] in full_prepare.PROTECTED:
        raise BootstrapError('protected_or_unowned_machine')
    if observer['guest']!={'cpu_kind':'shared','cpus':2,'memory_mb':1024} or observer['final_volume_gib']!=20 or observer['public_services']!=[]:
        raise BootstrapError('provider_shape')
    if len(observer['volume_ids'])!=1 or not re.fullmatch('[0-9a-f]{64}',observer['machines_api_response_sha256']):
        raise BootstrapError('provider_receipt')
    if not fixture and (os.environ.get('FLY_APP_NAME')!=observer['app'] or os.environ.get('FLY_MACHINE_ID')!=observer['machine_id']):
        raise BootstrapError('runtime_owner_mismatch')
    return {key:observer[key] for key in required} | ({'synthetic_local_fixture':True} if fixture else {})


def immutable(relative):
    if relative.as_posix()=='source/index.db.prefix':return True
    return relative.parts[:2]==('tantivy','index') and bool(re.fullmatch(r'[0-9a-f]{32}(?:\.[0-9]+)?\.(?:store|idx|term|pos|fast|fieldnorm|del)',relative.name))


def tree_witness(root):
    result={}
    for directory,dirs,files in os.walk(root):
        for name in dirs+files:
            path=Path(directory)/name;info=path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise BootstrapError('baseline_special_file')
            result[str(path.relative_to(root))]=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,stat.S_IMODE(info.st_mode))
    return result


def clone_baseline(baseline, destination, guard):
    canonical(baseline);canonical(destination)
    if destination.exists():raise BootstrapError('working_copy_exists')
    destination.mkdir(mode=0o700)
    counts={'hardlinked_immutable_files':0,'copied_mutable_files':0}
    try:
        for directory,dirs,files in os.walk(baseline):
            for name in dirs:
                source=Path(directory)/name
                if source.is_symlink():raise BootstrapError('baseline_symlink')
                (destination/source.relative_to(baseline)).mkdir(mode=0o700)
            for name in files:
                guard();source=Path(directory)/name;relative=source.relative_to(baseline);target=destination/relative
                if not stat.S_ISREG(source.lstat().st_mode):raise BootstrapError('baseline_special_file')
                if immutable(relative):
                    if stat.S_IMODE(source.stat().st_mode)&0o222:raise BootstrapError('mutable_baseline_segment')
                    os.link(source,target);counts['hardlinked_immutable_files']+=1
                else:
                    shutil.copyfile(source,target);target.chmod(0o600);counts['copied_mutable_files']+=1
        guard()
        return counts
    except BaseException:
        remove_working(destination)
        raise


def remove_working(path):
    canonical(path)
    if path.exists():
        # Never chmod files: immutable files can share an inode with baseline.
        for directory,dirs,_files in os.walk(path):
            Path(directory).chmod(0o700)
            if any((Path(directory)/name).is_symlink() for name in dirs):raise BootstrapError('working_symlink')
        shutil.rmtree(path)


def export_module(path):
    spec=importlib.util.spec_from_file_location('qualification_raw_export',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def generate_feed(manifest, source, exporter_path, deadline, count=64):
    if count!=64:raise BootstrapError('fixed_affected_actor_count')
    exporter=export_module(exporter_path)
    budget=exporter.Budget(SimpleNamespace(max_seconds=max(.1,min(60,deadline-time.time()))))
    selected=[];scanned=0
    with (source/'index.db.prefix').open('rb') as stream:
        flat=exporter.FlatActors(stream,budget,64*1024*1024)
        for did,blob in flat:
            scanned+=1
            if len(blob)<=MAX_ACTOR:
                raw=exporter.unpack_actor(did,blob,manifest['source_watermark'])
                actor={key:raw[key] for key in FIELDS}
                if actor['handle'] and len(encoded(actor))<=MAX_ACTOR:selected.append(actor)
            if len(selected)==count:break
            if scanned>=4096:raise BootstrapError('bounded_actor_sample_exhausted')
    if len(selected)!=count:raise BootstrapError('insufficient_actor_sample')
    with closing(sqlite3.connect((source/'index.db').as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        hosts=[row[0] for row in db.execute('SELECT host FROM pds_scopes WHERE enabled=1 ORDER BY host LIMIT 2')]
    if len(hosts)!=2 or any(not isinstance(host,str) or not host or len(host)>253 for host in hosts):
        raise BootstrapError('two_admitted_pds_targets_required')
    token=manifest['prefix_map']['sha256'][:12]
    current=copy.deepcopy(selected);batches=[];visible=None
    for cursor,name in enumerate(STAGES,1):
        if name=='restore-baseline':current=copy.deepcopy(selected)
        elif name=='restore-deleted':current=copy.deepcopy(visible)
        for index,actor in enumerate(current):
            if name=='rename':actor.update(handle=f'qual{token}-{index}.qualification.test',display_name=f'Qualification {token} Actor {index}')
            elif name=='rescore':actor['quality_score']=100000+index if actor['quality_score']!=100000+index else 200000+index
            elif name=='metadata':actor.update(avatar_url='',labels=[{'val':'qualification-fixture'}],associated={'qualification':{'token':token}},created_at='2026-10-29T00:00:00Z')
            elif name=='hide':actor['hidden']=True
            elif name=='show':actor['hidden']=False
            elif name=='delete':actor['deleted']=True
            elif name=='pds-move':
                raw=actor['pds']
                host=urllib.parse.urlsplit(raw if '://' in raw else 'https://'+raw).hostname
                actor['pds']='https://'+(hosts[1] if host==hosts[0] else hosts[0])
            actor['version']=manifest['source_watermark']+cursor
        if name=='show':visible=copy.deepcopy(current)
        batches.append({'batch_id':name,'cursor':cursor,'expected_cursor':cursor-1,'actors':copy.deepcopy(current)})
    feed={'schema_version':1,'build_id':manifest['active_build'],'source_watermark':manifest['source_watermark'],
          'source_kind':'frozen-materialized-hidden','baseline_actors':selected,'batches':batches,
          'replay_batch_ids':['rename','restore-baseline']}
    data=encoded(feed)
    if len(data)>MAX_FEED:raise BootstrapError('feed_byte_budget')
    return feed,data,hashlib.sha256(data).hexdigest()


class FullQualification:
    def __init__(self,root,manifest,arm,observer,nonce,deadline,*,app='/app',fixture=False):
        now=time.time()
        if not math.isfinite(deadline) or not now<deadline<=now+5400:raise BootstrapError('overall_deadline')
        self.root=canonical(root);self.app=Path(app);self.manifest=full_prepare.manifest_check(manifest);self.arm=arm
        self.observer=observer_check(observer,nonce,fixture);self.nonce=nonce;self.fixture=fixture
        if arm not in {'native','tantivy'}:raise BootstrapError('arm')
        if not fixture and self.root.parent!=Path('/data/qualification'):raise BootstrapError('owned_scratch_required')
        self.deadline=deadline;self.preparation_deadline=min(deadline-20,now+3600);self.started=now
        self.baseline=self.root/'baseline';self.working=self.root/'working';self.phase_path=self.root/'phase.json'
        self.lock=threading.RLock();self.child=None;self.prepared=False;self.reset_count=0;self.failures=[];self.preparation=None
        self.state='preparing';self.feed=None;self.feed_bytes=None;self.feed_sha=None;self.baseline_proof=None
        self.admin_port=18081;self.metrics_calls=0;self.oracle_ready=False;self.oracle_sha=None;self.cases_sha=None;self.children=[];self.telemetry=None

    def guard(self,preparing=False):
        if time.time()>=(self.preparation_deadline if preparing else self.deadline-15):raise BootstrapError('guest_deadline')
        location=self.root if self.root.exists() else self.root.parent
        if shutil.disk_usage(location).free<GIB:raise BootstrapError('disk_reserve')
        if self.root.exists():
            canonical(self.root)
            if self.root.is_symlink() or (self.root/'owner').read_text()!=OWNER:raise BootstrapError('owner_changed')
            total=0;seen=set()
            for directory,dirs,files in os.walk(self.root):
                for name in dirs+files:
                    try:info=(Path(directory)/name).lstat()
                    except FileNotFoundError:continue
                    if stat.S_ISLNK(info.st_mode):raise BootstrapError('scratch_symlink')
                    if stat.S_ISREG(info.st_mode) and (info.st_dev,info.st_ino) not in seen:
                        total+=info.st_size;seen.add((info.st_dev,info.st_ino))
            if total>19*GIB:raise BootstrapError('unique_inode_payload_budget')
            log=self.root/'engine.log'
            if log.exists() and log.stat().st_size>4*1024*1024:raise BootstrapError('engine_log_budget')

    def prepare(self,access):
        if self.arm=='native' and not self.fixture:
            expected=os.environ.get('QUAL_ORACLE_BINARY_SHA256','')
            if not re.fullmatch('[0-9a-f]{64}',expected) or sha(self.app/'tantivy-raw-oracle')!=expected:
                raise BootstrapError('oracle_binary_binding')
        self.preparation=full_prepare.Preparation(self.root,self.manifest,self.arm,self.preparation_deadline,
            app=self.app,allow_loopback_fixture=self.fixture)
        self.preparation.run(access)
        self.admit_prepared()

    def admit_prepared(self):
        self.guard(True)
        record=json.loads((self.baseline/'prepared.json').read_text())
        if record.get('prepared') is not True or record.get('arm')!=self.arm or record.get('build_id')!=self.manifest['active_build'] or record.get('source_database_sha256')!=self.manifest['sha256'] or record.get('source_prefix_sha256')!=self.manifest['prefix_map']['sha256']:
            raise BootstrapError('prepared_baseline_binding')
        self.baseline_proof=tree_witness(self.baseline)
        self.feed,self.feed_bytes,self.feed_sha=generate_feed(self.manifest,self.baseline/'source',self.app/'input/export.py',self.preparation_deadline)
        path=self.root/'updates.json'
        with path.open('xb') as stream:stream.write(self.feed_bytes);stream.flush();os.fsync(stream.fileno())
        path.chmod(0o400)
        atomic(self.root/'observer.json',self.observer)
        self.clone(True)
        if self.arm=='native' and not self.fixture:self.prepare_oracle()
        self.prepared=True;self.state='prepared'

    def wait_preparation(self,children):
        self.children.extend(child for child in children if child not in self.children)
        try:
            while any(child.poll() is None for child in children):
                self.guard(True)
                if any(child.poll() not in {None,0} for child in children):raise BootstrapError('oracle_pipeline_failed')
                time.sleep(.05)
            if any(child.returncode for child in children):raise BootstrapError('oracle_pipeline_failed')
        except BaseException:
            self.stop_children();raise

    def stop_children(self):
        for child in reversed(self.children):
            if child.poll() is None:
                try:os.killpg(child.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:os.killpg(child.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    child.wait(timeout=2)
        self.children=[]

    def prepare_oracle(self,fixture_cases=None):
        if self.arm!='native':raise BootstrapError('oracle_native_arm_required')
        if fixture_cases is not None and not self.fixture:raise BootstrapError('fixed_oracle_case_count')
        cases_count=512 if fixture_cases is None else fixture_cases
        self.guard(True);source=self.baseline/'source';sample=self.root/'oracle-sample.jsonl'
        exporter=export_module(self.app/'input/export.py')
        header={'type':'manifest','schema_version':1,'build_id':self.manifest['active_build'],
            'watermark':str(self.manifest['source_watermark']),'source_watermark':self.manifest['source_watermark'],
            'actor_count':self.manifest['actor_count'],'source_actor_count':self.manifest['actor_count'],
            'normalize_version':self.manifest['normalize_version'],'scoring_version':self.manifest['scoring_version'],
            'version_kind':'snapshot_watermark','source_format':'FLATPX05/actor-v1',
            'source_prefix_sha256':self.manifest['prefix_map']['sha256'],
            'source_database_sha256':self.manifest['sha256'],'partial':False}
        try:
            with sample.open('xb') as output,(source/'index.db.prefix').open('rb') as stream:
                output.write(exporter.jsonline(header));budget=exporter.Budget(SimpleNamespace(max_seconds=min(120,self.preparation_deadline-time.time())))
                for index,(did,blob) in enumerate(exporter.FlatActors(stream,budget,128*1024*1024)):
                    if index>=10000:break
                    data=exporter.jsonline(exporter.unpack_actor(did,blob,self.manifest['source_watermark']))
                    if output.tell()+len(data)>128*1024*1024:raise BootstrapError('oracle_sample_budget')
                    output.write(data)
            env={key:value for key,value in os.environ.items() if key in {'PATH','HOME','LANG'}}
            oracle=self.app/'tantivy-raw-oracle';policy=self.app/'identity-quarantine.json'
            with closing(sqlite3.connect((source/'index.db').as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
                hosts=[row[0] for row in db.execute('SELECT host FROM pds_scopes WHERE enabled=1 ORDER BY host LIMIT 100001')]
            roster={'schema_version':1,'build_id':self.manifest['active_build'],'source_watermark':self.manifest['source_watermark'],'hosts':hosts}
            if len(hosts)>100000 or len(encoded(roster))>32*1024*1024:raise BootstrapError('native_scope_roster_budget')
            atomic(self.root/'native-scopes.json',roster);del hosts,roster
            with (self.root/'oracle-freeze.log').open('xb') as log:
                child=subprocess.Popen([str(oracle),'freeze','--input',str(sample),'--output',str(self.root/'cases.json'),
                    '--identity-policy',str(policy),'--cases',str(cases_count),'--sample-actors','10000',
                    '--updates',str(self.root/'updates.json'),'--native-scopes',str(self.root/'native-scopes.json')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
                self.wait_preparation([child])
        finally:sample.unlink(missing_ok=True)
        if (self.root/'cases.json').stat().st_size>4*1024*1024:raise BootstrapError('oracle_cases_budget')
        seconds=max(1,min(1800,int(self.preparation_deadline-time.time())))
        with (self.root/'oracle-export.log').open('xb') as export_log,(self.root/'oracle-run.log').open('xb') as oracle_log:
            producer=subprocess.Popen([sys.executable,str(self.app/'input/export.py'),'--manifest',str(source/'manifest.json'),
                '--database',str(source/'index.db'),'--prefix',str(source/'index.db.prefix'),'--build-id',self.manifest['active_build'],
                '--scratch-root',str(self.root),'--output','-','--receipt',str(self.root/'oracle-export-receipt.json'),
                '--max-seconds',str(seconds)],env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
            self.children.append(producer)
            try:
                consumer=subprocess.Popen([str(oracle),'run','--input','-','--queries',str(self.root/'cases.json'),'--updates',str(self.root/'updates.json'),
                    '--output',str(self.root/'oracle.json'),'--identity-policy',str(policy),'--source-kind','frozen-materialized-hidden',
                    '--max-seconds',str(seconds),'--max-rss-mb','256'],env=env,stdin=producer.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
                producer.stdout.close();self.wait_preparation([producer,consumer])
            except BaseException:self.stop_children();raise
        path=self.root/'oracle.json'
        if path.stat().st_size>64*1024*1024:raise BootstrapError('oracle_output_budget')
        value=json.loads(path.read_bytes());receipt=value.get('receipt',{})
        cases=json.loads((self.root/'cases.json').read_bytes())
        exported=json.loads((self.root/'oracle-export-receipt.json').read_bytes())
        if receipt.get('source_build_id')!=self.manifest['active_build'] or receipt.get('source_watermark')!=self.manifest['source_watermark'] or receipt.get('actors_checked')!=self.manifest['actor_count'] or receipt.get('updates_sha256')!=self.feed_sha or receipt.get('queries_sha256')!=sha(self.root/'cases.json') or receipt.get('actor_stream_sha256')!=exported.get('actors_sha256') or [c.get('cursor') for c in value.get('checkpoints',[])]!=list(range(10)) or len(cases.get('cases',[]))!=cases_count:
            raise BootstrapError('oracle_source_binding')
        for name in ['cases.json','oracle.json']:(self.root/name).chmod(0o400)
        self.cases_sha=sha(self.root/'cases.json');self.oracle_sha=sha(path);self.oracle_ready=True

    def baseline_intact(self):
        if self.baseline_proof!=tree_witness(self.baseline):raise BootstrapError('baseline_changed')

    def clone(self,preparing=False):
        self.baseline_intact();self.clone_metrics=clone_baseline(self.baseline,self.working,lambda:self.guard(preparing))
        if self.arm=='native':
            env={key:value for key,value in os.environ.items() if key in {'PATH','HOME','LANG'}}
            env.update(TYPEAHEAD_ADMIT_PREFIX_PATH=str(self.working/'source/index.db.prefix'),TYPEAHEAD_ADMIT_PREFIX_SHA256=self.manifest['prefix_map']['sha256'])
            subprocess.run([str(self.app/'bin/admit-prefix')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=max(.1,min(120,self.preparation_deadline-time.time() if preparing else self.deadline-time.time()-20)))
            prefix=self.working/'source/index.db.prefix';cache=prefix.with_name(prefix.name+'.admitted')
            if not cache.exists() or int.from_bytes(cache.read_bytes()[72:88],'little',signed=True)!=prefix.stat().st_ctime_ns:raise BootstrapError('native_prefix_revalidation')
        self.baseline_intact()
        atomic(self.phase_path,{'schema':1,'acked':0,'inflight':None})
        self.config={'schema':1,'arm':self.arm,'root':str(self.root),'app':self.observer['app'],'machine_id':self.observer['machine_id'],
            'guest':self.observer['guest'],'volume_id':self.observer['volume_ids'][0],'final_volume_gib':20,'nonce':self.nonce,
            'corpus_sha256':hashlib.sha256(encoded({'build_id':self.manifest['active_build'],'database_sha256':self.manifest['sha256'],'prefix_sha256':self.manifest['prefix_map']['sha256']})).hexdigest(),
            'updates_sha256':self.feed_sha,'observer_receipt':str(self.root/'observer.json'),'private_listen':'::',
            'source_kind':'frozen-release-snapshot','allow_stale':True,'allow_partial':False,'measurement_deadline_s':900,
            'engine_settings':{'retrieval_strategy':'global-merge','writer_memory_mb':128,'concurrency':4,'max_candidates':10000},
            'paths':{key:str(value) for key,value in {'snapshot':self.working/'source/index.db','overlay':self.working/'overlay.db',
                'tmp':self.working/'tmp','request':self.working/'request.json','response':self.working/'response.json',
                'tantivy_data':self.working/'tantivy','phase_state':self.phase_path}.items()}}
        atomic(self.root/'run.json',self.config)

    def phase(self):
        if self.phase_path.stat().st_size>1024:raise BootstrapError('phase_budget')
        value=json.loads(self.phase_path.read_bytes())
        if set(value)!={'schema','acked','inflight'} or value['schema']!=1 or type(value['acked']) is not int or not 0<=value['acked']<=len(STAGES) or value['inflight'] not in {None,value['acked']+1}:raise BootstrapError('phase_state')
        return value

    def entrypoint_argv(self):return [sys.executable,str(self.app/'entrypoint.py'),'serve','--run',str(self.root/'run.json')]

    def engine_env(self):
        env={key:value for key,value in os.environ.items() if key in {'PATH','HOME','LANG','TMPDIR','FLY_APP_NAME','FLY_MACHINE_ID','FLY_MACHINE_VERSION'}}
        if self.arm=='tantivy':
            secret=os.environ.get('ADMIN_SECRET')
            if not secret or '\n' in secret or '\r' in secret:raise BootstrapError('loopback_admin_capability')
            env['ADMIN_SECRET']=secret
        return env

    def serve(self):
        self.guard()
        if not self.prepared:raise BootstrapError('not_prepared')
        if self.child and self.child.poll() is None:return {'serving':True,'already_started':True}
        remaining=int(self.deadline-time.time())-20
        if remaining<10:raise BootstrapError('serve_lease_exhausted')
        self.config['measurement_deadline_s']=min(900,remaining);atomic(self.root/'run.json',self.config)
        self.log=(self.root/'engine.log').open('ab')
        self.log.write(encoded({'event':'engine_start','arm':self.arm,'epoch':int(time.time())})+b'\n');self.log.flush()
        self.child=subprocess.Popen(self.entrypoint_argv(),env=self.engine_env(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.state='serving';return {'serving':True,'measurement_deadline_s':self.config['measurement_deadline_s']}

    def pause(self):
        if self.child and self.child.poll() is None:
            os.killpg(self.child.pid,signal.SIGTERM)
            try:self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(self.child.pid,signal.SIGKILL);self.child.wait(timeout=2)
        if hasattr(self,'log'):self.log.close()
        self.state='paused' if self.prepared else self.state
        return {'serving':False,'acked':self.phase()['acked'] if self.prepared else 0}

    def native_cursor(self):
        with closing(sqlite3.connect((self.working/'overlay.db').as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            row=db.execute("SELECT value FROM sync_meta WHERE key='qualification_cursor'").fetchone()
        if row is None:return 0
        value=json.loads(row[0])
        if value.get('feed_sha256')!=self.feed_sha:raise BootstrapError('native_feed_identity')
        return value['cursor']

    def admin(self,path,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.admin_port,timeout=10)
        try:
            secret=os.environ.get('ADMIN_SECRET','')
            conn.request('GET' if body is None else 'POST',path,None if body is None else encoded(body),
                {'Content-Type':'application/json','Authorization':'Bearer '+secret})
            response=conn.getresponse();raw=response.read(262145)
            if len(raw)>262144:raise BootstrapError('admin_response_budget')
            value=json.loads(raw)
            if response.status!=200:raise BootstrapError('fixed_batch_rejected')
            return value
        finally:conn.close()

    def cursor_value(self,batch):
        return {'schema':1,'feed_sha256':self.feed_sha,'seq':batch['cursor'],'batch_sha256':hashlib.sha256(encoded(batch)).hexdigest()}

    def apply(self,cursor):
        self.guard()
        if not self.prepared or not self.child or self.child.poll() is not None:raise BootstrapError('engine_not_serving')
        if type(cursor) is not int or not 1<=cursor<=len(self.feed['batches']):raise BootstrapError('unknown_fixed_batch')
        batch=self.feed['batches'][cursor-1];phase=self.phase()
        if cursor not in {phase['acked'],phase['acked']+1}:raise BootstrapError('out_of_order_batch')
        replay=cursor==phase['acked']
        if not replay:atomic(self.phase_path,{'schema':1,'acked':phase['acked'],'inflight':cursor})
        if self.arm=='native':
            current=self.native_cursor()
            if current not in {cursor-1,cursor}:raise BootstrapError('native_cursor_conflict')
            actors=[{key:(json.dumps(a[key],ensure_ascii=False,separators=(',',':')) if key in {'labels','associated'} else a[key]) for key in NATIVE_FIELDS} for a in batch['actors']]
            payload={'feed_sha256':self.feed_sha,'cursor':cursor,'expected_cursor':cursor-1,'actors':actors}
            inbox=self.working/'request.json';response=self.working/'response.json'
            if inbox.exists():raise BootstrapError('native_inbox_busy')
            response.unlink(missing_ok=True);atomic(inbox,payload)
            until=min(self.deadline-15,time.time()+15)
            while inbox.exists():
                self.guard()
                if time.time()>until:raise BootstrapError('native_ack_deadline')
                if self.child.poll() is not None:raise BootstrapError('native_writer_stopped')
                time.sleep(.02)
            value=json.loads(response.read_bytes())
            if value.get('accepted') is not True or value.get('cursor')!=cursor:raise BootstrapError('native_batch_rejected')
        else:
            status=self.admin('/index/status');actual=status.get('source_cursor');wanted=self.cursor_value(batch)
            if actual==wanted and not replay:value={'ack_recovered':True,'cursor':cursor}
            else:
                previous=None if cursor==1 else self.cursor_value(self.feed['batches'][cursor-2])
                expected=wanted if replay else previous
                if actual!=expected:raise BootstrapError('tantivy_cursor_conflict')
                value=self.admin('/admin/apply',{'actors':batch['actors'],'source_cursor':wanted,'expected_source_cursor':expected})
                if value.get('source_cursor')!=wanted:raise BootstrapError('tantivy_ack_cursor')
        atomic(self.phase_path,{'schema':1,'acked':cursor,'inflight':None})
        atomic(self.root/'last-ack.json',{'cursor':cursor,'batch_id':batch['batch_id'],'replay':replay,'receipt':value})
        return {'acked':cursor,'batch_id':batch['batch_id'],'replay':replay,'receipt':value}

    def reset(self):
        if not self.prepared:raise BootstrapError('not_prepared')
        self.pause();self.baseline_intact();remove_working(self.working);self.clone();self.reset_count+=1
        return {'reset':True,'acked':0,'reset_count':self.reset_count,'serving':False}

    def metrics(self):
        self.metrics_calls+=1
        result=self.summary()|{'metrics_calls':self.metrics_calls,'free_bytes':shutil.disk_usage(self.root if self.root.exists() else self.root.parent).free}
        if self.telemetry is None:
            from telemetry import Sampler
            paths=[self.working/'tantivy/index'] if self.arm=='tantivy' else [self.working/'overlay.db']
            self.telemetry=Sampler(index_paths=paths)
        pids=[os.getpid()]
        if self.child and self.child.poll() is None:pids.append(self.child.pid)
        result['telemetry']=self.telemetry.snapshot(pids=pids,include_descendants=True)
        return result

    def summary(self):
        value={'schema_version':1,'mode':'full','arm':self.arm,'state':self.state,'prepared':self.prepared,'nonce':self.nonce,
            'app':self.observer['app'],'machine_id':self.observer['machine_id'],'guest':self.observer['guest'],
            'final_volume_gib':self.observer['final_volume_gib'],'public_services':self.observer['public_services'],
            'volume_id':self.observer['volume_ids'][0],'build_id':self.manifest['active_build'],'source_watermark':self.manifest['source_watermark'],
            'oracle_ready':self.oracle_ready,'cases_sha256':self.cases_sha,'oracle_sha256':self.oracle_sha,
            'actor_count':self.manifest['actor_count'],'updates_sha256':self.feed_sha,'affected_actors':64,'feed_batches':9,'reset_count':self.reset_count,
            'source_kind':'frozen-release-snapshot','source_ready_attestation':False,'performance_qualified':False,
            'native_overlay_baseline':'fresh-empty-independent' if self.arm=='native' else None,'overall_deadline':self.deadline,'failures':list(self.failures[-4:])}
        if self.prepared:value.update(acked=self.phase()['acked'],clone=self.clone_metrics,corpus_sha256=self.config['corpus_sha256'],observer_receipt_sha256=sha(self.root/'observer.json'))
        return value

    def close(self):
        with self.lock:self.pause()
        self.stop_children()
        if self.preparation:self.preparation.stop()


def control_handler(service,secret):
    if not isinstance(secret,str) or not 16<=len(secret)<=1024 or '\r' in secret or '\n' in secret or secret==service.nonce or secret==os.environ.get('ADMIN_SECRET'):raise BootstrapError('control_capability')
    class Control(http.server.BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def setup(self):super().setup();self.connection.settimeout(3)
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
                if line in {b'\r\n',b'\n',b''}:break
            self.rfile=io.BytesIO(headers)
            try:return super().parse_request()
            finally:self.rfile=original
        def emit(self,status,value):
            data=encoded(value)
            if len(data)>MAX_FEED:raise BootstrapError('control_output_budget')
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close');self.end_headers();self.wfile.write(data);self.close_connection=True
        def authorized(self):return hmac.compare_digest(self.headers.get('X-Pilot-Authorization',''),secret)
        def dispatch(self,action):
            try:
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0')!='0':self.emit(400,{'error':'empty_control_body_required'});return
                if action=='prepared':self.emit(200,service.summary());return
                if not self.authorized():self.emit(403,{'error':'control_denied'});return
                if action=='metrics':self.emit(200,service.metrics());return
                if action in {'updates','cases','oracle'}:
                    if not service.prepared:raise BootstrapError('not_prepared')
                    filename,expected,limit={'updates':('updates.json',service.feed_sha,MAX_FEED),'cases':('cases.json',service.cases_sha,4*1024*1024),'oracle':('oracle.json',service.oracle_sha,64*1024*1024)}[action]
                    path=canonical(service.root/filename)
                    if not expected or path.stat().st_size>limit or sha(path)!=expected:raise BootstrapError('fixed_input_changed')
                    data=path.read_bytes()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close');self.end_headers();self.wfile.write(data);self.close_connection=True;return
                if not service.lock.acquire(blocking=False):self.emit(409,{'error':'control_busy'});return
                try:
                    if action=='serve':value=service.serve()
                    elif action=='pause':value=service.pause()
                    elif action=='restart':service.pause();value=service.serve()
                    elif action=='reset':value=service.reset()
                    elif action=='metrics':value=service.metrics()
                    elif isinstance(action,int):value=service.apply(action)
                    else:self.emit(404,{'error':'unknown_control'});return
                finally:service.lock.release()
                self.emit(200,value)
            except BootstrapError as error:self.emit(503,{'error':str(error)})
            except Exception as error:self.emit(503,{'error':'bounded_control_'+type(error).__name__})
        def do_GET(self):self.dispatch({'/prepared':'prepared','/metrics':'metrics','/inputs/updates':'updates','/inputs/cases':'cases','/inputs/oracle':'oracle'}.get(self.path,'unknown'))
        def do_POST(self):
            action={'/serve':'serve','/pause':'pause','/restart':'restart','/reset':'reset','/metrics':'metrics'}.get(self.path)
            if action is None and re.fullmatch(r'/apply/[1-9]',self.path):action=int(self.path.rsplit('/',1)[1])
            self.dispatch(action or 'unknown')
        def log_message(self,*_):pass
    return Control


class ControlServer(http.server.ThreadingHTTPServer):
    address_family=socket.AF_INET6
    daemon_threads=True
    request_queue_size=8
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(4)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):request.close();return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


def main():
    if os.environ.get('QUAL_MODE')!='full':raise BootstrapError('explicit_full_mode_required')
    role=os.environ.get('QUAL_ROLE');arm={'native-control':'native','tantivy-control':'tantivy'}.get(role)
    if arm is None:raise BootstrapError('engine_role_required')
    observer=json.loads(os.environ['QUAL_OBSERVER_JSON']);manifest=json.loads(Path('/app/source-manifest.json').read_text())
    service=FullQualification(os.environ.get('QUAL_ROOT','/data/qualification/run'),manifest,arm,observer,os.environ['QUAL_PILOT_NONCE'],float(os.environ['QUAL_DEADLINE']))
    parent=canonical(service.root.parent);parent.mkdir(mode=0o700,exist_ok=True)
    if not parent.is_dir():raise BootstrapError('owned_scratch_parent')
    server=ControlServer(('::',8082),control_handler(service,os.environ['QUAL_CONTROL_SECRET']))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def interrupted(*_):raise BootstrapError('guest_interrupted')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    signal.signal(signal.SIGALRM,interrupted);signal.setitimer(signal.ITIMER_REAL,max(.001,service.deadline-time.time()-15))
    try:
        access=json.loads(os.environ.pop('QUAL_SOURCE_ACCESS'));service.prepare(access);del access
        print(json.dumps({'event':'FULL_PREPARED',**service.summary()}),flush=True)
        while time.time()<service.deadline-20:
            service.guard()
            if service.child and service.child.poll() is not None and service.state=='serving':
                service.state='paused';service.failures.append({'error_class':'runtime_terminal','returncode':service.child.returncode})
            time.sleep(.5)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2);service.close()

if __name__=='__main__':
    try:main()
    except BaseException as error:
        code=str(error) if isinstance(error,(BootstrapError,full_prepare.PreparationError)) else 'full_bootstrap_failed'
        print(json.dumps({'event':'FULL_BOOTSTRAP_TERMINAL','error_class':code,'source_ready_attestation':False}),flush=True)
        raise SystemExit(1) from None
