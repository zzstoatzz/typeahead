# Compact production cutover

Status: preparation, 2026-10-17 UTC. This runbook supplements the
[snapshot rollout gates](snapshot-compaction-rollout.md); it does not waive them.
Nate authorized production rollout, disaster rollback, and eventual cleanup on
October 16. Production traffic has not changed.

## Verified starting state

- Search machine: `867e15ce4e1148`, `search-phase3`, ewr, four shared CPUs,
  2048 MB; volume `vol_4m3mkzpz1lmy383v`, 60 GB, 35 GB used/22 GB available.
- Image: `registry.fly.io/typeahead-search:deployment-01M433KYXAP6J35FA6BR9S7TEC`.
  Executable SHA-256:
  `f82400c65efa59456b3240828da41e496ad2e92532318972fbf9356e226ea920`.
- Serving generation: `b1792141282-189b`, watermark `1792141239`,
  12,560,923 actors, 142,072,036 prefixes, SQLite bytes `15620362240`;
  SHA-256 `269debe19da171d4c76045baedf0a528299cbf6b4aca9e88f1cdf9c03ba7d169`.
  The same manifest exists in the HeavyPad builder directory.
- Fly service has no readiness checks. `/health` status alone is not an admission
  gate. Replacement routing must wait for attached expected generation, completed
  overlay initialization, acceptable sync lag, and successful real searches.
- Prefect deployment `5d446811-c098-438e-b16c-f1ccd9da7e97` runs on `home-pool`,
  cron `0 9 */3 * *`, collision strategy `CANCEL_NEW`. Its October 16 run
  `a2fc6417-22c7-4481-ad66-5d7bdd28f44a` completed. Current parameters are empty;
  the live configuration does not contain `INDEX_QUIET`. Refresh before deploying;
  do not restore older whole-file Prefect configuration over concurrent work.
- The existing compaction pull is `3mxgdp7z2qh22`; there are no comments.
  Main's unrelated PDS work remains uncommitted in another checkout.

## Candidate and evidence

Source `38e0a27` (later rollout commits record evidence), executable SHA-256
`97bef48a1c9de968e08b91635de9e4a29968c0db522d73fb6010ff1d132b2e91`.
The 60-round pinned warm gate passes. Earlier cold and full-size rollback evidence
uses older executables and must not be represented as testing this candidate.

On October 17 the exact candidate passed all seven real-process recovery checks
inside the packaged Fly runtime, including real ENOSPC. Current-generation
conversion and differential correctness also passed; detailed evidence appears
below. Earlier incomplete attempts remain recorded in the release evidence.
The protected legacy source and verified compact output are under
`/home/stoat/typeahead-release-20261017` on HeavyPad. Isolated R2 publication
is in progress; no production pointer has changed.

## Cutover sequence and stop conditions

1. Finish exact-candidate cold/legacy compatibility, matched reload, full-size
   recovery and disaster restoration checks; retain every failed attempt. Package
   and pin the tested executable, runtime image, source, manifests, and hashes.
2. Inventory current Worker configuration and routing before choosing the switch.
   Prefer a separately addressed ready backend, keeping the existing backend
   running and synchronized. Rehearse switching in isolation and record the exact
   reverse command before any production switch. No in-place replacement of the
   sole serving machine during preparation.
3. Publish immutable paired artifacts to an isolated channel; verify readback.
   Admit legacy first on the replacement, then its same-watermark compact twin.
   Check both XRPC aliases and cache behavior through an isolated Worker target.
   Exercise restart and replacement from R2 with an empty data volume.
4. Switch only after the replacement serves the expected generation and overlay,
   with exact responses on frozen data, moderation/update transitions intact,
   passing latency gates, and readiness checks rejecting incomplete admission.
   Capture direct-backend and Worker errors/latency before and after the switch.
5. Roll back traffic immediately for correctness/alias/moderation failure,
   unavailable searches, or a confirmed latency regression. Preserve failed
   artifacts/logs. Do not run a benchmark against live traffic to diagnose it.
6. Integrate the publisher into current my-prefect-server main, preserving its
   schedule, delivery mode, credentials, and concurrent changes. Verify one complete
   scheduled compact build, paired upload, admission, and overlay pruning cycle.
   Observe a full daily traffic cycle before declaring rollout complete.

## Disaster rollback

Traffic rollback and artifact rollback are separate operations. The old backend
must remain live and synchronized through cutover so restoring traffic does not
wait for a 15 GB download. Record a pinned legacy image digest and protected
same-watermark legacy manifests/artifacts outside either serving volume.

For loss of the compact machine/volume, provision a fresh isolated volume and
restore from the protected artifacts. Verify checksums, expected watermark,
readiness, catch-up, moderation, and searches before routing. Exercise this path
with the final image and real R2 transport before cutover; a successful process
restart on the same disk is insufficient.

Never load an older snapshot over an overlay already pruned beyond its watermark.
For a binary/format rollback use the same-watermark twin and verified durable
activation procedure, or rebuild the overlay from the restored snapshot watermark
on a separate machine. Prove response equality after renames, hides, deletes,
and score updates. Scheduled publication must not silently advance or overwrite
protected rollback references while a rollback is underway.

## Cleanup gate

Remove old serving resources only after the daily observation, scheduled cycle,
restart/replacement, traffic reversal, and empty-volume disaster drill pass.
Keep the independently recoverable image, manifest, and legacy twin for a dated
rollback window; choose its expiry at cutover and record it here. Measure actual
required generation retention, incoming staging, overlay/WAL, and free-space
headroom before sizing a smaller volume. Do not infer peak disk savings from the
single-artifact compression ratio. Delete temporary channels, extra volumes,
credentials, and test data only after checking references and saving evidence.

## Remaining execution record

Pending: cold and compatibility candidate gates, release reload/rollback, isolated
channel publication verification, serving replacement readiness, route rehearsal,
disaster drill, production cutover, scheduled publication, observation, cleanup. Fill in
exact resources, commands, timestamps, and results as each action completes.

### Packaged candidate — October 17

The production runtime base is pinned to
`sha256:65cf9b0019df39319f5d006efb0642eaaebaa819f6ac5c28910f91b0620e1fc9`.
The candidate image platform digest is
`sha256:db843d21e8da8f28a34a71dcd398904287eed7fe242b5b35cf70fd745a846174`;
its executable checksum matches the warm-qualified candidate both in Docker and
on Fly. It is staged on `typeahead-search-compact`, machine `d895206f9ed638`,
volume `vol_4912m2168o2e2w3r` (initially 40 GB, extended to 60 GB), with no public services. The volume is for
isolated qualification and migration preparation; final retention sizing remains
unproven. The machine currently runs `sleep infinity`, with qualification processes
started explicitly. Do not treat machine state as serving readiness.

The frozen qualification corpus is downloading from R2 with checksum verification;
this is separate from the newer October 16 conversion on HeavyPad. Cold trials use
four shared CPUs/2048 MB, no swap, 60 randomized rounds and the unchanged thresholds.
CPU-credit and throttling metrics are available. No cold campaign starts until
artifact verification succeeds and the initial credit threshold is reached.
[Release inventory](../evals/compact-production-release.json) records the image,
resources, harness hashes, and current status. The real ENOSPC recovery test also
passed on HeavyPad in a private 8 MiB tmpfs mount namespace.

All seven real-process recovery tests passed inside this exact Fly image, with
no skips: corruption rejection, crash/moderation preservation, interrupted
download cleanup, shutdown, 8/32-client concurrency, and real ENOSPC recovery.
The [test log](../evals/compact-production-release-tests.log) is retained.

### Scheduled-promotion storage correction

Review of `ensureTwin` → `ensureGeneration` → durable activation →
`cleanupGenerations` shows that a new checkpoint requires both old and new
legacy/compact pairs on disk until activation completes. The current pair is
22,583,115,776 bytes; two pairs require about 45.2 GB before overlay/WAL and
other temporary files. The 40 GB candidate volume was extended to 60 GB
before the cold campaign. The earlier one-legacy/two-compact estimate was
insufficient for repeated scheduled publication. Retiring mandatory local twins
requires a separately tested recovery path before smaller production volumes
can be claimed safe; this release preserves the current recovery contract.

### Current-generation evidence

Conversion of `b1792141282-189b` completed with exhaustive native verification.
The compact pair is 6,962,753,536 bytes versus original SQLite 15,620,362,240
and actual VACUUM output 14,287,581,184: both 50% reduction gates pass. The exact
candidate also matched 8,000 initial, 935 mutation-transition, and 850 concurrent
responses per format on this current snapshot, without forced shutdown.
[Structured evidence](../evals/compact-production-release.json) includes the
manifest, per-format correctness results, and measured storage gate.

The first full-size rollback preflight stopped before starting a server because
the old qualification pointer had been removed. A fresh isolated channel under
`staging/compact-release-20261017/` is being published and verified for the
current generation. The deployed legacy executable always reads `latest.json`;
its rollback drill uses that fixed key within the isolated namespace, while
the new executable can select `legacy.json` explicitly. No production pointer
is altered. This preflight failure is retained and is not a passing rollback.

### Worker routing inventory — October 17

The active Worker deployment is `165af368-6b74-4dc3-873e-8f233293ef57`,
serving version `5daf6f08-c3d6-4cf4-bf9a-4f56efdef979` at 100%. It includes
the independently shipped mobile stats fix. `SEARCH_BACKEND_URL` is a secret
binding. Preserve this Worker code when switching the backend; do not deploy the
older Worker source from this rollout branch. The migration build ID (`compact-b...`) requires the freshness-parser fix
recorded with this rollout; the scheduled publisher’s `b...-px05` ID already
matches the legacy parser. Ship that narrow Worker fix while preserving the
stats page before switching routing. Before staging a binding-only
version, refresh active and latest-uploaded versions: Wrangler's version-secret
operation copies the latest uploaded version, which need not be active. Record
both old and replacement versions and rehearse reversal before production.

### Admission command

Run `python3 scripts/check-search-readiness.py <isolated-backend-url> --build <expected-build>`
before registering or switching traffic. It requires an attached, ready snapshot,
the exact expected generation, a successful overlay-sync watermark no older than
600 seconds, a nonempty corpus, and the exact-handle probe at rank one. On October
17 the live legacy backend passed; a deliberately incorrect expected build was
rejected. This is a cutover admission command, not a recurring health monitor.
A fresh replacement must additionally pass the alias, moderation, ranking and
recovery gates above. A Fly `/health` HTTP-status check alone remains insufficient.

### Freshness prerequisite shipped — October 17

Worker version `28622855-db7a-4cf6-9e18-762e3485f05a` shipped the compact-ID
freshness parser. Its isolated source was the exact stats release archive plus
only `freshness.ts` and its test changes; no PDS or search-route code changed.
All 97 Worker tests, typechecking, lint and post-deploy smoke passed. The public
freshness endpoint still reports healthy legacy build `b1792141282-189b` with a
known snapshot age. The pre-fix Worker version remains available, but use this
new version as the routing-switch base so rollback retains the monitoring fix.
