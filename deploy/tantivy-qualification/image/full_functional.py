"""Bounded post-timing Tantivy functional probe; no corpus scan or speed claim.

Run only after both timed arms are paused at cursor9. Native control is read-only.
All search truth and hydration expectations come from its hash-bound frozen files.
"""
import argparse
import gc
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time

sys.path.insert(0,'/app')
import full_generator as gen

class ProbeError(Exception):pass

def require(ok,code):
    if not ok:raise ProbeError(code)

class Budget:
    def __init__(self,deadline,seconds=45):
        require(0<seconds<=45,'functional_budget')
        self.end=min(time.monotonic()+seconds,time.monotonic()+deadline-time.time()-10)
    def timeout(self,maximum=5):
        remaining=self.end-time.monotonic()
        require(remaining>0,'functional_deadline')
        return min(maximum,remaining)
    def call(self,client,path,method='GET'):
        return client.ok(path,method=method,seconds=self.timeout())[0]

class Inputs:
    def __init__(self,client,budget):self.client=client;self.budget=budget
    def download(self,path,target,expected,limit):
        # Frozen helper versions may carry an older oracle transfer cap. This
        # remains the same hash-bound metadata, never a profile/corpus export.
        if path=='/inputs/oracle':limit=max(limit,64*1024*1024)
        with Path(target).open('xb') as output:
            code,_,_,checksum=self.client.request(path,limit=limit,deadline=time.monotonic()+self.budget.timeout(15),sink=output)
        require(code==200 and checksum==expected,'functional_input_hash')
        with Path(target).open() as stream:return json.load(stream),checksum

def plan(cases,truth,feed):
    affected={actor['did'] for actor in feed['baseline_actors']}
    require(len(affected)==64,'functional_affected_count')
    priority={'feed-exact':0,'feed-conjunction':1,'feed-hard-PDS':2,'feed-reversed':3}
    ordered=sorted(cases,key=lambda case:(priority.get(case.get('category'),10),case['id']))
    planned={};hydration={}
    for cursor in range(1,10):
        previous,current=truth[cursor-1],truth[cursor]
        def changed(case):
            a,b=previous[case['id']],current[case['id']]
            return a['status']==b['status']==200 and a['ids']!=b['ids'] and bool((set(a['ids'])|set(b['ids']))&affected)
        candidates=[case for case in ordered if changed(case)]
        mandatory=[]
        if cursor in {4,6}:
            mandatory=[case for case in candidates if (set(previous[case['id']]['ids'])-set(current[case['id']]['ids']))&affected]
            require(bool(mandatory),'functional_missing_removal_witness_'+str(cursor))
        if cursor==8:
            mandatory=[case for case in candidates if case.get('pds') or case.get('prefer_pds')]
            require(bool(mandatory),'functional_missing_pds_change_witness')
        chosen=[];categories=set()
        for case in mandatory[:1]+candidates:
            if case['id'] in {row['id'] for row in chosen}:continue
            if case.get('category') in categories:continue
            chosen.append(case);categories.add(case.get('category'))
            if len(chosen)==3:break
        if cursor in {3,5,7}:
            actors={actor['did']:actor for actor in feed['batches'][cursor-1]['actors']}
            witnesses=[]
            for case in ordered:
                if case.get('category')!='feed-exact' or current[case['id']]['status']!=200:continue
                for did in current[case['id']]['ids']:
                    if did in actors and not actors[did].get('hidden') and not actors[did].get('deleted'):
                        witnesses.append((case,actors[did]));break
            require(bool(witnesses),'functional_missing_hydration_witness_'+str(cursor))
            case,actor=witnesses[0];hydration[cursor]=(case['id'],actor)
            if case['id'] not in {row['id'] for row in chosen}:chosen=([case]+chosen)[:3]
        # Every stage receives at least one existing oracle query, even if its
        # presentation-only mutation leaves ranking unchanged.
        if not chosen:chosen=ordered[:1]
        planned[cursor]=[(dict(case,functional_changed_witness=(cursor in {4,6} and bool((set(previous[case['id']]['ids'])-set(current[case['id']]['ids']))&affected)) or (cursor==8 and bool(case.get('pds') or case.get('prefer_pds')) and changed(case))),truth[cursor][case['id']]) for case in chosen]
    require(sum(map(len,planned.values()))<=27,'functional_query_budget')
    return planned,hydration

def check_hydration(actual,expected):
    for public,raw in [('handle','handle'),('displayName','display_name'),('createdAt','created_at')]:
        require(actual.get(public,'')==expected.get(raw,''),'functional_hydration_'+public)
    for key,empty in [('labels',[]),('associated',{})]:
        wanted=expected.get(key,empty)
        if isinstance(wanted,str):wanted=json.loads(wanted)
        require(actual.get(key,empty)==wanted,'functional_hydration_'+key)
    require(expected.get('avatar_url','')=='','functional_hydration_avatar_precondition')
    require('avatar' not in actual and 'avatarFallback' not in actual,'functional_hydration_avatar_absence')

def run(native,control,search,nonce,oracle_binary_sha256,tantivy_binary_sha256,deadline,timed_result_sha256,*,fixture=False,max_seconds=45):
    budget=Budget(deadline,max_seconds);started=time.monotonic()
    require(gen.sha(timed_result_sha256),'functional_timed_result_hash')
    prepared={arm:budget.call(client,'/prepared') for arm,client in [('native',native),('tantivy',control)]}
    synthetic=prepared['native'].get('source_kind')=='frozen-synthetic-controller'
    gen.validate_prepared(prepared,nonce,fixture=fixture,synthetic_staged_pilot=synthetic)
    require(all(value.get('acked')==9 and value.get('state')=='paused' for value in prepared.values()),'functional_timing_not_quiescent')
    if not fixture:require(native.machine==prepared['native']['machine_id'] and control.machine==search.machine==prepared['tantivy']['machine_id'] and native.machine!=control.machine,'functional_owned_controls')
    with tempfile.TemporaryDirectory(prefix='qualification-functional-') as directory:
        cases,truth,proof=gen.load_inputs(Inputs(native,budget),prepared['native'],oracle_binary_sha256,Path(directory),fixture=fixture,synthetic_staged_pilot=synthetic)
        paths=list(Path(directory).glob('inputs-*/updates.json'));require(len(paths)==1,'functional_feed_path')
        feed=json.loads(paths[0].read_bytes());planned,hydration=plan(cases,truth,feed)
    del cases,truth,feed
    gc.collect()
    errors=[];queries=0;hydrated=0;cursor=9;reset=False;stages=0;changed_seen=set();provider_verified=False
    def record(code):
        if code not in errors:errors.append(code)
    try:
        value=budget.call(control,'/reset','POST');reset=True;cursor=0
        require(value.get('reset') is True and value.get('acked')==0,'functional_reset_ack')
        budget.call(control,'/serve','POST')
        identity=gen.wait_identity(search,control,'tantivy',prepared['tantivy'],nonce,tantivy_binary_sha256,fixture=fixture,expected_cursor=0,until=time.monotonic()+budget.timeout(8))
        if not fixture:
            require(isinstance(identity.get('provider'),dict),'functional_provider_proof');provider_verified=True
        for cursor in range(1,10):
            ack=budget.call(control,'/apply/'+str(cursor),'POST')
            require(ack.get('acked')==cursor and ack.get('replay') is False,'functional_apply_ack')
            stages+=1
            for case,expected in planned[cursor]:
                queries+=1;require(queries<=64,'functional_query_budget')
                status,headers,value,_=search.request(gen.query_path(case),deadline=time.monotonic()+budget.timeout(3))
                error=gen.check_answer(status,headers,value,case,{cursor:{case['id']:expected}},cursor,cursor,'tantivy')
                if error:record('functional_'+str(cursor)+'_'+error)
                elif case.get('functional_changed_witness'):changed_seen.add(cursor)
                if cursor in hydration and case['id']==hydration[cursor][0]:
                    actor=hydration[cursor][1]
                    returned=next((row for row in value.get('actors',[]) if row.get('did')==actor['did']),None) if isinstance(value,dict) else None
                    try:
                        require(returned is not None,'functional_hydration_actor_missing');check_hydration(returned,actor);hydrated+=1
                    except ProbeError as error:record(str(error)+'_'+str(cursor))
        require(hydrated==3,'functional_hydration_count')
    except Exception as error:
        record(str(error) if isinstance(error,ProbeError) else 'functional_transport_or_identity')
    finally:
        if reset:
            # Search mismatches do not interrupt restoration. For transport errors,
            # make a bounded best effort to finish the same monotonic frozen feed.
            try:
                current=budget.call(control,'/prepared').get('acked')
                require(type(current) is int and 0<=current<=9,'functional_final_cursor')
                for pending in range(current+1,10):budget.call(control,'/apply/'+str(pending),'POST')
                cursor=budget.call(control,'/prepared').get('acked')
            except Exception:cursor=None;record('functional_restoration_unconfirmed')
            try:budget.call(control,'/pause','POST')
            except Exception:record('functional_pause_unconfirmed')
    return {'schema_version':1,'scope':'post-measurement-functional-witnesses','passed':not errors and stages==9 and hydrated==3 and cursor==9 and changed_seen=={4,6,8},
            'performance_qualified':False,'full_corpus_qualified':False,'production_contact':False,'native_mutated':False,
            'actor_count':prepared['native']['actor_count'],'local_fixture':fixture,'runtime_provider_proof':provider_verified,
            'query_count':queries,'applied_stages':stages,'hydration_witnesses':hydrated,'changed_witness_stages':sorted(changed_seen),'required_changed_witness_stages':[4,6,8],
            'probe_sha256':gen.digest(Path(__file__).read_bytes()),'frozen_helper_sha256':gen.digest(Path(gen.__file__).read_bytes()),
            'final_cursor':cursor,'errors':errors,'wall_seconds':time.monotonic()-started,'timed_result_sha256':timed_result_sha256,
            'input_hashes':{key:prepared['native'][key] for key in ['cases_sha256','oracle_sha256','updates_sha256']},'oracle':proof}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ['native-control-url','tantivy-control-url','tantivy-url','oracle-binary-sha256','tantivy-binary-sha256','timed-result-sha256']:parser.add_argument('--'+key,required=True)
    args=parser.parse_args()
    require(os.environ.get('FLY_APP_NAME')==gen.APP,'functional_private_app')
    deadline=float(os.environ['QUAL_DEADLINE']);nonce=os.environ['QUAL_PILOT_NONCE'];secret=os.environ['QUAL_CONTROL_SECRET']
    def expired(*_):raise ProbeError('functional_deadline')
    signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,min(45,max(.001,deadline-time.time()-10)))
    native=gen.Client(args.native_control_url,8082,secret);control=gen.Client(args.tantivy_control_url,8082,secret);search=gen.Client(args.tantivy_url,8080)
    result=run(native,control,search,nonce,args.oracle_binary_sha256,args.tantivy_binary_sha256,deadline,args.timed_result_sha256)
    print('QUALIFICATION_POST_MEASUREMENT_FUNCTIONAL '+json.dumps(result,separators=(',',':')),flush=True)
    if not result['passed']:raise SystemExit(2)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('QUALIFICATION_POST_MEASUREMENT_FUNCTIONAL '+json.dumps({'passed':False,'scope':'post-measurement-functional-witnesses','performance_qualified':False,'full_corpus_qualified':False,'status':'inconclusive' if isinstance(error,ProbeError) and str(error).startswith('functional_missing_') else 'failed','error':str(error) if isinstance(error,ProbeError) else 'functional_failed'},separators=(',',':')),flush=True)
        raise SystemExit(1)
