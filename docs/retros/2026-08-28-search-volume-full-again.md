# Retrospective: 2026-08-25 → 08-28 search volume full, empty results for ~3 days

## What happened

The `typeahead-search` volume (50 GB) filled up. Sync from Turso stopped at
**2026-08-25 ~23:10 UTC** (WAL could no longer checkpoint), and on **08-28 09:21**
the scheduled snapshot promote started downloading a 13.5 GB build into a disk
with no room, failed with `no space left on device`, and left the service with
**no snapshot attached** (`build_id: null`). Every search then fell through to
the local-db path, which errored (`lease.query failed`), and the worker returned
`200 {"actors":[]}` for every query. Search was empty for every user from at
least the 09:21 promote until **20:40 UTC** — and stale for the three days before.

It was found by accident: while sampling real profiles for a feature pitch, a
`searchActors?q=nate` came back empty.

The disk math at the moment of failure, from `ls -la /data`:

| file | size |
|---|---|
| `local.db` + WAL | 11.4 + 2.1 GB |
| `index.A.db` (Aug 25, active) | 13.5 GB |
| `index.B.db` (Aug 19, rollback) | 13.1 GB |
| `index.A.db.part…partial` (Aug 28, downloading) | 12.1 GB → needed 13.5 |
| **total** | **≈ 52 GB on a 50 GB volume** |

This is the **same incident as 2026-05-27** (30 GB volume, 8.7 GB snapshot).
The memory note from that day said "keep the volume ≥ ~4× snapshot + local.db;
revisit as the corpus grows". The snapshot grew 8.7 → 13.5 GB and nobody
revisited. There was no cruft this time — the `.partial` sweep from `5ddf529`
worked — the volume was simply outgrown, on schedule.

## Timeline

| Time (UTC) | Event |
|---|---|
| 08-25 09:25 | Snapshot promoted to `index.A.db` (13.5 GB). Disk ~40 GB used. Normal. |
| 08-25 ~23:10 | Last successful incremental sync. WAL grows to 2.1 GB and stops checkpointing; `sync_lag_s` starts climbing. |
| ~08-26 | `/health/freshness` flips `stale:true` (replica lag > 1800 s). The change-only Discord webhook fires once. `/health` still says `ready:true`. |
| 08-28 09:21 | Indexer publishes `b1787907716-dcb8`. Promote watcher starts the download; rclone hits ENOSPC on attempt 1/3. |
| 08-28 10:21 | Machine restarts (no release; fly host event). Process boots on a full disk: no snapshot adopted, sync enters full resync at 22 s per 2000-row batch. |
| 08-28 20:38 | Found: `q=nate` → `[]`. `df` → 100%. |
| 08-28 20:39 | `fly volumes extend … -s 80` (online; fs grew without restart despite fly's "stop and start" message). |
| 08-28 20:40 | `fly machine restart`. Search answers again via local db. **Cost:** the pre-restart resync was at batch 5306 / ~5700 (10.6 M rows) — the restart threw that away. Should have checked resync progress in the logs before restarting. |
| 08-28 20:43 | Extended again to 100 GB for headroom (resync builds an ~11 GB `actors_stage`). |
| 08-28 21:03 | Snapshot `b1787907716-dcb8` attached, sentinel 5/5. `q=nate` served from the index. `just smoke`: all checks passed. Resync apply time drops 67 s → ~7 s per 2000-row batch the moment the download stops — the slowness was disk IO contention with rclone, not the WAL. At ~7 s × ~5,700 batches the resync completes on its own; `/health/freshness` stays `stale` until then. |

| 08-28 ~21:25 → 08-29 01:40 | **Restart loop, unnoticed for 4 h.** `adoptIfPresent` runs `compactOverlay` right after attach; it held the write mutex for one DELETE over the whole sync-derived overlay (tens of millions of prefix rows after 3 days). The resync's batch 2 blocked on `local.lock()`; the sync watchdog saw no progress for 20 min and exited; fly restarted; compaction started over. Logfire shows the same 4-span sequence every 21 min. Search kept serving from the attached snapshot throughout, so nothing user-visible — but the replica could never catch up. |
| 08-29 01:5x | `762fe39`: compaction in 20k-row chunks, mutex released between chunks, progress logged. Deployed to search. |

## Root causes

### 1. A known capacity cliff with no alarm and no owner

The May retro named the rule and the number. Nothing enforced it. Disk usage on
App B is not in `/health`, not in `/health/freshness`, not in any alert. The
first symptom was sync stalling, which *is* watched — but see (2).

**Fix (this incident):** volume 50 → 100 GB. At 13.5 GB snapshots that is
3 × 13.5 + 11.4 + 2.1 + 11 ≈ 65 GB peak, so ~35 GB headroom.

**Fix (still open):** put `disk_free_bytes` for `/data` into `/health` and make
`/health/freshness` go stale when free space < 1.5× snapshot size. The number is
one `statvfs` call; the promote watcher already knows the snapshot size from
`latest.json`.

### 2. One Discord message, three days ago, was the whole alert

`notifyFreshnessChange` deliberately posts only on *state change* (the July
retro: "hourly still-broken messages train you to ignore the channel"). It
worked as designed: one message when lag crossed 1800 s. Then silence for three
days while the service degraded from "stale" to "empty". A single message that
scrolls past is not an escalation.

**Fix (open):** re-post while stale on a widening schedule (1h, 6h, 24h), or
make the worker's `/stats` home tile show the freshness state so it is seen on
every visit. And distinguish *stale* from *serving nothing*: `probe_ok` should
check that a known handle (`zzstoatzz.io`) comes back non-empty, not that the
request returned 200.

### 3. `/health` reported `ready:true` with zero serving capacity

`actors: 11382142` is a count from the local db; `ready` means the process is
up. Neither says "a query will return results". The July retro said exactly this
("component liveness, no outcome check") and added `/health/freshness`; but the
search service's own `/health`, which fly and the worker read, still lies.

**Fix (open):** `ready` should be false when no snapshot is attached AND the
local-db read path last errored.

### 4. An unbounded critical section, invisible until the corpus made it long

`compactOverlay` was one DELETE under the write mutex. Correct, and fast
enough on a routine 3-day overlay. After an outage the overlay is the whole
delta, and "fast enough" became 20+ minutes — longer than the watchdog's
patience — while every other writer waited. The watchdog then did exactly
its job and made things worse: it turned one slow compaction into an infinite
sequence of partial ones.

**Fix (shipped, `762fe39`):** `compactChunk` deletes ≤20k rows per call by
primary key; `LocalDb.compactOverlay` loops, unlocking between chunks, and
logs start / every 50 chunks / done. Rule worth keeping: **no unbounded
statement under the write mutex** — anything proportional to the overlay or
the corpus must chunk and yield.

**Still open:** the watchdog should count compaction as progress (or the
promote runner should defer `compactOverlay` while a resync is in flight).
The chunking removes the deadlock; it does not make the two cooperate.

### 5. Restarting without reading the resync progress first

Operator error, mine. The 10:21 boot had been resyncing for 10 hours and was
~93 % done. `fly logs --no-tail | grep resync` before the restart would have
shown it, and the right move was extend the volume and *wait*. A restart is not
free when the process holds hours of unpersisted progress — the resync
watermark is only written on completion.

## What was fine

- The `.partial` sweep from May worked; the restart cleaned the debris.
- `fly volumes extend` is online; `df` grew immediately.
- The worker's `getProfiles` (Turso-backed) kept working throughout.
- The search index itself was fine — the snapshot `b1787907716-dcb8` was built
  and uploaded on schedule; only the consumer had nowhere to put it.
