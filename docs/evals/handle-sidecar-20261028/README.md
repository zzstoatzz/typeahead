# Handle sidecar evaluation

2026-10-28 — isolated prototype complete; not deployed. **Full-prefix matching
repairs the selective handle-recall failure with either index. The FST makes
one-edit lookup substantially cheaper in this layout, but neither prototype
qualifies the storage/performance issue's release target.**

The code is in [experiments/handle-sidecar](../../../experiments/handle-sidecar/README.md).
The [raw result archive](results.tgz) is 519,009 bytes, SHA-256
`29c518549a4276b880bd7f762ffc101806f727753027f9dc834449a3064f7318`.
Small manifests and derived summaries are alongside this document.

## Input and method

A new private Fly Machine used an independent fork of the inactive slot's disk,
in a different hardware zone from the managed backends. It had two shared CPUs,
1 GiB RAM and no swap or public services. No corpus passed through heavypad or
the laptop. Original backend instances, configurations and the route were read
before/after and match exactly; see [safety.json](safety.json).

The frozen effective input contains **12,136,964 valid, non-hidden handle rows**,
after applying an overlay with **181,876 actor rows / 7,495,360 prefix rows**.
690,177 rows were excluded for hidden, invalid or unsupported handle forms;
the export did not retain a reason-level breakdown. These counts do not measure
network coverage. Source snapshot: `b1793081565-0228-px05`. Input identities are
in [input.json](input.json), [snapshot-manifest.json](snapshot-manifest.json) and
[feed-manifest.json](feed-manifest.json).

Both implementations share packed actor records containing full handle, DID,
f32 score, PDS and direct profile locations. Sorted lookup uses a u64 ordinal
array; the FST maps each unique handle to the first record byte offset, without
loading that array. Duplicate handles preserve all DIDs. Matching happens before
quality ranking, complete exact-handle promotion and a 50-profile hydration cap.
Candidate enumeration has a separate 20,000-row budget and reports truncation.

Sorted fuzzy lookup generates insertion/deletion/substitution/transposition
variants and binary-searches their prefix ranges. FST fuzzy lookup intersects
with a distance-one prefix DFA. Both use a shared precomputed automaton builder.
Fuzzy mode is explicit for measurement; the proposed public fallback policy is
not implemented.

Runs used sorted/FST/FST/sorted order on the same frozen files. There were four
query workers. Exact warm phases offered 40 requests/s; other phases offered 20.
Scheduling backlog remains in latency. Every attempt completed and remained in
the archive; computations were not canceled at an HTTP deadline. Advisory file
eviction preceded cache-reset phases; this is not proof that every cache layer
was cold. Query sampling is systematic and is not a production traffic trace.

## Recall and typo matching

All **337 case answers match**, including ranked DIDs and profile checksums.
**285 cases completed enumeration; 52 hit the traversal budget.** Matching two
partial answers does not prove complete recall. See [correctness.json](correctness.json).

Independent fixture oracles cover all four edit types, multiple DIDs sharing a handle,
exact-handle priority, PDS filtering, profile locations, overlay renames,
tombstones and hidden/invalid actors. A fixture with 1,002 handles sharing their
first 16 bytes reproduces the existing selection order returning no long-prefix
match; both full-handle indexes recover its unique low-scoring actor.

All **155 genuinely edited handle queries** recovered the original handle string
in their top ten (131 distinct handle/query pairs). Five additional generated
transpositions were no-ops. Intended DIDs were not saved in the sampling manifest,
so this is **handle-string recovery, not intended-actor recall**. See
[recall.json](recall.json) and [data-quality.json](data-quality.json).

The frozen data has a material anomaly: `donald.trump.com` appears in 92 of 638
systematic samples and in 25 of the 160 generated-query source samples, under
many distinct DIDs. Those repeated queries hit the candidate budget. Both indexes
retain the claims; neither verifies their ownership. This makes the workload
partly a duplicate-handle stress test and prevents treating it as a clean verified
identity-directory oracle. No production diagnosis or identity repair was done.

## Latency

Pooled results from two runs per variant, **median / p99 in milliseconds**, with
profile reads and client scheduling delay included. They are standalone prototype
timings, not public API or deployed native-engine latency.

| Phase | Attempts per variant | Sorted ms | FST ms |
| --- | ---: | ---: | ---: |
| Cache-reset exact | 320 | 758.26 / 1413.87 | 0.78 / 156.29 |
| Warm exact | 480 | 0.22 / 7.10 | 0.18 / 10.13 |
| Varied one-edit queries | 320 | 27817.62 / 29817.45 | 2.05 / 1236.46 |
| Exact with native sync | 1280 | 0.14 / 10.11 | 0.13 / 118.81 |
| One-edit queries with native sync | 1280 | 31133.86 / 40344.65 | 1.72 / 3379.82 |

All 7,360 timed attempts matched their variant's frozen reference answer.
For mixed typo queries, sorted had **1,271/1,280 answers beyond ten seconds**;
FST had zero beyond ten seconds but **149/1,280 beyond one second**. These late
answers are retained, not counted as successful low-latency responses. See
[aggregate.json](aggregate.json), [summary.json](summary.json), and the archive.

Queueing is a large part of the sorted typo result. Among untruncated mixed
queries, service-time median / p99 was **3.49 / 7,268 ms** for sorted and
**1.52 / 995 ms** for FST. This diagnostic subset does not replace the full
failure-inclusive workload. See [complete-case-diagnostics.json](complete-case-diagnostics.json).
Exact lookup is similar once warm; these results do not support preferring FST
for warm exact-query tails.

## Memory and disk

| Component | Sorted | FST |
| --- | ---: | ---: |
| Common actor records | 1,022,673,236 B | 1,022,673,236 B |
| Lookup structure | 97,095,712 B | 110,479,612 B |
| Peak process RSS, varied typo phase | 639.8 MiB | 122.8 MiB |
| Peak process RSS, mixed typo phase | 606.2 MiB | 123.1 MiB |

The FST costs 13,383,900 additional bytes on disk in this representation, while
its smaller touched working set avoids many sorted-probe page accesses. RSS
includes mapped pages and excludes unmapped profile-page cache. The complete VM,
including kernel/file cache and native writer, remained constrained to 1 GiB.
[storage.json](storage.json) and per-run memory fields retain the measurements.

This is not a best-possible-layout comparison: sorted lookup probes common actor
records rather than a key-only pool, offsets are u64, and FST records still
contain duplicate handle strings. A compact key-only sorted control and a compact
FST metadata layout remain worthwhile before selecting a production encoding.

## Mixed sync and limits

The retained native evaluator is source `62266a3`, binary SHA-256
`58f3ac6207cb936853ba73db93def7120e6091355bc2ed7696f80f68579c3cdf`.
It replays 5,000 captured actors in real 500-row SQLite projection/commit batches,
with changing timestamps and unchanged profile content. All existing prefix
writes, automatic checkpoint behavior and the native full-pass checkpoint remain.

In each 32-second FST mixed-typo window only **one batch** completed, about
15.65 actor updates/s. Each exact mixed window completed **two batches**,
about 31.3 updates/s, for both variants. **Neither sorted mixed-typo run completed
a batch** before its query workload drained. No completed writer batch reported
failure; a writer remaining active is not evidence that it committed successfully.
See [sync.json](sync.json) for batch counts, actual elapsed time and replay lag.

Thus FST substantially reduces typo lookup pressure, while sync throughput remains
poor. These short windows do not meet the issue's sustained 30-batch gate. Replay
lag is relative to the offered synthetic replay cadence, not live freshness.
The sidecars are frozen views: atomic mutable-sidecar publication, new identities,
renames during live requests and rebuilding costs are not performance-qualified.

Profile reads use exported direct offsets and bypass the serving map's block
admission checks and HTTP encoding. No write-amplification savings were implemented
or measured. Display-name intersections, domain-suffix recall, eligible-PDS policy
and public alias/acknowledgment contracts remain outside this standalone program.
The traversal cap can run out before scoped matches are found; this is not a
replacement for the public PDS contract.

## Decision

Use full-query matching before candidate selection to address selective handle
recall; FST is not required for that fix. FST is promising as the typo candidate
source, given the latency and working-set difference observed here. Before
integration, compare compact key-only layouts, add ranked range retrieval or a
strict fallback for broad prefixes, and define atomic snapshot/overlay ownership.
The duplicate handle claims need separate identity-data investigation.

This prototype does not resolve yesterday's sync/storage incident and is not a
release candidate. The public API, Worker and native serving code are unchanged.
README only gains the experiment link; no public endpoint documentation changes
are required for this isolated candidate-source evaluation.

## Cleanup

Owner: `handle-sidecar-20261028`; Machine `2870d15f795d28`, independent volume
`vol_re1ql0132o3qxw34`. Payload budget was 16 GiB on a 20 GiB volume, root build
scratch 4 GiB, evidence 16 MiB, supervisor deadline 90 minutes. Scheduled snapshots
were disabled on the owned volume and its snapshot inventory was empty.

Research payload removal reclaimed 2,726,666,240 bytes before volume retirement;
[cleanup.json](cleanup.json) confirms the owned Machine and volume are absent.
The fork's remaining corpus and root build files retired with those resources.
Only source, small manifests and the compressed result archive remain locally.
