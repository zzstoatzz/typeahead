# Isolated retrieval comparison results

2026-10-28. Local synthetic experiments only. No production credentials, service
requests, configuration changes, deployments, corpus copies or host cache flush.
The [recommendation](recommendation.md) distinguishes the proposed fix from
these disposable prototypes. [Machine-readable summary](comparison-summary.json).

## Native comparison

Authoritative run: [native-comparison-2/results.json.gz](native-comparison-2/results.json.gz).
The same native builder, SQLite library, loopback evaluation HTTP server, packed
profiles, data and diagnostic endpoint are used for all three variants. Runtime
patches are applied only to disposable copies of committed source. Repository
runtime source is unchanged. The scripts retain source/patch hashes and build
logs. [Preparation](../../../experiments/recall-comparison/prepare.py),
[orchestrator](../../../experiments/recall-comparison/run.py).

- Baseline: current tiered per-prefix caps, 16-byte prefixes and merge/hydrate 50.
- Refill200: same stored membership; merge up to 200 and hydrate in 50-entry
  batches, rechecking PDS/long queries until 50 eligible candidates or exhaustion.
  Its trace distinguishes exhausting that candidate budget. It does not bound
  all posting/overlay reads.
- Complete control: full fixture membership (`INDEX_TOP_N_*=65535`), prefix max
  253, conjunction before selection and eligibility refill. This is deliberately
  a simple DID-string/hash-map implementation. All corpora are below the u16
  posting ceiling. It retains the old tokenizer and is **not** a complete CJK or
  token-limit fix, production encoding or capacity claim.

Twenty independent ASCII oracle queries include common/reversed display words,
handle/domain matching, exact duplicate handles, long prefixes, a display-only
actor, misses, and broad shared terms. Results compare ordered top-10 DIDs against
full effective actor state and stored authority ranking, not baseline answers.
The same cases run unscoped, in the eligible scope, after visibility/deletion
changes and during native projection. Hidden/tombstone states are installed
explicitly in the real overlay tables; this is not a full lifecycle-event or
operator-precedence qualification. Native projection and writer commits use the
real `sync.projectRow`, with 100 unchanged-content actors and version updates.

| Actors | Variant | Warm oracle | After hide/delete | Snapshot bytes | Warm engine p99 | Mixed engine p99 | RSS after mixed |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | Baseline | 15/20 | 12/20 | 3,043,328 | 0.473 ms | 0.724 ms | 20.1 MB |
| 1,000 | Refill200 | 17/20 | 15/20 | 3,043,328 | 0.457 ms | 0.757 ms | 20.4 MB |
| 1,000 | Complete control | 20/20 | 20/20 | 4,542,464 | 0.589 ms | 1.283 ms | 21.7 MB |
| 10,000 | Baseline | 15/20 | 12/20 | 10,162,176 | 0.461 ms | 0.767 ms | 24.6 MB |
| 10,000 | Refill200 | 17/20 | 15/20 | 10,162,176 | 0.491 ms | 0.744 ms | 24.6 MB |
| 10,000 | Complete control | 20/20 | 20/20 | 57,688,064 | 17.613 ms | 24.464 ms | 63.5 MB |

These figures are local ARM Mac observations, not 2 CPU / 1 GiB Fly estimates.
All attempted requests and errors are retained in the compressed JSON archive; the final run has zero request
errors. These are deliberately adversarial cases, not a production recall rate.
Scoped cases have the same correctness counts. During the mixed phase native
reprojection restores the two Justin matches in baseline/refill, reproducing the
already established dependence on which updates remain in the overlay. Complete
control remains 20/20 in every phase.

The full control repairs the ASCII cases but expands the 10k fixture from
54,734 keys / 167,902 memberships to 298,898 keys / 1,361,066 memberships. Both
longer keys and complete membership contribute; this is not a dictionary-only
size comparison. `common member` returns 400 stored posting entries in baseline
and 18,020 in complete control. Both decode 50 profiles, despite returning only
ten. Fewer profile decodes alone would not eliminate its membership/hash/sort
work. The earlier complete-membership-only control isolates cap removal without
changing prefix length; see [that comparison](native-control-comparison.json).

Mixed phases run for at least three seconds with a single closed-loop query
client and an independent real native writer. A 100-row batch is offered every
10 ms, intentionally faster than the writer can sustain. At 10k, baseline and
refill each commit 32 batches versus 24 for complete control. Their observed
SQLite row-change deltas are 148,544 / 148,544 / 128,112 respectively; the last
number covers fewer completed batches. Complete control changes more rows per
batch. No writer reports failure, but all accumulate approximately 2.7 seconds
of replay scheduling lag. This is evidence of offered overload, **not healthy
production freshness** or sustained qualification. Versions here are synthetic
ordering markers; writer lag measures its replay schedule, not live source age.

Counters span the query phase and evidence persistence before the final identity
read. RSS is a sample after a phase, not a process peak or file-cache accounting.
Requests are closed-loop, so arms complete different query counts; this is not
a fixed-offered-QPS saturation comparison. Engine time covers lookup execution;
HTTP timing includes the diagnostic handler and JSON. It is not public Worker
latency. Warm ordering is deterministic and initial first-pass timings are
process-cold only; the host's file cache is not evicted.

## Static alternative controls

[Authoritative results](static-plan-controls-3/results.json),
[script](../../../experiments/recall-comparison/alternatives.py).
These are Python drivers over native SQLite FTS5 and resident Python integer
arrays, **not** equivalent native HTTP implementations or mutable engine trials.
Their seed excludes non-ASCII actors before filling to the stated size, so
the background population differs slightly from the native pilot. They cover
19 ASCII queries at 1k/10k/30k, including visibility/deletion state,
reversed words, long prefixes, exact promotion and DID ties. Both pass all 19
at each size. Separate CJK, normalization, parser and lifecycle gates still apply.

The FTS plan has complete word-prefix matching, a field predicate before final
selection, current visibility before LIMIT, stored authority ordering and late
profile lookup. MATCH is assembled from quoted terms. Its plan uses FTS rowids,
metadata primary-key lookups and a temporary B-tree for ORDER BY. Its Python
predicate callback and simplified two-string profile payload prohibit comparing
absolute latency, memory or total storage directly with the native service.
It has no mutable sync benchmark. It is a plan control, not a proposal to revert
to the previous FTS implementation.

The rank-ID control stores complete full-prefix lists in authority/DID order,
then unions or seeks/intersects IDs and rejects current ineligible actors before
selecting ten. It avoids copying/decoding the entire lists at query time. This
proves one possible access pattern on these fixtures; it does not implement a
native compact dictionary, score-changing delta, production PDS policy or online
compaction. Its uncompressed wire count includes keys, lengths and u32 IDs but
excludes profiles/rank/eligibility metadata and Python container overhead; it is
not measured total native RAM or a production artifact estimate.

| Actors | `common member` FTS predicate rows | Approximate SQL VM steps | Rank-ID iterations/seeks | Rank-ID wire bytes, all keys |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 16 | 400 | 20 | 490,985 |
| 10,000 | 9,016 | 180,400 | 20 | 6,838,598 |
| 30,000 | 29,016 | 580,900 | 20 | 20,941,458 |

The counters measure different work units; they are not a speedup ratio. The
useful finding is that authority-sorting the FTS match set grows with this broad
population, whereas rank-ordered complete lists can stop after ten eligible
joint matches here. Adversarial low-ranked joint matches still require further
seeking; no constant-work or unconditional latency claim follows. This supports
prototyping complete compressed memberships plus ranked enumeration, instead of
simply raising caps or sorting all matches.

## Reproduction, failures and cleanup

```sh
python3 experiments/recall-comparison/run.py /absolute/new/native-evidence
python3 experiments/recall-comparison/alternatives.py /absolute/new/static-evidence
python3 experiments/recall-comparison/summarize.py \
  docs/evals/display-name-recall-20261028
```

Owner: `display-name-recall-20261028`. Native scratch has a 4 GiB budget,
30-minute wall limit and 2 GiB free reserve; static controls use 512 MiB,
five minutes and 1 GiB free reserve. Temporary trees and process groups have
finally-block teardown. Compiler/global dependency caches are separate from
scratch payload and are not globally pruned. Servers use credential-free
runtime environments and loopback-only binding; the indexer publishes only to
local scratch (`INDEX_CHANNEL=local`).

The summary checks every owned scratch path is absent and every native child
terminal. Both native runs and all three static runs are retired. Native run 1
is superseded because its workload omitted the broad filler terms and its mixed
phase was too short. Static run 1 had a SQL-placeholder harness error; static run
2 exposed a case-normalization bug in the experimental rank-ID driver. Run 3
fixes that driver bug. Failed/superseded evidence is retained, not counted as
passing final trials. No full fixture databases, indexes, binaries or serving
processes remain. See the receipts and SHA-256 values in the summary.
