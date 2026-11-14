# Sync hardening release — October 26, 2026

Native source: `f67005b` on main, including hardening `06a714c`. Immutable image:
`registry.fly.io/typeahead-search-refresh@sha256:b0492f07c8237d94c854da8530b3af5eab905506e69ff0104fa1ba5d98449bcd`.
The initial attempt used `06a714c` / digest `34c5a61a…`; both image receipts are
retained separately.
Controller source: `79be9aa` on my-prefect-server main and its GitHub mirror.

## Scope

This ports the remaining useful hardening from `d466bba` onto the deployed
certificate-clock repair. It adds no concurrent tasks and leaves `async_limit=8`
unchanged. Transport failures cause HTTP-client recovery after three consecutive
failures. Clean passes and completed HTTP responses feed the watchdog; resets and
transport-only failures do not. Applied pages and nonempty compaction remain
progress. Retries start at 15 seconds and cap at 300 seconds including jitter.
Cancellation remains effective. Historical certificate rotation remains a likely
cause, not a proven historical event.

It does not fix the [HTTP admission starvation incident](../retros/2026-10-26-search-timeouts-and-freshness-alerts.md).
An outage that prevents all HTTP responses can still trigger a watchdog restart.

## Deployment

Rollout [0b3df3ce-daf6-4ef3-b417-254350f3e4ff](https://prefect-server.waow.tech/runs/flow-run/0b3df3ce-daf6-4ef3-b417-254350f3e4ff)
started at 03:04:48 UTC on October 27 (22:04 Chicago, October 26), with only the
per-run `resume_rollout=true` parameter added. The existing published snapshot
`b1792908655-14d5-px05` is reused; there is no corpus rebuild or artifact publication.

The controller now includes the serving Machine's image digest in its idempotence
check. A changed native image can therefore install the same snapshot on the
inactive slot instead of incorrectly returning a no-op. Prepared-candidate
recovery retains the same image and publication checks.

Candidate: `typeahead-startup-20261024`, Machine `080d415b734708`, independent
20 GB volume `vol_4915xomx28k0ml5r`. Preparation uses four shared CPUs and 1 GiB;
final qualification uses two shared CPUs and 1 GiB before routing.
The existing serving Machine `85379da4636338` and its volume remain intact.

The publisher's source and live `INDEX_REFRESH_IMAGE` select the new digest.
The live original-server publisher command is pinned to `79be9aa`. The native
release changed no builder source or deployment defaults beyond that runtime
pin and image. `resume_rollout` is not a scheduled default.

A concurrent, user-authorized Prefect migration is owned by the separate
**New Realtime Voice Chat**. It preserved the image and both controller fixes in
its configuration. At the initial 03:04 UTC pin readback, the original deployment
still had `0 6 */2 * *` UTC active. By 03:31 UTC the migration had disabled those
original clocks; this release did not restore them or change worker ownership.
The in-flight recovery remained on the original server while the candidate
continued replay. Nate authorized direct cross-chat coordination; the migration
chat was given the run/resources and asked to wait for release completion before
moving that worker. The migration owns subsequent worker/API and schedule state.

The first run was canceled at 03:16:08 UTC, before routing changed, after a
freshness qualification bug was observed: `sync_lag_s=-1` (unknown) passed the
old `<600` check. Controller `79be9aa` requires `0 <= sync_lag_s < 600` when
freshness is required. Real HTTP tests cover -1, 0, 599 and 600 seconds. The
prepared candidate remains intact and continues replay; recovery does not repeat
transfer or admission. Prefect logged a cancellation-hook flow-loading warning
about its worker-context `MPS_PIN`; the run reached Cancelled and the independent
candidate and serving Machine were preserved.

Recovery run [a086bdcf-f093-43aa-a369-307847f46daf](https://prefect-server.waow.tech/runs/flow-run/a086bdcf-f093-43aa-a369-307847f46daf)
started at 03:18:02 UTC with controller `79be9aa` and reused the same prepared
Machine and volume. Its lag was known and stale, so qualification correctly
waited rather than routing early.

At 03:58:11 UTC the recovery task exhausted its first 40-minute freshness wait
and began retry 1/3. The same candidate continued replay without a new download,
admission or process restart. This tests prepared-candidate retention during a
readiness timeout rather than treating the timeout as successful qualification.

The initial updated-actor replay finished at 04:18:58 UTC, but the unsearchable
query exceeded its 60-second fetch deadline at 04:19:58. Tombstone cleanup then
returned 17,595 rows and applied 17,576 removals. The completed error span records
168,568 updates, zero clearances and 19 skipped projections in 3,977.94 seconds. PDS readiness stayed unavailable; qualification rejected
it and the recovery run failed at 04:19:25 without changing routing. Its earlier
freshness reading alone did not imply a clean completed replay.

Live EXPLAIN shows the original predicate used `idx_actors_updated_at`.
`COLLATE NOCASE` selects the existing `idx_actors_handle`; the live collated
query returned all 72 matching actors in 2.4 seconds. Even a 500-row-limited
uncollated probe exceeded 20 seconds. No schema/index mutation was performed.
The native SQL fix and a real SQLite index/selection regression follow.

At this point there was no active controller, so the migration chat was released
to move the idle cloud worker while preserving the independent prepared candidate.
Qualification will resume on the newly owned API only after candidate recovery.

The fix passed 154 native tests (four skipped), including real SQLite selector
and query-plan regression, real disconnected sockets/HTTP 503, and HTTPS clock
validation. The Linux ReleaseSafe image was built from `f67005b`; both Tangled CI workflows
also passed (pipeline `3mytfpeiqp522`). MPS source
`c8e7d03` and HeavyPad's target deployment pin the successor digest, preserving
its inactive clock and all other job variables.

The inactive candidate was stopped, updated and started with **only its image
changed**. Machine, volume, environment and four-CPU / 1 GiB preparation size are
unchanged. Its installed snapshot/admission and overlay are retained. The before
and after instance IDs are in the image-update receipt. No serving resource was
restarted. An exact live query with the new SQL subsequently returned 137 actors
in 3.48 seconds (7,728 reads, zero writes); the earlier count of 72 was a prior
point observation, not a constant corpus count.

PDS readiness remains fail-closed until the restarted candidate completes one
clean replay. The old protocol starts from the snapshot watermark while its
policy is unready, even when partial page progress exists. This conservative
replay adds release time; no completion hash was forged to bypass it.

The successor completed a clean replay at 05:36:07 UTC on October 27:
171,641 updates, 133 clearances and 17,897 removals in 3,711.16 seconds,
with no transport failures. PDS policy readiness persisted correctly. Subsequent
cached restarts resumed from the recent watermark rather than replaying the base.

Fly metrics during the production slow-request window at 05:04 UTC showed
1.07 cores of I/O wait and 9.01% memory pressure, with no CPU throttling. A
controlled inactive-slot comparison held two CPUs, image, snapshot, query order
and sync watermarks fixed while testing 1 GiB, 2 GiB, then 1 GiB. All 2,016
response hashes matched in every arm. Repeat-round disk reads were 233 MiB,
zero, then 281 MiB; repeat p99 was 31.0, 16.4, then 34.4 ms. This supports
memory reduction contributing to cache pressure, but does not establish the cause
of every multi-second request. See [measurements and limitations](../evals/sync-hardening-release-20261026/memory-findings.md).

The experiment restored the original candidate configuration, removed its private
backup and left routing unchanged. Final-size qualification retained two CPUs /
1 GiB. The optional 2 GiB proposal was withdrawn; improved warm cache behavior
did not establish outage causation.

## Verification

Before rollout, 153 native tests passed and four were skipped, including the
real socket failure/HTTP 503 test and the real HTTPS stale-clock regression.
All 14 native test build steps and all 16 Linux ReleaseSafe build steps passed.
Main commit `06a714c` also passed both Tangled CI workflows (`deploy.yml` and
`pds-repair.yml`, pipeline `3mytamwdejv22`).
Controller lint, deployment contracts and two default tests passed; the optional
live-R2 fixture test was skipped. Production rollout is the integration check
for same-snapshot image replacement and inactive-slot qualification.

Selected, credential-free release evidence is retained in
[`docs/evals/sync-hardening-release-20261026`](../evals/sync-hardening-release-20261026).

## Verified production state — October 27, 06:06 UTC

Routing promoted the qualified b049 candidate at 06:05:14 UTC. The independent
readback selects `080d415b734708` / `vol_4915xomx28k0ml5r`, retains the old
`85379da4636338` / `vol_r685k90m3x97opj4` as `previous`, and records ETag
`6077f42bb257c24971c2ad4602d44cf0`. Both Machines remain two shared CPUs /
1 GiB. No RAM increase shipped. See `production-after.json` in the evidence
directory. API/worker/schedule migration remains owned by my-prefect-server.

Public smoke passed all available checks; admin-secret and Turso-credential
checks were explicitly skipped in this worktree. Exact-handle search returned
`zzstoatzz.io` first. The routed index reports the expected build, 12,760,941
actors and 147,886,190 prefixes. Public freshness reported healthy with a
30-second lag. A production `sync.incremental` span beginning 06:05:03 UTC
completed OK in 67.36 seconds; logs record 1,195 updates, 63 clearances and
34 removals. Routed `http.health` spans identify this same production service.

Known slow queries remain: a two-character search took 6,026 ms during startup
sync. Health and smoke success do not establish a latency SLO. The separate
bounded-admission experiment is not deployed: native TCP tests passed, but its
Linux gate failed and the patch is parked in Git stash
`cd3da1b8999ec73b91254064a3b5d9e4e3655698`. That experiment is excluded from
this release; `admission-linux-tests.txt` records the rejected trial.
