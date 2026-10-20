# PDS repair cleanup — October 20, 2026

Cleanup replaces repair execution for this task. Coverage remains incomplete:
only two source repairs were verified. No new repair jobs are enabled.

## Removed

- Duplicate cloud historical reconstruction stopped; PID zero verified.
- All temporary `typeahead-pds*` units stopped/terminal and cleared; no loaded
  units remained. Abandoned cloud launchers archived off their original paths.
- Three unusable source exports deleted: 6,300,104,652 bytes. Their attempt
  receipts, failure logs and SQLite headers remain in the cloud evidence directory.
- Seven obsolete runtime/build directories removed (about 299 MB).
- Temporary Hetzner volume 107217925 (200 GB) unmounted, detached and deleted;
  absence verified through the provider inventory. It held toolchains/caches,
  not a completed comparison build. The qualified 33 MB binary/library pair was
  copied to the cloud root first and compared byte-for-byte.
- Full-export downloader, duplicate cloud reconstruction controller, undeployed
  discovery/reconciliation/retry queue, and generic stage/admission wrappers
  removed from source with their exclusive tests. Unused bundle download code
  removed; the durable byte budget remains because tail capture uses it.
- Newly started baseline stopped; its temporary Fly token is absent. No repair
  credentials remain under `/run/typeahead*`.

## Preserved and why

- Shipped SQL-error parser fix: prevents false successful replay admission.
- Serving and rollback machines, volumes, protected R2 pointer and artifacts:
  required production/recovery assets, not disposable experiment resources.
- Conditional repair ledger, canary, health checks and verification: exercised
  by the two actual repairs, including a real latency-stop failure.
- Completed HeavyPad reconstruction and 50k cutoff/replay evaluation: useful
  results; no new home bulk transfer or remote mutation was performed.
- Cloud source snapshot, candidate data, completed validated PLC tail and
  qualified binaries: already acquired reusable results. Incomplete historical
  reconstruction is retained as such, never labeled authoritative/current.
- Offline reconstruction/merge/report helpers and tests: reproduce retained
  results and validate provenance; they do not start background jobs.
- Bulk home schedule hold: prevents accidental ISP-heavy runs. No replacement
  recurring repair or publisher is claimed to be deployed.

Retained cloud evidence is `/var/lib/typeahead-pds-coverage-20261020` on
`167.233.79.205`. Local evidence is
`/Users/nate/.local/share/typeahead-experiments/pds-coverage-20261020`.
Archived launchers are historical evidence, not approved execution entrypoints.
Git retains removed code; it is not part of the maintained repair interface.

## Verification

All 32 remaining PDS tests passed using real SQLite/loopback HTTP. No source
imports remain for deleted modules. Production health was ready with 12,641,735
actors and 114-second lag. Serving generation `01M5DRQXFBG3RRXTRV1ZSVXCVC`
and private rollback generation `01M5DTD1CPT6A2X3K0P2BXWARR` were unchanged.
No serving configuration or routing mutation was made during cleanup.
