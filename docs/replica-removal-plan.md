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
disagree, log + count. Verify: rename/hide/unhide exercise — **run 2026-08-31 (yeadog test account), all
four legs pass**: display rename projected (`n:` overlay inserts), mod-override
hide suppressed from serving, unhide restored, and the restore suppressed the
old display's keys (op=1 → op=0 for every `n:zzstoatzztest*` key) via the NEW
overlay-then-base old-row source. Residue: yeadog's Turso `display_name` keeps
the test value (his real profile has none, and every upsert COALESCE-keeps
non-empty values); serving is unaffected. Remaining: one clean promote cycle
(2026-09-03). Rollback: one-line source switch.

## phase 3 — cut the replica

**Gate met 2026-09-01** — the promote cycle on v65 was clean: `projection_skipped`
0 across 259 passes, `belt_dropped` 0 over 3,070 searches, compaction of
4.99M rows finished `complete:true` in three bounded passes, zero watchdog
restarts, freshness fresh throughout. (The indexer cron is `0 9 */3 * *` —
day-of-month divisible by 3 — so 08-31 and 09-01 were consecutive builds.)
**Cut over 2026-09-02 06:24 UTC** with zero engine downtime: a second machine
was cloned onto a fresh 60 GB volume (cordoned, image swapped to the phase-3
build), it downloaded + attached the snapshot in 20 min from boot, replayed
the 19 h since the snapshot watermark in 47 min (314,968 rows, 118 cleared,
4,421 tombstoned, no errors), answered identically to the old box, passed
`just smoke` alone, and then the old machine + 100 GB volume were destroyed.
So a fresh box recovers from R2 in ≈ 20 min + ≈ 2.5 min per hour of lag —
the earlier "≈ 30 min for 3 days" undercounted the daily write volume
(the nightly enrichment run alone is ~250k rows).

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

What shipped (`services/`):

- sync loop: start = `max(sync_meta.last_sync, snapshot source_watermark)`
  (`resumePoint`, unit-tested), page Turso `updated_at > start − 300s`, project
  into the overlay with the old row from overlay-then-base, persist the
  watermark per page. Deleted: the `actors`/`actors_fts`/`actors_stage` tables,
  bootstrap, resync, the FTS engine (`search.zig` keeps only the response
  shape), `/debug/fts`, the hydrate belt, `ENABLE_LOCAL_FTS`, `USE_PREFIX_INDEX`,
  `SYNC_MAX_LAG_S`, `SYNC_BOOTSTRAP_PAUSE_MS`, `ENABLE_INDEX_PROMOTE` (the
  watcher is now unconditional for the search role). ~900 lines net.
- boot: the sync loop waits (feeding the watchdog) until the promote watcher
  has attached a snapshot — adopted from disk or downloaded. A volume that
  still carries the replica drops those tables and VACUUMs on first open.
- `/health`: `ready` = a validated snapshot is attached (the sentinel runs
  before every attach); `actors` = the attached snapshot's `index_meta`
  count; `sync_lag_s` unchanged (now − last successful pass).
  `/health/freshness` unchanged in contract.
- past tombstone retention (7 d) the pass logs a warning and continues: a
  deletion older than the watermark is baked out by the next promote.

- volume: now 60 GB (`vol_v3gd76k83q1q1qw4`), machine `search-phase3`.
  **−$6/mo, done.**

Verify: `just smoke`, `just bench-ranking`, `just bench-search`; kill the
machine mid-replay and confirm it resumes from the persisted watermark;
`/health/freshness` fresh within an hour of boot.

## phase 4 — right-size search (after 2 weeks of phase-3 metrics)

If 14-day peak mem < 50% of 1 GB and load peak < 2: `shared-cpu-2x`/1 GB.
Promote (download + attach + compaction) is the burst to watch. **−$8.88/mo.**

Evidence so far: the 2026-09-04 viral burst (~20× search volume) peaked at
load 0.21 / mem 19% — search traffic is not the sizing input. See
`cost-scalability.md` → "Scalability under traffic". The remaining unknown is
the first promote on the replica-less box (2026-09-04 09:00 UTC build).

## phase 5 — R2 retention + COSTS.md

Keep the two most recent builds; delete the rest from the indexer flow after a
successful promote. Rewrite COSTS.md's table to the measured numbers above.

## not in this plan

Replacing Turso, adding Postgres, or changing the ingester/indexer. The
research showed the failures live in the replica sync; that is the scope.
