"""Temporary unscheduled cloud diagnostic, with no production secrets."""
import os
from pathlib import Path
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
    subprocess.run(['python3', 'services/tantivy-normalizer/build.py', '--target', 'x86_64-linux-gnu'], env=env, check=True, timeout=120)
    subprocess.run(['cargo', 'build', '--release', '--locked', '--manifest-path',
        'services/tantivy/Cargo.toml'], env=env, check=True, timeout=600)
    # The supervisor owns the full benchmark process group, including reader children.
    import importlib.util
    spec = importlib.util.spec_from_file_location('import_performance', root / 'docs/evals/refresh-performance-20261105/benchmark.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(root / 'services/tantivy/target/release/typeahead-tantivy')
