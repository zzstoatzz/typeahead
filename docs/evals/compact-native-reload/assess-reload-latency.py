"""Assess only the separately frozen20-round reload protocol, never diagnostics."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics


def interval(values):
    rng = random.Random(94017)
    boot = sorted(statistics.median(rng.choices(values, k=20)) for _ in range(10000))
    return {'median_ratio': statistics.median(values), 'lower_one_sided_95': boot[499],
            'upper_one_sided_95': boot[9499], 'paired_ratios': values}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('results', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite assessment')
    root = Path(__file__).resolve().parent
    protocol_file = root / 'reload-latency-formal-protocol.json'
    p = json.loads(protocol_file.read_text())
    d = json.loads(args.results.read_text())
    assert d['formal'] is True and d['complete'] is True
    assert d['protocol_sha256'] == hashlib.sha256(protocol_file.read_bytes()).hexdigest()
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == p['assessor_sha256']
    assert d['binary_sha256_before'] == d['binary_sha256_after'] == p['binary_sha256']
    assert d['queries'] == p['queries'] and d['seed'] == 1791499057
    assert d['duration_s'] == 180 and d['swap_at_s'] == 2 and d['diagnostic_window_s'] == [2, 180]
    assert d['workers'] == 7 and d['final_reservoir'] == 10000 and d['reservoir_per_worker'] == 10000
    roles = ['baseline', 'compact', 'baseline-control']
    rng = random.Random(1791499057)
    schedule = []
    for _ in range(7):
        order = roles.copy(); rng.shuffle(order)
        rotations = [order[i:] + order[:i] for i in range(3)]; rng.shuffle(rotations)
        schedule.extend(rotations)
    assert d['schedule'] == schedule[:20]
    rows = d['runs']; assert len(rows) == 60
    assert [(r['round'], r['role']) for r in rows] == [(i, role) for i, order in enumerate(schedule[:20]) for role in order]
    for r in rows:
        assert r['variant'] == ('baseline' if r['role'] == 'baseline-control' else r['role'])
        assert r['all_bodies_match'] is True and r['non200'] == 0 and r['reads'] > 0
        assert sum(r['per_worker_reads']) == sum(r['per_query_reads']) == r['reads']
        assert all(n > 0 for n in r['per_worker_reads']) and all(n > 0 for n in r['per_query_reads'])
        assert all(r['successful_completions_per_second'][i] > 0 for i in range(1, 180))
        swap = r['swap']
        assert swap['status'] == 200 and 2 <= swap['start_s'] <= 2.1 and swap['start_s'] < swap['end_s'] < 180
        assert r['reload_completed_within_fixed_window'] is True
        required = {'.db', '.prefix'} if r['variant'] == 'compact' else {'.db'}
        assert set(r['artifact_identities']) == required
        for pair in r['artifact_identities'].values():
            assert len(pair) == 2 and (pair[0]['device'], pair[0]['inode']) != (pair[1]['device'], pair[1]['inode'])
        samples = r['samples']; assert len(samples) == 10000
        assert all(2 <= s['start_s'] < 180 and s['end_s'] >= s['start_s'] and s['ms'] >= 0 for s in samples)
        values = sorted(s['ms'] for s in samples)
        for metric, quantile in [('p95_ms', .95), ('p99_ms', .99)]:
            assert r['reload_window_sampled_latency'][metric] == values[int(len(values) * quantile)]
    metrics = {}
    for metric in ('p95_ms', 'p99_ms', 'cpu_seconds_per_request'):
        ratios = []; baseline = []; candidate = []
        for i in range(20):
            by_role = {r['role']: r for r in rows if r['round'] == i}
            b = statistics.mean(by_role[v]['reload_window_sampled_latency'][metric] for v in ('baseline', 'baseline-control'))
            c = by_role['compact']['reload_window_sampled_latency'][metric]
            assert b > 0
            baseline.append(b); candidate.append(c); ratios.append(c / b)
        metrics[metric] = {**interval(ratios), 'baseline_median_ms': statistics.median(baseline), 'candidate_median_ms': statistics.median(candidate)}
    primary = metrics['p95_ms']
    secondary = [name for name in ('p99_ms','cpu_seconds_per_request') if metrics[name]['lower_one_sided_95'] > 1]
    result = {'protocol_sha256': d['protocol_sha256'], 'results_sha256': hashlib.sha256(args.results.read_bytes()).hexdigest(),
              'validity_checks_passed': True, 'rounds': 20, 'metrics': metrics,
              'passed': primary['upper_one_sided_95'] <= 1 and not secondary, 'secondary_regressions_requiring_investigation': secondary,
              'classification': 'passed' if primary['upper_one_sided_95'] <= 1 else 'regressed' if primary['lower_one_sided_95'] > 1 else 'inconclusive',
              'scope': 'Paired median round-p95 for this fixed178-second reload read window; p99 secondary. No claim for arbitrary traffic or full worst-case stalls.'}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 2)


if __name__ == '__main__':
    main()
