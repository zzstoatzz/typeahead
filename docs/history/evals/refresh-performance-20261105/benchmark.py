"""Bounded synthetic import comparison; cloud diagnostic, never a release gate."""
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


def stop(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


def run(command, log, timeout, env=None, scratch=None):
    child = subprocess.Popen(command, stdout=log, stderr=log, env=env, start_new_session=True)
    deadline=time.monotonic()+timeout
    peak=0
    try:
        while child.poll() is None:
            if time.monotonic()>deadline:raise TimeoutError('Diagnostic import deadline')
            try:
                for line in Path(f'/proc/{child.pid}/status').read_text().splitlines():
                    if line.startswith('VmHWM:'):peak=max(peak,int(line.split()[1]))
            except FileNotFoundError:pass
            if scratch is not None and shutil.disk_usage(scratch).free<4*1024**3:
                raise ValueError('Diagnostic disk reserve exceeded')
            time.sleep(.1)
        if child.returncode:
            raise RuntimeError(f"Diagnostic command failed: {command[0]}")
        return peak
    finally:
        stop(child)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def query_matrix(binary, data, log):
    public, admin = port(), port()
    child = subprocess.Popen([str(binary), 'serve', '--data', str(data),
        '--allow-stale', 'true', '--retrieval', 'global-merge',
        '--listen', f'127.0.0.1:{public}', '--admin-listen', f'127.0.0.1:{admin}'],
        stdout=log, stderr=log, start_new_session=True)
    base = f'http://127.0.0.1:{public}'
    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return json.load(response)
    try:
        for attempt in range(100):
            if child.poll() is not None:
                raise RuntimeError('Reader exited')
            try:
                get('/health')
                break
            except OSError:
                time.sleep(.1)
        else:
            raise TimeoutError('Reader startup')
        answers, timings = [], []
        for query in ['actor', 'actor0000001.test', 'Engineer', '日本太', 'absent']:
            for pds in ['', 'local.test', 'remote.test']:
                params = {'q':query, 'limit':25}
                if pds:
                    params['pds'] = pds
                path = '/search?' + urllib.parse.urlencode(params)
                answer = get(path)['actors']
                answers.append(answer)
                for _ in range(3):
                    started = time.monotonic()
                    assert get(path)['actors'] == answer
                    timings.append((time.monotonic() - started) * 1000)
        return answers, sorted(timings)
    finally:
        stop(child)


def main(binary, reference_binary=None):
    binary = Path(binary).resolve()
    count = 1_000_000
    free_before = shutil.disk_usage('/var/tmp').free
    if free_before < 12 * 1024**3:
        raise ValueError('Insufficient diagnostic scratch capacity')
    results = []
    with tempfile.TemporaryDirectory(prefix='typeahead-import-perf-', dir='/var/tmp') as folder:
        root = Path(folder)
        source = root / 'actors.jsonl'
        digest = hashlib.sha256()
        with source.open('wb') as output:
            output.write((json.dumps({'type':'manifest', 'build_id':'bulk-perf-fixture',
                'source_watermark':100, 'actor_count':count, 'source_actor_count':count,
                'partial':False}) + '\n').encode())
            for n in range(count):
                actor = {'did':f'did:fixture:{n:08}', 'handle':f'actor{n:07}.test',
                    'display_name':'名前日本太郎' if n % 17 == 0 else f'Engineer Actor {n}',
                    'quality_score':(n * 37) % 10000, 'version':100,
                    'pds':'https://local.test' if n % 3 == 0 else 'https://remote.test',
                    'labels':[], 'associated':{}, 'hidden':False}
                line = (json.dumps(actor, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
                digest.update(line)
                output.write(line)
            output.write((json.dumps({'type':'end', 'build_id':'bulk-perf-fixture',
                'actor_count':count, 'actors_sha256':digest.hexdigest()}) + '\n').encode())
        reference = None
        variants = [
            ('baseline', reference_binary or binary, 1, 128, 50000, 1),
            *([('libc-serial', binary, 1, 128, 50000, 1)] if reference_binary else []),
            ('parallel-one-segment', binary, 4, 2048, 250000, 1),
            ('parallel-eight-segments', binary, 4, 2048, 250000, 8),
            ('baseline-repeat', reference_binary or binary, 1, 128, 50000, 1),
        ]
        for name, selected_binary, threads, memory, batch, segments in variants:
            data, log_path = root / 'data', root / (name + '.log')
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            started = time.monotonic()
            with log_path.open('w') as log:
                peak = run([str(selected_binary), 'import', '--data', str(data), '--input', str(source),
                    '--indexing-threads', str(threads), '--writer-memory-mb', str(memory),
                    '--batch-size', str(batch), '--final-segments', str(segments)], log, 300, scratch=root)
            elapsed = time.monotonic() - started
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            events = [json.loads(line) for line in log_path.read_text().splitlines() if line.startswith('{')]
            phases = [item for item in events if item.get('event') in ('import_indexed', 'import_merge_finished')]
            disk = sum(item.stat().st_size for item in root.rglob('*') if item.is_file())
            if disk > 8 * 1024**3 or shutil.disk_usage(root).free < 4 * 1024**3:
                raise ValueError('Diagnostic disk budget exceeded')
            with log_path.open('a') as log:
                answers, timings = query_matrix(selected_binary, data, log)
            if reference is None:
                reference = answers
            assert answers == reference, f'Retrieval differs: {name}'
            result = {'name':name, 'elapsed_s':elapsed, 'cpu_s':after.ru_utime + after.ru_stime - usage.ru_utime - usage.ru_stime,
                'user_cpu_s':after.ru_utime-usage.ru_utime, 'system_cpu_s':after.ru_stime-usage.ru_stime,
                'peak_rss_kib':peak, 'disk_bytes':disk, 'phases':phases,
                'query_p50_ms':timings[len(timings)//2], 'query_p95_ms':timings[int(len(timings)*.95)],
                'answers_sha256':hashlib.sha256(json.dumps(answers, sort_keys=True).encode()).hexdigest()}
            results.append(result)
            print(json.dumps({'event':'import_performance_variant', **result}), flush=True)
            shutil.rmtree(data)
    print(json.dumps({'event':'import_performance_complete', 'actors':count,
        'actors_sha256':digest.hexdigest(), 'free_before':free_before,
        'free_after':shutil.disk_usage('/var/tmp').free, 'results':results}), flush=True)


if __name__ == '__main__':
    import sys
    main(sys.argv[1])
