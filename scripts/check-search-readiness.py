#!/usr/bin/env python3
import argparse
import json
import time
import urllib.parse
import urllib.request


def check(url, build, max_lag, max_snapshot_age=None):
    def get(path):
        with urllib.request.urlopen(url.rstrip('/') + path, timeout=10) as response:
            return json.load(response)

    health = get('/health')
    index = get('/index/status')
    search = get('/search?' + urllib.parse.urlencode({'q': 'zzstoatzz.io', 'limit': 5}))
    failures = []
    if health.get('ready') is not True or index.get('attached') is not True:
        failures.append('snapshot is not attached and ready')
    if index.get('build_id') != build:
        failures.append('unexpected build')
    lag = health.get('sync_lag_s')
    if not isinstance(lag, (int, float)) or not 0 <= lag <= max_lag:
        failures.append('overlay sync is unknown or too old')
    if not isinstance(index.get('actor_count'), int) or index['actor_count'] <= 0:
        failures.append('empty or unknown corpus')
    watermark = index.get('source_watermark')
    snapshot_age = time.time() - watermark if isinstance(watermark, (int, float)) else None
    if max_snapshot_age is not None and (snapshot_age is None or not 0 <= snapshot_age < max_snapshot_age):
        failures.append('snapshot is outside the fresh-restore replay window')
    actors = search.get('actors', [])
    if not actors or actors[0].get('handle') != 'zzstoatzz.io':
        failures.append('exact-handle probe is not ranked first')
    return {'ready': not failures, 'failures': failures, 'build_id': index.get('build_id'),
            'sync_lag_s': lag, 'actor_count': index.get('actor_count'),
            'snapshot_age_s': snapshot_age}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Gate a prepared backend before registering production routing.')
    parser.add_argument('url')
    parser.add_argument('--build', required=True)
    parser.add_argument('--max-lag', type=int, default=600)
    parser.add_argument('--max-snapshot-age', type=int,
                        help='Fresh restore only: require watermark younger than this many seconds (at most 604800).')
    args = parser.parse_args()
    if args.max_snapshot_age is not None and not 0 < args.max_snapshot_age <= 604800:
        parser.error('--max-snapshot-age must be between 1 and 604800')
    try:
        result = check(args.url, args.build, args.max_lag, args.max_snapshot_age)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {'ready': False, 'failures': [str(error)]}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['ready'] else 1)
