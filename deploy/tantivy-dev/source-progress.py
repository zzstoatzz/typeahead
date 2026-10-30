#!/usr/bin/env python3
"""Bounded replay-log corroboration; importing this module performs no remote work."""
import argparse
import hashlib
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent
POLICY='turso-current-state-cutoff-v1'
MAX_TAIL=2*1024*1024
MARGIN=2.0
COUNTERS=('applied','replayed','stale','records','submitted_records','superseded_tombstones','ack_recoveries')

def finite(value):
    return type(value) in (int,float) and math.isfinite(value)

def counter(value):
    return type(value) is int and 0<=value<2**63

def windows_from_load(load):
    start,end=load.get('started_at'),load.get('finished_at')
    if not finite(start) or not finite(end) or start>=end:raise ValueError('invalid completed load window')
    phases=[p for p in load.get('phases',[]) if p.get('phase')=='live-replay']
    if len(phases)!=1:raise ValueError('exactly one completed live-replay phase is required')
    phase=phases[0];a,b=phase.get('started_at'),phase.get('finished_at')
    if not finite(a) or not finite(b) or not start<=a<b<=end:raise ValueError('invalid live-replay phase window')
    last=max(load['phases'],key=lambda p:p.get('finished_at',0))
    entire={'started_at':start,'finished_at':end,'before_through':load.get('initial',{}).get('watermark'),'after_through':last.get('after',{}).get('watermark')}
    live={'started_at':a,'finished_at':b,'before_through':phase.get('before',{}).get('watermark'),'after_through':phase.get('after',{}).get('watermark')}
    return {'entire_load':entire,'live_replay':live}

def capture(directory='/data'):
    root=Path(directory);sampler_path=root/'resource-sampler.json';log_path=root/'replay.log'
    with sampler_path.open('rb') as file:raw_sampler=file.read(65537)
    if len(raw_sampler)>65536:raise ValueError('resource baseline exceeds64KiB')
    sampler=json.loads(raw_sampler)
    if sampler.get('owner')!='tantivy-dev-20261028' or not counter(sampler.get('replay_log_start_bytes')) or not finite(sampler.get('created_at')):raise ValueError('invalid replay log baseline')
    offset=sampler['replay_log_start_bytes'];issues=[]
    with log_path.open('rb') as file:
        before=os.fstat(file.fileno());size=before.st_size
        if size<offset:
            issues.append('log_truncated_below_baseline');body=b'';start=size;baseline_boundary=False;start_boundary=False
        else:
            baseline_boundary=offset==0
            if offset:
                file.seek(offset-1);baseline_boundary=file.read(1)==b'\n'
            if not baseline_boundary:issues.append('baseline_is_partial_line')
            start=max(offset,size-MAX_TAIL)
            if start>offset:issues.append('new_log_exceeds_two_mib_tail')
            start_boundary=start==0
            if start:
                file.seek(start-1);start_boundary=file.read(1)==b'\n'
            file.seek(start);body=file.read(size-start)
            if len(body)!=size-start:issues.append('log_changed_or_truncated_during_read')
        after_fd=os.fstat(file.fileno())
    after_path=log_path.stat()
    if after_fd.st_size<size:issues.append('log_shrank_during_read')
    if (before.st_dev,before.st_ino)!=(after_path.st_dev,after_path.st_ino):issues.append('log_rotated_during_read')
    baseline_ino=sampler.get('replay_log_inode');baseline_dev=sampler.get('replay_log_device')
    identity_recorded=counter(baseline_ino) and counter(baseline_dev)
    if identity_recorded and (baseline_dev,baseline_ino)!=(before.st_dev,before.st_ino):issues.append('log_identity_differs_from_baseline')
    previous=root/'replay.log.previous';previous_time=None
    if previous.exists():
        previous_stat=previous.stat();previous_time=max(previous_stat.st_mtime,previous_stat.st_ctime)
        if previous_time>=sampler['created_at']:issues.append('writer_rotation_artifact_since_baseline')
    return {'body':body,'start_boundary':start_boundary,'baseline_created_at':sampler['created_at'],'baseline_offset':offset,'read_start_offset':start,'snapshot_size':size,'bytes_read':len(body),'tail_sha256':hashlib.sha256(body).hexdigest(),'baseline_inode_recorded':identity_recorded,'current_log_inode':before.st_ino,'current_log_device':before.st_dev,'appended_while_reading':after_fd.st_size>size,'previous_rotation_epoch':previous_time,'captured_at':time.time(),'issues':issues}

def parse_cycles(snapshot):
    body=snapshot['body'];issues=list(snapshot.get('issues',[]));initial_fragment=False;trailing_fragment=False
    if body and not snapshot['start_boundary']:
        initial_fragment=True;position=body.find(b'\n');body=b'' if position<0 else body[position+1:];issues.append('tail_begins_inside_partial_line')
    if body and not body.endswith(b'\n'):
        trailing_fragment=True;position=body.rfind(b'\n');body=b'' if position<0 else body[:position+1];issues.append('tail_ends_with_partial_line')
    errors=non_json=ignored=invalid=0;complete=[];paused=set();unexpected=0;total_lines=0
    for raw in body.splitlines():
        if not raw.strip():continue
        total_lines+=1
        try:row=json.loads(raw)
        except (ValueError,UnicodeError):non_json+=1;errors+=1;continue
        if not isinstance(row,dict):non_json+=1;errors+=1;continue
        phase=row.get('phase')
        if phase in ('replay-error','failed') or row.get('error') is not None or row.get('passed') is False:
            errors+=1;continue
        if phase not in ('caught-up','bounded-pause'):
            ignored+=1;continue
        if row.get('source_policy')!=POLICY:
            unexpected+=1;continue
        cutoff=row.get('source_cutoff');wall=row.get('wall_seconds')
        if not counter(cutoff) or not finite(wall) or not 0<=wall<=3600:
            invalid+=1;continue
        values={key:row.get(key,0 if key=='ack_recoveries' else None) for key in COUNTERS}
        if not all(counter(value) for value in values.values()) or values['records']!=values['submitted_records']+values['superseded_tombstones'] or values['applied']+values['replayed']+values['stale']>values['submitted_records']:
            invalid+=1;continue
        if phase=='bounded-pause':paused.add(cutoff);continue
        if row.get('caught_up') is not True or row.get('through')!=cutoff:
            invalid+=1;continue
        anchor=cutoff+2
        complete.append({'source_cutoff':cutoff,'through':cutoff,'wall_seconds':wall,'interval_lower':anchor-wall-MARGIN,'interval_upper':anchor+wall+MARGIN,'source_policy':POLICY,**values})
    if non_json:issues.append('unparsed_complete_log_lines')
    if unexpected:issues.append('unexpected_source_policy')
    if invalid:issues.append('invalid_progress_rows')
    counts={}
    for cycle in complete:counts[cycle['source_cutoff']]=counts.get(cycle['source_cutoff'],0)+1
    duplicates={key for key,n in counts.items() if n>1}
    if duplicates:issues.append('duplicate_caught_up_cutoff')
    excluded_resumes=sum(c['source_cutoff'] in paused for c in complete)
    eligible=[c for c in complete if c['source_cutoff'] not in paused|duplicates and c['source_cutoff']+2>=snapshot['baseline_created_at']-MARGIN]
    if any(b['through']<a['through'] for a,b in zip(complete,complete[1:])):issues.append('nonmonotonic_caught_up_through')
    return {'cycles':eligible,'complete_cycle_rows':len(complete),'paused_cutoffs':len(paused),'resumed_cycles_excluded':excluded_resumes,'errors_since_baseline':errors,'non_json_complete_lines':non_json,'invalid_progress_rows':invalid,'unexpected_policy_rows':unexpected,'ignored_complete_lines':ignored,'complete_lines':total_lines,'initial_partial_line':initial_fragment,'trailing_partial_line':trailing_fragment,'issues':sorted(set(issues))}

def summarize_window(window,cycles):
    included=[]
    for cycle in cycles:
        if cycle['interval_lower']<window['started_at'] or cycle['interval_upper']>window['finished_at']:continue
        before,after=window.get('before_through'),window.get('after_through')
        if counter(before) and cycle['through']<=before:continue
        if counter(after) and cycle['through']>after:continue
        included.append(cycle)
    totals={key:sum(c[key] for c in included) for key in COUNTERS}
    through=[c['through'] for c in included]
    return {'started_at':window['started_at'],'finished_at':window['finished_at'],'duration_seconds':window['finished_at']-window['started_at'],'fully_contained_cycles':len(included),'totals':totals,'through_range':None if not through else {'minimum':min(through),'maximum':max(through)},'watermark_witnesses_used':counter(window.get('before_through')) and counter(window.get('after_through')),'cycle_receipts':included[:8],'cycle_receipts_omitted':max(0,len(included)-8)}

def analyze(snapshot,windows):
    parsed=parse_cycles(snapshot);issues=list(parsed['issues'])
    if snapshot['captured_at']<windows['entire_load']['finished_at']-MARGIN:issues.append('capture_precedes_finished_load_window')
    if snapshot['baseline_created_at']>windows['entire_load']['started_at']+MARGIN:issues.append('baseline_started_after_load_window')
    summaries={name:summarize_window(window,parsed['cycles']) for name,window in windows.items()}
    live=summaries['live_replay']
    if not 295<=live['duration_seconds']<=305:issues.append('live_window_is_not_five_minutes')
    observed=live['fully_contained_cycles']>=1 and live['totals']['applied']>=1
    if not observed:issues.append('no_fully_contained_live_cycle_with_actual_applied_records')
    if parsed['errors_since_baseline']:issues.append('replay_errors_since_baseline')
    issues=sorted(set(issues));coverage_valid=not any(reason in issues for reason in ['log_truncated_below_baseline','baseline_is_partial_line','new_log_exceeds_two_mib_tail','log_changed_or_truncated_during_read','log_shrank_during_read','log_rotated_during_read','log_identity_differs_from_baseline','writer_rotation_artifact_since_baseline','tail_begins_inside_partial_line','tail_ends_with_partial_line','unparsed_complete_log_lines'])
    return {'owner':'tantivy-dev-20261028','kind':'bounded-source-progress-corroboration','source_policy':POLICY,'progress_observed':observed,'progress_verified':not issues,'coverage_valid':coverage_valid,'issues':issues,'windows':summaries,'log_capture':{key:value for key,value in snapshot.items() if key not in ('body','start_boundary','issues')},'parsing':{key:value for key,value in parsed.items() if key not in ('cycles','issues')},'cumulative_counters_not_summed':['source_bytes','source_requests'],'containment_method':'anchor=source_cutoff+2; interval=[anchor-wall_seconds-2,anchor+wall_seconds+2]; entire interval must fit; available before/after committed-watermark witnesses also constrain membership','limitations':['Only fully contained completed cycles are counted; totals are conservative lower bounds, not all source work.','Source clock and load-generator epochs use the requested two-second margin.','Baseline may lack inode identity; known writer rotation is checked via size, line boundary, open/path identity, and replay.log.previous modification time.','Resumed bounded-pause cycles and duplicate completion cutoffs are excluded.','Missing acknowledgements may undercount applied/replayed/stale; ack_recoveries is separate.','Applied is a committed logical actor-record count, not physical WAL or membership-write bytes.']}

def remote_source(windows,machine):
    definitions='\n\n'.join(inspect.getsource(fn) for fn in [finite,counter,capture,parse_cycles,summarize_window,analyze])
    return ('import hashlib,json,math,os,time\nfrom pathlib import Path\n'
            +'assert os.environ.get("FLY_APP_NAME")=="typeahead-tantivy-dev"\n'
            +'assert os.environ.get("FLY_MACHINE_ID")=='+repr(machine)+'\n'
            +'POLICY='+repr(POLICY)+'\nMAX_TAIL='+str(MAX_TAIL)+'\nMARGIN='+repr(MARGIN)+'\nCOUNTERS='+repr(COUNTERS)+'\n'
            +definitions+'\n'
            +'try:\n result=analyze(capture(),'+repr(windows)+')\n'
            +'except (OSError,ValueError,KeyError,TypeError):\n result={"owner":"tantivy-dev-20261028","progress_verified":False,"issues":["bounded_log_collection_failed"]}\n'
            +'print("HELPER_OK "+json.dumps(result,separators=(",",":")))\n')

def self_test():
    import tempfile
    import unittest
    def row(cutoff=1300,applied=2,wall=1,**extra):
        return {'phase':'caught-up','caught_up':True,'source_cutoff':cutoff,'through':cutoff,'wall_seconds':wall,'source_policy':POLICY,'records':5,'submitted_records':4,'superseded_tombstones':1,'applied':applied,'replayed':4-applied,'stale':0,'ack_recoveries':0,'source_bytes':999999999,'source_requests':9999,**extra}
    def snapshot(rows):
        return {'body':b''.join((json.dumps(r)+'\n').encode() for r in rows),'start_boundary':True,'baseline_created_at':999,'baseline_offset':0,'captured_at':1601,'issues':[]}
    windows={'entire_load':{'started_at':1000,'finished_at':1600},'live_replay':{'started_at':1200,'finished_at':1500}}
    class Tests(unittest.TestCase):
        def test_actual_applied_and_no_cumulative_sums(self):
            out=analyze(snapshot([row(),row(1400,1)]),windows);self.assertTrue(out['progress_verified']);self.assertEqual(out['windows']['live_replay']['totals']['applied'],3);self.assertNotIn('source_bytes',out['windows']['live_replay']['totals'])
        def test_watermark_only_and_edge_cycles_do_not_prove_progress(self):
            for rows in [[row(applied=0)],[row(cutoff=1198)],[row(cutoff=1498)]]:self.assertFalse(analyze(snapshot(rows),windows)['progress_verified'])
        def test_partial_rotation_truncation_and_errors_cannot_pass(self):
            for reason in ['log_truncated_below_baseline','writer_rotation_artifact_since_baseline']:
                value=snapshot([row()]);value['issues']=[reason];self.assertFalse(analyze(value,windows)['progress_verified'])
            value=snapshot([row()]);value['body']+=b'{"phase":';self.assertFalse(analyze(value,windows)['progress_verified'])
            self.assertFalse(analyze(snapshot([row(),{'phase':'replay-error','reason':'not copied'}]),windows)['progress_verified'])
        def test_policy_resume_duplicate_and_witness_guards(self):
            self.assertFalse(analyze(snapshot([row(source_policy='other')]),windows)['progress_verified'])
            self.assertFalse(analyze(snapshot([row(phase='bounded-pause',caught_up=False),row()]),windows)['progress_verified'])
            self.assertFalse(analyze(snapshot([row(),row()]),windows)['progress_verified'])
            narrow={**windows,'live_replay':{**windows['live_replay'],'after_through':1299}};self.assertFalse(analyze(snapshot([row()]),narrow)['progress_verified'])
        def test_real_offset_excludes_historical_error_prefix(self):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);prefix=b'{"phase":"replay-error"}\n';payload=(json.dumps(row())+'\n').encode();(root/'replay.log').write_bytes(prefix+payload);(root/'resource-sampler.json').write_text(json.dumps({'owner':'tantivy-dev-20261028','created_at':999,'replay_log_start_bytes':len(prefix)}))
                captured=capture(root);out=analyze(captured,windows);self.assertTrue(out['progress_verified']);self.assertEqual(out['parsing']['errors_since_baseline'],0)
                (root/'replay.log').write_bytes(b'x');self.assertFalse(analyze(capture(root),windows)['progress_verified'])
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Tests);result=unittest.TextTestRunner(verbosity=1).run(suite)
    return result.wasSuccessful()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--self-test',action='store_true');args=parser.parse_args()
    if args.self_test:return 0 if self_test() else 1
    load=json.loads((ROOT/'load-results.json').read_text());windows=windows_from_load(load)
    spec=importlib.util.spec_from_file_location('stage_helpers',ROOT/'stage-helpers.py');helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
    output=helpers.remote(remote_source(windows,helpers.MACHINE))
    markers=[line for line in output.splitlines() if line.startswith('HELPER_OK ')]
    if len(markers)!=1:raise RuntimeError('source progress did not return one bounded summary')
    result=json.loads(markers[0].split(' ',1)[1]);result['load_results_sha256']=hashlib.sha256((ROOT/'load-results.json').read_bytes()).hexdigest()
    target=ROOT/'source-progress.json';temporary=target.with_suffix('.json.tmp');temporary.write_text(json.dumps(result,indent=2)+'\n');os.replace(temporary,target)
    summary={'progress_verified':result.get('progress_verified',False),'issues':result.get('issues',[])}
    if 'windows' in result:
        summary.update(live_cycles=result['windows']['live_replay']['fully_contained_cycles'],live_applied=result['windows']['live_replay']['totals']['applied'],entire_load_applied=result['windows']['entire_load']['totals']['applied'],errors_since_baseline=result['parsing']['errors_since_baseline'])
    print(json.dumps(summary,sort_keys=True));return 0 if summary['progress_verified'] else 2
if __name__=='__main__':sys.exit(main())
