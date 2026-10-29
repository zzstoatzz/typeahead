"""Bounded, loopback-only paired engine screening; no provisioning or credentials."""
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
from pathlib import Path
import time
import urllib.parse

ROOT = Path(__file__).resolve().parent
PROTECTED = {'8e3d79b70dd748', '801539a6551778', '7845636a3d0ee8', '840e95a2153308'}

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def local_url(value):
    p = urllib.parse.urlsplit(value)
    if (p.scheme != 'http' or p.hostname not in {'localhost', '127.0.0.1', '::1'}
            or p.username or p.password or p.path not in {'', '/'} or p.query or p.fragment):
        raise ValueError('only explicit loopback evaluator/tunnel origins accepted')
    return p

def load(path):
    m = json.loads(path.read_text())
    if m['schema'] != 1 or m['mode'] not in {'pilot', 'full'}:
        raise ValueError('unknown qualification schema/mode')
    for name in ['corpus', 'queries', 'updates', 'oracle']:
        item = m['inputs'][name]
        target = path.parent / item['path']
        if digest(target) != item['sha256']:
            raise ValueError(name + ' hash mismatch')
    if m['inputs']['oracle']['method'] != 'independent-raw-profile':
        raise ValueError('independent oracle provenance required')
    cases = json.loads((path.parent / m['inputs']['queries']['path']).read_text())
    oracle = json.loads((path.parent / m['inputs']['oracle']['path']).read_text())
    if not 1 <= len(cases) <= 2048 or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('invalid case count/IDs')
    for c in cases:
        p = urllib.parse.urlsplit(c['path'])
        if p.path != '/search' or p.scheme or p.netloc or p.fragment:
            raise ValueError('only /search paths accepted')
        expected = oracle[c['id']]
        for arm in ['native', 'tantivy']:
            if not isinstance(expected[arm]['ids'], list) or not isinstance(expected[arm]['status'], int):
                raise ValueError('explicit ordered oracle IDs/status required')
        if expected['native'] != expected['tantivy'] and not expected.get('intentional_difference'):
            raise ValueError('different semantics require an explicit independent-oracle explanation')
    if set(m['arms']) != {'native', 'tantivy'}:
        raise ValueError('exactly native and tantivy arms required')
    ids = []
    for arm in m['arms'].values():
        local_url(arm['url'])
        if arm['machine_id'] in PROTECTED or arm['guest'] != {'cpu_kind': 'shared', 'cpus': 2, 'memory_mb': 1024}:
            raise ValueError('protected machine or unmatched guest')
        ids.append(arm['machine_id'])
    if len(set(ids + [m['load_generator_id']])) != 3:
        raise ValueError('load generator must be separate from both distinct engines')
    if len({a['volume_id'] for a in m['arms'].values()}) != 2:
        raise ValueError('independent volumes required')
    if not m['native_source']['immutable_r2'] or m['native_source']['protected_disk_used']:
        raise ValueError('native control must use immutable R2, not protected disks')
    total = 0
    for p in m['phases']:
        if p['name'] not in {'pilot', 'first-touch', 'warm', 'mixed'}:
            raise ValueError('unknown phase')
        if not (1 <= p['attempts'] <= 5000 and 0 < p['rps'] <= 10 and 1 <= p['concurrency'] <= 8):
            raise ValueError('phase exceeds bounds')
        if p['attempts'] / p['rps'] + 10 > p['deadline_s'] or p['deadline_s'] > 330:
            raise ValueError('insufficient or excessive phase deadline')
        total += p['deadline_s']
    if total > (90 if m['mode'] == 'pilot' else 900):
        raise ValueError('measurement time budget exceeded')
    if m['mode'] == 'pilot' and sum(p['attempts'] for p in m['phases']) > 60:
        raise ValueError('pilot request budget exceeded')
    if m['mode'] == 'full':
        if len(m['phases']) != 3 or [p['name'] for p in m['phases']] != ['first-touch', 'warm', 'mixed']:
            raise ValueError('full run requires first-touch, warm, mixed in order')
        first, warm, mixed = m['phases']
        if not (512 <= first['attempts'] <= len(cases) and warm['attempts'] >= 1000
                and mixed['attempts'] >= 1000 and mixed['attempts'] / mixed['rps'] >= 300):
            raise ValueError('insufficient distinct first-touch or warm/mixed samples')
    return m, cases, oracle

def request(base, path, timeout):
    p = local_url(base)
    conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        conn.request('GET', path, headers={'Connection': 'close'})
        response = conn.getresponse()
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('response deadline')
            if conn.sock:
                conn.sock.settimeout(remaining)
            chunk = response.read1(65536)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > 524288:
                raise ValueError('response byte budget')
        return response.status, json.loads(body)
    finally:
        conn.close()

def identity(m, name):
    arm = m['arms'][name]
    code, value = request(arm['url'], '/__evaluation', 3)
    required = {'nonce': m['nonce'], 'arm': name, 'machine_id': arm['machine_id'],
                'volume_id': arm['volume_id'], 'guest': arm['guest'], 'ready': True,
                'corpus_sha256': m['inputs']['corpus']['sha256'],
                'updates_sha256': m['inputs']['updates']['sha256']}
    if code != 200 or any(value.get(k) != v for k, v in required.items()):
        raise ValueError('evaluator ownership/input/shape mismatch: ' + name)
    return value

def percentile(rows, p):
    return sorted(rows)[math.ceil(len(rows) * p) - 1]

def phase(m, name, phase, cases, oracle):
    before = identity(m, name)
    start = time.monotonic()
    deadline = start + phase['deadline_s']
    def one(i):
        case = cases[i % len(cases)]
        scheduled = start + i / phase['rps']
        time.sleep(max(0, scheduled - time.monotonic()))
        sent = time.monotonic()
        error, status, ids = None, None, None
        try:
            if sent >= deadline:
                raise TimeoutError('phase deadline')
            status, body = request(m['arms'][name]['url'], case['path'], min(3, deadline - sent))
            if status == 200 and (not isinstance(body, dict) or not isinstance(body.get('actors'), list)):
                raise ValueError('invalid actor response shape')
            ids = [a['did'] for a in body['actors']] if status == 200 else []
            expected = oracle[case['id']][name]
            correct = status == expected['status'] and ids == expected['ids'] and len(ids) == len(set(ids))
        except Exception as exc:
            error, correct = type(exc).__name__, False
        done = time.monotonic()
        return {'case': case['id'], 'index': i, 'status': status, 'correct': correct, 'error': error,
                'queue_ms': (sent - scheduled) * 1000, 'request_ms': (done - sent) * 1000,
                'total_ms': (done - scheduled) * 1000}
    with concurrent.futures.ThreadPoolExecutor(max_workers=phase['concurrency']) as pool:
        rows = list(pool.map(one, range(phase['attempts'])))
    after = identity(m, name)
    return {'phase': phase['name'], 'before': before, 'after': after, 'attempts': rows,
            'failures': sum(not r['correct'] for r in rows),
            'total_ms': {str(p): percentile([r['total_ms'] for r in rows], p) for p in [.5, .95, .99, 1]}}

def compare(m, reports):
    failures = sum(p['failures'] for arm in reports.values() for p in arm)
    comparisons = []
    for a, b in zip(reports['native'], reports['tantivy']):
        comparisons.append({'phase': a['phase'], 'native_p99_ms': a['total_ms']['0.99'],
                            'tantivy_p99_ms': b['total_ms']['0.99'],
                            'tantivy_at_least_as_fast': all(b['total_ms'][p] <= a['total_ms'][p] for p in ['0.95', '0.99'])})
    return {'correctness_passed': failures == 0, 'failures': failures, 'comparisons': comparisons,
            'speed_screen_passed': failures == 0 and all(x['tantivy_at_least_as_fast'] for x in comparisons),
            'full_qualification': False,
            'remaining_evidence': ['matched committed update schedule/cursors and final actor-state digest',
                'correctness at each update/restart checkpoint, including affected actors',
                'same-shape restart and durable state recovery', 'independent observer memory/IO/CPU and lifecycle receipts'],
            'limitations': ['Sequential arms on distinct shared hosts: screening, not host-effect attribution.',
                'Timing includes all failed attempts and client queue.',
                'Per-arm independent oracle allows declared recall changes; byte equality is not assumed.',
                'Mixed phase may only time queries with stable expected IDs; affected actors need checkpoint oracle.',
                'No physical-cold-disk claim; corpus preparation time is separate from measurement.']}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path)
    p.add_argument('--validate-only', action='store_true')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    m, cases, oracle = load(args.manifest)
    if args.validate_only:
        print(json.dumps({'valid': True, 'mode': m['mode'], 'cases': len(cases)})); return
    if args.output is None or args.output.exists():
        raise ValueError('new output path required')
    for name in m['arms']:
        identity(m, name)
    reports = {name: [phase(m, name, ph, cases, oracle) for ph in m['phases']] for name in ['native', 'tantivy']}
    result = {'schema': 1, 'manifest_sha256': digest(args.manifest), 'arms': reports, **compare(m, reports)}
    with args.output.open('x') as f:
        f.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ['correctness_passed', 'speed_screen_passed', 'full_qualification']}))
    raise SystemExit(0 if result['correctness_passed'] else 1)

if __name__ == '__main__':
    main()
