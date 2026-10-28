#!/usr/bin/env python3
"""Small algorithm counterexamples, not an implementation or performance qualification."""
import argparse
import json
from pathlib import Path


def rank(rows):
    return sorted(rows, key=lambda r: (-r['score'], r['did']))


def matches(row, query):
    words = query.lower().split()
    tokens = row['display'].lower().split()
    return all(any(t.startswith(w) for t in tokens) for w in words)


def predict(rows, query, method, multiplier=1):
    rows = [r for r in rows if not r.get('hidden')]
    words = query.lower().split()
    lists = []
    for word in words:
        hits = rank([r for r in rows if any(t.startswith(word) for t in r['display'].lower().split())])
        cap = (300 if len(word) <= 3 else 200 if len(word) <= 6 else 100) * multiplier
        lists.append(hits if method == 'complete-postings' else hits[:cap])
    existing = set.intersection(*(set(r['did'] for r in a) for a in lists))
    selected = rank([r for r in rows if r['did'] in existing])[:50]
    if method == 'union-verify':
        union = set(r['did'] for a in lists for r in a)
        selected = [r for r in rank([r for r in rows if r['did'] in union])[:50] if matches(r,query)]
    elif method == 'rarest-seed-verify':
        selected = [r for r in min(lists,key=len)[:50] if matches(r,query)]
    elif method == 'phrase-fast-path':
        phrase = [r for r in rows if r['display'].lower().startswith(query.lower())]
        selected = rank({r['did']:r for r in selected+phrase}.values())[:50]
    elif method == 'stripped-handle-fallback':
        handles = [r for r in rows if r['handle'].startswith(''.join(words))]
        selected = rank({r['did']:r for r in selected+handles}.values())[:50]
    return [r['did'] for r in selected[:10]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('actors',type=Path)
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    assert not args.output.exists()
    rows = json.loads(args.actors.read_text())
    methods = ['current','doubled-caps','union-verify','rarest-seed-verify','phrase-fast-path','stripped-handle-fallback','complete-postings']
    queries = ['Justin Fa','Fa Justin','Justin Fagnani','Justin Falconer','Fagnani Justin']
    result = {'kind':'offline-algorithm-controls','scope':'ASCII multiword fixture only; no native implementation or production scale claim','queries':[]}
    for q in queries:
        oracle = [r['did'] for r in rank([r for r in rows if not r['hidden'] and matches(r,q)])[:10]]
        answers = {m:predict(rows,q,m,multiplier=2 if m=='doubled-caps' else 1) for m in methods}
        result['queries'].append({'query':q,'oracle':oracle,'methods':{m:{'dids':a,'matches_oracle':a==oracle} for m,a in answers.items()}})
    cases=[]
    for cap in [300,600,1200]:
        actors=[{'did':f'did:decoy:{i:05}','handle':f'person{i}.test','display':f'Other{i} Falcon{i}','score':1000+i,'hidden':False} for i in range(cap+1)]
        actors += [{'did':'did:target','handle':'unrelated.test','display':'Justin Fagnani','score':1,'hidden':False}]
        answer=predict(actors,'Justin Fa','current',multiplier=cap//300)
        cases.append({'cap':cap,'actors':len(actors),'expected':['did:target'],'actual':answer,'miss':not answer})
    result['finite_cap_counterexamples']=cases
    result['hard_budget_counterexample']={'description':'Two broad terms have interleaved disjoint high-score actors; their only joint match has the lowest score. A ranked seed budget before joint verification misses it.', 'sizes':[]}
    for size in [100,1000,10000]:
        actors=[]
        for i in range(size):
            actors.append({'did':f'did:actor:{i:05}','handle':f'person{i}.test','display':'Anchor Else' if i%2 else 'Other Tail','score':10000+i,'hidden':False})
        actors.append({'did':'did:joint','handle':'unrelated.test','display':'Anchor Tail','score':1,'hidden':False})
        result['hard_budget_counterexample']['sizes'].append({'actors':len(actors),'joint_matches':1,'seed50':predict(actors,'anchor tail','rarest-seed-verify'),'complete_postings':predict(actors,'anchor tail','complete-postings')})
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'queries':len(queries),'finite_caps_rejected':all(c['miss'] for c in cases)}))


if __name__=='__main__':
    main()
