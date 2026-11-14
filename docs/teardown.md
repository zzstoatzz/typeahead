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

Two things still use it, so it cannot go before the code under
[Turso in the Worker](#turso-in-the-worker) does:

- Under `STATS_MODE=read-d1` the Worker still writes every statistic to Turso
  as well as D1. `src/stats-store.ts` has no setting that writes D1 alone.
- `just deploy-worker` runs `just control-ready` first, which reads Turso's
  moderation rules.

Size and plan: not checked.

### Fly apps

| app | state | machines | volumes | other |
| --- | --- | --- | --- | --- |
| `typeahead-ingester` | suspended | `7845636a3d0ee8`, stopped | none | certificate for `ingest.typeahead.waow.tech`; two IPs |
| `typeahead-tantivy-a` | suspended | `e820357f16e048`, stopped | `vol_re161yxy0zgo80d4`, 20 GB | two IPs |
| `typeahead-tantivy-b` | suspended | `0803e1dc740128`, stopped | `vol_vgnwley2wdlnj884`, 20 GB | two IPs |
| `typeahead-tantivy-dev` | suspended | none | none | two IPs |

`fly apps list` shows no other `typeahead-*` app besides the two slots. The
two 20 GB volumes are billed while they exist.

The route record no longer names them: on 2026-11-13 its `current` was
`typeahead-shadow-a` and its `previous` `typeahead-shadow-b`.

With the apps, remove `https://typeahead-tantivy-dev.fly.dev`,
`https://typeahead-tantivy-a.fly.dev` and `https://typeahead-tantivy-b.fly.dev`
from the origin list in `src/search-route.ts` and from `MANAGED_ORIGINS` in
`scripts/snapshot_refresh/store_slot_route.py`.

### R2: old prefixes in `typeahead-index`

Keep `control/search-routing.json` and `control/store-slot-admissions/`.
Everything else was written for the old slots: published snapshot bundles and
manifests (`releases/…`), the bootstrap bundles the two store slots were
loaded from, and `control/tantivy-standby/`. The bucket held about 131 GB on
2026-11-12, about 93 GB of it under October prefixes.

### Prefect, in my-prefect-server

- `typeahead-identity-hourly` and `typeahead-enrich-backfill`: schedules
  paused 2026-11-13 (my-prefect-server commit `d21bee8`).
- `typeahead-index` and the automation that retries a missed refresh: paused
  earlier the same week.
- `typeahead-plc-identity` and `cloud-builder-probe`: two more deployments
  whose purpose is not known.
- Credential blocks: `typeahead-turso-*`, `typeahead-tantivy-a-fly-deploy-token`
  and `typeahead-tantivy-b-fly-deploy-token`.
- The flow code and image pins for them in that repository.

`just refresh-search` in this repository submits
`typeahead-index` and goes with it.

### DNS and certificate

`ingest.typeahead.waow.tech` is a CNAME to
`2m2mk80.typeahead-ingester.fly.dev`, with a certificate issued on the
`typeahead-ingester` app. Delete the DNS record with the app.

### Logfire

The alert `ingester heartbeat missing` is active and matching, because the
ingester is stopped. `native search overlay sync silent` is already disabled.
Both can be deleted. The two store slot alerts name
`docs/ops/store-slot-operations.md` and `docs/ops/cutover-to-store-slot.md` in
their descriptions; the runbook is now [operating](operating.md).

### Remote branches

`origin` is tangled, `github` is the mirror.

| branch | last commit | merged into main |
| --- | --- | --- |
| `origin/codex/tantivy-cutover-readiness` | 2026-10-30 | yes |
| `github/codex/tantivy-cutover-readiness` | 2026-10-30 | yes |
| `origin/archive/pds-repair-research-20261020` | 2026-10-20 | no, 66 commits |
| `origin/archive/preexisting-typeahead-work-20261020` | 2026-10-20 | no, 1 commit |
| `origin/codex/ranking-explainer` | 2026-10-21 | no, 3 commits |
| `origin/codex/native-compaction` | 2026-10-09 | no, 2 commits |
| `origin/codex/overlay-ranking` | 2026-10-09 | no, 2 commits |
| `origin/codex/full-name-recall` | 2026-09-05 | no, 2 commits |
| `origin/codex/wip-pre-jimray-cleanup` | 2026-07-22 | no, 1 commit |
| `origin/codex/brand-new-account-downrank` | 2026-06-25 | no, 1 commit |

The unmerged ones hold work against the native engine or the Turso system.
Whether any of it is still wanted: not checked.

## Work that remains

The Worker still writes to Turso on every request. `STATS_MODE=read-d1` reads
statistics from D1 and writes each one to Turso as well, and there is no
setting that writes D1 alone. The Turso bill does not stop until a D1-only
statistics mode ships.

### Evergreen's inventory

`site/services.json` in the `evergreen` repository still lists the old
typeahead resources and none of the new ones.

### Turso in the Worker

Remove once the owner is ready to let the database go. The Worker reads
nothing from Turso for accounts and writes nothing to its account tables.

- A statistics setting that writes D1 alone, then the Turso half of
  `src/stats-store.ts`, `src/stats-migration/`, `scripts/stats-d1.ts`, the
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
