#!/usr/bin/env python3
"""Static ASCII plan controls. Python overhead and cache state forbid engine ranking."""
import argparse
from array import array
from bisect import bisect_left
import heapq
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from run import lexical,quantiles,ROOT


def main():
    ap=argparse.ArgumentParser();ap.add_argument('output',type=Path);args=ap.parse_args()
    out=args.output.resolve();out.mkdir(exist_ok=False)
    scratch=Path(tempfile.mkdtemp(prefix='typeahead-recall-plan-controls-'))
    report={'owner':'display-name-recall-20261028','production_access':False,'scratch':str(scratch),'scratch_budget_bytes':512*1024**2,'wall_budget_seconds':300,'disk_cold':False,'sqlite_version':sqlite3.sqlite_version,'runs':[]};started=time.monotonic()
    def guard():
        if time.monotonic()-started>300:raise TimeoutError('wall budget')
        if sum(p.stat().st_size for p in scratch.rglob('*') if p.is_file())>512*1024**2:raise RuntimeError('disk budget')
        if shutil.disk_usage(scratch).free<1024**3:raise RuntimeError('free reserve')
    original=json.loads((ROOT/'docs/evals/display-name-recall-20261028/native-fixture-6/fixture-actors.json').read_text())
    queries=['Justin Fa','Fa Justin','Justin Fag','Justin Fagnani','Justin Falconer','fagnani','Justin','fa','background','common','common member','abcdefghijklmnopneed','qrstuvwxyzabcdefneed','longdisplaytokenneed','veil unique','churn','noresultsxyz','duplicate.fixture.test','justinfagnani.com']
    try:
        for n in (1000,10000,30000):
            guard();actors=[a for a in original if a['display'].isascii()]
            for i in range(n-len(actors)):actors.append({'did':f'did:plc:background{i:06}','handle':f'background{i:06}.fixture.test','display':f'Background Common Member{i%100}','score':100+i%200,'hidden':False,'pds':'https://scope.test'})
            actors.sort(key=lambda a:(-a['score'],a['did']))
            hidden={a['did'] for a in actors if a['did'].startswith('did:plc:veil') and a['did']!='did:plc:veilsurvivor'}
            deleted={a['did'] for a in actors if a['did'].startswith('did:plc:churn') and a['did']!='did:plc:churnsurvivor'}
            def oracle(q):return [a['did'] for a in sorted([a for a in actors if a['did'] not in hidden|deleted and lexical(a,q)],key=lambda a:(a['handle'].lower()!=q.lower(),-a['score'],a['did']))[:10]]
            path=scratch/f'{n}.db';db=sqlite3.connect(path);db.execute('PRAGMA cache_size=-20000')
            db.execute('CREATE TABLE actors(id INTEGER PRIMARY KEY,did TEXT,handle TEXT,display TEXT,score REAL,hidden INTEGER,pds TEXT,blob BLOB)')
            db.execute("CREATE VIRTUAL TABLE terms USING fts5(handle,display,content='actors',content_rowid='id',tokenize='ascii',prefix='2 3')")
            t=time.monotonic();db.executemany('INSERT INTO actors VALUES(?,?,?,?,?,?,?,?)',[(i,a['did'],a['handle'],a['display'],a['score'],int(a['did'] in hidden|deleted),a['pds'],a['handle'].encode()+b'\0'+a['display'].encode()) for i,a in enumerate(actors)])
            db.execute("INSERT INTO terms(terms) VALUES('rebuild')");db.commit();build=time.monotonic()-t
            calls=[0];vm=[0]
            def matches(handle,display,q):calls[0]+=1;return lexical({'handle':handle,'display':display},q)
            db.create_function('lexical',3,matches)
            def progress():vm[0]+=100;return 0
            db.set_progress_handler(progress,100)
            sql='SELECT a.id,a.did FROM terms JOIN actors a ON a.id=terms.rowid WHERE terms MATCH ? AND a.hidden=0 AND lexical(a.handle,a.display,?) ORDER BY (a.handle=?) DESC,a.score DESC,a.did ASC LIMIT 10'
            def expression(q):
                # Tokenized handles can be a superset; exact predicate runs before ORDER/LIMIT.
                tokens=q.replace('.',' ').replace('-',' ').split()
                return ' AND '.join(('display:' if ' ' in q else '')+'"'+w.replace('"','""')+'"*' for w in tokens)
            ftscases=[];ftssamples=[]
            for rep in range(5):
                for q in queries:
                    calls[0]=vm[0]=0;t=time.perf_counter_ns();rows=db.execute(sql,(expression(q),q,q)).fetchall()
                    profiles=[db.execute('SELECT blob FROM actors WHERE id=?',(r[0],)).fetchone()[0] for r in rows]
                    ftssamples.append({'sql_and_python_us':(time.perf_counter_ns()-t)/1000,'predicate_rows':calls[0],'vm_steps_approx':vm[0],'profile_rows':len(profiles),'profile_bytes':sum(map(len,profiles))})
                    if rep==0:ftscases.append({'q':q,'expected':oracle(q),'actual':[r[1] for r in rows],'predicate_rows':calls[0],'vm_steps_approx':vm[0]})
            plan=[list(r) for r in db.execute('EXPLAIN QUERY PLAN '+sql,(expression('common member'),'common member','common member'))]
            db.close()
            t=time.monotonic();postings={};exact={}
            for i,a in enumerate(actors):
                exact.setdefault(a['handle'],[]).append(i)
                keys=set()
                h=a['handle'].lower();suffixes=['.'.join(h.split('.')[j:]) for j in range(1,len(h.split('.')))]
                for kind,terms in [('h',[h]),('s',suffixes),('n',a['display'].lower().split())]:
                    for term in terms:
                        for end in range(1,len(term)+1):keys.add(kind+':'+term[:end])
                for key in keys:postings.setdefault(key,array('I')).append(i)
            rankbuild=time.monotonic()-t
            # Exact byte count for this specified uncompressed wire representation; no container estimate.
            encoded_bytes=sum(2+len(k.encode())+4+4*len(v) for k,v in postings.items())
            membership=sum(map(len,postings.values()))
            rankcases=[];ranksamples=[]
            for rep in range(5):
                for q in queries:
                    t=time.perf_counter_ns();steps=0;result=[];seen=set()
                    def eligible(i):return actors[i]['did'] not in hidden|deleted
                    for i in exact.get(q.lower(),[]):
                        steps+=1
                        if eligible(i):result.append(i);seen.add(i)
                        if len(result)==10:break
                    words=q.lower().split()
                    lists=[postings.get('n:'+w,array('I')) for w in words] if len(words)>1 else [postings.get(k+':'+q.lower(),array('I')) for k in ('h','n','s')]
                    if len(result)<10:
                        if len(words)>1:
                            lead=min(lists,key=len);others=[v for v in lists if v is not lead];positions=[0]*len(others)
                            for i in lead:
                                steps+=1;found=True
                                for j,v in enumerate(others):
                                    positions[j]=bisect_left(v,i,positions[j]);steps+=1
                                    if positions[j]==len(v) or v[positions[j]]!=i:found=False;break
                                if found and i not in seen and eligible(i):result.append(i);seen.add(i)
                                if len(result)==10:break
                        else:
                            for i in heapq.merge(*lists):
                                steps+=1
                                if i not in seen and eligible(i):result.append(i);seen.add(i)
                                if len(result)==10:break
                    actual=[actors[i]['did'] for i in result]
                    ranksamples.append({'python_us':(time.perf_counter_ns()-t)/1000,'id_iterations_and_seeks':steps,'profile_rows':len(result)})
                    if rep==0:rankcases.append({'q':q,'expected':oracle(q),'actual':actual,'id_iterations_and_seeks':steps})
            report['runs'].append({'actors':n,'fts':{'cases':ftscases,'correct':sum(c['expected']==c['actual'] for c in ftscases),'build_seconds':build,'sqlite_file_bytes':path.stat().st_size,'plan':plan,'metrics':{k:quantiles([s[k] for s in ftssamples]) for k in ftssamples[0]}},'rank_id_control':{'cases':rankcases,'correct':sum(c['expected']==c['actual'] for c in rankcases),'build_seconds':rankbuild,'keys':len(postings),'memberships':membership,'wire_bytes_u32':encoded_bytes,'decoded_membership_payload_bytes':membership*4,'metrics':{k:quantiles([s[k] for s in ranksamples]) for k in ranksamples[0]}}})
            path.unlink();(out/'results.json').write_text(json.dumps(report,indent=2)+'\n');del postings
        report['complete']=True
    except BaseException as e:report['error']=str(e);raise
    finally:
        report['scratch_bytes_before_cleanup']=sum(p.stat().st_size for p in scratch.rglob('*') if p.is_file());shutil.rmtree(scratch);report['scratch_removed']=not scratch.exists();report['wall_seconds']=time.monotonic()-started
        (out/'results.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:report.get(k) for k in ('complete','error','scratch_removed','wall_seconds')}))

if __name__=='__main__':main()
