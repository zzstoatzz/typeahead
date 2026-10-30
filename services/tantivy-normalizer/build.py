import argparse,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent
SERVICES=ROOT.parent
parser=argparse.ArgumentParser()
parser.add_argument('--target')
parser.add_argument('--dynamic',action='store_true')
parser.add_argument('--test',action='store_true')
parser.add_argument('--output')
args=parser.parse_args()
kind='test' if args.test else 'build-lib'
command=['zig',kind,'-O','ReleaseSafe','-lc','--cache-dir',str(ROOT/'.zig-cache')]
if not args.test:command+=['-dynamic' if args.dynamic else '-static','-fPIC','-fcompiler-rt']
if args.target:command+=['-target',args.target]
command+=['--dep','native_normalize','--dep','native_pds','--dep','native_search',
 '-Mroot='+str(ROOT/'adapter.zig'),'-Mnative_normalize='+str(SERVICES/'src/index/normalize.zig'),
 '-Mnative_pds='+str(SERVICES/'src/index/pds.zig'),'-Mnative_search='+str(SERVICES/'src/search.zig')]
if not args.test:
 suffix='.so' if args.dynamic and args.target and 'linux' in args.target else '.dylib' if args.dynamic else '.a'
 command+=['-femit-bin='+str(Path(args.output).resolve() if args.output else ROOT/('libtypeahead_normalizer'+suffix))]
subprocess.run(command,check=True)
