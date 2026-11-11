# Shadow search latency, 2026-11-11

Why `typeahead-shadow-a` answered searches in 76 ms at the median when production answered in 1.7 ms, what was changed, and what the latency gate of the [shadow comparison](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md) reads now. Every run is `compare.py --phases latency --production https://typeahead-tantivy-b.fly.dev --shadow https://typeahead-shadow-a.fly.dev`: 58 queries, one cold and five warm requests each, one request a second to each backend, server-side `Server-Timing`. The gate allows the shadow production's value × 1.10 + 2 ms at each of p50, p95 and p99, cold and warm.

## Causes

| cause | evidence | size |
|---|---|---|
| The machine had spent its shared-CPU quota. Four shared vCPUs run 20 ms of every 80 ms once the burst balance is gone, and the store process had kept it gone for at least six hours. | Balance at its floor, 5,087 jiffies of steal in 20 s against 596 of work; one query repeated on loopback took 6, 78 or 155 ms, independent of index commits. | 76 ms median; nothing else was visible under it |
| Every query opened the rank column of every segment, once per retrieval pass. Opening it decodes a block header per 512 documents, deleted ones included. | `perf` on the reader under query load: 43% of samples in that decode. | the 6 to 7 ms floor on the shadow's 24.7 M documents. Production runs the same code on 13 M documents; its share was not measured there |
| The index held 11.4 M deleted documents in 12 to 18 segments. Tantivy's default merge policy never merges for deletions, and the bootstrap resync had rewritten 85% of the snapshot segment. | Segment list from `meta.json`; the median of a loopback sample went from 1.8 ms to 1.15 ms when one merge brought it to 13.25 M documents in 3 to 6 segments. | about 0.7 ms at the median, and 4.1 GB of disk |
| The store process and the merge threads ran at the priority of search. | Reniced by hand on two dedicated CPUs: the store: 95th percentile of a loopback sample 5.2 ms before, 4.4 and 3.6 ms after. The merge threads, during a whole-index merge: 90th percentile 4.9 ms before, 3.0 ms after, twice. | the tail, not the median |
| Pages of the index not in the page cache. | 197 of 211 first-time queries took major faults, about nine each, median 6.9 ms; the 14 without took 1.5 ms. The faults were in the term dictionary and postings. | first-time queries only; see the memory table in sizing |

Looked at and not a cause: the index reload after each commit (the first query after a reload had the same median as the rest, 2.2 ms, before the merge settings changed), and the reader's runtime (searches and writes run on blocking threads; a write holds no lock a search takes).

The bridge committing every 1.3 s was not a latency cause by itself. It became a CPU cost with the new merge settings: one merge thread at 32 to 35% of a CPU rewriting the smallest level every other commit, until the rest between pushes went to 5 s and the smallest level's floor to 1,000 documents (0.4%).

## What changed

Commits on `shadow-latency`, in order: the rank column is opened once per generation and shared across generations; the serving writer's merge policy is configurable; the bridge's rest between pushes is configurable; merge threads can be reniced; new segments' files can be read into the page cache; and the shadow slot's configuration uses all of them. A production slot built from the same source keeps its behaviour unless it passes the new options, except for the rank column, which is not optional.

## Before and after

Server-side ms, shadow p50 / p95 / p99 (production's in the same run in brackets). [Sizing](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/shadow-slot/sizing.md#measured-on-the-machine-2026-11-11) has every run and the machine each ran on.

| | cold | warm |
|---|---|---|
| before, 4 shared CPUs (`runs/baseline.json`) | 75.06 / 240.16 / 315.72 (2.02 / 2.97 / 3.30) | 74.18 / 248.32 / 475.98 (1.75 / 2.31 / 3.06) |
| rank column fix only, 2 dedicated CPUs, 3 minutes after a restart (`runs/fix1-perf2.json`) | 34.71 / 286.69 / 2141.00 (3.26 / 4.89 / 8.01) | 2.29 / 5.27 / 8.90 (1.76 / 2.65 / 3.41) |
| everything, 2 dedicated CPUs, 4 GB, runs `perf2-v2-1` to `-3` | 1.88 / 3.78 / 8.40, 1.63 / 2.95 / 3.82, 1.82 / 2.87 / 4.05 | 1.20 / 2.68 / 4.29, 1.20 / 2.76 / 4.88, 1.23 / 2.53 / 3.88 |
| everything, 2 dedicated CPUs, 8 GB, runs `perf2-8g-1` to `-3` | 1.76 / 3.25 / 5.15, 1.70 / 3.31 / 4.50, 1.75 / 2.52 / 3.25 | 1.21 / 2.55 / 3.79, 1.20 / 2.40 / 3.85, 1.21 / 2.71 / 4.26 |
| everything, 4 shared CPUs, 4 GB, which is how the machine was left, runs `shared4-final-1` to `-3` | 2.24 / 4.18 / 5.19, 1.94 / 3.24 / 3.63, 1.80 / 3.02 / 3.68 | 1.29 / 2.60 / 3.26, 1.24 / 2.10 / 3.04, 1.31 / 2.10 / 2.95 |

The gate passed in all three runs on 8 GB and in all three on the shared machine, and failed once in three on two dedicated CPUs with 4 GB, on the cold p99, which is one request. On the shared machine it passes only while the CPU balance lasts: the settled machine used 0.23 of the 0.25 of a CPU it is entitled to.

`perf2-final-*` are the build before page-cache warming, on 2 dedicated CPUs and 4 GB. Run `-0` of each series started right after a restart and is the warm-up.

## Freshness

`sync_lag_s` sampled once a second for two minutes at the 5 s rest: 0 to 5 s, mean 2.5 s, against the readiness limit of 120 s. Production over 100 samples in the same minutes: 5 to 52 s, mean 29 s.

## Files

`runs/*.json` hold each run's summary, per-query timings and the store's `/health` before and after. `first-time-queries-*.json` are the three first-time-query samples and `first_time_queries.py` the script that took them. `merge_policy_sim.py` is the model the merge settings were chosen with: a week of commits replacing random documents, Tantivy's level rule, instant merges. It predicted 40% of a CPU for the first settings tried at a 1.3 s cadence, where 32 to 35% was then measured.

## Load on production

Production was sent only `/search` and `/index/status`. The comparison paces itself at one request a second. For about two minutes during run `perf2-final-0`, a `sync_lag_s` sample of `/index/status` every 1.2 s ran beside it, so production received about 1.8 requests a second, above the one a second this work was held to.
