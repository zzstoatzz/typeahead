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
  passes (profile fill, handle repair); managed-slot support in the route
  tool; the Turso poller the engine's tests ran through; and the model of the
  old SQL the store was tested against.

## Still shaped by the old system

- **Names Fly holds.** The apps are `typeahead-slot-a` and `-b` and their
  volumes `slot_data`, from when a slot ran beside the old system as its
  shadow. Fly cannot rename either; new apps on volumes restored from
  snapshots replace them.
- **The first-check pass** (`first_check` in the store's `/health`) exists
  because the stores were first loaded from a search bundle, which left
  accounts no profile source had answered for. It is still running on both
  slots. When `finished_at` is set on both, unset the
  `ENRICH_FIRST_CHECK_STAMP` secret (`fly secrets unset --stage`) and remove
  the pass from the store.
