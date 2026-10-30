"""Real 32-actor native evaluator ownership guard pilot; no cloud/source credentials."""
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
import urllib.request

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
BIN=HERE/'.build/host/bin'
BUILDER=REPO/'experiments/tantivy-qualification-native/.tools/bin/typeahead-ingester'

def main():
    checks=[];children=[];started=time.monotonic()
    env={k:os.environ[k] for k in ['PATH','HOME']}
    with tempfile.TemporaryDirectory(prefix='.guard-',dir=HERE) as temporary:
        scratch=Path(temporary).resolve();root=scratch/'private';root.mkdir();(root/'owner').write_text('native-qualification-v1')
        outside=scratch/'outside';outside.mkdir();outside_db=outside/'outside.db';outside_db.write_bytes(b'protected fixture witness')
        witness=hashlib.sha256(outside_db.read_bytes()).hexdigest()
        source=root/'source.db'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
            db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(f'did:fixture:{i:02}',f'g{i:02}.fixture.test',f'Guard Actor {i}','','[]','https://local.test','','{}',0,i,int(time.time())-60) for i in range(32)])
        settings=env|{'MODE':'indexer','INDEX_CHANNEL':'local','INDEX_SOURCE':'export','INDEX_SAMPLE_LIMIT':'0','INDEX_PDS_MAX_ACTORS':'10000',
                      'INDEX_REUSE_SOURCE':str(source),'INDEX_BUILD_ROOT':str(root/'build'),'INDEXER_VERSION':'qualification-guard-pilot'}
        with (scratch/'builder.log').open('w') as log:subprocess.run([str(BUILDER)],cwd=REPO/'services',env=settings,stdout=log,stderr=log,check=True,timeout=60)
        snapshot=next((root/'build').glob('build-*/index.db'))
        common=env|{'TYPEAHEAD_EVAL_NONCE':'qualification-guard-pilot','TYPEAHEAD_EVAL_INPUT_ID':'0'*64,
                    'TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT':'1','TYPEAHEAD_EVAL_PRIVATE_ROOT':str(root),
                    'TYPEAHEAD_EVAL_SNAPSHOT':str(snapshot),'LOCAL_DB_PATH':str(root/'overlay.db'),
                    'SQLITE_TMPDIR':str(root/'tmp'),'TYPEAHEAD_EVAL_QUALIFICATION':'1',
                    'FLY_APP_NAME':'typeahead-qual-pilot','FLY_MACHINE_ID':'fixture-guard-machine'}
        def check(name,value):
            if not value:raise AssertionError(name)
            checks.append(name)
        def stop(child):
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
        def negative(name,overrides):
            target=root/(name+'.db')
            with (scratch/(name+'.log')).open('w') as log:
                result=subprocess.run([str(BIN/'eval-server')],cwd=REPO/'services',env=common|{'PORT':'19091','LOCAL_DB_PATH':str(target)}|overrides,stdout=log,stderr=log,timeout=5)
            check(name,result.returncode!=0 and not target.exists())
            check(name+':outside-unchanged',hashlib.sha256(outside_db.read_bytes()).hexdigest()==witness)
        try:
            for machine in ['8e3d79b70dd748','801539a6551778','7845636a3d0ee8']:
                negative('protected-'+machine,{'FLY_MACHINE_ID':machine})
            negative('unexpected-app',{'FLY_APP_NAME':'typeahead-search-other'})
            negative('protected-root',{'TYPEAHEAD_EVAL_PRIVATE_ROOT':'/data/refresh'})
            (root/'outside-link.db').symlink_to(outside_db)
            negative('snapshot-symlink',{'TYPEAHEAD_EVAL_SNAPSHOT':str(root/'outside-link.db')})
            (root/'inside-link.db').symlink_to(snapshot)
            negative('inside-symlink',{'TYPEAHEAD_EVAL_SNAPSHOT':str(root/'inside-link.db')})
            prefix=Path(str(snapshot)+'.prefix');backup=Path(str(prefix)+'.backup')
            if prefix.exists():prefix.rename(backup)
            prefix.symlink_to(outside_db)
            negative('prefix-symlink',{})
            prefix.unlink()
            if backup.exists():backup.rename(prefix)
            negative('overlay-symlink',{'LOCAL_DB_PATH':str(root/'outside-link.db')})
            (root/'alias').symlink_to(outside,target_is_directory=True)
            negative('parent-symlink',{'LOCAL_DB_PATH':str(root/'alias'/'new.db')})
            negative('capture-outside',{'TYPEAHEAD_EVAL_CAPTURE_FEED':str(outside/'capture.json')})
            negative('old-background-writer',{'TYPEAHEAD_EVAL_FEED':str(root/'old-feed.json')})
            (root/'owner').write_text('wrong-owner');negative('wrong-owner',{});(root/'owner').write_text('native-qualification-v1')
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
            with (scratch/'positive.log').open('w') as log:
                child=subprocess.Popen([str(BIN/'eval-server')],cwd=REPO/'services',env=common|{'PORT':str(port)},stdout=log,stderr=log,start_new_session=True);children.append(child)
                deadline=time.monotonic()+10
                while True:
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/__evaluation',timeout=1) as r:identity=json.load(r)
                        break
                    except OSError:
                        if child.poll() is not None or time.monotonic()>deadline:raise RuntimeError('positive qualifier did not start: '+(scratch/'positive.log').read_text()[-1200:])
                        time.sleep(.05)
                check('positive-qualification-app',identity['nonce']=='qualification-guard-pilot')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/search?q=g00.fixture.test&limit=10',timeout=2) as r:body=json.load(r)
                check('positive-real-snapshot-search',body['actors'][0]['did']=='did:fixture:00')
                stop(child)
            bytes_used=sum(p.stat().st_size for p in scratch.rglob('*') if p.is_file())
            check('payload-budget',bytes_used<=250*1024*1024)
        finally:
            for child in reversed(children):stop(child)
        report={'passed':True,'kind':'actual-native-qualification-guard','actors':32,'checks':checks,'check_count':len(checks),
                'payload_bytes':bytes_used,'wall_s':time.monotonic()-started,'children_terminal':all(p.poll() is not None for p in children),
                'cloud_calls':0,'eval_server_sha256':hashlib.sha256((BIN/'eval-server').read_bytes()).hexdigest(),
                'guard_source_sha256':hashlib.sha256((REPO/'services/src/eval_server.zig').read_bytes()).hexdigest()}
    report['scratch_removed']=not scratch.exists();(HERE/'guard-results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))

if __name__=='__main__':main()
