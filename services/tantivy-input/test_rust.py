#!/usr/bin/env python3
"""Small real-Rust integration: synthetic source HTTP/SQLite, import, replay, restart."""
import argparse
import importlib.util
import hashlib
import shutil
import json
import os
import resource
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent

def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

def run(binary,scratch_root,policy_checks=False):
    fx=module('tiny_cloud_fixture','fixture.py');test=module('readonly_source_fixture','test_sync.py');sync=module('actual_source_replay','sync.py')
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    with tempfile.TemporaryDirectory(dir=scratch_root,prefix='.test-rust-sync-') as temp:
        p=Path(temp);private_binary=p/'typeahead-tantivy';shutil.copyfile(binary,private_binary);private_binary.chmod(0o700);binary=private_binary;binary_sha=hashlib.sha256(binary.read_bytes()).hexdigest();source=test.Fixture(p,now=int(time.time()));floor=source.floor
        try:
            payload,_=fx.fixture('fixture-native-http-replay',floor);(p/'input.jsonl').write_bytes(payload)
            imported=subprocess.run([str(binary),'import','--data',str(p/'index'),'--input',str(p/'input.jsonl'),'--writer-memory-mb','32','--batch-size','4'],capture_output=True,text=True,timeout=60)
            if imported.returncode:raise RuntimeError('Rust synthetic import failed')
            ports=[]
            for _ in range(2):
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));ports.append(sock.getsockname()[1])
            env={k:v for k,v in os.environ.items() if k in ['PATH','SYSTEMROOT','TMPDIR','LANG']};env['ADMIN_SECRET']='fixture-local-replay-secret'
            public=f'http://127.0.0.1:{ports[0]}';admin_url=f'http://127.0.0.1:{ports[1]}'
            def start(crash=None):
                child_env=env.copy()
                if crash is not None:child_env['TYPEAHEAD_TEST_CRASH']=crash
                proc=subprocess.Popen([str(binary),'serve','--data',str(p/'index'),'--listen',f'127.0.0.1:{ports[0]}','--admin-listen',f'127.0.0.1:{ports[1]}','--writer-memory-mb','32','--allow-partial','true','--allow-stale','true',*(['--retrieval',os.environ['TA_RETRIEVAL_STRATEGY']] if 'TA_RETRIEVAL_STRATEGY' in os.environ else [])],stdout=subprocess.DEVNULL,stderr=(p/'server.log').open('ab'),env=child_env)
                try:
                    for _ in range(100):
                        if proc.poll() is not None:raise RuntimeError('Rust fixture server exited')
                        try:
                            with urllib.request.urlopen(public+'/index/status',timeout=.5) as response:json.load(response)
                            return proc
                        except OSError:time.sleep(.05)
                    raise RuntimeError('Rust fixture server readiness timeout')
                except BaseException:stop(proc);raise
            def stop(proc):
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
            proc=start()
            try:
                source.actor('did:example:replay-added',floor+10,handle='new-cloud.fixture.test')
                source.actor('did:example:fixture-local-joint',floor+11,handle='')
                source.tombstone('did:example:fixture-remote-joint',floor+12)
                source.tombstone('did:example:fixture-local-joint',floor+11)
                source.actor('did:example:live-equal',floor+13,handle='equal-live.fixture.test');source.tombstone('did:example:live-equal',floor+13)
                source.actor('did:example:hidden-equal',floor+14,handle='hidden-equal.fixture.test',hidden=True);source.tombstone('did:example:hidden-equal',floor+14)
                source.actor('did:example:older-live',floor+15,handle='older-live.fixture.test');source.tombstone('did:example:older-live',floor+16)
                admin=sync.Admin(admin_url,env['ADMIN_SECRET'],public+'/index/status')
                result=sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run()
                assert result['caught_up'] and result['superseded_tombstones']==2 and result['replayed']>=1
                before=admin.status();saved=before['source_cursor'];assert saved['through']==source.now-2 and saved['lookback_seconds']==300
                http=sync.Http(5)
                body=http.request(public+'/search?q=new-cloud',env['ADMIN_SECRET'])[0]
                assert [a['did'] for a in body['actors']]==['did:example:replay-added']
                assert http.request(public+'/search?q=Justin%20Fa',env['ADMIN_SECRET'])[0]['actors']==[]
                assert http.request(public+'/search?q=equal-live',env['ADMIN_SECRET'])[0]['actors'][0]['did']=='did:example:live-equal'
                assert http.request(public+'/search?q=hidden-equal',env['ADMIN_SECRET'])[0]['actors']==[]
                assert http.request(public+'/search?q=older-live',env['ADMIN_SECRET'])[0]['actors']==[]
                assert before['actor_count']==8 and before['tombstone_count']==4
                stop(proc);proc=start();assert admin.status()['source_cursor']==saved
                assert http.request(public+'/search?q=new-cloud',env['ADMIN_SECRET'])[0]['actors'][0]['did']=='did:example:replay-added'
                resumed=sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run()
                assert resumed['caught_up'] and resumed['applied']==0 and resumed['replayed']>=3
                after=admin.status();assert after['indexed_memberships']==before['indexed_memberships'] and after['actor_count']==before['actor_count']
                # Legacy opaque cursors without a policy marker upgrade on a CAS using the exact old value.
                current=admin.status()['source_cursor'];legacy=json.loads(json.dumps(current));legacy.pop('source_policy')
                admin.apply([],current,legacy)
                upgraded=sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run()
                assert upgraded['caught_up'] and upgraded['applied']==0 and admin.status()['source_cursor']['source_policy']==sync.SOURCE_POLICY
                # The current source actor can be newer than the active cycle's fixed cutoff.
                t=admin.status()['source_cursor']['through']
                source.actor('did:example:replay-added',t+20,handle='new-cloud.fixture.test');source.tombstone('did:example:replay-added',t+10);source.now=t+17
                bounded=sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run()
                assert bounded['caught_up'] and bounded['through']==t+15 and http.request(public+'/search?q=new-cloud',env['ADMIN_SECRET'])[0]['actors']==[]
                source.now=t+25
                restored=sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run()
                assert restored['caught_up'] and http.request(public+'/search?q=new-cloud',env['ADMIN_SECRET'])[0]['actors'][0]['did']=='did:example:replay-added'
                # A new source transaction can arrive below the prior cutoff but within the declared overlap.
                source.actor('did:example:late-added',saved['through']-20,handle='late-cloud.fixture.test');source.now+=10
                late=sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run();assert late['caught_up'] and late['applied']==1
                assert http.request(public+'/search?q=late-cloud',env['ADMIN_SECRET'])[0]['actors'][0]['did']=='did:example:late-added'
                # A genuinely later equal-timestamp restore AFTER an applied deletion still fails closed.
                conflict_did='did:example:late-equal-conflict';stamp=source.now-10;source.tombstone(conflict_did,stamp)
                sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run();prior=admin.status()['source_cursor']
                source.actor(conflict_did,stamp,handle='late-equal-conflict.fixture.test');source.now+=5
                try:
                    sync.Synchronizer(source.source(),admin,page_size=2,max_pages=20,max_seconds=30,retries=1,backoff=0).run()
                    raise AssertionError('late equal-version restore unexpectedly accepted')
                except sync.CursorConflict:
                    stopped=admin.status()['source_cursor'];assert stopped['through']==prior['through'] and stopped['actors']['updated_at']<stamp
                # Migrate the stalled legacy cursor through a separate source-owned observation API.
                current_state=module('actual_current_state','current_state.py')
                def observe(anti_pages=200):
                    return current_state.ObservedSynchronizer(source.source(),admin,sync,page_size=2,max_pages=100,max_seconds=30,retries=1,backoff=0,anti_entropy_pages=anti_pages,sweep_cooldown_seconds=0).run()
                observed=observe()
                assert observed['caught_up'] and observed['source_policy']==current_state.SOURCE_POLICY
                assert http.request(public+'/search?q=late-equal-conflict',env['ADMIN_SECRET'])[0]['actors'][0]['did']==conflict_did
                assert admin.status()['source_observation_revision']>0
                assert observed['reconciliation']['last_completed_at'] is not None
                # Source current-state authority resolves same-second hide/show and unversioned activity changes.
                same_did='did:example:late-equal-conflict'
                from contextlib import closing
                import sqlite3
                with closing(sqlite3.connect(source.path)) as db,db:
                    db.execute('UPDATE actors SET hidden=1,last_activity_at=99 WHERE did=?',[same_did])
                observe()
                assert http.request(public+'/search?q=late-equal-conflict',env['ADMIN_SECRET'])[0]['actors']==[]
                with closing(sqlite3.connect(source.path)) as db,db:
                    db.execute('UPDATE actors SET hidden=0,last_activity_at=100 WHERE did=?',[same_did])
                observe()
                assert http.request(public+'/search?q=late-equal-conflict',env['ADMIN_SECRET'])[0]['actors'][0]['did']==same_did
                # A backdated new actor absent from the timestamp window is found by the source-DID walk.
                source.actor('did:example:backdated-new',floor-10,handle='backdated-new.fixture.test')
                observe()
                assert http.request(public+'/search?q=backdated-new',env['ADMIN_SECRET'])[0]['actors'][0]['did']=='did:example:backdated-new'
                # A source deletion without any retained tombstone is found by the backend-DID walk.
                with closing(sqlite3.connect(source.path)) as db,db:
                    db.execute('DELETE FROM actors WHERE did=?',[same_did]);db.execute('DELETE FROM tombstones WHERE did=?',[same_did])
                observe()
                assert http.request(public+'/search?q=late-equal-conflict',env['ADMIN_SECRET'])[0]['actors']==[]
                # Changes between actor-drain and tombstone-read are projected by fresh current-state lookup.
                race_did='did:example:between-streams';race_stamp=source.now-10
                source.tombstone(race_did,race_stamp)
                base=source.source()
                class RaceSource:
                    def __getattr__(self,name):return getattr(base,name)
                    def select(self,sql,args):
                        rows=base.select(sql,args)
                        if sql==sync.ACTORS_SQL and not rows:
                            source.actor(race_did,race_stamp,handle='between-streams.fixture.test')
                        return rows
                raced=current_state.ObservedSynchronizer(RaceSource(),admin,sync,page_size=2,max_pages=100,max_seconds=30,retries=1,backoff=0,anti_entropy_pages=0).run()
                assert raced['caught_up']
                assert http.request(public+'/search?q=between-streams',env['ADMIN_SECRET'])[0]['actors'][0]['did']==race_did
                persisted=admin.status();stop(proc);proc=start()
                assert admin.status()['source_cursor']==persisted['source_cursor']
                assert admin.status()['source_observation_revision']==persisted['source_observation_revision']
                stable=observe();assert stable['caught_up']
                stable_before=admin.status();again=observe();stable_after=admin.status()
                assert again['applied']==0 and stable_after['indexed_memberships']==stable_before['indexed_memberships']
                assert stable_after['actor_count']==stable_before['actor_count']
                try:
                    admin.apply([],stable_after['source_cursor'],stable_after['source_cursor'])
                    raise AssertionError('legacy generic writes accepted after source-authority migration')
                except sync.CursorConflict:pass
                # The real commit succeeds, but the adapter loses its response and recovers via status.
                original_http=admin.http
                class LostAckHttp(sync.Http):
                    dropped=False
                    def request(self,url,secret,body=None,max_bytes=4*1024*1024):
                        result=super().request(url,secret,body,max_bytes)
                        if url.endswith('/admin/source-reconcile') and not self.dropped:
                            self.dropped=True
                            raise sync.Retryable('fixture discarded committed response')
                        return result
                lossy=LostAckHttp(5);admin.http=lossy
                try:
                    recovered=observe(anti_pages=0)
                    assert lossy.dropped and recovered['ack_recoveries']==1
                finally:admin.http=original_http
                # Discard an accepted acknowledgement, restart, then retry the exact committed request.
                observation_admin=current_state.ObservationAdmin(admin,sync)
                digest_state,revision=observation_admin.state([race_did])
                source_actor=current_state.CurrentStateSource(source.source(),sync).observe([race_did])['observations'][0]['actor']
                next_cursor=json.loads(json.dumps(digest_state['source_cursor']));next_cursor['integration_probe']=1
                request={'actors':[source_actor],'expected_documents':digest_state['actors'],'expected_source_cursor':digest_state['source_cursor'],'source_cursor':next_cursor,'expected_observation_revision':revision}
                request['batch_id']=hashlib.sha256(sync.json_bytes(request)).hexdigest()
                discarded=http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],request)[0]
                stop(proc);proc=start()
                before_retry=admin.status()
                repeated=http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],request)[0]
                assert repeated==discarded
                assert admin.status()['source_observation_revision']==before_retry['source_observation_revision']==revision+1
                changed=json.loads(json.dumps(request));changed['source_cursor']['integration_probe']=2
                try:
                    http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],changed)
                    raise AssertionError('batch identity reused with changed body')
                except sync.CursorConflict:pass
                # Independent document and observation revision preconditions reject stale writers.
                for mismatch in ['document','revision']:
                    state,rev=observation_admin.state([race_did])
                    body={'actors':[source_actor],'expected_documents':state['actors'],'expected_source_cursor':state['source_cursor'],'source_cursor':state['source_cursor'],'expected_observation_revision':rev}
                    if mismatch=='document':body['expected_documents'][0]['document_sha256']='0'*64
                    else:body['expected_observation_revision']=rev-1
                    body['batch_id']=hashlib.sha256(sync.json_bytes(body)).hexdigest()
                    try:
                        http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],body)
                        raise AssertionError('source precondition mismatch accepted')
                    except sync.CursorConflict:pass
                    assert admin.status()['source_observation_revision']==rev
                for path,body in [('/admin/source-state',{'dids':[]}),('/admin/source-page',{'after_did':'','limit':2}),('/admin/source-reconcile',request)]:
                    try:
                        http.request(public+path,env['ADMIN_SECRET'],body)
                        raise AssertionError('source admin route publicly writable')
                    except sync.SafeError as error:assert '404' in str(error)
                    try:
                        http.request(admin_url+path,'wrong-fixture-secret',body)
                        raise AssertionError('source admin route accepted invalid auth')
                    except sync.SafeError as error:assert '401' in str(error)
                if policy_checks:
                    # Policy TTL is independent of the legacy synthetic allow-stale flag.
                    st,rev=observation_admin.state([])
                    stale_policy={'schema_version':1,'source_observed_at':int(time.time())-1000,'overrides':[],'blocked_domains':[],'complete':True}
                    stale_body={'actors':[],'expected_documents':[],'expected_source_cursor':st['source_cursor'],'source_cursor':st['source_cursor'],'expected_observation_revision':rev,'policy_snapshot':stale_policy}
                    stale_body['batch_id']=hashlib.sha256(sync.json_bytes(stale_body)).hexdigest()
                    http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],stale_body)
                    for path in ['/ready','/search?q=between-streams']:
                        try:
                            http.request(public+path,env['ADMIN_SECRET'])
                            raise AssertionError('expired policy served despite policy TTL')
                        except sync.Retryable as error:assert '503' in str(error)
                    with urllib.request.urlopen(public+'/health',timeout=5) as response:
                        assert response.headers['X-Typeahead-Binary-Sha256']==binary_sha
                        assert response.headers['X-Typeahead-Retrieval-Strategy']==os.environ.get('TA_RETRIEVAL_STRATEGY','per-segment')
                        assert response.headers['X-Typeahead-Engine']=='tantivy'
                        assert response.headers['X-Typeahead-Format']=='tantivy-0.27-f32-did-expiry-v4'
                        assert response.headers['X-Typeahead-Build']=='fixture-native-http-replay'
                    status_stale=admin.status()
                    no_policy_state,no_policy_revision=observation_admin.state([])
                    no_policy={'actors':[],'expected_documents':[],'expected_source_cursor':no_policy_state['source_cursor'],'source_cursor':no_policy_state['source_cursor'],'expected_observation_revision':no_policy_revision}
                    no_policy['batch_id']=hashlib.sha256(sync.json_bytes(no_policy)).hexdigest()
                    http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],no_policy)
                    assert admin.status()['moderation']['source_observed_at']==status_stale['moderation']['source_observed_at']
                    assert not admin.status()['ready']
                    assert not status_stale['moderation']['fresh'] and not status_stale['ready']
                    assert 'policy_snapshot' not in status_stale and 'last_source_batch' not in status_stale
                    for bad in [dict(stale_policy,complete=False),dict(stale_policy,source_observed_at=int(time.time())+1000)]:
                        st,rev=observation_admin.state([])
                        failed={'actors':[],'expected_documents':[],'expected_source_cursor':st['source_cursor'],'source_cursor':st['source_cursor'],'expected_observation_revision':rev,'policy_snapshot':bad}
                        failed['batch_id']=hashlib.sha256(sync.json_bytes(failed)).hexdigest()
                        try:
                            http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],failed)
                            raise AssertionError('invalid moderation snapshot accepted')
                        except sync.SafeError as error:assert '400' in str(error)
                        assert not admin.status()['ready'] and admin.status()['source_observation_revision']==rev
                    st,rev=observation_admin.state([])
                    fresh_body={'actors':[],'expected_documents':[],'expected_source_cursor':st['source_cursor'],'source_cursor':st['source_cursor'],'expected_observation_revision':rev,'policy_snapshot':dict(stale_policy,source_observed_at=int(time.time()))}
                    fresh_body['batch_id']=hashlib.sha256(sync.json_bytes(fresh_body)).hexdigest()
                    http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],fresh_body)
                    assert admin.status()['ready'] and admin.status()['format']=='tantivy-0.27-f32-did-expiry-v4'
                    st,rev=observation_admin.state([])
                    backwards={'actors':[],'expected_documents':[],'expected_source_cursor':st['source_cursor'],'source_cursor':st['source_cursor'],'expected_observation_revision':rev,'policy_snapshot':stale_policy}
                    backwards['batch_id']=hashlib.sha256(sync.json_bytes(backwards)).hexdigest()
                    try:
                        http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],backwards)
                        raise AssertionError('moderation policy clock moved backwards')
                    except sync.SafeError as error:assert '400' in str(error)
                    assert admin.status()['source_observation_revision']==rev and admin.status()['ready']
                # Kill the real source-owned commit before/after durability; verify every coupled field.
                for killpoint in ['before_commit','after_commit']:
                    old_state,old_revision=observation_admin.state([race_did])
                    source.actor(race_did,race_stamp,handle='atomic-'+killpoint.replace('_','-')+'.fixture.test')
                    updated=current_state.CurrentStateSource(source.source(),sync).observe([race_did])['observations'][0]['actor']
                    after_cursor=json.loads(json.dumps(old_state['source_cursor']));after_cursor['source_crash_probe']=killpoint
                    crash_body={'actors':[updated],'expected_documents':old_state['actors'],'expected_source_cursor':old_state['source_cursor'],'source_cursor':after_cursor,'expected_observation_revision':old_revision}
                    old_policy_status=admin.status()['moderation']
                    if policy_checks:crash_body['policy_snapshot']={'schema_version':1,'source_observed_at':int(time.time()),'overrides':[],'blocked_domains':[killpoint+'.unmatched.test'],'complete':True}
                    crash_body['batch_id']=hashlib.sha256(sync.json_bytes(crash_body)).hexdigest()
                    stop(proc);proc=start(crash=killpoint)
                    try:
                        http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],crash_body)
                        raise AssertionError('source commit killpoint did not terminate')
                    except sync.Retryable:pass
                    proc.wait(timeout=5);assert proc.returncode!=0
                    proc=start();recovered_status=admin.status();recovered_doc,_=observation_admin.state([race_did])
                    committed=killpoint=='after_commit'
                    assert recovered_status['source_cursor']==(after_cursor if committed else old_state['source_cursor'])
                    assert recovered_status['source_observation_revision']==old_revision+int(committed)
                    if policy_checks:assert (recovered_status['moderation']['sha256']!=old_policy_status['sha256'])==committed
                    assert (recovered_doc['actors'][0]['document_sha256']!=old_state['actors'][0]['document_sha256'])==committed
                    assert bool(http.request(public+'/search?q=atomic-'+killpoint.replace('_','-'),env['ADMIN_SECRET'])[0]['actors'])==committed
                    accepted=http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],crash_body)[0]
                    assert accepted['source_observation_revision']==old_revision+1
                    assert admin.status()['source_cursor']==after_cursor
                    assert http.request(public+'/search?q=atomic-'+killpoint.replace('_','-'),env['ADMIN_SECRET'])[0]['actors'][0]['did']==race_did
                    stop(proc);proc=start()
                    retried=http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],crash_body)[0]
                    assert retried==accepted and admin.status()['source_observation_revision']==old_revision+1
                    if policy_checks:assert admin.status()['moderation']['blocked_domain_count']==1
                if policy_checks:
                    # Policy-only writes must affect all candidates before accepted top-K, without actor timestamps.
                    for i in range(24):source.actor(f'did:example:policy-blocked-{i:02d}',race_stamp,handle=f'policy-blocked{i:02d}.blocked.test',score=1000-i)
                    for i in range(12):source.actor(f'did:example:policy-eligible-{i:02d}',race_stamp,handle=f'policy-eligible{i:02d}.safe.test',score=20-i)
                    source.actor('did:example:policy-label',race_stamp,handle='policy-label.safe.test',score=900)
                    source.actor('did:example:policy-expired',race_stamp,handle='policy-expired.safe.test',score=500,hidden=True)
                    with closing(sqlite3.connect(source.path)) as db,db:
                        active=json.dumps([{'src':'did:plc:ar7c4by46qjdydhdevvrndac','val':'!hide'}])
                        expired=json.dumps([{'src':'did:plc:ar7c4by46qjdydhdevvrndac','val':'!hide','exp':'1900-01-01T00:00:00Z'}])
                        db.execute('UPDATE actors SET labels=? WHERE did IN (?,?)',[active,'did:example:policy-blocked-00','did:example:policy-label'])
                        db.execute('UPDATE actors SET labels=? WHERE did=?',[expired,'did:example:policy-expired'])
                        db.execute("INSERT INTO blocked_domains(domain) VALUES ('blocked.test')")
                        db.execute("INSERT INTO mod_overrides(did,action) VALUES ('did:example:policy-blocked-00','show')")
                        db.execute("INSERT INTO mod_overrides(did,action) VALUES ('did:example:policy-eligible-00','hide')")
                    def policy_observe():
                        return current_state.ObservedSynchronizer(source.source(),admin,sync,page_size=2,max_pages=200,max_seconds=30,retries=1,backoff=0,anti_entropy_pages=200,policy_mode='snapshot',sweep_cooldown_seconds=0).run()
                    assert policy_observe()['caught_up']
                    expected=['did:example:policy-blocked-00','did:example:policy-expired']+[f'did:example:policy-eligible-{i:02d}' for i in range(1,9)]
                    for suffix in ['', '&pds=local.test', '&prefer_pds=local.test&preference=pds_first', '&prefer_pds=local.test&boost=100']:
                        actual=http.request(public+'/search?q=policy&limit=10'+suffix,env['ADMIN_SECRET'])[0]['actors']
                        assert [actor['did'] for actor in actual]==expected
                    count_before=admin.status()['indexed_memberships']
                    with closing(sqlite3.connect(source.path)) as db,db:
                        db.execute("DELETE FROM mod_overrides WHERE did='did:example:policy-blocked-00'")
                    policy_observe()
                    actual=http.request(public+'/search?q=policy&limit=10',env['ADMIN_SECRET'])[0]['actors']
                    assert [actor['did'] for actor in actual]==['did:example:policy-expired']+[f'did:example:policy-eligible-{i:02d}' for i in range(1,10)]
                    assert admin.status()['indexed_memberships']==count_before
                    with closing(sqlite3.connect(source.path)) as db,db:db.execute('DELETE FROM blocked_domains')
                    policy_observe()
                    actual=http.request(public+'/search?q=policy&limit=10',env['ADMIN_SECRET'])[0]['actors']
                    assert [actor['did'] for actor in actual]==[f'did:example:policy-blocked-{i:02d}' for i in range(1,11)]
                    assert admin.status()['indexed_memberships']==count_before
                    source.actor('did:example:policy-cjk-true',race_stamp,handle='policy-cjk-true.safe.test',score=1)
                    for i in range(12):source.actor(f'did:example:policy-cjk-false-{i:02d}',race_stamp,handle=f'policy-cjk-false{i:02d}.safe.test',score=10000-i)
                    with closing(sqlite3.connect(source.path)) as db,db:
                        db.execute("UPDATE actors SET display_name='日本 東京 本太' WHERE did LIKE 'did:example:policy-cjk-false-%'")
                        db.execute("UPDATE actors SET display_name='名前日本太郎' WHERE did='did:example:policy-cjk-true'")
                    policy_observe()
                    cjk=http.request(public+'/search?q='+urllib.parse.quote('日本太'),env['ADMIN_SECRET'])[0]['actors']
                    assert [a['did'] for a in cjk]==['did:example:policy-cjk-true']
                    # Unsupported expiry is an explicit 503, never a successful empty result.
                    with closing(sqlite3.connect(source.path)) as db,db:
                        db.execute('UPDATE actors SET labels=? WHERE did=?',[json.dumps([{'src':'did:plc:ar7c4by46qjdydhdevvrndac','val':'!hide','exp':'not-a-date'}]),'did:example:policy-cjk-true'])
                    policy_observe()
                    try:
                        http.request(public+'/search?q='+urllib.parse.quote('日本太'),env['ADMIN_SECRET'])
                        raise AssertionError('unsupported moderation expiry returned a successful response')
                    except sync.Retryable as error:assert '503' in str(error)
                    with closing(sqlite3.connect(source.path)) as db,db:db.execute("UPDATE actors SET labels='[]' WHERE did='did:example:policy-cjk-true'")
                    policy_observe()
                    assert admin.status()['reconciliation']['last_completed_started_at'] is not None
                    assert admin.status()['reconciliation']['last_completed_duration_s'] is not None
                    saved_policy=admin.status()['moderation'];stop(proc);proc=start();restored_policy=admin.status()['moderation']
                    assert restored_policy['sha256']==saved_policy['sha256'] and restored_policy['source_observed_at']==saved_policy['source_observed_at']
                    assert restored_policy['complete'] and restored_policy['override_count']==1 and restored_policy['blocked_domain_count']==0
                    public_status=http.request(public+'/index/status',env['ADMIN_SECRET'])[0]
                    assert 'policy_snapshot' not in public_status and 'last_source_batch' not in public_status
                    assert 'overrides' not in public_status['moderation'] and 'blocked_domains' not in public_status['moderation']
                    assert http.request(public+'/search?q='+urllib.parse.quote('日本太'),env['ADMIN_SECRET'])[0]['actors'][0]['did']=='did:example:policy-cjk-true'
                    for invalid_snapshot in [{'schema_version':1,'source_observed_at':source.now,'overrides':[],'blocked_domains':[],'complete':False}, {'schema_version':1,'source_observed_at':source.now,'overrides':[],'blocked_domains':[f'blocked{i}.test' for i in range(10001)],'complete':True}]:
                        st,rev=observation_admin.state([])
                        invalid={'actors':[],'expected_documents':[],'expected_source_cursor':st['source_cursor'],'source_cursor':st['source_cursor'],'expected_observation_revision':rev,'policy_snapshot':invalid_snapshot}
                        invalid['batch_id']=hashlib.sha256(sync.json_bytes(invalid)).hexdigest()
                        try:
                            http.request(admin_url+'/admin/source-reconcile',env['ADMIN_SECRET'],invalid)
                            raise AssertionError('incomplete or over-cap moderation policy accepted')
                        except sync.SafeError as error:assert '400' in str(error)
                        assert admin.status()['source_observation_revision']==rev
                expected_format=admin.status()['format']
                for path in ['/search?q=between-streams','/health','/ready','/index/status','/debug/search?q=between-streams']:
                    with urllib.request.urlopen(public+path,timeout=5) as response:
                        assert response.status==200
                        assert response.headers['X-Typeahead-Binary-Sha256']==binary_sha
                        assert response.headers['X-Typeahead-Retrieval-Strategy']==os.environ.get('TA_RETRIEVAL_STRATEGY','per-segment')
                        assert response.headers['X-Typeahead-Engine']=='tantivy'
                        assert response.headers['X-Typeahead-Build']=='fixture-native-http-replay'
                        assert response.headers['X-Typeahead-Format']==expected_format
                fingerprint_status=admin.status()
                assert fingerprint_status['binary_sha256']==binary_sha
                assert fingerprint_status['retrieval_strategy']==os.environ.get('TA_RETRIEVAL_STRATEGY','per-segment')
                # Installing different bytes at the same path does not relabel the running process.
                running_original=p/'running-original'
                os.replace(binary,running_original)
                try:
                    binary.write_bytes(running_original.read_bytes()+b'fixture installed replacement')
                    assert hashlib.sha256(binary.read_bytes()).hexdigest()!=binary_sha
                    assert admin.status()['binary_sha256']==binary_sha
                finally:os.replace(running_original,binary)
                return {'passed':True,'source':'real HTTP stub executing read-only SQLite queries','backend':'actual compiled Rust Tantivy','backend_binary_sha256':binary_sha,'sync_source_sha256':hashlib.sha256((ROOT/'sync.py').read_bytes()).hexdigest(),'test_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'new_actor_imported':True,'cleared_handle_removed':True,'tombstone_removed':True,'cursor_survives_restart':True,'replay_after_restart_passed':True,'replay_did_not_reindex':True,'bounded_lookback_recovers_late_actor':True,'source_requests':len(source.requests),'source_policy':current_state.SOURCE_POLICY,'legacy_source_policy':sync.SOURCE_POLICY,'current_state_source_sha256':hashlib.sha256((ROOT/'current_state.py').read_bytes()).hexdigest(),'stable_live_tombstone_tie_preserved':True,'hidden_tombstone_tie_preserved':True,'older_live_tombstone_applied':True,'future_restore_respects_cycle_cutoff':True,'legacy_cursor_policy_upgrade_passed':True,'legacy_late_equal_restore_conflict_preserved':True,'source_owned_late_restore_recovered':True,'same_second_moderation_observed':True,'backdated_new_actor_reconciled':True,'absence_without_tombstone_reconciled':True,'cross_stream_restore_observed':True,'observation_revision_survives_restart':True,'observation_replay_did_not_reindex':True,'generic_writes_rejected_after_observation_migration':True,'discarded_ack_exact_retry_after_restart':True,'client_ambiguous_ack_recovers_durable_cursor':True,'changed_body_batch_reuse_rejected':True,'document_and_revision_preconditions_enforced':True,'source_admin_routes_private_and_authenticated':True,'source_before_commit_crash_atomicity':True,'source_after_commit_crash_atomicity':True,'source_crash_retry_receipt_survives_restart':True,'actual_runtime_binary_fingerprint_survives_path_replacement':True,'retrieval_strategy':os.environ.get('TA_RETRIEVAL_STRATEGY','per-segment'),'captured_public_transport_identity_headers':True,'policy_snapshot_checks':policy_checks,'expiry_v4_format_fence':policy_checks,'policy_absence_does_not_refresh_ttl':policy_checks,'policy_clock_backwards_rejected':policy_checks,'policy_pre_topk_refill_and_preference':policy_checks,'policy_ttl_not_bypassed_by_allow_stale':policy_checks,'policy_commit_crash_atomicity':policy_checks,'policy_no_reindex_rule_deletes':policy_checks,'policy_cjk_conjunction_refill':policy_checks,'unsupported_expiry_returns_503':policy_checks,'policy_restart_and_public_redaction':policy_checks,'production_contact':False,'temporary_payloads_removed':True}
            except BaseException as error:
                error.add_note((p/'server.log').read_text(errors='replace')[-6000:])
                raise
            finally:stop(proc)
        finally:source.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--binary',type=Path,default=ROOT.parent/'tantivy/target/release/typeahead-tantivy');p.add_argument('--scratch-root',type=Path,default=ROOT);p.add_argument('--receipt',type=Path,default=ROOT/'sync-integration-receipt.json');p.add_argument('--policy-checks',action='store_true');args=p.parse_args()
    result=run(args.binary.resolve(),args.scratch_root.resolve(),policy_checks=args.policy_checks);args.receipt.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
