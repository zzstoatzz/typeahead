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
