"""Matched-duration reload diagnostic; no acceptance threshold is inferred here."""
import argparse
import concurrent.futures as cf
import hashlib
import heapq
import json
import os
from pathlib import Path
import random
import threading
import time


def identities(root, variant):
    import release_reload_support as support
    return support.identities(variant)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', required=True)
    ap.add_argument('--formal', action='store_true')
    ap.add_argument('--candidate', default='compact')
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--duration', type=float, default=180)
    ap.add_argument('--seed', type=int, default=1791490051)
    args = ap.parse_args()
    if args.rounds < 1 or not 5 <= args.duration <= 180:
        ap.error('rounds must be positive; duration must be 5..180 seconds')
    root = Path(__file__).resolve().parent
    out = root / args.output
    if out.exists():
        raise SystemExit('Output exists; refusing to overwrite evidence')
    cg = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1].lstrip('/')
    assert (cg / 'memory.max').read_text().strip() == str(2 * 1024**3)
    assert (cg / 'memory.swap.max').read_text().strip() == '0'
    os.environ.update(EVAL_OUTPUT=args.output, EVAL_VARIANTS=args.candidate, EVAL_FAMILY='real', EVAL_EVICT='1')
    import release_reload_support as a
    queries = json.loads((root / 'reload-latency-formal-protocol.json').read_text())['queries']
    assert len(queries) == 20
    protocol = None
    binary_hashes = None
    def binaries():
        return {v: hashlib.sha256(a.BINARY.read_bytes()).hexdigest()
                for v in ('baseline', args.candidate)}
    if args.formal:
        assert (args.rounds, args.duration, args.seed, args.candidate) == (20, 180, 1791499057, 'compact')
        protocol = json.loads((root / 'reload-latency-formal-protocol.json').read_text())
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == protocol['harness_sha256']
        assert queries == protocol['queries']
        assert hashlib.sha256((root/'release_reload_support.py').read_bytes()).hexdigest() == protocol['support_sha256']
        binary_hashes = binaries()
        assert binary_hashes == protocol['binary_sha256']
    artifact_checks = a.verify_artifacts()
    roles = ['baseline', args.candidate, 'baseline-control']
    schedule = []
    rng = random.Random(args.seed)
    # Each block of three rounds gives every role each temporal position once.
    for block in range((args.rounds + 2) // 3):
        order = roles.copy()
        rng.shuffle(order)
        rotations = [order[i:] + order[:i] for i in range(3)]
        rng.shuffle(rotations)
        schedule.extend(rotations)
    result = {'purpose': 'Fixed20-round formal reload latency test' if args.formal else __doc__,
              'formal': args.formal, 'binary_sha256_before': binary_hashes,
              'protocol_sha256': hashlib.sha256((root / 'reload-latency-formal-protocol.json').read_bytes()).hexdigest() if args.formal else None, 'duration_s': args.duration, 'swap_at_s': 2,
              'diagnostic_window_s': [2, args.duration], 'seed': args.seed,
              'schedule': schedule[:args.rounds], 'queries': queries,
              'workers': 7, 'reservoir_per_worker': 10000, 'final_reservoir': 10000,
              'acceptance': protocol['gate'] if args.formal else 'None: diagnostic only.',
              'sampling': 'Window [2, duration) only. Independent uniform priorities; merge seven local bottom-10000 reservoirs and keep global bottom-10000. No per-read shared lock.',
              'artifact_checks': artifact_checks, 'runs': []}
    golden = None
    def save():
        (out / 'reload-results.json').write_text(json.dumps(result, indent=2))
    for round_index, order in enumerate(schedule[:args.rounds]):
        for role in order:
            variant = 'baseline' if role == 'baseline-control' else role
            pair = identities(root, variant)
            for item in pair.values():
                fd = os.open(item[1]['path'], os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                finally:
                    os.close(fd)
            server = a.Server(variant, 'real', f'reload-{round_index:03d}-{role}', alternate='alternate')
            try:
                observed = [server.body(q) for q in queries]
                if golden is None:
                    golden = observed
                    result['response_sha256'] = hashlib.sha256(b''.join(len(b).to_bytes(8, 'little') + b for b in golden)).hexdigest()
                assert observed == golden, (role, 'golden mismatch')
                server.close_connections()
                usage_before = server.usage()
                epoch = [None]
                barrier = threading.Barrier(8, action=lambda: epoch.__setitem__(0, time.monotonic()), timeout=30)
                swap = {}
                stop = threading.Event()
                def reader(worker):
                    prng = random.Random(args.seed + round_index * 100 + worker)
                    reservoir = []
                    count = 0
                    counts = [0] * (int(args.duration) + 16)
                    query_counts = [0] * 20
                    barrier.wait()
                    while not stop.is_set():
                        began = time.monotonic() - epoch[0]
                        if began >= args.duration:
                            break
                        index = (worker + count * 7) % 20
                        status, body, ms = server.query(queries[index])
                        ended = time.monotonic() - epoch[0]
                        if status != 200 or body != golden[index]:
                            stop.set()
                            raise AssertionError((role, worker, index, status, body[:100]))
                        count += 1
                        query_counts[index] += 1
                        counts[min(int(ended), len(counts) - 1)] += 1
                        if began < 2:
                            continue
                        # A negative priority makes the largest retained priority the heap root.
                        item = (-prng.random(), worker, count, began, ended, index, ms)
                        if len(reservoir) < 10000:
                            heapq.heappush(reservoir, item)
                        elif item > reservoir[0]:
                            heapq.heapreplace(reservoir, item)
                    return {'count': count, 'seconds': counts, 'queries': query_counts, 'reservoir': reservoir}
                def reload():
                    barrier.wait()
                    stop.wait(max(0, 2 - (time.monotonic() - epoch[0])))
                    if stop.is_set():
                        return
                    swap['start_s'] = time.monotonic() - epoch[0]
                    status, body, ms = server.reload()
                    swap.update(end_s=time.monotonic() - epoch[0], status=status, ms=ms)
                    assert status == 200, (status, body[:100])
                with cf.ThreadPoolExecutor(max_workers=8) as pool:
                    futures = [pool.submit(reader, i) for i in range(7)]
                    reload_future = pool.submit(reload)
                    workers = [f.result() for f in futures]
                    reload_future.result()
                sample = heapq.nlargest(10000, (item for w in workers for item in w['reservoir']))
                rows = [{'worker': t[1], 'start_s': t[3], 'end_s': t[4], 'query_index': t[5], 'ms': t[6]} for t in sample]
                summary = a.summary([r['ms'] for r in rows if 2 <= r['start_s'] < args.duration])
                summary.pop('samples_ms')
                usage_after = server.usage()
                summary['cpu_seconds_per_request'] = (usage_after['cpu_seconds']-usage_before['cpu_seconds']) / sum(w['count'] for w in workers)
                seconds = [sum(w['seconds'][i] for w in workers) for i in range(len(workers[0]['seconds']))]
                record = {'round': round_index, 'role': role, 'variant': variant, 'artifact_identities': pair,
                          'reads': sum(w['count'] for w in workers), 'all_bodies_match': True, 'non200': 0, 'per_worker_reads': [w['count'] for w in workers],
                          'per_query_reads': [sum(w['queries'][i] for w in workers) for i in range(20)],
                          'successful_completions_per_second': seconds, 'swap': swap,
                          'reload_window_sampled_latency': summary, 'samples': rows,
                          'memory_events': (cg / 'memory.events').read_text(), 'usage_before': usage_before, 'usage_after': usage_after,
                          'reload_completed_within_fixed_window': swap['end_s'] < args.duration}
                result['runs'].append(record)
                save()
                assert 2 <= swap['start_s'] <= 2.1, 'Swap dispatch missed predefined 100ms scheduling allowance'
                assert record['reload_completed_within_fixed_window'], 'Reload exceeded fixed window; trial invalid, do not extend selectively'
                assert all(seconds[i] > 0 for i in range(1, int(args.duration))), 'Whole-second read progress gap'
                print(round_index, role, record['reads'], summary, 'swap_ms', swap['ms'], flush=True)
            finally:
                server.close()
    if args.formal:
        result['binary_sha256_after'] = binaries()
        assert result['binary_sha256_after'] == binary_hashes
    result['complete'] = True
    save()


if __name__ == '__main__':
    main()
