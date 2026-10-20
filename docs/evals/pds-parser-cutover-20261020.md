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

Both PDS-capable machines currently follow the root publisher pointer. Before
publishing a later cutoff or repaired snapshot, protect the rollback machine's
current checkpoint pointer while it is private and verify recovery again. The
older pre-PDS legacy backend is an emergency fallback requiring explicit scope
handling; it is not the routine rollback path for the live PDS filter.
