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

Results are pending. No production pacing policy is selected or deployed.
