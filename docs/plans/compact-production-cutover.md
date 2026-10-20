# Compact production cutover

Status: scheduled compact generation live and verified, October 20, 2026
00:50 UTC. Worker `6a814dee-4ffe-4b26-aa38-dc39e92f3c6c` serves compact build
`b1792400507-4df8-px05`; legacy remains ready behind reverse Worker
`86590349-5a6e-42c1-a6de-4c5d8835ff3b`. Refresh current versions before rollback.
Scheduled publication, daily observation, protected pair readback, native admission,
live catch-up, ranking and empty-directory recovery passed. The four stopped
qualification machines and volumes are removed. The Prefect source is on main and
its superseded PR/worktree are closed/removed. Typeahead source integration uses a
non-force Git fast-forward after passing CI; see the closeout below.
The dated sections below preserve superseded states and earlier failures; use this
status and the latest dated section for current operations. Only Nate's two
October 17 performance exceptions apply.

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

Current executable: `cc2d5a1`, SHA-256
`758d458b829f350531364982d24136b9e1097f4e4c34977996a37488ea9a3606`.
The following summarizes completed evidence as of October 17; the dated sections
below retain the full chronology, including superseded attempts.

| Gate | Current result |
| --- | --- |
| Recovery, disaster restore, legacy rollback | Passed on the exact candidate |
| Complete live replay and restart admission | Passed on the isolated public replacement |
| Ranking and current-Worker alias/cache checks | Passed; broader oracle overlap remains below the aspirational target |
| Bounded cold campaign | All 180 trials valid, primary gates passed, no secondary regression flags |
| Uncapped warm campaign | Inconclusive at the unchanged strict gate; +3.5% mixed-read p95 upper confidence bound |
| Native matched reload | Passed all 60 trials; p95 upper confidence ratio 0.96794, no secondary flags |
| Deployed legacy performance comparison | Old control crashed; incomplete, not passing |
| Production routing | Verified at 19:37 UTC; legacy backend retained |
| Scheduled compact cycle and daily observation | Passed; source/PR/worktree cleanup remains |

Four completed qualification machines and their volumes were removed October 20.
The compact production backend and legacy rollback backend remain running. Nate approved the warm-latency and unavailable legacy-comparison exceptions on
October 17, conditional on reload passing. Reload has now passed; other cutover
and recovery requirements remain in force.

### Earlier candidate and repair history

Superseded source `38e0a27` (retained historical evidence), executable SHA-256
`97bef48a1c9de968e08b91635de9e4a29968c0db522d73fb6010ff1d132b2e91`.
Its 60-round pinned warm gate passed. Earlier cold and full-size rollback evidence
uses older executables and must not be represented as testing this candidate.

On October 17 the exact candidate passed all seven real-process recovery checks
inside the packaged Fly runtime, including real ENOSPC. Current-generation
conversion and differential correctness also passed; detailed evidence appears
below. Earlier incomplete attempts remain recorded in the release evidence.
The protected legacy source and verified compact output are under
`/home/stoat/typeahead-release-20261017` on HeavyPad. Isolated R2 publication and protected checkpoint readback are complete; no
production pointer has changed. The current candidate is `cc2d5a1`, described below.

### October 17 shutdown failure

The full-corpus live R2-pointer drill switched legacy → compact → legacy with
seven concurrent readers and completed the before/after 8,000-response comparisons.
It then failed the 20-second SIGTERM drain deadline. No passing result is claimed.
The [retained server log](../evals/compact-hot-reload-shutdown-failure.log.gz)
shows R2 child cancellation converted into `RcloneFailed`, followed by continued
polling. A real-rclone stalled-endpoint regression reproduces this on the exact
candidate ([failure evidence](../evals/compact-r2-cancel-original.log.gz)).

The transport must propagate cancellation, reap its child, and the promotion
watcher must exit on cancellation. The Linux regression passes with the local replacement executable
`758d458b829f350531364982d24136b9e1097f4e4c34977996a37488ea9a3606`
([evidence](../evals/compact-r2-cancel-fixed.log.gz)); all five lifecycle variants
exit cleanly, and the stalled R2 connection closes. Zig 0.17's canceled POSIX wait
clears the child ID without reaping it; retain the ID for explicit child cleanup.
The first repair exited but leaked the child and was rejected by this check.
A macOS lifecycle run also failed all five SIGTERM cases and is not claimed as
passing; Linux is the deployed runtime. Native unit checks passed (142 passed,
4 skipped). The final repair `cc2d5a1` also passed all seven Linux recovery tests without
skips ([log](../evals/compact-cancel-fixed-recovery.log.gz)) and the
[full-size live reload drill](../evals/compact-cancel-fixed-hot-reload.json):
352,382 verified concurrent reads while switching to compact, 10,185 while
switching back, 8,000 exact responses before and after, and clean SIGTERM exit.
This is correctness/recovery evidence, not matched latency acceptance.
The replacement image is running only on isolated Fly qualification/recovery machines;
its immutable digests are in the release evidence JSON. Fresh 60-round warm and
cold campaigns use the [recorded unchanged settings](../evals/compact-cancel-fixed-campaigns.json).
The cold campaign waits for the same CPU-credit gate after machine replacement.
The fresh-volume restore launcher now requires this replacement binary and the
verified protected checkpoint. Source tag `compact-candidate-cc2d5a1` resolves to
`cc2d5a1439793c050ac20404f97edbb20df6113e`; a complete bundle restores that commit.
Its OCI archive was verified through every blob and the executable, then loaded
and run without network/pulls. Both replacement objects have full verified R2 readback under
`releases/compact-candidate-cc2d5a1/{source.bundle,image.tar.gz}`, outside the
21-day `builds/` expiration rule. The legacy runtime backup stays under the
previous release prefix. The exact deployed-reader compatibility campaign was later attempted with the
unmodified legacy binary and stopped after that control crashed; its failure
is retained below. The cold campaign completed and passed independently.
The superseded cold controller and queued compatibility controller were stopped;
completed and interrupted trials are preserved. The replacement requires its own qualification; existing results
remain associated with `38e0a27`. Production routing remains unchanged.

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

Pending: resolution of the inconclusive warm gate and failed legacy-control
comparison, completion and assessment of matched reload latency, production cutover,
scheduled publication, daily observation, and cleanup. The complete cold campaign,
isolated replacement replay/restart admission, route rehearsal, fresh-volume
restoration, legacy binary rollback, native full-size reload correctness, protected
publication, and independent image/source recovery have passed as recorded below.
Refresh readiness immediately before routing; a historical admission result does
not establish current readiness. Record exact resources, commands, timestamps, and
results as each remaining action completes.

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

At the initial inventory, Worker deployment `165af368-6b74-4dc3-873e-8f233293ef57` was active,
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
For an empty-overlay restore, also supply `--max-snapshot-age 604800`. This
rejects unknown/future watermarks and snapshots at least seven days old, because
Turso tombstones expire after seven days. Require a complete successful sync pass
before admission; the per-page lag alone is insufficient. This age restriction
does not diagnose a continuously synchronized backend with an older base.
On October 17, the live legacy backend passed ordinary admission; its status
endpoint omits `source_watermark`, so fresh-restore admission correctly rejected
it as unknown. The private replacement then passed the seven-day age check and rejected a
deliberate one-second limit through a loopback-only SSH forwarding proxy. Both
checks still rejected overall admission because live replay was behind; see
[actual probe results](../evals/compact-recovery-age-admission.json).
A fresh replacement must additionally pass the alias, moderation, ranking and
recovery gates above. A Fly `/health` HTTP-status check alone remains insufficient.

### Freshness prerequisite shipped — October 17

Worker version `28622855-db7a-4cf6-9e18-762e3485f05a` shipped the compact-ID
freshness parser. Its isolated source was the exact stats release archive plus
only `freshness.ts` and its test changes; no PDS or search-route code changed.
All 97 Worker tests, typechecking, lint and post-deploy smoke passed. The smoke
recipe skipped authenticated admin-override checks (no `TYPEAHEAD_SECRET`)
and the optional comparison benchmark; those are not claimed as verified here. The public
freshness endpoint still reports healthy legacy build `b1792141282-189b` with a
known snapshot age. The pre-fix Worker version remains available, but use this
latest reconciled version below as the routing-switch base so rollback retains the monitoring fix.

### Fresh-volume recovery target — October 17

Machine `781e732c264678` (`compact-recovery`) in `typeahead-search-compact`
uses the pinned candidate image and new encrypted 60 GB volume
`vol_rnzg2g0gkko1lmpr`. Before copying test fixtures, `/data` contained only
`lost+found` and used 24 KiB; the executable SHA matched the qualified binary.
It has no public services. The restore harness is staged separately from the
latency machine and will run only after the current-generation rollback drill
produces verified reference response hashes. Restoring the artifact is distinct
from replaying the live upstream overlay; both must pass before traffic admission.
The target may become the serving replacement only after all rollout gates pass;
otherwise it remains a temporary resource covered by the cleanup gate. The drill
uses a deliberately isolated upstream and writes fixture mutations to its overlay.
Before reusing this machine for serving, stop the drill process, discard only its
evaluation overlay database/WAL/SHM, preserve the verified snapshot artifacts,
and rebuild the overlay from the snapshot watermark against real Turso. Never
route traffic to an overlay containing qualification mutations.

### Current-generation rollback passed — October 17

The exact candidate served `compact-b1792141282-189b`, accepted 60 overlay
mutations, then matched 8,000 response bodies through both dual-reader legacy
fallback and the deployed legacy executable. The protected same-watermark legacy
twin was hard-linked into `index.A.db` and the index directory fsynced **before**
switching the isolated format pointer. Preserve the overlay throughout; do not
use an older-watermark snapshot. The new reader shut down cleanly; the deployed
legacy process used the drill's explicit SIGKILL teardown.

The initial fixture attempt seeded publisher-format `manifest.json` directly in
a generation directory, which correctly failed the reader's canonical local
manifest check (`GenerationIdentityCollision`). That failed directory remains
at `full-rollback-fixture-collision`; the corrected A-slot procedure passed in
unit `typeahead-compact-rollback-aslot-20261017.service`. The full reference
response hashes are retained in [compressed evidence](../evals/compact-current-rollback.json.gz).
This verifies local format/binary rollback with the real R2 legacy pointer;
it does not claim fresh-volume restoration or live upstream catch-up. The
fresh-volume drill waits for both this result and full compact publication.

### Legacy-reader comparison staged — October 17

The unmodified deployed binary (`f82400c6…`) and comparison harness are staged
on qualification machine `d895206f9ed638`. Its frozen legacy pointer was uploaded
and read-back verified at `staging/compact-release-20261017/frozen-compat/latest.json`.
The [pinned protocol](../evals/compact-current-compat-protocol.json) preserves all
queries, the original paired thresholds, production reader pool sizes (old four,
candidate eight), CPU-credit checks and no-swap memory limit. The comparison is
queued after collection and acceptance of all 180 compact cold trials; any failed
cold trial or gate stops that continuation. No compatibility result is claimed yet.
The old decoder's known UTF-8 race remains present; the baseline was not patched
and no queries were removed. Preserve this isolated pointer until evidence is
collected, then include it in temporary-channel cleanup.

### Publisher pin and source recovery — October 17

The canonical Tangled tag `compact-candidate-38e0a27` resolves to the exact
qualified source `38e0a27234bbb76a1210da01e72e6a0d0fde67fc`. A shallow clone
using the flow's `--depth 1 --branch <ref>` path resolved to that commit. A
complete Git bundle was independently cloned and uploaded/read-back verified
at `releases/compact-candidate-38e0a27/source.bundle`; its SHA-256 is recorded
in the release inventory. The configured GitHub Typeahead fallback returned
“Repository not found” from this host, so it is not a verified recovery source.

The publisher integration is [pull request 10](https://tangled.org/zzstoatzz.io/my-prefect-server/pulls/10).
A fresh read of the live deployment shows `MPS_PIN=@2e743f38633bca962818d52e540257b99f102fc6`,
empty parameters, the unchanged `0 9 */3 * *` schedule and no `INDEX_QUIET`.
The integration branch includes that source revision. No publisher deployment
or schedule mutation has occurred in this rollout. Pin the selected Typeahead
source tag explicitly when enabling compact publication; registration and a
complete scheduled cycle remain pending.

### Independent runtime archives and retention — October 17

Both Linux amd64 runtime images are archived outside the Fly registry at
`releases/compact-candidate-38e0a27/{compact,legacy}-image.tar.gz` in the index
bucket. Every OCI blob, manifest reference and executable checksum was verified;
both archives loaded into Docker and ran the checksum command successfully.
Both R2 objects passed full readback verification. Archive hashes and exact
platform digests are recorded in the release inventory. `docker image load -i
<archive>` restores the compact tag or legacy image ID; retag and push to a
recovery app's registry after `fly auth docker` if the original registry is gone.
Do not delete the old app relying only on its registry references.

The actual bucket lifecycle expires `builds/` at 21 days and `staging/` at 7 days;
there is no object-expiration rule for `releases/`. Thus the original paths alone
do not protect a longer rollback window. A copy of the current pair and original
manifests is being verified under
`releases/compact-candidate-38e0a27/checkpoint-b1792141282-189b/`, preserving the
relative `builds/...` object keys. Its `latest.json` is the legacy pointer and
`compact.json` is the compact pointer. The fresh-volume drill uses this prefix
as the R2 bucket subpath, and may start only once `protected-checkpoint.json`
records success. Protection is pending until that record exists. The release
prefix is manually retired only after the dated rollback window and cleanup gate.

Before cleanup, protect and verify a fresh legacy/compact pair from the completed
scheduled publication. Thirty-day source/image retention does not provide a
thirty-day empty-overlay restore window: snapshots must remain within the
seven-day tombstone replay window through catchup. Refresh the protected pair
after publication; if it ages out, rebuild from current durable data instead of
admitting an old snapshot. Keep the synchronized rollback backend until these
recovery conditions and the observation gates are satisfied.

## Native matched reload qualification

The [native reload harness and frozen protocol](../evals/compact-native-reload/reload-latency-formal-protocol.json)
exercise the actual release executable, R2 pointer polling, full incoming artifact
validation, durable activation, and read-pool replacement. The previous lab-only
swap test does not qualify this path. Its 30-second window excluded the real
69–89-second admission observed during development; the replacement protocol
fixes a 180-second window for every role, with dispatch at two seconds. This is
an explicitly different experiment, not a claim that the older lab test passed.

The benchmark retains 20 paired rounds, two SQLite controls, seven readers,
the original query set, uniform latency sampling, exact response equality,
whole-second read progress, and the one-sided 95% p95 upper-ratio threshold of
1. Inconclusive results fail. p99 and serving CPU per completed request remain
secondary gates. Every incoming artifact has a distinct inode and verified hash.
The release runs under a 2 GiB/no-swap cgroup. Artifacts are cached locally;
network artifact download performance is covered by the separate empty-volume
restoration drill, not this matched reload test.

A separate one-round development run is queued after the warm campaign exits,
so the two experiments do not compete for CPU/memory. Its results cannot count
toward the formal 20 rounds. The native harness is syntax-checked but not yet
runtime-validated; no reload latency acceptance is claimed.

## Protected checkpoint and current Worker checks

On October 17 the [protected checkpoint](../evals/compact-protected-checkpoint.json)
completed full SHA readback for both formats and their manifests, with isolated
legacy/compact pointers written last. No production pointer changed. The
replacement executable has started restoring from that namespace onto the empty
recovery volume. The restoration process is live; completion is not yet claimed.
This fixture drill uses the default four-slot read pool; eventual live serving
must use the separately qualified eight-slot configuration and a clean upstream
replay, without fixture mutations.

The exact source of Worker version `28622855-db7a-4cf6-9e18-762e3485f05a`
passed [isolated public error/CORS checks](../evals/compact-current-worker-errors.json):
both aliases agree for absent/blank queries, invalid limits, and an unavailable
backend. OPTIONS succeeds for both, and unavailable-backend answers remain
uncached. All bindings were local, database access and fallbacks disabled. The
local Worker was stopped afterward. Successful full-corpus alias checks remain
a separate gate against the replacement backend.

## Legacy backend safety during pointer change

The [exact deployed-reader drill](../evals/compact-deployed-reader-rejection.json)
passed on October 17. Binary `f82400c65efa59456b3240828da41e496ad2e92532318972fbf9356e226ea920`
first served the full current legacy snapshot and 60 overlay mutations. Its
isolated R2 `latest.json` then changed to the real compact manifest, with the
compact database available under that namespace. Three explicit
`UnsupportedManifestVersion` rejections were observed while the old build stayed
attached; all 8,000 response hashes matched before and after. The production
pointer and backend were untouched. The unchanged old process uses forced
teardown in this drill; no clean-shutdown claim is made for it.

This establishes the pointer-first preparation step: the old serving backend can
retain its legacy snapshot while the replacement is configured for the compact
production pointer. Routing still waits for all remaining release gates and live
overlay catch-up. Reverse routing must retain the old backend and its data until
independent disaster restoration and the observation/publication gates complete.

The fresh-volume drill now has a second, conditional stage: after compact
restoration has exited successfully and written its result, materialize the
restored legacy twin as `index.A.db`, fsync the directory, and boot the exact
old reader against the protected namespace's legacy `latest.json`. Require
all 8,000 reference responses again. This tests binary rollback from independently
restored artifacts rather than the earlier host-local fixture. Its controller is
`/data/qualification/legacy_restore_after_fresh.py`; results will be
`/data/disaster-current-20261017/legacy-restore-result.json`. It is queued, not
passed. Before clearing fixture overlays or preparing this machine for live
traffic, require **both** restore result files to pass and both drill processes
to have exited.

## Parallel cold qualification

On October 17, before any replacement cold trial had begun, the run switched to
the protocol's previously frozen four-machine amendment. Every machine has the
same candidate image, four shared CPUs, 2048 MiB, no swap, and a 60 GB volume.
Round `r` belongs to shard `r % 4`; its SQLite control, compact trial, and second
SQLite control remain together. Workload, randomization, CPU-credit/throttling
guards, and acceptance thresholds are unchanged. All 180 trials must pass
validity and be assessed together. The same arrangement is queued for exact
legacy-reader comparison only after the combined cold gate passes.

Additional temporary resources are listed in the
[campaign evidence](../evals/compact-cancel-fixed-campaigns.json). Their
volumes have scheduled snapshots disabled. Artifact preparation verifies full
sizes and hashes on each machine. Cleanup must remove these three machines and
volumes, plus the original latency machine, after qualification evidence is
preserved. Do not delete the app if the recovery machine has become the serving
backend; retain that machine and its volume through the production rollout gates.

### Worker reconciliation — October 17, 05:59 UTC

The later stats deployment `9087d1fd-1364-409e-a6ef-6b5d737ff57c`
overwrote the freshness parser fix. Version
`2564108f-c5c7-4d2b-9c78-e5031bb4e232` restores it while preserving that
exact stats page. The isolated release differs from `/tmp/typeahead-stats-release`
only in `freshness.ts` and its test; no search route or PDS changes were shipped.
All 97 unit tests, typechecking and lint of the affected files passed.

The initial smoke run failed the labeled-actor inclusion check for
`jcsalterego.bsky.social`. Follow-up exact-handle queries returned the actor
from both public aliases and the unchanged legacy backend with identical bodies.
A complete smoke rerun passed; the original transient miss is not explained or
erased. Admin override checks, Turso query-plan checks and comparison remained
skipped in this isolated release. Readiness verified legacy build
`b1792141282-189b` with 260-second lag; public freshness reported healthy.

Use this reconciled version as the reverse-routing base, refreshing the active
deployment immediately before staging a binding change. The earlier reverse
command was a dry run only; no route reversal has yet been exercised.
See [deployment evidence](../evals/compact-worker-reconciliation.json).

### Replacement warm gate did not pass — October 17, 06:04 UTC

The final candidate `cc2d5a1` completed all 180 trials across 60 rounds.
The unchanged assessment [did not pass](../evals/compact-cancel-fixed-warm/gate.json):
four primary phases passed, while mixed-read p95 was inconclusive
(median paired ratio 0.97065, one-sided 95% upper bound 1.03543; required <=1).
No secondary regressions were flagged. All trials passed their correctness,
read-only zero-physical-I/O and clean-shutdown guards. The full
[raw evidence](../evals/compact-cancel-fixed-warm/bench-results.json.gz) is retained.

This blocks cutover. The older candidate's passing run does not qualify this
binary, and the new run must not be selectively repeated until it passes.
Mixed-read median p95 was 2.091 ms for compact, 2.078 ms for baseline and
2.182 ms for baseline-control; these absolute medians do not override the paired
acceptance criterion. Cold qualification and independent recovery/diagnostic
work continue to identify any additional issues before choosing a correction.

### Mixed-read investigation and native reload queue — October 17, 06:12 UTC

The warm harness pins the generator to CPUs 8/10/12/14 and its serving child
to CPUs 0/2/4/6. The legacy-rejection drill overlapped the warm campaign from
05:41:25 to 05:41:39 UTC (7.789 CPU seconds), during trial 59. Its launch used
CPU 0, so the earlier claim that it avoided the warm server's cores was wrong.
This short overlap does not establish why the complete mixed-read gate was
inconclusive; no trials are excluded and no pass is inferred.

The bounded native reload development diagnostic admitted the compact generation
in 68.208 seconds while completing 655,078 exact matching responses. Its two
controls and overall completion remain pending. Unit
`typeahead-queue-native-reload-formal-20261017` waits for that diagnostic to exit
and verifies its complete three-role correctness/admission record before starting
the already frozen 20-round formal protocol. It does not select by diagnostic
latency or modify thresholds. Formal execution is capped at 2 GiB, no swap, a
private 64 MiB temporary filesystem and six hours; it uses a separate evidence
directory. The warm gate still independently prevents cutover.
The [queue controller](../evals/compact-native-reload/queue-formal.py) fails closed
if any development run is missing or invalid.

### Empty-volume recovery and legacy rollback passed — October 17

The final candidate restored the protected current-corpus pair from R2 on the
fresh recovery volume, admitted `compact-b1792141282-189b` in 1,929.888 seconds,
and matched all 8,000 reference responses after 60 mutations. It shut down cleanly.
The queued unmodified deployed reader then admitted the restored legacy twin in
1.003 seconds and matched the same 8,000 responses with the overlay preserved.
The old reader required its documented forced shutdown. Both controllers and
serving processes have exited; their fixture overlay remains isolated.
[Evidence](../evals/compact-fresh-volume-recovery.json) records both results.

This verifies independent artifact restoration and binary/format rollback.
It does not verify live upstream catch-up or traffic reversal. Before preparing
this machine for serving, archive the evidence, remove only its fixture overlay,
and replay real changes from the protected snapshot watermark with pool size 8.
Production traffic remains on the original backend.

### Native reload diagnostic complete; formal run active — October 17

All three bounded diagnostic runs completed with exact matching responses and
admission within the fixed 180-second window: compact 655,078 responses /
68.208-second admission, baseline-control 593,264 / 47.830 seconds, baseline
587,421 / 48.562 seconds. Clean teardown completed for each.
The [full diagnostic record](../evals/compact-native-reload/diagnostic.json.gz)
is excluded from formal acceptance. The verified queue started
`typeahead-native-reload-formal-20261017`; it uses the frozen 20-round protocol
and separate `reload-release/formal-bounded` output.

### Recovered compact XRPC contract passed — October 17

The final candidate served the recovered current-corpus compact snapshot with
`READ_POOL_SIZE=8` through the current Worker source
(`2564108f-c5c7-4d2b-9c78-e5031bb4e232`) in a local isolated runtime.
All 224 responses passed: seven queries including Unicode, four limits, both
aliases, repeated requests and both alias orderings. Each body matched the native
backend exactly; CORS and the 60-second cache behavior passed.
[Evidence](../evals/compact-recovered-xrpc.json.gz) retains the responses.

The native listener is IPv4-only, so an ephemeral byte-forwarding bridge connected
the Fly IPv6 tunnel to it. The initial local Worker startup used the module's
named constant exports directly and failed; the default-entry wrapper corrected
that harness error without changing production source. Fake Turso credentials
prevented live access; expected background statistics-write network errors are
not claimed as an error-free integration run. Live catch-up is still untested.
Both local Worker instances exited after testing; the native test server, bridge
and tunnel were explicitly stopped. The restored volume and fixture overlay
remain preserved for the next preparation step.

### Binding switch and reversal rehearsed — October 17

An isolated disposable Worker verified `versions secret put` stages a new
`SEARCH_BACKEND_URL` without changing active traffic, and `versions deploy
NEW_VERSION@100% --yes` switches the binding. **Reversal with `versions deploy
BASE_VERSION@100%` failed with Cloudflare error 10220** because the secret changed.
The earlier dry run could not detect this restriction.

The working reversal is:

```sh
bunx --no-install wrangler rollback "$BASE_VERSION" --yes \
  --message 'Restore verified legacy search backend'
```

Wrangler's rollback handler retries with Cloudflare's explicit secret-rollback
confirmation. In the rehearsal, only `SEARCH_BACKEND_URL` changed. Both the
returned binding value and the final 100%-baseline deployment were verified.
The disposable Worker was deleted and API absence verified.
[Evidence](../evals/compact-routing-rehearsal.json) records the failed and working
commands. This verifies the control plane; real backend readiness remains an
independent gate. No production routing changed.

For the actual switch, refresh active and latest-uploaded versions immediately
before staging; require the intended current code at 100% and no unexpected
latest upload. Save that version as `BASE_VERSION`, with both backend readiness
results and the exact existing backend URL. Preserve all unrelated bindings.
After staging, verify the old backend is still active before deploying the new
version. Before unattended rollback, verify no intervening unrelated secret/code
change occurred; otherwise inspect the changed-secret list without `--yes` and
preserve those changes explicitly. A rollback success message alone is insufficient:
verify the active version, direct legacy readiness, public search and alias bodies.

### Worker changed again during independent stats work — October 17

Fresh control-plane inventory shows active/latest Worker
`9c01bcf3-314c-453f-93a0-c76954c8419e`, deployment
`cb5b4c86-3ade-4c98-9f75-59558ccb6f98`, at 100%. The stats session's README
identifies this deployment; `/tmp/typeahead-stats-release` contains its newer
stats page but the older freshness parser. The compact-ID prerequisite is no
longer claimed as live. Existing production legacy IDs continue to parse.

Do not deploy the older `worker-current-release` archive over the new stats page,
and do not include the root checkout's newer unapproved visual proposal. Before
cutover, reconcile only the freshness parser with the latest approved release
archive, verify the source diff and deployment, then refresh the reverse target.
The 224 XRPC checks remain evidence for the explicitly recorded `2564108f…`
source, not a blanket verification of subsequent deployments.

### Private replacement preparing live replay — October 17, 06:32 UTC

After both recovery drills and the alias checks passed, machine `781e732c264678`
was updated in place to run the pinned final image with pool size 8 and a fresh
overlay at `/data/live-overlay/overlay.db`. The fixture overlay remains under
`/data/disaster-current-20261017/overlay.db` and is not used for live replay.
The independently restored snapshot pair remains the input. The R2 namespace
is pinned to the protected checkpoint; the root production pointer is untouched.

Turso credentials from the existing encrypted store were staged without
restarting qualification machines; only the recovery machine was updated.
No public services are configured. The matching existing Fly-owned
`ADMIN_SECRET` was subsequently staged after a non-mutating invalid-body probe
returned the expected HTTP 400 from the legacy backend. It is not active on the
currently running replacement; the planned restart will apply it and verify the
same probe there before cutover. Its restart policy is on-failure, at most ten retries.
[Machine configuration](../evals/compact-private-replacement-config.json) contains
no credential values. Curl was installed only for private inspection; it is not
a runtime dependency or a change to the pinned image.

The [bounded admission observer](../evals/compact-observe-private-replacement.py)
requires the exact build, attached/readiness state, nonempty corpus, <=600-second
known sync lag, completed overlay compaction, the rank-one probe, and a successful
full sync-pass log after this boot. A recent partial-page watermark alone cannot
prove that the unsearchable/tombstone phases completed. It runs for at most two
hours and records failed/unknown observations; a deadline does not restart the
machine. Initial checks correctly rejected the unattached snapshot. Live catch-up
has not yet been claimed complete.

### Following scheduled publications after recovery — pending

The private replacement intentionally reads the protected checkpoint subpath and
`compact.json`. Leaving that configuration in production would pin it indefinitely;
a successful publisher run alone would not demonstrate end-to-end promotion.

After serving cutover and the first scheduled compact publication, verify the root
`latest.json` names the new compact manifest and its same-watermark legacy twin,
with all hashes and uploads complete. Keep the old backend synchronized and ready
for traffic reversal. Only then remove the recovery bucket suffix and set
`INDEX_LATEST_KEY=latest.json` on the replacement, retaining its image, volume,
live overlay, and read-pool configuration. The
[pending follow-publisher configuration](../evals/compact-follow-publisher-config-pending.json)
is a template, not deployed state; refresh it against the machine before applying.
Do not apply it while root `latest.json` still names the old legacy generation.

Require native admission of the scheduled compact build, a full successful overlay
sync, compaction completion, matching aliases, moderation/ranking checks, and
healthy serving before counting the scheduled cycle complete. Protect and readback
verify this new pair before cleanup. Subsequent polls must target the root pointer;
record the final machine configuration as evidence. For publication rollback, use
the new generation's verified legacy twin; do not combine an older snapshot with
an overlay pruned past its watermark.

### Latest approved Worker preparation — October 17, 06:56 UTC

Active Worker `f109afd8-33ea-416b-ac02-6ebf2bed0028` now serves 100% in
deployment `e360206e-3f74-4bca-b87f-42a2156bc175`. Its approved source archive
is `/tmp/typeahead-stats-approved-release`; the older stats archive is not the
current deployment. The unrelated local pill-consolidation proposal is excluded.

The [prepared reconciliation](../evals/compact-worker-f109-preparation.json) changes
only the freshness parser and its test relative to this approved source, preserves
the exact stats bytes and Wrangler config, and passes 97 unit tests, type checking,
and focused lint. It is not deployed. Refresh active/latest versions again before
shipping; subsequent approved stats work must not be overwritten.

### Qualification scheduling amendment — October 17, 07:06 UTC

Cold shard 0 completed all 45 assigned trials with valid CPU/exit guards; its
results were archived before reuse. To avoid idle machines, the waiting-only
compatibility orchestrator was replaced with a [per-machine pipeline](../evals/compact-compat-pipeline.py).
Each machine starts legacy correctness and compatibility only after its own exact
45 cold trial indices are complete and valid. No running cold process was stopped.
The old waiter had no child processes when replaced.

This changes scheduling only: binary, inputs, queries, randomization, round-to-machine
assignment, CPU guards, sample counts, and acceptance thresholds remain unchanged.
Compatibility uses its existing isolated namespace and output directories. Both
combined 180-trial gates still must pass; a shard completion is not acceptance.
Native reload qualification on HeavyPad and live overlay replay remain separate.

### Live replay passed; isolated public restart underway — October 17, 07:16 UTC

The fresh overlay completed its full live pass: 165,630 updated actors, 209
unsearchable actors cleared, and 8,217 deletions applied. The final private
admission check had 15 seconds of lag and ranked `zzstoatzz.io` first. See
[full-pass evidence](../evals/compact-private-complete-sync.json) and
[admission evidence](../evals/compact-private-live-ready.json).

The replacement was then updated with the staged existing admin credential and
public service configuration. Its image, volume, overlay paths, read pool, and
protected snapshot pointer are preserved. The isolated address is
`https://typeahead-search-compact.fly.dev`; production routing is unchanged.
[Configuration and allocated addresses](../evals/compact-public-replacement-preparation.json)
record the update. The credential passed the
[authenticated invalid-body probe](../evals/compact-replacement-admin-credential-check.json)
without mutation. [Restart admission](../evals/compact-public-restart-ready.json)
passed at 07:20 UTC: exact compact build, complete new sync pass (354 updates,
34 clears, 37 deletes), 20-second lag, and exact-handle rank one. Native attachment
after restart took about 207 seconds. HTTP 200 during that loading interval
was not treated as readiness. `just bench-ranking` against the isolated public
URL passed every acceptance check; [full output](../evals/compact-public-ranking.log.gz)
is retained. The no-stress engine comparison is running; current-Worker alias
checks are prepared to run afterward.

### Deployed-reader performance comparison failed — October 17, 07:24 UTC

The unmodified deployed legacy binary `f82400c…` passed the initial serialized
10,060-response correctness comparison against candidate `758d458…`, but crashed
in trial 1 (`baseline-control`) during the eight-client phase. The server logged
a segmentation fault followed by recursive fatal-handler frames; the HTTP client
received a disconnect. The cleanup collector then encountered the already-exited
process. CPU throttle counters stayed zero. These logs do not establish the
underlying cause of this particular crash.

[Failed trial, resource guards, and server log](../evals/compact-deployed-compat-failure/)
are preserved. This is not a compact-candidate crash, and it is not a passing
legacy performance comparison. No retry, query exclusion, or patched baseline
was substituted. The waiting compatibility orchestrator had no active child
processes and was stopped to prevent other shards starting this now-incomplete
campaign. The three active cold controllers and native reload experiment were
left untouched; collect their complete results independently.

### Isolated public engine and current-Worker aliases — October 17, 07:30 UTC

The complete `just bench-search --url https://typeahead-search-compact.fly.dev`
run finished with stress disabled: 44 queries, three runs per query. Median of
per-query p50 was 42.15 ms for cache-busted requests, 40.5 ms warm, and 98.05 ms
for the side-by-side Bluesky oracle. This network-level comparison does not
replace the frozen same-data non-regression gate.
[Report](../evals/compact-public-engine-bench.json.gz) and
[log](../evals/compact-public-engine-bench.log.gz) retain all results.

Mean oracle overlap was 49.8%, below the aspirational 75% target. Display-name
query `paul frazee` returned no actors; its exact handle did resolve. Two
serialized production-baseline probes produced byte-identical bodies for those
queries, as recorded in [the comparison](../evals/compact-display-name-comparison.json).
Do not claim the run proves all search-quality targets are met. The separate
canonical ranking suite passed its acceptance checks.

The prepared `f109afd8…` Worker source plus the freshness parser passed
[224 alias/CORS/cache checks](../evals/compact-public-f109-xrpc.json.gz) against
the live isolated replacement: seven queries, four limits, both alias orders,
and repeated cache reads, with exact backend bodies. Both temporary local
Workers exited after the checks. This source is tested locally, not deployed.

### Complete cold campaign passed — October 17, 07:37 UTC

All four shards finished: 180 valid trials, 60 matched rounds, exact candidate
`cc2d5a1`, pool size 8, four shared CPUs and 2 GiB per machine. The
[combined gate](../evals/compact-cancel-fixed-cold/gate.json) passes all five primary
read phases, with no secondary regression flags. Every per-trial CPU/exit guard
and the full result set are archived beside the gate. No rounds were excluded
or retried. This uses the frozen qualification corpus, not the later live overlay.

Upper one-sided 95% p95 ratio bounds were 0.337 after SQLite-cache restart,
0.823 warm, 0.112 broad, 0.414 with eight clients, and 0.850 during mixed reads.
These bounded-memory results do not replace the separate inconclusive uncapped
warm gate or the unfinished native reload qualification.

Median compact startup was 205.4 seconds versus about 5.4 seconds for the legacy
formats. The cold serving improvement does not erase this admission cost. Use
the already-admitted replacement for routing, retain the ready legacy backend,
and do not substitute a direct in-place restart for the planned cutover.

### Qualification compute stopped after evidence collection — October 17

All four cold-only machines are stopped after their 180 trials and diagnostic
archives were saved. [Machine state](../evals/compact-qualification-compute-stopped.json)
confirms that only the isolated replacement remains started in this app.
[Diagnostic archives](../evals/compact-cancel-fixed-cold/diagnostics/inventory.json)
include each trial's server log, host snapshots, process status, memory summary,
and raw benchmark result. Volumes are retained pending final qualification and
cleanup; this stops compute billing, not volume billing. Production and the
ready replacement were not stopped. Native reload qualification remains active
on HeavyPad.

### Legacy rollback target rechecked — October 17

The [rollback readiness check](../evals/compact-legacy-rollback-readiness.json)
confirmed the unchanged legacy image/machine is started with host status OK,
expected generation, exact-handle rank one, and 45-second overlay lag. Fly event
history also records an unrequested non-OOM exit 134 on October 16 at 18:02 UTC,
followed by restart at 18:05 UTC. These records do not establish its cause or link
it to the isolated concurrent-read crash. Readiness is a point-in-time check,
not a claim that the legacy binary cannot fail.

The public status chart's latest five-minute bucket was about one hour old when
inspected. Its counts cannot establish the current error rate. Refresh direct
readiness and appropriately fresh telemetry before switching or rolling back.

### Reload passed and acceptance exceptions approved — October 17

The frozen matched-reload assessor independently reproduced a pass on all 60
trials (20 rounds). All validity checks passed; p95 upper confidence ratio is
0.967942, p99 0.968945, and CPU/request 0.916238. No secondary regression flags.
The service exited successfully. [Complete evidence](../evals/compact-native-reload/formal-results/gate.json)
includes the compressed raw results and terminal service status.

Nate explicitly accepted the +3.5% warm p95 upper confidence bound and unavailable
unmodified-legacy performance comparison, conditional on reload passing, and
authorized safe cutover with rollback retained, verification, and cleanup. These
are recorded exceptions; the warm assessment remains inconclusive and the crashed
legacy comparison remains incomplete. Other gates and cleanup conditions are
unchanged. Production routing has not yet changed at this checkpoint.

### Production cutover verified — October 17, 19:37 UTC

[Cutover evidence](../evals/compact-production-cutover/cutover-result.json) records
fresh admission for both backends, five before/after alias probes, control-plane
versions, and the expected compact build through public freshness. The full
[post-cutover smoke gate](../evals/compact-production-cutover/cutover-smoke.log)
passed, including authenticated admin operations and live database query plans.
These few latency probes are operational checks, not a new benchmark campaign.

Worker `44402a8f-5a39-4079-9bc5-f63ac747f366` first deployed the tested freshness
fix over the approved f109 source and passed smoke. Version
`0b97a76f-4836-4756-97a0-d3fa5b4a210f` changes only `SEARCH_BACKEND_URL` to
`https://typeahead-search-compact.fly.dev` and serves 100% of traffic. Preserve the
running legacy backend and use the rehearsed rollback command with `44402a8f…`
after checking for intervening changes. Immediate checks passed, so no rollback
was executed. Both backends retain their existing volumes.

The earliest full daily observation completes October 18 at 19:37 UTC. Cleanup
still requires the complete scheduled compact publication and fresh protected
paired artifacts, not just this elapsed interval. Retain protected rollback
artifacts through at least October 24, 19:37 UTC; confirm the newly protected
snapshot remains within its seven-day replay window throughout that period.

### Compact publisher registered — October 17, 19:44 UTC

Source and configuration landed on `my-prefect-server` main `e9c62cd` on both
remotes. Full local checks passed: 742 tests, 3 skips, lint/format/type checks,
and web checks. A missing local-source result now fails explicitly before
publication, resolving the typecheck failure. [Registered configuration](../evals/compact-production-cutover/publisher-registered.json)
confirms compact format, tag `compact-candidate-cc2d5a1`, exact package pin,
`INDEX_QUIET=0`, concurrency 1 / CANCEL_NEW, and unchanged cron.

[Queued scheduler runs](../evals/compact-production-cutover/publisher-scheduled-runs.json)
contain the new parameters; next is October 19 09:00 UTC, run
`2ac84d00-c085-42bc-8f23-b8f3b0c7ab3a`. Registration replaced the old queued
SQLite-default runs. Re-query before observing because later registrations may
replace their IDs. This does not yet prove a successful scheduled publication.
The initial Tangled CI attempt failed to pull its container (missing Content-Length)
before executing checks; targeted registration followed the complete local checks.

HeavyPad resolved the exact source tag through Tangled HTTPS. The configured
GitHub fallback returned repository-not-found, so it is not counted as redundant
source access. Protected source/runtime archives remain independently verified.

Initial bounded live request-counter reads showed no 5xx statuses; [evidence](../evals/compact-production-cutover/initial-request-counts.json)
covers the specified five-minute buckets, not the required full daily window.

### Follow-up and CI status — October 17

The same container-image pull failure recurred on the explicit CI retry
`3my3u6jgez622`, before tests or registration. Local full checks and explicit
registration evidence remain valid; CI is not represented as passed.

Thread follow-up `finish-typeahead-cutover-verification` runs at 14:45 America/Chicago
starting with the next daily occurrence. It must verify the full observation window
and real scheduled publication, complete the root-pointer transition and recovery
checks, then remove only unneeded resources and disable itself. This is finite
rollout work, not a replacement for normal production monitoring. The older paused
health automation belongs to another investigation and was not changed.

### Publisher CI repaired and passed — October 17, 20:00 UTC

[Pipeline `3my3ukq4ems22`](../evals/compact-production-cutover/publisher-ci-passed.json)
on `my-prefect-server` main `e4275b5` completed successfully through deployment
registration. CI ran 741 passing tests with four skips, lint/format/type checks,
and web checks. The real converter integration was already run explicitly during
local qualification; CI's default environment skips that optional integration.
[Complete CI log](../evals/compact-production-cutover/publisher-ci.log.gz) is retained.

The repair moves the workflow to the existing NixOS microVM engine, avoiding the
Nixery manifest response rejected by Docker for missing Content-Length. Three
real Git tests now inherit the runner PATH instead of hiding Nix-installed Git
behind hard-coded macOS/Linux directories. No checks or registration steps were
removed. A direct HeavyPad Docker pull reproduced the old failure before repair.

Live Typeahead deployment readback confirms package pin `e4275b5`, compact format,
qualified tag, unchanged cron, and `INDEX_QUIET=0`. CI registration regenerated
queued runs with compact parameters; [current schedule evidence](../evals/compact-production-cutover/publisher-scheduled-after-ci.json)
identifies October 19 09:00 UTC run `bbd0eb3c-9d3e-4387-b8f2-e983a531fb56`.
The prior CI infrastructure blocker is resolved. Scheduled publication, daily
observation, pointer transition, and recovery-gated cleanup remain outstanding.

### Daily traffic review — October 18

The exact October 17 19:40 to October 18 19:40 UTC window contains records in
all 288 five-minute buckets: 14,902 search HTTP 200s, 205 search HTTP 429s, ten
search HTTP 400s, and no HTTP 5xx responses across endpoints. The 429s occurred
in eight buckets (03:00 and 13:35–14:05 UTC); the Worker has an explicit search
rate limiter. Caller attribution was not established, so do not describe every
request as successful or infer capacity from this observation. See
[daily counters](../evals/compact-production-cutover/daily-observation-oct18.json).

Fresh readiness checks passed for both backends: compact overlay lag 226 seconds,
legacy 195 seconds, expected paired build IDs, exact-handle rank one, and
12,560,923 snapshot actors. Compact snapshot age was 212,792 seconds, inside the
seven-day fresh-restore window. Four qualification machines remain stopped and
the serving compact machine remains started. No serving configuration changed.

The October 17 live performance investigation found that compact has no
LOGFIRE_TOKEN and emits no compact service records in Logfire. The full-day
counter review therefore does not prove full-day latency or backend error
coverage. Restore telemetry using an availability-preserving transition and
verify actual compact spans; retained legacy telemetry is not a substitute.

Publisher registration refreshed on October 18 at 15:12 UTC after unrelated
my-prefect-server changes. Its package pin is now
`@1f324326d4b3a9986149bb457c58ed5f96719285`; the index flow and its deployment
configuration are unchanged from the previously verified pin. Readback confirms
the qualified tag, compact format, active unchanged schedule, and auto-scheduled
October 19 09:00 UTC run `e477782a-f15d-472f-93f3-d10aef781b23`.
Earlier queued run IDs no longer exist; always refresh before checking progress.

The scheduled publication has not happened. Root-pointer transition, protected
fresh paired readback/recovery, production deployment recipe reconciliation and
cleanup remain gated. The finite follow-up stays active. No resources were
deleted and no additional acceptance exception was taken.

### Compact Logfire repaired — October 19

Nate requested immediate repair without downtime. The current Worker had advanced
to `4df98ae7-5ad4-4f04-8797-99ad50a43d5e`; the repair preserved that version's
source/bindings and changed only SEARCH_BACKEND_URL for each transition. Do not
roll back to the original October 17 source now.

The existing Fly-owned LOGFIRE_TOKEN was transferred privately from legacy to
the compact app's secret store, staged without deploying the four stopped
qualification machines. Traffic moved to the verified legacy backend with Worker
`0ed67e0c-070e-4558-99b1-819d35c2d5ef`, then only machine `781e732c264678` was
updated from its freshly read exact configuration. Configuration equality was
verified afterward: same image, volume, checkpoint, overlay and service settings.
Compact loaded its snapshot while legacy served production. An initial Python
freshness request received HTTP 403; curl confirmed healthy legacy freshness
before the machine update proceeded. Continuous public search probes succeeded.

After native readiness and 30 successful native searches, Logfire returned actual
`search` spans under `typeahead-search-compact`, attributed to the qualified
cc2d5a1 image digest f085469e…, with sample_rate=10. See
[span evidence](../evals/compact-logfire-repair-spans.json). Startup logs alone were
not used as admission evidence. Future replacement admission must require both
the telemetry credential and a verified search span from the expected image.

Traffic returned to compact with Worker `fe998f27-df76-47dc-bd58-ad6d92fcafbf`.
Public freshness passed with 26-second lag; five canonical/alias pairs were
identical. Legacy remained ready at 88-second lag. The current rollback version
is `0ed67e0c-070e-4558-99b1-819d35c2d5ef`: after checking for intervening changes,
use the rehearsed `wrangler rollback VERSION --yes` command, then verify public
freshness/search. Old source rollback targets are historical only.

The token remains in Fly's existing secret domain; no rotation or new credential
was introduced. The root-pointer, scheduled-publication and recovery cleanup
gates are unchanged by this repair.

Final verification: full smoke suite exited 0, all 157 continuous public search
probes succeeded, and Logfire returned three more compact search spans after
traffic returned. [Repair results](../evals/compact-logfire-repair/result.json)
retain probe history, native readiness, alias/freshness summary and smoke output.
No observed search downtime occurred; sampled probes are not a claim about every
individual user request. All serving and rollback resources remain intact.

### Scheduled compact publication confirmed — October 19, daily follow-up

Run `e477782a-f15d-472f-93f3-d10aef781b23` completed at 11:25:18 UTC after
starting at 09:00:13 UTC with compact format and the qualified source tag.
[Publisher logs](../evals/compact-production-cutover/publisher-oct19-log.json)
record verified paired uploads and root latest.json publication. Local pruning
was conservatively skipped because the public freshness URL was unreachable
from its Python client; no serving generation was deleted.

Independent R2 manifest reads confirm root latest.json equals the immutable
compact manifest for `b1792400507-4df8-px05`. The legacy twin is
`b1792400507-4df8`; both have source watermark 1792400505 and 12,620,975 actors.
The legacy database is 15,736,393,728 bytes and the compact prefix map is
7,019,524,096 bytes (plus the 16,384-byte database).

Protection and full streaming checksum readback were launched locally via
`/Users/nate/.local/share/typeahead-experiments/publication-20261019/protect.py`.
The destination is
`releases/compact-candidate-cc2d5a1/checkpoint-b1792400507-4df8/`.
This is **pending**, not verified: inspect `protect.log` and require
`protected-readback.json` with verified=true before counting this gate. The
script copies immutably and only publishes protected pointers after every
artifact passes complete size/hash readback. Do not start a duplicate while
the existing process is running.

HeavyPad SSH requested renewed Tailscale authentication; renewal was requested
from Nate. Local R2 verification continues independently. Production remains
on the protected October 16 generation, healthy; legacy rollback passed with
295-second lag. The serving volume has 30,995,352 KiB available, enough for the
new pair. Worker fe998f27… is still active. No root-pointer transition, recovery
admission, qualification deletion or final cleanup has been performed yet.

### Root-pointer transition in progress — October 19 after SSH renewal

HeavyPad access was renewed. The scheduled pair passed fresh full-size rollback
qualification with the exact cc2d5a1 binary (SHA 758d458b…) and retained legacy
binary (SHA f82400c6…): 8,000 exact response comparisons per stage and 60 mutation
checks, including compact-to-legacy format and original-binary rollback.
Evidence is archived in [the scheduled-pair rollback result](../evals/compact-scheduled-publication-20261019/rollback-result.json)
with its executed harness and immutable manifests.
The systemd user unit `typeahead-scheduled-rollback-20261019` completed successfully.

Traffic temporarily moved to legacy using Worker
`e4d942d2-9d78-446c-8e7e-1ae979800565`, preserving the current source. Compact
machine 781e732c264678 was updated from a fresh config read with only init changed
to `/app/typeahead-ingester` and INDEX_LATEST_KEY changed to latest.json. Its R2
bucket is no longer suffixed with the old checkpoint. Native admission of the
new generation is still pending; do not route traffic back based on old-build
readiness. No qualification resources have been deleted.

Local controller `/Users/nate/.local/share/typeahead-experiments/publisher-transition-20261019/transition.py`
records its Worker versions, refreshed machine config and readiness results.
It only waits for the expected b1792400507-4df8-px05 build and lag <=300 seconds;
it does not automatically return traffic. Public probes run alongside it.
Logfire confirms native download from root builds/b1792400507-4df8/index.db.

The independent protected-copy/readback process is still running locally.
`publication-20261019/start_recovery.py` waits for verified=true, then copies that
evidence to HeavyPad and launches user unit
`typeahead-scheduled-empty-restore-20261019`. The recovery script requires the
passing rollback result, starts with an empty directory, downloads from the new
protected prefix, checks exact results/mutations, and records its result under
`/home/stoat/typeahead-release-20261019/empty-restore/result.json`. Do not count
this check until its terminal result passes. Check existing processes/units
before relaunching anything. Production remains protected by the live legacy route.

At 20:18 UTC the legacy twin download on Fly completed; native validation is still
in progress and the old compact snapshot remains attached. Overlay projection
was slow during the transfer, then advanced again at 20:12 UTC. This does not
satisfy the <=300-second catch-up gate. Production remains on legacy and public
probes have observed no failures. Logfire continues to receive compact search
spans from direct readiness probes.

Two attempts to prepare a separate bootstrap root stopped before any machine
configuration changed because the downloaded file changed from its temporary
name to its final name. The completed staging database is now hardlinked into
`/data/publisher-current-20261019/index/generations/b1792400507-4df8/index.db`
to preserve the download if a retry becomes necessary; this is an unverified
cache, not an admitted recovery generation. No manifest or active record was
installed there and INDEX_BUILD_ROOT remains `/data/disaster-current-20261017`.
Let current native validation finish before restarting. The empty bootstrap
root does not change production behavior or justify bypassing checksum checks.

At 20:20:51 UTC native validation admitted the new legacy twin into the generation
cache and began fetching the compact prefix. Overlay lag was still too high.
At 20:22 UTC the compact machine was restarted with only INDEX_BUILD_ROOT changed
to `/data/publisher-current-20261019`, using the native-written legacy manifest
and hardlinked database. Native validation repeats the cached twin's checksum;
there is no active record in the new root, so sync waits for admission and feeds
its watchdog rather than projecting against the old snapshot during downloads.
The old root and live overlay remain intact. `bootstrap-config.json` and
`bootstrap-update.log` retain this transition locally.

An intervening Worker deployment changed the active version to
`a6fa2302-5e37-46b5-8f02-244aac16de0e`. The initial version guard stopped before
mutation; fresh public freshness confirmed healthy legacy routing, and the
current version was retained. Do not roll back to e4d942d2 without reconciling
these newer Worker changes. Return routing must clone the then-current version.

The protected legacy database, legacy manifest and compact database have now
passed complete R2 checksum readback. The compact prefix copy/readback is still
pending, so the protected pair is not yet complete and empty-directory recovery
has not started.

Probe audit correction at 20:26 UTC: one probe at 20:02:24.923 UTC raised
`list index out of range` after parsing the HTTP response, consistent with an
empty `actors` list. The adjacent probes passed. The harness did not retain the
failed response body/status, so do not classify this as a proven HTTP outage or
claim zero probe failures. Logfire for 20:02–20:03 UTC returned no warnings/errors;
its 1-in-10 sampled searches cannot identify the exact request. Investigate this
transient correctness failure before declaring transition verification complete.
Public freshness was healthy at the audit. The existing finite follow-up now
runs every 15 minutes while native admission and protected readback progress;
it remains quiet unless there is a meaningful change.

### Protected pair verified — October 19, 20:40 follow-up

The fresh protected pair passed complete size/SHA-256 readback for all five
artifacts and both protected pointers. [Readback evidence](../evals/compact-scheduled-publication-20261019/protected-readback.json)
records the immutable prefix `releases/compact-candidate-cc2d5a1/checkpoint-b1792400507-4df8`.
HeavyPad user unit `typeahead-scheduled-empty-restore-20261019` started at 20:36
UTC and remains running; its log is `empty-restore/restore.log`, not server.log.
No passing restore result exists yet. Fly finished downloading the compact prefix
and is validating it; native admission and overlay catch-up remain pending.
Legacy public freshness is healthy. No resource deletion or routing change occurred.

The original bounded public probe completed. An additional bounded 20-minute
probe, `publisher-transition-20261019/probe-detailed.py`, now retains status,
headers, elapsed time and failed response bodies in `public-probes-detailed.jsonl`.
The earlier isolated empty result took roughly three seconds before the next
probe interval; the existing Worker has a three-second backend timeout that can
return an uncached empty list. This is a possible explanation, not proof of the
failed request's cause. Logfire has no matching Worker request record. The
correctness anomaly remains unresolved; no gate is waived.

### Native admission and empty-directory recovery passed — October 19, 20:57 UTC

Fly attached b1792400507-4df8-px05 at 20:41:48 UTC. The native active record
retains b1792400507-4df8 as its same-watermark legacy previous generation.
Logfire returned sampled search spans after attachment from the unchanged
qualified image, including 20:55:36 UTC with exact_hits=1 and results=3.
The direct ranking suite passed all acceptance criteria against the new build;
[output](../evals/compact-scheduled-publication-20261019/ranking.log) is archived.

HeavyPad's empty-directory recovery completed successfully with clean shutdown:
[terminal result](../evals/compact-scheduled-publication-20261019/empty-restore-result.json)
records native admission in 1,181.74 seconds, 8,000 response comparisons and
60 mutations. This used the protected R2 pair and the qualified binary. The
isolated upstream fixture does not establish live catch-up; that gate remains
on the serving machine, with lag about 2,185 seconds at 20:55. Native overlay
compaction reported 1,595,942 rows after 806.8 seconds; replay continues advancing.
Do not treat these completed recovery checks as permission to waive the lag gate.

The richer public probe had 163 successful checks and no failures at 20:55:31;
the earlier single empty-result anomaly remains unexplained. Production remains
on legacy and no routing or resource mutation occurred in this follow-up.
README now reflects admitted generation/recovery versus pending live catch-up.

Fresh deployment readback found another intervening Worker version:
`86590349-5a6e-42c1-a6de-4c5d8835ff3b` at 100%. Public freshness still confirms
healthy legacy build b1792141282-189b (85-second lag). Preserve this newer source;
local transition state contains an older version and must not be used blindly.

At 21:10 UTC native overlay lag decreased to 1,567 seconds; compaction reported
2,595,942 rows processed at 21:06:50 UTC and remains in progress. Public legacy
freshness passed (214-second lag). The richer bounded probe completed all 240
checks successfully; [summary](../evals/compact-scheduled-publication-20261019/public-probes-followup.json)
retains its interval and evidence path. This does not erase the earlier failure.
The finite follow-up prompt now reflects completed recovery/admission gates and
pending live catch-up, avoiding duplicate restoration jobs. README's pending
catch-up status remains accurate; no deployment or resource change was made.

### Scheduled generation returned to production — October 20, 00:50 UTC

Fresh compact readiness passed with 54-second lag; legacy rollback passed at
121 seconds. Existing Worker 86590349… was cloned with only SEARCH_BACKEND_URL
changed, preserving intervening source changes. Worker 6a814dee… now serves the
scheduled generation with root latest.json, unchanged qualified binary and the
original overlay. Public freshness passed at 70-second lag. Five canonical/alias
response pairs were byte-identical, and authenticated smoke passed before and
after routing (including admin CRUD and live Turso query-plan checks). Optional
Bluesky comparison was not run; the direct ranking suite already passed.
[Return evidence](../evals/compact-scheduled-publication-20261019/return-result.json)
and both smoke logs are archived. Fresh compact Logfire spans from the qualified
image were verified at 00:48:21 and 00:48:25 UTC before routing.

All four stopped qualification machines and their four 60 GB volumes were
removed after fresh inventory matched the explicit allowed IDs and the protected
pair's empty-directory restoration passed. [Cleanup record](../evals/compact-scheduled-publication-20261019/qualification-cleanup.json)
records the IDs and verifies absence. The production app, production volume,
legacy app/machine/volume, protected R2 pair, source/image archives and old serving
roots remain intact. No recovery path depends on the removed qualification disks.
The production volume has not been downsized.

`services/fly.search-compact.toml` records the qualified image and live environment,
mount, sizing and HTTP settings; Fly config validation passed. The legacy config
is explicitly marked rollback-only. A plain Fly deploy does not provide the
Worker routing/readiness protection, so `just deploy-search` remains guarded.
For updates, read fresh machine configuration, verify healthy legacy, clone the
current Worker to route legacy, update only the serving machine, wait for expected
build and <=300-second lag, and clone the then-current Worker back. Run full smoke
with existing credentials and reverse to legacy if it fails. Preserve staged
secrets and unrelated configuration when refreshing the machine.

The earlier single empty public result happened while legacy served. Its cause
is not established; the richer subsequent 240-probe campaign passed. No claim
of zero observed failures is made. A further bounded probe records the return in
`publisher-transition-20261019/public-probes-return.jsonl`. Source/PR/worktree
cleanup remains; keep the finite follow-up active until reconciled.

### Source reconciliation — October 20, 00:56 UTC

Prefect PR 3my2cq5txmp22 was closed after ancestry verification confirmed all its
commits through e9c62cd are already on main. Its clean temporary worktree was
removed; only ignored caches/dependencies were present. Main's unrelated changes
were preserved. Typeahead's PR now has a current title/body and round. Local
Worker typecheck, lint, all 97 tests and dry-run bundle passed.

Manual CI pipeline 3mybgflj5mk22 on f796bb5 failed before executing any check:
Nixery image pull reported missing or empty Content-Length. The workflow now uses
the same NixOS microVM engine that passed in the Prefect repository, retaining
its Bun/Node dependencies and all verification commands. Deployment remains
manual; CI only builds/tests/dry-runs. A new pipeline must pass before merge.

### Final observation and source integration — October 20, 01:01 UTC

All 120 bounded return probes passed with exact-handle rank 1; their complete
[records](../evals/compact-scheduled-publication-20261019/public-probes-return.jsonl)
are archived. This is in addition to the earlier 240 passing detailed probes,
not a claim that the original isolated empty result never occurred. Production
and rollback remain retained as specified above.

The repaired microVM pipeline 3mybghh5hol22 passed on 3695f3c, including typecheck,
lint, 97 Worker tests and Wrangler dry-run. [CI result](../evals/compact-scheduled-publication-20261019/ci-result.json)
and full log are archived. The final changes after that verified source revision
are only this closeout documentation and evidence. The PR round includes the CI
repair; Tangled's merge API rejected its approximately 45 MB evidence patch with
HTTP 413. Integration therefore uses a normal non-force Git fast-forward of the
verified branch; the PR is closed only after checking main contains it. The
primary checkout's unrelated dirty files must remain untouched.

The clean Prefect temporary worktree was removed after verifying its commits
are on main. The Typeahead rollout worktree is intentionally retained as the
operational runbook and recovery-harness path referenced by existing artifacts;
other experiment worktrees are left intact because their ownership and remaining
uses are separate from production cleanup. The protected fresh pair and both
source/runtime archives remain available independently of the removed Fly
qualification resources. Retain the existing legacy backend and old serving roots
for rollback, and honor the October 24 minimum artifact-retention deadline.
