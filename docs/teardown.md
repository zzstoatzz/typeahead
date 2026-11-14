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

One thing still reads it, so it cannot go before the code under
[Turso in the Worker](#turso-in-the-worker) does: `just deploy-worker` runs
`just control-ready` first, which reads Turso's moderation rules.

The Worker itself stopped using it on 2026-11-13: `STATS_MODE=d1-only` writes
statistics to D1 alone, and `TURSO_ACTORS=off` had already stopped the account
writes.

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

## Work that remains

### Evergreen's inventory

`site/services.json` in the `evergreen` repository still lists the old
typeahead resources and none of the new ones.

### Turso in the Worker

Remove once the owner is ready to let the database go. The Worker reads
nothing from Turso for accounts and writes nothing to its account tables.

- The Turso half of `src/stats-store.ts` and the modes other than `d1-only`, `src/stats-migration/`, `scripts/stats-d1.ts`, the
  `statsMigrationRoute` call in `src/index.ts`, and the `stats-backfill` and
  `stats-parity` recipes.
- `src/turso-policy-mirror.ts` and its calls. `mirrorRequestedActor`,
  `handleDelete`, `handleEnrichProfiles` and `enrichProfileDids` in
  `src/handlers/admin.ts`.
- `src/handlers/ingest.ts`, `src/identity-queue.ts`, `src/account-labels.ts`
  and `src/backfill.ts`, with the `/admin/*` routes that answer 410 now.
- `refreshModeration` in `src/cron.ts`.
- The Turso branch of `src/handlers/search.ts`, with `TURSO_FALLBACK_ON_EMPTY`
  and `TURSO_FALLBACK_ON_UNREACHABLE`.
- `countSnapshot` and `countExclusions`, and the Turso halves of
  `src/handlers/profiles.ts`, `src/handlers/reputation.ts` and `src/corpus.ts`.
- `/slot/control/profile-fill` and `src/handlers/slot-profile-fill.test.ts`.
- `PROFILE_MODE`, `CORPUS_MODE`, `TURSO_ACTORS` and `STATS_MODE` themselves.
  `getDb`, `tursoDb`, the `@libsql/client` dependency, and the `TURSO_URL` and
  `TURSO_AUTH_TOKEN` secrets on the Worker and in `.env`.
- `scripts/turso-parity.ts` and `src/turso-parity.ts` with the `profile-check`
  and `corpus-check` recipes. `scripts/control-backfill.ts` with
  `control-backfill-sql`, `control-check`, `control-served-check` and
  `control-ready`, including `control-ready` as a prerequisite of
  `just deploy-worker`.
- The two Turso `EXPLAIN` checks in `scripts/smoke.py`.
- `scripts/prioritized-enrich.py`, `scripts/cleanup-dead-actors.py`, the
  recovery scripts and the `cleanup`, `cleanup-dry` and `enrich-plan` recipes.
- `docs/ops/turso-schema-20261110.sql`, once `src/control-d1-fixture.ts` and
  `src/stats-d1.test.ts` no longer load it.
- The `architecture` section of `/llms.txt` (`src/pages/llms.ts`) and the
  Turso panel of the costs page (`src/pages/costs.ts`) still describe the old
  system.

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
