# Automatic snapshot refresh — October 25, 2026

Qualified and live October 25, 2026. Automatic refresh is enabled and its queued
runs are verified. The architecture and operator contract are in
[the refresh runbook](../plans/automatic-snapshot-refresh.md).

## Reason for the change

The original installer followed a shared pointer on the serving disk. Publishing
a generation could therefore start a large download and full validation alongside
live search. Pausing publication avoided that interference but left an incomplete
operating state. Fast admitted startup alone did not repair the refresh boundary.

The cloud publisher now builds and fully verifies artifacts before an isolated
installer prepares the inactive backend. It has independent compute, a fresh disk,
and its own fixed slot pointer. Only after admission, overlay catch-up, query checks
and a restart at the final size does the controller switch the R2 routing record.
The previous backend keeps running for immediate routing rollback.

## Qualified components

- Installer image: `registry.fly.io/typeahead-search-refresh@sha256:0c431115b6528d626ae36669dda9cc73d256cba6223bd443cf27daebc6ae512a`.
  Its native binaries match the previously qualified admitted-startup image; the
  additional Python bootstrap installs and admits before starting search.
- Transport version: `67b582cb-2e28-442d-8f4d-0c587d634b89`; public binding update
  `936893b4-c7be-4ecc-ac58-0d7babda28e1`. The binding transition initially selected
  the same serving backend and required no machine restart.
- Managed slots: `typeahead-startup-20261024` and `typeahead-search-refresh`.
  The original `typeahead-search-compact` remains outside controller ownership.
- Ready publication: `control/builds/latest.json`; traffic selection:
  `control/search-routing.json`; fixed slot pointers: `control/slots/<app>/latest.json`.
  Current/previous artifacts and their legacy twins remain under `releases/search/`.
  The historical root `latest.json` is unchanged.

Real disposable installation tested valid data, cached restart, corrupt-artifact
rejection and retry without changing prior activation. Real R2 conditional writes
rejected a stale writer. Runtime transport tests covered missing/corrupt routing,
exact origins, scoped queries and admin authentication. Fixture machines, volumes
and R2 keys were removed after testing. Receipts:

- [Installer qualification](../evals/startup-refresh-install-tests-20261025.json)
- [Conditional routing seed](../evals/startup-refresh-route-seed-20261025.json)
- [Guarded fixture cleanup](../evals/startup-refresh-cleanup-tests-20261025.json)
- [Public router checks](../evals/startup-refresh-router-live-20261025.json)
- [Authenticated admin framing](../evals/startup-refresh-admin-route-20261025.json)
- [Linux parent-death cleanup](../evals/startup-refresh-process-cleanup-linux-20261025.json)

## Full generation qualification

Source build run `ad535ec3-6d75-4d00-b234-520252846987` produced the full generation
`b1792908655-14d5` with watermark `1792908646`, 12,760,941 actors and 147,886,190
prefix entries. It was handed off after completion so corrected publication code
could use the data without another corpus build. The compact generation is
`b1792908655-14d5-px05`.

Two recovery attempts failed before publication: one lacked the compactor after
a fresh checkout, and one selected the wrong compiler because a file was passed
to a directory-taking selector. The corrected path restores the compactor from
its persistent cache with the pinned Zig 0.17 toolchain. Run
`a09ed417-db6d-4959-bca3-75bf7074d057` resumes publication and rollout from that
completed source. [Completed-source receipt](../evals/startup-refresh-completed-source-20261025.json).

Cancellation also exposed an orphaned converter. Subprocesses now have a parent
liveness guard that stops the full process group on cancellation, timeout or
parent death; private scratch directories are cleaned afterward. Linux testing
killed the parent with SIGKILL while descendants ignored SIGTERM and verified
that they stopped. No serving machine was involved in these recovery checks.

Publication exposed a Rclone link-handling mismatch: the recovery wrapper used
symlinks to avoid another payload copy, while Rclone's default did not follow them.
The durable publisher environment now explicitly sets `RCLONE_COPY_LINKS=true`.
The already-running retry used same-filesystem hard links to the identical compact
payloads; no payload was copied or rebuilt. A real native-converter/publisher/
Rclone filesystem test now covers the complete wrapper and retry, and a real R2
upload/readback/retry probe passed with link handling enabled. Its private test
object was deleted. [R2 receipt](../evals/startup-refresh-symlink-publication-20261025.json).

A read-only repeat of the already-selected generation passed direct semantic
checks and five public verification rounds without changing routing. Recovery
now repeats those checks before reporting idempotent success.
[Repeat-generation receipt](../evals/startup-refresh-idempotency-20261025.json).

The first full candidate installed and passed initial probes, but final-size
qualification raced Fly's asynchronous restart. A query timed out and the old
failure handler deleted the prepared candidate. Public routing stayed on the
healthy existing backend. The corrected controller confirms a stopped Machine,
updates with `skip_launch`, starts it and confirms started state before HTTP gates.
A real throwaway Machine passed that sequence and was removed without a disk,
serving ports or route change. [Restart barrier receipt](../evals/startup-refresh-restart-barrier-20261025.json).
Failed preparation is now retained in the inactive slot for diagnosis or
`resume_rollout=true`; recovery uses the already-verified publication without
another build/upload/readback. The next new generation replaces that inactive slot.

## Accepted operating state

The successful full rollout was run `a41f37f4-f866-481d-82a3-12a10bf0bc57`
with source `f12a70411dab320078c9fe1e4e9468ba8573f657`. It reused the already-verified publication from `a09ed417-db6d-4959-bca3-75bf7074d057`
and completed independent installation, admission, explicit cached restart at two
shared CPUs / 1 GiB, guarded promotion, five public rounds, archival and pruning.
[Full rollout receipt](../evals/startup-refresh-full-rollout-20261025.json).

Run `4489c8b4-2e52-4fb5-b10c-e6d6bd2efe67` exchanged routing back to the previous
backend without changing a machine or volume. Public freshness and direct query
contracts passed. [Rollback receipt](../evals/startup-refresh-rollback-20261025.json).
Run `a2a18f99-f849-48d6-bd99-f41e3560b312`, using the later retry-capable code,
then recovered through the already-prepared new backend. Two cold-query checks
exceeded the unchanged 2.5-second budget; retries preserved the same machine,
volume and admission. The accepted attempt repeated cached restart, query gates,
conditional promotion and five public rounds, then completed. It did not rebuild,
upload, download or re-admit the generation.
[Prepared-backend recovery](../evals/startup-refresh-resumed-rollout-20261025.json).
These probes do not establish sub-second cold query latency or a p95 performance claim.

Production is build `b1792908655-14d5-px05`, machine `85379da4636338`, volume
`vol_r685k90m3x97opj4` (20 GB) in `typeahead-search-refresh`. The previous backend
is `7841e35f965528` / `vol_v8eyllqdyykq0z5v` (60 GB) in
`typeahead-startup-20261024`. Both use two shared CPUs / 1 GiB. The original
`typeahead-search-compact` remains separately retained as agreed.
[Resource inventory](../evals/startup-refresh-resources-20261025.json).
Global/exact queries, both public aliases, hostname/URL PDS forms, unknown-PDS
empty results, explicit excluded-PDS rejection and real Evergreen inventory/status
validation pass. [Public acceptance](../evals/startup-refresh-acceptance-20261025.json).
The legacy root pointer checksum remains unchanged.

The active fixed cron is `0 6 */2 * *`, UTC: odd-numbered days of the month at
06:00, at most 48 hours apart. Actual queued runs are October 27, 29 and 31 at
06:00 UTC, with compact format, the qualified builder ref, automatic rollout,
cloud pool concurrency one, and no recovery-only parameters. The interval
scheduler had stored the anchor but generated times relative to registration;
fixed cron prevents repeated registration from postponing refresh indefinitely.
[Live schedule and queued-run receipt](../evals/startup-refresh-schedule-20261025.json).
PLC identity remains paused; the old home publisher is not restored.

## Final cleanup

The cross-app immutable-image pull succeeded under the other app's scoped deploy
credential on a machine with no serving ports or disk. It exited successfully
and was destroyed; the previous backend and route stayed unchanged.
[Image access check](../evals/startup-refresh-cross-app-image-20261025.json).
The unsuccessful recovery fork and its never-started machine were removed.
[Failed-fork cleanup](../evals/startup-refresh-failed-fork-cleanup-20261025.json).
No test machines or generation-owned multipart uploads remain. The Fly CLI lists
only the intended active volume in each managed app. Raw list results can lag
per-volume GET: retired volumes are `pending_destroy` on the canonical GET even
when the list still reports `waiting_for_detach`; the inventory records both.

Removed 7,292,937,432 bytes of abandoned `.compact-release-*` staging from the old
cancelled converter after preserving completed archives. Linux testing verified
that cleanup preserves open files, recent staging, symlinks and completed builds.
The publisher now runs that cleanup before space admission and after archival.
Local SSD free space is 105.41 GiB, above the unchanged 96 GiB build admission
reserve. [Staging cleanup](../evals/startup-refresh-staging-cleanup-20261025.json).
The temporary handoff disk guard is inactive with MainPID zero and its script
removed. The permanent cloud worker remains active with its own resource limits,
process-group cleanup and per-command scratch cleanup. No freshness thresholds,
alert routes or alert muting were changed.

## Superseded-backend retirement

Later October 25, the operator authorized retirement after accepting the smaller
production backend. The active route and public freshness were checked before
each deletion. The routing record was conditionally updated to `previous: null`
before removing the old target. Current build, machine, disk and image did not change.

Removed:

- Original `typeahead-search-compact` machine `86e502fe55e4e8` and 60 GB volume `vol_vxmqwnpnlewdgzj4` (four shared CPUs / 2 GiB).
- Previous managed `typeahead-startup-20261024` machine `7841e35f965528` and 60 GB volume `vol_v8eyllqdyykq0z5v` (two shared CPUs / 1 GiB).

Both apps now have no machines or active volumes. The two managed app identities,
credentials, installer image and schedule remain, allowing the next refresh to
create an independent candidate in the empty slot. Old on-disk overlays are gone;
recovery requires installation and replay from retained R2 artifacts. Immediate
old-backend routing rollback is unavailable until another successful refresh.
Public freshness and exact-handle queries through both aliases passed after cleanup.
[Retirement inventory and receipt](../evals/search-retirement-20261025.json).

At the connector's configured list prices, deleting both machines removes
$19.91/month of always-on compute and deleting 120 GB removes $18/month of volumes.
The current search baseline is $6.64/month compute plus $3/month for its 20 GB disk;
the separate ingester, snapshot storage, builder and shared services are additional.
These are list-price estimates, not invoice savings or a complete project bill.

## Retired home datasets

Later October 25, the operator requested cleanup of the old home build and
qualification datasets. HeavyPad root disk usage fell from 92% to 43%; 852 GiB
was reclaimed. The two newest home build generations, small scripts, manifests,
logs and JSON results remain. Large obsolete experiment database/prefix payloads
were removed; historical local paths in earlier evaluations may now be retired.
The archived cloud generations, R2 artifacts and serving disks were not changed.
The audit also stopped a localhost evaluation server orphaned since October 8.

The home publisher remains disabled. The active cloud publisher's retention now
runs before new-build disk admission as well as after success, counts format twins
as one generation, and can identify a working stale backend from HTTP 503. Unknown
identity still protects data and blocks oversized retention with a visible failure.
See the [producer audit](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/audits/2026-10-25-worker-disk-cleanup.md).
