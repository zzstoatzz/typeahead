"""Build a pinned native baseline and candidate with identical evaluation instrumentation."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if shutil.disk_usage(root).free < 20 * 1024**3: raise RuntimeError('local build disk reserve')
    if args.manifest.exists(): raise ValueError('preserve the previous build manifest')
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--', 'services/src'], cwd=root)
    if dirty: raise ValueError('commit native source before building a revision-identified candidate')
    args.output.mkdir(exist_ok=False)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    report = {'baseline_commit': subprocess.check_output(['git', 'rev-parse', args.baseline], cwd=root, text=True).strip(),
              'candidate_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
              'zig': subprocess.check_output(['zig', 'version'], text=True).strip(),
              'flags': ['-Doptimize=ReleaseSafe', '-Dtarget=x86_64-linux-gnu'], 'neutral_evaluator_files': {}}
    names = ['server.zig', 'eval_server.zig', 'eval_writer.zig']
    for name in names:
        report['neutral_evaluator_files'][name] = hashlib.sha256((root / 'services/src' / name).read_bytes()).hexdigest()
    flat = (root / 'services/src/index/flat_prefix.zig').read_text()
    capture = flat[flat.index('    pub fn actorFenceKeys('):flat.index('    pub fn close(self: Map)')]
    report['baseline_neutral_patches'] = {'sync_visibility': ['pub OverlayCtx', 'pub projectRow'],
                                         'actor_fence_capture_sha256': hashlib.sha256(capture.encode()).hexdigest()}
    def build(cwd):
        child = subprocess.Popen(['zig', 'build', 'eval-server', *report['flags'], '--summary', 'all'], cwd=cwd, start_new_session=True)
        try:
            if child.wait(timeout=300): raise RuntimeError('evaluation build failed')
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: os.killpg(child.pid, signal.SIGKILL); child.wait()
    with tempfile.TemporaryDirectory(prefix='typeahead-pair-build-') as temporary:
        work = Path(temporary)
        data = subprocess.check_output(['git', 'archive', report['baseline_commit'], 'services/src', 'services/build.zig', 'services/build.zig.zon'], cwd=root)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive: archive.extractall(work, filter='data')
        (work / 'services/zig-pkg').symlink_to(root / 'services/zig-pkg', target_is_directory=True)
        for name in names: shutil.copy2(root / 'services/src' / name, work / 'services/src' / name)
        sync = work / 'services/src/db/sync.zig'
        sync.write_text(sync.read_text().replace('const OverlayCtx = struct', 'pub const OverlayCtx = struct').replace('fn projectRow(', 'pub fn projectRow('))
        target = work / 'services/src/index/flat_prefix.zig'
        original = target.read_text()
        add_capture = '    pub fn actorFenceKeys(' not in original
        report['baseline_neutral_patches']['actor_fence_capture_applied'] = add_capture
        if add_capture:
            target.write_text(original.replace('    pub fn close(self: Map)', capture + '    pub fn close(self: Map)', 1))
        build(work / 'services')
        shutil.copy2(work / 'services/zig-out/bin/eval-server', args.output / 'baseline')
    build(root / 'services')
    shutil.copy2(root / 'services/zig-out/bin/eval-server', args.output / 'candidate')
    report['binaries'] = {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in [args.output / 'baseline', args.output / 'candidate']}
    args.manifest.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__': main()
