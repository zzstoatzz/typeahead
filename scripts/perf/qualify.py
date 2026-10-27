#!/usr/bin/env python3
"""Bounded, failure-inclusive HTTP qualification of an explicitly marked local evaluator."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


def request(base, path, timeout):
    started = time.monotonic()
    status, body, error = None, b'', None
    try:
        req = urllib.request.Request(base + path, headers={'User-Agent': 'typeahead-isolated-qualification/1'})
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = response.status
            chunks = []
            length = 0
            while chunk := response.read1(65536):
                length += len(chunk)
                if length > 524288:
                    raise ValueError('response exceeds 512 KiB')
                chunks.append(chunk)
                if time.monotonic() - started > timeout:
                    raise TimeoutError('response exceeded deadline')
            body = b''.join(chunks)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        error = type(exc).__name__
    return {'status': status, 'sha256': hashlib.sha256(body).hexdigest(),
            'elapsed_ms': (time.monotonic() - started) * 1000, 'error': error}, body


def verify_target(base, nonce, timeout, input_id=None):
    url = urllib.parse.urlsplit(base)
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
            or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('qualification requires an explicit HTTP loopback evaluator or tunnel')
    result, body = request(base.rstrip('/'), '/__evaluation', timeout)
    if result['error'] or result['status'] != 200:
        raise ValueError('target has no evaluation identity; no workload sent')
    identity = json.loads(body)
    if identity.get('nonce') != nonce or identity.get('ready') is not True:
        raise ValueError('evaluation identity or readiness mismatch; no workload sent')
    if input_id is not None and identity.get('input_id') != input_id:
        raise ValueError('frozen input identity mismatch; no workload sent')
    return identity


def load_cases(path):
    cases = json.loads(Path(path).read_text())
    if not isinstance(cases, list) or not 1 <= len(cases) <= 2048:
        raise ValueError('provide 1..2048 explicit cases')
    seen = set()
    for case in cases:
        if not isinstance(case.get('id'), str) or case['id'] in seen:
            raise ValueError('case IDs must be unique strings')
        seen.add(case['id'])
        parsed = urllib.parse.urlsplit(case['path'])
        if parsed.path != '/search' or parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError('cases may only address /search on the verified target')
        case.setdefault('status', 200)
    return cases


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def summarize(rows, p95_ms, p99_ms):
    searches = [r['end_to_end_ms'] for r in rows if r['expected_status'] == 200]
    p95, p99 = percentile(searches, .95), percentile(searches, .99)
    failures = sum(not r['correct'] for r in rows)
    return {'attempts': len(rows), 'failures': failures, 'search_attempts': len(searches),
            'p50_ms': percentile(searches, .5), 'p95_ms': p95, 'p99_ms': p99,
            'max_ms': max(searches) if searches else None,
            'passed': failures == 0 and p95 is not None and p95 <= p95_ms and p99 <= p99_ms}


def capture(base, cases, timeout, rps=4):
    reference = {}
    attempts = []
    started = time.monotonic()
    for index, case in enumerate(cases):
        time.sleep(max(0, started + index / rps - time.monotonic()))
        result, body = request(base, case['path'], timeout)
        correct = result['error'] is None and result['status'] == case['status']
        try:
            payload = json.loads(body)
            correct = correct and isinstance(payload, dict)
            if 'error' in case:
                correct = correct and payload.get('error') == case['error']
            if case['status'] == 200:
                actors = payload['actors']
                correct = correct and isinstance(actors, list) and len(actors) >= case.get('min_actors', 0)
                if 'first_handle' in case:
                    correct = correct and bool(actors) and actors[0].get('handle') == case['first_handle']
        except (ValueError, KeyError, TypeError, AttributeError):
            correct = False
        attempts.append({'id': case['id'], **result, 'correct': correct})
        reference[case['id']] = {'path': case['path'], 'status': case['status'], 'sha256': result['sha256']}
    return {'reference': reference, 'attempts': attempts, 'passed': all(r['correct'] for r in attempts)}


def measure(base, cases, reference, rounds, concurrency, rps, timeout, duration):
    expected = reference['reference']
    for case in cases:
        if expected[case['id']]['path'] != case['path'] or expected[case['id']]['status'] != case['status']:
            raise ValueError('reference/case contract mismatch')
    start = time.monotonic()
    deadline = start + duration

    def attempt(index):
        case = cases[index % len(cases)]
        scheduled = start + index / rps
        time.sleep(max(0, scheduled - time.monotonic()))
        queue_ms = max(0, (time.monotonic() - scheduled) * 1000)
        if time.monotonic() >= deadline:
            result = {'status': None, 'sha256': None, 'elapsed_ms': 0, 'error': 'run_deadline'}
        else:
            result, _ = request(base, case['path'], min(timeout, deadline - time.monotonic()))
        correct = (result['error'] is None and result['status'] == case['status']
                   and result['sha256'] == expected[case['id']]['sha256'])
        return {'id': case['id'], 'index': index, 'expected_status': case['status'],
                'queue_ms': queue_ms, 'end_to_end_ms': (time.monotonic() - scheduled) * 1000,
                'correct': correct, **result}

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(attempt, range(len(cases) * rounds)))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--input-id', required=True)
    parser.add_argument('--cases', required=True)
    parser.add_argument('--reference')
    parser.add_argument('--output', required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--rps', type=float, default=4)
    parser.add_argument('--timeout', type=float, default=2.5)
    parser.add_argument('--duration', type=float, default=180)
    parser.add_argument('--p95-ms', type=float, default=50)
    parser.add_argument('--p99-ms', type=float, default=100)
    args = parser.parse_args()
    if Path(args.output).exists():
        raise ValueError('output already exists; preserve each qualification attempt separately')
    cases = load_cases(args.cases)
    if not (1 <= args.concurrency <= 8 and 0 < args.rps <= 10 and 0 < args.timeout <= 10
            and 0 < args.duration <= 300 and 1 <= args.rounds <= 20
            and len(cases) * args.rounds <= 20000
            and len(cases) * args.rounds / args.rps <= args.duration):
        raise ValueError('workload exceeds qualification bounds')
    if len(args.input_id) != 64 or any(c not in '0123456789abcdef' for c in args.input_id):
        raise ValueError('input ID must be a SHA256 of the frozen input manifest')
    identity = verify_target(args.url, args.nonce, args.timeout, args.input_id)
    if args.reference:
        reference = json.loads(Path(args.reference).read_text())
        if reference.get('passed') is not True:
            raise ValueError('reference capture did not pass')
        if reference.get('identity', {}).get('input_id') != args.input_id:
            raise ValueError('reference uses a different frozen input manifest')
        rows = measure(args.url.rstrip('/'), cases, reference, args.rounds, args.concurrency,
                       args.rps, args.timeout, args.duration)
        report = {'kind': 'qualification', 'identity': identity, 'settings': vars(args), 'attempts': rows,
                  'summary': summarize(rows, args.p95_ms, args.p99_ms)}
        report['summary']['minimum_search_attempts'] = 1000
        report['summary']['enough_samples'] = report['summary']['search_attempts'] >= 1000
        report['summary']['passed'] = report['summary']['passed'] and report['summary']['enough_samples']
        passed = report['summary']['passed']
    else:
        report = {'kind': 'reference-capture', 'identity': identity, **capture(args.url.rstrip('/'), cases, args.timeout, args.rps)}
        passed = report['passed']
    try:
        verify_target(args.url, args.nonce, args.timeout, args.input_id)
    except (OSError, ValueError) as exc:
        report['identity_end_error'] = str(exc)
        passed = False
        if 'summary' in report:
            report['summary']['passed'] = False
        else:
            report['passed'] = False
    with Path(args.output).open('x') as output:
        output.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report.get('summary', {'passed': passed, 'captured': len(cases)})))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
