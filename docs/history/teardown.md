# Teardown

The system before the store slots had a separate ingester, a Turso database,
a twice-daily index refresh and two Turso-fed search slots. Its apps, data,
flows, credentials and code are removed. This page lists what was removed.

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
- Fly apps `typeahead-shadow-a` and `typeahead-shadow-b`, replaced by
  `typeahead-slot-a` and `typeahead-slot-b` on volumes restored from a
  snapshot.
- The Logfire alerts for the ingester and the native engine.
- Every remote branch except `main`.
- In this repository: the Worker's Turso client, modes and retired endpoints'
  handlers; the ingester role; the refresh controller; the R2 bundle
  bootstrap; the comparison tooling and experiments; the finished one-time
  passes (profile fill, handle repair, first check); managed-slot support in the route
  tool; the Turso poller the engine's tests ran through; and the model of the
  old SQL the store was tested against.
