#!/usr/bin/env python3
"""Local synthetic retrieval pilot; never reads service credentials or production."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from prepare import prepare, ROOT

BUDGET = 4 * 1024**3
ENV = {k:os.environ[k] for k in ('PATH','HOME')}
ENV['LANG']='C.UTF-8'


def quantiles(values):
    values=sorted(values)
    return {'count':len(values),'p50':values[int((len(values)-1)*.5)],'p95':values[int((len(values)-1)*.95)],'p99':values[int((len(values)-1)*.99)],'max':values[-1]} if values else None


def lexical(a,q):
    words=q.lower().split()
    display=a['display'].lower().split()
    if len(words)>1:
        return all(any(t.startswith(w) for t in display) for w in words)
    q=words[0]
    h=a['handle'].lower()
    return h.startswith(q) or any(t.startswith(q) for t in h.split('.')) or any(t.startswith(q) for t in display)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('output',type=Path)
    ap.add_argument('--sizes',default='1000,10000')
    args=ap.parse_args()
    out=args.output.resolve();out.mkdir(exist_ok=False)
    scratch=Path(tempfile.mkdtemp(prefix='typeahead-recall-comparison-'))
    children=[];streams=[];started=time.monotonic()
    report={'owner':'display-name-recall-20261028','source':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'production_access':False,'scratch':str(scratch),'scratch_budget_bytes':BUDGET,'wall_budget_seconds':1800,'free_reserve_bytes':2*1024**3,'disk_cold':False,'runs':[]}
    def save(): (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    def guard():
        if time.monotonic()-started>1800:raise TimeoutError('wall budget')
        if shutil.disk_usage(scratch).free<2*1024**3:raise RuntimeError('disk reserve')
        if sum(p.stat().st_size for p in scratch.rglob('*') if p.is_file() and not p.is_symlink())>BUDGET:raise RuntimeError('disk budget')
    def spawn(cmd,cwd,env,log):
        stream=(out/log).open('w');streams.append(stream)
        p=subprocess.Popen(cmd,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True);children.append(p);return p
    def stop(p):
        if p.poll() is None:
            os.killpg(p.pid,signal.SIGTERM)
            try:p.wait(timeout=4)
            except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=4)
    def run(cmd,cwd,log,timeout,env=None):
        p=spawn(cmd,cwd,env or ENV,log);deadline=time.monotonic()+timeout
        while p.poll() is None:
            guard()
            if time.monotonic()>deadline:raise TimeoutError(log)
            time.sleep(.2)
        if p.returncode:raise RuntimeError(log+' failed')
    def interrupted(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    try:
        binaries=prepare(scratch/'source-copies',run);report['variants']=binaries;save()
        original=json.loads((ROOT/'docs/evals/display-name-recall-20261028/native-fixture-6/fixture-actors.json').read_text())
        queries=['Justin Fa','Fa Justin','Justin Fag','Justin Fagnani','Justin Falconer','fagnani','Justin','fa','other','background','common','common member','abcdefghijklmnopneed','qrstuvwxyzabcdefneed','longdisplaytokenneed','veil unique','churn','noresultsxyz','duplicate.fixture.test','justinfagnani.com']
        for count in map(int,args.sizes.split(',')):
            if not len(original)<=count<65535:raise ValueError('size outside complete fixture format')
            actors=[dict(a) for a in original]
            for i in range(count-len(actors)):
                actors.append({'did':f'did:plc:background{i:06}','handle':f'background{i:06}.fixture.test','display':f'Background Common Member{i%100}','score':100+i%200,'hidden':False,'pds':'https://scope.test'})
            folder=scratch/str(count);folder.mkdir()
            source=folder/'actors.db';stamp=int(time.time())-60
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,handle TEXT,display_name TEXT,avatar_url TEXT,labels TEXT,pds TEXT,created_at TEXT,associated TEXT,hidden INTEGER,quality_score REAL,updated_at INTEGER)')
                db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?,?,?)',[(a['did'],a['handle'],a['display'],'','[]',a['pds'],'','{}',0,a['score'],stamp) for a in actors])
            snapshots={}
            for variant in ('baseline','complete'):
                build=folder/('build-'+variant)
                env=ENV|{'MODE':'indexer','INDEX_CHANNEL':'local','INDEX_SOURCE':'export','INDEX_SAMPLE_LIMIT':'0','INDEX_PDS_MAX_ACTORS':'10000','INDEX_REUSE_SOURCE':str(source),'INDEX_BUILD_ROOT':str(build),'INDEXER_VERSION':report['source']}
                if variant=='complete':env|={k:'65535' for k in ['INDEX_TOP_N_SHORT','INDEX_TOP_N_MEDIUM','INDEX_TOP_N_LONG']}
                t=time.monotonic();run([binaries[variant]['builder']],ROOT/'services',f'{count}-{variant}-index.log',300,env)
                path=next(build.glob('build-*/index.db'))
                with sqlite3.connect(path) as db:
                    postings=db.execute('SELECT count(*),sum(length(dids)) FROM prefix_top_base').fetchone()
                    memberships=sum(int.from_bytes(r[0][1:3],'little') for r in db.execute('SELECT dids FROM prefix_top_base'))
                    blobs=dict(db.execute('SELECT did,blob FROM actor_compact_base'))
                snapshots[variant]={'path':path,'build_seconds':time.monotonic()-t,'bytes':path.stat().st_size,'keys':postings[0],'posting_bytes':postings[1],'memberships':memberships,'blobs':blobs}
            for variant in binaries:
                guard();snap=snapshots['complete' if variant=='complete' else 'baseline'];overlay=folder/(variant+'-overlay.db')
                arm={'actors':count,'variant':variant,'snapshot':{k:v for k,v in snap.items() if k not in ('path','blobs')},'phases':[]}
                report['runs'].append(arm);save()
                with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
                base=f'http://127.0.0.1:{port}'
                env=ENV|{'TYPEAHEAD_EVAL_NONCE':'recall-comparison-local-only','TYPEAHEAD_EVAL_SNAPSHOT':str(snap['path']),'LOCAL_DB_PATH':str(overlay),'SQLITE_TMPDIR':str(folder/'tmp'),'PORT':str(port)}
                def request(q=None,scope=None):
                    params={'q':q,'limit':10} if q else {}
                    if scope:params['pds']=scope
                    path='/debug/search' if q else '/__evaluation'
                    t=time.perf_counter_ns()
                    with urllib.request.urlopen(base+path+'?'+urllib.parse.urlencode(params),timeout=10) as r:body=json.load(r)
                    return body,(time.perf_counter_ns()-t)/1000
                def start(label):
                    p=spawn([binaries[variant]['server']],ROOT/'services',env,f'{count}-{variant}-{label}.log');deadline=time.monotonic()+20
                    while True:
                        try:
                            identity,_=request()
                            if identity.get('nonce')!='recall-comparison-local-only':raise RuntimeError('wrong server')
                            return p
                        except OSError:
                            if p.poll() is not None or time.monotonic()>deadline:raise RuntimeError('server startup')
                            time.sleep(.05)
                hidden={a['did'] for a in actors if a['did'].startswith('did:plc:veil') and a['did']!='did:plc:veilsurvivor'}
                deleted={a['did'] for a in actors if a['did'].startswith('did:plc:churn') and a['did']!='did:plc:churnsurvivor'}
                def oracle(q,scope=None,changed=False):
                    eligible=[a for a in actors if (not changed or a['did'] not in hidden|deleted) and (not scope or a['pds']=='https://'+scope) and lexical(a,q)]
                    return [a['did'] for a in sorted(eligible,key=lambda a:(a['handle'].lower()!=q.lower(),-a['score'],a['did']))[:10]]
                def phase(label,repeats,changed=False,scoped=False,minimum_seconds=0):
                    samples=[];errors=[];cases=[];phase_started=time.monotonic();rep=0
                    while rep<repeats or time.monotonic()-phase_started<minimum_seconds:
                        order=list(queries)
                        random.Random(rep+10).shuffle(order)
                        for q in order:
                            scope='scope.test' if scoped else None
                            try:
                                body,us=request(q,scope)
                                samples.append({'engine_us':body['engine_us'],'http_us':us,'profile_candidates':body['profile_candidates'],'profile_blobs_decoded':body['profile_blobs_decoded'],'profile_bytes_loaded':body['profile_bytes_loaded'],'posting_entries_returned':sum(k['base']+k['overlay'] for k in body['keys']),'budget_exhausted':body['candidate_budget_exhausted']})
                                if rep==0:
                                    actual=[a['did'] for a in body['candidates'][:10]];expected=oracle(q,scope,changed)
                                    cases.append({'q':q,'pds':scope,'expected':expected,'actual':actual,'correct':actual==expected,'trace':body})
                            except Exception as e:errors.append({'q':q,'error':str(e)})
                        rep+=1
                    rss=int(subprocess.check_output(['ps','-o','rss=','-p',str(p.pid)],text=True).strip())*1024
                    result={'name':label,'attempts':rep*len(queries),'wall_seconds':time.monotonic()-phase_started,'errors':errors,'rss_bytes_after':rss,'cases':cases,'metrics':{k:quantiles([s[k] for s in samples]) for k in samples[0] if k!='budget_exhausted'} if samples else {},'budget_exhaustions':sum(s['budget_exhausted'] for s in samples),'samples':samples}
                    arm['phases'].append(result);save()
                p=start('initial');phase('process-first-pass-not-disk-cold',1);phase('warm',10);phase('scoped-warm',2,scoped=True)
                with sqlite3.connect(overlay) as db:
                    for did in hidden:db.execute('INSERT OR REPLACE INTO actor_overlay(did,blob,hidden,updated_at,source) VALUES(?,?,1,?,0)',(did,snap['blobs'][did],stamp+100))
                    for did in deleted:db.execute('INSERT OR REPLACE INTO actor_overlay(did,blob,hidden,updated_at,source) VALUES(?,NULL,0,?,0)',(did,stamp+100))
                phase('hidden-and-deleted',2,changed=True)
                stop(p)
                feed=folder/(variant+'-feed.json');trigger=folder/(variant+'-start')
                selected=actors[:100]
                feed.write_text(json.dumps([{'did':a['did'],'blob_hex':snap['blobs'][a['did']].hex(),'updated_at':stamp+200+i} for i,a in enumerate(selected)]))
                env|={'TYPEAHEAD_EVAL_FEED':str(feed),'TYPEAHEAD_EVAL_MAX_ACTORS':'100','TYPEAHEAD_EVAL_WRITER_MODE':'mixed','TYPEAHEAD_EVAL_WRITER_START':str(trigger),'TYPEAHEAD_EVAL_WRITER_DELAY_MS':'10'}
                p=start('mixed');identity,_=request();arm['mixed_writer_before']=identity.get('writer');trigger.touch();phase('mixed-native-projection',10,changed=True,minimum_seconds=3)
                identity,_=request();arm['mixed_writer']=identity.get('writer')
                arm['overlay_bytes']=sum(p.stat().st_size for p in folder.glob(variant+'-overlay.db*'))
                stop(p);save()
            shutil.rmtree(folder)
        report['complete']=True
    except BaseException as e:
        report['error']=type(e).__name__+': '+str(e);raise
    finally:
        for p in reversed(children):stop(p)
        for stream in streams:stream.close()
        report['scratch_bytes_before_cleanup']=sum(p.stat().st_size for p in scratch.rglob('*') if p.is_file() and not p.is_symlink())
        shutil.rmtree(scratch)
        report['scratch_removed']=not scratch.exists();report['children_terminal']=all(p.poll() is not None for p in children);report['wall_seconds']=time.monotonic()-started;save()
        print(json.dumps({k:report.get(k) for k in ('complete','error','wall_seconds','scratch_removed','children_terminal')}))

if __name__=='__main__':main()
