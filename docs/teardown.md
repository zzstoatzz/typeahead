# Teardown

What is left over from the old system. Search runs on the two store slots;
everything here is retired and still exists, or is code that only the retired
parts used. Read on 2026-11-13 with read-only commands, with measurements
from the cutover of the same date. "Not checked" marks what neither covered.

Nothing in the first group may be deleted without the owner's explicit go.
Turso has not been approved for deletion.

## Permanent deletions that need the owner's go

### The Turso database `typeahead`

Take a final dump first and keep it somewhere that outlives the database.

Nothing in this repository's Worker, tests or tooling reads or writes it, and
`just deploy-worker` does not consult it. The deployed Worker has made no
Turso query since 2026-11-13. Before deleting the database:

- Deploy the public Worker from a commit without the Turso client. The Worker
  deployed before it builds a client on every request, though it sends
  nothing, and the deploy gate of earlier commits reads Turso's rules.
- Delete the Worker's `TURSO_URL` and `TURSO_AUTH_TOKEN` secrets
  (`bunx wrangler secret delete`) and take both out of `.env`.

The code under [The old ingester's code](#the-old-ingesters-code) can still
read it and runs nowhere.

Size and plan: not checked.

### R2: old prefixes in `typeahead-index`

Keep `control/search-routing.json` and `control/store-slot-admissions/`.
Everything else was written for the old slots: published snapshot bundles and
manifests (`releases/…`), the bootstrap bundles the two store slots were
loaded from, and `control/tantivy-standby/`. The bucket held about 131 GB on
2026-11-12, about 93 GB of it under October prefixes.

### Still in my-prefect-server

- The flow code and tests for the three removed deployments, and
  `just refresh-search` in this repository, which submitted `typeahead-index`.
- The `typeahead-turso-*` and `typeahead-tantivy-*-fly-deploy-token` credential
  blocks. The deploy tokens are for apps that no longer exist.
- The deployments `typeahead-plc-identity` and `cloud-builder-probe`. What
  they are for: not checked.
- `deploy/prefect-edge/typeahead_health.py` still probes the old ingester and
  the two destroyed slots.

## Already removed

On 2026-11-14, with the owner's go:

- The Fly apps `typeahead-ingester`, `typeahead-tantivy-a`, `typeahead-tantivy-b`
  and `typeahead-tantivy-dev`, with their machines, volumes and addresses.
- The DNS record `ingest.typeahead.waow.tech`.
- The Prefect deployments `typeahead-index`, `typeahead-identity-hourly` and
  `typeahead-enrich-backfill`, and the automation that retried a missed
  refresh (my-prefect-server `c1c80a2`).
- The Logfire alerts `ingester heartbeat missing` and `native search overlay
  sync silent`.
- Every remote branch except `main`, on both remotes.

From the code, on 2026-11-14. The deployed Worker changes with its next
deploy:

- The Worker's Turso client, the `@libsql/client` dependency and the
  `TURSO_URL` and `TURSO_AUTH_TOKEN` bindings.
- `STATS_MODE`, `PROFILE_MODE`, `CORPUS_MODE` and `TURSO_ACTORS`, with the
  code only their other values reached: the Turso statistics store and the
  dual write, the Turso profile and reputation lookups and their compare
  mode, the Turso corpus counts, and the mirror of policy changes and
  requested accounts into Turso.
- The handlers behind `/admin/ingest`, `/admin/delete`,
  `/admin/enrich-profiles`, `/admin/identity/*` and `/admin/labels*`, and the
  cron's moderation refresh. Those paths answer 410 from a static table.
- The Turso search fallback and its two switches, `/slot/control/profile-fill`,
  the statistics migration and its `/admin/stats-backfill` and
  `/admin/stats-parity` routes.
- The comparison with Turso in `just control-ready`, and the recipes
  `control-backfill-sql`, `control-check`, `control-served-check`,
  `profile-check`, `corpus-check`, `stats-backfill`, `stats-parity`,
  `enrich-plan`, `cleanup` and `cleanup-dry` with their scripts.
- The two Turso `EXPLAIN` checks in the smoke suite, and the scripts that
  queried Turso or posted to the retired endpoints: the PLC and targeted
  backfills, the PDS repair canary, and the ingest-recovery steps that
  audited, compared, reconciled or refreshed Turso rows.
- The Turso schema dump and the in-memory libSQL the tests used in its place.

## Work that remains

### Evergreen's inventory

`site/services.json` in the `evergreen` repository still lists the old
typeahead resources and none of the new ones.

### Turso's traces in the Worker

None of these reads or writes Turso.

- `TursoDB` in `src/db.ts`, an alias of `SqlDb`. `src/moderation.ts` imports
  it, and that file's bytes are hashed into `src/moderation-regexes.json`,
  which the slots' engine is built against. Renaming it is a slot image
  change.
- The 410 answer for the retired `/admin` paths says "the Turso actor
  tables". It is the body those paths answered before, kept so a caller that
  is still running sees no change.
- `endpointLabel` in `src/metrics.ts` still has labels for the retired
  `/admin` paths, so their 410s keep their own rows on `/stats`.
- The `actor_deltas` and `counters` tables in `typeahead-stats`. Nothing
  writes them; `/stats` still reads `actor_deltas`.
- `migrations/stats-d1/0001_statistics_tables.sql` names Turso and the schema
  dump in its opening comment. It is an applied migration.
- A comment in `src/handlers/og.ts`, the notes in `ops/usage/README.md`, and
  `scripts/tests/turso_transport.py`, which drives the old ingester's Zig
  tests.
- `scripts/capture-ingest-recovery.py`, `scripts/check-recovery-status.py`,
  `scripts/plan-ingest-recovery.py` and the `scripts/pds_repair_*.py` modules
  are what is left of the ingest recovery and the PDS repair. They read the
  relay, the old ingester's health and local files.
- `scripts/snapshot_refresh/test_search_accounting.mjs` expects the search
  handler to answer 200 with no actors when its engine fails. It answers from
  Bluesky or 503, so the script fails; no check runs it.

### The old ingester's code

- The destroyed slots' origins in `src/search-route.ts` and `MANAGED_ORIGINS`
  in `scripts/snapshot_refresh/store_slot_route.py`.

- The ingester role in `services/src/`: `main.zig`'s `MODE=ingester` path,
  `ingest.zig`, and what only it uses. The store role is the same binary, so
  this is a separation and not a directory delete.
- `services/tantivy-input/`, the poller that fed the old slots from Turso.
- `services/fly.toml` and `just deploy-ingest`.
- `deploy/tantivy-dev/` and `deploy/tantivy-dev-ui/`, tooling for retired
  trial backends. Whether the `tantivy-dev-ui` preview Worker is still
  deployed: not checked.

### Left on the slots

- The Fly secret `ENRICH_FIRST_CHECK_STAMP` on both slots. It restarted the
  one-time first-check pass and has to stay until `first_check.finished_at`
  is set in the store's `/health`. On 2026-11-13 it was 0 on both: slot A had
  examined 793,755 accounts and slot B 354,452. Then unset it with
  `fly secrets unset --stage` so nothing restarts.
- Machine environment from past deploys, on both slots: `PROFILE_FILL=0`,
  `PROFILE_FILL_PAGE=1000`, `PROFILE_FILL_STEP_SECONDS=0` and
  `INDEX_RESYNC_AFTER`. None is in `deploy/shadow-slot/fly.toml`, so the next
  plain deploy drops them.
- `/data/compare` on slot A's volume, left by the comparison run before the
  cutover. It exists; its size was not measured.
- `deploy/shadow-slot/README.md`, `bootstrap.md`, `sizing.md` and
  `validation.md` still describe the slot as a shadow beside production, and
  `fly.toml` begins with "Not deployed".
- The Fly app names still say `shadow`.

### The readiness gap

After a restart a slot reports ready, and `standby` reports it fit, long
before it answers at normal speed: about 10 minutes at 2 GB, and hours after a
full resync at the shared size. A flip in that window would be admitted and
would serve slow or failing answers. Until readiness or the admission measures
latency, run a [latency round](operating.md#restarts-and-warm-up) against the
slot's own origin and require a clean one before a flip.

### Smaller things

- `SEARCH_CACHE_GENERATION` in `wrangler.jsonc` still carries the name of an
  October cutover. It is only a cache key.
- `/health/freshness` still has fields for snapshot age and checkpoint
  coverage that are always `null`, and its alert text says "behind turso".
- The cron's alert for the control database points at `docs/slot-control.md`,
  which is now `docs/history/slot-control.md`.
- Nothing runs `store_slot_route.py standby` on a schedule, so a standby that
  stops being fit is found only when someone looks.
