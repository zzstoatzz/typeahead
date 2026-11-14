# Search sizing observations — October 27, 2026 UTC

At 05:03:56–05:04:07 UTC, the unchanged production backend logged searches taking
924, 1,282, 2,056 and 2,698 ms. Its search timer begins after read-pool checkout;
these durations include engine work, not just edge/accept-queue waiting.
Warm loopback `na` samples were 35.65 ms with 1.17 MiB of storage reads and
19 major faults, then 19.17 ms with no reads/faults.

## Fly metrics

The one-minute samples around the slow window show CPU work peaking at 0.076
cores, zero CPU throttling, and full burst credits. At 05:04:15, I/O wait reached
1.068 cores of the two-vCPU VM, memory pressure `some avg10` reached 9.01%, and
storage reads reached 3.49 MB/s. Proxy five-minute histogram p99 peaked near
4.15 seconds at 05:04:30; this is a histogram estimate, not an exact search-only
percentile. Memory-pressure percentages describe task stall time, as documented
by [Linux PSI](https://docs.kernel.org/accounting/psi.html).

Historical samples on the former two-GiB compact backend averaged about 1.41 GiB
of file cache; the current one-GiB backend averaged about 0.63 GiB. These samples
cover different serving/data periods and are not a controlled resize experiment.
They establish reduced cache headroom, not sole causation of the slow searches.
CPU, memory and disk units follow the [Fly metric reference](https://fly.io/docs/monitoring/metrics/).

## Inactive A/B/A comparison

The same candidate, b049 image, snapshot and volume ran with two vCPUs at one,
two, then one GiB. Each arm completed the same 1,008 deterministic queries twice.
All 2,016 per-query response hashes were identical across all three arms. Startup
sync was allowed to finish; watermark/compaction markers stayed unchanged during
each <=120-second measurement window. Normal connectivity was preserved.

| RAM | First-pass p99 | Repeat p99 | Repeat disk reads | Cache after workload |
| --- | ---: | ---: | ---: | ---: |
| 1 GiB | 116.6 ms | 31.0 ms | 232.8 MiB | 698.1 MiB |
| 2 GiB | 131.4 ms | 16.4 ms | 0 MiB | 910.7 MiB |
| 1 GiB again | 119.7 ms | 34.4 ms | 281.1 MiB | 692.5 MiB |

The RAM reduction causes extra reads and worse repeat-pass latency for this
working set. The quiet workload did not reproduce multi-second searches, and
first-pass tails did not improve at two GiB. It therefore does not prove that
RAM alone caused the production spike; concurrent sync/I/O and cache reclaim
remain plausible amplifiers. Production was untouched by the experiment.

The initial attempt to freeze sync with a Machine environment override aborted
before workload when the isolation guard failed; the app's secret URL took
precedence. Its config was restored. The successful trial instead verified quiet
startup windows and response equivalence. It restored the normal environment,
connectivity and two-CPU / one-GiB baseline, removed its protected temporary config
backup, and verified the persisted PDS policy. The release retained two CPUs / one GiB. Warm cache improvements alone did
not establish outage causation or justify reverting the announced downsize.
No serving Machine was resized in place.

Raw evidence: `fly-metrics-current.json`, `fly-metrics-recent-range.json`,
`fly-metrics-resize-history.json`, `live-slowness-0504.json`, `live-query-io.json`,
`ram-*.json`, and `ram-comparison.json`. The workload and supervised runner are
retained alongside the receipts. No corpus copy or home-side bulk transfer was
created for this comparison.
