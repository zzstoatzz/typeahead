#!/usr/bin/env python3
"""Bounded, read-only health observation for PDS repair qualification."""
import argparse
import datetime
import json
import math
import time
import urllib.request
import urllib.error
from pathlib import Path

ENDPOINTS = {
    'search': 'https://typeahead-search-compact.fly.dev/health',
    'ingest': 'https://typeahead-ingester.fly.dev/health',
}


def fetch_health(url):
    start = time.monotonic()
    with urllib.request.urlopen(url, timeout=10) as response:
        if response.status != 200:
            raise ValueError('Health HTTP status is not 200')
        data = json.load(response)
    return {'body': data, 'seconds': time.monotonic() - start}


def error_details(error):
    result={'type':type(error).__name__,'detail':str(error)}
    if isinstance(error,urllib.error.HTTPError):result['status']=error.code
    if isinstance(error,urllib.error.URLError):result['reason']=str(error.reason)
    return result


def search_lag(sample, now):
    if 'search_progress' not in sample:
        return sample['search']['body']['sync_lag_s']
    proof=sample['search_progress']
    from pds_fly_progress import EXPECTED, timestamp
    identity=proof['identity']
    if ({key:identity[key] for key in EXPECTED}!=EXPECTED
            or proof['source']!='fly-machine-completed-sync-log'
            or not 0<=now-proof['observed_at']<=20
            or not timestamp(identity['updated_at'])<=proof['completed_at']<=proof['observed_at']):
        raise ValueError('Invalid completed sync proof')
    return now-proof['completed_at']


def blockers(sample, previous=None, *, max_queue, max_write_age, max_search_lag,
             max_probe_age=20, now=None):
    reasons = []
    now = time.time() if now is None else now
    if not isinstance(sample.get('at'), (int, float)) or not 0 <= now-sample['at'] <= max_probe_age:
        return ['stale or missing health observation']
    if sample.get('errors'):
        return ['health endpoint unavailable']
    try:
        ingest = sample['ingest']['body']
        search = sample['search']['body']
        counters = ingest['ingest']
        identities = ingest['identity']
        for value in [counters[k] for k in ('queued','deletes_queued','backpressure_count','last_write_at','source_event_at')] + [identities[k] for k in ('dropped','post_failed','processed')] + [search['sync_lag_s']]:
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0:
                return ['invalid health telemetry']
        if ingest['status'] != 'ok' or ingest['role'] != 'ingester':
            reasons.append('ingester unhealthy')
        if search['status'] != 'ok' or search['ready'] is not True:
            reasons.append('search not ready')
        if counters['backpressure_active'] is not False:
            reasons.append('ingest backpressure')
        if counters['queued'] > max_queue or counters['deletes_queued'] > max_queue:
            reasons.append('ingest queue exceeds budget')
        if not 0 <= now-counters['last_write_at'] <= max_write_age:
            reasons.append('ingest successful write is stale')
        if not 0 <= now-counters['source_event_at'] <= max_write_age:
            reasons.append('ingest source event is stale')
        if not 0 <= search_lag(sample,now) <= max_search_lag:
            reasons.append('search catch-up exceeds budget')
        if previous is not None:
            if ('search_progress' in previous)!=('search_progress' in sample):
                reasons.append('search freshness evidence changed')
            elif 'search_progress' in sample and sample['search_progress']['identity']!=previous['search_progress']['identity']:
                reasons.append('search generation changed')
            old = previous['ingest']['body']
            for container, key in [('identity','dropped'), ('identity','post_failed'), ('ingest','backpressure_count')]:
                if ingest[container][key] != old[container][key]:
                    reasons.append(f'{container}.{key} changed or reset')
            if identities['processed'] < old['identity']['processed']:
                reasons.append('ingester restarted')
    except (KeyError,TypeError,ValueError):
        reasons.append('incomplete health telemetry')
    return reasons


def observe(output, samples, interval, fly_progress=False):
    if samples < 1 or not 10 <= interval <= 300:
        raise ValueError('Use positive samples and interval between 10 and 300 seconds')
    with Path(output).open('x') as stream:
        for index in range(samples):
            sample = {'at': time.time(), 'index': index, 'errors': {}}
            if fly_progress:
                try:
                    from pds_fly_progress import fetch_progress
                    sample['search_progress']=fetch_progress()
                except Exception as error:
                    sample['errors']['search_progress']=error_details(error)
            for name,url in ENDPOINTS.items():
                try:
                    sample[name] = fetch_health(url)
                except Exception as error:
                    sample['errors'][name] = error_details(error)
            sample['finished_at']=time.time()
            stream.write(json.dumps(sample)+'\n');stream.flush()
            print(json.dumps({'index': index, 'at': datetime.datetime.fromtimestamp(sample['at'],datetime.timezone.utc).isoformat(), 'errors': sample['errors']}),flush=True)
            if index + 1 < samples:
                time.sleep(interval)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--samples',type=int,default=80)
    parser.add_argument('--interval',type=float,default=15)
    parser.add_argument('--fly-progress',action='store_true')
    args=parser.parse_args();observe(args.output,args.samples,args.interval,args.fly_progress)
