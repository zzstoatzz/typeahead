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

On October 17 the exact candidate passed six real-process checks: corrupt-current
and modified-twin rejection; crash fallback preserving newer moderation; interrupted
rclone download cleanup; SIGTERM before/after admission and with stalled upstream;
eight persistent mixed clients; and 32 persistent mixed clients. The first run
skipped transport because rclone was absent from PATH; the individual test passed
with `/home/stoat/.local/bin` added. Disk-full remains to be rechecked in this run.

Current-generation conversion is running on HeavyPad under user unit
`typeahead-compact-convert-20261017.service`, one CPU maximum, 8 GB memory maximum,
four-hour deadline. Work root: `/home/stoat/typeahead-release-20261017`.
`legacy-b1792141282-189b` protects the immutable legacy source using a hard link;
`compact-b1792141282-189b` is published locally only after exhaustive conversion
verification. No R2 pointer is changed by this job.

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

Pending: cold and compatibility candidate gates, release reload/rollback, image
packaging, isolated channel/replacement provisioning, route rehearsal, disaster
drill, production cutover, scheduled publication, observation, cleanup. Fill in
exact resources, commands, timestamps, and results as each action completes.

### Packaged candidate — October 17

The production runtime base is pinned to
`sha256:65cf9b0019df39319f5d006efb0642eaaebaa819f6ac5c28910f91b0620e1fc9`.
The candidate image platform digest is
`sha256:db843d21e8da8f28a34a71dcd398904287eed7fe242b5b35cf70fd745a846174`;
its executable checksum matches the warm-qualified candidate both in Docker and
on Fly. It is staged on `typeahead-search-compact`, machine `d895206f9ed638`,
volume `vol_4912m2168o2e2w3r` (40 GB), with no public services. The volume is for
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
