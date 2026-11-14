import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

parser = argparse.ArgumentParser()
parser.add_argument('input', type=Path)
parser.add_argument('--variant', choices=['compact', 'candidate'], default='compact')
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--warm', action='store_true')
args = parser.parse_args()
raw = args.input.read_bytes()
rows = json.loads(raw)
assert len(rows) == 180 and not any(r['errors'] for r in rows), 'incomplete or failed trials'
assert not any(r['usage'].get('forced_shutdown', False) for r in rows)
if args.warm:
    assert all(all(row['timings'][phase]['io']['read_bytes'] == 0 for phase in ('restart_sqlite_cache', 'warm', 'broad', 'concurrency8')) for row in rows), 'physical reads during warm trial'
by_round = {}
for repeat in range(60):
    group = [r for r in rows if r['round'] == repeat]
    assert len(group) == 3 and {r['variant'] for r in group} == {'baseline', 'baseline-control', args.variant}
    by_round[repeat] = {r['variant']: r for r in group}


def interval(values):
    assert len(values) == 60 and all(math.isfinite(v) and v >= 0 for v in values)
    rng = random.Random(94017)
    boot = sorted(statistics.median(rng.choices(values, k=60)) for _ in range(10000))
    return {'median_ratio': statistics.median(values), 'lower_one_sided_95': boot[499],
            'upper_one_sided_95': boot[9499]}


def compare(getter):
    ratios = []
    for group in by_round.values():
        reference = statistics.mean(getter(group[v]) for v in ('baseline', 'baseline-control'))
        if reference <= 0:
            return {'unresolved': 'measurement below clock resolution'}
        ratios.append(getter(group[args.variant]) / reference)
    return interval(ratios)


phases = ('restart_sqlite_cache', 'warm', 'broad', 'concurrency8', 'mixed_reads')
primary = {phase: compare(lambda row, phase=phase: row['timings'][phase]['p95_ms']) for phase in phases}
secondary = {phase + '_p99': compare(lambda row, phase=phase: row['timings'][phase]['p99_ms']) for phase in phases}
secondary.update({phase + '_cpu': compare(lambda row, phase=phase: row['timings'][phase]['cpu_seconds'])
                  for phase in phases[:-1]})
secondary['mixed_writes_p95'] = compare(lambda row: row['timings']['mixed_writes']['p95_ms'])
secondary['mixed_serving_cpu'] = compare(lambda row: row['usage']['mixed_read_write_cpu_seconds'])
secondary['peak_rss'] = compare(lambda row: row['usage']['peak_rss_bytes'])
result = {
    'input_sha256': hashlib.sha256(raw).hexdigest(), 'variant': args.variant, 'rounds': 60,
    'primary': primary, 'secondary': secondary,
    'primary_pass': all(value.get('upper_one_sided_95', math.inf) <= 1 for value in primary.values()),
    'secondary_regressions_requiring_investigation': [name for name, value in secondary.items()
                                                     if value.get('lower_one_sided_95', 0) > 1],
    'startup_ms_medians': {variant: statistics.median(r['usage']['startup_ms'] for r in rows if r['variant'] == variant)
                           for variant in ('baseline', 'baseline-control', args.variant)},
    'baseline_control_noise': {phase: interval([g['baseline-control']['timings'][phase]['p95_ms'] /
                                               g['baseline']['timings'][phase]['p95_ms'] for g in by_round.values()])
                               for phase in phases},
}
args.output.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['primary_pass'] and not result['secondary_regressions_requiring_investigation'] else 2)
