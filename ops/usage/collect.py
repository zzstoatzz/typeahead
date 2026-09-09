#!/usr/bin/env python3
"""Read Cloudflare aggregate analytics once; no database queries or production writes."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
QUERY = '''query($a:string,$lo:string,$hi:string){viewer{accounts(filter:{accountTag:$a}){
 hourly:workersInvocationsAdaptive(limit:1000,filter:{scriptName:"typeahead",datetime_geq:$lo,datetime_lt:$hi}){
 dimensions{datetimeHour} avg{sampleInterval}
 sum{requests errors subrequests cpuTimeUs} quantiles{cpuTimeP50 cpuTimeP95 cpuTimeP99 wallTimeP95}}
 total:workersInvocationsAdaptive(limit:1,filter:{scriptName:"typeahead",datetime_geq:$lo,datetime_lt:$hi}){
 sum{requests errors subrequests cpuTimeUs} quantiles{cpuTimeP50 cpuTimeP95 cpuTimeP99 wallTimeP95}}
 outcomes:workersInvocationsAdaptive(limit:100,filter:{scriptName:"typeahead",datetime_geq:$lo,datetime_lt:$hi}){
 dimensions{status} sum{requests}}
}}}'''

def collect(hours):
    env = dict(line.split('=', 1) for line in (ROOT / '.env').read_text().splitlines()
               if '=' in line and not line.startswith('#'))
    token = os.environ.get('CLOUDFLARE_API_TOKEN') or env['CF_NEW'].strip('\"\'')
    # Exclude the incomplete hour and aggregation delay; comparable hourly buckets.
    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(hours=hours)
    body = {'query': QUERY, 'variables': {'a': '3e9ba01cd687b3c4d29033908177072e',
                                         'lo': start.isoformat(), 'hi': end.isoformat()}}
    req = urllib.request.Request('https://api.cloudflare.com/client/v4/graphql',
          data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + token,
                                                  'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.load(response)
    if result.get('errors'):
        raise RuntimeError(result['errors'])
    account = result['data']['viewer']['accounts'][0]
    if not account['total']:
        raise RuntimeError('No analytics returned; do not display unavailable data as zero.')
    account['hourly'].sort(key=lambda row: row['dimensions']['datetimeHour'])
    return {'start': start.isoformat(), 'end': end.isoformat(),
            'collected': dt.datetime.now(dt.timezone.utc).isoformat(), 'cloudflare': account}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=int, choices=(24, 48, 72, 168), default=24)
    parser.add_argument('--output', type=Path, default=Path('/tmp/typeahead-usage.json'))
    args = parser.parse_args()
    result = collect(args.hours)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({'saved': str(args.output), 'start': result['start'], 'end': result['end'],
                      'totals': result['cloudflare']['total']}))
