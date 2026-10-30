import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from smoke import BIN, actor, request, start, stop


def seed(path, rows, expected=None, source_count=None, bad_footer=False):
    floor=rows[0]['version'];count=len(rows) if expected is None else expected
    lines=[json.dumps(row,separators=(',',':')).encode()+b'\n' for row in sorted(rows,key=lambda row:row['did'])]
    checksum=hashlib.sha256(b''.join(lines)).hexdigest()
    header={'type':'manifest','build_id':f'b{floor}-expiry','source_watermark':floor,'actor_count':count,'source_actor_count':count if source_count is None else source_count,'partial':False,'normalize_version':'v2-cjk-bigram'}
    footer={'type':'end','build_id':header['build_id'],'actor_count':len(rows),'actors_sha256':'0'*64 if bad_footer else checksum}
    path.write_bytes(json.dumps(header).encode()+b'\n'+b''.join(lines)+json.dumps(footer).encode()+b'\n')
    return checksum


def imported(seed_path,data):
    return subprocess.run([str(BIN),'import','--data',str(data),'--input',str(seed_path),'--batch-size','1'],capture_output=True,timeout=30)


def status(data):
    result=subprocess.run([str(BIN),'status','--data',str(data)],capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def observe(admin,secret,rows,step):
    old=request(admin,'/admin/source-state',{'dids':[row['did'] for row in rows]},secret)[2]
    body={'actors':rows,'expected_documents':old['actors'],'expected_source_cursor':old['source_cursor'],'source_cursor':{'step':step},'expected_observation_revision':old['source_observation_revision'],'batch_id':hashlib.sha256(f'expiry-step-{step}'.encode()).hexdigest()}
    code,_,receipt=request(admin,'/admin/source-reconcile',body,secret)
    assert code==200,(code,receipt)
    return receipt


def run():
    checks=[];floor=int(time.time())-10
    with tempfile.TemporaryDirectory(prefix='tantivy-expiry-coverage-') as temporary:
        root=Path(temporary);secret=secrets.token_urlsafe(24)
        rows=[actor(n,f'coverage{n}.test','Coverage Needle') for n in range(1,8)]
        for row in rows:row['version']=floor
        rows[1]['labels']=[{'src':'did:plc:custom','val':'custom','exp':'2030-01-01T00:00:00Z'}]
        rows[2]['labels']=[{'src':'did:plc:custom','val':'custom','exp':'unsupported-expiry-private-sentinel'}]
        rows[3]['hidden']=True;rows[3]['labels']=[None]
        rows[4]['labels']=[{'neg':True,'exp':{'unsupported':'ignored because negated'}}]
        rows[5]['labels']=[{'src':'did:plc:ar7c4by46qjdydhdevvrndac','val':'spam'}]
        rows[6]['deleted']=True;rows[6]['labels']=[None]
        input_path=root/'corpus.jsonl';data=root/'data';checksum=seed(input_path,rows)
        result=imported(input_path,data);assert result.returncode==0,result.stderr
        metadata=json.loads(result.stdout);coverage=metadata['expiry_coverage']
        assert metadata['format']=='tantivy-0.27-f32-did-expiry-v4'
        assert coverage['import_complete'] and coverage['full_source'] and coverage['expected_input_actors']==7 and coverage['source_actor_count']==7 and coverage['input_actors_checked']==7
        assert coverage['verified_input_actors_sha256']==checksum
        assert coverage['current']['live_actors']==metadata['actor_count']==6
        assert coverage['current']['actors_with_unsupported_expiry']==coverage['current']['actors_with_malformed_labels']==1
        assert coverage['current']['unsupported_expiry']==coverage['current']['supported_expiry']==1
        assert coverage['current']['candidate_hide_labels']==1
        checks.append('full_stream_count_footer_hash_and_hidden_actor_coverage')
        with (root/'server.log').open('wb') as log:
            child,public,admin=start(data,log,secret)
            try:
                code,_,receipt=request(public,'/ready');assert code==503
                code,_,receipt=request(public,'/search?q=Coverage');assert code==503 and 'actors' not in receipt
                public_status=request(public,'/index/status')[2]
                assert not public_status['expiry_coverage_ready'] and public_status['expiry_coverage_state']=='blocked'
                telemetry=json.dumps(public_status['expiry_coverage']);assert 'unsupported-expiry-private-sentinel' not in telemetry and not any(row['did'] in telemetry for row in rows)
                checks.append('unsupported_and_malformed_corpus_block_all_search_even_fixture_flags')
                repaired=copy.deepcopy(rows[2]);repaired['labels']=[]
                observe(admin,secret,[repaired],1)
                assert request(public,'/ready')[0]==503
                fixed=copy.deepcopy(rows[3]);fixed['labels']=[]
                observe(admin,secret,[fixed],2)
                assert request(public,'/ready')[0]==200
                current=request(public,'/index/status')[2]['expiry_coverage']
                assert current['current']['unsupported_expiry']==current['current']['actors_with_malformed_labels']==0
                assert current['imported']['actors_with_unsupported_expiry']==current['imported']['actors_with_malformed_labels']==1
                assert current['source_document_changes']==2 and current['input_actors_checked']==7
                replay=copy.deepcopy(fixed);replay['version']+=100
                observe(admin,secret,[replay],3)
                assert request(public,'/index/status')[2]['expiry_coverage']==current
                offender=copy.deepcopy(repaired);offender['labels']=rows[2]['labels']
                observe(admin,secret,[offender],4);assert request(public,'/ready')[0]==503
                deleted=copy.deepcopy(offender);deleted['deleted']=True
                observe(admin,secret,[deleted],5);assert request(public,'/ready')[0]==200
                committed=request(public,'/index/status')[2]['expiry_coverage']
                assert committed['current']['live_actors']==5 and committed['current']['unsupported_expiry']==0
                checks.append('source_mutations_repair_delete_and_version_only_counter_invariants')
            finally:stop(child)
            child,public,admin=start(data,log,secret)
            try:
                assert request(public,'/ready')[0]==200
                assert request(public,'/index/status')[2]['expiry_coverage']==committed
                checks.append('coverage_and_repaired_readiness_survive_restart')
            finally:stop(child)
        for kind in ['bad-footer','short-count','malformed-shape']:
            local=copy.deepcopy(rows[:3])
            if kind=='malformed-shape':local[-1]['labels']={'invalid':'array required'}
            path=root/(kind+'.jsonl');target=root/kind
            seed(path,local,expected=4 if kind=='short-count' else 3,bad_footer=kind=='bad-footer')
            result=imported(path,target);assert result.returncode!=0
            partial=status(target);evidence=partial['expiry_coverage']
            assert not partial['import_complete'] and not evidence['import_complete'] and evidence['verified_input_actors_sha256'] is None
            assert evidence['input_actors_checked']==partial['input_actors']
            assert evidence['expected_input_actors']==(4 if kind=='short-count' else 3)
        mismatched=root/'source-mismatch.jsonl';seed(mismatched,[copy.deepcopy(rows[0])],source_count=2)
        rejected=imported(mismatched,root/'source-mismatch');assert rejected.returncode!=0 and b'full-source manifest required' in rejected.stderr
        checks.append('failed_footer_source_count_and_label_shape_preserve_incomplete_checkpoints')
        # An existing precoverage index cannot acquire invented complete evidence.
        legacy=root/'legacy'
        clean=[copy.deepcopy(rows[0])];path=root/'legacy.jsonl';seed(path,clean);assert imported(path,legacy).returncode==0
        meta_path=legacy/'index/meta.json';meta=json.loads(meta_path.read_text());payload=json.loads(meta['payload']);payload.pop('expiry_coverage');payload['format']='tantivy-0.27-f32-did-v1';meta['payload']=json.dumps(payload);meta_path.write_text(json.dumps(meta))
        with (root/'legacy.log').open('wb') as log:
            child,public,admin=start(legacy,log,secret)
            try:
                result=request(public,'/index/status')[2];assert result['expiry_coverage_state']=='unknown' and not result['expiry_coverage_ready'];assert request(public,'/ready')[0]==503
                checks.append('legacy_coverage_unknown_never_inferred_zero')
            finally:stop(child)
    report={'passed':True,'scope':'synthetic-real-binary','full_live_source_audit_ran':False,'fixture':{'input_actors':7,'initial_live_actors':6,'initial_unsupported_expiry_actors':1,'initial_malformed_label_actors':1,'source_watermark':floor,'actor_stream_sha256':checksum,'classifier_sha256':coverage['classifier_sha256']},'checks':checks,'binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest(),'expiry_coverage_telemetry_contains_raw_labels_or_dids':False,'production_contact':False,'legacy_upgrade':'bounded committed-index audit may establish evidence; no automatic full-reimport claim'}
    if os.environ.get('TA_EXPIRY_COVERAGE_RECEIPT'):Path(os.environ['TA_EXPIRY_COVERAGE_RECEIPT']).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':run()
