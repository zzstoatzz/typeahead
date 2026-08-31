# research: does the search box need the Turso replica?

2026-08-29. Empirical, no design decisions yet. Question: on `typeahead-search`
(App B) there are three copies of the actor corpus — Turso (system of record),
an 11.4 GB sqlite replica of `actors` in `/data/local.db`, and the 13.5 GB
prefix-index snapshot from R2. The last three retros all live in the code that
keeps the replica in sync. What is the replica actually load-bearing for?

## 1. inventory of replica consumers on the search role

Production config (`services/fly.search.toml`): `USE_PREFIX_INDEX=1`,
`ENABLE_LOCAL_FTS=0`, `ENABLE_INDEX_PROMOTE=1`.

| consumer | reads | needed from replica | already in snapshot + overlay? |
|---|---|---|---|
| **search hot path** — `index_search.zig:272` `hydrate()` | `SELECT did, hidden FROM actors WHERE did IN (…)` | existence + `hidden` "safety belt" | **yes, redundantly**: `actor_overlay.hidden` / tombstone (`:283-286`) and the packed blob's `hidden` (`:295`). The comment calls it a belt "while overlay correctness is still being earned". This is the **only** replica touch on the hot path. |
| overlay projection — `sync.zig:1074` `snapshotOldActorRow` | `SELECT handle, display_name FROM actors WHERE did = ?` | old handle/display to diff prefix keys on update/delete | **yes**: the same pair is packed in `actor_overlay.blob` (post-watermark) or `idx.actor_compact_base.blob` (pre-watermark). Not read from there today. |
| `/health` — `LocalDb.zig:610,627` | `sync_meta.actor_count`, `sync_meta.last_sync` | live count, `sync_lag_s` | replica-lifecycle facts; `index_meta.actor_count` is build-time |
| sync loop cursor — `sync.zig:141,619` | `sync_meta.sync_complete`, `last_sync` | its own state | n/a |
| legacy FTS engine — `search.zig:294-367` | `actors` rows, `actors_fts MATCH` | candidates + full rows incl. follower counts | **off in production** and `actors_fts` isn't maintained (`ENABLE_LOCAL_FTS=0`) |
| `/debug/fts` — `server.zig:468` | both tables | debug | n/a |

Two facts that contradict what we believed:

- **When no snapshot is attached, the replica does not serve search.** With
  `USE_PREFIX_INDEX` set, the index engine runs, its `idx.*` queries fail, and
  `/search` returns 500 → the worker's bsky backstop answers. (On 08-28 the
  worker returned `{"actors":[]}` because that backstop path also failed; the
  "search answered via local db" I reported after the restart was the backstop.)
- **The FTS fallback is not a fallback.** It only runs when `USE_PREFIX_INDEX`
  is *unset* (the check is `getenv != null`, so `=0` still routes to the index),
  and its table isn't maintained on the search box.

**Conclusion of the inventory:** the replica's only production job is to be
the *source of old rows* for overlay projection, plus a redundant hidden check.
Everything users see comes from snapshot + overlay.

## 2. what the sync layer costs, measured (logfire, `typeahead-search`)

| span | 7-day count | avg | p95 |
|---|---|---|---|
| `sync.query.resync_batch` | 22,313 | 0.4 s | 0.6 s |
| `sync.apply.resync_batch` | 21,977 | 5.5 s | 43 s |
| `sync.query.updated` (incremental page) | 175 | 0.6 s | 1.6 s |
| `sync.apply.updated` | 158 | 2.2 s | 6.8 s |
| `sync.resync` (full) | 2 | 11.4 h | — |

Search requests are **not instrumented** as spans — only sync and `/health`
are — so the serving-path mix can't be measured. That is a gap to close before
any redesign is judged.

Turso's write rate the incremental must drain: 2k–9k `actors` rows/hour over
the last 96 h (`updated_at` histogram, index range scan, 849k rows read). The
incremental drains a 500-row page in ~3 s ≈ 170 rows/s ≈ 600k/hour — **60×
headroom**. Drain rate is not the problem.

## 3. the sync has been broken since 08-19, not since 08-25

Incremental sync runs per day (`sync.incremental` spans):

```
08-15  219   08-16  278   08-17  264   08-18  269
08-19  118   ← stops ~15:30 UTC (last_sync = 1787137408)
08-20    0   08-21    0   08-22    4 (11.4 h resync)   08-23  0   08-24  0
08-25    4 (11.5 h resync)   08-26  0   08-27  0   08-28  0   08-29  0
```

From 08-20 00:18 the spans show the exact 21-minute cycle diagnosed tonight:
`read_last_sync → resync batch 1 → batch 2 query → silence → watchdog restart`.
The two 11-hour resyncs that *did* complete were on promote days (08-22,
08-25), each followed within hours by the same silence.

Mechanism, now understood end to end:

1. `adoptIfPresent` runs `compactOverlay` under the write mutex after every
   attach (and boot re-attaches). Compaction is one DELETE over every overlay
   row ≤ watermark.
2. Compaction that gets killed never compacts, so the overlay only grows
   (16M+ rows ≤ watermark tonight; a `count(*)` on it exceeded 110 s). Each
   attempt is longer than the last — a ratchet.
3. Once one compaction exceeds the sync watchdog's 20 min, the resync is
   blocked past the watchdog, the process restarts, compaction restarts from
   zero, forever. Lag > 6 h ⇒ every boot chooses resync ⇒ nothing ever
   advances `last_sync`.
4. Ten days of resync attempts (`actors_stage` create/drop, 2.1 GB WAL that
   can't checkpoint) is what filled the 50 GB volume on 08-28 — the disk was
   the *symptom*.

`762fe39` + `f6e7cd0` (chunked, deferred compaction) break step 3. They do
not address step 1/2 (compaction only at promote) or the underlying cost (a
full 11 M-row replica rebuild whenever lag > 6 h, watermark persisted only at
completion).

## 4. what removing the replica would change

If the search box kept **snapshot + overlay only**:

- sync becomes "Turso rows with `updated_at > watermark` → overlay projection".
  The old handle/display comes from overlay-then-base blob (same bytes).
- recovery from any outage becomes: download snapshot (~15 min at 1 GB/min)
  + replay rows newer than its watermark. Bounded by promote cadence: 3 days ×
  ~100k rows/day ≈ 300k rows ≈ 600 pages ≈ **30 min**, not 11 h. Resumable by
  construction (the overlay is the watermark).
- `/data` drops the 11.4 GB `actors`, its WAL, and the 11 GB resync staging:
  peak ≈ 3 × snapshot + overlay. The `actors_fts` table and the whole FTS
  engine go with it (already dead in production).
- what is lost: `/health`'s live actor count (use `index_meta.actor_count` +
  overlay row count), the hidden safety belt (overlay + blob already carry
  `hidden`), and `/debug/fts`.
- what stays: Turso as system of record, the ingester, the indexer on
  heavypad, the promote path, the overlay. No new technology.

## 5. open before deciding

- Instrument `/search`: which path answered (base / overlay / miss), latency.
  Without it "the overlay is correct enough to drop the belt" is a belief.
- Overlay size gauge in `/index/status` (row counts by `source`, rows ≤
  watermark). Tonight's 16M+ was invisible.
- Compaction cadence: prune on every incremental pass (rows ≤ attached
  watermark), not only at promote, so it never ratchets.
- The sync watchdog vs. long phases (resync's FTS build, compaction): a
  "long phase" budget or a heartbeat. Otherwise the same loop recurs in a
  different phase.
- Confirm the bsky backstop behaviour in the worker when the engine 500s —
  on 08-28 it returned `[]`, which is the worst of both.

## addendum 2026-08-31 — phase 2's first day settled the old-row question

The verifier (`snapshotOldVerified`) compared overlay-then-base against the
replica for every projected row. All three disagreement classes, traced to
ground truth, came out the same way: **the replica was the wrong source**.
Replica-missing rows (handle-flap) were the belt drops suppressing live
actors; source-empty rows had no serving presence so an empty old row is
correct; differing rows need the BASE's stale representation to suppress the
keys the base actually serves — the replica's fresher values would miss them.
The comparison designed to earn trust in the new source ended up indicting
the old one in every class.
