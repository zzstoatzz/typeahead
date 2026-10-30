import copy
from contextlib import closing
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('readonly_sync',ROOT/'sync.py');sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)

class Fixture:
    def __init__(self,root,now=2000000):
        self.path=root/'source.db';self.now=now;self.floor=now-100;self.requests=[];self.batches=[];self.fail_commits=False;self.drop_ack=False
        self.cursor=None;self.watermark=self.floor;self.actors={};self.applied_docs=0;self.admin_secret='fixture-admin-not-a-secret';self.source_secret='fixture-readonly-not-a-secret'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''CREATE TABLE actors(did TEXT PRIMARY KEY, handle TEXT NOT NULL, display_name TEXT, avatar_url TEXT, hidden INTEGER, labels TEXT, created_at TEXT, associated TEXT, pds TEXT, followers_count INTEGER, follows_count INTEGER, posts_count INTEGER, last_activity_at INTEGER, quality_score REAL, updated_at INTEGER);
CREATE INDEX idx_actors_updated_at ON actors(updated_at,did);
CREATE TABLE tombstones(did TEXT PRIMARY KEY,deleted_at INTEGER);
CREATE INDEX idx_tombstones_deleted_at ON tombstones(deleted_at);
CREATE TABLE mod_overrides(did TEXT PRIMARY KEY, action TEXT NOT NULL, reason TEXT, created_at INTEGER);
CREATE TABLE blocked_domains(domain TEXT PRIMARY KEY, reason TEXT, created_at INTEGER);''')
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def respond(self,code,value):
                data=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_GET(self):
                if self.path!='/index/status' or self.headers.get('Authorization')!='Bearer '+owner.admin_secret:self.respond(401,{});return
                self.respond(200,{'import_complete':True,'build_id':'fixture','snapshot_watermark':owner.floor,'watermark':owner.watermark,'source_cursor':owner.cursor})
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/v2/pipeline':
                    if self.headers.get('Authorization')!='Bearer '+owner.source_secret:self.respond(401,{});return
                    self.source(body);return
                if self.path!='/admin/apply' or self.headers.get('Authorization')!='Bearer '+owner.admin_secret:self.respond(401,{});return
                owner.batches.append(copy.deepcopy(body))
                if owner.fail_commits:self.respond(503,{});return
                if body.get('expected_source_cursor')!=owner.cursor:self.respond(409,{});return
                candidate=copy.deepcopy(owner.actors);applied=replayed=stale=0
                for actor in body['actors']:
                    old=candidate.get(actor['did'])
                    if old and actor['version']<old['version']:stale+=1;continue
                    if old and actor['version']==old['version']:
                        if old!=actor:self.respond(409,{});return
                        replayed+=1;continue
                    candidate[actor['did']]=actor;applied+=1
                owner.applied_docs+=applied;owner.actors=candidate;owner.cursor=body['source_cursor'];owner.watermark=body.get('watermark',owner.watermark)
                if owner.drop_ack:owner.drop_ack=False;self.close_connection=True;return
                self.respond(200,{'source_cursor':owner.cursor,'watermark':owner.watermark,'opstamp':len(owner.batches),'applied':applied,'replayed':replayed,'stale':stale})
            def source(self,body):
                try:
                    assert [r['type'] for r in body['requests']]==['execute','close']
                    stmt=body['requests'][0]['stmt'];sql=stmt['sql'];assert sql in sync.SELECTS and sql.startswith(('SELECT ', 'WITH requested AS '));assert ';' not in sql
                    owner.requests.append(copy.deepcopy(stmt));args=[]
                    for value in stmt['args']:
                        assert value['type'] in ['integer','text'];args.append(int(value['value']) if value['type']=='integer' else value['value'])
                    with closing(sqlite3.connect(owner.path)) as db, db:
                        db.execute('PRAGMA query_only=ON');db.create_function('unixepoch',0,lambda:owner.now)
                        cur=db.execute(sql,args);columns=[{'name':x[0]} for x in cur.description];raw=cur.fetchall()
                    rows=[]
                    for row in raw:
                        cells=[]
                        for v in row:
                            if v is None:cells.append({'type':'null'})
                            elif type(v) is int:cells.append({'type':'integer','value':str(v)})
                            elif type(v) is float:cells.append({'type':'float','value':v})
                            else:cells.append({'type':'text','value':v})
                        rows.append(cells)
                    self.respond(200,{'results':[{'type':'ok','response':{'type':'execute','result':{'cols':columns,'rows':rows,'affected_row_count':0}}},{'type':'ok','response':{'type':'close'}}]})
                except Exception:self.respond(400,{'error':'invalid readonly fixture request'})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url=f'http://127.0.0.1:{self.server.server_port}'
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
    def actor(self,did,version,handle=None,hidden=False,score=.1):
        record=(did,handle if handle is not None else did.split(':')[-1]+'.fixture.test','Alice 東京','bafkavatar',int(hidden),'[{"val":"test"}]','2026-10-28T00:00:00Z','{"chat":{"allowIncoming":"all"}}','https://local.test',3,4,5,6,score,version)
        with closing(sqlite3.connect(self.path)) as db, db:db.execute('INSERT OR REPLACE INTO actors VALUES('+','.join('?' for _ in record)+')',record)
    def tombstone(self,did,version):
        with closing(sqlite3.connect(self.path)) as db, db:db.execute('INSERT OR REPLACE INTO tombstones VALUES(?,?)',(did,version))
    def source(self):return sync.Turso(self.url,self.source_secret,allow_test_loopback=True,retries=1,backoff=0)
    def admin(self):return sync.Admin(self.url,self.admin_secret)
    def runner(self,**kwargs):return sync.Synchronizer(self.source(),self.admin(),page_size=3,max_pages=100,max_seconds=30,retries=1,backoff=0,**kwargs)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT,prefix='.test-sync-');self.addCleanup(self.tmp.cleanup)
        self.fixture=Fixture(Path(self.tmp.name));self.addCleanup(self.fixture.close)
    def test_real_sqlite_keysets_same_second_clears_tombstones_and_hydration(self):
        f=self.fixture
        for i in range(11):f.actor(f'did:example:{i:03d}',f.floor+10)
        f.actor('did:example:cleared',f.floor+11,handle='');f.actor('did:example:hidden',f.floor+12,hidden=True)
        f.actor('did:example:floor',f.floor);f.actor('did:example:future',f.now)
        for i in range(7):f.tombstone(f'did:example:gone{i}',f.floor+13)
        result=f.runner().run();self.assertTrue(result['caught_up']);self.assertEqual(result['through'],f.now-2)
        self.assertEqual(len(f.actors),20);self.assertNotIn('did:example:floor',f.actors);self.assertNotIn('did:example:future',f.actors)
        self.assertTrue(f.actors['did:example:cleared']['deleted']);self.assertTrue(f.actors['did:example:hidden']['hidden']);self.assertTrue(f.actors['did:example:gone0']['deleted'])
        self.assertEqual(f.actors['did:example:000']['display_name'],'Alice 東京');self.assertEqual(f.actors['did:example:000']['labels'],[{'val':'test'}]);self.assertEqual(f.actors['did:example:000']['associated'],{'chat':{'allowIncoming':'all'}})
        self.assertEqual(f.actors['did:example:000']['version'],f.floor+10);self.assertEqual(f.watermark,f.now-2)
        # Fully caught-up watermark appears only on the final drained-stream commit.
        self.assertTrue(all('watermark' not in b for b in f.batches[:-1]));self.assertIn('watermark',f.batches[-1])
        self.assertTrue(all(r['sql'] in sync.SELECTS for r in f.requests));self.assertTrue(all(r.get('want_rows') is True for r in f.requests))
    def test_commit_failure_does_not_advance_any_durable_cursor(self):
        f=self.fixture;f.actor('did:example:one',f.floor+10);f.fail_commits=True
        status=Path(self.tmp.name)/'progress.json'
        with self.assertRaises(sync.Retryable):f.runner(status_file=str(status)).run()
        self.assertIsNone(f.cursor);self.assertEqual(f.watermark,f.floor);self.assertEqual(f.actors,{});self.assertFalse(status.exists())
        f.fail_commits=False;self.assertTrue(f.runner(status_file=str(status)).run()['caught_up']);self.assertTrue(status.exists());self.assertIn('did:example:one',f.actors)
    def test_lost_ack_recovers_committed_cursor_without_replaying_batch(self):
        f=self.fixture;f.actor('did:example:one',f.floor+10);f.drop_ack=True
        self.assertTrue(f.runner().run()['caught_up']);self.assertEqual(sum(bool(b['actors']) for b in f.batches),1)
    def test_stale_delete_cannot_remove_newer_actor_and_equal_delete_replays(self):
        f=self.fixture;f.actor('did:example:restored',f.floor+20);f.tombstone('did:example:restored',f.floor+10)
        f.actor('did:example:both',f.floor+12,handle='');f.tombstone('did:example:both',f.floor+12)
        self.assertTrue(f.runner().run()['caught_up']);self.assertFalse(f.actors['did:example:restored']['deleted']);self.assertTrue(f.actors['did:example:both']['deleted'])
        before=copy.deepcopy(f.actors);self.assertTrue(f.runner().run()['caught_up']);self.assertEqual(f.actors,before)
    def test_bounded_pause_resume_and_cursor_cas(self):
        f=self.fixture
        for i in range(8):f.actor(f'did:example:{i:03d}',f.floor+10)
        runner=f.runner();runner.max_pages=1;result=runner.run();self.assertFalse(result['caught_up']);self.assertEqual(len(f.actors),3);self.assertEqual(f.watermark,f.floor)
        saved=copy.deepcopy(f.cursor);self.assertTrue(f.runner().run()['caught_up']);self.assertEqual(len(f.actors),8)
        with self.assertRaises(sync.CursorConflict):f.admin().apply([],saved,saved)
    def test_late_same_second_rows_are_replayed_at_overlap_cutoff(self):
        f=self.fixture;self.assertTrue(f.runner().run()['caught_up']);through=f.cursor['through'];f.actor('did:example:late',through)
        f.now+=5;self.assertTrue(f.runner().run()['caught_up']);self.assertIn('did:example:late',f.actors)
    def test_bounded_lookback_recovers_late_older_rows_without_reindexing_replays(self):
        f=self.fixture;f.floor=f.now-1000
        f.actor('did:example:existing',f.now-20)
        self.assertTrue(f.runner().run()['caught_up']);through=f.cursor['through']
        self.assertEqual(f.cursor['lookback_seconds'],300);self.assertEqual(f.applied_docs,1)
        f.actor('did:example:late',through-100);f.actor('did:example:too-old',through-301)
        f.tombstone('did:example:late-delete',through-200);f.now+=10
        result=f.runner().run();self.assertTrue(result['caught_up']);self.assertIn('did:example:late',f.actors);self.assertTrue(f.actors['did:example:late-delete']['deleted']);self.assertNotIn('did:example:too-old',f.actors)
        self.assertEqual(result['applied'],2);self.assertGreaterEqual(result['replayed'],1);self.assertEqual(f.applied_docs,3)
        again=f.runner().run();self.assertEqual(again['applied'],0);self.assertEqual(f.applied_docs,3);self.assertGreaterEqual(again['replayed'],3)
    def test_current_state_join_supersedes_only_eligible_retained_tombstones(self):
        f=self.fixture;v=f.floor+10
        for suffix,version,handle,hidden in [('equal',v,'equal.fixture.test',False),('newer',v+5,'newer.fixture.test',False),('hidden',v,'hidden.fixture.test',True),('older',v,'older.fixture.test',False),('cleared',v,'',False)]:
            did='did:example:'+suffix;f.actor(did,version,handle=handle,hidden=hidden);f.tombstone(did,v+1 if suffix=='older' else v)
        f.tombstone('did:example:absent',v)
        result=f.runner().run();self.assertTrue(result['caught_up']);self.assertEqual(result['superseded_tombstones'],3);self.assertEqual(result['records'],11);self.assertEqual(result['submitted_records'],8)
        self.assertFalse(f.actors['did:example:equal']['deleted']);self.assertFalse(f.actors['did:example:newer']['deleted']);self.assertTrue(f.actors['did:example:hidden']['hidden']);self.assertFalse(f.actors['did:example:hidden']['deleted'])
        self.assertTrue(f.actors['did:example:older']['deleted']);self.assertTrue(f.actors['did:example:absent']['deleted']);self.assertTrue(f.actors['did:example:cleared']['deleted']);self.assertEqual(result['replayed'],1)
        self.assertEqual(f.cursor['source_policy'],sync.SOURCE_POLICY)
    def test_all_superseded_page_advances_raw_cursor_and_resumes_bounded(self):
        f=self.fixture
        for i in range(4):
            did=f'did:example:live{i}';f.actor(did,f.floor+10);f.tombstone(did,f.floor+10)
        actor_pass=f.runner();actor_pass.max_pages=3
        first=actor_pass.run();self.assertFalse(first['caught_up']);self.assertEqual(f.cursor['cycle']['stream'],'tombstones')
        raw_pass=f.runner();raw_pass.max_pages=1
        second=raw_pass.run();self.assertEqual(second['records'],3);self.assertEqual(second['submitted_records'],0);self.assertEqual(second['superseded_tombstones'],3);self.assertEqual(f.cursor['tombstones']['did'],'did:example:live2');self.assertEqual(f.watermark,f.floor)
        last=f.runner().run();self.assertTrue(last['caught_up']);self.assertEqual(last['superseded_tombstones'],1);self.assertEqual(f.applied_docs,4)
    def test_future_live_row_does_not_supersede_tombstone_before_cycle_cutoff(self):
        f=self.fixture;f.floor=80;f.now=107;f.watermark=80;did='did:example:future-restore'
        f.actor(did,90)
        with closing(sqlite3.connect(f.path)) as db, db:
            db.row_factory=sqlite3.Row;old=dict(db.execute('SELECT * FROM actors WHERE did=?',(did,)).fetchone())
        f.actors[did]=sync.actor_record(old);f.actor(did,110);f.tombstone(did,100)
        first=f.runner().run();self.assertEqual(first['through'],105);self.assertEqual(first['superseded_tombstones'],0);self.assertTrue(f.actors[did]['deleted']);self.assertEqual(f.actors[did]['version'],100)
        f.now=114;second=f.runner().run();self.assertEqual(second['through'],112);self.assertEqual(second['superseded_tombstones'],1);self.assertFalse(f.actors[did]['deleted']);self.assertEqual(f.actors[did]['version'],110)
    def test_late_equal_version_restore_after_actual_deletion_still_conflicts(self):
        f=self.fixture;f.floor=f.now-1000;stamp=f.now-20;did='did:example:late-equal-restore';f.tombstone(did,stamp)
        self.assertTrue(f.runner().run()['caught_up']);old=copy.deepcopy(f.cursor);self.assertTrue(f.actors[did]['deleted'])
        f.actor(did,stamp);f.now+=5
        with self.assertRaises(sync.CursorConflict):f.runner().run()
        self.assertEqual(f.cursor,old);self.assertTrue(f.actors[did]['deleted'])
    def test_legacy_source_policy_upgrade_is_atomic_and_unknown_policy_rejected(self):
        f=self.fixture;self.assertTrue(f.runner().run()['caught_up']);f.cursor.pop('source_policy');old=copy.deepcopy(f.cursor)
        self.assertTrue(f.runner().run()['caught_up']);self.assertEqual(f.cursor['source_policy'],sync.SOURCE_POLICY);self.assertEqual(f.batches[-2]['expected_source_cursor'],old)
        f.cursor['source_policy']='unsupported-policy'
        with self.assertRaisesRegex(sync.SafeError,'source policy'):f.runner().run()
    def test_source_gap_requires_fresh_snapshot(self):
        f=self.fixture;f.now=f.floor+sync.RETENTION_SECONDS+1
        with self.assertRaisesRegex(sync.SafeError,'retention'):f.runner().run()
        self.assertEqual(f.actors,{});self.assertIsNone(f.cursor)
    def test_queries_use_actual_source_indexes_without_global_scan(self):
        f=self.fixture
        with closing(sqlite3.connect(f.path)) as db, db:
            for sql in [sync.ACTORS_SQL,sync.TOMBSTONES_SQL]:
                plan=[row[-1] for row in db.execute('EXPLAIN QUERY PLAN '+sql,[f.floor,f.floor,'',f.now-2,3])]
                self.assertTrue(any('SEARCH' in p and ('idx_actors_updated_at' in p or 'idx_tombstones_deleted_at' in p) for p in plan),plan)
                self.assertFalse(any(p.startswith('SCAN ') for p in plan),plan)
                if sql==sync.TOMBSTONES_SQL:self.assertTrue(any('SEARCH a ' in p and ('PRIMARY KEY' in p or 'sqlite_autoindex_actors' in p) for p in plan),plan)
    def test_https_source_loopback_admin_and_select_only_guards(self):
        with self.assertRaises(sync.SafeError):sync.Turso(self.fixture.url,'fixture')
        with self.assertRaises(sync.SafeError):sync.Admin('https://example.com','fixture')
        with self.assertRaises(sync.SafeError):sync.source_url('https://user:secret@example.com')
        with self.assertRaises(sync.SafeError):self.fixture.source().select('DELETE FROM actors',[])
        self.assertEqual(self.fixture.requests,[])

if __name__=='__main__':unittest.main()
