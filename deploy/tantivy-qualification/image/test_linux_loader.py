"""Offline local-container loader and ELF dependency check; no engine workload."""
import argparse
import json
from pathlib import Path
import re
import subprocess

HERE=Path(__file__).resolve().parent
BASE='python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',default='typeahead-qualification:native-local');args=parser.parse_args()
    dependencies={}
    for name in ['eval-server','qualification-native','admit-prefix','typeahead-ingester']:
        output=subprocess.run(['xcrun','llvm-objdump','-p',str(HERE/'.build/linux/bin'/name)],text=True,capture_output=True,check=True,timeout=10).stdout
        dependencies[name]=re.findall(r'^\s*NEEDED\s+(\S+)',output,re.MULTILINE)
        if any('sqlite' in value.lower() for value in dependencies[name]):raise ValueError('unexpected external SQLite linkage')
    rust=HERE/'.build/context/bin/typeahead-tantivy'
    if rust.exists():
        output=subprocess.run(['xcrun','llvm-objdump','-p',str(rust)],text=True,capture_output=True,check=True,timeout=10).stdout
        dependencies['typeahead-tantivy']=re.findall(r'^\s*NEEDED\s+(\S+)',output,re.MULTILINE)
    code='''import json,subprocess,sqlite3,pathlib
rows=[]
for name in ['eval-server','qualification-native','admit-prefix']:
 r=subprocess.run(['/app/bin/'+name],capture_output=True,text=True,timeout=5)
 if r.returncode!=1 or 'Missing' not in r.stderr:raise RuntimeError(name+' loader/runtime sentinel failed')
 rows.append({'binary':name,'code':r.returncode,'expected_missing_configuration':True})
if pathlib.Path('/app/bin/typeahead-tantivy').exists():
 r=subprocess.run(['/app/bin/typeahead-tantivy'],capture_output=True,text=True,timeout=5)
 if r.returncode!=1 or not json.loads(r.stderr).get('error'):raise RuntimeError('Rust loader failure')
 rows.append({'binary':'typeahead-tantivy','code':r.returncode,'expected_missing_configuration':True})
r=subprocess.run(['/app/bin/typeahead-ingester'],env={'LD_TRACE_LOADED_OBJECTS':'1'},capture_output=True,text=True,timeout=5)
if r.returncode!=0 or 'not found' in r.stdout+r.stderr:raise RuntimeError('builder dynamic loader failure')
print(json.dumps({'tools':rows,'builder_loader_only':True,'python_sqlite_version':sqlite3.sqlite_version}))'''
    result=subprocess.run(['docker','run','--rm','--network','none','--platform','linux/amd64','--entrypoint','python3',
                           args.image,'-c',code],capture_output=True,text=True,timeout=60)
    if result.returncode:raise RuntimeError(result.stderr[:2000])
    report={'passed':True,'kind':'local-linux-container-loader','base':BASE,'platform':'linux/amd64',
            'elf_inspector':'Xcode llvm-objdump -p (readelf-equivalent ELF dynamic table)',
            'dynamic_dependencies':dependencies,'external_sqlite_required':False,**json.loads(result.stdout),
            'cloud_calls':0,'corpus_transferred':False,'network':'none','local_image':args.image}
    (HERE/'loader-results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))

if __name__=='__main__':main()
