"""Build local qualification context/image; never compiles Rust or pushes an image."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
BASE='python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f'

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def linux_elf(path):
    with path.open('rb') as stream:header=stream.read(20)
    if header[:6]!=b'\x7fELF\x02\x01' or int.from_bytes(header[18:20],'little')!=62:raise ValueError('expected Linux x86_64 ELF binary')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rust-binary',type=Path)
    parser.add_argument('--rust-sha256')
    parser.add_argument('--skip-native-build',action='store_true')
    parser.add_argument('--docker',action='store_true')
    parser.add_argument('--tag',default='typeahead-qualification:local')
    args=parser.parse_args()
    build=HERE/'.build';build.mkdir(exist_ok=True)
    prefix=build/'linux';started=time.monotonic()
    def run(command,label,timeout=360):
        result=subprocess.run(command,cwd=REPO/'services',text=True,capture_output=True,timeout=timeout)
        (build/(label+'.log')).write_text(result.stdout+result.stderr)
        if result.returncode:raise RuntimeError(label+': '+result.stderr[:2500])
        return result.stdout
    source_paths=sorted(p for p in (REPO/'services/src').rglob('*') if p.is_file())
    source_paths.extend([REPO/'services/build.zig',REPO/'services/build.zig.zon',REPO/'experiments/tantivy-qualification-native/main.zig'])
    sources={str(p.relative_to(REPO)):sha(p) for p in source_paths}
    if not args.skip_native_build:
        for targets,label in [(['qualification-native','eval-server','admit-prefix'],'native-tools'),([], 'native-builder')]:
            run(['zig','build',*targets,'-Dtarget=x86_64-linux-gnu','-Doptimize=ReleaseSafe','--prefix',str(prefix)],label)
        receipt={'target':'x86_64-linux-gnu','optimize':'ReleaseSafe','source_sha256':sources,
                 'binaries':{p.name:{'sha256':sha(p),'bytes':p.stat().st_size} for p in (prefix/'bin').iterdir()},
                 'wall_s':time.monotonic()-started,'cloud_calls':0}
        (HERE/'native-build.json').write_text(json.dumps(receipt,indent=2)+'\n')
    else:
        receipt=json.loads((HERE/'native-build.json').read_text())
        if receipt.get('source_sha256')!=sources:raise ValueError('native source changed since receipt')
    context=build/'context'
    if context.exists():shutil.rmtree(context)
    (context/'bin').mkdir(parents=True)
    for name in ['eval-server','qualification-native','admit-prefix','typeahead-ingester']:
        source=prefix/'bin'/name
        linux_elf(source)
        if sha(source)!=receipt['binaries'][name]['sha256']:raise ValueError('native binary hash differs')
        shutil.copy2(source,context/'bin'/name)
    (context/'input').mkdir()
    for name in ['export.py','fixture.py']:shutil.copyfile(REPO/'services/tantivy-input'/name,context/'input'/name)
    shutil.copyfile(REPO/'deploy/tantivy-qualification/reaper.py',context/'input/reaper.py')
    engines=['native']
    if args.rust_binary:
        linux_elf(args.rust_binary)
        if not args.rust_sha256 or sha(args.rust_binary)!=args.rust_sha256:raise ValueError('frozen Rust binary hash required')
        shutil.copy2(args.rust_binary,context/'bin/typeahead-tantivy')
        if sha(context/'bin/typeahead-tantivy')!=args.rust_sha256:raise ValueError('Rust binary changed during copy')
        engines.append('tantivy')
    for name in ['Dockerfile','entrypoint.py','pilot_bootstrap.py','generator.py']:shutil.copyfile(HERE/name,context/name)
    metadata={'kind':'private-matched-qualification-image','base':BASE,'engines':engines,'native':receipt,
              'rust_binary_sha256':args.rust_sha256,'entrypoint_sha256':sha(HERE/'entrypoint.py'),
              'pilot_bootstrap_sha256':sha(HERE/'pilot_bootstrap.py'),'generator_sha256':sha(HERE/'generator.py'),
              'input_sources_sha256':{p.name:sha(p) for p in (context/'input').iterdir()},
              'fixture_max_actors':100,'final_guest':{'cpu_kind':'shared','cpus':2,'memory_mb':1024},
              'final_volume_gib':20,'temporary_preparation_volume_gib':40,
              'corpus_transferred':False,'image_pushed':False}
    (context/'build-metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    if args.docker:
        run(['docker','build','--network','none','--platform','linux/amd64','--tag',args.tag,str(context)],'docker',600)
        metadata['local_image_tag']=args.tag
    (HERE/'image-build.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print(json.dumps({'engines':engines,'context':str(context),'local_image_tag':metadata.get('local_image_tag')}))

if __name__=='__main__':main()
