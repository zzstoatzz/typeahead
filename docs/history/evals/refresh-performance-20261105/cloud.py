"""Temporary unscheduled cloud diagnostic, with no production secrets."""
import os
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from prefect import flow


@flow(name='typeahead-import-performance', retries=0, timeout_seconds=1800, log_prints=True)
def benchmark():
    root = Path.cwd()
    env = dict(os.environ, CARGO_BUILD_JOBS='4')
    subprocess.run(['sudo', 'env', 'NEEDRESTART_MODE=l', 'DEBIAN_FRONTEND=noninteractive',
        'apt-get', 'install', '-y', '-qq', '--no-install-recommends',
        'build-essential', 'pkg-config'], check=True, timeout=180)
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
        'https://sh.rustup.rs', '-o', '/var/tmp/typeahead-rustup.sh'], check=True, timeout=60)
    subprocess.run(['sh', '/var/tmp/typeahead-rustup.sh', '-y', '--profile', 'minimal',
        '--default-toolchain', '1.91.1'], check=True, timeout=300)
    env['PATH'] = str(Path.home() / '.cargo/bin') + ':' + env['PATH']
    zig = Path('/root/.local/opt/zig-x86_64-linux-0.17.0')
    env['PATH'] = str(zig) + ':' + env['PATH']
    adapter=root/'services/tantivy-normalizer/adapter.zig'
    original=adapter.read_bytes()
    assert original.count(b'const page = std.heap.c_allocator;')==1
    baseline=original.replace(b'const page = std.heap.c_allocator;',b'const page = std.heap.page_allocator;')
    binaries={}
    try:
        for name,source in [('page',baseline),('libc',original)]:
            adapter.write_bytes(source)
            subprocess.run(['python3', 'services/tantivy-normalizer/build.py', '--target', 'x86_64-linux-gnu'], env=env, check=True, timeout=120)
            subprocess.run(['cargo', 'build', '--release', '--locked', '--manifest-path',
                'services/tantivy/Cargo.toml'], env=env, check=True, timeout=600)
            binary=root/('typeahead-tantivy-'+name)
            shutil.copy2(root/'services/tantivy/target/release/typeahead-tantivy',binary)
            binaries[name]=binary
            print(json.dumps({'event':'import_benchmark_binary','allocator':name,
                'normalizer_source_sha256':hashlib.sha256(source).hexdigest(),
                'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}),flush=True)
    finally:
        adapter.write_bytes(original)
    # The supervisor owns the full benchmark process group, including reader children.
    import importlib.util
    spec = importlib.util.spec_from_file_location('import_performance', root / 'docs/evals/refresh-performance-20261105/benchmark.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(binaries['libc'],reference_binary=binaries['page'])
