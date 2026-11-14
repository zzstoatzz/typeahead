"""Simulate tantivy LogMergePolicy on a stream of small commits that replace random live documents."""
import math, random, sys
def simulate(min_segments, L, clip, ratio, per_commit, commits, corpus=13_200_000, max_docs=10**12, seed=1):
    rnd = random.Random(seed)
    segs = [[corpus, 0]]  # [max_doc, deleted]
    rewritten = 0; counts = []; big = 0; ingested = 0
    for step in range(commits):
        # deletes: replaced docs chosen uniformly among live docs
        live = [s[0]-s[1] for s in segs]; total = sum(live)
        # distribute per_commit deletes proportionally (expected value, stochastic rounding)
        for i, s in enumerate(segs):
            x = per_commit * live[i] / total
            k = int(x) + (1 if rnd.random() < x - int(x) else 0)
            s[1] += min(k, s[0]-s[1])
        segs = [s for s in segs if s[0] > s[1]]
        segs.append([per_commit, 0]); ingested += per_commit
        while True:
            cand = sorted((s for s in segs if s[0]-s[1] <= max_docs), key=lambda s: -s[0])
            levels = []; cur = float('inf')
            for s in cand:
                lg = math.log2(max(clip, s[0]-s[1]))
                if lg < cur - L:
                    cur = lg; levels.append([])
                levels[-1].append(s)
            merged = False
            for lv in levels:
                if len(lv) >= min_segments or any(s[1]/s[0] > ratio for s in lv):
                    if len(lv) == 1 and lv[0][1] == 0: continue
                    n = sum(s[0]-s[1] for s in lv); rewritten += n
                    if n > 1_000_000: big += 1
                    ids = set(map(id, lv)); segs = [s for s in segs if id(s) not in ids]; segs.append([n, 0]); merged = True
            if not merged: break
        counts.append(len(segs))
    counts.sort()
    dead = sum(s[1] for s in segs); alld = sum(s[0] for s in segs)
    return dict(avg=sum(counts)/len(counts), p95=counts[int(len(counts)*.95)], max=counts[-1], amp=rewritten/ingested, big_merges=big, deleted_share=dead/alld,
                big=sorted((s[0] for s in segs), reverse=True)[:4])
days = 7
for name, cadence in (("5 s", 5), ("1.3 s", 1.3)):
    per = max(1, round(57_000/3600*cadence)); commits = int(days*86400/cadence)
    if cadence < 2: commits = int(2*86400/cadence)
    for (m, L, clip, r) in ((8, .75, 10000, 1.0), (8, .75, 10000, .2), (3, 3, 10000, .2), (3, 3, 1000, .2), (4, 2, 1000, .2), (3, 2, 1000, .2), (4, 3, 1000, .2)):
        out = simulate(m, L, clip, r, per, commits)
        d = commits*cadence/86400
        print(f"cadence {name:5s} docs/commit {per:3d} min={m} L={L} clip={clip:5d} ratio={r}: segments avg {out['avg']:.1f} p95 {out['p95']} max {out['max']}; rewrites/doc {out['amp']:.0f} = {out['amp']*per/cadence*40e-6*100:.1f}% of a core at 40us/doc; big merges/day {out['big_merges']/d:.1f}; deleted {out['deleted_share']*100:.0f}%; largest {out['big']}")
