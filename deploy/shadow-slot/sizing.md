# Shadow slot sizing

Figures are from the [2026-11-10 zkv measurement](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/evals/zkv-scale-20261110/README.md) unless noted: store 9.4 GB settled and 18.6 GB at the peak of a full compaction, 10.0 GB at the peak of the bulk load, Tantivy index 4.9 GB, bundle 6.4 GB, 60 MB of resident metadata per store handle at fence stride 32. They come from one run on local NVMe, not from a Fly volume.

## Volume: 40 GB

| moment | arithmetic | GB |
|---|---|---|
| steady state | store 9.4 + index 4.9 | 14.3 |
| full store compaction | store peak 18.6 + index 4.9 | 23.5 |
| index rebuild during a compaction | 23.5 + second index 4.9 | 28.4 |
| the same, if the rebuild's final merge holds old and new segments | 28.4 + 4.9 | 33.3 |
| bootstrap, bundle downloaded and extracted | bundle 6.4 + index 4.9 | 11.3 |
| bootstrap, store loading | index 4.9 + load peak 10.0 | 14.9 |
| bootstrap, store settling | index 4.9 + 18.6 | 23.5 |

The 20 GB of a production slot does not cover a compaction (23.5). 30 GB covers a rebuild during a compaction (28.4) but not the merge bound. 40 GB covers the worst row with 6.7 GB left for the 1 GiB free-space reserve the production installer keeps, a change log whose retention is undecided, and growth. It matches the "roughly 40 GB per slot" in the design's open questions.

Not measured: the disk a rebuild's final merge needs (the 4.9 is an upper bound from keeping both copies), how often a deep compaction recurs under live writes, the change log's size over time, and how fast the corpus grows the store and index. The measurement run saw a middle level of 0.5 GB above the settled store after its write storm.

## Machine: four shared CPUs, 4 GB

Memory, in MB:

| consumer | budget | basis |
|---|---|---|
| index process | 1024 | production runs it with its poller on a 1 GB machine; the 2026-10-28 load test on the retired dev slot peaked at 695 MB resident, mapped pages included |
| store process, ingest side | 256 | today's ingester runs on a 256 MB machine with its eight identity workers and its tails |
| store handle and memtable | 70 | 60 MB metadata plus the harness's 8 MiB flush threshold |
| compaction and bursts | 310 | the bulk loader, which ran maintenance in process, peaked at 376 MB resident with 4 KiB blocks |
| index rebuild | 400 | a second reader process with the default 128 MB writer budget plus a full scan of the store; not measured |
| total | about 2060 | |

2 GB would hold steady state with about 0.4 GB of page cache for 14.3 GB of mapped files and nothing for a rebuild. 4 GB leaves about 1.9 GB of page cache with a rebuild running. Start at 4 GB, read real use from Fly's metrics, and shrink if the numbers allow.

CPU: the two processes have three shared CPUs between them today (one for the ingester, two for a slot), and the shadow adds compaction and index rebuilds that production does on a separate build machine. Four is the next size. A full-corpus trial on shared CPUs in October reported 71.7% steal during an import, so rebuild time on this machine is a thing to measure, not assume.

Not measured: memory of the store process itself (it does not exist), the index process's footprint on the current corpus, compaction memory apart from the loader, and whether Fly accepts 4 GB with four shared CPUs (expected, not checked against the platform).

## Cost

From [Fly's pricing page](https://fly.io/docs/about/pricing/), read 2026-11-10, prices for `ewr`: shared-cpu-1x with 256 MB is $2.19 a month, shared-cpu-2x with 2 GB is $13.39, additional memory is $6.00 per GB a month, and volumes are $0.15 per GB a month of provisioned capacity. The page lists no four-CPU price; the figure below is derived from the listed ones (2 × 2.19 + 1.5 × 6.00 = 13.38 reproduces the two-CPU price).

| | arithmetic | a month |
|---|---|---|
| machine, 4 shared CPUs and 4 GB | 4 × 2.19 + 3 × 6.00 | $26.76 |
| volume, 40 GB | 40 × 0.15 | $6.00 |
| total | | $32.76 |

For comparison, a production slot by the same arithmetic is 2 × 2.19 + 0.5 × 6.00 + 20 × 0.15 = $10.38. A 2 GB shadow on two shared CPUs would be $13.39 + $6.00 = $19.39. Volume snapshots are $0.08 per GB a month after the first 10 GB and are not planned here.

## Measured on the machine, 2026-11-11

The sections above are arithmetic from other measurements. This one is what `typeahead-shadow-a` did while the search-latency gate of the [shadow comparison](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/README.md) was being met. The runs, their per-query timings and the store's state around each are in [docs/evals/shadow-latency-20261111](https://tangled.org/zzstoatzz.io/typeahead/tree/main/docs/evals/shadow-latency-20261111).

### Four shared CPUs are a quarter of one CPU

Fly gives a shared vCPU 5 ms of every 80 ms, pooled across the machine's vCPUs, and a burst balance that lets it run above that until the balance is spent; a machine that has spent it and used its quota does not run again until the next 80 ms period ([Fly's CPU documentation](https://docs.fly.io/machines/cpu-performance), read 2026-11-11). Four shared vCPUs are 20 ms in 80: 0.25 of a CPU, which is what `fly_instance_cpu_baseline` reports for this machine.

At 11:30 UTC the machine's balance had been under 7 ms in twelve of the thirteen half-hourly samples of the six hours before (the other followed a restart), with `fly_instance_cpu_throttle` rising throughout. Inside it, 20 seconds held 596 jiffies of user and system time, 0.30 of a CPU, and 5,087 of steal; nearly all of the work was the store process's. The same query sent again and again on loopback took about 6 ms, or about 78 ms, or about 155 ms, whichever number of whole periods it had to wait, and which one did not depend on whether the index had committed in between. The comparison's warm median was 74 ms against production's 1.75 ms.

Those six hours were not the store's steady state: the bootstrap resync, a comparison walk and the one-time handle repair (which finished at 11:48) ran in them. What the processes want when nothing holds them back, measured on two dedicated CPUs:

| state | store process | index process |
|---|---|---|
| minutes after a restart, catching up | 0.5 to 1.0 of a CPU: its writer thread alone 0.43 to 0.73 | 0.02 |
| settled, enrichment at its 0.5 requests a second, no repair, no backlog | 0.11 to 0.18 | 0.016 to 0.024, merges included |
| a merge of the whole index (13 million documents) | | one merge thread for 19 minutes, 963 s of CPU time; the process's anonymous memory peaked at 0.82 GB, against 0.05 to 0.25 GB otherwise |

A profile of the store process while it was catching up put 33% of its samples in store maintenance (compaction), 18% in applying writes, 32% in opening HTTPS connections (21% in the TLS handshake, 5% re-reading the certificate bundle) and 9% in the relay stream. None of that was changed here.

So the settled machine fits under a quarter of a CPU, by a little: restored to four shared CPUs with the settings below it used 0.23 of a CPU and its balance grew by about a tenth of a second a minute. A whole-index merge needs about 960 CPU-seconds and the balance holds at most 500, so on shared CPUs the merge, which recurs every few days, spends the balance in its first minutes and then every search waits on the quota until the merge has finished at the throttled rate. The same holds for a resync, a comparison walk or a restart's catch-up.

The last measurement is the settled case. Back on four shared CPUs and 4 GB with the same build (commit 3cb79ae), from 14:26 UTC: no throttling in 24 minutes, CPU use between 0.21 and 0.25 of a CPU in ten of the eleven two-minute samples after the first four minutes (0.31 in the last), and the balance rising from 19.3 s to 21.7 s. Three runs after one to warm up, with enrichment running, one to two store compactions a run, no store stall and no bridge error:

| run | | production p50 / p95 / p99 | shadow p50 / p95 / p99 | gate |
|---|---|---|---|---|
| 1 | cold | 1.91 / 2.51 / 3.23 | 2.24 / 4.18 / 5.19 | passes |
| | warm | 1.86 / 2.58 / 3.56 | 1.29 / 2.60 / 3.26 | passes |
| 2 | cold | 1.93 / 3.09 / 3.66 | 1.94 / 3.24 / 3.63 | passes |
| | warm | 1.86 / 3.11 / 3.73 | 1.24 / 2.10 / 3.04 | passes |
| 3 | cold | 1.99 / 3.40 / 3.52 | 1.80 / 3.02 / 3.68 | passes |
| | warm | 1.90 / 3.17 / 4.02 | 1.31 / 2.10 / 2.95 | passes |

The gate passes on the machine as it is sized, for as long as the balance lasts: 22 seconds of it, refilling at a tenth of a second a minute. The six hours before this work are what the other side of that looks like.

### Two dedicated CPUs

`fly machine update --vm-cpu-kind performance --vm-cpus 2`, same 4 GB, same build (commit 3cb79ae). Server-side search time in ms, gate (c) of the comparison (shadow at most production × 1.10 + 2 ms at each of p50, p95 and p99), three runs after one run to warm up, each about six minutes, 58 queries, one cold and five warm requests each. Background during every run: enrichment running at 0.5 requests a second with its queue backed up, one to two store compactions, the handle repair finished, no store stall, no bridge error.

| run | | production p50 / p95 / p99 | shadow p50 / p95 / p99 | gate |
|---|---|---|---|---|
| 1 | cold | 1.83 / 2.55 / 3.42 | 1.88 / 3.78 / 8.40 | fails on p99 |
| | warm | 1.80 / 2.70 / 3.62 | 1.20 / 2.68 / 4.29 | passes |
| 2 | cold | 1.87 / 3.38 / 4.02 | 1.63 / 2.95 / 3.82 | passes |
| | warm | 1.82 / 2.43 / 3.72 | 1.20 / 2.76 / 4.88 | passes |
| 3 | cold | 1.84 / 2.26 / 3.96 | 1.82 / 2.87 / 4.05 | passes |
| | warm | 1.82 / 2.53 / 3.37 | 1.23 / 2.53 / 3.88 | passes |

The cold p99 of 58 requests is the slowest one. In run 1 it was the first request of the run, 8.4 ms. The build before this one (no page-cache warming) gave the same picture on the same machine: three runs, warm passing in all, cold failing once on one 6.4 ms request. What makes that one request slow is not established: giving the store process and the merge threads the idle scheduling class instead of niceness did not change the tail of a loopback sample, and requests next to a commit were only slightly slower than the rest (3.7% over 3 ms against 2.4%).

### Memory

The index is read through memory maps, so what a search costs depends on what the page cache holds, and 4 GB holds about 3.4 GB of files for an index of 4.7 GB and a store of 4.9 GB. Left alone 25 minutes after a restart, the cache held 1.0 GB of the index and 2.3 GB of the store. Sent queries neither backend had been sent before, one a second each:

| | production p50 / p95 / max | shadow p50 / p95 / max |
|---|---|---|
| 4 GB, nothing warmed, 119 queries | 4.43 / 5.85 / 6.89 | 5.63 / 11.55 / 16.72 |
| 4 GB, term, fast-field, fieldnorm and postings files read into the cache, 110 queries | 4.07 / 5.79 / 6.65 | 2.49 / 7.76 / 15.35 |
| 8 GB, the whole index in the cache, 95 queries | 4.50 / 6.52 / 9.58 | 2.31 / 5.31 / 104.15 |

The reader now does the second row's reading itself for each new segment (`INDEX_WARM_SEGMENT_FILES`). The stored profiles, 1.9 GB, do not fit beside the rest in 4 GB, and each result returned is a read of them. The 104 ms request in the third row was one query three minutes after a restart; the next slowest were 16.8 and 11.1 ms. It was not explained.

With 8 GB (`--vm-memory 8192`) and the stored-profile file read into the cache by hand as well, the gate passed in all three runs and in the warm-up run before them:

| run | | production p50 / p95 / p99 | shadow p50 / p95 / p99 | gate |
|---|---|---|---|---|
| 1 | cold | 2.01 / 3.03 / 3.28 | 1.76 / 3.25 / 5.15 | passes |
| | warm | 1.85 / 3.02 / 3.66 | 1.21 / 2.55 / 3.79 | passes |
| 2 | cold | 1.89 / 2.70 / 3.12 | 1.70 / 3.31 / 4.50 | passes |
| | warm | 1.85 / 3.04 / 4.62 | 1.20 / 2.40 / 3.85 | passes |
| 3 | cold | 1.89 / 2.61 / 3.23 | 1.75 / 2.52 / 3.25 | passes |
| | warm | 1.82 / 2.37 / 3.29 | 1.21 / 2.71 / 4.26 | passes |

Run 1's cold p99 passed by 0.46 ms. Three runs do not show that 8 GB is what removes the cold outlier; they show it did not occur in them.

### What a restart and a whole-index merge cost searches

Neither is covered by the gate, which measures a settled machine.

- A run started within a minute of a restart had cold requests of 2.3 s and 0.4 s and a cold median of 6.6 ms: the reader reports ready before its files are in the cache.
- The whole-index merge of 12:13 to 12:32 UTC, on two dedicated CPUs: a loopback sample of 400 requests taken in its first three minutes, which were also the first three minutes after a restart, had a 99th percentile of 375 ms. Later samples during the merge had medians of 1.75 to 1.87 ms and 90th percentiles of 3.0 to 4.9 ms (3.0 with the merge threads at niceness 19, 4.9 without), against 1.15 ms and 2.0 to 2.2 ms once it had landed; the 99th percentiles were 6.2 to 7.4 ms during and 5.2 to 6.6 ms after. One merge was observed.

### What a store restart costs the watermark

The index answers searches while its watermark is at most 120 s old, on its own clock (`sync_lag_s` in `/index/status` is that clock minus the watermark). The watermark is a second the store process sends: the bridge's clock when it began a read of the change log that reached the log's end. Only a committed push or heartbeat from the bridge moves it, and a heartbeat with no documents does: in six of the samples below the index's position stood still while its watermark advanced. So after the store process stops, the lag grows a second a second until the new process has opened the store and its bridge has committed one push.

Measured on 2026-11-11 on four shared CPUs and 4 GB, 13.2 million actors in 641 tables, `sync_lag_s` read every 2 s from outside. "Before" is commit 6f92b05 with timing lines added; "after" is 00c6a14, and 75d76b8 for the last machine start. Times are from the supervisor's `started store` line.

| what happened | build | bridge's first push | relay connected | peak `sync_lag_s` |
|---|---|---|---|---|
| machine start (a deploy), index warming its files beside it | before, 19:23:59 UTC | 68 s | 106 s | 82 |
| | after, 20:03:33 | 39 s | 23 s | 53 |
| | after, 20:43:38 | 22 s | 22 s | 35 |
| store killed, page cache warm, CPU balance left | before, 19:10:27 | 3 s | 5 s | 7, then 11 |
| | after, 20:10:54 | 3 s | 3 s | 10 |
| store killed, page cache dropped first | before, 19:11:55 | 7 s | 20 s | 14, then 15 |
| | after, 20:16:44 | 6 s | 6 s | 13 |
| store killed as the CPU balance ran out (four busy loops for 40 s first) | before, 19:37:41 | 17 s | 35 s | 21, then 33 |
| | after, 20:22:59 | 6 s | 6 s | 14, then 12 |
| store killed with the balance at zero for minutes | before, 19:44:49 | 13 s | 33 s | 16, then 32 |
| | after, 20:27:58 | 9 s | 8 s | 15, then 12 |
| | after, 20:33:40 | 5 s | 5 s | 10, then 12 |

A kill's peak includes what the watermark had aged before the kill (the bridge rests 5 s between pushes) and the supervisor's 1 to 2 s. "Then" is the highest the lag reached in the five minutes after the restart, where that was 10 s or more: before, with the machine out of balance, the owner's turn ran 36 to 38 s once a minute (28 to 30 s of it one change-log trim) and the bridge, waiting behind it for room in the queue, sent nothing; 49 of 165 samples in that run were at 12 s or more. In the three runs after, 5, 3 and 1 of about 170 were.

Where the time went before, on the machine start: 42.9 s opening the store for writing (641 tables' metadata, read one file at a time beside the index warming its own), 7.8 s counting queues for the health snapshot, 1.1 s and 0.8 s opening the `/store/*` handle and the bridge's, 15 s for the bridge's first push, while the corpus scan read the whole actor range (174 s); then the relay consumer waited 52.5 s for the store lock, held by the owner's first turn, 52.1 s of which was one trim's scan. After: 21.8 s and 19.9 s opening the store (the metadata is asked for ahead), 1.4 s opening the bridge's handle, 16 s and 1 s for the first push; the relay connects as the bridge starts.

What is left is the open, which nothing can run ahead of, and the first push. On a machine start the index's watermark was already 18, 19 and 29 s old when the index came back in the three deploys watched, so a start has to reach its first push inside about 90 s; it took 39 s and 22 s the twice it was measured on this build. That is two samples, on a store of this size: the open reads every table's metadata, so it grows with the store.

### Prices

From [Fly's pricing page](https://docs.fly.io/about/pricing), read 2026-11-11, for `ewr`: performance-1x with 2 GB is $33.00 a month, performance-2x with 4 GB is $66.00, additional memory is $6.00 per GB. The page prices no four- or eight-CPU shared machine; those rows are derived as in the cost table above (a shared vCPU comes to $0.69: 2.19 − 0.25 × 6.00).

| machine | a month | over today's | baseline CPU | gate (c) |
|---|---|---|---|---|
| 4 shared CPUs, 4 GB (today) | $26.76 | | 0.25 | passes in 3 of 3 runs while settled, with 0.02 of a CPU to spare; failed at every percentile, by 70 ms and more, while the balance was spent |
| 8 shared CPUs, 4 GB | $29.52 | $2.76 | 0.5 | not measured; a whole-index merge still outlasts the 500 s balance |
| 1 dedicated CPU, 4 GB | $45.00 | $18.24 | 1 | not measured |
| 2 dedicated CPUs, 4 GB | $66.00 | $39.24 | 2 | warm passes in 6 of 6 runs, cold in 4 of 6 |
| 2 dedicated CPUs, 8 GB | $90.00 | $63.24 | 2 | passes in 3 of 3 runs |

The volume is $6.00 on top of each. The machine was returned to four shared CPUs and 4 GB after these measurements, and `fly.toml` still asks for that.
