#!/usr/bin/env python3
"""Small real-HTTP operational qualification, restricted to a synthetic localhost build."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.parse

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('readonly_replay',ROOT/'sync.py');replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)

def run(args):
    started=time.monotonic();expected=json.loads(Path(args.expected).read_text());base=replay.local_url(args.url)
    if urllib.parse.urlsplit(base).path not in ['','/']:raise replay.SafeError('fixture public target must be loopback origin')
    if not expected.get('synthetic') or not expected['build_id'].startswith('fixture-'):raise replay.SafeError('qualification requires explicit synthetic fixture identity')
    http=replay.Http(5);secret=os.environ.get(args.admin_secret_env,'');public_token='fixture-public-read';checks=mutations=0
    def status():return http.request(base+'/index/status',public_token,max_bytes=256*1024)[0]
    state=status()
    if state.get('build_id')!=expected['build_id'] or state.get('import_complete') is not True:raise replay.SafeError('target is not the completed matching synthetic fixture')
    def search(q,ids):
        nonlocal checks
        params={'q':q['q'],'limit':'10'}
        for k in ['pds','prefer_pds','preference','boost']:
            if q.get(k) is not None:params[k]=q[k]
        body=http.request(base+'/search?'+urllib.parse.urlencode(params),public_token,max_bytes=512*1024)[0]
        if [a['did'] for a in body.get('actors',[])]!=ids:raise replay.SafeError('fixture query failed: '+q['id'])
        checks+=1;return body
    for q in expected['queries']+expected.get('preference_queries',[]):search(q,q['expected_ids'])
    exact=next(q for q in expected['queries'] if q['id']=='exact-priority');body=search(exact,exact['expected_ids'])
    for actor in body['actors']:
        want=expected.get('hydration_by_did',{}).get(actor['did'])
        if want is not None:
            if actor!=want:raise replay.SafeError('fixture hydration differs')
            checks+=1
    if args.mutate:
        if not secret:raise replay.SafeError('synthetic admin mutation requires cloud-only ADMIN_SECRET')
        if not args.input:raise replay.SafeError('synthetic mutation requires original fixture JSONL')
        records=[json.loads(l) for l in Path(args.input).read_bytes().splitlines()]
        originals={a['did']:a for a in records if a.get('type')=='actor'}
        if records[0]['build_id']!=expected['build_id']:raise replay.SafeError('fixture mutation input identity differs')
        version=args.version_start
        if version is None or version<=records[0]['source_watermark']:raise replay.SafeError('explicit mutation version must exceed fixture snapshot')
        admin=replay.local_url(args.admin_url)
        if urllib.parse.urlsplit(admin).path not in ['','/']:raise replay.SafeError('fixture admin target must be loopback origin')
        def apply(actor):
            nonlocal mutations
            result=http.request(admin+'/admin/apply',secret,{'actors':[actor]},max_bytes=256*1024)[0];mutations+=1;return result
        local=copy.deepcopy(originals['did:example:fixture-local-joint']);remote=copy.deepcopy(originals['did:example:fixture-remote-joint'])
        global_query=next(q for q in expected['queries'] if q['id']=='joint-global');local_query=next(q for q in expected['queries'] if q['id']=='joint-local');remote_query=next(q for q in expected['queries'] if q['id']=='joint-remote')
        local.update(version=version,hidden=True);apply(local);search(local_query,[])
        replayed=apply(local)
        if replayed.get('replayed',0)<1:raise replay.SafeError('equal-version fixture replay was not acknowledged')
        stale=copy.deepcopy(local);stale.update(version=version-1,hidden=False);apply(stale);search(local_query,[])
        local.update(version=version+1,hidden=False);apply(local);search(local_query,[local['did']])
        remote.update(version=version+2,deleted=True);apply(remote);search(global_query,[local['did']])
        remote.update(version=version+1,deleted=False);apply(remote);search(remote_query,[])
        remote.update(version=version+3);apply(remote);search(remote_query,[remote['did']])
        local.update(version=version+4,pds='https://remote.test');apply(local);search(local_query,[]);search(remote_query,[remote['did'],local['did']])
        local.update(version=version+5,pds='https://local.test',quality_score=1000);apply(local);search(global_query,[local['did'],remote['did']])
        local.update(version=version+6,quality_score=100);apply(local);search(global_query,global_query['expected_ids'])
        if status().get('source_cursor')!=state.get('source_cursor'):raise replay.SafeError('fixture admin mutation changed source replay cursor')
    result={'passed':True,'synthetic':True,'build_id':expected['build_id'],'checks':checks,'admin_mutations':mutations,'wall_seconds':time.monotonic()-started,'production_contact':False,'restart_qualified':False}
    with Path(args.report).open('xb') as f:f.write((json.dumps(result,sort_keys=True)+'\n').encode())
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',default='http://127.0.0.1:8080');p.add_argument('--admin-url',default='http://127.0.0.1:8081');p.add_argument('--admin-secret-env',default='ADMIN_SECRET');p.add_argument('--input');p.add_argument('--expected',required=True);p.add_argument('--report',required=True);p.add_argument('--mutate',action='store_true');p.add_argument('--version-start',type=int);a=p.parse_args()
    def expired(*_):raise replay.SafeError('synthetic qualification wall deadline')
    signal.signal(signal.SIGALRM,expired);signal.alarm(120)
    print(json.dumps(run(a),sort_keys=True))
if __name__=='__main__':
    try:main()
    except (replay.SafeError,ValueError,KeyError,TypeError,OSError) as exc:
        reason=str(exc) if isinstance(exc,replay.SafeError) else 'fixture input or HTTP parsing failed'
        print(json.dumps({'passed':False,'error':'synthetic_qualification_failed','reason':reason}),file=sys.stderr);sys.exit(1)
