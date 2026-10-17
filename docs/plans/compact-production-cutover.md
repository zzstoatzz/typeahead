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
previous release prefix. The exact deployed-reader compatibility campaign is
queued after all cold trials and the strict cold gate pass; it keeps the
unmodified legacy binary and will retain any correctness failure.
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

Pending: replacement warm/cold and compatibility gates, matched reload latency,
serving replacement readiness, route rehearsal, fresh-volume disaster drill,
production cutover, scheduled publication, observation, cleanup. Native full-size
reload correctness, protected publication and independent image/source recovery
have passed as recorded below. Fill in
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
A fresh read of the live deployment shows `MPS_PIN=@523b0fe053fe453559e637c1c5cc800d4de7a3c2`,
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
