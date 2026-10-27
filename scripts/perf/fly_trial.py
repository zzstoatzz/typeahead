"""Offline qualification entry point for a new private Fly Machine; never serves traffic."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import sys
import time

from cloud_trial import Trial, GIB
import install_snapshot


class FlyTrial(Trial):
    def __init__(self, manifest):
        self.scratch = Path('/data/refresh')
        self.payload = self.scratch / 'index/generations' / manifest['active_build']
        self.evidence = Path('/data/evidence')
        self.evidence.mkdir(exist_ok=True)
        self.args = argparse.Namespace(bin_dir='/app/perf', cases='/app/perf/cases-varied.json', pre_admitted=True)
        self.children = []
        self.started = time.monotonic()
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        memory_kib = int(memory['MemTotal'].split()[0])
        if os.cpu_count() != 2 or not 850 * 1024 <= memory_kib <= 1024 * 1024:
            raise RuntimeError('private VM resource shape does not match qualification')
        if len(Path('/proc/swaps').read_text().splitlines()) != 1: raise RuntimeError('qualification requires no swap')
        self.report = {'kind': 'private-fly-full-snapshot', 'goal_qualified': False, 'runs': {},
                       'limitations': ['captured 5000-actor overlay is smaller than aged live overlay',
                                       'no Turso or live freshness qualification'],
                       'capture_method': 'first 5000 actor-namespace page fences',
                       'machine_id': os.environ.get('FLY_MACHINE_ID'), 'run_id': os.environ['PERF_RUN_ID'],
                       'hardware': {'cpus': os.cpu_count(), 'memory_kib': memory_kib, 'machine': os.uname().machine}}

    def guard(self):
        if time.monotonic() - self.started > 3000:
            raise TimeoutError('measurement wall budget exceeded')
        if shutil.disk_usage(self.scratch).free < 2 * GIB:
            raise RuntimeError('private volume reserve breached')
        for path, limit in [(self.payload, 16 * GIB), (self.evidence, 16 * 1024 * 1024)]:
            size = 0
            for p in path.rglob('*'):
                try:
                    if p.is_file(): size += p.stat().st_size
                except FileNotFoundError: pass
            if size > limit: raise RuntimeError('private trial storage budget breached')

    def capture(self):
        self.save('capturing-private-input')
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PORT': '18081',
               'TYPEAHEAD_EVAL_SNAPSHOT': str(self.payload / 'index.db'),
               'LOCAL_DB_PATH': str(self.payload / 'capture.db'),
               'TYPEAHEAD_EVAL_NONCE': self.report['run_id'],
               'TYPEAHEAD_EVAL_PRIVATE_ROOT': str(self.payload),
               'TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT': '1',
               'TYPEAHEAD_EVAL_CAPTURE_FEED': str(self.payload / 'feed.json')}
        with (self.evidence / 'capture.log').open('xb') as output:
            child = subprocess.Popen(['/app/perf/baseline'], env=env, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(child)
        deadline = time.monotonic() + 180
        while child.poll() is None:
            self.guard()
            if time.monotonic() > deadline:
                self.stop(child)
                raise TimeoutError('capturing private input')
            time.sleep(.25)
        if child.returncode: raise RuntimeError('private capture failed')
        if len(json.loads((self.payload / 'feed.json').read_text())) != 5000:
            raise RuntimeError('incomplete private capture')

    def retire(self):
        expected = self.scratch / 'index/generations' / os.environ['REFRESH_BUILD_ID']
        if self.payload != expected or self.payload.is_symlink():
            raise RuntimeError('unsafe payload cleanup')
        before = shutil.disk_usage(self.scratch).free
        allocated = sum(p.stat().st_blocks * 512 for p in self.payload.rglob('*') if p.is_file())
        shutil.rmtree(self.payload)
        after = shutil.disk_usage(self.scratch).free
        (self.evidence / 'cleanup.json').write_text(json.dumps({'payload_removed': not self.payload.exists(),
            'allocated_before': allocated, 'free_before': before, 'free_after': after}, indent=2) + '\n')


def main():
    started = time.monotonic()
    def interrupted(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    trial = None
    preparing = None
    evidence = Path('/data/evidence')
    evidence.mkdir(exist_ok=True)
    try:
        (evidence / 'status.json').write_text(json.dumps({'phase': 'installing', 'run_id': os.environ['PERF_RUN_ID']}))
        with (evidence / 'install.log').open('xb') as output:
            preparing = subprocess.Popen([sys.executable, '-c', 'import install_snapshot; install_snapshot.prepare()'],
                cwd='/app/perf', env=dict(os.environ, PATH='/app/perf:/usr/local/bin:/usr/bin:/bin'),
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        while preparing.poll() is None:
            if time.monotonic() - started > 2400: raise TimeoutError('private installation deadline')
            if shutil.disk_usage('/data').free < 2 * GIB: raise RuntimeError('private installation disk reserve')
            time.sleep(.5)
        if preparing.returncode: raise RuntimeError('private installation failed')
        manifest = json.loads((install_snapshot.ROOT / 'index/generations' / os.environ['REFRESH_BUILD_ID'] / 'manifest.json').read_text())
        trial = FlyTrial(manifest)
        trial.capture()
        trial.measure(manifest)
    except BaseException as exc:
        (evidence / 'failure.json').write_text(json.dumps({'kind': type(exc).__name__,
                                                         'message': str(exc)[:1024]}) + '\n')
    finally:
        if preparing and preparing.poll() is None:
            os.killpg(preparing.pid, signal.SIGTERM)
            try: preparing.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(preparing.pid, signal.SIGKILL); preparing.wait(timeout=5)
        try:
            if trial:
                for child in reversed(trial.children): trial.stop(child)
                trial.retire()
            elif install_snapshot.ROOT.exists():
                generations = install_snapshot.ROOT / 'index/generations'
                before = shutil.disk_usage('/data').free
                if generations.exists(): shutil.rmtree(generations)
                (evidence / 'cleanup.json').write_text(json.dumps({'installation_failed': True,
                    'payload_removed': not generations.exists(), 'free_before': before,
                    'free_after': shutil.disk_usage('/data').free}) + '\n')
        except BaseException as exc:
            (evidence / 'cleanup-failure.json').write_text(json.dumps({'kind': type(exc).__name__,
                'message': str(exc)[:1024], 'requires_volume_retirement': True}) + '\n')
        with tarfile.open(evidence / 'results.tgz', 'w:gz') as archive:
            for path in evidence.iterdir():
                if path.name not in ('results.tgz', 'status.json') and path.is_file(): archive.add(path, arcname=path.name)
        data = evidence / 'results.tgz'
        (evidence / 'status.json').write_text(json.dumps({'phase': 'results-ready', 'bytes': data.stat().st_size,
            'sha256': hashlib.sha256(data.read_bytes()).hexdigest(), 'run_id': os.environ['PERF_RUN_ID']}) + '\n')
    while time.monotonic() - started < 5100:
        if (evidence / 'collected').exists(): return
        time.sleep(1)


if __name__ == '__main__': main()
