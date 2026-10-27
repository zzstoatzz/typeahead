#!/usr/bin/env python3
"""Supervised offline corpus profile. No network beyond the private loopback namespace."""
import argparse
import ctypes
from contextlib import closing
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import uuid

import qualify

GIB = 1024 ** 3


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def residency(path, split_at=None):
    size = path.stat().st_size
    pages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
    if not size:
        return {'pages': 0, 'resident_pages': 0}
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
    libc.mincore.restype = ctypes.c_int
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.mmap.restype = ctypes.c_void_p
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.munmap.restype = ctypes.c_int
    with path.open('rb') as source:
        address = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_PRIVATE, source.fileno(), 0)
        if address == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), 'read-only private residency mapping failed')
        try:
            vector = (ctypes.c_ubyte * pages)()
            if libc.mincore(address, size, vector):
                raise OSError(ctypes.get_errno(), 'private file residency check failed')
            result = {'pages': pages, 'resident_pages': sum(value & 1 for value in vector)}
            if split_at is not None:
                if not 0 <= split_at <= size or split_at % mmap.PAGESIZE:
                    raise ValueError('invalid metadata/payload split')
                split_page = split_at // mmap.PAGESIZE
                result.update(metadata_pages=split_page,
                    metadata_resident_pages=sum(value & 1 for value in vector[:split_page]),
                    payload_pages=pages - split_page,
                    payload_resident_pages=sum(value & 1 for value in vector[split_page:]))
            return result
        finally:
            if libc.munmap(address, size):
                raise OSError(ctypes.get_errno(), 'private residency unmap failed')



def cleanup(scratch, evidence):
    payload = scratch / 'payload'
    if scratch.parent != Path('/var/lib/typeahead-perf') or scratch.is_symlink() or payload.is_symlink():
        raise ValueError('unsafe cleanup root')
    if not (scratch / 'owner.json').is_file():
        return
    owner = json.loads((scratch / 'owner.json').read_text())
    if owner != {'owner': 'typeahead-perf-goal', 'run': scratch.name}:
        raise ValueError('experiment owner mismatch')
    if not payload.exists() and (evidence / 'cleanup.json').is_file():
        return
    before = shutil.disk_usage(scratch).free
    allocated = sum(p.stat().st_blocks * 512 for p in payload.rglob('*') if p.is_file()) if payload.exists() else 0
    shutil.rmtree(payload, ignore_errors=False) if payload.exists() else None
    after = shutil.disk_usage(scratch).free
    (evidence / 'cleanup.json').write_text(json.dumps({'payload_removed': not payload.exists(),
        'allocated_before': allocated, 'free_before': before, 'free_after': after}, indent=2) + '\n')


class Trial:
    def __init__(self, args):
        self.args = args
        self.scratch, self.evidence = Path(args.scratch), Path(args.evidence)
        if self.scratch.parent != Path('/var/lib/typeahead-perf') or self.evidence.parent != Path('/var/lib/typeahead-perf-evidence'):
            raise ValueError('unexpected experiment roots')
        self.payload = self.scratch / 'payload'
        self.children = []
        self.started = time.monotonic()
        self.report = {'kind': 'offline-full-snapshot-profile', 'goal_qualified': False,
                       'limitations': ['cloud SSD differs from Fly storage', 'captured 5000-actor overlay is smaller than live overlay',
                                       'no Turso, discovery or live freshness qualification'], 'runs': {}}

    def save(self, phase, **values):
        self.report.update(phase=phase, **values)
        temporary = self.evidence / 'state.json.tmp'
        temporary.write_text(json.dumps(self.report, indent=2) + '\n')
        temporary.replace(self.evidence / 'state.json')
        print(json.dumps({'phase': phase, **values}), flush=True)

    def guard(self):
        if time.monotonic() - self.started > 5000:
            raise TimeoutError('trial wall budget exceeded')
        if shutil.disk_usage(self.scratch).free < 32 * GIB:
            raise RuntimeError('host disk reserve breached')
        for path, limit in [(self.payload, 12 * GIB), (self.evidence, 16 * 1024 * 1024)]:
            size = 0
            for p in path.rglob('*'):
                try:
                    if p.is_file(): size += p.stat().st_size
                except FileNotFoundError:
                    pass
            if size > limit: raise RuntimeError('experiment storage budget breached')

    def copy(self, source, destination, expected):
        size, copied = source.stat().st_size, 0
        hasher = hashlib.sha256()
        started = time.monotonic()
        last_report = 0
        with source.open('rb') as incoming, destination.open('xb') as output:
            while chunk := incoming.read(1024 * 1024):
                self.guard()
                output.write(chunk)
                hasher.update(chunk)
                copied += len(chunk)
                if copied - last_report >= 128 * 1024 * 1024:
                    self.save('copying', copied_bytes=copied, source_bytes=size)
                    last_report = copied
                time.sleep(max(0, copied / (8 * 1024 * 1024) - (time.monotonic() - started)))
            output.flush()
            os.fsync(output.fileno())
        if hasher.hexdigest() != expected or source.stat().st_ino == destination.stat().st_ino:
            raise RuntimeError('copied input identity mismatch')

    def stop(self, child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def run(self, command, name, timeout):
        with (self.evidence / (name + '.log')).open('xb') as output:
            child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(child)
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            self.guard()
            if time.monotonic() > deadline:
                self.stop(child)
                raise TimeoutError(name)
            time.sleep(.25)
        return child.returncode

    def start_server(self, name, overlay, input_id, trigger):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        nonce = uuid.uuid4().hex
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'SQLITE_TMPDIR': str(self.payload / 'tmp'),
               'TYPEAHEAD_EVAL_SNAPSHOT': str(self.payload / 'index.db'), 'LOCAL_DB_PATH': str(overlay),
               'TYPEAHEAD_EVAL_NONCE': nonce, 'TYPEAHEAD_EVAL_INPUT_ID': input_id,
               'TYPEAHEAD_EVAL_FEED': str(self.payload / 'feed.json'),
               'TYPEAHEAD_EVAL_MAX_ACTORS': str(getattr(self.args, 'actor_count', 5000)), 'TYPEAHEAD_EVAL_WRITER_MODE': 'mixed',
               'TYPEAHEAD_EVAL_WRITER_START': str(trigger), 'TYPEAHEAD_EVAL_WRITER_DELAY_MS': '5000',
               'TYPEAHEAD_EVAL_BACKGROUND_SLOTS': '3', 'PORT': str(port)}
        if name != 'bootstrap' or getattr(self.args, 'pre_admitted', False):
            env.update(TYPEAHEAD_EVAL_ADMITTED_SNAPSHOT='1', TYPEAHEAD_EVAL_PRIVATE_ROOT=str(self.payload))
        if name != 'bootstrap':
            env['TYPEAHEAD_EVAL_SKIP_PREFILL'] = '1'
        if getattr(self.args, 'overlay_mmap_zero', False):
            env['TYPEAHEAD_EVAL_OVERLAY_MMAP_ZERO'] = '1'
        executable = Path(self.args.bin_dir) / ('baseline' if name == 'bootstrap' else name)
        with (self.evidence / (name + '-server.log')).open('xb') as output:
            child = subprocess.Popen([str(executable)], env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(child)
        base = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + (1800 if name == 'bootstrap' else 900)
        while True:
            self.guard()
            if child.poll() is not None:
                raise RuntimeError(name + ' evaluator exited before readiness')
            try:
                identity = qualify.verify_target(base, nonce, .5, input_id)
                return child, base, nonce, identity
            except (OSError, ValueError):
                if time.monotonic() > deadline: raise TimeoutError(name + ' evaluator startup')
                time.sleep(.25)

    def metrics(self, child):
        root = Path('/proc') / str(child.pid)
        result = {k: int(v) for k, v in (line.split(':') for line in (root / 'io').read_text().splitlines())}
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        result['cpu_ms'] = (int(fields[11]) + int(fields[12])) * 1000 / os.sysconf('SC_CLK_TCK')
        result.update({line.split(':')[0]: int(line.split(':')[1].split()[0]) * 1024
                       for line in (root / 'status').read_text().splitlines()
                       if line.startswith(('VmRSS:', 'VmHWM:', 'RssAnon:', 'RssFile:'))})
        return result

    def evict_private(self, overlay):
        with closing(sqlite3.connect(overlay)) as conn:
            busy, _, _ = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if busy: raise RuntimeError('private checkpoint busy before cache reset')
        receipts = {}
        for path in [self.payload / 'index.db', self.payload / 'index.db.prefix',
                     self.payload / 'index.db.prefix.admitted', overlay]:
            if not path.resolve().is_relative_to(self.payload.resolve()) or path.is_symlink():
                raise ValueError('cache reset outside owned scratch')
            with path.open('rb') as source:
                os.fsync(source.fileno())
                os.posix_fadvise(source.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            receipts[path.name] = residency(path)
            if receipts[path.name]['resident_pages'] > 1:
                raise RuntimeError('private file remains warm after advisory eviction')
        return receipts

    def execute(self):
        self.payload.mkdir(exist_ok=False)
        (self.scratch / 'owner.json').write_text(json.dumps({'owner': 'typeahead-perf-goal', 'run': self.scratch.name}))
        self.save('preparing')
        self.guard()
        source = Path(self.args.source)
        if {name for _, name in socket.if_nameindex()} != {'lo'}:
            raise RuntimeError('private network namespace is required')
        if not os.statvfs(source).f_flag & os.ST_RDONLY:
            raise RuntimeError('archived source must be mounted read-only')
        cgroup_path = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1].lstrip('/')
        limits = {name: (cgroup_path / name).read_text().strip()
                  for name in ['memory.max', 'memory.swap.max', 'cpu.max']}
        if limits['memory.max'] != str(GIB) or limits['memory.swap.max'] != '0':
            raise RuntimeError('incorrect memory limits')
        quota, period = limits['cpu.max'].split()
        if quota == 'max' or int(quota) / int(period) > 2:
            raise RuntimeError('incorrect CPU ceiling')
        self.report['limits'] = limits
        if any(name.startswith(('TURSO_', 'INDEX_R2_', 'PREFECT_', 'LOGFIRE_', 'FLY_')) for name in os.environ):
            raise RuntimeError('credential environment is not allowed in the offline trial')
        manifest = json.loads((source / 'manifest.json').read_text())
        self.copy(source / 'index.db', self.payload / 'index.db', manifest['sha256'])
        self.copy(source / 'index.db.prefix', self.payload / 'index.db.prefix', manifest['prefix_map']['sha256'])
        legacy = source.parent / ('build-' + manifest['legacy_twin']['build_id']) / 'index.db'
        if legacy.resolve().parent.parent != source.resolve().parent:
            raise ValueError('legacy twin outside immutable archive root')
        with sqlite3.connect('file:' + str(legacy) + '?mode=ro&immutable=1', uri=True) as conn:
            actors = [{'did': did, 'blob_hex': blob.hex()} for did, blob in
                      conn.execute('SELECT did,blob FROM actor_compact_base ORDER BY did LIMIT 5000')]
        if len(actors) != 5000: raise RuntimeError('insufficient captured source actors')
        feed = self.payload / 'feed.json'
        feed.write_text(json.dumps(actors, separators=(',', ':')))
        del actors
        self.measure(manifest)

    def retire_overlay(self, path):
        if path.parent != self.payload or path.is_symlink(): raise ValueError('unsafe private overlay retirement')
        before = shutil.disk_usage(self.payload).free
        for suffix in ['', '-wal', '-shm']:
            owned = path.with_name(path.name + suffix)
            if owned.is_symlink(): raise ValueError('unexpected overlay symlink')
            owned.unlink(missing_ok=True)
        self.report.setdefault('retired_overlays', []).append({'name': path.name, 'free_before': before,
            'free_after': shutil.disk_usage(self.payload).free})

    def measure(self, manifest):
        feed = self.payload / 'feed.json'
        frozen = self.payload / 'frozen-overlay.db'
        if getattr(self.args, 'frozen_overlay', False):
            if not frozen.is_file() or frozen.is_symlink(): raise ValueError('missing frozen private overlay')
        else:
            self.save('bootstrap-admission-and-prefill')
            overlay = self.payload / 'bootstrap.db'
            child, base, nonce, _ = self.start_server('bootstrap', overlay, '0' * 64, self.payload / 'never-start')
            self.report['bootstrap_metrics'] = self.metrics(child)
            self.stop(child)
            with closing(sqlite3.connect('file:' + str(overlay) + '?mode=ro', uri=True)) as incoming, closing(sqlite3.connect(frozen)) as output:
                incoming.backup(output)
            self.retire_overlay(overlay)
        inputs = {'snapshot_sha256': manifest['sha256'], 'prefix_sha256': manifest['prefix_map']['sha256'],
                  'overlay_sha256': digest(frozen), 'feed_sha256': digest(feed),
                  'cases_sha256': digest(Path(self.args.cases)), 'captured_actors': getattr(self.args, 'actor_count', 5000),
                  'guest': {'cpu_ceiling': 2, 'memory_bytes': GIB, 'background_slots': 3},
                  'writer': {'mode': 'mixed', 'batch_size': 500, 'scheduled_interval_ms': 5000}}
        encoded = json.dumps(inputs, sort_keys=True).encode()
        input_id = hashlib.sha256(encoded).hexdigest()
        (self.evidence / 'inputs.json').write_text(json.dumps({'input_id': input_id, **inputs}, indent=2) + '\n')
        reference = self.evidence / 'reference.json'
        def client(base, nonce, timeout=2.5):
            return [sys.executable, str(Path(__file__).with_name('qualify.py')), '--url', base,
                    '--nonce', nonce, '--input-id', input_id, '--cases', self.args.cases,
                    '--rps', '10', '--concurrency', '4', '--duration', '250', '--timeout', str(timeout)]

        for name in ['baseline', 'candidate']:
            self.save(name + '-startup')
            overlay = self.payload / (name + '-overlay.db')
            shutil.copyfile(frozen, overlay)
            trigger = self.payload / (name + '-writer-start')
            if name == 'baseline':
                child, base, nonce, _ = self.start_server(name, overlay, input_id, trigger)
                self.save('reference-capture')
                code = self.run(client(base, nonce, timeout=10) + ['--rounds', '1', '--output', str(reference)], 'reference', 300)
                self.stop(child)
                (self.evidence / 'baseline-server.log').rename(self.evidence / 'reference-server.log')
                if code: raise RuntimeError('baseline reference capture failed')
            cache_receipt = self.evict_private(overlay)
            startup = time.monotonic()
            child, base, nonce, identity = self.start_server(name, overlay, input_id, trigger)
            startup_ms = (time.monotonic() - startup) * 1000
            with (self.payload / 'index.db.prefix').open('rb') as stream:
                root_bytes = struct.unpack_from('<Q', stream.read(80), 32)[0]
            post_ready = {'prefix': residency(self.payload / 'index.db.prefix', root_bytes),
                          'admission': residency(self.payload / 'index.db.prefix.admitted')}
            common = client(base, nonce)
            self.report['runs'][name] = {'identity': identity, 'startup_ms': startup_ms,
                'pre_start_residency': cache_receipt, 'post_ready_residency': post_ready, 'phases': {}}
            self.save(name + '-ready')
            if post_ready['prefix']['payload_resident_pages'] * mmap.PAGESIZE > 2 * 1024 * 1024:
                raise RuntimeError('payload pages warmed before first-touch measurement')
            for phase, rounds, p95, p99 in [('first-touch', 1, 250, 500), ('warm', 2, 50, 100), ('mixed', 2, 50, 100)]:
                self.save(name + '-' + phase)
                if phase == 'mixed': trigger.touch()
                output = self.evidence / (name + '-' + phase + '.json')
                before = self.metrics(child)
                command = common + ['--reference', str(reference), '--rounds', str(rounds),
                                    '--p95-ms', str(p95), '--p99-ms', str(p99), '--output', str(output)]
                if phase == 'mixed': command += ['--require-writer-batches', '30']
                code = self.run(command, name + '-' + phase, 280)
                after = self.metrics(child)
                report = json.loads(output.read_text())
                self.report['runs'][name]['phases'][phase] = {'returncode': code,
                    'summary': report['summary'], 'writer_overlap': report.get('writer_overlap'),
                    'process_before': before, 'process_after': after}
            self.stop(child)
            self.retire_overlay(overlay)
        self.save('completed-offline-profile')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--source')
    parser.add_argument('--bin-dir')
    parser.add_argument('--cases')
    parser.add_argument('--cleanup-only', action='store_true')
    args = parser.parse_args()
    trial = Trial(args)
    if args.cleanup_only:
        cleanup(trial.scratch, trial.evidence)
        return

    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        trial.execute()
    except BaseException as exc:
        trial.save('failed', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        for child in reversed(trial.children): trial.stop(child)
        cleanup(trial.scratch, trial.evidence)


if __name__ == '__main__':
    main()
