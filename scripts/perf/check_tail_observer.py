"""Read-only real-service overlap/replacement test, with bounded cleanup."""
import datetime
import json
from pathlib import Path
import time

from tail_observer import TailObserver, WORKERS

output = Path('docs/evals/perf-goal-20261027/transport-overlap/shared-hint/dual-observer-preflight.json')
if output.exists():
    raise RuntimeError('preserve prior observer evidence')
record = {'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'production_mutations': 0, 'generated_requests': 0, 'unique_invocations': {}, 'replacements': []}
observer = TailObserver()
try:
    for role in WORKERS:
        observer.add(role, 0)
    observer.wait_ready(1)
    for role in WORKERS:
        observer.add(role, 1)
    observer.wait_ready(2)
    observer.armed = True
    started = time.monotonic()
    replaced = set()
    print('overlapping real-service observers connected; read-only check active', flush=True)
    while time.monotonic() - started < 600:
        elapsed = time.monotonic() - started
        for role, after in [('public', 30), ('transport', 60)]:
            if elapsed >= after and role not in replaced:
                child = next(c for c in observer.connected(role) if c['slot'] == 0)
                observer.replace(child)
                replaced.add(role)
                record['replacements'].append({'role': role, 'elapsed_s': round(elapsed, 1)})
        for role, entry in observer.pump():
            record['unique_invocations'][role] = record['unique_invocations'].get(role, 0) + 1
            if entry.get('exceptions'):
                raise RuntimeError('runtime exception observed during preflight')
    observer.wait_ready(2)
    for child in observer.connected('public'):
        observer.stop(child)
    try:
        observer.pump()
    except RuntimeError as exc:
        if str(exc) != 'all observers lost for a Worker':
            raise
        record['total_loss_rejected'] = True
    else:
        raise RuntimeError('complete observer loss was accepted')
    record['status'] = 'passed-real-overlap-replacement-and-total-loss-check'
except BaseException as exc:
    record.update(status='failed', error=type(exc).__name__ + ': ' + str(exc))
finally:
    try:
        record['observer'] = observer.close()
    except BaseException as exc:
        record.update(status='cleanup-unconfirmed', cleanup_error=type(exc).__name__ + ': ' + str(exc))
    record['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    output.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'status': record['status'], 'unique_invocations': record['unique_invocations'],
                      'error': record.get('error'), 'cleanup_error': record.get('cleanup_error')}), flush=True)
