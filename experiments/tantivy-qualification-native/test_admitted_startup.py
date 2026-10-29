"""Actual Linux writer startup/admission/IO pilot; synthetic data, no network."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
sys.path.insert(0,str(REPO/'scripts/snapshot_compaction'))
from reference_build import build

def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def packed(i):
    blob=bytearray(struct.pack('<BBf',1,False,float(i)))
    for value in [f'actor{i:04}.fixture.test','Actor '+str(i),'x'*60000 if i else '','[]','','','{}']:
        raw=value.encode();blob.extend(struct.pack('<H',len(raw)));blob.extend(raw)
    return bytes(blob)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--writer',type=Path,required=True)
    parser.add_argument('--admit',type=Path,default=Path('/app/bin/admit-prefix'));parser.add_argument('--receipt',type=Path)
    args=parser.parse_args()
    if sys.platform!='linux':raise RuntimeError('real Linux proc IO required')
    started=time.monotonic();checks=[];measurements={};children=[]
    with tempfile.TemporaryDirectory(prefix='native-admitted-') as directory:
        root=Path(directory).resolve();(root/'owner').write_text('native-qualification-v1');(root/'tmp').mkdir()
        source=root/'legacy.db'
        with closing(sqlite3.connect(source)) as db:
            db.executescript('CREATE TABLE actor_compact_base(did TEXT PRIMARY KEY,blob BLOB NOT NULL) WITHOUT ROWID;CREATE TABLE prefix_top_base(key TEXT PRIMARY KEY,dids BLOB NOT NULL) WITHOUT ROWID;CREATE TABLE index_meta(key TEXT PRIMARY KEY,value TEXT);')
            db.executemany('INSERT INTO actor_compact_base VALUES(?,?)',[(f'did:fixture:{i:04}',packed(i))for i in range(1024)])
            db.executemany('INSERT INTO index_meta VALUES(?,?)',{'build_id':'fixture-native-admission','normalize_version':'v2-cjk-bigram','scoring_version':'v1-quality-score','source_watermark':'1000','actor_count':'1024','prefix_count':'0'}.items());db.commit()
        release=root/'snapshot';build(source,release);source.unlink()
        snapshot=release/'index.db';prefix=release/'index.db.prefix';prefix_sha=sha(prefix)
        prefix_bytes=prefix.stat().st_size
        if not 50*1024**2<=prefix_bytes<=100*1024**2:raise AssertionError('sized fixture budget')
        base={key:os.environ[key]for key in ['PATH','HOME']if key in os.environ}
        base.update(NATIVE_QUAL_ROOT=str(root),TYPEAHEAD_EVAL_SNAPSHOT=str(snapshot),SQLITE_TMPDIR=str(root/'tmp'),
                    NATIVE_QUAL_FEED_SHA256='a'*64,NATIVE_QUAL_SECONDS='60')
        def check(name,condition):
            if not condition:raise AssertionError(name)
            checks.append(name)
        def admit():
            subprocess.run([str(args.admit)],env=base|{'TYPEAHEAD_ADMIT_PREFIX_PATH':str(prefix),'TYPEAHEAD_ADMIT_PREFIX_SHA256':prefix_sha},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=30)
        def run(name,flag,success):
            job=root/name;job.mkdir()
            request=job/'request.json';response=job/'response.json';overlay=job/'overlay.db'
            payload={'feed_sha256':'a'*64,'cursor':1,'expected_cursor':0,'actors':[{'did':'did:fixture:0000','handle':'renamed.fixture.test','display_name':'Renamed','quality_score':999,'version':1001}]}
            request.write_text(json.dumps(payload))
            env=base|{'NATIVE_QUAL_REQUEST':str(request),'NATIVE_QUAL_RESPONSE':str(response),'LOCAL_DB_PATH':str(overlay)}
            if flag is not None:env['TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT']=flag
            begin=time.monotonic()
            with (job/'stderr.log').open('w') as log:
                child=subprocess.Popen([str(args.writer)],env=env,stdout=log,stderr=log,start_new_session=True);children.append(child)
                try:
                    until=time.monotonic()+30
                    while not response.exists() and child.poll() is None and time.monotonic()<until:time.sleep(.01)
                    if success:
                        check(name+':acknowledged',response.exists() and child.poll() is None)
                        ack=json.loads(response.read_text());check(name+':committed',ack['accepted'] and ack['applied']==1 and ack['transactions']==1)
                        counters={key.rstrip(':'):int(value)for key,value in [line.split()for line in Path(f'/proc/{child.pid}/io').read_text().splitlines()]}
                        measurements[name]={'startup_and_apply_seconds':time.monotonic()-begin,'rchar':counters['rchar'],'read_bytes':counters['read_bytes']}
                        with closing(sqlite3.connect(overlay))as db:
                            check(name+':actor-persisted',db.execute("SELECT count(*)FROM actor_overlay WHERE did='did:fixture:0000'AND blob IS NOT NULL").fetchone()[0]==1)
                    else:
                        child.wait(timeout=3)
                        check(name+':failed-closed',child.returncode!=0 and not response.exists() and request.exists())
                        check(name+':admission-error',any(word in (job/'stderr.log').read_text() for word in ['Admission','Admitted']))
                finally:
                    if child.poll()is None:
                        os.killpg(child.pid,signal.SIGTERM)
                        try:child.wait(timeout=3)
                        except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
        try:
            admit();run('cached','1',True)
            check('cached-prefix-read-budget',measurements['cached']['rchar']<prefix_bytes//8)
            receipt=Path(str(prefix)+'.admitted');original=receipt.read_bytes();corrupt=bytearray(original);corrupt[8]^=1;receipt.write_bytes(corrupt)
            run('invalid-receipt','1',False)
            receipt.unlink();run('missing-receipt','1',False)
            receipt.write_bytes(original)
            info=prefix.stat();os.utime(prefix,ns=(info.st_atime_ns,info.st_mtime_ns+1_000_000))
            run('stale-receipt','1',False)
            run('default-full-validation',None,True)
            check('default-reads-whole-prefix',measurements['default-full-validation']['rchar']>=prefix_bytes)
            receipt.write_bytes(corrupt)
            run('non-one-full-validation','true',True)
            check('only-exact-one-uses-admission',measurements['non-one-full-validation']['rchar']>=prefix_bytes)
            payload_bytes=sum(path.stat().st_size for path in root.rglob('*')if path.is_file())
            check('payload-under-250MiB',payload_bytes<250*1024**2)
        finally:
            check('children-terminal',all(child.poll()is not None for child in children))
        result={'passed':True,'scope':'synthetic-native-writer-admission-startup','writer_sha256':sha(args.writer),
                'prefix_bytes':prefix_bytes,'measurements':measurements,'checks':checks,'wall_seconds':time.monotonic()-started,
                'payload_bytes':payload_bytes,'cloud_calls':0,'eval_server_modified':False}
    result['scratch_retired']=not root.exists()
    if args.receipt:args.receipt.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))

if __name__=='__main__':main()
