# Full archive PDS join candidate

Status: full artifact published and checksum readback verified; deployed
qualification in progress on October 31, 2026 UTC. **Not admitted or cut over**.
Production routing, source ingestion and authoritative identity handling remain
unchanged. Data processing runs only in the cloud, with no source-database writes.
The user clarified that HeavyPad may be contacted for Prefect control-plane operations.

The existing cloud archive exported 16,249,268 usable mappings from 16,300,003
repo records in 462.48 seconds. The mapping is 1,478,053,888 bytes, SHA-256
`1b1fa39e7b5d8f5224fafb08b23cb3a8e277da680f39ff6899280283ffd65e47`.
These are archive mappings, not the count of Typeahead accounts recovered.
The verified full stream contains 12,981,514 records, including 12,890,252 live
actors. The join supplies 5,026,154 previously missing PDS values; 4,917,558 live
actors still have no mapping in the available archive. Valid scoped coverage is
7,972,692 actors (61.85%), up from 2,946,538 (22.86%). These are build-input
measurements, not yet a deployed-candidate result.

The runner calls the existing `fetch_checkpoint`, `build_checkpoint` and
`freeze_bundle` functions directly on the cloud host. This keeps the data build on the cloud host. Prefect control-plane access is
used separately for inactive-slot recovery and qualification. It downloads one source checkpoint and streams actors
into the pinned production Rust binary; it does not write a full JSONL copy.
The mapping is hard-linked into the generation on the same filesystem, then
included in the portable release. A separate content-addressed mapping object
allows later cloud publishers to use the same verified input.

Bounds: metadata export 20 minutes/3 GiB; checkpoint fetch 15 minutes/8 GiB;
future checkpoint preparation 90 minutes, within the worker's two-hour limit.
The initial full attempt used 60 minutes; its verified import was recovered
with separate bounded merge and catch-up runs. These are failure
cutoffs, not expected runtimes. Scratch is owned by this candidate under
`/tmp/typeahead-pds-join-full` on the existing archive host. Keep the candidate
until qualification/cutover review; remove redundant checkpoint and test files
after measurements and verification are recorded.

Cloud integration tests: 23 passed, one skipped and one Prefect-context test
excluded; installer/slot tests: 22 passed, three skipped, 136 subchecks passed.
The real Rust reader imported, caught up, froze, installed and reopened the
joined fixture. Missing/corrupted mappings and removed provenance were rejected.
The earlier pilot verifies explicit source migrations and deletions take priority.

Candidate image:
`registry.fly.io/typeahead-tantivy-b@sha256:39fc1880a5bbe3c118256478b5773974c84590e6141b7ee076431e6ed1ff917f`.
Reader SHA-256:
`ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d`.
Typeahead source is `a19405e1fa1fd2a1aaecd8a77ac0a23c9cb102af`; controller source
is `9f4e8ac6395f778178ab38e5f83d13a1af15da9a`. The reader adds checked import
recovery and exact JSON float parsing; retrieval behavior is unchanged.
The Dockerfile uses portable COPY plus chmod because
the cloud host's Docker lacks BuildKit.

Prefect recovery completed in run `bd5e7194-aba2-474f-9b79-5b0af0241656`.
The old stopped machine and volume were retired after verifying their immutable
release artifact remains in R2. Its pending reservation is removed; slot B is
available and production routing is unchanged. See [the recovery receipt](slot-recovery.json).
The temporary unscheduled deployment was deleted. Run-level overrides were being
omitted by the Zig Prefect server's polling response; the server fix is committed
as `0eb434d9d41abb2a4e5581384cd1b561a9a46518`. The fixed API and services image is
deployed and the production API contract probe passed. Actual worker execution
will be checked through the normal candidate prepare deployment.
The normal deployment executed the candidate code. Its first available-cloud
run stopped at the inactive-volume fence: Fly still lists the previously deleted
volume as detached `pending_destroy`. The controller now ignores such unrelated
deleted entries while continuing to reject active, attached or same-request
volumes. All 25 controller tests passed. Preparation resumes request
`6fc28231cca440ef8369f6abad8b6975` in flow run
`1d9eedd0-dbaf-4c06-83f9-98a4700a2c5c`. This uses the normal deployment in Helsinki
after the default Falkenstein allocation returned unavailable capacity.

The original 60-minute deadline interrupted the final merge after the producer
had completed and its receipt matched every imported record. Recovery resumes
the saved index only after checking its committed footer, source identity,
counts and receipt hash; it does not download or import the corpus again.
The recovery fixture exercises crash, resume, catch-up and portable publication.
A separate regression exposed loss of floating-point precision in JSON receipt
round trips. Exact float parsing passes the reproduced case. All 11 checkpoint
tests and 15 real engine smoke checks pass. Installer tests pass (13 tests,
one unavailable optional integration skipped).

The first live replay completed but exceeded the polling freshness window.
The builder now requires another fresh cycle before admission. Packaging validates
the stopped snapshot at a single captured time, rather than invalidating it merely
because archive creation takes longer than two minutes. Runtime gates remain strict.

Measured data work: archive export 462.48 s; checkpoint acquisition 534.39 s;
full actor stream 3,485.68 s; resumed merge 889 s. Additional live catch-up,
packaging, debugging and qualification are separate; these figures are not an
end-to-end production refresh guarantee. The portable bundle is 6,324,633,600 bytes
(5.89 GiB), including its 1.38-GiB mapping. See [build measurements](candidate-build.json).
The [publication receipt](publication-proof.json) binds the verified bundle and
manifest. No routing pointer was written. README architecture and public API
guidance remain accurate; these candidate recovery changes do not change them.

Zig Zen was run. Relevant follow-ups: bind the fallback through every boundary,
fail closed on missing artifacts, include it in disk measurements, and ensure
all child processes and source connections close even if another cleanup fails.
Deployed runtime coverage, scoped search, resource and live-update verification remain pending.

### Candidate installation recovery (2026-10-31 UTC)

Normal deployment run `1d9eedd0-dbaf-4c06-83f9-98a4700a2c5c` created
owned machine `8d4930ae350ed8` and volume `vol_42kxgzm0kdw88284`, then timed
out awaiting start. The machine remained stopped; the old controller discarded
the provider error, so its cause is unproven. An explicit start of that same
machine succeeded. Preparation resumed as normal deployment run
`7aea315f-5ed3-453d-98f3-c57cd29293ac`, preserving request
`6fc28231cca440ef8369f6abad8b6975`, artifact and resource ownership.

The controller branch now preserves sanitized HTTP status/transport error
information in its journal and transition errors (`41ff65c`; 25 lifecycle
tests passed). This diagnostic commit is not yet the running controller pin.

The read-only live-update probe selected 30 source accounts updated after bundle
completion, with missing source PDS and an archive mapping. Selection examined
1,000 recent rows in two SELECT requests (736,413 response bytes); it wrote
no source rows. Its scripts are retained here. The deployed probe must still
pass; selecting samples alone does not establish preservation.

### Interrupted installation transfer

The first slot download stopped at 3,987,730,432 of 6,324,633,600 bytes. Fly
reported exit code 1 and `oom_killed=false`; subsequent starts rejected the
incomplete cache. The old downloader discarded rclone stderr, so the underlying
transport error is not known. Preparation run `7aea315f` was canceled.

`c476fc5` adds bounded range-resumption with a final whole-bundle checksum,
validates cached deployment/source manifests before reuse, and records retry
byte counts and exit codes. The revised image is
`registry.fly.io/typeahead-tantivy-b@sha256:7431d390561b82224fc21085182a6545bfce2bb80decd70068e59ff476a396ca`.
It reuses the same Rust binary and portable artifact. Its Linux tests at two
CPUs/1 GiB passed (9 tests, 2 optional serving integrations skipped), including
real rclone HTTP interruption/range recovery and cached-manifest installation.
See [test output](slot-transfer-tests.log). Zig Zen was rerun for this change.

Normal deployment run `c9182ad4-2ea3-430d-b5e8-63f8a8bc43af` completed the
preparation cancellation. The stopped partial machine and volume were retired,
and the old retention marker was conditionally removed after checking ownership,
resource deletion and the retained artifact manifest. See [retirement receipt](partial-slot-retired.json).

Revised preparation run `0ba7f255-da1a-4702-b75b-99d5eee68d03` uses request
`a7d276e5a7a84939a05e0915b5004179`, Typeahead `c476fc5` and controller `41ff65c`.
See [preparation receipt](resumable-slot-preparation.json). Production routing
still points to A; this is preparation only. The immutable image binding requires
a fresh owned slot, so this attempt downloads the published artifact again. It
does not rebuild or republish the snapshot.
