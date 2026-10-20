#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import random
import socket
import sqlite3
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def request(base, query, pds=None, limit=50, path='/search'):
    params = {'q': query, 'limit': limit}
    if pds is not None:
        params['pds'] = pds
    start = time.perf_counter()
    try:
        response = urllib.request.urlopen(base + path + '?' + urllib.parse.urlencode(params), timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), dict(response.headers), (time.perf_counter() - start) * 1000


def start_server(binary, snapshot, directory, name):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'TYPEAHEAD_EVAL_SNAPSHOT': str(snapshot.resolve()),
           'LOCAL_DB_PATH': str((directory / (name + '.db')).resolve()), 'PORT': str(port), 'ADMIN_SECRET': 'local-pds-evaluation'}
    log = open(directory / (name + '.log'), 'w')
    process = subprocess.Popen([str(binary.resolve())], env=env, stdout=log, stderr=log)
    log.close()
    base = f'http://127.0.0.1:{port}'
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f'{name} failed; inspect its log')
        try:
            request(base, 'bob')
            return process, base
        except OSError:
            time.sleep(.1)
    process.terminate()
    raise RuntimeError(f'{name} did not start')


def summarize(values):
    values = sorted(values)
    return {'n': len(values), 'p50_ms': round(statistics.median(values), 3),
            'p95_ms': round(values[int(len(values)*.95)], 3), 'max_ms': round(max(values), 3)}


def main():
    parser = argparse.ArgumentParser(description='Compare real local search servers against a PDS-only snapshot and pre-change snapshot')
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--full', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-server', type=Path)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--standard-benchmarks', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    processes = []
    try:
        urls = []
        for name, snapshot in [('full', args.full), ('reference', args.reference), ('baseline', args.baseline)]:
            process, url = start_server(args.baseline_server if name == "baseline" and args.baseline_server else args.server, snapshot, args.output, name)
            processes.append(process)
            urls.append(url)
        full, reference, baseline = urls
        queries = ['b', 'bo', 'bob', 'bob smith', 'smith', '佐藤', '太郎', 'éloïse', 'muller',
                   'definitelyarealcompany', 'bob.test', 'bob.acme.test', 'bob.hidden.test', 'missing']
        rng = random.Random(914)
        for _ in range(200):
            handle = f'{rng.choice(["bob", "person"])}{rng.randrange(100000)}.custom.test'
            queries.append(handle[:rng.randrange(1, len(handle)+1)])
        comparisons = 0
        for query in queries:
            for limit in [1, 10, 50, 100]:
                scoped = request(full, query, 'acme.test', limit)
                expected = request(reference, query, limit=limit)
                assert scoped[0] == expected[0] == 200, (query, scoped, expected)
                assert scoped[1] == expected[1], ('PDS-only oracle', query, limit, scoped[1], expected[1])
                assert request(full, query, limit=limit)[1] == request(baseline, query, limit=limit)[1], ('global regression', query)
                comparisons += 2
        got = request(full, 'bob', 'HTTPS://ACME.TEST./')
        assert got[0] == 200 and got[1] == request(full, 'bob', 'acme.test')[1]
        assert json.loads(request(full, 'bob', 'missing.test')[1]) == {'actors': []}
        assert request(full, 'bob', 'big.test')[0] == 503
        old_scope = request(baseline, 'bob', 'acme.test')
        if args.baseline_server:
            assert old_scope[0] == 200 and not old_scope[2].get('x-typeahead-pds')
        else:
            assert old_scope[0] == 503
        for value in ['', '*', 'acme.test/path', 'acme.test?x', 'a..test', 'user@acme.test', 'acme.test:443', 'a'*254]:
            assert request(full, 'bob', value)[0] == 400, value
        scoped = json.loads(request(full, 'bob', 'acme.test')[1])['actors']
        global_rows = json.loads(request(full, 'bob')[1])['actors']
        assert scoped and not set(a['did'] for a in scoped).intersection(a['did'] for a in global_rows)
        assert json.loads(request(full, 'bob.test', 'acme.test')[1])['actors'][0]['did'] == 'did:plc:exact'
        debug = json.loads(request(full, 'bob', 'acme.test', path='/debug/search')[1])
        assert all(k['key'].endswith('\x1facme.test') for k in debug['keys'])
        timings = {}
        cases = [('global_baseline', baseline, None), ('global_candidate', full, None),
                 ('scoped', full, 'acme.test'), ('reference', reference, None)]
        values = {name: [] for name, _, _ in cases}
        for i in range(400):
            rng.shuffle(cases)
            for name, url, pds in cases:
                status, _, _, elapsed = request(url, queries[i % len(queries)], pds)
                assert status == 200
                values[name].append(elapsed)
        timings.update({name: summarize(v) for name, v in values.items()})
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(lambda i: request(full, queries[i % len(queries)], 'acme.test'), range(400)))
        assert all(row[0] == 200 for row in responses)
        timings['scoped_concurrency_20'] = summarize([r[3] for r in responses])
        if args.worker:
            subprocess.run(['node', 'scripts/test-pds-worker.mjs', full, baseline], check=True)
        standard_benchmarks = {}
        if args.standard_benchmarks:
            for name, command in [('ranking', ['just', 'bench-ranking', full]), ('search', ['just', 'bench-search', '--url', full, '--quick'])]:
                with open(args.output / (name + '.log'), 'w') as output:
                    result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
                    standard_benchmarks[name] = result.returncode
        def refresh(**fields):
            payload = {'did': 'did:plc:exact', 'handle': 'bob.test', 'display_name': 'Bob',
                       'pds': 'https://acme.test', 'updated_at': 1800000001, **fields}
            req = urllib.request.Request(full + '/admin/refresh', data=json.dumps(payload).encode(),
                                         headers={'Authorization': 'Bearer local-pds-evaluation', 'Content-Type': 'application/json'})
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=10) as response:
                assert response.status == 200
            return (time.perf_counter()-start)*1000
        refresh(pds='https://sub.acme.test')
        assert json.loads(request(full, 'bob.test', 'acme.test')[1])['actors'] == []
        assert json.loads(request(full, 'bob.test', 'sub.acme.test')[1])['actors'][0]['did'] == 'did:plc:exact'
        refresh(hidden=1)
        assert json.loads(request(full, 'bob.test', 'acme.test')[1])['actors'] == []
        refresh(handle='alice.test', display_name='Alice')
        assert json.loads(request(full, 'bob.test', 'acme.test')[1])['actors'] == []
        assert json.loads(request(full, 'alice.test', 'acme.test')[1])['actors'][0]['did'] == 'did:plc:exact'
        refresh(pds='https://big.test')
        with sqlite3.connect(args.output / 'full.db') as db:
            assert db.execute("SELECT COUNT(*) FROM prefix_overlay WHERE key LIKE ?", ('%\x1fbig.test',)).fetchone()[0] == 0
            policy = db.execute("SELECT value FROM sync_meta WHERE key='pds_scope_policy'").fetchone()[0]
            db.execute("DELETE FROM sync_meta WHERE key='pds_scope_policy'")
        assert request(full, 'bob', 'acme.test')[0] == 503
        assert request(full, 'bob')[0] == 200
        with sqlite3.connect(args.output / 'full.db') as db:
            db.execute("INSERT INTO sync_meta VALUES('pds_scope_policy',?)", (policy,))
        assert request(full, 'bob', 'acme.test')[0] == 200
        update_times = {'small': [], 'large': []}
        for i in range(200):
            for size, host in [('small', 'acme.test'), ('large', 'big.test')]:
                update_times[size].append(refresh(did=f'did:plc:eval-{size}-{i}', handle=f'eval{i}.test', pds='https://'+host))
        timings['update_small'] = summarize(update_times['small'])
        timings['update_large'] = summarize(update_times['large'])
        report = {'standard_benchmarks': standard_benchmarks, 'baseline_engine': str(args.baseline_server or args.server), 'fixture_actors' : 100005, 'comparisons': comparisons, 'queries': len(queries),
                  'snapshot_bytes': {name: path.stat().st_size + (Path(str(path) + '.prefix').stat().st_size if Path(str(path) + '.prefix').exists() else 0) for name, path in [('full', args.full), ('baseline', args.baseline), ('reference', args.reference)]},
                  'http_latency': timings, 'debug_scoped_bob': debug}
        (args.output / 'results.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k != 'debug_scoped_bob'}, indent=2))
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=10)


if __name__ == '__main__':
    main()
