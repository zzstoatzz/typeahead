# Standby corpus-scan experiment

Owner: this Typeahead task, November 16, 2026. Experiment target:
`typeahead-slot-b`, machine `781e7e3ad11068`. Slot A remains serving. The
initial route admission is retained in `.eval/scan-standby-admission.json`;
it reported slot A healthy and slot B fit. No route flip is planned.

## Procedure and limits

The existing image, resource size, store writer and search engine stay on B.
Temporarily set `CORPUS_SCAN_SECONDS=0` on that machine and let it restart and
become fit, preventing its hourly scan from overlapping controlled scans.
Preserve its original machine config for restoration. This does not touch A.

The optional `build-corpus-scan-probe` Zig target builds a read-only tool using
the production scanner and layout. Rest zero takes the unchanged scan path;
rest 20 ms reads at most 1,000 records, releases the cursor, waits, refreshes
the reader and resumes. Both count the complete actor range. No store or
index snapshot is copied. The store continues writing while it is read, so
these counts describe a moving corpus, as the current hourly scan does.

Run 90-second no-scan controls around an unpaced / paced / unpaced series.
Collect one loopback `/debug/search` at a time, with a one-second gap after
each response, using a fixed manifest from the saved cloud run. Same binary,
same queries, same machine; index updates continue. Preserve returned DIDs,
latencies, hydration and moderation timings, HTTP failures, phase boundaries
and scan totals. Compare common cases as well as full phase distributions;
changed rankings must be visible rather than assumed equivalent. Fetch Fly
CPU, disk and memory metrics for the exact interval afterwards.

The supervisor is `setsid timeout --kill-after=10 2400 bash corpus-scan.sh`.
The script stops both its sampler and scanner on exit. It checks the public
route before each phase and every 30 samples and aborts if B starts serving.
The original machine config is restored after results are copied out, even
when the trial fails. Do not restart, resize, pause or deploy to A.

Scratch root: `/tmp/typeahead-scan-20261116` on B. Disk budget: 64 MiB for the
3.8 MiB tool, manifests, logs and request data. No corpus payload is created.
Each output file has a 32 MiB shell limit. Remove this exact directory after
copying its small evidence files; verify no trial processes remain, restore
the machine config, check the unchanged route and run standby admission.

## Results

The trial completed successfully at 20:47:21 UTC. The probe binary's SHA-256
was `dc19e93c3b4ba5df4571dbea4e3f8a6c3449c965928aaa2211919aab31f2375b`,
built from `8bc283f`; the running harness was `21451c2`. It used the existing
engine binary and live index throughout. The query manifest held 180 queries:
the original 20 eval cases plus handle prefixes derived from that saved run,
with no new upstream sampling. It sent 851 loopback diagnostic requests.

| Phase | Requests | Failures | Handler p50 | Handler p90 |
| --- | ---: | ---: | ---: | ---: |
| Initial post-restart control | 47 | 8 | 144.348 ms | 1,804.290 ms |
| Unpaced scan, first | 130 | 0 | 51.076 ms | 358.400 ms |
| No scan, recovery | 88 | 0 | 1.770 ms | 4.777 ms |
| 20 ms rest / 1,000 records | 276 | 0 | 1.839 ms | 7.300 ms |
| No scan, recovery | 89 | 0 | 0.982 ms | 1.822 ms |
| Unpaced scan, repeated | 133 | 0 | 38.916 ms | 327.324 ms |
| No scan, final | 88 | 0 | 1.573 ms | 4.403 ms |

Quantiles in this table cover successful responses; failures are shown
separately. The initial control still suffered cold post-restart I/O and is
not the steady-state baseline. Admission alone did not establish warm latency.
The two unpaced scans took 148,998 and 150,387 ms; the paced scan took 283,080 ms.
All three counted the complete range, roughly 13.337 million records.

The phases have different durations and therefore different case coverage.
There were 88 query cases present in every steady-state phase, with identical
ordered result DIDs across all their observations. Average repeated passes
within each query first, then give those queries equal weight:

| Matched 88-query cohort | p90 of per-query mean handler time |
| --- | ---: |
| First unpaced scan | 383.920 ms |
| No-scan recovery | 4.777 ms |
| Paced scan | 3.252 ms |
| No-scan recovery | 1.822 ms |
| Repeated unpaced scan | 193.281 ms |
| Final no-scan control | 4.403 ms |

These are not public `/eval` request quantiles. The body was produced over
loopback with no Worker or CDN. The unchanged engine, matched queries, stable
results and repeated unpaced regression support a causal link between the
unpaced read and search stalls, beyond the earlier production correlation.

Fly's complete 15-second windows within the phases showed B averaging
42.30% and 38.90% CPU I/O wait during the unpaced scans, versus 0.36% during
the paced scan and 0.14–0.73% during the recovery controls. No phase incurred
CPU quota throttling. A and B were on different physical hosts (`e510` and
`6e98` respectively). This qualification covers this machine and workload;
pacing does not guarantee isolation under other resource pressure.

## Cleanup and deployed state

The driver copied out its bounded logs and results and restarted B using the
saved machine configuration. Fly's update merged environment variables: the
temporary `CORPUS_SCAN_SECONDS=0` remained. After Nate required that stats not
interfere with production, that disabled-scan setting was deliberately retained
as an interim mitigation. Every other config field, image and resource size
matched the original. This is not a deployed pacing policy or the full fix.
An ordinary deploy using the current canonical config could re-enable the old
default: this mitigation is not a categorical safeguard.

The restart retired the ephemeral scratch root and all trial processes. A
follow-up check confirmed the root absent and no probe process. Root filesystem
available space returned exactly to its initial 7,892,033,536 bytes. There were
no copied stores, indexes or hard links to reclaim. B passed standby admission;
A's machine ID, instance version, image, configuration and serving route were
unchanged and serving was healthy. No route flip occurred. No images were built
or deployed; the standalone tool was the only uploaded executable and is gone.

The service test suite and the new paced-pagination/count/cancellation regression
passed before the tool ran. [Results, provenance and compact samples](2026-11-16-standby-scan-results.json)
are retained; raw responses, metrics, configs and logs remain under
`.eval/scan-experiment/`. `scripts/experiments/corpus-scan-results.ts` validates
the raw request records and derives the latency and matching summaries.

## Required replacement

The experiment establishes that pacing can mitigate the observed contention.
It does not meet the subsequently requested guarantee. See
[statistics isolation](2026-11-16-statistics-isolation.md) for the history and
the required removal of observational scans from serving roles, maintained
counters, offline bootstrap/audits and executable regression/admission gates.
No maintained-counter replacement or categorical protection has been deployed.
README's description of the deployed architecture remains accurate; it is not
changed to claim the proposed replacement exists.
