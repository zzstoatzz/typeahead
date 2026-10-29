"""Exercise the added probe outside timed stats using the existing tiny Linux image."""
import importlib.util
import json
from pathlib import Path
import subprocess

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('full_image_fixture',HERE/'test_full_image.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

EXTRA=r'''
 import full_functional as functional
 native_cursor_before=services['native'].phase()['acked']
 result=functional.run(controls['native'],controls['tantivy'],gen.Client('http://[::1]:8080',8080,fixture=True),nonce,
  sha(pathlib.Path('/app/tantivy-raw-oracle')),sha(pathlib.Path('/app/bin/typeahead-tantivy')),
  time.time()+60,'e'*64,fixture=True,max_seconds=45)
 assert result['passed'],result
 assert result['final_cursor']==9 and result['hydration_witnesses']==3 and result['query_count']<=27,result
 assert result['changed_witness_stages']==[4,6,8]
 assert not result['performance_qualified'] and not result['full_corpus_qualified']
 assert services['native'].phase()['acked']==native_cursor_before and services['native'].reset_count==0
 assert services['tantivy'].phase()['acked']==9 and services['tantivy'].state=='paused'
 # Prove preflight rejects an oracle set without a nontrivial contextual move,
 # without running another probe or mutating either engine.
 with (services['native'].root/'updates.json').open() as stream:feed=json.load(stream)
 broken={cursor:{key:dict(value) for key,value in answers.items()} for cursor,answers in truth.items()}
 for case in cases:
  if case.get('pds') or case.get('prefer_pds'):broken[8][case['id']]=broken[7][case['id']]
 try:functional.plan(cases,broken,feed)
 except functional.ProbeError as error:assert str(error)=='functional_missing_pds_change_witness'
 else:raise AssertionError('missing contextual change was silently accepted')
 print('FUNCTIONAL_IMAGE_INTEGRATION '+json.dumps(result))
'''

def main():
    before=" print('FULL_IMAGE_INTEGRATION '"
    assert before in fixture.PROGRAM
    program=fixture.PROGRAM.replace(before,EXTRA+'\n'+before,1)
    result=subprocess.run(['docker','run','--rm','-i','--network','none','--memory','1g','--cpus','2','--platform','linux/amd64',
        '-v',str(HERE.parents[2]/'scripts/snapshot_compaction')+':/reference:ro',
        '-v',str(HERE/'full_functional.py')+':/app/full_functional.py:ro',
        '--entrypoint','timeout','typeahead-qualification:full-local','240','python3','-'],
        input=program,capture_output=True,text=True,timeout=260)
    if result.returncode:raise RuntimeError(result.stdout[-3000:]+result.stderr[-4000:])
    line=next(line for line in result.stdout.splitlines() if line.startswith('FUNCTIONAL_IMAGE_INTEGRATION '))
    report=json.loads(line.removeprefix('FUNCTIONAL_IMAGE_INTEGRATION '))
    (HERE/'functional-image-integration.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

if __name__=='__main__':main()
