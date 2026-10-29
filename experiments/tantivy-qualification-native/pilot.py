"""Small real native writer/search pilot; no source credentials or cloud calls."""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BIN = HERE / '.tools' / 'bin'
BUDGET = 250 * 1024 * 1024

def main():
    started = time.monotonic()
    checks, children = [], []
    env = {k: os.environ[k] for k in ['PATH', 'HOME']}
    with tempfile.TemporaryDirectory(prefix='pilot-', dir=HERE) as directory:
        root = Path(directory).resolve()
        (root / 'owner').write_text('native-qualification-v1')
        def guard():
            if time.monotonic() - started > 180:
                raise TimeoutError('pilot wall deadline')
            if sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) > BUDGET:
                raise RuntimeError('250MiB payload budget')
        def check(name, ok):
            if not ok:
                raise AssertionError(name)
            checks.append(name)
        source = root / 'source.db'
        stamp = int(time.time()) - 60
        actors = {did: {'did': 'did:fixture:' + did, 'handle': did + '.fixture.test',
                       'display_name': name, 'pds': 'https://' + pds, 'quality_score': score,
                       'hidden': False, 'deleted': False} for did, name, pds, score in
                  [('a', 'Alice Alpha', 'local.test', 30), ('b', 'Bob Beta', 'remote.test', 20),
                   ('c', 'Charlie Gamma', 'local.test', 10)]}
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
            db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                [(a['did'],a['handle'],a['display_name'],'','[]',a['pds'],'','{}',0,a['quality_score'],stamp) for a in actors.values()])
        baseenv = env | {'MODE':'indexer','INDEX_CHANNEL':'local','INDEX_SOURCE':'export','INDEX_SAMPLE_LIMIT':'0',
                        'INDEX_PDS_MAX_ACTORS':'10000','INDEX_REUSE_SOURCE':str(source),'INDEX_BUILD_ROOT':str(root/'build'),
                        'INDEXER_VERSION':'isolated-native-qualification-pilot'}
        with (root/'build.log').open('w') as log:
            subprocess.run([str(BIN/'typeahead-ingester')], cwd=REPO/'services',env=baseenv,stdout=log,stderr=log,check=True,timeout=60)
        snapshot = next((root/'build').glob('build-*/index.db'))
        with sqlite3.connect(snapshot) as db:
            floor = int(db.execute("SELECT value FROM index_meta WHERE key='source_watermark'").fetchone()[0])
        common = env | {'LOCAL_DB_PATH':str(root/'overlay.db'),'SQLITE_TMPDIR':str(root/'tmp'),
                        'TYPEAHEAD_EVAL_SNAPSHOT':str(snapshot),'TYPEAHEAD_EVAL_PRIVATE_ROOT':str(root),
                        'TYPEAHEAD_EVAL_NONCE':'native-qualified-fixture-only','TYPEAHEAD_EVAL_INPUT_ID':'0'*64}
        sock = socket.socket();sock.bind(('127.0.0.1',0));port = sock.getsockname()[1];sock.close()
        logs = []
        def spawn(binary, settings):
            log = (root/(binary+'-'+str(len(children))+'.log')).open('w');logs.append(log)
            child = subprocess.Popen([str(BIN/binary)],env=settings,cwd=REPO/'services',stdout=log,stderr=log,start_new_session=True)
            children.append(child);return child
        def stop(child):
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
        def get(query,pds=None):
            params={'q':query,'limit':10}
            if pds:params['pds']=pds
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/search?'+urllib.parse.urlencode(params),timeout=3) as r:
                return [a['did'] for a in json.load(r)['actors']]
        def oracle(query):
            eligible = [a for a in actors.values() if not a['hidden'] and not a['deleted'] and
                        (a['handle'].lower().startswith(query.lower()) or any(w.startswith(query.lower()) for w in a['display_name'].lower().split()))]
            return [a['did'] for a in sorted(eligible,key=lambda a:(a['handle'].lower()!=query.lower(),-a['quality_score'],a['did']))]
        def verify():
            for query in ['alice','renamed','bob','charlie','newactor','a.fixture.test']:
                check('raw-profile-oracle:'+query,get(query)==oracle(query))
        planned_new={'did':'did:fixture:new','handle':'newactor.fixture.test','display_name':'NewActor','version':floor+3}
        planned_rename=actors['a']|{'version':floor+2,'handle':'renamed.fixture.test','display_name':'Renamed Alpha'}
        planned_hide=actors['b']|{'version':floor+2,'hidden':True,'pds':'https://local.test'}
        ordered_feed=[
            {'cursor':1,'actors':[planned_rename,planned_hide]},
            {'cursor':2,'actors':[planned_rename|{'version':floor+1,'display_name':'Stale Wrong'}]},
            {'cursor':3,'actors':[actors['c']|{'version':floor+3,'deleted':True},planned_new]},
            {'cursor':4,'actors':[planned_hide|{'version':floor+4,'hidden':False},
                planned_new|{'version':floor+4,'quality_score':100,'hidden':False,'deleted':False,'display_name':'Renamed Pi','pds':'https://remote.test'}]},
        ]
        feed_bytes=json.dumps(ordered_feed,sort_keys=True,separators=(',',':')).encode()
        (root/'feed.json').write_bytes(feed_bytes)
        feed = hashlib.sha256(feed_bytes).hexdigest()
        adapterenv = common | {'NATIVE_QUAL_ROOT':str(root),'NATIVE_QUAL_REQUEST':str(root/'request.json'),
                               'NATIVE_QUAL_RESPONSE':str(root/'response.json'),'NATIVE_QUAL_FEED_SHA256':feed,
                               'NATIVE_QUAL_SECONDS':'120'}
        def send(batch):
            guard()
            temp=root/'request.part';temp.write_text(json.dumps(batch,separators=(',',':')));temp.replace(root/'request.json')
            deadline=time.monotonic()+10
            while (root/'request.json').exists():
                if adapter.poll() is not None:raise RuntimeError('adapter stopped: '+str([p.read_text()[-1000:] for p in root.glob('qualification-native*.log')]))
                if time.monotonic()>deadline:raise TimeoutError('batch acknowledgement')
                time.sleep(.01)
            response=json.loads((root/'response.json').read_text())
            if response.get('accepted') and response.get('transactions')==1:
                check('committed-batch-matches-frozen-feed',batch['actors']==ordered_feed[batch['cursor']-1]['actors'])
            return response
        def batch(cursor, changes):
            return {'feed_sha256':feed,'cursor':cursor,'expected_cursor':cursor-1,'actors':changes}
        def actor(key, version, **change):
            return actors[key] | {'version':version} | change
        def cursor():
            with sqlite3.connect(root/'overlay.db') as db:
                row=db.execute("SELECT value FROM sync_meta WHERE key='qualification_cursor'").fetchone()
                return json.loads(row[0])['cursor'] if row else 0
        try:
            server=spawn('eval-server',common|{'PORT':str(port)})
            for _ in range(100):
                try:get('alice');break
                except OSError:
                    if server.poll() is not None:raise RuntimeError('search stopped')
                    time.sleep(.05)
            adapter=spawn('qualification-native',adapterenv)
            verify()
            rename=actor('a',floor+2,handle='renamed.fixture.test',display_name='Renamed Alpha')
            hide=actor('b',floor+2,hidden=True,pds='https://local.test')
            first=batch(1,[rename,hide]);r=send(first)
            check('batch1-one-transaction',r['accepted'] and r['applied']==2 and r['transactions']==1 and cursor()==1)
            actors['a']=rename;actors['b']=hide;verify()
            r=send(first);check('identical-batch-idempotent',r['accepted'] and r['transactions']==0 and cursor()==1)
            stale=batch(2,[actor('a',floor+1,display_name='Stale Wrong')]);r=send(stale)
            check('stale-row-does-not-reindex',r['stale']==1 and r['applied']==0 and cursor()==2);verify()
            conflict=batch(3,[actor('a',floor+2,display_name='Conflict Wrong')]);r=send(conflict)
            check('equal-version-conflict-closed',not r['accepted'] and r['err']=='SourceVersionConflict' and cursor()==2)
            r=send(batch(5,[]));check('cursor-CAS-closed',not r['accepted'] and r['err']=='CursorConflict' and cursor()==2)
            with sqlite3.connect(root/'overlay.db') as db:
                db.executescript("CREATE TABLE qualification_parent(id INTEGER PRIMARY KEY); CREATE TABLE qualification_fk(id INTEGER REFERENCES qualification_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER qualification_commit_failure AFTER INSERT ON qualification_actor_state WHEN NEW.did='did:fixture:new' BEGIN INSERT INTO qualification_fk VALUES(999); END;")
            new={'did':'did:fixture:new','handle':'newactor.fixture.test','display_name':'NewActor','version':floor+3}
            failed=batch(3,[actor('c',floor+3,deleted=True),new]);r=send(failed)
            check('actual-deferred-COMMIT-failure-rollback',not r['accepted'] and cursor()==2);verify()
            with sqlite3.connect(root/'overlay.db') as db:db.execute('DROP TRIGGER qualification_commit_failure')
            r=send(failed);check('delete-and-unknown-actor-commit',r['accepted'] and r['applied']==2 and r['transactions']==1 and cursor()==3)
            actors['c']=actor('c',floor+3,deleted=True);actors['new']=new|{'quality_score':0,'hidden':False,'deleted':False};verify()
            stop(adapter);adapter=spawn('qualification-native',adapterenv)
            r=send(failed);check('durable-restart-idempotency',r['accepted'] and r['transactions']==0 and cursor()==3);verify()
            shown=actor('b',floor+4,hidden=False,pds='https://local.test')
            rescored=actor('new',floor+4,display_name='Renamed Pi',quality_score=100,pds='https://remote.test')
            r=send(batch(4,[shown,rescored]));check('show-migrate-rescore-one-transaction',r['accepted'] and r['applied']==2 and r['transactions']==1 and cursor()==4)
            actors['b']=shown;actors['new']=rescored;verify()
            check('PDS-migration-new-scope',get('bob','local.test')==['did:fixture:b'])
            check('PDS-migration-old-scope-suppressed',get('bob','remote.test')==[])
            check('rescore-ordering',get('renamed')==['did:fixture:new','did:fixture:a'])
            duplicate=batch(5,[actor('a',floor+5),actor('a',floor+6)])
            r=send(duplicate);check('duplicate-actor-batch-rejected',not r['accepted'] and r['err']=='DuplicateActor' and cursor()==4)
            wrongfeed=batch(5,[]);wrongfeed['feed_sha256']='f'*64
            r=send(wrongfeed);check('frozen-feed-conflict-rejected',not r['accepted'] and cursor()==4)
            maximum=2**64-1
            r=send({'feed_sha256':feed,'cursor':maximum,'expected_cursor':maximum,'actors':[]})
            check('cursor-overflow-rejected',not r['accepted'] and cursor()==4)
            for label, overrides in [('production-app',{'FLY_APP_NAME':'typeahead-startup-protected'}),
                                      ('snapshot-symlink',{'TYPEAHEAD_EVAL_SNAPSHOT':str(root/'outside.db')})]:
                if label=='snapshot-symlink':(root/'outside.db').symlink_to(HERE/'README.md')
                denied=subprocess.run([str(BIN/'qualification-native')],env=adapterenv|overrides,
                                      stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
                check('isolation-denied:'+label,denied.returncode!=0)
            guard()
            report={'kind':'actual-native-batch-and-search-local-pilot','passed':True,'production_access':False,
                    'checks':checks,'check_count':len(checks),'payload_bytes':sum(p.stat().st_size for p in root.rglob('*') if p.is_file()),
                    'payload_budget_bytes':BUDGET,'feed_sha256':feed,'wall_s':time.monotonic()-started,
                    'actual_commit_failure_tested':True,'restart_and_replay_tested':True,
                    'normal_projectRow_and_projectRemoval':True,'cloud_calls':0,
                    'binaries_sha256':{name:hashlib.sha256((BIN/name).read_bytes()).hexdigest() for name in ['qualification-native','eval-server','typeahead-ingester']},
                    'adapter_source_sha256':hashlib.sha256((HERE/'main.zig').read_bytes()).hexdigest()}
        finally:
            for child in reversed(children):stop(child)
            for log in logs:log.close()
        report['children_terminal']=all(c.poll() is not None for c in children)
        (HERE/'fixture-feed.json').write_bytes(feed_bytes)
        report['feed_artifact']='fixture-feed.json'
    report['scratch_removed']=not root.exists()
    (HERE/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['passed','check_count','payload_bytes','wall_s','children_terminal','scratch_removed']}))

if __name__=='__main__':main()
