# Startup validation buffering experiment

This is an isolated experiment, not a production deployment. Production remains
on its existing image with four shared CPUs and 2 GiB RAM. The disposable Fly app
`typeahead-startup-20261024` uses two shared CPUs and 1 GiB RAM, without public
services. Neither production search nor the ingester is an experiment target.

The candidate retains full-file hashing and structural verification, but reads
up to 1 MiB at a time rather than issuing one read per 8 KiB extent. Both binaries
are in the same reusable image:
`sha256:5f7bbf06530fead019fd6464c0dbcdb5e5ff004d9a05128fab2bca427055de8f`.
Repeating a measurement does not rebuild the image.

The full 7.2 GB download and its queued comparison were stopped at Nate's request.
The first subset takes 20,000 actual actor records from already copied pages and
regenerates 165,432 bounded handle-prefix postings. It has an empty overlay and
is not a faithful copy of production's ranking corpus or PDS coverage.

The first precise comparison uses baseline/candidate/candidate/baseline order,
per-file `POSIX_FADV_DONTNEED` before each process, and 5 ms readiness polling.
The approximately 7.8 MB read workload produced baseline startup times of
47 and 442 ms, versus candidate times of 50 and 217 ms. This variability does
not establish a startup speedup. Process read calls fell from 1,039 to 102.
All 240 general-search responses matched by status and body hash, with no
query errors. Scoped queries consistently returned unavailable because the
subset does not include the required scope metadata.

Raw measurements: [20,000-actor subset](startup-buffer-subset-20261024.json).
The harness is [scripts/startup_eval.py](../../scripts/startup_eval.py).

A second subset contains 100,000 actual actors, 759,330 regenerated prefix keys,
and a 38,617,088-byte prefix file. Extracting actors took 1.72 seconds; preparation
including the native packer and full validation took 28.64 seconds. Baseline
startup measured 2.267 and 2.260 seconds; candidate startup measured 2.266 and
2.276 seconds. Buffering did not measurably improve startup on this workload.
Raw measurements: [100,000-actor subset](startup-buffer-subset-100k-20261024.json).

## Metadata-only startup prototype

After these measurements, Nate explicitly requested a different architecture
with readiness under one second. The evaluation server now has an experimental
mode for a previously validated immutable snapshot: check bounded header fields,
memory-map the root directory without prefaulting it, and read pages on demand.
The mode requires both `TYPEAHEAD_EVAL_VERIFIED_SNAPSHOT` and a disposable
`typeahead-startup-*` app. Production recovery continues using full validation.

On the same verified 100,000-actor subset, readiness measured 18 ms in both
candidate runs, versus 1.314 and 2.290 seconds for the baseline. Startup disk
reads were 77,824–204,800 bytes for the candidate versus 38,641,664 bytes for
the baseline. All 240 general-search responses matched, with no query errors.
The slowest cold candidate query was 44.586 ms; warm maximums stayed below 1 ms.
Raw measurements: [metadata-only subset](startup-metadata-subset-100k-20261024.json).

This establishes an isolated performance result, not a production-safe admission
protocol. The fixture was fully verified before evaluation. The prototype does
not itself bind prior verification to the exact opened file, and FLATPX05 has no
per-page checksum. Production integration must establish immutable file identity
and the chosen integrity checks before enabling this mode outside evaluation.

## Reusing every complete downloaded actor page

The full download was stopped with 2.09 GB allocated in a sparse 7.20 GB file.
Actor pages precede prefix pages, so the usable portion contained more than a
quarter of the actors. The [subset preparation script](../../scripts/startup_downloaded_subset.py)
selects actor extents wholly contained in downloaded data ranges, excluding holes
and partial extents. It retains their byte-identical page payloads and constructs
a new valid directory. It adds complete prefix pages from the 100,000-actor
fixture because the original prefix region was not downloaded.

The resulting file contains 9,775,492 actor records and 759,179 prefix keys,
occupying 2,088,714,240 bytes. Preparation took 246.98 seconds on the disposable
machine. It required no additional download. The full-validation baseline then
successfully checked the constructed file before either metadata-only run.

| Measurement | Full-scan baseline | Metadata run 1 | Metadata run 2 |
| --- | ---: | ---: | ---: |
| Readiness | 123.471 s | 0.018 s | 0.012 s |
| Startup disk bytes | 2,088,976,384 | 344,064 | 45,056 |
| Cold query maximum | 25.136 ms | 38.937 ms | 129.943 ms |
| Warm query maximum | 0.998 ms | 1.029 ms | 1.338 ms |

All 180 general-search responses matched by status and body hash, with no query
errors. Each process started after per-file OS cache eviction, on the same
two-shared-CPU, 1 GiB machine. This does not evict the Fly host's storage caches.
The prefix lists cover the smaller fixture, not all 9.78 million actors: this
tests startup against a larger actor directory and payload, not full production
ranking, concurrency, or overlay behavior.

Raw measurements and subset identity:
[downloaded-data comparison](startup-metadata-downloaded-20261024.json).
The test disk is retained for the continuing experiment at Nate's request;
production was not changed or benchmarked during this experiment.

Active lab resources: machine `7841e35f965528`, volume
`vol_v8eyllqdyykq0z5v` (20 GB, scheduled snapshots disabled), app
`typeahead-startup-20261024`. The abandoned 60 GB restore volume and its machine
were deleted. When the continuing experiment is finished, remove the complete lab
with `fly apps destroy typeahead-startup-20261024 --yes`.

The native Linux image build and local native tests passed, including corruption
checks around the 1 MiB read boundary, oversized values, and end-of-file. These
checks establish retained validation behavior; they do not establish production
memory capacity, overlay performance, or a safe production size reduction.
