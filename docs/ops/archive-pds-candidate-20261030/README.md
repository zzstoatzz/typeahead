# Full archive PDS join candidate

Status (October 31, 2026 UTC): full candidate installed and admitted after restart
through the normal deployment. **Not cut over**. The scoped release proof is published and build scratch is removed.
The [completion audit](completion-audit.json) verified the healthy candidate,
unchanged production route/ETag and all published evidence hashes.
Cutover still requires explicit approval and the configuration steps below.
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
measurements. After restart, the deployed reader reported 12,893,931 actors,
4,917,822 missing PDS values and two invalid values: also 61.86% valid coverage.
Live ingestion explains the small count difference.

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

## Verified candidate

- Image: `registry.fly.io/typeahead-tantivy-b@sha256:7431d390561b82224fc21085182a6545bfce2bb80decd70068e59ff476a396ca`.
- Reader SHA: `ef2be085f7ad7b2bf4ea1f0e4976ee07e37b4041a95b5f34c31c562399f17a4d`.
- Typeahead source `c476fc5`; controller source `41ff65c`.
- Machine `8d4930ae3502d8`, volume `vol_vxmpk8k2exk5x1n4`: two shared CPUs, 1 GiB RAM, 20 GiB disk.
- [Prepared operation](prepared-operation.json), [plan](prepared-plan.json), and [runtime admission](runtime-admission.json).
- [Existing production UI pointed to the candidate](https://typeahead-tantivy-dev-ui.n8-3e9.workers.dev/): byte-identical homepage, with a different backend binding.

The [post-restart scope probe](verification-post-restart.json) found all 100 joined
accounts across 53 Bluesky PDS hosts, with no incorrect-scope matches. All 30
[accounts updated after bundle completion](verification-live-updates.json) retained
the joined PDS and matched their current display names. The [53-request HTTP
smoke test](preview-smoke.json) passed, as did [homepage equivalence](preview-ui-equivalence.json).

The [final image functional test](final-image-smoke.json) passed all 15 checks in
a network-isolated cloud container with two CPUs and 1 GiB, using global-merge
retrieval. It covers moderation, migration, deletion, cursor atomicity, crash
recovery and restart. [Evidence hashes and execution binding](verification-evidence.json)
keep this result separate from the still-incomplete release proof.

The [small warm-query comparison](matched-query-check.json) made 64 interleaved
requests, including warmups, with no failures. For 24 measured requests per
backend, server p50/p95 was 2.71/4.56 ms on A and 2.33/2.76 ms on B. This is a
regression check, not a load test or a general speedup claim.

Normal preparation run `0ba7f255-da1a-4702-b75b-99d5eee68d03` completed in
41m46s, including installation, catch-up and restart. Export start to prepared
candidate took about 4h19m including debugging and retries; steady refresh time
has not been established. Candidate disk usage peaked at 12.65 GB during
installation and settled at 6.32 GB after removing the downloaded archive.
Restart qualification took about three minutes. [Controller cleanup](controller-cleanup.json)
confirmed no remaining VM for that run. The candidate stays available for review.

## Recovery history

The following records describe earlier attempts, not the current state.

Prefect recovery completed in run `bd5e7194-aba2-474f-9b79-5b0af0241656`.
The old stopped machine and volume were retired after verifying their immutable
release artifact remains in R2. Its pending reservation is removed; slot B is
available and production routing is unchanged. See [the recovery receipt](slot-recovery.json).
The temporary unscheduled deployment was deleted. Run-level overrides were being
omitted by the Zig Prefect server's polling response; the server fix is committed
as `0eb434d9d41abb2a4e5581384cd1b561a9a46518`. The fixed API and services image is
deployed and the production API contract probe passed. Actual worker execution was subsequently verified through the normal deployment
in completed run `0ba7f255-da1a-4702-b75b-99d5eee68d03`.
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
Deployed runtime verification subsequently passed; see the verified candidate above.

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
tests passed). This diagnostic commit was used by the successful preparation run.

The read-only live-update probe selected 30 source accounts updated after bundle
completion, with missing source PDS and an archive mapping. Selection examined
1,000 recent rows in two SELECT requests (736,413 response bytes); it wrote
no source rows. Its scripts are retained here. The deployed probe subsequently passed; see the live-update verification above.

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

The revised candidate machine is `8d4930ae3502d8`, volume
`vol_vxmpk8k2exk5x1n4`, at two shared CPUs / 1 GiB / 20 GiB. During its
real download, rclone exited 5 after 4,844,421,120 bytes; range recovery
continued to the full 6,324,633,600-byte bundle. Installation subsequently completed and the candidate passed restart admission.
This observation records the transfer phase only. Its
separate download and installation checksum passes reread the bundle; measured
startup time must include both. See [installation observation](install-observation.json).

## Cutover review

[Release proof](release-proof.json) and [immutable readback receipt](release-publication.json)
bind the exact image, binary and serving allocation. Gate descriptions state the
measured scope; they do not establish sustained-load or refresh-time guarantees.

Before an approved cutover, persist controller `41ff65c`, Typeahead `c476fc5`, the
image above and the content-addressed fallback mapping in the normal publisher
configuration, including `INDEX_PDS_FALLBACK_SHA256=1b1fa39e7b5d8f5224fafb08b23cb3a8e277da680f39ff6899280283ffd65e47`.
Follow the repository main-branch rollout rule. Keep the existing
06:00/18:00 UTC cadence. Otherwise a later refresh can discard the joined coverage.
Recheck candidate freshness and the current route ETag, then promote prepared plan
`23335a2301d801d80aa32e67fb7bfce429318be3c3c20291da932467208c2e18`
with the release receipt. Retain A for rollback; verify public requests after the
route/cache barrier. No promotion or schedule/configuration mutation was performed.

[Scratch cleanup](scratch-cleanup.json) removed only the owned cloud build root
and measured 21,589,962,752 bytes of additional free space. Small scripts, results
and storage samples are retained here. The isolated Fly candidate and immutable
R2 bundle/mapping remain. Zig Zen was rerun after cleanup; the relevant resource
lifecycle fix is explicit ownership, evidence preservation and verified reclamation.
README.md architecture and public API guidance need no change: production routing
and public behavior are unchanged, and candidate details belong in this record.
