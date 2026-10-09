# Snapshot compaction rollout

2026-10-08 — authorized rollout in local implementation; no deployment or production mutation performed.
The [isolated evaluation](../evals/flat-prefix-compaction.md) is complete. Its
51.4% reduction against VACUUM and passing read gates establish the design, not
production integration or performance on Fly hardware.

Local qualification on 2026-10-08 reproduced exact responses with the Docker
release executable: 8,000 initial responses, 935 overlay-transition responses,
and 850 concurrent responses per format. Conversion exhaustively compared all
151,692,969 actor/posting pairs; the resulting 6,789,846,175-byte snapshot
(including metadata and manifest) is 51.4084% smaller than the VACUUM reference.
Temporary rollback twins are additional storage. Release latency qualification
and production deployment remain pending.

The process-level checks exposed an existing shutdown bug in both formats:
sync/watchdog sleeps swallowed cancellation and resumed sleeping after SIGTERM.
The rollout returns on cancellation, cancels an in-flight Turso fetch, and
propagates cancellation through sync rather than retrying it. Run
`TYPEAHEAD_TEST_RELEASE=/absolute/path/to/typeahead-ingester python3 -m unittest
discover -s scripts/snapshot_compaction -p test_release_lifecycle.py` to verify
termination before admission, with either format admitted, and during a stalled
TCP connection. This test runs
the real executable against local artifacts with no production database access.

Linux qualification also caught `fsync` rejecting Zig's default path-only
directory descriptor. Activation now opens a readable directory descriptor
before syncing it. Run the Zig suite on Linux, including the real rclone cases;
macOS alone does not exercise this distinction. `test_release_recovery.py`
checks process-crash recovery, truncated-map fallback, newer moderation state,
covered-overlay pruning, and refusal to serve an older-watermark fallback.
The existing fatal-exit watchdog also requested a 64 KiB pthread stack, smaller
than the release executable's 256 KiB static TLS segment on Linux. Its stack
reservation is now 1 MiB so the existing deadline tests can spawn the watchdog.
This thread is created only on the fatal-exit path.

Publication is tested with real local rclone transport and an isolated R2 prefix.
R2 `lsjson --stat` returns a directory for a missing object, unlike local storage;
the publisher now distinguishes that from an existing immutable file. The live
test covers first publication, identical retry, corruption rejection, unchanged
pointer on failure, and cleanup. It requires `TYPEAHEAD_TEST_R2=1`, the existing
`INDEX_R2_*` environment, and optionally `INDEX_RCLONE_BIN`.

The first actual-release mixed-workload trial failed before yielding a complete
round: the eight-task async budget includes the sync, watchdog, and promotion
tasks, starving eight persistent HTTP clients. A focused process test reproduced
a 5.4-second connection-timeout stall. The search role now uses a bounded
16-task budget; the focused test and a full-corpus mixed-workload diagnostic pass.
The failed run is retained, and formal performance qualification restarts from
round zero on the corrected image. Both storage variants use the same task
budget. The separate old-release compatibility comparison uses identical
`Connection: close` requests to avoid the old persistent-connection starvation.

## Scope and release structure

Port the tested `unified8scratch` reader and conversion into a clean branch from
the production code baseline. Keep opaque values, the shared immutable map,
bounded scratch, and SQLite overlay. Preserve the current CPU compatibility
baseline. Exclude laboratory HTTP routes, experimental switches, and the
uncommitted PDS-scope work. No scoring, normalization, query limits, or public API
changes belong in this rollout.

Use three reviewable changes:

1. **Reader and builder:** minimal production reader, legacy-format support,
   caller-owned query buffers, and an offline converter with exhaustive equality
   checks. Start with conversion of the existing SQLite builder output rather
   than a second independent indexing algorithm.
2. **Artifact lifecycle:** versioned manifest, publication, download, admission,
   atomic activation, recovery, cleanup, and rollback tests.
3. **Release configuration and runbook:** isolated Fly qualification, reader-first
   deployment, controlled pointer promotion, and eventual scheduled publication.

The evaluated source and frozen hashes are in the evidence directory linked from
the evaluation. Rebuild and revalidate the actual production binary after porting;
the lab binary is not a release artifact.

## Production integration

The current `manifest.zig` describes one SQLite file. `builder.zig` uploads it
before the manifest and pointer. `promote_runner.zig` downloads into an inactive
A/B file, validates SQLite tables, runs SQL sentinels, renames, swaps the read
pool, then prunes the overlay. Each of these assumptions must cover the pair.

- Introduce a new manifest version for the compact artifact. List fixed metadata
  and map object keys, individual sizes/checksums, format version, build identity,
  counts, source watermark, normalization/scoring versions, and existing caps.
  Retain legacy manifest support. Reject unsupported versions, mixed generations,
  missing files, invalid paths, size/hash mismatches, and mismatched metadata.
- Publish immutable generation objects first, verify completion, then publish
  their manifest and finally the selected channel pointer. Never repurpose a
  published generation ID. Give a format conversion a new artifact/build identity
  even when its source watermark is unchanged: today's watcher skips an already
  attached build ID.
- Download to a private staging directory on the serving volume. Validate the
  complete pair and run sentinels through the real prefix lookup and hydration
  adapter, not empty compatibility views. Preserve count floors and all current
  semantic gates.
- Fsync the files and directory, rename the validated generation directory, and
  atomically persist its activation record. A manifest or activation record is
  the unit of identity; two independent file renames are not an atomic pair.
  Publish one prepared generation to the read pool; old leases retain their old
  metadata/map handles until they drain. Define and test recovery at each boundary
  between durable activation and in-memory publication.
- Keep the previous validated generation reachable on failure or restart. Cleanup
  may remove abandoned staging directories and unreferenced generations only;
  it must account for active leases and protected rollback artifacts. Readiness
  requires an admitted generation. Keep serving the old one during validation.
- Expose active format/build/watermark, admission duration and rejection reason,
  readiness, overlay lag, read latency/errors, process RSS, and cgroup memory and
  pressure. Reuse existing telemetry. Admission took about 17 seconds in the lab;
  configure readiness/startup budgets from the slower target-host measurement.
  Avoid redundant whole-map hashing by sharing one verified admission result
  where ownership permits; do not weaken integrity checks to meet a time budget.

Files affected: `services/src/index/{manifest,builder,promote,promote_runner}.zig`,
`services/src/db/LocalDb.zig`, search and sync base-record access, plus reader
module/build wiring. Review `r2.zig` and the home indexer flow for paired upload,
retry, disk sizing, and retained artifacts.

## Rollback correctness

Retaining an old snapshot is insufficient after `compactOverlay` deletes updates
covered by a newer watermark. Restoring the older base can resurrect deletes or
lose renames and moderation changes.

For the initial cutover, encode the **same source snapshot** in both formats and
retain its legacy twin. Use distinct artifact IDs with equal source watermarks,
counts, caps, and contents. Format rollback then changes representation without
moving the source watermark backward. Do not advance to another generation during
this qualification window.

During the first scheduled compact rebuild, retain and verify a legacy twin from
the same checkpoint as well. To roll back the binary, first restore a valid legacy
manifest/pointer and let the dual reader adopt that twin, verify serving, and only
then deploy the old binary. Pin publication so the watcher cannot immediately
readopt the rejected artifact. Keep the mutable overlay and operator overrides.

A rollback to an **older watermark** needs either retained overlay history back to
that watermark or a complete replay, including deletions, before serving. Do not
assume the current incremental cursor provides that history. If neither is
available, use the same-watermark twin or rebuild a current legacy snapshot.
The rollback drill must exercise this distinction explicitly.

## Qualification gates

Before any production pointer changes:

- Reproduce exact actor/posting equality and differential HTTP results on a fresh,
  full corpus. Exercise updates, deletes, renames, hidden actors, exact promotion,
  all supported limits, and both endpoint aliases. Run service suites and the
  repository's search/ranking checks against explicit isolated URLs.
- Inject failures using real files/processes: truncated or mismatched pairs,
  interrupted upload/download, disk full, crash before/after durable activation,
  failed read-pool replacement, concurrent leases, restart with either generation,
  rollback after overlay pruning, and orphan cleanup. Every failure must retain a
  correct serving generation or fail readiness; never silently serve partial data.
- Use a separate Fly app/volume at the tracked search size (2048 MB, four shared
  CPUs). Verify actual hardware, CPU credits/throttling, volume capacity, and
  deployed configuration before sizing the run. The existing lab used different
  hardware. Warm the new process before routing traffic; a single-machine restart
  is not a zero-downtime strategy.
- Compare legacy and compact data from the same source checkpoint with matched
  overlays. Freeze the release binary, artifacts, queries, and acceptance protocol.
  Require at least 50% less total required snapshot disk than both the original
  and VACUUM references, exact responses, and the existing strict read p95 gates
  (one-sided 95% upper paired ratio <= 1; inconclusive fails). Cover uncapped and
  2 GiB modes, restart/warm/broad/concurrent/mixed reads, and matched reload windows.
  Inspect p99, read CPU, admission, retention, errors, and overlay writes separately.
- Provision migration headroom for legacy and compact twins, a complete staged
  incoming generation, overlay/WAL/temp files, and failed-download cleanup. At the
  measured sizes, legacy plus two compact generations alone is roughly 28.9 GB.
  Size from actual artifacts and filesystem free space. The 51.4% saving describes
  one serving artifact, not peak migration disk or every retained backup.

## Deployment sequence

1. **Inventory and isolate.** Record current release/image, R2 pointer/manifest,
   artifact checksums, source watermark, overlay cursor/lag, volume usage, and
   scheduled builder configuration. Confirm no conflicting publish is in flight.
   The checked-in Prefect job currently publishes prod; initial experimental
   builds must explicitly override it to local/staging. The watcher currently
   hard-codes `latest.json`, so add an explicit isolated pointer configuration
   rather than assuming staging builds are automatically consumable.
2. **Qualify the release.** Complete the gates above using staging objects and the
   isolated Fly target. Retain all results, including failures. Abort qualification
   on wrong/unavailable responses, unsupported recovery paths, or a failed or
   inconclusive read gate.
3. **Deploy the dual reader against legacy data.** Verify identical results and
   no legacy-path regression. Keep the production publisher on legacy output.
   Prove that an old binary rejects the new manifest cleanly without displacing
   its current snapshot. Record the exact tested rollback image and legacy twin.
4. **Promote the same-watermark compact generation.** Warm/admit a separate
   serving instance before routing production traffic. Use a reversible backend
   switch or verified blue/green routing; do not assume percentage routing exists.
   Observe a full daily traffic cycle, plus an explicit restart and replacement
   exercise. Monitor both the service directly and the Worker so its edge cache
   cannot hide failures. Correctness/errors/read regressions trigger restoration
   of the protected legacy twin and backend. Do not loosen the acceptance bar.
5. **Enable scheduled compact publication.** Update the actual home-indexer
   deployment (`my-prefect-server/flows/typeahead_index.py` and `prefect.yaml`),
   preserving the schedule and concurrency policy. Verify the resolved source
   revision/compiler and deployment configuration. Validate one complete scheduled
   build → paired upload → admission → overlay projection/pruning cycle, with a
   same-watermark legacy twin retained for that build.
6. **Close out.** After the scheduled cycle and rollback drill pass, document the
   actual deployed image/format/build and evidence in README and operations docs.
   Retain legacy read support through the agreed rollback window. Remove temporary
   routing and duplicate publication only when recovery no longer depends on them;
   keep bounded generation retention. Do not add a permanent alternate codec,
   application cache, or parallel indexing pipeline to this release.

## Approval boundary

This document plans the rollout only. No production pointer, deployment, schedule,
traffic routing, or volume configuration has changed. The implementation and
staging evidence should make the eventual production change concrete and reviewable.


## Implementation status — 2026-10-08

Work is isolated on `codex/snapshot-compaction`; production has not changed.
The dual reader, prepared read-pool exchange, immutable generation admission,
and durable activation record are implemented locally. Legacy publishing
remains the default. `INDEX_LATEST_KEY` selects an isolated manifest pointer;
its default remains `latest.json`.

Admission validates metadata against every serving field in either manifest
version, including the rollback twin's actual watermark. Compact generations
require a same-watermark legacy twin with a distinct identity. Recovery ignores
unfinished activation files and refuses older-watermark fallback. Cleanup
retains the current generation and its recorded predecessor.

Local tests cover conversion fidelity and source preservation, rejected source
metadata/checksums, activation interruption, corrupt-current fallback, newer
overlay preservation, and refusing rollback behind the current watermark.
The optional real transport test uses rclone's local backend:
`TYPEAHEAD_TEST_RCLONE=1 zig build test`. No remote service is mocked.

Compact-pair transport now covers missing and corrupt map fallback through
a verified legacy twin. The Python publisher uploads immutable artifacts,
reads them back to verify size/SHA-256, then writes the compact manifest and
channel pointer last. Retries accept identical objects and reject collisions.
Local real-rclone tests verify pointer preservation on failed publication.

The Prefect integration is isolated in a separate worktree, with
`snapshot_format="sqlite"` still the default. Compact mode builds locally,
then converts and publishes in a separately retryable task. Source builds
remain protected while their compact twin serves. No deployment parameters,
schedule, or credentials have changed.

Still required: full crash-boundary review, production-release performance
qualification on Fly, staged deployment, and the scheduled-cycle/rollback
drills above. The mandatory legacy-twin admission policy is temporary; retiring
it needs an explicitly tested recovery path before the final rollout stage. A passing Linux build is not a rollout gate by itself.


Reproduce local compact-pair tests from the typeahead checkout:

```sh
python3 -m unittest discover -s scripts/snapshot_compaction -p 'test_*.py'
python3 scripts/snapshot_compaction/recovery_fixture.py /tmp/typeahead-pair-fixture
cd services
TYPEAHEAD_TEST_RCLONE=1 TYPEAHEAD_TEST_PAIR_ROOT=/tmp/typeahead-pair-fixture zig build test
```

The fixture directory must not already exist. It contains only synthetic data
and can be removed after the test. Transport tests use rclone's local backend.
