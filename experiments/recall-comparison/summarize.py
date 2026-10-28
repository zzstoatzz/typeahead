#!/usr/bin/env python3
"""Validate retained evidence and emit compact comparison tables."""
import argparse
import hashlib
import gzip
import json
from pathlib import Path


def read_result(path):
    if path.exists(): return path,json.loads(path.read_text())
    compressed=Path(str(path)+'.gz')
    return compressed,json.loads(gzip.decompress(compressed.read_bytes()))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('directory',type=Path);args=ap.parse_args();root=args.directory
    _,native=read_result(root/'native-comparison-2/results.json')
    _,static=read_result(root/'static-plan-controls-3/results.json')
    for run in (native,static):
        assert run['complete'] and not run['production_access'] and run['scratch_removed']
    assert native['children_terminal']
    result={'native':[],'static_controls':[],'retired_runs':[],'files':{}}
    for arm in native['runs']:
        phases={p['name']:p for p in arm['phases']}
        assert all(not p['errors'] for p in phases.values())
        if arm['variant']=='complete':assert all(c['correct'] for p in phases.values() for c in p['cases'])
        before=arm['mixed_writer_before'];after=arm['mixed_writer']
        assert after['failures']==0 and after['batches']>before['batches']
        mixed=phases['mixed-native-projection']
        result['native'].append({'actors':arm['actors'],'variant':arm['variant'],'snapshot_bytes':arm['snapshot']['bytes'],'memberships':arm['snapshot']['memberships'],'keys':arm['snapshot']['keys'],'warm_correct':sum(c['correct'] for c in phases['warm']['cases']),'cases_per_phase':len(phases['warm']['cases']),'post_update_correct':sum(c['correct'] for c in phases['hidden-and-deleted']['cases']),'warm_engine_us':phases['warm']['metrics']['engine_us'],'warm_http_us':phases['warm']['metrics']['http_us'],'mixed_engine_us':mixed['metrics']['engine_us'],'mixed_http_us':mixed['metrics']['http_us'],'mixed_rss_bytes_after':mixed['rss_bytes_after'],'mixed_attempts':mixed['attempts'],'mixed_seconds':mixed['wall_seconds'],'mixed_committed_batches':after['batches']-before['batches'],'mixed_committed_rows':after['rows']-before['rows'],'mixed_sqlite_changes':after['changes']-before['changes'],'mixed_lag_ms':after['max_lag_ms'],'overlay_bytes':arm['overlay_bytes']})
    for arm in static['runs']:
        assert arm['fts']['correct']==19 and arm['rank_id_control']['correct']==19
        broadfts=next(c for c in arm['fts']['cases'] if c['q']=='common member')
        broadrank=next(c for c in arm['rank_id_control']['cases'] if c['q']=='common member')
        result['static_controls'].append({'actors':arm['actors'],'fts_cases_correct':arm['fts']['correct'],'rank_id_cases_correct':arm['rank_id_control']['correct'],'fts_broad_predicate_rows':broadfts['predicate_rows'],'fts_broad_vm_steps_approx':broadfts['vm_steps_approx'],'rank_broad_id_iterations_and_seeks':broadrank['id_iterations_and_seeks'],'rank_id_wire_bytes_u32':arm['rank_id_control']['wire_bytes_u32'],'rank_memberships':arm['rank_id_control']['memberships'],'fts_sqlite_file_bytes':arm['fts']['sqlite_file_bytes'],'fts_plan':arm['fts']['plan']})
    for name in ['native-comparison-1','native-comparison-2','static-plan-controls-1','static-plan-controls-2','static-plan-controls-3']:
        path,r=read_result(root/name/'results.json')
        assert r['scratch_removed'] and not Path(r['scratch']).exists()
        if 'children_terminal' in r:assert r['children_terminal']
        result['retired_runs'].append({'name':name,'scratch':r['scratch'],'scratch_removed':True,'error':r.get('error'),'complete':r.get('complete',False),'children_terminal':r.get('children_terminal'),'scratch_bytes_before_cleanup':r['scratch_bytes_before_cleanup']})
        result['files'][str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
    (root/'comparison-summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
