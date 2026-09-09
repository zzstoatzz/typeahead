#!/usr/bin/env python3
"""Sequential offline comparison; Linux child memory/CPU and wall-time limits.

The input is a JSON array of query strings. Never starts a sync loop or sends
requests to production. A timeout, process failure, or mismatch fails the run.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import re
import subprocess
import sys
import time


def compare_query(binary, overlay, snapshot, query):
    env = {'PATH': '/usr/bin:/bin',
           'TYPEAHEAD_EVAL_OVERLAY': str(overlay.resolve()),
           'TYPEAHEAD_EVAL_SNAPSHOT': str(snapshot.resolve()),
           'TYPEAHEAD_EVAL_QUERY': query}
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("limit_exec.py")), str(binary.resolve())], env=env, capture_output=True,
            text=True, timeout=15,
        )
        try:
            result = json.loads(process.stdout)
        except (ValueError, TypeError):
            result = {'error': 'invalid_output'}
        metrics = ('baseline_us', 'candidate_us', 'actors', 'baseline_overlay_rows', 'candidate_overlay_rows')
        if (not isinstance(result, dict) or type(result.get('equal')) is not bool
                or any(type(result.get(k)) is not int or result[k] < 0 for k in metrics)):
            result = {'error': 'invalid_output'}
        if process.returncode != 0:
            error = re.search(r'^error: ([A-Za-z][A-Za-z0-9_]*)', process.stderr, re.MULTILINE)
            result['child_error'] = error.group(1) if error else 'unclassified'
        result['returncode'] = process.returncode
        result['passed'] = process.returncode == 0 and result.get('equal') is True
    except subprocess.TimeoutExpired:
        result = {'passed': False, 'error': 'wall_timeout'}
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    result['child_cpu_s'] = (usage_after.ru_utime + usage_after.ru_stime
                             - usage_before.ru_utime - usage_before.ru_stime)
    result['wall_s'] = time.monotonic() - started
    # Linux reports KiB; this is the run-wide high-water mark, not per-query RSS.
    result['run_peak_child_rss_kib'] = usage_after.ru_maxrss
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--overlay', required=True, type=Path)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--queries', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('Run on Linux so the resource limits have the tested semantics')
    queries = json.loads(args.queries.read_text())
    if not isinstance(queries, list) or not queries or len(queries) > 1000:
        parser.error('Expected between 1 and 1000 query strings')
    if any(not isinstance(q, str) or len(q.encode()) > 256 or '\0' in q for q in queries):
        parser.error('Queries must be strings of at most 256 UTF-8 bytes, without NUL')
    for path in (args.binary, args.overlay, args.snapshot):
        if not path.is_file():
            parser.error(f'Missing input: {path}')
    results = []
    for query in queries:
        result = compare_query(args.binary, args.overlay, args.snapshot, query)
        result['query'] = query
        results.append(result)
        # Preserve progress if a later case is interrupted. No actor data output.
        args.output.write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
