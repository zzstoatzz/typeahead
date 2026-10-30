import argparse
from pathlib import Path
import subprocess
import os
import re

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
p=argparse.ArgumentParser();p.add_argument('--zig-target');p.add_argument('--rust-target');args=p.parse_args()
assert subprocess.check_output(['zig','version'],text=True).strip()=='0.17.0'
if args.zig_target and (not re.fullmatch(r'[A-Za-z0-9_.-]+',args.zig_target) or args.zig_target in ['.','..']):raise ValueError('invalid target')
directory=ROOT/'lib'/(args.zig_target or 'host')
directory.mkdir(parents=True,exist_ok=True)
common=['zig','build-lib','-O','ReleaseSafe','-lc','-static','-fPIC','-fcompiler-rt','--cache-dir',str(ROOT/'.zig-cache')]
if args.zig_target:common+=['-target',args.zig_target]
modules={'native_normalize':REPO/'services/src/index/normalize.zig','native_pds':REPO/'services/src/index/pds.zig','native_search':REPO/'services/src/search.zig'}
for name,source,deps in [('oracle_projection',ROOT/'projection.zig',['native_normalize','native_pds']),('typeahead_normalizer',REPO/'services/tantivy-normalizer/adapter.zig',list(modules))]:
    command=list(common)
    for dep in deps:command+=['--dep',dep]
    command+=['-Mroot='+str(source)]+['-M'+dep+'='+str(modules[dep]) for dep in deps]+['-femit-bin='+str(directory/('lib'+name+'.a'))]
    subprocess.run(command,check=True)
command=['cargo','zigbuild' if args.rust_target else 'build','--release','--locked','--offline','--jobs','2','--manifest-path',str(ROOT/'Cargo.toml')]
if args.rust_target:command+=['--target',args.rust_target]
subprocess.run(command,check=True,env=dict(os.environ,ORACLE_LIB_DIR=str(directory)))
