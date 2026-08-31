# plan: remove the Turso replica from the search box

Follows `search-replica-research.md` (2026-08-29). Goal: the search box holds
**snapshot + overlay only**; recovery is "download snapshot, replay rows newer
than its watermark" instead of an 11-hour replica rebuild; the sync code that
produced the last three retros goes away. Secondary goal: cut dedicated spend.

Each phase is independently shippable and reversible. Nothing in a later phase
starts until the previous phase's verification has been *observed*, not assumed.

## cost, measured 2026-08-29

| line | today | after plan | how |
|---|---|---|---|
| `typeahead-search` compute 4cpu/2GB | $17.76 | $8.88 (2cpu/1GB) | phase 4, only if 2 weeks of post-cut metrics allow |
| `typeahead-search` volume 100GB | $15.00 | $9.00 (60GB) | phase 3: 3×snapshot + overlay, no replica/staging/WAL |
| `typeahead-ingester` compute 2cpu/512MB | $6.38 | $3.19 (1cpu/256MB) | phase 1: sized for a sync role removed in `96f0870`; load 0.02 |
| `typeahead-ingester` volume 10GB | $1.50 | $0 | phase 1: 28 KB used, nothing writes `/data` |
| `typeahead-indexer` (Fly) | $0 | $0 | already gone (COSTS.md still lists $16 — fix) |
| R2 snapshot builds | ~$0.2/build | keep 2 | phase 5 |
| **dedicated total** | **≈ $41/mo** | **≈ $22/mo** | |

Turso and Cloudflare are shared org spend and unchanged by this plan, except
that Turso *reads* drop by the ~11M rows per resync attempt (22k resync pages
in the last 7 days alone).

## phase 0 — measure (no behavior change)

Ship together; nothing here changes what users see.

1. **`/search` instrumentation.** One logfire span per request with: engine,
   candidates from base vs overlay, results hydrated from overlay vs base, and
   `belt_disagreements` — the count of candidates the `actors` safety belt in
   `index_search.zig:272` would drop that overlay+blob `hidden` would NOT (and
   vice versa). This number is the entire argument for phase 3. Sampled if
   volume demands.
2. **Overlay gauge** in `/index/status`: rows in `actor_overlay` /
   `prefix_overlay` by `source`, and rows ≤ attached watermark. Tonight's 16M+
   was invisible.
3. **Compact on every incremental pass**, chunked, not only at promote — so the
   overlay never ratchets. (Prunes rows ≤ the *attached* watermark; safe today.)
4. ~~Watchdog long-phase budget~~ — scoped down after reading the code: the
   resync path writes straight into live `actors` (no staging swap, no DDL, FTS
   rebuild skipped under `ENABLE_LOCAL_FTS=0`), so it has no silent finishing
   phase; only the fresh-volume bootstrap builds indexes, and phase 3 deletes
   it. Compaction now feeds the watchdog per chunk instead.

Verify: a promote cycle (3 days) with the gauge flat after compaction, zero
watchdog restarts, `belt_disagreements` observed.

## phase 1 — ingester right-size (independent, cost only)

`fly scale vm shared-cpu-1x --memory 256 -a typeahead-ingester`; remove the
`[mounts]` block and destroy `typeahead_data` (first: confirm `df` still shows
~0 and `grep /data services/src` still finds nothing). Verify: ingest rate on
`/stats` unchanged over 24h, no watchdog restarts, `fly_instance_cpu_throttle`
= 0. Rollback: scale back up. **−$4.69/mo.**

## phase 2 — projection reads old rows from overlay-then-base

`snapshotOldActorRow` (`sync.zig:1074`) reads `actor_overlay.blob`, falling
back to `idx.actor_compact_base.blob`, *instead of* `actors`. The replica stays;
the hydrate belt stays. Add a temporary comparison: when both sources exist and
disagree, log + count. Verify: one full promote cycle with zero disagreements
and rename/hide/unhide smoke checks (`request-indexing` an actor, rename via
`/admin/refresh`, see prefix keys move). Rollback: one-line source switch.

## phase 3 — cut the replica

Gate (re-revised 2026-08-31, after tracing each class to ground truth):
the mismatch counters turned out to measure how often the REPLICA would have
mis-projected, not whether the new source errs. Projection's job is to
suppress stale keys **that exist in serving**, so the correct old row is by
construction the base/overlay representation:

- `replica_empty` (116 in the first pass): replica lost rows serving has
  (handle-flap) — the class behind the belt drops suppressing live actors.
- `source_empty` (traced: did:plc:2kji4vm4ftowkwevmznx5bzv): serving never had
  the actor (in Turso + replica, absent from base and overlay until this very
  update projected it) — empty old is correct; the replica's answer would emit
  suppressions for keys that do not exist in serving.
- `differs` (traced: did:plc:b3bal4vwdhg5c54imx5izbkh): the base serves the
  OLD representation's tokens; suppressing them requires the base's values.
  The replica's fresher values would miss them — the replica-as-source was
  leaving stale prefix entries in this class.

The functional gate for phase 3 is therefore: `projection_skipped = 0`,
`belt_dropped` trending to 0 under the new source, the rename/hide/unhide
smoke checks passing, and one clean promote cycle on v64+. The counters stay
as telemetry, not as a zero-target.

- sync loop becomes: watermark = attached snapshot's `source_watermark` (or the
  last applied `updated_at` in `sync_meta`), page Turso `updated_at > watermark`,
  project into overlay, persist watermark per page. No `actors` table, no
  `actors_stage`, no resync path, no FTS engine (`search.zig`, `ENABLE_LOCAL_FTS`,
  `/debug/fts`), no `SYNC_MAX_LAG_S` self-heal.
- boot with no snapshot: adopt on disk or download; then replay. First-boot
  cost ≈ download (~15 min) + rows since watermark (≈ 300k over 3 days ≈ 30 min).
- `/health`: `ready` = snapshot attached AND sentinel query returns rows;
  `sync_lag_s` = now − last applied `updated_at`; actor count =
  `index_meta.actor_count` + overlay upserts. `/health/freshness` unchanged in
  contract.
- drop the hydrate belt (its data source is gone; the overlay/blob check remains).
- volume: Fly can't shrink. Create a 60 GB volume, deploy against it (data is
  rebuildable from R2), destroy the 100 GB one. **−$6/mo.**

Verify: `just smoke`, `just bench-ranking`, `just bench-search`; kill the
machine mid-replay and confirm it resumes from the persisted watermark;
`/health/freshness` fresh within an hour of boot.

## phase 4 — right-size search (after 2 weeks of phase-3 metrics)

If 14-day peak mem < 50% of 1 GB and load peak < 2: `shared-cpu-2x`/1 GB.
Promote (download + attach + compaction) is the burst to watch. **−$8.88/mo.**

## phase 5 — R2 retention + COSTS.md

Keep the two most recent builds; delete the rest from the indexer flow after a
successful promote. Rewrite COSTS.md's table to the measured numbers above.

## not in this plan

Replacing Turso, adding Postgres, or changing the ingester/indexer. The
research showed the failures live in the replica sync; that is the scope.
