# Teardown

The system before the store slots had a separate ingester, a Turso database,
a twice-daily index refresh and two Turso-fed search slots. Its apps, data,
flows, credentials and code are removed. This page lists what was removed
and what in the repository still carries the old system's shape.

## Removed

2026-11-13 and 14, with the owner's go for every deletion:

- Fly apps `typeahead-ingester`, `typeahead-tantivy-a`, `typeahead-tantivy-b`
  and `typeahead-tantivy-dev`, with their machines, volumes and addresses.
- The Turso database `typeahead`. No dump was taken: the slots hold the
  accounts, the control database held identical moderation rules, and the
  statistics history is in D1.
- Everything in the `typeahead-index` R2 bucket except the route record and
  the store-slot admissions: about 122 GB of bundles and refresh records.
- The DNS record `ingest.typeahead.waow.tech`.
- In my-prefect-server: the index, identity and enrichment deployments and
  flows, the health exporter, the ephemeral Hetzner builder and its reaper
  Worker, their work pools and their credential blocks.
- On HeavyPad: the typeahead worker and exporter units, and every
  `~/typeahead-*` directory.
- The Logfire alerts for the ingester and the native engine.
- Every remote branch except `main`.
- In this repository: the Worker's Turso client, modes and retired endpoints'
  handlers; the ingester role; the refresh controller; the R2 bundle
  bootstrap; the comparison tooling and experiments; the finished one-time
  passes (profile fill, handle repair); and managed-slot support in the route
  tool.

## Still shaped by the old system

- **The search engine's source protocol.** `services/tantivy` still accepts
  the Turso poller's source policy beside the store's, with the code and
  tests for it, and `services/tantivy-input/` is that poller. `just
  test-tantivy` drives the engine through the poller for policy expiry,
  rejected snapshots and crash recovery. Removing both means porting those
  tests to the store's bridge and changes the engine binary, which the route
  admission pins.
- **`src/moderation.ts` imports a type named `TursoDB`.** The file's bytes
  are hashed into the rules both slots compare at admission, so editing it
  needs both slots upgraded through a failover, not an ordinary flip.
- **`services/src/store_process/sql_model.zig`** models the old SQL behaviour
  as the reference for the store role's differential tests.
- **The first-check pass** (`first_check` in the store's `/health`) is still
  running on both slots. When `finished_at` is set, unset the
  `ENRICH_FIRST_CHECK_STAMP` secret on each (`fly secrets unset --stage`) and
  remove the pass.
- **Names Fly holds.** The apps are `typeahead-shadow-a` and `-b` and the
  volumes `shadow_data`. Fly cannot rename either.
- **The readiness gap.** After a restart a slot reports ready before its
  search engine answers at normal speed, and at 2 GB latency took about ten
  minutes to settle. Until `/ready` waits for the index files to be read
  into memory, require a clean latency round before flipping to a slot that
  restarted ([operating](operating.md#restarts-and-warm-up)).
- **Nothing runs `slot_route.py standby` on a schedule**, so a standby that
  stops being fit is found only when someone looks.
