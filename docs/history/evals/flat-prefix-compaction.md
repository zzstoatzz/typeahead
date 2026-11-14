# Snapshot compaction

2026-10-08 — **isolated goal complete; not deployed**. Acceptance requires at least 50% less disk, identical results, and every predeclared read-latency comparison to pass. Inconclusive results do not pass.

| Snapshot | Total bytes | Reduction versus VACUUM |
| --- | ---: | ---: |
| Serving snapshot | 15,280,254,976 | — |
| Same contents after SQLite VACUUM | 13,973,295,104 | — |
| Unified map, metadata, and manifest | 6,789,845,476 | 51.4% |

The candidate is 55.6% smaller than the serving file. One immutable map stores profiles and prefix lists, with unchanged opaque values. Frontcoded keys and page-local sharing of identical values reduce overhead. A shared 28.3 MB directory routes each lookup to one 8 KiB extent; oversized records use bounded larger extents. Reads use 16 KiB scratch space and copy only the selected value into the request arena. The mutable overlay stays in SQLite.

All **12,388,730 profiles and 139,304,239 prefix lists** match build `b1791408253-dac0` byte-for-byte. Fresh differential testing matches 9,131 HTTP responses, including overlay updates, plus 870 concurrent reload reads with zero unavailable responses. Native and Linux suites pass 139 tests each, with two skipped. Independent checks cover 8,000 rehashed mutations, 282,624 truncation points, allocation failures, oversized records, and returned-value lifetime. Repository search and canonical ranking checks pass for both versions, with identical local ranking results. Both reference and candidate include the same caller-owned query-buffer fix for a previously reproduced concurrency bug; that fix is also not deployed.

## Why this design

The earlier prefix-only format passed size and correctness but failed cold reads, including a measured 3.8% regression after disabling SQLite mmap. Tracing attributed 98.5% of measured read time in full-result queries to profile hydration. Of 128 sampled first-touch profile pages, 101 were B-tree interior pages. Replacing that tree with the existing page format removes dependent reads without adding a second codec or cache.

A useful cost model is `dependent cache misses × I/O latency + bytes / bandwidth + decoding + queueing`. Smaller files alone do not guarantee faster reads. The unified map cuts cold read calls sharply; reusable scratch also avoids retaining a full page per profile. Diagnostic trials are selection evidence, not acceptance.

[STAR-lite](https://tangled.org/microcosm.blue/star/blob/f9b8c80346b4b38b3d93840f33a42fb7e119b2d9/star-lite/readme.md) informed keeping opaque records and removing derived structure. [Hubble](https://tangled.org/microcosm.blue/hubble/blob/f24051201c2ee2703497e0f26a0992f3f5db74f2/hubble/src/store/mod.rs) informed resident routing and bounded reads. [JSS](https://github.com/bluesky-social/jetstream/blob/8facdb4daab7fe5ce273f5f182f6eb0136054eb4/docs/README.md) targets archive replay; whole-block compression was not adopted for these point lookups.

## Acceptance and limits

The frozen candidate is tested for 60 randomized rounds per mode against two identical reference controls. Each of five phases—restart, warm, broad, concurrent, and mixed reads—must have a one-sided 95% upper confidence bound on its paired p95 ratio at or below 1. No latency tolerance is added.

All ten comparisons pass, with zero request errors across 360 trials. Median paired p95 improvements:

| Read phase | Uncapped | 2 GiB, no swap |
| --- | ---: | ---: |
| Restart | 50.7% | 61.4% |
| Warm | 15.6% | 17.5% |
| Broad | 18.1% | 46.8% |
| Eight concurrent readers | 2.8% | 24.1% |
| Reads mixed with updates | 7.5% | 8.6% |

Every read-p99 upper confidence bound also passes. Peak process RSS medians fall from 132.1 to 42.9 MiB uncapped and 117.3 to 46.3 MiB bounded. Separate diagnostic read CPU falls from 0.83 to 0.62 seconds; this diagnostic is not the formal latency gate.

Tests use the full serving snapshot on different hardware, with 2,000 fresh generated queries rather than recorded traffic. Latency uses limit 50; correctness also covers 1, 10, and 100. Bounded trials include process memory and page cache in a 2 GiB cgroup with swap disabled; only private experiment files are evicted. Uncapped trials prewarm each entire artifact. Artifact, binary, and query hashes are frozen. After timing, all 12 frozen input hashes verified unchanged. Sustained testing runs 204,000 reads per version: identical response digests, constant descriptors/mappings, and anonymous memory within 4 KiB over the final 40,000 reads. Four distinct-file reloads/rollbacks serve 344,938 candidate reads with zero wrong or unavailable responses and successful reads throughout admission.

Snapshot admission validates and hashes the full map: startup rises from roughly 32 ms to 17 seconds; reloads under sustained load take approximately 18–27 seconds while the old map continues serving. An initial unequal-duration reload diagnostic was inconclusive. A separately frozen 20-round comparison then tested equal 30-second windows, with one reload at second 2, seven readers, two reference controls, and 2 GiB/no swap. It passes the same strict p95 rule: paired median ratio 0.9624, one-sided 95% upper bound 0.9776. Read p99 also passes (upper bound 0.9968); all 60 trials maintain exact responses and continuous read progress. Each trial uses a uniform 10,000-read latency sample; raw samples and the frozen protocol are retained. Uncapped mixed-write p99 is also inconclusive (median ratio 1.021); no claim is made that every operation improves. Process RSS excludes unmapped file cache, which remains included in the cgroup limit. Neither metric should be confused with offline builder memory.

The [rollout plan](../plans/snapshot-compaction-rollout.md) covers production integration: paired-artifact publication, download, promotion, recovery, and sentinel checks. Existing promotion rejects this experimental format. The laboratory server contains evaluation routes and must not be deployed.

Evidence, source, and rejected attempts are retained under `~/.local/share/typeahead-experiments/production-validation-1791480000/evolution/`, mirrored on `stoat@heavypad` under `/home/stoat/typeahead-compaction-validation-1791480000/evolution/`. The current candidate is `unified8scratch`; its protocol and frozen-input manifest define acceptance. Production files, pointers, and services are unchanged.
