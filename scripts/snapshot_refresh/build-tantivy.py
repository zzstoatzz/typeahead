"""Build the managed slot image from an already frozen Linux Rust binary; never push."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',required=True,type=Path);parser.add_argument('--binary-sha256',required=True);parser.add_argument('--image',required=True)
    args=parser.parse_args()
    with args.binary.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=args.binary_sha256:raise ValueError('Frozen Linux reader differs')
    with args.binary.open('rb') as stream:
        header=stream.read(20)
        if header[:6]!=b'\x7fELF\x02\x01' or header[18:20]!=b'\x3e\x00':raise ValueError('Linux x86_64 ELF reader required')
    with tempfile.TemporaryDirectory(prefix='tantivy-slot-image-') as directory:
        context=Path(directory);shutil.copy2(args.binary,context/'typeahead-tantivy')
        for name in ['tantivy-slot.py','tantivy_install.py','install.py']:shutil.copy2(HERE/name,context/name)
        (context/'input').mkdir()
        for name in ['sync.py','current_state.py','budget.py']:shutil.copy2(ROOT/'services/tantivy-input'/name,context/'input'/name)
        subprocess.run(['docker','build','--platform','linux/amd64','-f',str(HERE/'Dockerfile.tantivy'),'-t',args.image,str(context)],check=True)


if __name__=='__main__':main()
