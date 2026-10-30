# Matched full-corpus performance component

Run `1ca2a04e4c8b`, October 30, 2026. The frozen-feed component passed; this is not complete release qualification.

Same 12,816,605-actor snapshot, input/query/update hashes and independent raw-source oracle. Both private Fly backends served on two shared CPUs, 1 GiB RAM and separate 20 GiB disks. A separate generator sent 10 requests/second per backend; latency includes queueing. Temporary preparation used two performance CPUs and 4 GiB, and does not qualify serving speed.

## Latency for features supported by both backends

| Phase | Native p95 | Tantivy p95 | Native p99 | Tantivy p99 |
| --- | ---: | ---: | ---: | ---: |
| first touch | 55.02 ms | 21.50 ms | 63.92 ms | 50.82 ms |
| warm | 37.06 ms | 9.04 ms | 42.24 ms | 11.01 ms |
| mixed | 37.28 ms | 8.20 ms | 41.95 ms | 9.95 ms |

Tantivy passed every predeclared relative phase gate and the warm/mixed absolute targets. First-touch means the first scheduled query pass after preparation, not an independently established cold disk/cache state.

## Correctness and resources

Tantivy had zero oracle mismatches and zero transport failures across 5,224 requests, including 512 after process restart. Both backends durably acknowledged all nine update stages. The additional 25-query functional probe passed hide/delete/PDS-change checks and three hydration witnesses; it left native unchanged. Native mismatches remain in the raw receipt; these curated cases do not measure production recall.

| Mixed-phase observation | Native | Tantivy |
| --- | ---: | ---: |
| Configured payload footprint | 6.88 GiB | 4.21 GiB |
| Summed selected-process RSS | 254.10 MiB | 484.77 MiB |
| Minimum guest available memory | 700.15 MiB | 674.95 MiB |
| Minimum free disk | 11.60 GiB | 14.27 GiB |

The configured payload footprint excludes unconfigured paths and is not allocated disk. Summed RSS can double-count shared pages; Tantivy used more of this measure, so this result does not establish a RAM reduction. Both arms had zero OOM kills and zero swap-in/out during measurement and passed required resource observations.

## Remaining release gates

The running reader was the exact D1 executable, but the benchmark image differs from the managed slot image. The query set is curated, not captured production traffic. The update feed is frozen and does not establish live source/policy coverage or sustained full-round merge behavior. Actual slot deployment, live completed-round coverage, public preview/API smoke and recurring refresh/recovery remain required.

The supervisor captured the complete, untruncated receipt and functional proof, then cleaned up. A fresh provider list showed zero qualification machines. Both volumes are non-billable pending-destroy records; physical purge is provider-managed. Production was unchanged.

[Full receipt, identities, metrics, functional proof and cleanup](qualification-full-1ca2a04e4c8b.json).
