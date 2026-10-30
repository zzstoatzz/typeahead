import json
import math
from pathlib import Path
import sys


def percentile(values,q):
    v=sorted(values)
    return v[max(0,math.ceil(len(v)*q)-1)] if v else None


def summarize(root):
    groups={}
    for p in root.glob('[0-3]-*-*.json'):
        r=json.loads(p.read_text())
        if 'rows' not in r:continue
        _,variant,phase=p.stem.split('-',2)
        g=groups.setdefault((phase,variant),{'rows':[],'runs':[]})
        g['rows'].extend(r['rows']);g['runs'].append({k:v for k,v in r.items() if k!='rows'})
    out=[]
    for (phase,variant),g in sorted(groups.items()):
        rows=g['rows'];times=[r['elapsed_ms'] for r in rows];service=[r['service_ms'] for r in rows]
        out.append({'phase':phase,'variant':variant,'attempts':len(rows),'p50_ms':percentile(times,.5),'p95_ms':percentile(times,.95),'p99_ms':percentile(times,.99),'max_ms':max(times),'service_p99_ms':percentile(service,.99),'truncated':sum(r['truncated'] for r in rows),'over_1s':sum(t>1000 for t in times),'over_10s':sum(t>10000 for t in times),'peak_rss_mib':max(int(line.split(':')[1].split()[0])/1024 for run in g['runs'] for line in run['memory'] if line.startswith('VmHWM:')),'runs':g['runs']})
    return out


if __name__=='__main__':
    root=Path(sys.argv[1]);result=summarize(root)
    (root/'aggregate.json').write_text(json.dumps(result,indent=2)+'\n')
    print('| Phase | Variant | Attempts | p50 ms | p95 ms | p99 ms | Truncated |')
    print('| --- | --- | ---: | ---: | ---: | ---: | ---: |')
    for r in result:
        print(f"| {r['phase']} | {r['variant']} | {r['attempts']} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} | {r['p99_ms']:.2f} | {r['truncated']} |")
    input_data=json.loads((root/'input.json').read_text())
    cases={c['id']:c for c in json.loads((root/'cases.json').read_text())}
    quality={}
    for variant in ['sorted','fst']:
        checks=[json.loads(line) for line in (root/(variant+'-check.jsonl')).read_text().splitlines()]
        stats={}
        for row in checks:
            case=cases[row['case']];kind=case['id'].split('-')[0]
            s=stats.setdefault(kind,{'cases':0,'truncated':0,'intended_handle_in_top10':0,'actual_typos':0,'typos_recovered':0})
            s['cases']+=1;s['truncated']+=int(row['answer']['truncated'])
            if kind in ['exact','fuzzy','scope']:
                index=int(case['id'].split('-')[1]);handle=input_data['samples'][index]['handle']
                found=any(h['handle']==handle for h in row['answer']['hits'])
                s['intended_handle_in_top10']+=int(found)
                if kind=='fuzzy' and case['q']!=handle[:len(case['q'])]:
                    s['actual_typos']+=1;s['typos_recovered']+=int(found)
        quality[variant]=stats
    sync=[]
    for r in json.loads((root/'summary.json').read_text()):
        if 'writer' in r:
            sync.append({'tag':r['tag'],'wall_s':r['wall_s'],**r['writer'],'rows_per_second':r['writer']['rows']/r['wall_s']})
    (root/'sync.json').write_text(json.dumps(sync,indent=2)+'\n')
    (root/'recall.json').write_text(json.dumps(quality,indent=2)+'\n')
    print(json.dumps(quality,indent=2))
