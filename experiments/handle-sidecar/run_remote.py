import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT=Path('/data/research')
EVIDENCE=Path('/data/research-evidence')
GEN=Path('/data/refresh/index/generations/b1793081565-0228-px05')
SNAP=GEN/'index.db.prefix'
OVERLAY=Path('/data/refresh/overlay.db')
SOURCE=Path('/opt/research')
BIN=SOURCE/'target/release/handle-sidecar'
CHILDREN=[]
START=time.monotonic()


def guard():
    assert time.monotonic()-START<4500
    assert shutil.disk_usage('/data').free>2*1024**3


def run(cmd,name,env=None,timeout=1200):
    guard()
    with (EVIDENCE/name).open('w') as out:
        c=subprocess.Popen([str(x) for x in cmd],stdout=out,stderr=subprocess.STDOUT,env=env,start_new_session=True)
        CHILDREN.append(c)
        try:
            while c.poll() is None:
                guard()
                if time.monotonic()-START>4500:raise TimeoutError(name)
                time.sleep(.25)
            if c.returncode:raise RuntimeError(f'{name}: subprocess exit {c.returncode}')
        finally:
            if c.poll() is None:stop(c)


def stop(c):
    if c.poll() is None:
        os.killpg(c.pid,signal.SIGTERM)
        try:c.wait(5)
        except subprocess.TimeoutExpired:os.killpg(c.pid,signal.SIGKILL);c.wait()


def status(phase):
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE/'status.json').write_text(json.dumps({'phase':phase,'elapsed_s':time.monotonic()-START})+'\n')
    print(phase,flush=True)


def metrics(c):
    p=Path('/proc')/str(c.pid)
    return {'status': '\n'.join(s for s in (p/'status').read_text().splitlines() if s.startswith(('VmRSS:','VmHWM:','RssAnon:','RssFile:'))), 'io':(p/'io').read_text()}


def evict():
    receipts={}
    for path in [SNAP,ROOT/'dict/records.bin',ROOT/'dict/offsets.bin',ROOT/'dict/handles.fst',ROOT/'dict/overlay-profiles.bin']:
        with path.open('rb') as f:os.posix_fadvise(f.fileno(),0,0,os.POSIX_FADV_DONTNEED)
        receipts[str(path.name)]=path.stat().st_size
    return receipts


def native(overlay,trigger):
    assert hashlib.sha256(Path('/app/perf/baseline').read_bytes()).hexdigest()=='58f3ac6207cb936853ba73db93def7120e6091355bc2ed7696f80f68579c3cdf'
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PORT':'18081','LOCAL_DB_PATH':str(overlay),'TYPEAHEAD_EVAL_SNAPSHOT':str(GEN/'index.db'),'TYPEAHEAD_EVAL_PRIVATE_ROOT':'/data','TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT':'1','TYPEAHEAD_EVAL_NONCE':'handle-sidecar-20261028','TYPEAHEAD_EVAL_INPUT_ID':'sidecar','TYPEAHEAD_EVAL_FEED':str(ROOT/'feed.json'),'TYPEAHEAD_EVAL_MAX_ACTORS':'5000','TYPEAHEAD_EVAL_WRITER_MODE':'mixed','TYPEAHEAD_EVAL_WRITER_START':str(trigger),'TYPEAHEAD_EVAL_WRITER_DELAY_MS':'1000','TYPEAHEAD_EVAL_BACKGROUND_SLOTS':'3','TYPEAHEAD_EVAL_SKIP_PREFILL':'1'}
    log=(EVIDENCE/(overlay.stem+'-native.log')).open('w')
    c=subprocess.Popen(['/app/perf/baseline'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True);log.close();CHILDREN.append(c)
    for _ in range(240):
        if c.poll() is not None:raise RuntimeError('native evaluator exited')
        try:
            with urllib.request.urlopen('http://127.0.0.1:18081/__evaluation',timeout=1) as response:
                value=json.load(response)
            assert value['nonce']=='handle-sidecar-20261028' and value['ready']
            return c
        except OSError:time.sleep(.5)
    raise TimeoutError('native startup')


def main():
    assert os.cpu_count()==2 and len(Path('/proc/swaps').read_text().splitlines())==1
    assert not ROOT.exists()
    ROOT.mkdir();EVIDENCE.mkdir(exist_ok=True)
    (ROOT/'owner.json').write_text(json.dumps({'owner':'handle-sidecar-20261028','budget':16*1024**3}))
    initial=shutil.disk_usage('/data')._asdict()
    status('exporting-full-handle-corpus')
    run([sys.executable,SOURCE/'export.py',SNAP,OVERLAY,ROOT/'dict'],'export.log')
    d=ROOT/'dict'
    report=json.loads((d/'input.json').read_text())
    assert report['overlay']['actor_overlay']>=80518,'larger overlay required'
    shutil.copy2(d/'input.json',EVIDENCE/'input.json')
    status('sorting-full-handles')
    run(['sort','-S','128M','-T',ROOT,'-t','\t','-k1,1','-k2,2',d/'unsorted.tsv','-o',d/'sorted.tsv'],'sort.log',env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
    (d/'unsorted.tsv').unlink()
    status('building-both-sidecars')
    run([BIN,'build',d],'build.json')
    (d/'sorted.tsv').unlink()
    samples=report.pop('samples')
    cases=[]
    for i,s in enumerate(samples[:160]):
        h=s['handle'];q=h[:min(len(h)-1,20)]
        if not q:continue
        cases.append({'id':f'exact-{i}','q':q})
        if i<50 and s['pds']:cases.append({'id':f'scope-{i}','q':h[:min(5,len(h)-1)],'pds':s['pds']})
        q=h[:min(len(h)-1,12)]
        if len(q)>=4:
            edit=q[:1]+q[2]+q[1]+q[3:]
            cases.append({'id':f'fuzzy-{i}','q':edit,'fuzzy':True})
    for q in ['a','n','alice','nate','photographer','zzstoatzz.io','abcdefghijklmnop.zeb']:
        cases.append({'id':'fixed-'+q,'q':q})
    (ROOT/'cases.json').write_text(json.dumps(cases));shutil.copy2(ROOT/'cases.json',EVIDENCE/'cases.json')
    exact=[c for c in cases if not c.get('fuzzy')];fuzzy=[c for c in cases if c.get('fuzzy')]
    for name,items in [('exact',exact),('fuzzy',fuzzy)]: (ROOT/(name+'.json')).write_text(json.dumps(items))
    status('checking-full-corpus-equivalence')
    for variant in ['sorted','fst']:
        run([BIN,'check',d,variant,SNAP,ROOT/'cases.json',20000],variant+'-check.jsonl')
    checks={v:[json.loads(s) for s in (EVIDENCE/(v+'-check.jsonl')).read_text().splitlines()] for v in ['sorted','fst']}
    comparable=matched=truncated=0
    for a,b in zip(checks['sorted'],checks['fst']):
        assert a['case']==b['case']
        if a['answer']['truncated'] or b['answer']['truncated']:truncated+=1
        else:
            comparable+=1
            assert a['answer']['hits']==b['answer']['hits'],a['case']
            assert a['answer']['checksum']==b['answer']['checksum'],a['case']
            matched+=1
    (EVIDENCE/'correctness.json').write_text(json.dumps({'complete_cases':comparable,'matched':matched,'truncated_cases':truncated,'total':len(cases)},indent=2)+'\n')
    status('capturing-native-sync-feed')
    with closing(sqlite3.connect(f'file:{OVERLAY}?mode=ro',uri=True)) as source:
        with closing(sqlite3.connect(ROOT/'frozen.db')) as dst:source.backup(dst)
        feed=[{'did':did,'blob_hex':blob.hex(),'updated_at':ts} for did,blob,ts in source.execute('SELECT did,blob,updated_at FROM actor_overlay WHERE blob IS NOT NULL AND source=0 ORDER BY did LIMIT 5000')]
    assert len(feed)==5000
    (ROOT/'feed.json').write_text(json.dumps(feed,separators=(',',':')))
    outputs=[]
    for iteration,variant in enumerate(['sorted','fst','fst','sorted']):
        for phase,casefile,attempts,rps in [('cold-exact','exact',160,20),('warm-exact','exact',240,40),('warm-fuzzy','fuzzy',160,20),('mixed-exact','exact',640,20),('mixed-fuzzy','fuzzy',640,20)]:
            guard();tag=f'{iteration}-{variant}-{phase}';status(tag)
            writer=None;trigger=ROOT/(tag+'.start');overlay=ROOT/(tag+'.db')
            record={'tag':tag,'variant':variant,'phase':phase,'cache_reset':evict() if phase.startswith('cold') else None}
            try:
                if phase.startswith('mixed'):
                    with closing(sqlite3.connect(ROOT/'frozen.db')) as src,closing(sqlite3.connect(overlay)) as dst:src.backup(dst)
                    writer=native(overlay,trigger);record['writer_before']=metrics(writer);trigger.touch()
                cmd=[BIN,'bench',d,variant,SNAP,ROOT/(casefile+'.json'),20000,attempts,rps,'hydrate']
                run(cmd,tag+'.json')
                result=json.loads((EVIDENCE/(tag+'.json')).read_text())
                expected={v['case']:v['answer']['hits'] for v in checks[variant]}
                incorrect=sum(row['hits']!=expected[row['case']] for row in result['rows'])
                assert incorrect==0
                record.update({k:v for k,v in result.items() if k!='rows'});record['incorrect']=incorrect
                if writer:
                    record['writer_after']=metrics(writer)
                    with urllib.request.urlopen('http://127.0.0.1:18081/__evaluation',timeout=10) as response:record['writer']=json.load(response)['writer']
                    assert record['writer']['failures']==0
            finally:
                if writer:stop(writer)
                for p in [overlay,Path(str(overlay)+'-wal'),Path(str(overlay)+'-shm'),trigger]:p.unlink(missing_ok=True)
            outputs.append(record);(EVIDENCE/'summary.json').write_text(json.dumps(outputs,indent=2)+'\n')
    (EVIDENCE/'storage.json').write_text(json.dumps({'initial':initial,'files':{p.name:p.stat().st_size for p in d.iterdir() if p.is_file()},'memory_total':Path('/proc/meminfo').read_text(),'native_binary_sha256':hashlib.sha256(Path('/app/perf/baseline').read_bytes()).hexdigest()},indent=2)+'\n')


if __name__=='__main__':
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    EVIDENCE.mkdir(exist_ok=True)
    try:main()
    except BaseException as exc:
        import traceback
        (EVIDENCE/'failure.json').write_text(json.dumps({'error':repr(exc),'traceback':traceback.format_exc()},indent=2)+'\n')
    finally:
        for c in CHILDREN:stop(c)
        before=shutil.disk_usage('/data').free
        if ROOT.exists():
            assert json.loads((ROOT/'owner.json').read_text())['owner']=='handle-sidecar-20261028'
            shutil.rmtree(ROOT)
        (EVIDENCE/'payload-cleanup.json').write_text(json.dumps({'research_removed':not ROOT.exists(),'free_before':before,'free_after':shutil.disk_usage('/data').free})+'\n')
        with tarfile.open(EVIDENCE/'results.tgz','w:gz') as t:
            for p in EVIDENCE.iterdir():
                if p.is_file() and p.name not in ['results.tgz','status.json']:t.add(p,arcname=p.name)
        result=EVIDENCE/'results.tgz'
        (EVIDENCE/'status.json').write_text(json.dumps({'phase':'ready','bytes':result.stat().st_size,'sha256':hashlib.sha256(result.read_bytes()).hexdigest()})+'\n')
