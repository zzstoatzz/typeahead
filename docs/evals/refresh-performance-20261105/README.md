# Refresh performance evaluation

Status: offline importer qualified; publisher registered at 17:54 UTC, 2026-11-05.
The [normal production refresh](production-result.json) subsequently used importer
`f1e6e3a2…` and completed at 19:34 UTC (13:34 CT). Its build
`b1793901791-cp-e13a387b75` is serving and the public freshness probe passes.
Production import/producer work through the start of live catch-up took 1,609 s
(26m49s); the full refresh took 5,482 s (91m22s). The earlier comparable run's
import was about 41 minutes and full refresh about 113 minutes. These are separate
production runs, not the controlled allocator-only experiment below. No additional
deployment is needed to activate the faster importer.
The serving binary, allocation and routing remain unchanged. Earlier
100,000-actor batching results do not establish full-corpus performance.

[Controlled million-actor results](import-allocator.json): page-allocation
baseline and repeat both took 129.25 s; libc allocation took 52.73 s with the
same 169 MiB peak RSS and identical query bodies. System CPU fell from 79.6–79.7 s
to 10.4 s. Four workers with eight segments reached 47.70 s, but used 556 MiB
and more CPU. The selected change is allocator-only, keeping the existing
one-worker/128 MiB/50,000-record/one-segment defaults.

The [immutable importer artifact](importer-artifact.json) was uploaded directly
from the cloud builder and read back there by SHA-256. The
[actual production reader compatibility check](production-reader-compatibility.json)
opened its new 500-actor index and matched all 15 query/scope combinations.
The publisher implementation keeps that production reader for full-build source
handoff, freezing and serving; only offline import uses the new executable.
The controlled measurements below are synthetic import timings; production timing
is recorded separately above. The root README was reviewed and retains its concise overview;
the detailed binary-role distinction belongs in the architecture/operations docs.
The [publisher readback](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/audits/2026-11-05-typeahead-fast-importer-registration.json)
confirms source `26fae38`, the immutable importer artifact, the unchanged reader
and release proof, concurrency one with `CANCEL_NEW`, and the next automatic
18:00 UTC run without job overrides. No serving machine, image or routing changed.

Owner: this Typeahead performance task. The first diagnostic uses one temporary,
unscheduled cloud Prefect deployment in `typeahead-ephemeral`, concurrency one,
no source-database or Fly credentials in its job environment, and no serving or
rollback slot access. Its temporary
VM owns `/var/tmp/typeahead-import-perf-*`, with an 8 GiB fixture/index budget,
at least 4 GiB free, and a 35-minute total VM limit. A supervised process group
is terminated on timeout or cancellation. Fixture/index copies are removed in
`finally`; only small JSON results are logged. The worker deletes the VM on exit.
No corpus or build dependencies are downloaded to HeavyPad or the laptop. One
13.6 MB diagnostic executable crossed the operator relay; the selected importer
was instead published and verified directly between its cloud VM and R2.

The diagnostic generates one deterministic million-actor JSONL stream, then
compares the existing settings with four indexing workers, a 2 GiB total writer
budget, larger batches, and one versus eight final segments. Indexing and final
merge timings are recorded separately; the same HTTP query/PDS scope matrix is
compared across the actual readers. A repeated baseline checks order effects.
Synthetic results guide the next experiment; they do not qualify a corpus-scale
rollout. Existing import/footer integrity and admission gates remain mandatory.

[Initial import comparison](import-initial.json): baseline 160.9/165.7 s;
four workers with one final segment 172.3 s; four workers with eight final
segments 147.9 s. Public query bodies matched across every variant. More writer
threads alone did not fix the serial normalization path. The initial RSS field
is the cumulative child high-water mark (including compilation), not an isolated
import measurement; subsequent diagnostics sample the import's own VmHWM and
report user and system CPU separately. Initial binaries targeted the builder's
CPU; they are diagnostic artifacts, not production releases. Subsequent builds
explicitly target baseline x86_64 Linux for portable runtime qualification.

The next candidate replaces the normalizer's page allocator with libc allocation
while retaining one independent arena per FFI result. A local repeated
[40,000-call comparison](normalizer-allocation-local.json) measured 0.417–0.419 s
with page allocation versus 0.213–0.222 s with libc; all reply bytes matched.
This component result does not establish import speed. The same cloud million-
actor comparison must run against the new allocator before selecting defaults.
That comparison builds two portable binaries from the same Rust source, changing
only the adapter allocator. It logs both source and binary hashes, then compares
page-allocation baseline/repeat, libc with unchanged import settings, and libc
with four workers and one/eight final segments. Source is restored after both
builds. Fixture and scratch limits remain unchanged.

Diagnostic setup corrections: the first submission had an invalid source pin and
was cancelled. Run `717005e5-2e97-4c8f-b590-445ee1b5d93c` then reached compiler
setup, where Ubuntu's `needrestart` restarted `prefect-flow.service` after libc
development package installation (exit 143). Its archived VM diagnostic confirms
that service restart; it was not an import failure. The diagnostic now uses
`NEEDRESTART_MODE=l` explicitly through sudo. Both temporary VMs were deleted by
the worker; no production deployment or worker was restarted.

The candidate slot downloader writes four 64 MiB ranges directly into one file.
It records completed ranges only after fsync in an atomic sidecar bound to the
immutable object key, size and range size. A retry preserves completed ranges;
old serial partials preserve whole completed ranges too. An interrupted range
can repeat at most 64 MiB. Stat checks reject oversize objects before allocation;
the transfer retains the one-hour deadline, three attempts per range and 1 GiB
free-space reserve. Small metadata transfers retain the serial path. These source
changes are tested locally, not deployed. Real rclone/HTTP failure tests verify
concurrent requests, interrupted-range recovery, binding rejection and byte limits.

The candidate installer hashes archive bytes during streaming extraction,
including padding and trailing bytes. It retains per-member hashes, inventory
validation, binary identity and actual-reader admission before moving the staged
directory into place. This removes the installer's independent full-archive read;
the downloader's cache/transfer hash remains. Corrupt padding/trailing bytes are
rejected and staging is removed. This source change is not deployed either.

Performance CPU during inactive-slot installation remains to be evaluated. The
serving slot must never be resized, restarted or used for these experiments.

The independent CPU diagnostic uses a new `typeahead-perf-20261105` Fly app,
no services or credentials, no volumes, and the existing D13 runtime image.
Two auto-destroy machines compare shared and performance CPU with the same
512 MiB synthetic tar, 4 GiB memory, and a startup-credit drain before measuring
sustained work. Fly requires at least 4 GiB for two performance CPUs; its rejected
1 GiB request created no machine. An additional shared/1 GiB measurement represents
the current serving allocation. Each process has a five-minute alarm and owns one
`/tmp/typeahead-install-cpu-*` scratch directory, bounded at 2 GiB and removed
on exit. This isolates hashing/extraction from networking; it does not qualify
a production installer or authorize a permanent serving allocation change.

[CPU diagnostic results](installation-cpu.json): at equal 4 GiB memory, the
512 MiB hash-then-extract fixture took 33.06 s on shared CPU and 7.36 s on
performance CPU. These are single component observations, not a full-refresh
speedup. Hashing during extraction did not improve wall time in this fixture
(51.99 s shared, 7.95 s performance), despite slightly lower CPU usage. The fixed
order and burst/IO effects limit that comparison; do not claim a measured gain
from removing the independent read. All three machines auto-destroyed, no volumes
were created, and the diagnostic app was deleted after checking both inventories.
The candidate installer subsequently uses a 1 MiB tar streaming buffer instead
of Python's 10 KiB default, matching its member-copy block size; this adjustment
has correctness coverage but is not included in the timing observations above.

The transfer diagnostic uses a separate auto-destroy Fly machine in
`typeahead-transfer-20261105`, shared 2 CPU / 1 GiB, EWR, no services or volumes.
It receives a 15-minute presigned GET capability for the existing immutable
6 AM artifact, rather than R2 credentials. It compares a 128 MiB prefix with one
and four concurrent HTTP ranges and repeats the serial baseline; all hashes must
match. Total remote reads are bounded at 384 MiB. The ten-minute process alarm,
one-file scratch budget below 1 GiB and temporary-directory teardown bound the
experiment. This measures the network/HTTP path; the actual rclone downloader's
failure and recovery behavior is covered separately above.

[R2 prefix results](transfer.json): serial reads took 12.83 s and 11.04 s;
four ranges took 7.91 s, with identical prefix SHA-256. This is a 28–38% reduction
for that bounded prefix, not a full-bundle or rclone throughput guarantee. The
machine and app are deleted; machine/volume inventories were empty first.

### Noon rollout investigation

Read-only inspection of the serving A machine `822d71b763d008` and its durable
operation `6162bcd4deaa4571a6eaeffb6a19399d` recovered these UTC boundaries for
the noon build. No restart, bulk download or production benchmark was performed.

| Boundary | UTC |
| --- | --- |
| Artifact publication task ended | 18:51:35 |
| Slot metadata cache created | 18:52:27 |
| Deployment and artifact manifests written | 18:52:29–30 |
| Base segment fast-field file created | 19:11:47 |
| Base segment document-store file created / last written | 19:14:57 / 19:19:06 |
| Base segment term file last written | 19:21:24 |
| Installation receipt `installed_at` | 19:24:27 |
| Durable operation promoted | 19:34:01 |
| Retirement task started | 19:34:03 |

File birth and modification times came from `stat` on immutable base-segment
files; the installer copies bytes without restoring archive timestamps. Thus
the document-store extraction alone took approximately 249 seconds for
2,130,676,963 bytes (8.6 MB/s including hashing and scheduling). Extraction was
already underway by 19:11:47, 12m40s before the receipt's `installed_at` timestamp.
That timestamp is assigned before final fallback verification and directory
promotion, so it is not an installation-completion timestamp.
The preceding approximately 19-minute block combines download, download checksum,
the separate installer archive checksum, and any earlier extracted members;
these cannot be separated retrospectively from retained evidence.

The deployed `/app/tantivy_install.py` still hashes the entire archive before
extracting and checking each member. The D13 downloader is serial `rclone cat`,
followed by its own full archive checksum. Parallel transfer and streaming
installer changes in this repository are not in that runtime image. This is
consistent with substantial shared-CPU installation cost, but does not establish
how much time was network-limited. The remaining 9m36s includes final fallback
verification, initial runtime catch-up, qualification, cached restart and promotion; it is not a measured
catch-up duration. Prefect emits no intervening rollout logs and the controller
silently retries qualification errors. Per-phase timings and bounded retry
diagnostics are needed before assigning those residual minutes to a cause.

### Bounded HeavyPad correctness check

The [HeavyPad result](heavypad-correctness.json) and [test log](heavypad-correctness.log)
record eight passing checks using the unchanged production reader, Python 3.12,
and HeavyPad's existing rclone 1.69.1. Synthetic archives and loopback HTTP/S3
endpoints exercise parallel ranges, resuming only unfinished ranges, oversized
objects, stale journal binding, truncated serial transfers, archive padding and
trailing corruption, member checksums, installed reader validation, cached
restart, and deployment identity drift.

The range fixture now uses rclone's actual S3 backend against a local protocol
fixture; the older HTTP backend reported a directory instead of the file during
`lsjson --stat`. Its assertion also requires the intended interrupted-range
failure, so an unrelated preflight rejection cannot masquerade as recovery coverage.
No production implementation changed in this check.

Owner: this Typeahead test session. Scratch root:
`/dev/shm/typeahead-install-check-20261105`, RAM-backed, capped by the runner at
256 MiB with a 180-second deadline. A transient systemd unit additionally bounds
memory at 512 MiB, CPU at 50%, runtime at 200 seconds, nice at 19 and I/O priority
at idle. The passing run took 8.17 seconds and 2.129 CPU-seconds; sampled peak
scratch was 15,917,575 bytes. The only sizable external input was the existing
13,245,896-byte reader, copied from Fly and SHA-verified. No corpus, Turso or R2
access, cloud allocation, or production mutation occurred. Scratch and the local
reader copy were removed; the unit was inactive and no test processes remained.
These are correctness results, not production throughput measurements. The README
entry point needs no change because runtime roles and usage remain unchanged.
