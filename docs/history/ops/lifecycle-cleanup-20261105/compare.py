import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import signal
import statistics
import time
import urllib.parse
import urllib.request

BINARY = 'ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d'
ORIGINS = {'https://typeahead-tantivy-a.fly.dev', 'https://typeahead-tantivy-b.fly.dev'}
QUERIES = [dict(q=q, limit=10) for q in ['zzstoatzz.io', 'jola.dev', 'pfrazee.com',
    'dholms.xyz', 'alice', 'nate', 'john', 'rebecca', '東京', 'atproto', 'jay', 'dan']]
QUERIES += [dict(q='zzstoatzz', limit=10, pds='https://pds.zzstoatzz.io')]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*_):
        raise ValueError('Unexpected search redirect')


def sample(origin, machine, build, query):
    request = urllib.request.Request(origin+'/search?'+urllib.parse.urlencode(query),
        headers={'User-Agent':'typeahead-lifecycle-qualification/1'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
        raw = response.read(1024**2+1)
        if response.status != 200 or len(raw) > 1024**2:
            raise ValueError('Search status or response bound failed')
        expected = {'X-Typeahead-Engine':'tantivy', 'X-Typeahead-Build':build,
            'X-Typeahead-Format':'tantivy-0.27-f32-did-expiry-v4',
            'X-Typeahead-Binary-Sha256':BINARY, 'X-Typeahead-Retrieval-Strategy':'global-merge',
            'X-Typeahead-Machine-Id':machine}
        if 'pds' in query:
            expected['X-Typeahead-Pds'] = urllib.parse.urlsplit(query['pds']).hostname
        if any(response.headers.get(key) != value for key,value in expected.items()):
            raise ValueError('Search identity or scope acknowledgment changed')
        if 'pds' not in query and response.headers.get('X-Typeahead-Pds',''):
            raise ValueError('Unexpected scope acknowledgment on a global search')
        version = response.headers.get('X-Typeahead-Machine-Version')
        if not version or response.headers.get('X-Typeahead-Failover') is not None:
            raise ValueError('Missing instance acknowledgment or unexpected fallback')
        timing = re.search(r'(?:^|,\s*)search;dur=([0-9]+(?:\.[0-9]+)?)',
            response.headers.get('Server-Timing',''))
        if timing is None or not math.isfinite(float(timing[1])):
            raise ValueError('Missing or invalid engine timing')
        body = json.loads(raw)
        if not isinstance(body,dict) or not isinstance(body.get('actors'),list):
            raise ValueError('Invalid search response shape')
        if query['q'] == 'zzstoatzz.io' and (not body['actors'] or
                body['actors'][0].get('handle') != 'zzstoatzz.io'):
            raise ValueError('Exact handle is not ranked first')
        return dict(engine_ms=float(timing[1]), body_sha256=hashlib.sha256(raw).hexdigest(),
            actor_count=len(body['actors']), machine_version=version)


def compare(targets, build, test_only):
    rows, pairs, next_at = [], [], dict.fromkeys(targets,0)
    try:
        for repeat in range(3):
            for index,query in enumerate(QUERIES):
                pair = []
                names = list(targets)
                if (repeat+index)%2:
                    names.reverse()
                for name in names:
                    time.sleep(max(0,next_at[name]-time.monotonic()))
                    started = time.monotonic()
                    next_at[name] = started+1
                    row = dict(target=name, query=query, warmup=repeat==0)
                    try:
                        row.update(sample(*targets[name],build,query),ok=True)
                    except Exception as error:
                        row.update(ok=False,error=dict(kind=type(error).__name__,
                            http_status=getattr(error,'code',None),
                            detail=str(error) if isinstance(error,ValueError) else None))
                    row['elapsed_ms'] = (time.monotonic()-started)*1000
                    rows.append(row);pair.append(row)
                    if not row['ok']:
                        break
                identical = (len(pair)==2 and all(row['ok'] for row in pair) and
                    pair[0]['body_sha256']==pair[1]['body_sha256'])
                pairs.append(dict(query=query,warmup=repeat==0,identical=identical))
                if not identical:
                    return result(rows,pairs,test_only)
    except (TimeoutError,KeyboardInterrupt) as error:
        report = result(rows,pairs,test_only)
        report.update(passed=False,aborted=type(error).__name__)
        return report
    return result(rows,pairs,test_only)


def result(rows,pairs,test_only):
    metrics = {}
    for name in ['baseline','candidate']:
        times = sorted(row['engine_ms'] for row in rows
            if row['target']==name and row.get('ok') and not row['warmup'])
        versions = {row['machine_version'] for row in rows
            if row['target']==name and row.get('ok')}
        metrics[name] = dict(samples=len(times), instance_consistent=len(versions)==1,
            median_ms=statistics.median(times) if times else None,
            sample_p95_ms=times[math.ceil(.95*len(times))-1] if times else None,
            max_ms=max(times) if times else None)
    a,b = metrics['baseline'],metrics['candidate']
    passed = (len(pairs)==39 and len(rows)==78 and all(pair['identical'] for pair in pairs)
        and all(m['samples']==26 and m['instance_consistent'] for m in metrics.values())
        and b['median_ms']<=a['median_ms']+2 and b['sample_p95_ms']<=a['sample_p95_ms']+5
        and b['sample_p95_ms']<=100)
    return dict(passed=passed,test_only=test_only,metrics=metrics,pairs=pairs,samples=rows,
        acceptance='All 39 paired bodies identical; 26 measured samples per target; '
            'candidate median <= baseline +2ms, nearest-rank sample p95 <= baseline +5ms and <=100ms.',
        limitations='Small warm-engine regression cohort; not a production percentile or sustained-load SLO.')


def main():
    parser = argparse.ArgumentParser()
    for name in ['baseline','candidate','baseline-machine','candidate-machine','build-id','output']:
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--test-loopback',action='store_true')
    args = parser.parse_args()
    targets = dict(baseline=(args.baseline,args.baseline_machine),
        candidate=(args.candidate,args.candidate_machine))
    if args.baseline == args.candidate or not re.fullmatch(r'b[0-9]{10}-[A-Za-z0-9_-]+',args.build_id):
        parser.error('Distinct targets and an exact shared build are required')
    for origin,machine in targets.values():
        parsed = urllib.parse.urlsplit(origin)
        allowed = origin in ORIGINS
        if args.test_loopback:
            allowed = (parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port
                and not parsed.username and not parsed.password and not parsed.path
                and not parsed.query and not parsed.fragment)
        if not allowed or not re.fullmatch(r'[a-f0-9]{14}',machine):
            parser.error('Only exact managed origins or explicitly marked loopback fixtures are allowed')
    def expired(*_):
        raise TimeoutError('Comparison execution budget expired')
    signal.signal(signal.SIGALRM,expired)
    with Path(args.output).open('x') as output:
        signal.alarm(300)
        try:
            report = compare(targets,args.build_id,args.test_loopback)
        finally:
            signal.alarm(0)
        report.update(at=time.time(),build_id=args.build_id,targets=targets)
        output.write(json.dumps(report,indent=2)+'\n')
    print(json.dumps({key:report[key] for key in ['passed','test_only','metrics']}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
