# Parser-fix production cutover

Verified October 20, 2026 at 23:15 UTC. This release rejects malformed or failed
Turso SQL responses before replay can claim readiness. It does not complete the
PDS coverage repair or change the live 10,000-actor cutoff.

## Serving and recovery state

Traffic moved at 23:12:43 UTC with no Worker source or origin configuration change.

| Role | Machine | Generation | Routing |
| --- | --- | --- | --- |
| Serving | `86e502fe55e4e8` | `01M5DRQXFBG3RRXTRV1ZSVXCVC` | Started, uncordoned |
| Immediate PDS-capable rollback | `8d4916be311008` | `01M5CXE7SXEP6Q7S4EJ5ER96M9` | Started, cordoned |

Both belong to `typeahead-search-compact`. Serving image:
`registry.fly.io/typeahead-search-compact@sha256:433155b25711c8e98b3885a56528dca26d0cb36c38d22f0a3ece6487888cc3bd`.
Its native source is qualified commit `b52ae564399cf5f0a9e24f634a147406fa038293`.
Rollback retains image digest
`sha256:7df51c4b6ba2b26dcfc1aeb15b7821c7ec0caf6594cb511fc93ddbe946101938`.

Serving volume is `vol_vxmqwnpnlewdgzj4` (`pds_replay_data`). Rollback volume
`vol_4qlwyx1kl6o19x6r` remains intact. The older compact and legacy recovery
resources remain intact too. Do not delete either production app or these
volumes as temporary qualification cleanup.

The serving build remains `b1792484789-3d65-px05`, source watermark 1792484192,
root `/data/pds-current-20261020`, overlay `/data/pds-overlay/overlay.db`, and
root `latest.json`. Snapshot actor count is 12,641,735. The configuration file
records this serving image and volume; plain deployment is still guarded.

## Verification

The candidate was qualified privately, then given service configuration with
`skip_service_registration=true` and the expected `current_version`. After its
restart, native admission, build, catch-up, ranking, PDS handling and Logfire
were checked again. Candidate was uncordoned before the old machine was
cordoned, preserving an overlap of healthy backends.

- Both authenticated public smoke runs passed, including admin and live query-plan checks.
- All 11 canonical ranking queries passed and matched previous-serving response bodies.
- Malformed scopes returned 400; unknown scopes returned empty results; uppercase hosts
  normalized correctly; an excluded large host returned 503 `PdsScopeUnavailable`.
- After cutover, 20 public canonical/alias pairs were byte-identical with expected first results.
- Six sampled Logfire search spans from the serving digest appeared after cutover,
  with no ERROR status among that sample. This is not a fleet-wide zero-error claim.
- A before/log/after inventory check proved completed sync at 23:15:26 on the
  exact serving generation. The write-admission fingerprint now requires that identity.

[Archived evidence](pds-parser-cutover-20261020/) includes response bodies,
smoke output and completed-sync proof. SSH request durations are not engine
benchmarks; this release makes no performance improvement claim.

## Rollback and subsequent publication

Refresh current inventory, configurations and health before every routing change;
other sessions can deploy. Preserve newer Worker source. To reverse this transition,
first uncordon the healthy previous PDS-capable machine, verify its routing, then
cordon the new machine. Never remove the only healthy routed backend. Repeat
public smoke, scoped aliases and Logfire verification, and reconcile the explicit
write-admission identity only after that verification.

Production follows the root publisher pointer. As verified October 20 at 23:35
UTC, private rollback `8d4916be311008` is pinned to the protected current build
(see below). Preserve that pin when publishing a later cutoff or repaired snapshot. The
older pre-PDS legacy backend is an emergency fallback requiring explicit scope
handling; it is not the routine rollback path for the live PDS filter.


## Bulk home schedule hold — October 20, 23:27 UTC

MPS main `7fedc3be6d20340ecc212753d8a06d48edb2ccdc` is verified on both
repository remotes and records inactive schedules for `typeahead-index` and
`typeahead-plc-identity`. Live Prefect reads confirmed both schedules inactive
and zero scheduled, pending or running flows for those two deployments after
cancelling the three remaining queued index runs. Other schedules and production
services were not paused.

The existing qualified index pair is about 23.3 GB; uploading and reading it
back would use about 46.6 GB before the source export. Moving this work to the
existing Hetzner server avoids another bulk transfer through the metered home
connection. Cloud reconstruction and checkpoint qualification remain in progress.
Neither a recurring cloud publisher nor recurring repair is enabled yet.
Resume bulk schedules only after qualifying the cloud executor, budgets,
protected recovery pair, native replay and ingestion/search admission, and
updating authoritative deployment configuration.


## Pinned PDS rollback — October 20, 23:35 UTC

Saved and byte-verified the current manifest at
`releases/pds-coverage-rollback-20261020/b1792484789-3d65-px05/latest.json`.
Updated only the private rollback machine's `INDEX_LATEST_KEY`, guarding its
current version and retaining service exclusion. New generation:
`01M5DTD1CPT6A2X3K0P2BXWARR`. Production's generation and public routing stayed
unchanged. The immediate post-update inventory assertion ran before the new
configuration was visible; subsequent fresh inventory confirmed the intended
pointer and cordoned state. No second update was issued.

Native startup progressed through local snapshot reads, then passed readiness
with 12,641,735 actors, exact build `b1792484789-3d65-px05`, and 77-second overlay
lag. Global exact-handle and selfhosted.social searches matched the serving
machine's complete response bodies and expected first handles. An initial probe
used the public XRPC route on the native server and was rejected; the corrected
`/search` probes passed. Inventory before/after verified the private generation
and unchanged production generation.

[Verification evidence](pds-parser-cutover-20261020/rollback-pin-state.json).
This check verifies restart with the protected pointer and existing local
artifacts; it is not a new empty-directory disaster restore or full R2 artifact
readback. Existing recovery artifacts remain retained.
